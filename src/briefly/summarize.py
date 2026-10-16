"""Map-Reduce 提炼引擎：周报和完整总结共用。

流程：逐块提取工作项（并发）→ 格式错误的块重试 → 引用校验 → 去重 → 覆盖检查 → 分批合并 → 按调用方的提示词合成
→ 编号换回出处并检查漏写。
原则：能用代码保证的不交给模型自觉——每块都被处理（输入覆盖），每条都能在原文找到（引用校验），
每个工作项都写进了结果或被明确列出（输出覆盖）。
"""
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, Field, field_validator

from . import config
from .materials import wrap_material
from .skills import read_prompt
from .context_builder import input_limit
from .context_store import ContextStore, content_hash
from .tokens import estimate_tokens

# 每次调用模型后的回调：(任务类型 map/reduce/synthesize, 估算输入 tokens, 模型返回的消息, 出处如 "1-3")，用于观测
OnCall = Callable[[str, int, object, str], None]

log = logging.getLogger(__name__)

CATEGORY_NAMES = {"done": "已完成", "in_progress": "进行中", "issue": "问题风险", "plan": "计划", "other": "其他"}


class _Item(BaseModel):
    category: Literal["done", "in_progress", "issue", "plan", "other"] = Field(
        description="done 已完成 / in_progress 进行中 / issue 问题或风险 / plan 计划或待办 / other 其他"
    )
    point: str = Field(description="一句话概括这一项，数字、日期、人名与原文一致")
    quote: str = Field(description="支撑这一项的原文，逐字照抄")
    risk_reason: str = Field(
        default="",
        description="仅 issue 填写：原文中写明问题、阻碍、异常、延期或依赖的那部分文字，逐字照抄；其他类别留空",
    )
    subject: str = Field(default="", description="仅当这一项写的是某个对象的日期、金额、数量、状态、负责人或进度时填写：对象的最短名称（如项目名、任务名）")
    attribute: Literal["", "date", "amount", "quantity", "status", "owner", "progress"] = Field(
        default="", description="对应的属性：date 日期 / amount 金额 / quantity 数量 / status 状态 / owner 负责人 / progress 进度；不属于这几类留空"
    )
    aspect: str = Field(default="", description="这个值具体指什么（如日期的“上线”“提测”“交付”，金额的“预算”），两三个字")
    value: str = Field(default="", description="属性的值，从原文逐字照抄，保留单位、币种和精度（如“10 月 20 日”“8000 元”“60%”）")
    importance: Literal["high", "medium", "low"] = Field(
        default="medium",
        description="按“用途”判断的候选重要度：high 读者必须知道（结果、决定、关键数据、问题）/ medium 一般工作 / low 日常琐事",
    )


    # 次要字段宽松解析：取值不合法时置空或用默认值，不让一个次要字段拖垮整块的提取（核心字段 category / point / quote 仍严格校验）
    # 枚举仍保留在 schema 里给模型看；DeepSeek 等 OpenAI 兼容接口不支持严格 JSON Schema，取值偶尔会越界
    @field_validator("attribute", mode="before")
    @classmethod
    def _lenient_attribute(cls, value):
        if value in ("date", "amount", "quantity", "status", "owner", "progress", ""):
            return value
        log.info("提取结果的 attribute 取值不合法（%r），已置空", value)
        return ""

    @field_validator("importance", mode="before")
    @classmethod
    def _lenient_importance(cls, value):
        mapping = {"高": "high", "中": "medium", "低": "low", "重要": "high", "一般": "medium", "次要": "low"}
        value = mapping.get(str(value).strip(), str(value).strip().lower()) if value is not None else "medium"
        if value not in ("high", "medium", "low"):
            log.info("提取结果的 importance 取值不合法（%r），按 medium 处理", value)
            return "medium"
        return value

    @field_validator("risk_reason", "subject", "aspect", "value", mode="before")
    @classmethod
    def _lenient_text(cls, value):
        return "" if value is None else str(value)


class Extraction(BaseModel):
    """从一段材料中提取的工作项。"""

    items: list[_Item] = Field(description="工作项；没有相关内容时为空列表")


@dataclass(frozen=True)
class Source:
    """一块待提取的原文。"""

    material_id: int
    chunk: int  # 块号，从 1 开始
    title: str
    content: str
    offset: int = 0  # 这一块在材料全文中的起始位置，用于记录事实的原文位置

    @property
    def tag(self) -> str:
        return f"{self.material_id}-{self.chunk}"


