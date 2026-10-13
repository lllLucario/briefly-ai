"""输出检查：发现最终回答、周报里混入了内部处理信息（块号、Chunk、程序出处标记等）。

只检测、不删除：原文可能真的写着“第 3 块”之类的话，所以匹配到的内容如果在原材料或用户输入里出现过，就不算泄漏。
检测到后由调用方决定：对话重试一次（InternalTermCheck），周报写进系统提示供人工核对。不额外调用模型做审核。
"""
import logging
import re

from langchain.agents.middleware import AgentMiddleware, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage

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
    """最终回答（不再调用工具的那次）含内部处理信息时，带着提示重试一次；仍有则保留回答并记日志，供人工排查。

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
        terms = find_internal_terms(answer.text, self._sources(request))
        if not terms:
            return response
        log.warning("回答中出现内部处理信息 %s，重试一次", terms)
        hint = HumanMessage(RETRY_HINT.format(terms="、".join(terms)), additional_kwargs={"internal": True})
        retry = handler(request.override(messages=[*request.messages, answer, hint]))
        second = _final_answer(retry)
        if second is None:
            return response
        left = find_internal_terms(second.text, self._sources(request))
        if left:
            log.warning("重试后回答中仍有内部处理信息 %s，需要人工排查", left)
            second.response_metadata = {**second.response_metadata, "internal_terms": left}
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
