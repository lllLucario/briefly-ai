"""业务数据库（SQLAlchemy）。会话记忆由 LangGraph checkpointer 单独存储，不在这里。"""
import logging
from pathlib import Path

from sqlalchemy import Engine, StaticPool, create_engine, event
from sqlalchemy.orm import DeclarativeBase

from . import config

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def create_db_engine(url: str | None = None) -> Engine:
    """创建引擎并建表。sqlite 内存库用于测试。"""
    from . import context_store, handoffs, intent_log, mail, materials, sessions  # noqa: F401  注册表结构
    from .feishu import service  # noqa: F401
    from .todos import models as _todos  # noqa: F401
    from .tracking import models as _tracking  # noqa: F401
    from .weekly import models as _weekly  # noqa: F401
    from .knowledge import models as _knowledge  # noqa: F401  只新增表，不改旧材料/块

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
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _enable_foreign_keys)
    Base.metadata.create_all(engine)
    # create_all 不会给已存在的表补建新索引；旧库启动时幂等补建，不改块号或正文。
    for index in materials.MaterialChunk.__table__.indexes:
        if index.name == "ix_material_chunks_material_index":
            index.create(engine, checkfirst=True)
    # 旧库里知识库入库的材料与用户上传混存：幂等标为知识库专属，材料库入口不再暴露
    marked = materials.mark_knowledge_only(engine)
    if marked:
        log.info("已将 %d 份知识库入库材料移出材料库（只经知识库检索读取）", marked)
    if engine.dialect.name == "sqlite":
        orphans = foreign_key_problems(engine)
        if orphans:  # 打开约束之前写进去的失效引用：只报告，不自动删（由人判断怎么处理）
            log.warning("数据库里有 %d 条引用指向不存在的记录：%s", sum(orphans.values()), orphans)
    return engine


def foreign_key_problems(engine: Engine) -> dict[str, int]:
    """SQLite 外键检查：{表名: 引用失效的行数}。"""
    with engine.connect() as conn:
        rows = conn.exec_driver_sql("PRAGMA foreign_key_check").all()
    out: dict[str, int] = {}
    for row in rows:
        out[row[0]] = out.get(row[0], 0) + 1
    return out


def _enable_foreign_keys(dbapi_connection, _record) -> None:
    """SQLite 默认不检查外键：每个连接打开约束，表结构里的 ON DELETE CASCADE / SET NULL 才生效，
    也不会写进指向不存在记录的引用。"""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()