@dataclass(frozen=True)
class WorkItem:
    category: str
    point: str
    quote: str
    source: str  # 出处：材料号-块号，如 "1-3"
    risk_reason: str = ""  # 问题风险的原文依据（逐字）
    needs_review: bool = False  # 标为问题风险，但依据缺失或在原文中找不到：不能写进【问题与风险】，请用户确认
    fact_id: str = ""  # 稳定的事实编号：材料号-块号.序号（#n 只是一次合成里的临时编号）
    span_start: int = -1  # 原文摘录在材料全文中的起始位置；-1 表示只能按忽略空白匹配到，位置不精确
    importance: str = "medium"  # 模型给的候选重要度，不是客观事实；标准随用途（周报、完整总结）不同
    subject: str = ""  # 关键字段（日期、金额、数量、状态、负责人、进度）的主体、属性和值，用于发现多来源冲突
    attribute: str = ""
    value: str = ""
    aspect: str = ""  # 属性的具体含义（上线 / 提测 / 交付……），避免把不同的日期当成同一属性
    group: str = ""  # 所属待办（父待办 › 子待办），周报按父待办分组；不是工作内容本身

    @property
    def verified_risk(self) -> bool:
        return self.category == "issue" and not self.needs_review


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, message) -> None:
        self.calls += 1
        meta = getattr(message, "usage_metadata", None) or {}
        self.input_tokens += meta.get("input_tokens", 0)
        self.output_tokens += meta.get("output_tokens", 0)


@dataclass
class ExtractionResult:
    items: list[WorkItem]
    total: int  # 应处理的块数
    failed: list[dict] = field(default_factory=list)  # 重试后仍失败的块：{source, error}
    dropped: list[dict] = field(default_factory=list)  # 被丢弃的条目：{source, point, quote, reason}
    usage: Usage = field(default_factory=Usage)
    cached: int = 0  # 直接用了缓存结果的块数
    incomplete: list[dict] = field(default_factory=list)  # 提取遗漏：原文摘录里有、要点里都没写的数字等，{fact_ids, quote, missing}

    @property
    def processed(self) -> int:
        return self.total - len(self.failed)


def _norm(text: str) -> str:
    """校验引用时忽略空白和 Markdown 转义的反斜杠。"""
    return re.sub(r"[\s\\]+", "", text)


def _extract_messages(
    source: Source, focus: str, period: str | None, prompt: str = "extract", purpose: str = ""
) -> list[tuple[str, str]]:
    user = (
        f"简述（用户说明的重点，不是筛选条件）：{focus.strip() or '（无）'}\n"
        f"周期：{period or '未指定'}\n"
        + (f"用途：{purpose}\n" if purpose else "")
        + "\n"
        + wrap_material(f"【材料 {source.material_id}《{source.title}》第 {source.chunk} 块】", source.content)
    )
    return [("system", read_prompt(prompt)), ("user", user)]


def _cache_key(source: Source, focus: str, period: str | None, prompt: str = "extract", purpose: str = "") -> str:
    version = "extract-v1" if prompt == "extract" else f"{prompt}-v1"
    parts = [version, read_prompt(prompt), focus.strip(), period or "", source.content]
    return content_hash(*parts, purpose) if purpose else content_hash(*parts)


def _message_tokens(messages: list[tuple[str, str]]) -> int:
    return sum(estimate_tokens(text) + 4 for _, text in messages)


