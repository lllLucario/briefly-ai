"""输出检查：发现最终回答、周报里混入了内部处理信息（块号、Chunk、程序出处标记等）。

只检测、不删除：原文可能真的写着“第 3 块”之类的话，所以匹配到的内容如果在原材料或用户输入里出现过，就不算泄漏。
检测到后由调用方决定：对话重试一次（InternalTermCheck），周报写进系统提示供人工核对。不额外调用模型做审核。
"""
import logging
import re

from langchain.agents.middleware import AgentMiddleware, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

log = logging.getLogger(__name__)

INTERNAL_PATTERNS = [
    re.compile(r"第\s*\d+\s*(?:[–—\-~～至到]\s*\d+\s*)?块"),  # 第 3 块、第 7–9 块
    re.compile(r"共\s*\d+\s*块"),
    re.compile(r"\d+\s*/\s*\d+\s*块"),
    re.compile(r"块号"),
    re.compile(r"(?i)\bchunks?\b"),
    re.compile(r"(?i)\bmap[\s-]?reduce\b"),
    re.compile(r"\bmaterial_id\b|\btool_call\b|\bsummarize_materials\b|\bread_material\b|\bget_outline\b"),
    re.compile(r"〔\d+-\d+(?:\s*[、,，]\s*\d+-\d+)*〕"),  # 程序内部的出处标记〔材料号-块号〕
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
    两类问题合并成一次重试。

    sources(request) 返回判断依据的原文（本轮用户输入、可访问材料的正文）。放在 ContextBuilder 外层：重试也经过预算检查。
    """

    def __init__(self, sources) -> None:
        super().__init__()
        self.sources = sources

    def wrap_model_call(self, request, handler):
        response = handler(request)
        answer = _final_answer(response)
        if answer is None:
            return response
        conflicts = turn_conflicts(request.messages)
        terms = find_internal_terms(answer.text, self._sources(request))
        missing = missing_conflict_values(answer.text, conflicts)
        if not terms and not missing:
            return response
        log.warning("回答检查未通过（内部信息 %s，缺少的冲突说法 %s），重试一次", terms, missing)
        hints = []
        if terms:
            hints.append(RETRY_HINT.format(terms="、".join(terms)))
        if missing:
            hints.append(CONFLICT_HINT.format(missing="、".join(missing)))
        hint = HumanMessage("\n".join(hints), additional_kwargs={"internal": True})
        retry = handler(request.override(messages=[*request.messages, answer, hint]))
        second = _final_answer(retry)
        if second is None:
            return response
        left = find_internal_terms(second.text, self._sources(request))
        left_conflicts = missing_conflict_values(second.text, conflicts)
        if left or left_conflicts:
            log.warning("重试后回答仍未通过检查（内部信息 %s，缺少的冲突说法 %s），需要人工排查", left, left_conflicts)
            second.response_metadata = {**second.response_metadata, "internal_terms": left, "missing_conflicts": left_conflicts}
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
