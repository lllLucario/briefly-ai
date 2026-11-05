"""模型实例和模型响应缓存的唯一出口（OpenAI 兼容接口），服务商、地址、模型由 .env 决定。"""
import contextvars
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from time import perf_counter

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI
from pydantic import ValidationError

from . import config, turn_timing
from .llm_cache import CACHEABLE_SITES, active_scope, cache_key, record_cache_event

log = logging.getLogger(__name__)


class ModelConfigurationError(RuntimeError):
    """模型配置缺失；可直接向用户显示，不包含配置值。"""


def missing_model_config() -> list[str]:
    """缺少的模型配置项名称，不含配置值。"""
    return [name for name, value in (
        ("BRIEFLY_API_KEY（或 OPENAI_API_KEY）", config.API_KEY),
        ("BRIEFLY_MODEL", config.MODEL),
    ) if not value or not value.strip()]


def create_chat_model(*, max_retries: int | None = None, timeout: float | None = None) -> BaseChatModel:
    missing = missing_model_config()
    if missing:
        raise ModelConfigurationError(
            f"模型配置不完整：缺少 {'、'.join(missing)}。请在 .env 中设置后重启服务；材料管理等不需要模型的功能仍可使用。"
        )
    # 必须在创建SDK客户端之前设置；创建后修改模型字段不会重建连接层。
    options = {}
    if max_retries is not None:
        options["max_retries"] = max_retries
    if timeout is not None:
        options["timeout"] = timeout
    return ChatOpenAI(
        model=config.MODEL,
        api_key=config.API_KEY,
        base_url=config.BASE_URL,
        max_tokens=config.MAX_TOKENS,
        extra_body=_thinking_body(),
        **options,
    )


def _thinking_body() -> dict | None:
    """DeepSeek 默认开思考模式。开着时带工具的多轮请求必须回传每轮的 reasoning_content，
    ChatOpenAI 不回传会报 400；思考也让每次调用更慢更贵。所以默认显式关闭。"""
    if config.THINKING is None:
        return None
    return {"thinking": {"type": "enabled" if config.THINKING else "disabled"}}


_fresh: contextvars.ContextVar[bool] = contextvars.ContextVar("briefly_fresh_model_calls", default=False)


@contextmanager
def fresh_model_calls() -> Iterator[None]:
    """用户明确要求重做时用：里面的 CachedCall 不读缓存，但结果通过检查后仍然写入（覆盖旧结果）。

    放在用户动作的入口（路由、一轮对话），不放在调用点：同一次重做可能经过多个调用点。线程池会复制上下文，工具里也有效。
    """
    token = _fresh.set(True)
    try:
        yield
    finally:
        _fresh.reset(token)


