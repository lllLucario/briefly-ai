"""工具预算：在工具执行前预留、执行后结算，控制一轮提问中工具结果的总量。

为什么要“预留”：同一步并行发起的多个工具调用由框架在多个线程里同时执行，如果只在返回后统计，
每个调用都会以为自己拥有全部剩余预算。这里在执行前加锁，从剩余预算中划出本次调用能用的份额，
工具通过 output_limit() 读到这个份额并据此决定返回多少；超出份额的结果完整存入 tool_results，
只把开头部分和引用放进上下文，绝不静默截断。

限制项：单次工具结果上限、一轮合计上限、调用次数（由 MAX_TOOL_ROUNDS 间接限制）、并发数、一轮总耗时。
"""
import logging
import threading
import time
from collections import OrderedDict
from contextvars import ContextVar
from dataclasses import dataclass, field

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage, ToolMessage

from . import config
from .context import Context
from .context_store import ContextStore
from .tokens import TokenCounter

log = logging.getLogger(__name__)

MIN_GRANT_TOKENS = 300  # 剩余预算不足这个数时不再执行工具
# 个别工具的单次上限（其余用 TOOL_RESULT_TOKENS）。预留按上限计，上限贴近实际能让并行调用多分到几份：
# 读一块约 1500 字、按章节最多 SECTION_READ_CHARS 字，检索最多 10 条片段；完整提炼的要点清单较大
TOOL_LIMITS = {
    "read_material": 3000,
    "search_materials": 3000,
    "list_materials": 3000,
    "recall_conversation": 3000,
    "summarize_materials": 12000,
}

_output_limit: ContextVar[int | None] = ContextVar("tool_output_limit", default=None)


def output_limit() -> int:
    """当前这次工具调用可以返回的 token 数。不在预算中间件下执行（如直接调用工具）时取单次上限。"""
    return _output_limit.get() or config.TOOL_RESULT_TOKENS


def head_within(text: str, tokens: int, counter: TokenCounter) -> str:
    """取不超过 tokens 的开头部分，尽量在换行处断开。"""
    if counter.count_text(text) <= tokens:
        return text
    lo, hi = 0, len(text)
    while lo < hi:  # 二分找最长的前缀
        mid = (lo + hi + 1) // 2
        if counter.count_text(text[:mid]) <= tokens:
            lo = mid
        else:
            hi = mid - 1
    head = text[:lo]
    cut = head.rfind("\n")
    return head[:cut] if cut > lo * 0.6 else head


def turn_key(state) -> str:
    """本轮提问的标识：最后一条用户消息的 id（框架写入状态时会补上 id）。"""
    messages = (state or {}).get("messages", []) if isinstance(state, dict) else getattr(state, "messages", [])
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            return message.id or str(id(message))
    return ""


@dataclass
class TurnLedger:
    used: int = 0  # 已结算
    reserved: int = 0  # 已预留、还没结算
    calls: int = 0
    refused: int = 0
    started: float = field(default_factory=time.monotonic)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def remaining(self) -> int:
        return config.TURN_TOOL_TOKENS - self.used - self.reserved


def _text(content) -> str:
    return content if isinstance(content, str) else str(content)


class ToolBudgetMiddleware(AgentMiddleware):
    def __init__(self, counter: TokenCounter, store: ContextStore | None = None) -> None:
        super().__init__()
        self.counter = counter
        self.store = store
        self._ledgers: OrderedDict[tuple[str, str], TurnLedger] = OrderedDict()
        self._ledgers_lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(config.TOOL_CONCURRENCY)

    def ledger(self, thread_id: str, key: str) -> TurnLedger:
        with self._ledgers_lock:
            ledger = self._ledgers.get((thread_id, key))
            if ledger is None:
                ledger = self._ledgers[(thread_id, key)] = TurnLedger()
                while len(self._ledgers) > 64:  # 只需要保留进行中的几轮
                    self._ledgers.popitem(last=False)
            return ledger

    def wrap_tool_call(self, request, handler):
        context: Context = request.runtime.context or Context()
        call = request.tool_call
        name = call.get("name", "")
        ledger = self.ledger(context.thread_id, turn_key(request.state))
        cap = TOOL_LIMITS.get(name, config.TOOL_RESULT_TOKENS)

        # 先拿并发槽位再预留：只有正在执行的调用持有预留，排队的调用等前面的按实际大小结算后再分份额
        with self._slots:
            with ledger.lock:  # 预留：并行的调用依次从剩余预算中划份额，不会重复拿到同一份
                elapsed = time.monotonic() - ledger.started
                remaining = ledger.remaining()
                refusal = None
                if elapsed > config.TURN_TIME_BUDGET_S:
                    refusal = f"本轮工具已运行 {elapsed:.0f} 秒，超过时间预算（{config.TURN_TIME_BUDGET_S:.0f} 秒），不再调用工具。"
                elif remaining < MIN_GRANT_TOKENS:
                    refusal = (
                        f"本轮工具结果已用约 {ledger.used + ledger.reserved} tokens，达到单轮预算（{config.TURN_TOOL_TOKENS}），"
                        "本次未执行。请根据已有内容回答；需要完整内容时改用 summarize_materials，"
                        "只需某个主题时用 search_materials 定位后只读相关的块。"
                    )
                if refusal:
                    ledger.refused += 1
                    log.info("工具 %s 未执行：%s", name, refusal)
                    return ToolMessage(refusal, tool_call_id=call["id"], name=name, status="error")
                grant = min(cap, remaining)
                ledger.reserved += grant
                ledger.calls += 1

            actual = 0
            try:
                token = _output_limit.set(grant)
                try:
                    result = handler(request)
                finally:
                    _output_limit.reset(token)
                if isinstance(result, ToolMessage):
                    size = self.counter.count_text(_text(result.content))
                    if size > grant:
                        result = self._offload(result, size, grant, call, context)
                    actual = self.counter.count_text(_text(result.content))
                return result
            finally:
                with ledger.lock:
                    ledger.reserved -= grant
                    ledger.used += actual

    def _offload(self, result: ToolMessage, size: int, grant: int, call: dict, context: Context) -> ToolMessage:
        """结果超出份额：完整结果存库，上下文里只放开头部分和引用。"""
        content = _text(result.content)
        result_id = None
        if self.store is not None:
            try:
                result_id = self.store.save_tool_result(
                    context.thread_id, context.run_id, call.get("name", ""), call["id"], call.get("args", {}), content, size
                )
            except Exception as e:
                log.warning("保存工具结果失败: %s", e)
        if result_id:
            notice = (
                f"【工具结果过长：约 {size} tokens，超出本次可用的 {grant}。完整结果已保存为 {result_id}，"
                f"下面只是开头部分；需要后续内容请用 read_tool_result(result_id=\"{result_id}\", page=2) 继续读，"
                "或缩小范围重新调用】"
            )
        else:
            notice = f"【工具结果过长：约 {size} tokens，超出本次可用的 {grant}，下面只是开头部分；请缩小范围重新调用】"
        head = head_within(content, max(grant - self.counter.count_text(notice) - 50, 0), self.counter)
        if head.count("【材料开始】") > head.count("【材料结束】"):
            head += "\n……（预览到此为止）\n【材料结束】"
        log.info("工具 %s 结果 %s tokens 超出份额 %s，已存为 %s", call.get("name"), size, grant, result_id)
        return result.model_copy(update={"content": f"{notice}\n{head}"})
