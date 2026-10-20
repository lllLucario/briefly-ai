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
        if re.search(r"(?:取消|解除|不再限制|不限制|不限|不用控制)\s*(?:输出|回复|回答)?\s*字数(?:限制)?", text):
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
]

RETRY_HINT = (
    "【系统检查】上面的回答中出现了程序内部的处理信息：{terms}。这些是工具调用和检索用的信息（块号、程序出处标记、工具名等），"
    "不是材料内容，用户也看不懂。请重写回答：内容不变，去掉这些信息；需要说明出处时用材料标题和章节名。直接输出重写后的完整回答。"
)


CONFLICT_HINT = (
    "【系统检查】工具结果列出了不同材料之间看不出哪个为准的说法，回答里缺少：{missing}。"
    "请重写回答：同时写出各个说法并说明材料之间不一致，不要只写其中一个；其他内容不变。直接输出重写后的完整回答。"
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
    """回答里缺少的冲突说法。日期、金额等按数字比较（“10 月 20 日”与“10月20日”算同一个）。"""
    flat = _squash(answer)
    numbers = set(re.findall(r"\d+(?:\.\d+)?", answer))
    missing = []
    for c in conflicts:
        for value in c.get("values", []):
            digits = re.findall(r"\d+(?:\.\d+)?", value)
            if _squash(value) in flat or (digits and all(d in numbers for d in digits)):
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


class InternalTermCheck(AgentMiddleware):
    """最终回答（不再调用工具的那次）的检查，有问题时带着提示重试一次；仍有则保留回答并记日志，供人工排查。

    - 含内部处理信息（块号等，Bug 1）；
    - 本轮工具结果里有看不出哪个为准的冲突，回答却只写了其中一种说法（Agent 改写工具结果时把矛盾“统一”了，Bug 4）。
    也核对用户明确给出的字数上限；所有问题合并成最多一次重试。

    sources(request) 返回判断依据的原文（本轮用户输入、可访问材料的正文）。放在 ContextBuilder 外层：重试也经过预算检查。
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
        conflicts = turn_conflicts(request.messages)
        terms = find_internal_terms(answer.text, self._sources(request))
        missing = missing_conflict_values(answer.text, conflicts)
        limit = self.length_limit(request)
        too_long = limit is not None and reply_chars(answer.text) > limit
        if not terms and not missing and not too_long:
            return response
        log.warning("回答检查未通过（内部信息 %s，缺少的冲突说法 %s，超出字数上限 %s），重试一次", terms, missing, limit if too_long else None)
        hints = []
        if terms:
            hints.append(RETRY_HINT.format(terms="、".join(terms)))
        if missing:
            hints.append(CONFLICT_HINT.format(missing="、".join(missing)))
        if too_long:
            hints.append(f"上一条回复共 {reply_chars(answer.text)} 字，违反用户仍有效的整条回复最多 {limit} 字要求。请重新压缩，所有标题、正文、说明和注合计不得超过 {limit} 字（不计空白）。保留原文事实、数字、日期和确定程度；精简修改说明，不添加可选的注，不截断或删除关键事实。")
        hint = HumanMessage("\n".join(hints), additional_kwargs={"internal": True})
        retry = handler(request.override(messages=[*request.messages, answer, hint]))
        second = _final_answer(retry)
        if second is None:
            # 重试可能因预算被拒绝，或需要继续读取工具；不能悄悄退回已知有问题的原回答。
            return retry
        left = find_internal_terms(second.text, self._sources(request))
        left_conflicts = missing_conflict_values(second.text, conflicts)
        left_length = limit is not None and reply_chars(second.text) > limit
        if left or left_conflicts or left_length:
            log.warning("重试后回答仍未通过检查（内部信息 %s，缺少的冲突说法 %s，超出字数上限 %s），需要人工排查", left, left_conflicts, limit if left_length else None)
            second.response_metadata = {**second.response_metadata, "internal_terms": left, "missing_conflicts": left_conflicts}
            if left_length:
                second.response_metadata.update({"length_limit": limit, "actual_chars": reply_chars(second.text)})
        return retry

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
