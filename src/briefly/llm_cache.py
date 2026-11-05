"""模型响应缓存的存储、键和统计。调用入口在 llm.py 的 CachedCall，其他地方不直接读写这里的表。

为什么不用 LangChain 的 BaseCache：它在 _generate_with_cache 里拿到模型返回后立刻写入，这时回答检查、引用核对都还没做，
无法做到“只缓存通过检查的结果”；键也放不进主体和依据身份，而且只能按模型实例开启，不能按调用点开启。

默认不缓存：只有请求经过 ResponseCacheScope（主体来自 auth.current_principal）时才有作用域，定时任务、后台线程、
命令行都没有作用域，直接调用模型。值只存 JSON，不用 pickle。日志只写键的哈希前缀，不写内容。
"""
import contextvars
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import DateTime, Engine, Integer, JSON, String, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker
from starlette.requests import Request

from . import config
from .auth import current_principal
from .db import Base

log = logging.getLogger(__name__)

KEY_VERSION = 1  # 回答检查规则变严等提示词哈希看不出来的变化：加一，旧条目全部不再命中

# 白名单。没列出的调用点（CachedCall 会拒绝）及不缓存的原因：
# - agent.py 带工具的智能体循环：工具有副作用，结果依赖当前状态；
# - compaction.py 会话压缩：输入含不断增长的对话，几乎不会重复；
# - handoffs.py 待办候选：用户重复点击就是想重新提取，缓存会让重试失效；
# - summarize.py 逐块提取：ChunkExtraction 已按块缓存，不重复做。
CACHEABLE_SITES = frozenset({"knowledge_answer", "synthesize", "conflict_judge", "evidence_compose"})


class LlmCacheEntry(Base):
    __tablename__ = "llm_cache_entries"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    site: Mapped[str] = mapped_column(String(40), index=True)
    principal_id: Mapped[str] = mapped_column(String(100), index=True)  # 也在键里；单独存一列，方便以后按主体清理
    value: Mapped[dict] = mapped_column(JSON)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)  # 生成这条结果实际花费的量，命中时算作节省
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    last_used_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)


class CacheStat(Base):
    """各缓存的累计计数，名字统一为“层.调用点”。统计不和缓存条目放一起：条目过期清理后，累计数不能跟着消失。"""

    __tablename__ = "cache_stats"

    name: Mapped[str] = mapped_column(String(60), primary_key=True)
    hits: Mapped[int] = mapped_column(Integer, default=0)
    misses: Mapped[int] = mapped_column(Integer, default=0)
    stores: Mapped[int] = mapped_column(Integer, default=0)
    rejected: Mapped[int] = mapped_column(Integer, default=0)  # 生成了但没通过检查、未写入
    saved_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    saved_output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    saved_ms: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


def record_cache_event(engine: Engine, name: str, **counts: int) -> None:
    """累加计数。统计失败只记日志，不影响业务。不要在同一连接的未提交事务里调用。"""
    if not any(counts.values()):
        return
    factory = sessionmaker(engine)
    values = {k: getattr(CacheStat, k) + v for k, v in counts.items()}
    try:
        for _ in range(2):  # 两个线程同时插入第一行时，后到的一方改为更新
            try:
                with factory.begin() as s:
                    if s.execute(update(CacheStat).where(CacheStat.name == name).values(**values)).rowcount:
                        return
                    s.add(CacheStat(name=name, **counts))
                return
            except IntegrityError:
                continue
    except Exception as e:
        log.warning("写缓存统计失败 %s: %s", name, type(e).__name__)


def _canonical_messages(messages) -> list:
    out = []
    for message in messages:
        if isinstance(message, tuple):
            out.append(list(message))
        else:
            out.append([message.type, message.content])
    return out


def model_identity(model) -> dict:
    """影响输出的模型参数。thinking 模式在 ChatOpenAI 的 extra_body 里。不含密钥。"""
    return dict(type=type(model).__name__,
                name=getattr(model, "model_name", None) or getattr(model, "model", None),
                base_url=getattr(model, "openai_api_base", None),
                temperature=getattr(model, "temperature", None),
                max_tokens=getattr(model, "max_tokens", None),
                extra_body=getattr(model, "extra_body", None))


def cache_key(site: str, model, messages, principal_id: str, *, schema=None, tools=None, extra=None) -> str:
    """提示词都在 messages 里，内容变了哈希就变，不需要手工的提示词版本号。"""
    payload = dict(version=KEY_VERSION, site=site, model=model_identity(model), messages=_canonical_messages(messages),
                   schema=schema.model_json_schema() if schema is not None else None, tools=tools,
                   principal=principal_id, extra=extra)
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


@dataclass(frozen=True)
class CachedResult:
    value: dict
    input_tokens: int
    output_tokens: int
    latency_ms: int


