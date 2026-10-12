"""Context Builder：每次调用模型前统一组装上下文，并在发送前检查总预算。

组成（按保留优先级从高到低）：
1. 系统提示词（base.md + 能力说明）与工具定义 —— 必须完整
2. 当前轮：用户消息 + 本轮的工具往返 —— 必须完整；本轮较早的工具结果在必要时换成引用（可重新读取）
3. 当前任务状态（指定能力、所选材料、工具预算余量）
4. 会话记忆（压缩后的早期对话；要求、决定、未完成事项完整保留）
5. 最近几轮原文（按整轮从新到旧放；之前各轮里的大段工具结果、材料全文换成引用）

必要部分（1、2）放不下时，不截断、不发送，返回明确的“超出预算”说明。
预算：min(MAX_INPUT_TOKENS, CONTEXT_LIMIT − 预留输出 − 安全余量)，计数按校准比例放大。
接口仍报上下文超长时，按 0.6 倍预算重建并重试，最多 CONTEXT_RETRIES 次。
所有改动只作用于发给模型的消息，checkpointer 里的历史保持原样。
"""
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field

from langchain.agents.middleware import AgentMiddleware, ModelResponse
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage

from . import config
from .compaction import Compactor, render_memory, split_turns
from .context import Context
from .context_store import ContextStore
from .tokens import TokenCounter

log = logging.getLogger(__name__)

CONTEXT_OVERFLOW = (
    "这次对话和材料的内容超出了模型一次能处理的长度。请开启新会话（/reset），"
    "或让我用 summarize_materials 先把材料提炼成要点，再继续。"
)
BUDGET_EXCEEDED = (
    "这次请求本身（当前消息及本轮读取的材料）超出了单次可处理的长度（约 {need} tokens，上限 {limit}），"
    "为避免截断内容，没有发送给模型。请缩小范围（指定章节或关注点），或让我用 summarize_materials 分批提炼。"
)
_MATERIAL_BLOCK = re.compile(r"(【[^】\n]*】)\n【材料开始】\n(.*?)\n【材料结束】", re.S)
_RESULT_ID = re.compile(r"完整结果已保存为 (r_[0-9a-f]+)")


def input_limit(factor: float = 1.0) -> int:
    """单次输入上限：自定预算与模型窗口（扣除预留输出和安全余量）取小。"""
    limit = config.MAX_INPUT_TOKENS
    if config.CONTEXT_LIMIT:
        limit = min(limit, config.CONTEXT_LIMIT - config.RESERVED_OUTPUT_TOKENS - config.SAFETY_MARGIN_TOKENS)
    return int(limit * factor)


def is_context_overflow(error: Exception) -> bool:
    text = str(error).lower()
    return "context" in text and any(k in text for k in ("length", "too long", "maximum", "exceed"))


def _text(content) -> str:
    return content if isinstance(content, str) else str(content)


def material_blocks(text: str) -> list[str]:
    return [m.group(2) for m in _MATERIAL_BLOCK.finditer(text)]


def stub_tool_result(message: ToolMessage, tokens: int) -> ToolMessage:
    """工具结果换成引用，保留 tool_call_id 等字段，不破坏工具调用与结果的配对。"""
    saved = _RESULT_ID.search(_text(message.content))
    ref = saved.group(1) if saved else message.tool_call_id
    stub = (
        f"[已省略：之前 {message.name or '工具'} 返回的结果（约 {tokens} tokens）。"
        f"需要时重新调用工具，或用 read_tool_result(result_id=\"{ref}\") 读回]"
    )
    return message.model_copy(update={"content": stub})


def stub_materials(message: AnyMessage, threshold: int, counter: TokenCounter) -> tuple[AnyMessage, int]:
    """消息里附的大段材料全文换成说明（材料仍在材料库，可用工具重新读取）。返回 (消息, 替换的块数)。"""
    text = _text(message.content)
    count = 0

    def replace(match: re.Match) -> str:
        nonlocal count
        size = counter.count_text(match.group(2))
        if size <= threshold:
            return match.group(0)
        count += 1
        return f"{match.group(1)}\n【材料开始】\n（之前附过的材料全文，约 {size} tokens，已省略；需要时用材料工具重新读取）\n【材料结束】"

    new = _MATERIAL_BLOCK.sub(replace, text)
    return (message.model_copy(update={"content": new}) if count else message), count


