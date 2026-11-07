"""业务数据库（SQLAlchemy）。会话记忆由 LangGraph checkpointer 单独存储，不在这里。"""
import logging
from pathlib import Path

from sqlalchemy import Engine, StaticPool, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase

from . import config

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


def register_models() -> None:
    """导入所有模型，把表注册进 Base.metadata。"""
    from . import context_store, handoffs, intent_log, mail, materials, sessions  # noqa: F401  注册表结构
    from .feishu import service  # noqa: F401
    from .todos import models as _todos  # noqa: F401
    from .tracking import models as _tracking  # noqa: F401
    from .weekly import models as _weekly  # noqa: F401
    from .knowledge import models as _knowledge  # noqa: F401  只新增表，不改旧材料/块
    from . import links  # noqa: F401  工作区与知识库文档的关联
    from . import llm_cache  # noqa: F401  模型响应缓存和缓存统计
    from . import outbound  # noqa: F401  外发回执


def is_postgres(url: str) -> bool:
    return url.startswith(("postgresql:", "postgresql+", "postgres:"))


def database_url(url: str) -> str:
    """PostgreSQL 地址统一用 psycopg 驱动，配置里可以写成普通的 postgresql://。"""
    if url.startswith("postgres://"):
        url = "postgresql://" + url.removeprefix("postgres://")
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url


def create_db_engine(url: str | None = None, **options) -> Engine:
    """创建引擎并把表结构升到最新。sqlite 内存库用于测试。options 原样传给 create_engine。"""
    register_models()
    from . import materials, schema

    url = database_url(url or config.DATABASE_URL)
    kwargs: dict = {}
    memory = url in {"sqlite://", "sqlite:///:memory:"}
    if url.startswith("sqlite"):
        # 工具可能在其他线程执行
        kwargs["connect_args"] = {"check_same_thread": False}
        if memory:
            kwargs["poolclass"] = StaticPool  # 所有连接共用同一个内存库
        else:
            # 解析后再取路径：str(engine.url) 会把盘符的冒号编码成 %3A，直接当路径会在当前目录建出 C%3A/...
            if database := make_url(url).database:
                Path(database).parent.mkdir(parents=True, exist_ok=True)
    elif is_postgres(url):
        kwargs["pool_pre_ping"] = True
    # 不让数据库异常的文字带上 SQL 参数（材料正文、问题原文），这些异常可能进日志
    try:
        engine = create_engine(url, hide_parameters=True, **{**kwargs, **options})
    except ModuleNotFoundError as exc:
        raise RuntimeError("连接 PostgreSQL 需要额外依赖，请安装 postgres 分组：pip install -e '.[rag,postgres]'") from exc
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _enable_foreign_keys)
    if not memory:
        schema.upgrade(engine)
    if engine.dialect.name == "postgresql":
        event.listen(engine, "before_cursor_execute", _replace_nul, retval=True)
        _warn_about_collation(engine)
        return engine  # 下面几步只针对 SQLite 旧库；PostgreSQL 的数据由迁移命令导入，导入后跑一次
    Base.metadata.create_all(engine)  # 在迁移之后：只补缺的表，不和迁移脚本抢着建
    # create_all 不会给已存在的表补建新索引；旧库启动时幂等补建，不改块号或正文。
    for index in materials.MaterialChunk.__table__.indexes:
        if index.name == "ix_material_chunks_material_index":
            index.create(engine, checkfirst=True)
    # 旧库里知识库入库的材料与用户上传混存：幂等标为知识库专属，材料库入口不再暴露
    marked = materials.mark_knowledge_only(engine)
    if marked:
        log.info("已将 %d 份知识库入库材料移出材料库（只经知识库检索读取）", marked)
    orphans = foreign_key_problems(engine)
    if orphans:  # 打开约束之前写进去的失效引用：只报告，不自动删（由人判断怎么处理）
        log.warning("数据库里有 %d 条引用指向不存在的记录：%s", sum(orphans.values()), orphans)
    return engine


def _warn_about_collation(engine: Engine) -> None:
    """非二进制排序（如 en_US）下，按文本排序的结果会和 SQLite 不一样。只提醒，不拦。"""
    with engine.connect() as connection:
        # datlocale 是 PostgreSQL 17 才有的列；builtin、ICU 的排序规则记在这里，libc 的记在 datcollate
        column = "coalesce(datlocale, datcollate)" if connection.dialect.server_version_info >= (17,) else "datcollate"
        collation = connection.exec_driver_sql(f"SELECT {column} FROM pg_database WHERE datname = current_database()").scalar()
    if collation not in {"C", "C.UTF-8", "C.utf8", "POSIX"}:
        log.warning("PostgreSQL 数据库的排序规则是 %s：文本排序会和 SQLite 不同，建议用 C.UTF-8 建库（见 README）", collation)


def _replace_nul(_connection, _cursor, statement, parameters, _context, _executemany):
    """PostgreSQL 的 text 不能存 NUL，整条写入会失败。这里是最后一道保险：换成等长的 U+FFFD，位置不变。

    材料正文在算指纹之前就替换了（MaterialStore），不会走到这里；这里兜住标题、文件名、查询词这类没有指纹的文本。"""
    def clean(value):
        return value.replace("\x00", "\ufffd") if isinstance(value, str) and "\x00" in value else value

    if isinstance(parameters, dict):
        parameters = {key: clean(value) for key, value in parameters.items()}
    elif isinstance(parameters, (list, tuple)):
        parameters = [{key: clean(value) for key, value in row.items()} if isinstance(row, dict) else clean(row) for row in parameters]
    return statement, parameters


def foreign_key_problems(engine: Engine) -> dict[str, int]:
    """SQLite 外键检查：{表名: 引用失效的行数}。PostgreSQL 的约束一直生效，不会有这种数据。"""
    if engine.dialect.name != "sqlite":
        return {}
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