def extract_items(
    model: BaseChatModel,
    sources: list[Source],
    focus: str = "",
    period: str | None = None,
    cache: ContextStore | None = None,
    on_call: OnCall | None = None,
    prompt: str = "extract",
    purpose: str = "",
) -> ExtractionResult:
    """逐块提取工作项（prompt 为提取说明的提示词名，文件追踪用 evidence_extract）。每块都会处理；失败的块重试 MAP_RETRIES 次，仍失败的记入 failed，不静默丢弃。

    每块是一次独立的调用，只带提取说明和这一块原文，不带其他块和对话历史，单次输入不随块数增长。
    传了 cache 时，提取结果按（块内容、关注点、周期、提示词）缓存：中途失败后重跑、同一材料再次提炼时不重算。
    """
    result = ExtractionResult(items=[], total=len(sources))
    extractor = model.with_structured_output(Extraction, method="function_calling", include_raw=True)
    errors: dict[Source, str] = {}
    outputs: dict[Source, Extraction] = {}
    pending = []
    for source in sources:
        cached = cache.cached_extraction(_cache_key(source, focus, period, prompt, purpose)) if cache else None
        if cached is not None:
            outputs[source] = Extraction(items=[_Item(**i) for i in cached])
            result.cached += 1
            continue
        tokens = _message_tokens(_extract_messages(source, focus, period, prompt, purpose))
        if tokens > input_limit():  # 块大小由分块决定，正常不会发生；发生时不硬发
            errors[source] = f"这一块约 {tokens} tokens，超出单次输入上限 {input_limit()}"
            continue
        pending.append(source)
    over_budget = [s for s in sources if s in errors]

    for _ in range(1 + config.MAP_RETRIES):
        if not pending:
            break
        requests = [_extract_messages(s, focus, period, prompt, purpose) for s in pending]
        responses = extractor.batch(requests, config={"max_concurrency": config.MAP_CONCURRENCY}, return_exceptions=True)
        retry = []
        for source, request, response in zip(pending, requests, responses):
            if isinstance(response, Exception):
                errors[source] = f"{type(response).__name__}: {response}"
                retry.append(source)
                continue
            result.usage.add(response["raw"])
            if on_call:
                on_call("map", _message_tokens(request), response["raw"], source.tag)
            if response["parsed"] is None:
                errors[source] = f"输出格式不对：{response['parsing_error']}"
                retry.append(source)
            else:
                outputs[source] = response["parsed"]
                if cache:
                    key = _cache_key(source, focus, period, prompt, purpose)
                    cache.cache_extraction(key, [i.model_dump() for i in response["parsed"].items])
        pending = retry
    pending = over_budget + pending

    result.failed = [{"source": s.tag, "error": errors[s][:300]} for s in pending]
    quoted_in: dict[str, str] = {}  # 引用 → 最先出现在哪一块
    for source in sources:  # 按原文顺序输出
        if source not in outputs:
            continue
        content = _norm(source.content)
        for item in outputs[source].items:
            quote = _norm(item.quote)
            if not quote or quote not in content:
                result.dropped.append(
                    {"source": source.tag, "point": item.point, "quote": item.quote, "reason": "引用在原文中找不到"}
                )
            elif quoted_in.get(quote, source.tag) != source.tag:
                # 相邻块有重叠，同一句原文会在两块里各提取一次，只留先出现的；
                # 同一块里同一句拆出的几项（一句话写了几件事）都保留
                continue
            else:
                quoted_in.setdefault(quote, source.tag)
                reason = item.risk_reason.strip()
                # 问题风险必须有原文依据：依据为空或在原文里找不到的，标为待复核（Pydantic 只能保证字段存在，不能保证语义对）
                review = item.category == "issue" and not (reason and _norm(reason) in content)
                k = sum(1 for i in result.items if i.source == source.tag) + 1
                at = source.content.find(item.quote.strip())
                result.items.append(
                    WorkItem(
                        item.category,
                        item.point.strip(),
                        item.quote.strip(),
                        source.tag,
                        reason,
                        review,
                        f"{source.tag}.{k}",
                        source.offset + at if at >= 0 else -1,
                        item.importance,
                        item.subject.strip(),
                        item.attribute,
                        item.value.strip(),
                        item.aspect.strip(),
                    )
                )
    result.incomplete = map_omissions(result.items)
    return result


# 硬信息：数字、日期、百分比、英文标识、书名号。用来判断“写全了没有”——只比较这些，不比较措辞，误报少
_HARD = re.compile(r"\d+(?:[.:：/\-]\d+)*\s*%?|[A-Za-z][A-Za-z0-9_\-]{1,}|《[^》]+》")


def hard_terms(text: str) -> list[str]:
    return list(dict.fromkeys(re.sub(r"\s+", "", t) for t in _HARD.findall(text)))


def missing_terms(terms: list[str], text: str) -> list[str]:
    flat = re.sub(r"\s+", "", text)
    return [t for t in terms if t not in flat]


def map_omissions(items: list[WorkItem]) -> list[dict]:
    """提取遗漏：同一段原文摘录里的硬信息，在由它提取出的所有要点里都没有出现。

    例：原文“完成登录模块，修复 Token 过期问题，新增 3 个权限校验测试”只提取成“完成登录模块”，Token、3 就是遗漏。
    """
    groups: dict[str, list[WorkItem]] = {}
    for item in items:
        if not item.fact_id:
            continue  # 不是从原文提取的（如用户已确认的工作记录）：没有“提取遗漏”可言
        groups.setdefault(_norm(item.quote), []).append(item)
    out = []
    for group in groups.values():
        lost = missing_terms(hard_terms(group[0].quote), " ".join(i.point for i in group))
        if lost:
            out.append({"fact_ids": [i.fact_id for i in group], "quote": group[0].quote, "missing": lost})
    return out


