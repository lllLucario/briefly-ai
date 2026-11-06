"""`briefly db`：结构升级、从 SQLite 搬到 PostgreSQL、重建派生索引。

migrate-from-sqlite 的保证：
- 源库只读（sqlite3 的 mode=ro），目标业务表必须全空；
- 业务表在一个事务里按外键顺序导入，核对每张表的行数和内容校验、复制并核对会话记录之后才提交，中断或校验失败时目标
  仍是空的，可以直接重来；
- 索引不复制（SQLite 和 PostgreSQL 的制品不互认），提交后用当前 Embedding 重建并逐代 verify()，中断后用 rebuild-index 重来；
- 飞书令牌文件和知识库原件目录是文件，不在数据库里，不动。回滚：把 DATABASE_URL 改回去即可，源库没有被改过。
"""
import argparse
from datetime import date, datetime
import hashlib
import json
import logging
from pathlib import Path
import sqlite3
import sys

from sqlalchemy import NullPool, String, create_engine, func, inspect, select, text, update
from sqlalchemy.orm import Session

from . import config
from .db import Base, create_db_engine, database_url, is_postgres, register_models

log = logging.getLogger(__name__)

_BATCH = 50
_LOCK = 0x42524647
_REPLACEMENT = "�"


class MigrationRefused(Exception):
    """迁移前检查没通过，目标库没有写入。"""


def _read_only(path: Path):
    return create_engine("sqlite://", creator=lambda: sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False),
                         poolclass=NullPool, hide_parameters=True)


def _copied_columns(table, columns: set[str]):
    return [column for column in table.columns if column.name in columns]


def preflight(source) -> dict:
    """搬之前能看出来的问题都在这里报出来，不写目标库。返回各表要复制的列。"""
    problems, plan = [], {}
    inspector = inspect(source)
    existing = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing:
            plan[table.name] = None  # 旧库没有这张表：目标里就是空表
            continue
        columns = {column["name"] for column in inspector.get_columns(table.name)}
        unknown = columns - {column.name for column in table.columns}
        if unknown:
            problems.append(f"{table.name} 有当前模型没有的列 {sorted(unknown)}，请先用当前版本启动一次或人工确认")
        for column in table.columns:
            if column.name not in columns and not column.nullable and column.default is None and column.server_default is None:
                problems.append(f"{table.name} 缺少必填列 {column.name}")
        plan[table.name] = columns
    with source.connect() as connection:
        orphans = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
        counts: dict[str, int] = {}
        for row in orphans:
            counts[row[0]] = counts.get(row[0], 0) + 1
        if counts:
            problems.append(f"有引用指向不存在的记录，PostgreSQL 的外键不接受：{counts}。请先人工处理")
        for table in Base.metadata.sorted_tables:
            columns = plan[table.name]
            if not columns:
                continue
            for column in table.columns:
                if column.name in columns and isinstance(column.type, String) and column.type.length:
                    over = connection.scalar(select(func.count()).select_from(table).where(func.length(column) > column.type.length))
                    if over:
                        problems.append(f"{table.name}.{column.name} 有 {over} 行超过长度上限 {column.type.length}（SQLite 不检查，PostgreSQL 会拒绝）")
        if plan.get("materials") and plan.get("knowledge_versions") is not None:
            referenced = connection.exec_driver_sql(
                "SELECT count(*) FROM materials m WHERE instr(m.content, char(0)) > 0 AND EXISTS "
                "(SELECT 1 FROM knowledge_versions v WHERE v.material_id = m.id)").scalar()
            if referenced:
                problems.append(f"有 {referenced} 份知识库材料的正文含 NUL。替换后指纹会变，版本核验不会通过，请在 SQLite 上重新导入这些文件再迁移")
    if problems:
        raise MigrationRefused("；".join(problems))
    return plan


def _clean(value):
    return value.replace("\x00", _REPLACEMENT) if isinstance(value, str) and "\x00" in value else value


