"""输出检查：发现最终回答、周报里混入了内部处理信息（块号、Chunk、程序出处标记等）。

只检测、不删除：原文可能真的写着“第 3 块”之类的话，所以匹配到的内容如果在原材料或用户输入里出现过，就不算泄漏。
检测到后由调用方决定：对话重试一次（InternalTermCheck），周报写进系统提示供人工核对。不额外调用模型做审核。
"""
import logging
import re

from langchain.agents.middleware import AgentMiddleware, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

log = logging.getLogger(__name__)


def reply_char_limit(messages) -> int | None:
    """从用户原始要求读取明确字数上限，不读取工具、助手或标明的材料正文。

    默认沿用到用户更改；“仅本次/这次/本轮”只约束当轮。自然语言中未能
    确定识别的要求仍交给模型，不从上下限范围或材料里的数字猜测。
    """
    persistent = current = None
    for message in messages:
        if not isinstance(message, HumanMessage) or message.additional_kwargs.get("internal"):
            continue
        current = persistent
        text = message.additional_kwargs.get("display", message.text)
        text = re.sub(r"```.*?```", "", text, flags=re.S)
        text = re.split(r"(?:原文|材料|正文|待处理内容)\s*[:：]|【材料开始】", text, maxsplit=1)[0]
        text = re.sub(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"\n]*"|\x27[^\x27\n]*\x27|`[^`\n]*`', "", text)
        text = re.sub(r"(?:不要|不用|不必|无需)\s*(?:再)?\s*(?:控制在|限制在|遵守)\s*\d+\s*字(?:以内|以下|内)?", "取消字数限制", text)
        text = re.sub(r"\d+\s*[-—–~～至到]\s*\d+\s*字(?:以内|以下|内)?", "", text)
        local = bool(re.search(r"仅本次|这次|本轮", text))
        if re.search(r"(?:取消|解除|(?:不|不用|不必|无需|不需要|别)\s*(?:再)?\s*(?:限制|控制|限|管))\s*(?:输出|回复|回答)?\s*(?:的)?\s*字数(?:限制|上限)?"
                     r"|字数\s*(?:不限|(?:不用|不必|无需|不需要|不再)\s*(?:限制|控制)|随意)", text):
            current = None
            if not local:
                persistent = None
        matches = list(re.finditer(r"(?:不要超过|不超过|最多|控制在|限制在|精简到|压缩到)\s*(\d{1,5})\s*字(?:以内|以下|内)?|(\d{1,5})\s*字(?:以内|以下)", text))
        if matches:
            current = int(matches[-1].group(1) or matches[-1].group(2)) or None
            if not local:
                persistent = current
    return current


def reply_chars(text: str) -> int:
    return len(re.sub(r"\s+", "", text))

INTERNAL_PATTERNS = [
    re.compile(r"第\s*\d+\s*(?:[–—\-~～至到]\s*\d+\s*)?块"),  # 第 3 块、第 7–9 块
    re.compile(r"共\s*\d+\s*块"),
    re.compile(r"\d+\s*/\s*\d+\s*块"),
    re.compile(r"块号"),
    re.compile(r"(?i)\bchunks?\b"),
    re.compile(r"(?i)\bmap[\s-]?reduce\b"),
    re.compile(r"\bmaterial_id\b|\btool_call\b|\bsummarize_materials\b|\bread_material\b|\bget_outline\b"),
    re.compile(r"〔\d+-\d+(?:\s*[、,，]\s*\d+-\d+)*〕"),  # 程序内部的出处标记〔材料号-块号〕
    # 评论程序标注的出处（“文档自身出处标注不一致”）：原文没有“出处”二字时，说的就是工具结果里的〔〕（2026-10-15 实测）
    re.compile(r"出处(?:标注|标记)"),
    # 材料库编号：程序给模型的材料都写作“材料 3《标题》”，用户看不到这个编号（2026-10-22 实测：“材料1《meeting.txt》”）
    re.compile(r"材料\s*\d+\s*《[^》\n]{0,80}》?"),
    re.compile(r"材料\s*(?:编号|ID|id)\s*[:：为是]?\s*\d+|(?:已|转)?存为材料\s*\d+"),
]

# 程序拼进用户消息的材料编号（附件标题、材料清单、长输入转存说明）：不是用户写的，不能用来豁免泄漏
PROGRAM_MATERIAL_REF = re.compile(r"(?<=材料)\s*\d+(?=\s*《)|(?<=存为材料)\s*\d+")

RETRY_HINT = (
    "【系统检查】上面的回答中出现了程序内部的处理信息：{terms}。这些是工具调用和检索用的信息（块号、程序出处标记、工具名、材料编号等），"
    "不是材料内容，用户也看不懂。请重写回答：内容不变，去掉这些信息；需要说明出处时用材料标题和章节名，不写材料编号。直接输出重写后的完整回答。"
)


CONFLICT_HINT = (
    "【系统检查】工具结果列出了不同材料之间看不出哪个为准的说法，回答里缺少：{missing}。"
    "请重写回答：同时写出各个说法并说明材料之间不一致，不要只写其中一个；其他内容不变。直接输出重写后的完整回答。"
)
DATE_HINT = (
    "【系统检查】回答里相对时间换算成的日期不对：{problems}。请调用 resolve_date 重新换算：材料里的相对时间以材料中的日期为基准，"
    "把原文日期照抄为 base_date（原文没写年份就不要补年份）；材料没有日期时按今天。只改正这些日期，其他内容不变，直接输出重写后的完整回答。"
)
_ATTR = {"date": "日期", "amount": "金额", "quantity": "数量", "status": "状态", "owner": "负责人", "progress": "进度"}


def turn_conflicts(messages) -> list[dict]:
    """本轮（最后一条用户消息之后）工具结果里记录的、看不出哪个为准的冲突（来自 summarize_materials 的 artifact）。"""
    start = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage) and not m.additional_kwargs.get("internal")), default=-1)
    out = []
    for m in messages[start + 1 :]:
        if isinstance(m, ToolMessage) and isinstance(m.artifact, dict):
            out += [c for c in m.artifact.get("conflicts", []) if c.get("resolution") == "unresolved"]
    return out


def missing_conflict_values(answer: str, conflicts: list[dict]) -> list[str]:
    """回答里缺少的冲突原值；纯日期容忍分隔符变化，不用散落数字代替事实。"""
    from .conflicts import value_in
    missing = []
    for c in conflicts:
        for value in c.get("values", []):
            if value_in(answer,value):
                continue
            missing.append(f"{c.get('subject', '')} 的{_ATTR.get(c.get('attribute'), '')}“{value}”")
    return missing


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


def find_internal_terms(text: str, sources: list[str] | None = None) -> list[str]:
    """返回 text 中疑似内部处理信息的片段（去重）；在 sources（原材料、用户输入）里也出现过的不算。"""
    haystack = _squash("\n".join(sources or []))
    found: list[str] = []
    for pattern in INTERNAL_PATTERNS:
        for m in pattern.finditer(text):
            term = m.group(0)
            if _squash(term) in haystack or term in found:
                continue
            found.append(term)
    return found


_PERSON = r"(?:我(?:已经|已)|(?:已经|已)(?:为你|帮你|替你))(?:成功)?(?:为你|帮你|替你)?(?:成功)?"
_WRITE = r"(?:保存|创建|添加|新建|发送|发出|提交|修改|更新)"
_OBJECT = r"(?:待办|任务|周报|邮件|状态)"
_SUCCESS = re.compile(
    _PERSON + _WRITE + r"[^。！？，；\n]{0,30}?" + _OBJECT
    + "|" + _PERSON + r"(?:将|把)[^。！？，；\n]{0,30}?" + _OBJECT + r"(?:全部|都|成功)?" + _WRITE
)


def false_success_claims(text: str) -> list[str]:
    """仅拦第一人称已完成业务操作；引用、第三人称原文及操作指引不算。"""
    text = re.sub(r"```.*?```|`[^`\n]*`|“[^”]*”|「[^」]*」|『[^』]*』|\"[^\"\n]*\"", "", text, flags=re.S)
    text = re.sub(r"(?m)^\s*>.*$", "", text)
    return list(dict.fromkeys(m.group(0) for m in _SUCCESS.finditer(text)))


