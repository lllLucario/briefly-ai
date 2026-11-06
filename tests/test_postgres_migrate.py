"""briefly db migrate-from-sqlite：源库只读、目标必须为空、行数和内容校验、序列重置、中断后能重来。"""
import hashlib
import json
from pathlib import Path
import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver
from langchain_core.messages import HumanMessage
from langgraph.graph import START, MessagesState, StateGraph
import pytest
pytest.importorskip("pgvector", reason="PostgreSQL 用例需安装 .[postgres]")
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from briefly import db_migrate
from briefly.db import Base, register_models
from briefly.knowledge.index import vector_blob  # noqa: F401
from briefly.knowledge.models import IndexGeneration
from briefly.knowledge.pg_index import PostgresIndex
from briefly.knowledge.service import KnowledgeService
from briefly.links import EgressHold
from briefly.materials import Material, MaterialStore
from briefly.outbound import OutboundClaim
from briefly.todos.models import Todo, TodoIdentity
import pg_mode
from test_knowledge_slicing import Encoder

pytestmark = pytest.mark.postgres


@pytest.fixture
def target():
    """空的 PostgreSQL schema 的地址。"""
    return pg_mode._connection_url(pg_mode._new_schema())


@pytest.fixture
def source(tmp_path):
    """真的 SQLite 文件，不经过 pg_mode 的替换。"""
    path = tmp_path / "app.db"
    engine = pg_mode.real_create_db_engine(f"sqlite:///{path.as_posix()}")
    store, knowledge = MaterialStore(engine), KnowledgeService(engine)
    material, _ = store.add_text("制度", "住宿上限650元。" * 20)
    store.add_text("普通材料", "含\x00NUL 的正文")
    kb = knowledge.create("制度库")
    document, version, _ = knowledge.attach(kb.id, material.id)
    from briefly.knowledge.lifecycle import IndexLifecycle
    from briefly.knowledge.index import real_local_index

    IndexLifecycle(engine, real_local_index(tmp_path / "index.sqlite")).build(kb.id, document.id, version.id, Encoder(), expected_revision=1)
    with Session(engine) as s:
        s.add_all([Todo(id=5, title="子", parent_id=None), Todo(id=3, title="父")])
        s.flush()
        s.get(Todo, 5).parent_id = 3
        s.add(TodoIdentity(id=9))  # 账本里还有已彻底删除的 9 号：迁移后不能重用
        s.add(EgressHold(material_id=material.id, kb_id="k1", kb_name="库", document_id="d1", document_title="文档"))
        s.add(OutboundClaim(key="k" * 64, kind="mail", report_id=1, status="done", record_id=2))
        s.commit()
    engine.dispose()
    return path


@pytest.fixture
def checkpoints(tmp_path):
    path = tmp_path / "memory.db"
    connection = sqlite3.connect(path, check_same_thread=False)
    graph = StateGraph(MessagesState)
    graph.add_node("echo", lambda state: dict(messages=[HumanMessage("回声")]))
    graph.add_edge(START, "echo")
    compiled = graph.compile(checkpointer=SqliteSaver(connection))
    for thread in ("t1", "t2"):
        compiled.invoke(dict(messages=[HumanMessage(f"你好 {thread}")]), dict(configurable=dict(thread_id=thread)))
    connection.close()
    return path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_migrates_everything_and_checks_it(source, checkpoints, target):
    before = digest(source)
    report = db_migrate.migrate_from_sqlite(source, target, checkpoints)
    assert digest(source) == before  # 源库一个字节都没变
    assert report["tables"]["materials"]["rows"] == 2
    assert report["tables"]["todos"]["rows"] == 2
    assert report["checkpoints"]["threads"] == 2 and report["checkpoints"]["checkpoints"] > 2
    assert report["flags"]["workspace_egress_holds"] == 1 and report["flags"]["outbound_claims"] == 1
    assert report["flags"]["knowledge_index_generations"] == 1
    assert report["warnings"] == []

    engine = pg_mode.real_create_db_engine(target)
    with Session(engine) as s:
        assert s.get(Todo, 5).parent_id == 3
        # 新待办避开账本里的编号
        next_todo = Todo(title="新")
        s.add(next_todo)
        s.flush()
        assert next_todo.id > 9
        replaced = s.scalar(select(Material).where(Material.title == "普通材料"))
        assert replaced.content == "含�NUL 的正文"
        assert replaced.content_hash == hashlib.sha256(replaced.content.encode()).hexdigest()
    engine.dispose()

    from briefly.agent import postgres_checkpointer

    saver = postgres_checkpointer(target)
    state = saver.get_tuple(dict(configurable=dict(thread_id="t1", checkpoint_ns="")))
    assert [m.content for m in state.checkpoint["channel_values"]["messages"]] == ["你好 t1", "回声"]
    saver.conn.close()


def _add_chat_session(path: Path) -> None:
    db = sqlite3.connect(path)
    db.execute("INSERT INTO chat_sessions(thread_id,title,created_at,updated_at) VALUES ('t1','会话','2026-01-01 00:00:00','2026-01-01 00:00:00')")
    db.commit()
    db.close()


