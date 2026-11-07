"""只在 PostgreSQL 上有意义的用例：真并发、多进程外发幂等、NUL、会话记录、索引后端差异。

其余用例由 pg_mode.py 让现有测试整批跑在 PostgreSQL 上。"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier
import time

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from langchain_core.messages import AIMessage, HumanMessage
import pytest
pytest.importorskip("pgvector", reason="PostgreSQL 用例需安装 .[postgres]")
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from briefly import schema
from briefly.agent import postgres_checkpointer
from briefly.db import create_db_engine
from briefly.knowledge import removal
from briefly.knowledge.index import open_index
from briefly.knowledge.ingestion import ImportService
from briefly.knowledge.lifecycle import IndexLifecycle
from briefly.knowledge.models import IndexGeneration, KnowledgeBase, KnowledgePublication
from briefly.knowledge.pg_index import PostgresIndex
from briefly.knowledge.service import KnowledgeConflict, KnowledgeService
from briefly.links import DocumentLink
from briefly.materials import MaterialStore
from briefly.todos.models import Todo
import pg_mode
from test_knowledge_slicing import Encoder

pytestmark = pytest.mark.postgres

_WORKER = """
import sys, time
from briefly import outbound
from briefly.db import create_db_engine
engine = create_db_engine(sys.argv[1])
time.sleep(max(0, float(sys.argv[2]) - time.time()))  # 几个进程同时开始
claim = outbound.begin(engine, "mail", 1, "同一份周报", None, False)
if claim.owned:
    outbound.attach(engine, claim.key, 7)
    time.sleep(0.5)  # 外部发送耗时：其他进程这时应该在等
    outbound.finish(engine, claim.key)
