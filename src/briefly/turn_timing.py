"""一轮对话的耗时拆分：意图判断、检索、模型调用、回答检查、总耗时，以及服务商返回的上下文缓存 token。

计时对象放在 ContextVar 里、由本轮共用：LangGraph 和 LangChain 的线程池会复制上下文，复制的是对象引用，
工具线程里的耗时也记到同一轮。没有进行中的轮次时这里什么都不做。只记数字，不记内容。
"""
import contextvars
import functools
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from time import perf_counter

SPANS = ("intent", "retrieval", "model", "check")


@dataclass
class TurnTiming:
    started: float = field(default_factory=perf_counter)
    spans: dict[str, float] = field(default_factory=lambda: dict.fromkeys(SPANS, 0.0))
    run_id: str = ""
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    provider_cache_hit_tokens: int = 0
    provider_cache_miss_tokens: int = 0

    def summary(self) -> dict:
        total = perf_counter() - self.started
        return dict(total_ms=int(total * 1000), **{f"{k}_ms": int(v * 1000) for k, v in self.spans.items()},
                    model_calls=self.model_calls, input_tokens=self.input_tokens, output_tokens=self.output_tokens,
                    provider_cache_hit_tokens=self.provider_cache_hit_tokens,
                    provider_cache_miss_tokens=self.provider_cache_miss_tokens)


_current: contextvars.ContextVar[TurnTiming | None] = contextvars.ContextVar("briefly_turn_timing", default=None)


def current() -> TurnTiming | None:
    return _current.get()


@contextmanager
def measure() -> Iterator[TurnTiming | None]:
    """开始一轮。已在一轮之内时（单问捷径内部还会跑一次智能体）返回 None，内层耗时算进外层。"""
    if _current.get() is not None:
        yield None
        return
    turn = TurnTiming()
    token = _current.set(turn)
    try:
        yield turn
    finally:
        _current.reset(token)


def add(name: str, seconds: float) -> None:
    turn = _current.get()
    if turn is not None:
        turn.spans[name] = turn.spans.get(name, 0.0) + seconds


@contextmanager
def span(name: str) -> Iterator[None]:
    started = perf_counter()
    try:
        yield
    finally:
        add(name, perf_counter() - started)


def timed(name: str):
    def decorate(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with span(name):
                return fn(*args, **kwargs)
        return wrapper
    return decorate


def provider_cache_tokens(message) -> tuple[int, int]:
    """服务商上下文缓存的 (命中, 未命中) 输入 token。

    DeepSeek 把 prompt_cache_hit_tokens、prompt_cache_miss_tokens 放在 usage 顶层，ChatOpenAI 原样保留在
    response_metadata["token_usage"]；OpenAI 式的 cached_tokens 由 LangChain 换算到 input_token_details.cache_read。
    这是服务商一侧的前缀缓存，和本项目的模型响应缓存是两回事。
    """
    usage = (getattr(message, "response_metadata", None) or {}).get("token_usage") or {}
    details = (getattr(message, "usage_metadata", None) or {}).get("input_token_details") or {}
    hit = usage.get("prompt_cache_hit_tokens")
    return (details.get("cache_read") or 0 if hit is None else hit), usage.get("prompt_cache_miss_tokens") or 0


def add_usage(message) -> None:
    """记一次实际发生的模型调用。"""
    turn = _current.get()
    if turn is None:
        return
    usage = getattr(message, "usage_metadata", None) or {}
    hit, miss = provider_cache_tokens(message)
    turn.model_calls += 1
    turn.input_tokens += usage.get("input_tokens") or 0
    turn.output_tokens += usage.get("output_tokens") or 0
    turn.provider_cache_hit_tokens += hit
    turn.provider_cache_miss_tokens += miss