@pytest.mark.parametrize("leave_file", [True, False], ids=["main_file_without_wal", "no_file"])
def test_warns_when_sessions_exist_but_no_checkpoints_copied(source, target, tmp_path, capsys, leave_file):
    """WAL 模式的会话记录文件只复制主文件时，里面没有表，看起来像“没有记录”。只警告，不拒绝。"""
    _add_chat_session(source)
    copied = tmp_path / "copied-without-wal.db"
    if leave_file:
        sqlite3.connect(copied).close()
        copied.write_bytes(b"")
    code = db_migrate.main(["migrate-from-sqlite", "--source", str(source), "--checkpoints", str(copied), "--target", target, "--no-index"])
    captured = capsys.readouterr()
    assert code == 0
    assert "-wal" in captured.err and "警告" in captured.err
    assert "-wal" in json.loads(captured.out)["warnings"][0]


def test_no_warning_when_sessions_and_checkpoints_both_present(source, checkpoints, target):
    _add_chat_session(source)
    assert db_migrate.migrate_from_sqlite(source, target, checkpoints)["warnings"] == []


def test_no_warning_when_there_are_no_sessions(source, target, tmp_path):
    assert db_migrate.migrate_from_sqlite(source, target, tmp_path / "none.db")["warnings"] == []


def test_index_rebuild_passes_verify_and_can_repeat(source, checkpoints, target):
    db_migrate.migrate_from_sqlite(source, target, checkpoints)
    for _ in range(2):
        result = db_migrate.rebuild_index(target, Encoder())
        assert result["generations_rebuilt"] == 1 and result["generations_skipped"] == []
    engine = pg_mode.real_create_db_engine(target)
    with Session(engine) as s:
        generation = s.scalar(select(IndexGeneration).where(IndexGeneration.status == "ready"))
        from briefly.knowledge.lifecycle import IndexLifecycle
        from briefly.knowledge.models import KnowledgeVersion

        rows = IndexLifecycle.rows(s, s.get(KnowledgeVersion, generation.version_id))
    PostgresIndex(engine).verify(generation, rows)
    engine.dispose()


def test_refuses_target_that_is_not_empty(source, checkpoints, target):
    db_migrate.migrate_from_sqlite(source, target, checkpoints)
    with pytest.raises(db_migrate.MigrationRefused, match="不是空的"):
        db_migrate.migrate_from_sqlite(source, target, checkpoints)


def test_interrupted_migration_leaves_target_empty_and_can_restart(source, checkpoints, target, monkeypatch):
    real = db_migrate.copy_checkpoints

    def interrupted(*_args):
        raise KeyboardInterrupt

    monkeypatch.setattr(db_migrate, "copy_checkpoints", interrupted)
    with pytest.raises(KeyboardInterrupt):
        db_migrate.migrate_from_sqlite(source, target, checkpoints)
    engine = pg_mode.real_create_db_engine(target)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM materials")).scalar() == 0
    engine.dispose()
    monkeypatch.setattr(db_migrate, "copy_checkpoints", real)
    assert db_migrate.migrate_from_sqlite(source, target, checkpoints)["tables"]["materials"]["rows"] == 2


def test_mismatch_rolls_back(source, checkpoints, target, monkeypatch):
    calls = iter(["错的校验"])
    real = db_migrate._table_digest
    monkeypatch.setattr(db_migrate, "_table_digest", lambda digests: next(calls, None) or real(digests))
    with pytest.raises(db_migrate.MigrationRefused, match="校验不一致"):
        db_migrate.migrate_from_sqlite(source, target, checkpoints)
    engine = pg_mode.real_create_db_engine(target)
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM materials")).scalar() == 0
    engine.dispose()


def test_preflight_reports_problems_before_touching_target(source, target):
    connection = sqlite3.connect(source)
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("INSERT INTO todo_materials(todo_id, material_id, relation, created_at) VALUES (999, 999, 'direct', '2026-01-01 00:00:00')")
    connection.execute("UPDATE todos SET status = 'too-long-status-value' WHERE id = 3")
    connection.commit()
    connection.close()
    with pytest.raises(db_migrate.MigrationRefused) as caught:
        db_migrate.migrate_from_sqlite(source, target)
    assert "不存在的记录" in str(caught.value) and "todos.status" in str(caught.value)


def test_knowledge_material_with_nul_is_refused(source, target):
    connection = sqlite3.connect(source)
    connection.execute("UPDATE materials SET content = '住宿' || char(0) || '上限' WHERE title = '制度'")
    connection.commit()
    connection.close()
    with pytest.raises(db_migrate.MigrationRefused, match="NUL"):
        db_migrate.migrate_from_sqlite(source, target)


def test_every_model_table_is_in_the_copy_plan(source):
    register_models()
    plan = db_migrate.preflight(db_migrate._read_only(source))
    assert set(plan) == set(Base.metadata.tables)
