"""业务库结构迁移（Alembic）。脚本在 briefly/migrations，随包分发。

- PostgreSQL 只走迁移，派生索引表也在迁移里。
- 多个进程同时第一次启动时要排队（API、命令行、定时任务可能同时起）：PostgreSQL 用咨询锁，SQLite 用 BEGIN IMMEDIATE。
  拿到锁之后重读版本，别的进程已经升完就直接返回。
- SQLite 文件也走迁移：基线脚本的建表和建索引都带 IF NOT EXISTS，旧库只补缺的表和索引，不动已有数据，
  然后打上版本号。之后版本相同就直接返回。db.py 随后照旧跑 create_all 和启动补丁（SQLite 的现有行为不变）。
- 内存 SQLite（测试和无配置时的回退）不经过这里，只用 create_all。
"""
from functools import lru_cache
from pathlib import Path

from sqlalchemy import Engine, MetaData, text

_ADVISORY_LOCK = 0x42524644
_SQLITE_LOCK_WAIT_MS = 120_000  # 老库第一次升级要建几十张表，默认的 5 秒不够等
# 会话记录的表由 LangGraph 的 PostgresSaver.setup() 管理，不归这里
_CHECKPOINT_TABLES = {"checkpoints", "checkpoint_writes", "checkpoint_blobs", "checkpoint_migrations"}


def alembic_config(connection=None):
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
    if connection is not None:
        config.attributes["connection"] = connection
    return config


@lru_cache(maxsize=1)
def head() -> str:
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(alembic_config()).get_current_head()


def current(connection) -> str | None:
    from alembic.migration import MigrationContext

    return MigrationContext.configure(connection).get_current_revision()


def target_metadata(dialect: str) -> list[MetaData]:
    from .db import Base, register_models

    register_models()
    if dialect != "postgresql":
        return [Base.metadata]
    from .knowledge.pg_tables import metadata

    return [Base.metadata, metadata]


def include_object(_object, name, kind, _reflected, _compare_to) -> bool:
    return not (kind == "table" and name in _CHECKPOINT_TABLES)


def upgrade(engine: Engine) -> None:
    """把数据库升到最新版本；已是最新时不做任何事。"""
    from alembic import command

    with engine.connect() as connection:
        if current(connection) == head():
            return
    sqlite = engine.dialect.name == "sqlite"
    try:
        with engine.begin() as connection:
            if sqlite:
                connection.exec_driver_sql(f"PRAGMA busy_timeout={_SQLITE_LOCK_WAIT_MS}")
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            elif engine.dialect.name == "postgresql":
                connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), dict(key=_ADVISORY_LOCK))
            if current(connection) != head():
                command.upgrade(alembic_config(connection), "head")
    finally:
        if sqlite:
            engine.dispose()  # 丢掉这条调大了等待时间的连接，之后的连接用默认值