print(int(claim.owned), claim.record_id)
engine.dispose()
"""


@pytest.fixture
def engine():
    return create_db_engine("sqlite://")


def url_of(engine) -> str:
    return engine.url.render_as_string(hide_password=False)


def test_outbound_claim_is_atomic_across_processes(engine):
    """重复发送靠数据库的原子占位拦住。用真的多进程验证，排除进程内锁的帮助。"""
    source = str(Path(__file__).resolve().parents[1] / "src")
    start = str(time.time() + 8)
    env = {**os.environ, "PYTHONPATH": source, "PYTHONUTF8": "1"}
    workers = [subprocess.Popen([sys.executable, "-c", _WORKER, url_of(engine), start], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, encoding="utf-8", env=env) for _ in range(4)]
    results = []
    for worker in workers:
        out, err = worker.communicate(timeout=120)
        assert worker.returncode == 0, err
        results.append(out.split())
    assert sorted(results) == [["0", "7"], ["0", "7"], ["0", "7"], ["1", "None"]]


def test_import_lease_has_one_winner_under_concurrency(engine, tmp_path):
    knowledge = KnowledgeService(engine)
    kb = knowledge.create("库")
    imports = ImportService(engine, tmp_path, PostgresIndex(engine), Encoder)
    job, _ = imports.enqueue(kb.id, "a.txt", b"content", request_key="r1")
    barrier = Barrier(6)

    def worker(_):
        barrier.wait(timeout=10)
        try:
            with imports._lease(kb.id, job.id, "queued", "parsing"):
                time.sleep(0.2)
            return "won"
        except KnowledgeConflict:
            return "lost"

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(worker, range(6)))
    assert results.count("won") == 1 and results.count("lost") == 5


def test_concurrent_publish_has_one_winner_and_loser_leaves_nothing(engine):
    store, knowledge = MaterialStore(engine), KnowledgeService(engine)
    material, _ = store.add_text("制度", "住宿上限650元。")
    kb = knowledge.create("库")
    document, version, _ = knowledge.attach(kb.id, material.id)
    lifecycle = IndexLifecycle(engine, PostgresIndex(engine))
    barrier = Barrier(2)

    class Together(Encoder):
        def embed_documents(self, texts):
            barrier.wait(timeout=10)  # 两边都完成了发布前的检查才继续：依赖单写者的逻辑会在这里露馅
            return super().embed_documents(texts)

    def worker(_):
        try:
            return lifecycle.build(kb.id, document.id, version.id, Together(), expected_revision=1)
        except KnowledgeConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, range(2)))
    winners = [result for result in results if result]
    assert len(winners) == 1
    with Session(engine) as s:
        assert s.get(KnowledgePublication, document.id).generation_id == winners[0]
        assert {g.status for g in s.scalars(select(IndexGeneration))} == {"ready", "failed"}
    with engine.connect() as connection:
        assert connection.execute(text("SELECT DISTINCT generation FROM kb_index_chunks")).scalars().all() == winners


def _base_with_document(engine, tmp_path):
    store, knowledge = MaterialStore(engine), KnowledgeService(engine)
    material, _ = store.add_text("制度", "住宿上限650元。")
    kb = knowledge.create("库")
    document, version, _ = knowledge.attach(kb.id, material.id)
    imports = ImportService(engine, tmp_path, PostgresIndex(engine), Encoder)
    imports.lifecycle.build(kb.id, document.id, version.id, Encoder(), expected_revision=1)
    return imports, kb, document


def _link_appears_during_delete(engine, kb, document, times):
    """后台的疑似重复检测在删除的两条语句之间提交一条关联。SQLite 的写锁不允许这样，PostgreSQL 允许。"""
    left = [times]

    def concurrent_commit(_conn, _cursor, statement, *_args):
        if statement.startswith("DELETE FROM knowledge_versions") and left[0]:
            left[0] -= 1
            # 每次一份新材料：同一对材料和文档的关联正被这个事务删除，再写同样的一对会等它提交，用例自己卡死
            material, _ = MaterialStore(engine).add_text(f"改稿{left[0]}", f"住宿上限{left[0]}元。")
            with Session(engine) as s, s.begin():
                s.add(DocumentLink(material_id=material.id, kb_id=kb.id, document_id=document.id, kind="suggested"))

    event.listen(engine, "before_cursor_execute", concurrent_commit)
    return lambda: event.remove(engine, "before_cursor_execute", concurrent_commit)


def test_delete_base_survives_a_link_committed_mid_delete(engine, tmp_path):
    imports, kb, document = _base_with_document(engine, tmp_path)
    stop = _link_appears_during_delete(engine, kb, document, times=1)
    try:
        assert removal.delete_base(imports, kb.id)["kb_id"] == kb.id
    finally:
        stop()
    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(DocumentLink)) == 0
        assert s.get(KnowledgeBase, kb.id) is None


def test_delete_base_reports_conflict_if_writes_keep_coming(engine, tmp_path):
    imports, kb, document = _base_with_document(engine, tmp_path)
    stop = _link_appears_during_delete(engine, kb, document, times=100)
    try:
        with pytest.raises(KnowledgeConflict, match="正有新的写入"):
            removal.delete_base(imports, kb.id)
    finally:
        stop()
    with Session(engine) as s:  # 没有删掉任何东西
        assert s.get(KnowledgeBase, kb.id) is not None


def test_todo_ids_never_reused_and_two_levels_enforced_by_database(engine):
    from briefly.todos.service import TodoService

    todos = TodoService(engine, MaterialStore(engine))
    first = todos.create("甲")
    todos.delete_permanently(first.id)
    second = todos.create("乙")
    assert second.id > first.id
    child = todos.create("丙", parent_id=second.id)
    with Session(engine) as s, pytest.raises(IntegrityError):
        s.add(Todo(title="丁", parent_id=child.id))
        s.commit()


def test_nul_in_material_keeps_fingerprint_and_positions(engine):
    store = MaterialStore(engine)
    body = "前文\x00后续不得丢失😀。第二句"
    material, created = store.add_text("标题\x00带NUL", body)
    assert created
    saved = store.get(material.id)
    expected = body.replace("\x00", "�")
    assert saved.content == expected and len(saved.content) == len(body)  # 等长：章节和切片位置不变
    assert saved.content_hash == hashlib.sha256(expected.encode()).hexdigest()  # 指纹对得上存下来的正文
    assert saved.title == "标题�带NUL"
    again, created = store.add_text("再次", body)  # 同样的内容不重复入库
    assert not created and again.id == material.id
    assert store.section_text(material.id, 2, 6, 100)[0] == expected[2:6]
    assert store.search("\x00") is not None  # 查询词带 NUL 不报错


def test_nul_in_knowledge_version_passes_verification(engine):
    store, knowledge = MaterialStore(engine), KnowledgeService(engine)
    material, _ = store.add_text("制度", "住宿\x00上限650元。")
    kb = knowledge.create("库")
    document, version, _ = knowledge.attach(kb.id, material.id)
    lifecycle = IndexLifecycle(engine, PostgresIndex(engine))
    lifecycle.build(kb.id, document.id, version.id, Encoder(), expected_revision=1)
    assert lifecycle.validate_scope([kb.id], Encoder())["status"] == "ready"


def test_index_signature_is_backend_specific_and_verify_catches_tampering(engine):
    store, knowledge = MaterialStore(engine), KnowledgeService(engine)
    material, _m = store.add_text("制度", "住宿上限650元。")
    kb = knowledge.create("库")
    document, version, _v = knowledge.attach(kb.id, material.id)
    index = open_index(engine, "ignored.sqlite")
    assert isinstance(index, PostgresIndex)
    lifecycle = IndexLifecycle(engine, index)
    generation_id = lifecycle.build(kb.id, document.id, version.id, Encoder(), expected_revision=1)
    with Session(engine) as s:
        generation = s.get(IndexGeneration, generation_id)
        rows = IndexLifecycle.rows(s, version)
    index.verify(generation, rows)
    assert PostgresIndex.signature(generation) not in _sqlite_signature(generation)
    with engine.begin() as connection:
        connection.execute(text("UPDATE kb_index_chunks SET content = content || 'x'"))
    with pytest.raises(KnowledgeConflict):
        index.verify(generation, rows)


def _sqlite_signature(generation):
    from briefly.knowledge.service import fingerprint

    return {fingerprint(dict(identity=generation.embedding_identity, storage_schema=1,
                             lexical="cjk-bigrams-ascii-v1", metric="cosine-normalized-f32"))}


def test_failed_build_leaves_no_partial_generation(engine, monkeypatch):
    index = PostgresIndex(engine)

    class Generation:
        id, dimension, embedding_identity = "g1", 2, dict(model="m")

    with pytest.raises(ValueError):  # 第二条向量无效：写入之前就失败
        index.build(Generation, [(1, "甲"), (2, "乙")], [[1.0, 0.0], [float("nan"), 0.0]])
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM kb_index_chunks")).scalar() == 0
    with pytest.raises(Exception):  # 同一事务里编号重复：数据库拒绝，前面写的也撤销
        index.build(Generation, [(1, "甲"), (1, "乙")], [[1.0, 0.0], [0.0, 1.0]])
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM kb_index_chunks")).scalar() == 0
        assert connection.execute(text("SELECT count(*) FROM kb_index_generations")).scalar() == 0


def test_vectors_of_different_dimensions_coexist_and_long_words_are_ignored(engine):
    index = PostgresIndex(engine)

    class Small:
        id, dimension, embedding_identity = "small", 2, dict(model="a")

    class Large:
        id, dimension, embedding_identity = "large", 3, dict(model="b")

    huge = "a" * 3000  # 编码后超过 2046 字节：PostgreSQL 不收进 tsvector，也不报错（已知差异）
    index.build(Small, [(1, f"合同审批 {huge}")], [[1.0, 0.0]])
    index.build(Large, [(1, "合同审批")], [[1.0, 0.0, 0.0]])
    dense, lexical = index.search("合同", [1.0, 0.0], 2, ["small"])
    assert dense[0][:2] == ("small", 1) and dense[0][2] == pytest.approx(0)
    assert lexical[0][:2] == ("small", 1)
    assert index.search(huge, [1.0, 0.0], 2, ["small"])[1] == []
    assert index.nearest([1.0, 0.0], 2, ["small"]) == pytest.approx(1)


def _config(thread):
    return dict(configurable=dict(thread_id=thread, checkpoint_ns=""))


def test_checkpointer_supports_what_the_agent_uses(engine):
    from briefly.agent import Agent
    from fakes import FunctionModel

    saver = postgres_checkpointer(url_of(engine))
    try:
        saver.setup()  # 重复建表不报错
        store = MaterialStore(engine)
        model = FunctionModel(respond=lambda _: AIMessage("回复"), calls=[])
        agent = Agent(model, checkpointer=saver, thread_id="t1", store=store)
        assert agent.chat("你好\x00世界")  # 用户消息带 NUL 也能保存
        state = saver.get_tuple(_config("t1"))
        assert state is not None
        messages = state.checkpoint["channel_values"]["messages"]
        assert isinstance(messages[0], HumanMessage) and "世界" in messages[0].content and isinstance(messages[-1], AIMessage)
        assert messages[0].id  # 消息元数据还在（重发、纠正按它定位）
        restored = Agent(FunctionModel(respond=lambda _: AIMessage("回复"), calls=[]), checkpointer=saver, thread_id="t1", store=store)
        restored.chat("继续")
        assert len(saver.get_tuple(_config("t1")).checkpoint["channel_values"]["messages"]) > len(messages)
        saver.delete_thread("t1")
        assert saver.get_tuple(_config("t1")) is None
    finally:
        saver.conn.close()


def test_checkpointer_follows_the_database_url(engine, tmp_path):
    from langgraph.checkpoint.postgres import PostgresSaver

    from briefly import agent as agent_module

    saver = agent_module.open_checkpointer(url_of(engine))
    assert isinstance(saver, PostgresSaver)
    saver.conn.close()


def test_models_and_migrations_have_no_difference(engine):
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts=dict(compare_type=True, include_object=schema.include_object))
        assert compare_metadata(context, schema.target_metadata("postgresql")) == []
        assert schema.current(connection) == schema.head()


def test_concurrent_first_start_on_empty_database_all_succeed(tmp_path):
    """多个进程第一次启动：业务表由咨询锁排队，会话记录表也要排队，否则 setup() 撞唯一约束，后到的启动失败。"""
    from test_schema_migrations import start_together

    url = pg_mode._connection_url(pg_mode._new_schema())
    start_together(url, tmp_path)
    again = create_db_engine(url)
    with again.connect() as connection:
        assert schema.current(connection) == schema.head()
        assert connection.execute(text("SELECT count(*) FROM checkpoint_migrations")).scalar() == 10
        assert connection.execute(text("SELECT count(*) FROM pg_index WHERE NOT indisvalid")).scalar() == 0
    again.dispose()


def test_second_start_does_not_migrate_again(engine):
    again = create_db_engine(url_of(engine))
    with again.connect() as connection:
        assert schema.current(connection) == schema.head()
    again.dispose()