REVIEW_LABEL = "待复核"  # 标为问题风险但没有原文依据


def _label(item: WorkItem) -> str:
    if item.category == "issue" and item.needs_review:
        return REVIEW_LABEL
    return CATEGORY_NAMES.get(item.category, "其他")


def format_items(items: list[WorkItem]) -> str:
    """给模型看的工作项：用编号 #n 标识，模型引用编号，代码再换回出处并检查有没有漏写。

    问题风险附原文依据；同一句原文拆出的几项（如“下周要做 X，但 Y 可能影响上线”拆成待办和风险）注明关联，分别写进各自栏目。
    """
    first_with_quote: dict[str, int] = {}
    lost = {o["fact_ids"][0]: o["missing"] for o in map_omissions(items)}
    lines = []
    for n, i in enumerate(items, start=1):
        line = f"- #{n} 【{_label(i)}】{i.point}\n  原文：{i.quote}"
        if i.fact_id in lost:  # 提取时可能遗漏：让写作时回到原文写全
            line += f"\n  （原文中还写了 {'、'.join(lost[i.fact_id])}，概括里没有，写的时候请按原文写全）"
        if i.verified_risk and i.risk_reason:
            line += f"\n  风险依据：{i.risk_reason}"
        if i.group:
            line += f"\n  所属待办：{i.group}"
        key = _norm(i.quote)
        if key in first_with_quote:
            line += f"\n  （与 #{first_with_quote[key]} 出自同一句原文，但是另一项内容，同样要写进周报，按自己的类别放进对应栏目）"
        else:
            first_with_quote[key] = n
        lines.append(line)
    return "\n".join(lines)


_REF = re.compile(r"〔([^〕]*#\d+[^〕]*)〕")
_MISMATCHED_REF = re.compile(r"〔\s*(#\d+(?:\s*[、,，]\s*#\d+)*)\s*】|【\s*(#\d+(?:\s*[、,，]\s*#\d+)*)\s*〕")
_BARE_REF = re.compile(r"(?<![\w#〔])#(\d+)(?!\d)")  # 模型偶尔不加〔〕直接写 #13


def resolve_refs(text: str, items: list[WorkItem]) -> tuple[str, set[int], list[int]]:
    """把模型写的编号标记〔#3〕〔#3、#5〕换回出处〔1-3〕〔2-5〕；其中的“简述”等其他标记原样保留。

    返回 (换好的文本, 被引用的编号, 不存在的编号)。
    """
    used: set[int] = set()
    invalid: list[int] = []
    # 模型偶尔把括号写错（〔#17】、【#3〕）：先统一成〔〕，否则编号换不回出处，还会被当成来源不明的数字
    text = _MISMATCHED_REF.sub(lambda m: f"〔{m.group(1) or m.group(2)}〕", text)

    def replace(match: re.Match) -> str:
        tags = []
        for n in map(int, re.findall(r"#(\d+)", match.group(1))):
            if 1 <= n <= len(items):
                used.add(n)
                tags.append(items[n - 1].source)
            else:
                invalid.append(n)
        rest = re.sub(r"#\d+", "", match.group(1)).strip(" 、,，;；")
        return "".join(f"〔{t}〕" for t in dict.fromkeys(tags)) + (f"〔{rest}〕" if rest else "")

    def replace_bare(match: re.Match) -> str:
        n = int(match.group(1))
        if 1 <= n <= len(items):
            used.add(n)
            return f"〔{items[n - 1].source}〕"
        return match.group(0)  # 不在编号范围内的可能是正文本身的内容（如 #1 号议题），原样保留

    text = _REF.sub(replace, text)
    return _BARE_REF.sub(replace_bare, text), used, invalid


@dataclass
class Synthesis:
    text: str  # 出处已从编号换回〔材料号-块号〕
    merge_levels: int  # 分批合并了几层；0 表示一次合成
    usage: Usage
    uncovered: list[WorkItem] = field(default_factory=list)  # 没有写进结果的工作项
    invalid_refs: list[int] = field(default_factory=list)  # 模型引用了不存在的编号
    coverage_checked: bool = True  # 模型完全没按编号标注时为 False，无法判断有没有漏写
    problems: list[str] = field(default_factory=list)  # 调用方检查（check）重写一次后仍存在的问题
    rewritten: bool = False  # 是否因检查不通过重写过
    raw: str = ""  # 换回出处之前的结果（保留 #编号），用于逐行核对每个事实写在哪里