class ResponseCache:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._session = sessionmaker(engine, expire_on_commit=False)
        self.prune()

    def get(self, key: str) -> CachedResult | None:
        now = datetime.now()
        try:
            with self._session.begin() as s:
                row = s.get(LlmCacheEntry, key)
                if row is None or row.expires_at <= now or not isinstance(row.value, dict):
                    return None
                row.last_used_at = now
                return CachedResult(row.value, row.input_tokens, row.output_tokens, row.latency_ms)
        except Exception as e:  # 读不到就当没缓存，照常调用模型
            log.warning("读模型缓存失败 key=%s: %s", key[:12], type(e).__name__)
            return None

    def put(self, key: str, site: str, principal_id: str, value: dict, *, input_tokens=0, output_tokens=0, latency_ms=0) -> bool:
        """写入。失败只记日志，不影响回答。并发写同一键时先写入的留下：相同键的结果可以互换。"""
        now = datetime.now()
        entry = LlmCacheEntry(key=key, site=site, principal_id=principal_id, value=value, input_tokens=input_tokens,
                              output_tokens=output_tokens, latency_ms=latency_ms, created_at=now, last_used_at=now,
                              expires_at=now + timedelta(hours=config.LLM_CACHE_TTL_HOURS))
        try:
            with self._session.begin() as s:
                s.merge(entry)
        except IntegrityError:
            log.debug("模型缓存并发写同一键 key=%s", key[:12])
            return True
        except Exception as e:
            log.warning("写模型缓存失败 key=%s: %s", key[:12], type(e).__name__)
            return False
        self.prune()
        return True

    def prune(self) -> None:
        """删掉过期条目；仍超过上限时删掉最久没用过的。"""
        try:
            with self._session.begin() as s:
                s.execute(delete(LlmCacheEntry).where(LlmCacheEntry.expires_at <= datetime.now()))
                extra = (s.scalar(select(func.count()).select_from(LlmCacheEntry)) or 0) - config.LLM_CACHE_MAX_ENTRIES
                if extra > 0:
                    oldest = select(LlmCacheEntry.key).order_by(LlmCacheEntry.last_used_at, LlmCacheEntry.created_at).limit(extra)
                    s.execute(delete(LlmCacheEntry).where(LlmCacheEntry.key.in_(oldest)))
        except Exception as e:
            log.warning("清理模型缓存失败: %s", type(e).__name__)

    def entries(self) -> int:
        with self._session() as s:
            return s.scalar(select(func.count()).select_from(LlmCacheEntry)) or 0


@dataclass(frozen=True)
class CacheScope:
    cache: ResponseCache
    principal_id: str


_scope: contextvars.ContextVar[CacheScope | None] = contextvars.ContextVar("briefly_llm_cache_scope", default=None)


def active_scope() -> CacheScope | None:
    return _scope.get() if config.LLM_CACHE else None


def bind_scope(cache: ResponseCache, principal_id: str) -> contextvars.Token:
    return _scope.set(CacheScope(cache, principal_id))


def release_scope(token: contextvars.Token) -> None:
    _scope.reset(token)


class ResponseCacheScope:
    """把当前请求的主体和缓存绑定到本次请求。必须在 AuthMiddleware 内层：主体由它写入。
    同步路由经 anyio 进线程池时会复制上下文，所以在路由和工具线程里都读得到。"""

    def __init__(self, app, cache: ResponseCache) -> None:
        self.app, self.cache = app, cache

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = bind_scope(self.cache, current_principal(Request(scope)).id)
        try:
            await self.app(scope, receive, send)
        finally:
            release_scope(token)


def cache_stats(engine: Engine, extra: list[dict] | None = None) -> dict:
    """只读统计。extra 是不写 cache_stats 表的缓存（从已有记录算出来的），口径相同。"""
    factory = sessionmaker(engine)
    with factory() as s:
        rows = list(s.scalars(select(CacheStat).order_by(CacheStat.name)))
        entries = s.scalar(select(func.count()).select_from(LlmCacheEntry)) or 0
    caches = [dict(name=r.name, hits=r.hits, misses=r.misses, stores=r.stores, rejected=r.rejected,
                   saved_input_tokens=r.saved_input_tokens, saved_output_tokens=r.saved_output_tokens, saved_ms=r.saved_ms)
              for r in rows] + (extra or [])
    for item in caches:
        total = item.get("hits", 0) + item.get("misses", 0)
        item["hit_rate"] = round(item.get("hits", 0) / total, 4) if total else None
    return dict(llm_cache=dict(enabled=config.LLM_CACHE, entries=entries, ttl_hours=config.LLM_CACHE_TTL_HOURS,
                               max_entries=config.LLM_CACHE_MAX_ENTRIES),
                caches=caches,
                note="累计计数，取决于使用中有多少重复请求，不代表下一段时间的节省比例；节省量按生成该结果时的实际用量计算。")