def condense(messages: list[AnyMessage], threshold: int, counter: TokenCounter) -> tuple[list[AnyMessage], int]:
    """之前各轮：大段工具结果、材料全文换成引用。模型在那一轮给出的回答保留原文。"""
    out, stubbed = [], 0
    for m in messages:
        if isinstance(m, ToolMessage):
            size = counter.count_text(_text(m.content))
            if size > threshold:
                m = stub_tool_result(m, size)
                stubbed += 1
        elif isinstance(m, HumanMessage):
            m, n = stub_materials(m, threshold, counter)
            stubbed += n
        out.append(m)
    return out, stubbed


class ContextBudgetExceeded(Exception):
    def __init__(self, need: int, limit: int) -> None:
        super().__init__(BUDGET_EXCEEDED.format(need=need, limit=limit))
        self.need, self.limit = need, limit


@dataclass
class Built:
    system: SystemMessage
    messages: list[AnyMessage]
    estimated: int  # 原始估算
    budgeted: int  # 按校准比例放大后
    limit: int
    parts: dict = field(default_factory=dict)  # 观测：各部分 token 数等


class ContextBuilder(AgentMiddleware):
    def __init__(
        self,
        counter: TokenCounter,
        store: ContextStore,
        compactor: Compactor,
        model_name: str = "",
        task_state=None,
    ) -> None:
        """task_state(context, state) 返回当前任务状态文本（由 Agent 提供，含工具预算余量）。"""
        super().__init__()
        self.counter = counter
        self.store = store
        self.compactor = compactor
        self.model_name = model_name
        self.task_state = task_state
        self._tool_tokens: dict[tuple, int] = {}

    def tool_tokens(self, tools) -> int:
        key = tuple(getattr(t, "name", None) or str(t) for t in tools or [])
        if key not in self._tool_tokens:
            self._tool_tokens[key] = self.counter.count_tools(tools)
        return self._tool_tokens[key]

    def build(self, request, context: Context, factor: float = 1.0) -> Built:
        c = self.counter
        limit = input_limit(factor)
        messages = list(request.messages)
        base_system = _text(request.system_message.content) if request.system_message else ""
        tool_tokens = self.tool_tokens(request.tools)
        memory, start = self.compactor.load(context.thread_id, messages)

        turns = split_turns(messages)
        cur_start = turns[-1][0] if turns else 0
        if cur_start < start:  # 检查点不应越过当前轮，防御
            start = 0
        current = messages[cur_start:]
        task = self.task_state(context, request.state) if self.task_state else ""

        # 1 + 2：必须完整的部分
        fixed = c.count_text(base_system) + tool_tokens + c.count_text(task)
        current_tokens = c.count_messages(current)
        stubbed = 0
        if c.budgeted(fixed + current_tokens) > limit:
            # 本轮较早的工具结果换成引用（从最早的开始，最近一次保留），直到放得下
            tool_idx = [i for i, m in enumerate(current) if isinstance(m, ToolMessage)][:-1]
            for i in tool_idx:
                size = c.count_text(_text(current[i].content))
                current[i] = stub_tool_result(current[i], size)
                stubbed += 1
                current_tokens = c.count_messages(current)
                if c.budgeted(fixed + current_tokens) <= limit:
                    break
        if c.budgeted(fixed + current_tokens) > limit:
            raise ContextBudgetExceeded(c.budgeted(fixed + current_tokens), limit)

        # 4：会话记忆，放不下时缩小渲染预算（低优先级条目先省略）
        room = limit - c.budgeted(fixed + current_tokens)
        memory_text, _ = render_memory(memory, min(config.MEMORY_TOKENS, room), c)
        memory_tokens = c.count_text(memory_text)
        room -= c.budgeted(memory_tokens)

        # 5：最近几轮，按整轮从新到旧
        history_budget = min(int(config.HISTORY_TOKENS * factor), max(room, 0))
        kept: list[list[AnyMessage]] = []
        used = 0
        prev = [(s, e) for s, e in turns[:-1] if s >= start]
        omitted = 0
        for i, (s, e) in enumerate(reversed(prev)):
            turn, n = condense(messages[s:e], config.STALE_RESULT_TOKENS, c)
            cost = c.count_messages(turn)
            if c.budgeted(used + cost) > history_budget:
                omitted = len(prev) - i
                break
            kept.insert(0, turn)
            used += cost
            stubbed += n
        history = [m for turn in kept for m in turn]

        notice = ""
        if omitted:
            notice = (
                f"注意：本会话更早的 {omitted} 轮对话没有放进上下文，也还没有整理进会话记忆；"
                "如需其中内容，用 recall_conversation 查找。"
            )
        system_text = "\n\n".join(p for p in [base_system, memory_text, task, notice] if p)
        system = SystemMessage(system_text)
        out = [*history, *current]
        estimated = c.count_text(system_text) + tool_tokens + c.count_messages(out) + 4
        all_text = [_text(m.content) for m in out if not isinstance(m, ToolMessage)]
        blocks = [b for t in all_text for b in material_blocks(t)]
        tool_texts = [_text(m.content) for m in out if isinstance(m, ToolMessage)]
        hashes = sorted(
            {hashlib.sha1(b.encode("utf-8")).hexdigest()[:12] for b in blocks + [b for t in tool_texts for b in material_blocks(t)]}
        )
        parts = {
            "system_tokens": c.count_text(base_system) + c.count_text(task),
            "tool_schema_tokens": tool_tokens,
            "memory_tokens": memory_tokens,
            "history_tokens": used,
            "material_tokens": sum(c.count_text(b) for b in blocks),
            "tool_result_tokens": sum(c.count_text(t) for t in tool_texts),
            "current_tokens": current_tokens,
            "tool_call_count": sum(len(getattr(m, "tool_calls", None) or []) for m in current),
            "history_turns": len(kept),
            "omitted_turns": omitted,
            "stubbed_results": stubbed,
            "material_hashes": hashes,
        }
        if omitted:
            log.warning("会话 %s：%s 轮未压缩的历史没有放进上下文", context.thread_id, omitted)
        return Built(system, out, estimated, c.budgeted(estimated), limit, parts)

    def _record(self, context: Context, built: Built | None, **fields) -> None:
        data = dict(
            thread_id=context.thread_id,
            run_id=context.run_id,
            model=self.model_name,
            task_type="chat",
            compaction_triggered=context.compacted,
        )
        if built:
            data.update(built.parts, estimated_tokens=built.estimated, budgeted_tokens=built.budgeted, context_limit=built.limit)
        data.update(fields)
        log.info("context_usage %s", {k: v for k, v in data.items() if k != "material_hashes"})
        self.store.record_usage(**data)

    def wrap_model_call(self, request, handler):
        context: Context = request.runtime.context or Context()
        factor = 1.0
        for attempt in range(config.CONTEXT_RETRIES + 1):
            try:
                built = self.build(request, context, factor)
            except ContextBudgetExceeded as e:
                self._record(context, None, context_overflow=True, budgeted_tokens=e.need, context_limit=e.limit, note="发送前超出预算")
                return AIMessage(str(e))
            started = time.monotonic()
            try:
                response = handler(request.override(system_message=built.system, messages=built.messages))
            except Exception as e:
                if not is_context_overflow(e):
                    raise
                latency = int((time.monotonic() - started) * 1000)
                self._record(context, built, context_overflow=True, latency_ms=latency, note=f"接口报超长：{str(e)[:300]}")
                if attempt < config.CONTEXT_RETRIES:
                    factor *= 0.6
                    continue
                return AIMessage(CONTEXT_OVERFLOW)
            latency = int((time.monotonic() - started) * 1000)
            result = response.result if isinstance(response, ModelResponse) else [response]
            ai = next((m for m in reversed(result) if isinstance(m, AIMessage)), None)
            usage = (getattr(ai, "usage_metadata", None) or {}) if ai else {}
            self.counter.calibrate(built.estimated, usage.get("input_tokens"))
            self._record(
                context,
                built,
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
                latency_ms=latency,
                note="重试" if attempt else "",
            )
            return response
        return AIMessage(CONTEXT_OVERFLOW)