def synthesize(
    model: BaseChatModel,
    system_prompt: str,
    request: str,
    items: list[WorkItem],
    budget: int | None = None,
    aside_heading: str | None = None,
    on_call: OnCall | None = None,
    check: Callable[[str], list[str]] | None = None,
) -> Synthesis:
    """按调用方给的提示词，把工作项合成最终文本。工作项超出预算时先分批合并（分层摘要），再合成。

    aside_heading：结果中这个标题之后的内容（如周报后的【给用户的提醒】）不算正文，
    其中引用的编号照样换回出处，但不算“已写进结果”。
    check：对合成结果（编号尚未换回出处）做规则检查，返回问题列表；有问题时带着问题重写一次，
    仍有的问题记入 Synthesis.problems，由调用方提示用户核对（不额外调用模型审核）。
    """
    budget = budget or config.REDUCE_BUDGET_TOKENS
    usage = Usage()
    text = format_items(items) or "（没有提取到相关工作项）"
    levels = 0
    while estimate_tokens(text) > budget:
        batches = _split_lines(text, budget // 2)
        if len(batches) < 2:
            break
        requests = [[("system", read_prompt("merge")), ("user", batch)] for batch in batches]
        merged = model.batch(requests, config={"max_concurrency": config.MAP_CONCURRENCY})
        for request_messages, message in zip(requests, merged):
            usage.add(message)
            if on_call:
                on_call("reduce", _message_tokens(request_messages), message, f"第 {levels + 1} 层")
        new_text = "\n".join(m.text.strip() for m in merged)
        if estimate_tokens(new_text) >= estimate_tokens(text):  # 合并没有变短，避免死循环
            break
        text, levels = new_text, levels + 1

    final_messages = [("system", system_prompt), ("user", f"{request}\n\n工作项（每条的 #编号 用于标注出处）：\n{text}")]
    final_tokens = _message_tokens(final_messages)
    if final_tokens > input_limit():  # 分批合并后仍放不下：明确报错，不截断工作项硬发
        raise ValueError(
            f"要点过多，分批合并后仍有约 {final_tokens} tokens，超出单次输入上限 {input_limit()}；"
            "请缩小材料范围或写明关注点后重试"
        )
    final = model.invoke(final_messages)
    usage.add(final)
    if on_call:
        on_call("synthesize", final_tokens, final, "")
    output = final.text.strip()
    problems = check(output) if check else []
    rewritten = False
    if problems:
        keep = f"，{aside_heading}部分也要保留" if aside_heading and aside_heading in output else ""
        feedback = f"检查发现以下问题，请改正后重新输出完整结果（格式和其他内容不变{keep}）：\n" + "\n".join(f"- {p}" for p in problems)
        retry_messages = [*final_messages, ("assistant", output), ("user", feedback)]
        second = model.invoke(retry_messages)
        usage.add(second)
        if on_call:
            on_call("synthesize", _message_tokens(retry_messages), second, "重写")
        second_output = second.text.strip()
        if aside_heading and aside_heading in output and aside_heading not in second_output:
            # 重写时漏掉了正文之后的部分（如给用户的提醒）：接回初稿的，不让提醒因为重写丢失
            second_output += "\n\n" + aside_heading + output.partition(aside_heading)[2]
        second_problems = check(second_output)
        if len(second_problems) <= len(problems):  # 重写没有变好就保留原稿
            output, problems, rewritten = second_output, second_problems, True
    body, heading, aside = output.partition(aside_heading) if aside_heading else (output, "", "")
    resolved, used, invalid = resolve_refs(body, items)
    if heading:
        aside_text, _, aside_invalid = resolve_refs(aside, items)
        resolved, invalid = f"{resolved}{heading}{aside_text}", invalid + aside_invalid
    checked = bool(used) or not items
    uncovered = [item for n, item in enumerate(items, start=1) if n not in used] if checked else []
    return Synthesis(resolved, levels, usage, uncovered, invalid, checked, problems, rewritten, output)


def _split_lines(text: str, budget: int) -> list[str]:
    """按行切成若干批，每批不超过预算；“原文：”行跟着上一条走，不拆开。"""
    entries: list[str] = []
    for line in text.splitlines():
        if entries and not line.startswith("- "):
            entries[-1] += "\n" + line
        else:
            entries.append(line)
    batches, current, size = [], [], 0
    for entry in entries:
        cost = estimate_tokens(entry)
        if current and size + cost > budget:
            batches.append("\n".join(current))
            current, size = [], 0
        current.append(entry)
        size += cost
    if current:
        batches.append("\n".join(current))
    return batches
