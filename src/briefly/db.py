"""业务数据库（SQLAlchemy）。会话记忆由 LangGraph checkpointer 单独存储，不在这里。"""
from pathlib import Path

from sqlalchemy import Engine, StaticPool, create_engine
from sqlalchemy.orm import DeclarativeBase

from . import config


class Base(DeclarativeBase):
    pass


def create_db_engine(url: str | None = None) -> Engine:
    """创建引擎并建表。sqlite 内存库用于测试。"""
    from . import materials  # noqa: F401  注册表结构

    url = url or config.DATABASE_URL
    kwargs: dict = {}
    if url.startswith("sqlite"):
        # 工具可能在其他线程执行
        kwargs["connect_args"] = {"check_same_thread": False}
        if url in {"sqlite://", "sqlite:///:memory:"}:
            kwargs["poolclass"] = StaticPool  # 所有连接共用同一个内存库
        else:
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, **kwargs)
    Base.metadata.create_all(engine)
    return engine
