"""用 PostgreSQL 跑同一批测试：设置 BRIEFLY_TEST_DATABASE_URL 时启用，没设时什么也不做。

现有用例写的是 create_db_engine("sqlite://")、LocalIndex(路径)、sqlite_checkpointer(路径)。这里在测试模块导入之前
把这三个入口换掉，让它们落到 PostgreSQL，用例本身不用改：
- 每个数据库地址（内存库每次、文件库按路径）对应一个独立 schema，靠连接的 search_path 隔离；同一用例里再次打开同一个
  文件地址，得到的还是同一个 schema，“重启后数据还在”这类用例才成立；
- 用例结束时释放引擎和连接池，并删除 schema。

只适用于 SQLite 的用例在 pg_skips.py 里逐条列出原因，跳过的不算验证过。
"""
import atexit
import itertools
import logging
import os
from pathlib import Path

URL = os.getenv("BRIEFLY_TEST_DATABASE_URL", "")
_PREFIX = "brieflytest_"
_numbers = itertools.count(1)
_test_active = False
_schemas: dict[str, str] = {}  # 当前用例：sqlite 地址或路径 → schema
_owned: list[str] = []  # 当前用例要删除的 schema
_engines: list = []
_pools: list = []
_admin = None
real_create_db_engine = None  # 没被替换的 create_db_engine：迁移用例要真的 SQLite 源库


def enabled() -> bool:
    return bool(URL)


def _connection_url(schema: str) -> str:
    from sqlalchemy.engine import make_url

    from briefly.db import database_url

    url = make_url(database_url(URL)).update_query_dict({"options": f"-csearch_path={schema},public"})
    return url.render_as_string(hide_password=False)


def _new_schema() -> str:
    from sqlalchemy import text

    schema = f"{_PREFIX}{os.getpid()}_{next(_numbers)}"
    with _admin.begin() as connection:
        connection.execute(text(f"CREATE SCHEMA {schema}"))
    if _test_active:
        _owned.append(schema)
    return schema


def _schema_for(key: str) -> str:
    if key in {"sqlite://", "sqlite:///:memory:"}:
        return _new_schema()
    if key not in _schemas:
        _schemas[key] = _new_schema()
    return _schemas[key]


def _drop_stale() -> None:
    """上一次异常退出留下的 schema。"""
    from sqlalchemy import text

    with _admin.connect() as connection:
        names = connection.execute(text("SELECT nspname FROM pg_namespace WHERE nspname LIKE :p"), dict(p=_PREFIX.replace("_", r"\_") + "%")).scalars().all()
    for name in names:
        _drop(name)


def _drop(schema: str) -> None:
    from sqlalchemy import text

    # 后台线程可能还拿着锁：等一小会儿，删不掉就留到这次运行结束再删
    try:
        with _admin.begin() as connection:
            connection.execute(text("SET LOCAL lock_timeout = '2s'"))
            connection.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
    except Exception:
        logging.getLogger(__name__).debug("schema %s 暂时删不掉", schema, exc_info=True)


def install() -> None:
    """在任何测试模块导入 briefly.db 的函数之前调用。"""
    if not enabled():
        return
    from sqlalchemy import create_engine, text

    import briefly.db as db

    global _admin
    _admin = create_engine(db.database_url(URL), hide_parameters=True)
    with _admin.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    _drop_stale()
    atexit.register(_drop_stale)

    real_engine = db.create_db_engine
    global real_create_db_engine
    real_create_db_engine = real_engine

    def create_db_engine(url=None):
        from briefly import config

        url = url or config.DATABASE_URL
        if db.is_postgres(url):  # 用例明确要 PostgreSQL（迁移命令的目标库等）
            return real_engine(url)
        engine = real_engine(_connection_url(_schema_for(url)))
        _engines.append(engine)
        return engine

    db.create_db_engine = create_db_engine

    # 现在才导入：这些模块 from .db import create_db_engine 拿到的是上面这个
    import briefly.agent as agent
    from briefly.knowledge import index as knowledge_index
    from briefly.knowledge.pg_index import PostgresIndex

    def local_index(path):
        key = f"index:{Path(path).resolve()}"
        engine = real_engine(_connection_url(_schema_for(key)))
        _engines.append(engine)
        built = PostgresIndex(engine)
        built.path = Path(path)
        return built

    knowledge_index.real_local_index = knowledge_index.LocalIndex
    knowledge_index.LocalIndex = local_index

    def checkpointer(path):
        saver = agent.postgres_checkpointer(_connection_url(_schema_for(f"checkpoint:{Path(path).resolve()}")))
        _pools.append(saver.conn)
        return saver

    agent.sqlite_checkpointer = checkpointer


def start_test() -> None:
    if not enabled():
        return
    global _test_active
    _test_active = True
    _schemas.clear()


def finish_test() -> None:
    if not enabled():
        return
    global _test_active
    _test_active = False
    _schemas.clear()
    while _pools:
        _pools.pop().close()
    import briefly.agent as agent  # 进程级的回退引擎：schema 马上要删，下个用例重建

    agent._fallback = None
    while _engines:
        _engines.pop().dispose()
    while _owned:
        _drop(_owned.pop())
