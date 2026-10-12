"""上下文管理的持久化（业务库，与材料同库）：会话记忆、超大工具结果、调用观测、Map 结果缓存。

与 checkpointer 的分工：checkpointer 保存完整原始对话（含工具交互），这里只存派生数据，删掉不影响原始历史。
写入失败只记日志、不打断对话（观测数据、缓存），会话记忆每个版本一次事务写入，失败时保留上一版本。
"""
import hashlib
import logging
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Engine,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from .db import Base

log = logging.getLogger(__name__)


class SessionMemoryVersion(Base):
    """会话记忆的一个版本。每次压缩新增一行，不改旧行，可回看和回退。"""

    __tablename__ = "session_memory"
    __table_args__ = (UniqueConstraint("thread_id", "version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSON)  # SessionMemory
    # 压缩检查点：已压进记忆的最后一条消息的 id，及到它为止的消息条数（id 找不到时的退路）
    compacted_message_id: Mapped[str] = mapped_column(String(100), default="")
    compacted_count: Mapped[int] = mapped_column(Integer, default=0)
    changes: Mapped[dict] = mapped_column(JSON, default=dict)  # 本次增删了哪些条目，便于溯源
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ToolResultRecord(Base):
    """超出预算、没有完整放进上下文的工具结果。"""

    __tablename__ = "tool_results"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[str] = mapped_column(String(32), default="")
    tool_name: Mapped[str] = mapped_column(String(100))
    tool_call_id: Mapped[str] = mapped_column(String(100), default="", index=True)
    args: Mapped[dict] = mapped_column(JSON, default=dict)
    content: Mapped[str] = mapped_column(Text)
    tokens: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ContextUsage(Base):
    """一次模型调用的上下文构成。只存数量和哈希，不存正文。"""

    __tablename__ = "context_usage"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    run_id: Mapped[str] = mapped_column(String(32), index=True, default="")
    model: Mapped[str] = mapped_column(String(100), default="")
    task_type: Mapped[str] = mapped_column(String(20))  # chat / compaction / map / reduce / synthesize
    context_limit: Mapped[int] = mapped_column(Integer, default=0)
    estimated_tokens: Mapped[int] = mapped_column(Integer, default=0)  # 原始估算
    budgeted_tokens: Mapped[int] = mapped_column(Integer, default=0)  # 按校准比例放大后
    system_tokens: Mapped[int] = mapped_column(Integer, default=0)
    tool_schema_tokens: Mapped[int] = mapped_column(Integer, default=0)
    memory_tokens: Mapped[int] = mapped_column(Integer, default=0)
    history_tokens: Mapped[int] = mapped_column(Integer, default=0)
    material_tokens: Mapped[int] = mapped_column(Integer, default=0)  # 消息里附的材料全文
    tool_result_tokens: Mapped[int] = mapped_column(Integer, default=0)
    current_tokens: Mapped[int] = mapped_column(Integer, default=0)  # 当前轮（用户消息 + 本轮工具往返）
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)  # API 返回的实际值
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tool_call_count: Mapped[int] = mapped_column(Integer, default=0)
    history_turns: Mapped[int] = mapped_column(Integer, default=0)  # 窗口内的历史轮数
    omitted_turns: Mapped[int] = mapped_column(Integer, default=0)  # 窗口外且未压缩的轮数（降级）
    stubbed_results: Mapped[int] = mapped_column(Integer, default=0)  # 换成引用的工具结果 / 材料块数
    compaction_triggered: Mapped[bool] = mapped_column(Boolean, default=False)
    context_overflow: Mapped[bool] = mapped_column(Boolean, default=False)
    material_hashes: Mapped[list] = mapped_column(JSON, default=list)  # 本次携带的材料块哈希，查重复传输
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class ChunkExtraction(Base):
    """Map 阶段单块的提取结果。键 = 块内容 + 关注点 + 周期 + 提示词的哈希，同样输入不再重算。"""

    __tablename__ = "chunk_extractions"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    items: Mapped[list] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


def content_hash(*parts: str) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


class ContextStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._session = sessionmaker(engine, expire_on_commit=False)

    # ---- 会话记忆
    def latest_memory(self, thread_id: str) -> SessionMemoryVersion | None:
        query = (
            select(SessionMemoryVersion)
            .where(SessionMemoryVersion.thread_id == thread_id)
            .order_by(SessionMemoryVersion.version.desc())
            .limit(1)
        )
        with self._session() as s:
            return s.scalar(query)

    def save_memory(
        self, thread_id: str, data: dict, message_id: str, count: int, changes: dict
    ) -> SessionMemoryVersion:
        """新增一个版本（单个事务）。版本号冲突（并发压缩）时抛出，调用方保留旧版本。"""
        with self._session() as s, s.begin():
            current = s.scalar(
                select(func.max(SessionMemoryVersion.version)).where(SessionMemoryVersion.thread_id == thread_id)
            )
            row = SessionMemoryVersion(
                thread_id=thread_id,
                version=(current or 0) + 1,
                data=data,
                compacted_message_id=message_id,
                compacted_count=count,
                changes=changes,
            )
            s.add(row)
        return row

    def memory_versions(self, thread_id: str) -> list[SessionMemoryVersion]:
        query = (
            select(SessionMemoryVersion)
            .where(SessionMemoryVersion.thread_id == thread_id)
            .order_by(SessionMemoryVersion.version)
        )
        with self._session() as s:
            return list(s.scalars(query))

    # ---- 工具结果
    def save_tool_result(
        self, thread_id: str, run_id: str, tool_name: str, tool_call_id: str, args: dict, content: str, tokens: int
    ) -> str:
        result_id = "r_" + uuid.uuid4().hex[:10]
        with self._session() as s, s.begin():
            s.add(
                ToolResultRecord(
                    id=result_id,
                    thread_id=thread_id,
                    run_id=run_id,
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    args=args,
                    content=content,
                    tokens=tokens,
                )
            )
        return result_id

    def tool_result(self, result_id: str) -> ToolResultRecord | None:
        with self._session() as s:
            return s.get(ToolResultRecord, result_id)

    # ---- 观测
    def record_usage(self, **fields) -> None:
        try:
            with self._session() as s, s.begin():
                s.add(ContextUsage(**fields))
        except Exception as e:  # 观测数据不能影响对话
            log.warning("记录上下文用量失败: %s", e)

    def usage(self, thread_id: str | None = None, run_id: str | None = None, limit: int = 50) -> list[ContextUsage]:
        query = select(ContextUsage).order_by(ContextUsage.id.desc()).limit(limit)
        if thread_id:
            query = query.where(ContextUsage.thread_id == thread_id)
        if run_id:
            query = query.where(ContextUsage.run_id == run_id)
        with self._session() as s:
            return list(s.scalars(query))

    def token_scale(self, model: str, samples: int = 20) -> float | None:
        """最近几次对话调用的“实际 / 估算”中位数，用于启动时校准计数器。"""
        query = (
            select(ContextUsage.input_tokens, ContextUsage.estimated_tokens)
            .where(ContextUsage.model == model, ContextUsage.input_tokens.is_not(None), ContextUsage.estimated_tokens > 200)
            .order_by(ContextUsage.id.desc())
            .limit(samples)
        )
        with self._session() as s:
            ratios = sorted(actual / est for actual, est in s.execute(query).all() if actual and est)
        return ratios[len(ratios) // 2] if ratios else None

    # ---- Map 结果缓存
    def cached_extraction(self, key: str) -> list | None:
        try:
            with self._session() as s:
                row = s.get(ChunkExtraction, key)
            return row.items if row else None
        except Exception as e:
            log.warning("读取提取缓存失败: %s", e)
            return None

    def cache_extraction(self, key: str, items: list) -> None:
        try:
            with self._session() as s, s.begin():
                s.merge(ChunkExtraction(key=key, items=items))
        except Exception as e:
            log.warning("写入提取缓存失败: %s", e)