def annotate_evidence(answer, messages):
    from .evidence_gaps import turn_evidence_gaps
    gaps = turn_evidence_gaps(messages, answer.response_metadata.get("context_omitted_turns", 0))
    if gaps:
        answer.response_metadata["evidence_gaps"] = gaps


class InternalTermCheck(AgentMiddleware):
    """最终回答（不再调用工具的那次）的检查，有问题时带着提示重试一次；仍有则保留回答并记日志，供人工排查。

    - 含内部处理信息（块号等，Bug 1）；
    - 本轮工具结果里有看不出哪个为准的冲突，回答却只写了其中一种说法（Agent 改写工具结果时把矛盾“统一”了，Bug 4）。
    也核对明确字数上限、完整提炼事实的事项对应关系及明确要求保留的章节编号。
    事实检查仅覆盖工具已核实的重要字段，不是通用语义裁判；所有问题合并成最多一次重试。

    sources(request) 返回用户原话、会话涉及材料与本轮工具结果。放在 ContextBuilder 外层：重试也经过预算检查。
    """

    def __init__(self, sources, length_limit=None) -> None:
        super().__init__()
        self.sources = sources
        self.length_limit = length_limit or (lambda request: reply_char_limit(request.messages))

    def wrap_model_call(self, request, handler):
        response = handler(request)
        answer = _final_answer(response)
        if answer is None:
            return response
        annotate_evidence(answer, request.messages)
        claims = false_success_claims(answer.text)
        sources = self._sources(request)  # 一次最终回答只取一次，重试沿用同一依据
        conflicts = turn_conflicts(request.messages)
        terms = find_internal_terms(answer.text, sources)
        missing = missing_conflict_values(answer.text, conflicts)
        fidelity = self._fidelity(answer.text,request, sources)
        dates = self._dates(answer.text, request, sources, record=True)
        limit = self.length_limit(request)
        too_long = limit is not None and reply_chars(answer.text) > limit
        if not terms and not missing and not too_long and not fidelity and not dates and not claims:
            return response
        log.warning("回答检查未通过（内部信息 %s，缺少的冲突说法 %s，超出字数上限 %s，事实/编号问题 %s 项，日期换算错误 %s 处，虚报操作成功 %s 处），重试一次", terms, missing, limit if too_long else None,len(fidelity), len(dates), len(claims))
        hints = []
        if claims:
            hints.append("【系统检查】你声称已经保存、发送或修改业务数据，但你只有只读工具，没有执行这些操作的能力。请重写：待办需在结构化预览中由用户勾选确认保存，正式周报需在周报页审核生成；邮件等外发需由用户在对应页面明确操作。不要声称操作已完成，也不要把检查说明写进回答。只输出完整回答。")
        if terms:
            hints.append(RETRY_HINT.format(terms="、".join(terms)))
        if missing:
            hints.append(CONFLICT_HINT.format(missing="、".join(missing)))
        if fidelity:
            hints.append("【系统检查】用户本轮明确要求保留事实或原文章节编号，已核实的要点存在遗漏或关联变化："+"；".join(fidelity)+"。请按原始事项保留负责人、日期、状态、否定和编号；数字出现在其他事项下不能代替。若工具内容被截断，请用read_tool_result继续读取。只输出重写后的回答，不把检查说明写进正文。")
        if dates:
            hints.append(DATE_HINT.format(problems="；".join(dates)))
        if too_long:
            hints.append(f"上一条回复共 {reply_chars(answer.text)} 字，违反用户仍有效的整条回复最多 {limit} 字要求。请重新压缩，所有标题、正文、说明和注合计不得超过 {limit} 字（不计空白）。保留原文事实、数字、日期和确定程度；精简修改说明，不添加可选的注，不截断或删除关键事实。")
        hint = HumanMessage("\n".join(hints), additional_kwargs={"internal": True})
        retry = handler(request.override(messages=[*request.messages, answer, hint]))
        second = _final_answer(retry)
        if second is None:
            # 重试可能因预算被拒绝，或需要继续读取工具；不能悄悄退回已知有问题的原回答。
            return retry
        annotate_evidence(second, request.messages)
        left_claims = false_success_claims(second.text)
        if left_claims:
            second.response_metadata["false_success_claims"] = left_claims
        left = find_internal_terms(second.text, sources)
        left_conflicts = missing_conflict_values(second.text, conflicts)
        left_length = limit is not None and reply_chars(second.text) > limit
        left_fidelity = self._fidelity(second.text,request, sources)
        left_dates = self._dates(second.text, request, sources)
        if dates:
            log.info("日期纠正：重写后%s", "已无明确错误" if not left_dates else f"仍有 {len(left_dates)} 处错误")
        if left or left_conflicts or left_length or left_fidelity or left_dates or left_claims:
            log.warning("重试后回答仍未通过检查（内部信息 %s，缺少的冲突说法 %s，超出字数上限 %s，事实/编号问题 %s 项，日期换算错误 %s 处，虚报操作成功 %s 处），需要人工排查", left, left_conflicts, limit if left_length else None,len(left_fidelity), len(left_dates), len(left_claims))
            second.response_metadata = {**second.response_metadata, "internal_terms": left, "missing_conflicts": left_conflicts}
            if left_length:
                second.response_metadata.update({"length_limit": limit, "actual_chars": reply_chars(second.text)})
            if left_fidelity:
                second.response_metadata["fidelity_issues"] = left_fidelity
            if left_dates:
                second.response_metadata["date_issues"] = left_dates
        return retry

    def _dates(self, text, request, sources, record: bool = False) -> list[str]:
        """明确的日期换算错误；record 时记录核对指标（只记数量，不记回答内容）。"""
        from .date_check import review_dates

        runtime = getattr(request, "runtime", None)
        now = getattr(getattr(runtime, "context", None), "now", None)  # 本次请求的时间；没有时用配置时区的今天
        review = review_dates(text, sources, now.date() if now else None)
        if record and review.checked:
            log.info("日期核对：可核对 %d 处，明确错误 %d 处，基准不确定（按宽松规则放行）%d 处",
                     review.checked, len(review.problems), review.uncertain)
        return review.problems

    def _fidelity(self, text, request, sources) -> list[str]:
        from .fidelity import current_instruction, fact_problems, outline_problems

        problems = fact_problems(text,request.messages)
        if "编号" in current_instruction(request.messages):
            problems += outline_problems(text,request.messages,sources)
        return problems

    def _sources(self, request) -> list[str]:
        try:
            return self.sources(request)
        except Exception as e:  # 取不到依据时宁可多报（会多重试一次），不影响回答
            log.warning("取输出检查的依据失败: %s", e)
            return []


def _final_answer(response) -> AIMessage | None:
    """模型生成的最终回答；程序直接返回的固定提示（超长、预算不足等）不检查。"""
    from .context_builder import SYSTEM_REPLY

    result = response.result if isinstance(response, ModelResponse) else [response]
    ai = next((m for m in reversed(result) if isinstance(m, AIMessage)), None)
    if ai is None or ai.tool_calls or not ai.text or ai.response_metadata.get(SYSTEM_REPLY):
        return None
    return ai
