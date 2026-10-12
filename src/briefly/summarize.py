"""Map-Reduce 提炼引擎：周报和完整总结共用。

流程：逐块提取工作项（并发）→ 格式错误的块重试 → 引用校验 → 去重 → 覆盖检查 → 分批合并 → 按调用方的提示词合成。
原则：能用代码保证的不交给模型自觉——每块都被处理（覆盖检查），每条都能在原文找到（引用校验）。
"""
import re
from dataclasses import dataclass, field
from typing import Literal

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, Field

from . import config
from .materials import wrap_material
from .skills import read_prompt
from .tokens import estimate_tokens

CATEGORY_NAMES = {"done": "已完成", "in_progress": "进行中", "issue": "问题风险", "plan": "计划", "other": "其他"}


class _Item(BaseModel):
    category: Literal["done", "in_progress", "issue", "plan", "other"] = Field(
        description="done 已完成 / in_progress 进行中 / issue 问题或风险 / plan 计划或待办 / other 其他"
    )
    point: str = Field(description="一句话概括这一项，数字、日期、人名与原文一致")
    quote: str = Field(description="支撑这一项的原文，逐字照抄")


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

    @property
    def tag(self) -> str:
        return f"{self.material_id}-{self.chunk}"


@dataclass(frozen=True)
class WorkItem:
    category: str
    point: str
    quote: str
    source: str  # 出处：材料号-块号，如 "1-3"


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

    @property
    def processed(self) -> int:
        return self.total - len(self.failed)


def _norm(text: str) -> str:
    """校验引用时忽略空白和 Markdown 转义的反斜杠。"""
    return re.sub(r"[\s\\]+", "", text)


def _extract_messages(source: Source, focus: str, period: str | None) -> list[tuple[str, str]]:
    user = (
        f"简述：{focus.strip() or '（无，提取全部工作内容）'}\n"
        f"周期：{period or '未指定'}\n\n"
        + wrap_material(f"【材料 {source.material_id}《{source.title}》第 {source.chunk} 块】", source.content)
    )
    return [("system", read_prompt("extract")), ("user", user)]


def extract_items(
    model: BaseChatModel, sources: list[Source], focus: str = "", period: str | None = None
) -> ExtractionResult:
    """逐块提取工作项。每块都会处理；失败的块重试 MAP_RETRIES 次，仍失败的记入 failed，不静默丢弃。"""
    result = ExtractionResult(items=[], total=len(sources))
    extractor = model.with_structured_output(Extraction, method="function_calling", include_raw=True)
    pending = list(sources)
    errors: dict[Source, str] = {}
    outputs: dict[Source, Extraction] = {}

    for _ in range(1 + config.MAP_RETRIES):
        if not pending:
            break
        responses = extractor.batch(
            [_extract_messages(s, focus, period) for s in pending],
            config={"max_concurrency": config.MAP_CONCURRENCY},
            return_exceptions=True,
        )
        retry = []
        for source, response in zip(pending, responses):
            if isinstance(response, Exception):
                errors[source] = f"{type(response).__name__}: {response}"
                retry.append(source)
                continue
            result.usage.add(response["raw"])
            if response["parsed"] is None:
                errors[source] = f"输出格式不对：{response['parsing_error']}"
                retry.append(source)
            else:
                outputs[source] = response["parsed"]
        pending = retry

    result.failed = [{"source": s.tag, "error": errors[s][:300]} for s in pending]
    seen: set[str] = set()
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
            elif quote in seen:  # 相邻块有重叠，同一句原文可能被提取两次
                continue
            else:
                seen.add(quote)
                result.items.append(WorkItem(item.category, item.point.strip(), item.quote.strip(), source.tag))
    return result


def format_items(items: list[WorkItem]) -> str:
    return "\n".join(
        f"- 【{CATEGORY_NAMES.get(i.category, '其他')}】{i.point}〔{i.source}〕\n  原文：{i.quote}" for i in items
    )


@dataclass
class Synthesis:
    text: str
    merge_levels: int  # 分批合并了几层；0 表示一次合成
    usage: Usage


def synthesize(
    model: BaseChatModel, system_prompt: str, request: str, items: list[WorkItem], budget: int | None = None
) -> Synthesis:
    """按调用方给的提示词，把工作项合成最终文本。工作项超出预算时先分批合并（分层摘要），再合成。"""
    budget = budget or config.REDUCE_BUDGET_TOKENS
    usage = Usage()
    text = format_items(items) or "（没有提取到相关工作项）"
    levels = 0
    while estimate_tokens(text) > budget:
        batches = _split_lines(text, budget // 2)
        if len(batches) < 2:
            break
        merged = model.batch(
            [[("system", read_prompt("merge")), ("user", batch)] for batch in batches],
            config={"max_concurrency": config.MAP_CONCURRENCY},
        )
        for message in merged:
            usage.add(message)
        new_text = "\n".join(m.text.strip() for m in merged)
        if estimate_tokens(new_text) >= estimate_tokens(text):  # 合并没有变短，避免死循环
            break
        text, levels = new_text, levels + 1

    final = model.invoke([("system", system_prompt), ("user", f"{request}\n\n工作项（每条后的〔〕为出处）：\n{text}")])
    usage.add(final)
    return Synthesis(final.text.strip(), levels, usage)


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