class CachedCall:
    """一次可缓存的模型调用。流程是“查 → 调用 → 调用方检查 → accept”，所以只有通过检查的结果才进缓存。

    site 必须在白名单里。没有作用域（开关关闭、不在请求内）时 text()/structured() 就是原来的调用。
    必须在授权检查和依据包构建之后再创建：命中不再经过任何上游检查。
    """

    def __init__(self, site: str, model, messages, *, schema=None, tools=None, extra=None) -> None:
        if site not in CACHEABLE_SITES:
            raise ValueError(f"调用点 {site} 不在模型缓存白名单内")
        self.site, self.model, self.messages, self.schema = site, model, messages, schema
        self.scope = active_scope()
        self.hit = False
        self.key = None
        self._pending = None
        if self.scope is not None:
            self.key = cache_key(site, model, messages, self.scope.principal_id, schema=schema, tools=tools, extra=extra)

    @property
    def _stat(self) -> str:
        return f"llm.{self.site}"

    def lookup(self) -> dict | None:
        if self.key is None:
            return None
        if _fresh.get():
            record_cache_event(self.scope.cache.engine, self._stat, refreshed=1)
            return None
        cached = self.scope.cache.get(self.key)
        if cached is None:
            record_cache_event(self.scope.cache.engine, self._stat, misses=1)
            return None
        self.hit = True
        record_cache_event(self.scope.cache.engine, self._stat, hits=1, saved_input_tokens=cached.input_tokens,
                           saved_output_tokens=cached.output_tokens, saved_ms=cached.latency_ms)
        log.info("模型缓存命中 site=%s key=%s", self.site, self.key[:12])
        return cached.value

    def remember(self, value: dict, *, input_tokens=0, output_tokens=0, latency_ms=0, truncated=False) -> None:
        """记下这次的结果，等调用方检查后 accept。被截断的结果不进缓存。"""
        if self.key is not None and not truncated:
            self._pending = dict(value=value, input_tokens=input_tokens, output_tokens=output_tokens, latency_ms=latency_ms)
        elif self.key is not None:
            self.reject()

    def _remember_message(self, value: dict, message, seconds: float) -> None:
        usage = getattr(message, "usage_metadata", None) or {}
        self.remember(value, input_tokens=usage.get("input_tokens") or 0, output_tokens=usage.get("output_tokens") or 0,
                      latency_ms=int(seconds * 1000), truncated=message.response_metadata.get("finish_reason") == "length")

    def text(self) -> AIMessage:
        """普通文本调用。命中时返回没有 usage_metadata 的消息：这次没有花费 token。"""
        cached = self.lookup()
        if cached is not None:
            return AIMessage(cached["text"], response_metadata={"llm_cache_hit": True})
        started = perf_counter()
        with turn_timing.span("model"):
            message = self.model.invoke(self.messages)
        turn_timing.add_usage(message)
        self._remember_message(dict(text=message.text), message, perf_counter() - started)
        return message

    def structured(self):
        """结构化调用。值按 schema 校验后还原；校验不过（代码改了校验规则）就当没命中。"""
        cached = self.lookup()
        if cached is not None:
            try:
                return self.schema.model_validate(cached)
            except ValidationError:
                self.hit = False
        if self.key is None:
            with turn_timing.span("model"):
                return self.model.with_structured_output(self.schema, method="function_calling").invoke(self.messages)
        started = perf_counter()
        with turn_timing.span("model"):
            response = self.model.with_structured_output(self.schema, method="function_calling", include_raw=True).invoke(self.messages)
        turn_timing.add_usage(response["raw"])
        if response.get("parsing_error"):
            raise response["parsing_error"]
        parsed = response["parsed"]
        if parsed is not None:
            self._remember_message(parsed.model_dump(mode="json"), response["raw"], perf_counter() - started)
        return parsed

    def accept(self, value: dict | None = None) -> None:
        """调用方的检查通过了，写入。value 是调用方处理后的最终结果（如已脱敏），不传就存模型返回的。
        没有待写入的结果时（命中、关闭）什么都不做。写失败不影响回答。"""
        pending, self._pending = self._pending, None
        if pending is None:
            return
        returned = pending.pop("value")
        value = returned if value is None else value
        if self.scope.cache.put(self.key, self.site, self.scope.principal_id, value, **pending):
            record_cache_event(self.scope.cache.engine, self._stat, stores=1)

    def reject(self) -> None:
        """结果没通过检查，不缓存。"""
        self._pending = None
        if self.key is not None:
            record_cache_event(self.scope.cache.engine, self._stat, rejected=1)


def create_local_embeddings(model_dir: str, *, threads: int = 4, max_input_tokens: int | None = None):
    """W3 本地选型入口：只加载白名单中已准备的CPU模型，不自动下载或联网推理。"""
    import json
    from pathlib import Path
    from .local_embeddings import LocalBgeEmbeddings, LocalJinaEmbeddings

    manifest = json.loads((Path(model_dir) / "prepared.json").read_text(encoding="utf-8"))
    adapters = {"BAAI/bge-small-zh-v1.5":LocalBgeEmbeddings,
                "jinaai/jina-embeddings-v2-base-zh":LocalJinaEmbeddings}
    adapter = adapters.get(manifest.get("model"))
    if adapter is None:
        raise ValueError("本地Embedding模型未在已验证候选白名单中")
    return adapter(model_dir, threads=threads, max_input_tokens=max_input_tokens)