def _normalized(table, row: dict) -> dict:
    """PostgreSQL 的 text 不能存 NUL：和新写入一样换成 U+FFFD。没有知识库版本引用的材料，指纹跟着重算（preflight 已拦住其他情况）。"""
    cleaned = {key: _clean(value) for key, value in row.items()}
    if table.name == "materials" and cleaned.get("content") != row.get("content"):
        cleaned["content_hash"] = hashlib.sha256(cleaned["content"].encode("utf-8")).hexdigest()
    return cleaned


def _canonical(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return value


def _row_digest(row: dict) -> str:
    payload = json.dumps({key: _canonical(value) for key, value in sorted(row.items())}, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _table_digest(digests: list[str]) -> str:
    return hashlib.sha256("".join(sorted(digests)).encode()).hexdigest()


def _copy_table(source_connection, target_connection, table, columns: set[str]) -> dict:
    copied = _copied_columns(table, columns)
    self_references = [fk.parent.name for fk in table.foreign_keys if fk.column.table is table]
    primary = [column.name for column in table.primary_key.columns]
    # 连上父子关系的 UPDATE 不能触发 onupdate，否则 updated_at 变成现在，内容校验也不会过
    stamped = [column.name for column in copied if column.onupdate is not None]
    digests, deferred, count = [], [], 0
    result = source_connection.execute(select(*copied), execution_options=dict(yield_per=_BATCH))
    for batch in result.mappings().partitions(_BATCH):
        rows = []
        for raw in batch:
            row = _normalized(table, dict(raw))
            digests.append(_row_digest(row))
            count += 1
            if self_references:  # 父子行的先后不一定，先留空，整表写完再连上
                pending = {name: row[name] for name in self_references if row.get(name) is not None}
                if pending:
                    deferred.append(({name: row[name] for name in primary}, {**pending, **{name: row[name] for name in stamped}}))
                    row = {**row, **{name: None for name in pending}}
            rows.append(row)
        target_connection.execute(table.insert(), rows)
    for key, pending in deferred:
        target_connection.execute(update(table).where(*(table.c[name] == value for name, value in key.items())).values(**pending))
    actual = [_row_digest(dict(row)) for row in target_connection.execute(select(*copied), execution_options=dict(yield_per=_BATCH)).mappings()]
    source_digest, target_digest = _table_digest(digests), _table_digest(actual)
    if count != len(actual) or source_digest != target_digest:
        raise MigrationRefused(f"{table.name} 导入后校验不一致（源 {count} 行，目标 {len(actual)} 行），目标库已回滚")
    return dict(rows=count, sha256=target_digest)


def _reset_sequences(connection) -> None:
    """显式写入编号不会推进序列，不重置的话下一条新数据就撞主键。待办编号还要避开账本里删掉的编号（永不重用）。"""
    for table in Base.metadata.sorted_tables:
        primary = list(table.primary_key.columns)
        if len(primary) != 1 or not primary[0].type.python_type is int:
            continue
        column = primary[0].name
        sequence = connection.execute(text("SELECT pg_get_serial_sequence(:t, :c)"), dict(t=table.name, c=column)).scalar()
        if not sequence:
            continue
        largest = connection.scalar(select(func.max(table.c[column]))) or 0
        if table.name == "todos":
            largest = max(largest, connection.execute(text("SELECT coalesce(max(id), 0) FROM todo_identities")).scalar())
        connection.execute(text("SELECT setval(:s, :v, :called)"), dict(s=sequence, v=max(largest, 1), called=largest > 0))


def _refuse_if_not_empty(connection) -> None:
    from .knowledge.pg_tables import metadata

    occupied = [table.name for table in [*Base.metadata.sorted_tables, *metadata.sorted_tables]
                if connection.scalar(select(func.count()).select_from(table))]
    if occupied:
        raise MigrationRefused(f"目标库不是空的，已拒绝：{occupied[:10]}")


def copy_checkpoints(path: Path, url: str) -> dict:
    """会话记录经 checkpointer API 复制，两边的存储格式由 LangGraph 负责。重复执行时先清掉同一会话，结果一样。"""
    from langgraph.checkpoint.sqlite import SqliteSaver

    from .agent import postgres_checkpointer

    if not path.exists():
        return dict(threads=0, checkpoints=0, note=f"没有会话记录文件 {path.name}")
    source_connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, check_same_thread=False)
    target = postgres_checkpointer(url)
    try:
        if not source_connection.execute("SELECT 1 FROM sqlite_master WHERE name='checkpoints'").fetchone():
            return dict(threads=0, checkpoints=0)
        source = SqliteSaver(source_connection)
        source.is_setup = True  # 只读连接不能建表
        threads = [row[0] for row in source_connection.execute("SELECT DISTINCT thread_id FROM checkpoints ORDER BY 1")]
        total = 0
        for thread in threads:
            target.delete_thread(thread)
            items = list(source.list(dict(configurable=dict(thread_id=thread))))
            for item in reversed(items):  # 从旧到新
                settings = item.config["configurable"]
                parent = (item.parent_config or {}).get("configurable", {}).get("checkpoint_id")
                target.put(dict(configurable=dict(thread_id=thread, checkpoint_ns=settings.get("checkpoint_ns", ""), checkpoint_id=parent)),
                           item.checkpoint, item.metadata, dict(item.checkpoint["channel_versions"]))
                tasks: dict[str, list] = {}
                for task_id, channel, value in item.pending_writes or []:
                    tasks.setdefault(task_id, []).append((channel, value))
                for task_id, writes in tasks.items():
                    target.put_writes(item.config, writes, task_id)
                total += 1
            for item in items:
                copied = target.get_tuple(item.config)
                if copied is None or copied.checkpoint["id"] != item.checkpoint["id"] or copied.metadata != item.metadata \
                        or copied.checkpoint["channel_values"] != item.checkpoint["channel_values"] \
                        or sorted(map(repr, copied.pending_writes or [])) != sorted(map(repr, item.pending_writes or [])):
                    raise MigrationRefused(f"会话记录校验不一致：{thread}，目标业务表已回滚；会话记录重来时会覆盖")
        return dict(threads=len(threads), checkpoints=total)
    finally:
        source_connection.close()
        target.conn.close()


def migrate_from_sqlite(source_path, target_url: str, checkpoints_path=None) -> dict:
    register_models()
    source_path = Path(source_path)
    if not source_path.exists():
        raise MigrationRefused(f"源库不存在：{source_path}")
    if not is_postgres(database_url(target_url)):
        raise MigrationRefused("目标必须是 PostgreSQL 地址")
    source = _read_only(source_path)
    plan = preflight(source)
    target = create_db_engine(target_url)
    tables, checkpoints = {}, {}
    # setup() 里有 CREATE INDEX CONCURRENTLY，要等库内所有事务结束。必须在开始下面的大事务之前跑完，否则互相等死
    from .agent import postgres_checkpointer

    postgres_checkpointer(target_url).conn.close()
    try:
        with source.connect() as source_connection, target.begin() as connection:
            if not connection.execute(text("SELECT pg_try_advisory_xact_lock(:key)"), dict(key=_LOCK)).scalar():
                raise MigrationRefused("另一个迁移正在进行")
            _refuse_if_not_empty(connection)
            for table in Base.metadata.sorted_tables:
                columns = plan[table.name]
                tables[table.name] = _copy_table(source_connection, connection, table, columns) if columns else dict(rows=0, sha256=_table_digest([]))
            _reset_sequences(connection)
            checkpoints = copy_checkpoints(Path(checkpoints_path or config.DB_PATH), target_url)
        # 旧库里知识库入库的材料幂等标记：SQLite 每次启动都做，PostgreSQL 只在导入后做这一次
        from .materials import mark_knowledge_only

        marked = mark_knowledge_only(target)
        flags = _flags(target)
    finally:
        source.dispose()
        target.dispose()
    return dict(tables=tables, checkpoints=checkpoints, knowledge_only_marked=marked, flags=flags)


def _flags(engine) -> dict:
    """单独列出和外发限制、索引代次有关的数字，方便对照。"""
    with Session(engine) as s:
        count = lambda name: s.execute(text(f"SELECT count(*) FROM {name}")).scalar()
        return dict(workspace_egress_holds=count("workspace_egress_holds"), knowledge_base_removals=count("knowledge_base_removals"),
                    outbound_claims=count("outbound_claims"), knowledge_index_generations=count("knowledge_index_generations"),
                    knowledge_publications=count("knowledge_publications"))


def rebuild_index(target_url: str, encoder) -> dict:
    """按业务库重建全部就绪代次并逐代 verify()；重复执行结果一样。工作区索引随后对齐（可见性由 materials 决定）。"""
    from sqlalchemy.orm import sessionmaker

    from .knowledge.lifecycle import IndexLifecycle
    from .knowledge.models import IndexGeneration, KnowledgeVersion
    from .knowledge.pg_index import PostgresIndex
    from .knowledge.service import embedding_identity, fingerprint
    from .workspace_index import WorkspaceIndex

    engine = create_db_engine(target_url)
    try:
        index = PostgresIndex(engine)
        wanted = fingerprint(embedding_identity(encoder.identity))
        built, skipped = 0, []
        factory = sessionmaker(engine, expire_on_commit=False)
        with factory() as s:
            generations = list(s.scalars(select(IndexGeneration).where(IndexGeneration.status == "ready").order_by(IndexGeneration.created_at)))
        for generation in generations:
            identity = dict(generation.embedding_identity)
            identity.pop("index_contract", None)
            if fingerprint(identity) != wanted:
                skipped.append(generation.id)
                continue
            with factory() as s:
                version = s.get(KnowledgeVersion, generation.version_id)
                rows = IndexLifecycle.rows(s, version)
            vectors = []
            for start in range(0, len(rows), 16):
                vectors.extend(encoder.embed_documents([content for _, content in rows[start:start + 16]]))
            index.build(generation, rows, vectors)
            index.verify(generation, rows)
            built += 1
        workspace = WorkspaceIndex(engine, "", lambda: encoder).sync(encoder)
        if skipped:
            log.warning("%d 个代次用的 Embedding 和当前配置不同，没有重建：%s", len(skipped), skipped[:5])
        return dict(generations_rebuilt=built, generations_skipped=skipped, workspace=workspace)
    finally:
        engine.dispose()


def _encoder():
    if not config.EMBEDDING_DIR:
        return None
    from .llm import create_local_embeddings

    return create_local_embeddings(config.EMBEDDING_DIR)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="briefly db", description="数据库：结构升级、从 SQLite 迁移到 PostgreSQL、重建索引")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("upgrade", help="把 DATABASE_URL 指向的库升到最新结构")
    move = commands.add_parser("migrate-from-sqlite", help="把 SQLite 数据复制到空的 PostgreSQL。源库只读")
    move.add_argument("--source", default="data/app.db", help="SQLite 业务库文件（默认 data/app.db）")
    move.add_argument("--checkpoints", default=None, help="SQLite 会话记录文件（默认 BRIEFLY_DB_PATH）")
    move.add_argument("--target", default=None, help="PostgreSQL 地址（默认 DATABASE_URL）")
    move.add_argument("--no-index", action="store_true", help="先不重建索引，之后用 rebuild-index")
    rebuild = commands.add_parser("rebuild-index", help="按业务库重建知识和工作区索引，并校验")
    rebuild.add_argument("--target", default=None, help="PostgreSQL 地址（默认 DATABASE_URL）")
    args = parser.parse_args(argv)

    if args.command == "upgrade":
        create_db_engine().dispose()
        print("数据库结构已是最新")
        return 0
    target = args.target or config.DATABASE_URL
    try:
        report = {}
        if args.command == "migrate-from-sqlite":
            report = migrate_from_sqlite(args.source, target, args.checkpoints)
            if args.no_index:
                report["index"] = "未重建：运行 briefly db rebuild-index"
        if not report.get("index"):
            encoder = _encoder()
            if encoder is None:
                report["index"] = "未重建：没有配置 BRIEFLY_EMBEDDING_DIR。配置后运行 briefly db rebuild-index"
            else:
                report["index"] = rebuild_index(target, encoder)
    except MigrationRefused as exc:
        print(f"迁移没有执行：{exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0
