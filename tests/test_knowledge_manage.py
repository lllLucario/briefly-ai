"""知识库重命名和删除：接口、事务、并发、工作区关联、旧引用、助理与路由。假编码器、假模型、原创虚构资料。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import logging
from threading import Barrier

import pytest

pytest.importorskip("sqlite_vec", reason="知识库管理回归需安装.[rag]")
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from briefly import config
from briefly.agent import Agent, sqlite_checkpointer
from briefly.api import create_app
from briefly.db import create_db_engine, foreign_key_problems
from briefly.knowledge import models, removal
from briefly.knowledge.ingestion import utcnow
from briefly.knowledge.service import KB_REMOVED, KnowledgeNotFound
from briefly.links import DocumentLink, LinkService
from briefly.llm_cache import LlmCacheEntry
from briefly.loaders import LoadedDocument
from briefly.materials import KNOWLEDGE, Material, MaterialStore
from fakes import FunctionModel
from test_knowledge_slicing import Encoder

CONTENT = "# 差旅制度\n正式员工在上海住宿上限为每晚650元，按实际发票报销。"
TABLES = (models.KnowledgeDocument, models.KnowledgeVersion, models.KnowledgeChunk, models.IndexGeneration,
          models.KnowledgePublication, models.KnowledgeImport, models.KnowledgeImportMetrics,
          models.KnowledgeParseCache, models.KnowledgeEgressGrant, DocumentLink)


def respond(messages):
    """最后一条是用户消息时先检索一次，拿到结果后作答。"""
    if isinstance(messages[-1], ToolMessage):
        return AIMessage("按依据作答〔依据1〕" if "650" in messages[-1].text else "资料里没有查到")
    return AIMessage("", tool_calls=[dict(name="search_knowledge", args=dict(questions=["上海住宿上限"]), id="c1", type="tool_call")])


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
    engine = create_db_engine("sqlite:///" + (tmp_path / "api.db").as_posix())
    saver = sqlite_checkpointer(tmp_path / "chat.db")
    model = FunctionModel(respond=respond, calls=[])
    app = create_app(MaterialStore(engine), model_factory=lambda: model,
                     agent_factory=lambda s: Agent(model, store=s, checkpointer=saver))
    imports = app.state.knowledge_imports
    imports.encoder_factory = Encoder
    imports.parser = lambda filename, data: LoadedDocument(data.decode(), True)
    imports.parser_identity = dict(version="test")  # 打开解析缓存，验证保存的解析结果也被删除
    try:
        with TestClient(app, base_url="http://127.0.0.1") as client:
            client.app_state = app.state
            client.engine = engine
            client.model = model
            yield client
    finally:
        saver.conn.close()
        engine.dispose()


def new_base(client, name="差旅库", *, content=CONTENT, filename="制度.md", allow=False):
    kb = client.post("/knowledge-bases", json={"name": name}).json()["id"]
    response = client.post(f"/knowledge-bases/{kb}/imports", data={"request_key": f"{kb}-1", "tokens": "64"},
                           files=[("files", (filename, content.encode()))])
    assert response.status_code == 202
    assert [job["status"] for job in client.get(f"/knowledge-bases/{kb}/imports").json()] == ["ready"]
    if allow:
        client.put(f"/knowledge-bases/{kb}/egress", json={"allowed": True})
    return kb


def remove(client, kb):
    return client.delete(f"/knowledge-bases/{kb}", params={"confirm": True})


def rows(client, kb=None):
    """业务库里属于知识库的行数；kb 给定时只算这个库。"""
    counts = {}
    with Session(client.engine) as s:
        for table in TABLES:
            query = select(func.count()).select_from(table)
            if kb and hasattr(table, "kb_id"):
                query = query.where(table.kb_id == kb)
            counts[table.__tablename__] = s.scalar(query)
    return counts


def generations(client):
    index = client.app_state.knowledge_imports.lifecycle.index
    if client.engine.dialect.name == "postgresql":
        with client.engine.connect() as connection:
            return (connection.exec_driver_sql("SELECT id FROM kb_index_generations").scalars().all(),
                    connection.exec_driver_sql("SELECT count(*) FROM kb_index_chunks").scalar())
    with index.connect() as db:
        return [row[0] for row in db.execute("SELECT id FROM generations")], db.execute("SELECT count(*) FROM lexical").fetchone()[0]


# ---- 重命名
def test_rename_keeps_egress_index_and_references(world):
    kb = new_base(world, allow=True)
    [doc] = world.get(f"/knowledge-bases/{kb}/documents").json()
    [version] = world.get(f"/knowledge-bases/{kb}/documents/{doc['id']}/versions").json()["versions"]
    before = world.post(f"/knowledge-bases/{kb}/search", json={"question": "上海住宿"}).json()["evidence"][0]["reference"]

    response = world.patch(f"/knowledge-bases/{kb}", json={"name": "  新名字  "})
    assert response.status_code == 200 and response.json()["name"] == "新名字" and response.json()["external_allowed"] is True
    assert [b["name"] for b in world.get("/knowledge-bases").json()] == ["新名字"]
    assert world.post("/knowledge-scope", json={"kb_ids": [kb]}).json()["status"] == "ready"
    assert world.get(f"/knowledge-bases/{kb}/documents").json()[0]["active_version_id"] == version["id"]
    assert world.post(f"/knowledge-bases/{kb}/search", json={"question": "上海住宿"}).json()["evidence"][0]["reference"] == before
    assert world.get(f"/knowledge-bases/{kb}/references/{before}").json()["status"] == "current"


def test_rename_follows_create_rules(world):
    first = world.post("/knowledge-bases", json={"name": "甲"}).json()["id"]
    second = world.post("/knowledge-bases", json={"name": "乙"}).json()["id"]
    assert world.patch(f"/knowledge-bases/{second}", json={"name": "甲"}).status_code == 200  # 创建不限制重名，重命名也不限制
    assert world.patch(f"/knowledge-bases/{first}", json={"name": "   "}).status_code == 400
    assert world.patch(f"/knowledge-bases/{first}", json={"name": ""}).status_code == 422
    assert world.patch(f"/knowledge-bases/{first}", json={"name": "长" * 201}).status_code == 422
    assert world.patch("/knowledge-bases/none", json={"name": "丙"}).status_code == 404


# ---- 删除
def test_delete_removes_everything_of_the_base(world):
    kb = new_base(world, allow=True)
    failed = world.post(f"/knowledge-bases/{kb}/imports", data={"request_key": "bad", "tokens": "64"},
                        files=[("files", ("坏.md", b"   "))]).json()["files"][0]["id"]
    payload = world.app_state.knowledge_imports._payload(failed)
    assert payload.is_file()  # 失败任务的上传暂存还在
    assert generations(world)[0] and rows(world)["knowledge_parse_cache"]
    with Session(world.engine) as s:
        material_id = s.scalar(select(models.KnowledgeVersion.material_id))
    assert world.get(f"/knowledge-bases/{kb}/deletion-preview").json()["documents"] == 1

    response = remove(world, kb)
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "差旅库" and body["documents"] == 1 and body["removed_materials"] == 1 and body["cleanup_pending"] is False
    assert world.get("/knowledge-bases").json() == []
    assert not any(rows(world).values())  # 文档、切片、代次、发布、任务、指标、解析结果、授权、关联都没有了
    assert foreign_key_problems(world.engine) == {}
    assert generations(world) == ([], 0)  # 向量和 FTS 索引
    assert not payload.exists()
    with Session(world.engine) as s:
        assert s.get(Material, material_id) is None  # 只属于这个库的正文
        assert s.get(models.KnowledgeBase, kb) is None
        assert s.get(models.KnowledgeBaseRemoval, kb).name == "差旅库"
    for path in ("documents", "imports"):
        assert world.get(f"/knowledge-bases/{kb}/{path}").status_code == 404
    assert world.put(f"/knowledge-bases/{kb}/egress", json={"allowed": True}).status_code == 404
    assert world.post(f"/knowledge-bases/{kb}/imports", data={"request_key": "late", "tokens": "64"},
                      files=[("files", ("x.md", b"text"))]).status_code == 404


def test_delete_needs_confirmation_and_missing_base_is_404(world):
    kb = new_base(world)
    refused = world.delete(f"/knowledge-bases/{kb}")
    assert refused.status_code == 409 and refused.json().get("confirm") is True
    assert [b["id"] for b in world.get("/knowledge-bases").json()] == [kb]
    assert remove(world, "none").status_code == 404
    assert world.get("/knowledge-bases/none/deletion-preview").status_code == 404
    assert remove(world, kb).status_code == 200
    assert remove(world, kb).status_code == 404  # 重复删除


@pytest.mark.parametrize("status", ["queued", "parsing", "preview", "indexing"])
def test_delete_refused_while_import_running(world, status):
    kb = new_base(world)
    before = rows(world)
    job, _ = world.app_state.knowledge_imports.enqueue(kb, "新.md", b"# new\ntext", request_key="running", tokens=64)
    with Session(world.engine) as s, s.begin():
        row = s.get(models.KnowledgeImport, job.id)
        row.status, row.lease_token, row.lease_until = status, "live", utcnow() + timedelta(seconds=60)
    assert world.get(f"/knowledge-bases/{kb}/deletion-preview").json()["active_imports"] == 1
    response = remove(world, kb)
    assert response.status_code == 409 and "正在入库" in response.json()["detail"] and "confirm" not in response.json()
    assert world.get("/knowledge-bases").json()[0]["id"] == kb
    assert rows(world)["knowledge_documents"] == before["knowledge_documents"]
    assert world.app_state.knowledge_imports._payload(job.id).is_file()
    with Session(world.engine) as s:  # 不抢任务的租约
        row = s.get(models.KnowledgeImport, job.id)
        assert row.status == status and row.lease_token == "live"


def test_import_with_expired_lease_does_not_block_delete(world):
    kb = new_base(world)
    job, _ = world.app_state.knowledge_imports.enqueue(kb, "新.md", b"# new\ntext", request_key="dead", tokens=64)
    with Session(world.engine) as s, s.begin():
        row = s.get(models.KnowledgeImport, job.id)
        row.status, row.lease_token, row.lease_until = "parsing", "dead", utcnow() - timedelta(seconds=1)
    assert remove(world, kb).status_code == 200
    assert not world.app_state.knowledge_imports._payload(job.id).exists()


def test_stale_worker_cannot_write_after_delete(world):
    kb = new_base(world)
    imports = world.app_state.knowledge_imports
    job, _ = imports.enqueue(kb, "新.md", b"# new\ntext", request_key="stale", tokens=64)
    with Session(world.engine) as s, s.begin():
        s.get(models.KnowledgeImport, job.id).status = "failed"
    assert remove(world, kb).status_code == 200
    with pytest.raises(KnowledgeNotFound):
        imports.process(kb, job.id)
    with pytest.raises(KnowledgeNotFound):
        imports.retry(kb, job.id)
    with pytest.raises(IntegrityError), Session(world.engine) as s, s.begin():  # 晚到的写入被外键挡住
        s.add(models.KnowledgeImport(kb_id=kb, request_key="k", request_fingerprint="f", filename="f", source_key="s",
                                     expected_revision=0, tokens=64))


def test_concurrent_delete_succeeds_once(world):
    kb = new_base(world)
    imports = world.app_state.knowledge_imports
    barrier = Barrier(4)

    def attempt(_):
        barrier.wait()
        try:
            removal.delete_base(imports, kb)
            return "deleted"
        except KnowledgeNotFound:
            return "missing"

    with ThreadPoolExecutor(4) as pool:
        results = sorted(pool.map(attempt, range(4)))
    assert results == ["deleted", "missing", "missing", "missing"]
    assert not any(rows(world).values())
    assert foreign_key_problems(world.engine) == {}


def test_failure_inside_transaction_leaves_base_intact(world, monkeypatch):
    kb = new_base(world, allow=True)
    before = rows(world)
    indexed = generations(world)

    def broken(session):
        raise RuntimeError("中途失败")

    monkeypatch.setattr(removal, "purge_knowledge_answers", broken)
    with pytest.raises(RuntimeError):
        removal.delete_base(world.app_state.knowledge_imports, kb)
    assert rows(world) == before and generations(world) == indexed
    with Session(world.engine) as s:
        assert s.get(models.KnowledgeBaseRemoval, kb) is None
    assert world.get("/knowledge-bases").json()[0]["external_allowed"] is True
    assert world.post("/knowledge-scope", json={"kb_ids": [kb]}).json()["status"] == "ready"


def test_cleanup_failure_is_logged_and_retried(world, monkeypatch, caplog):
    kb = new_base(world)
    imports = world.app_state.knowledge_imports
    [generation], _ = generations(world)
    real = imports.lifecycle.index.delete

    def broken(ids):
        raise OSError("索引文件被占用")

    monkeypatch.setattr(imports.lifecycle.index, "delete", broken)
    with caplog.at_level(logging.WARNING, logger="briefly.knowledge.removal"):
        response = remove(world, kb)
    assert response.status_code == 200 and response.json()["cleanup_pending"] is True  # 不影响删除结果
    assert any("索引清理失败" in r.getMessage() for r in caplog.records)
    assert world.get("/knowledge-bases").json() == [] and not any(rows(world).values())
    with Session(world.engine) as s:
        assert s.get(models.KnowledgeBaseRemoval, kb).generation_ids == [generation]

    monkeypatch.setattr(imports.lifecycle.index, "delete", real)
    removal.retry_pending_removals(imports)  # 启动时重试
    assert generations(world) == ([], 0)
    with Session(world.engine) as s:
        assert s.get(models.KnowledgeBaseRemoval, kb).generation_ids == []


def test_cached_answers_are_purged(world):
    kb = new_base(world)
    with Session(world.engine) as s, s.begin():
        now = utcnow()
        s.add(LlmCacheEntry(key="a" * 64, site="knowledge_answer", principal_id="local", value={}, expires_at=now + timedelta(hours=1)))
        s.add(LlmCacheEntry(key="b" * 64, site="synthesize", principal_id="local", value={}, expires_at=now + timedelta(hours=1)))
    assert remove(world, kb).status_code == 200
    with Session(world.engine) as s:
        assert list(s.scalars(select(LlmCacheEntry.site))) == ["synthesize"]


# ---- 工作区
def test_workspace_material_survives_and_link_is_cut(world):
    kb = new_base(world)  # 未允许外发
    other = new_base(world, "其他库", content="# 其他\n这是另一个库的虚构内容，和工作区材料无关。", filename="其他.md")
    store = world.app_state.store
    doc = world.get(f"/knowledge-bases/{kb}/documents").json()[0]
    copied = world.post(f"/knowledge-bases/{kb}/documents/{doc['id']}/copy-to-workspace").json()
    material = copied["material_id"]
    assert world.get(f"/materials/{material}").json()["links"]
    assert store.egress_blocked([material])  # 继承未授权库的外发限制
    assert world.get(f"/knowledge-bases/{kb}/deletion-preview").json()["workspace_links"] == 1

    body = remove(world, kb).json()
    assert body["workspace_links"] == 1 and body["removed_materials"] == 0
    detail = world.get(f"/materials/{material}").json()
    assert detail["links"] == [] and "650" in detail["content"]
    assert material in [m["id"] for m in world.get("/materials").json()]
    assert store.egress_blocked([material])  # 未授权库：关联断开，副本继续禁止外发（第 97 条）
    with Session(world.engine) as s:
        assert s.scalar(select(func.count()).select_from(DocumentLink)) == 0
    assert world.post("/knowledge-scope", json={"kb_ids": [other]}).json()["status"] == "ready"  # 其他库不受影响


def test_material_shared_with_another_base_is_kept(world):
    first = new_base(world, "甲库")
    second = new_base(world, "乙库")  # 同样内容：两个库的版本引用同一条材料
    with Session(world.engine) as s:
        materials = set(s.scalars(select(models.KnowledgeVersion.material_id)))
    assert len(materials) == 1
    body = remove(world, first).json()
    assert body["removed_materials"] == 0
    assert world.post(f"/knowledge-bases/{second}/search", json={"question": "上海住宿"}).json()["evidence"]
    assert world.post("/knowledge-scope", json={"kb_ids": [second]}).json()["status"] == "ready"
    assert remove(world, second).json()["removed_materials"] == 1  # 最后一个库删除时才删正文


def test_retained_material_keeps_quality_limits_of_deleted_versions(world):
    from briefly.knowledge.evidence import material_quality_notice

    kb = new_base(world)
    store = world.app_state.store
    doc = world.get(f"/knowledge-bases/{kb}/documents").json()[0]
    material = world.post(f"/knowledge-bases/{kb}/documents/{doc['id']}/copy-to-workspace").json()["material_id"]
    with Session(world.engine) as s, s.begin():
        version = s.scalar(select(models.KnowledgeVersion))
        version.source_snapshot = {**version.source_snapshot, "parse_quality": dict(table_structure_status="partial")}
    assert "单元格归属" in material_quality_notice(store, [material])
    assert remove(world, kb).status_code == 200
    assert "单元格归属" in material_quality_notice(store, [material])  # 版本没了，限制不放松


# ---- 旧引用
def test_old_reference_says_source_deleted(world):
    kb = new_base(world, allow=True)
    reference = world.post(f"/knowledge-bases/{kb}/search", json={"question": "上海住宿"}).json()["evidence"][0]["reference"]
    assert remove(world, kb).status_code == 200
    response = world.get(f"/knowledge-bases/{kb}/references/{reference}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == KB_REMOVED and body["content"] is None and body["reference"] == reference
    assert world.get(f"/knowledge-bases/{kb}/references/bad").status_code == 400
    assert world.get(f"/knowledge-bases/none/references/{reference}").status_code == 404  # 从未存在的库不说“已删除”


# ---- 助理与路由
def tool_results(client):
    return [m.text for call in client.model.calls for m in call if isinstance(m, ToolMessage)]


def test_assistant_stops_searching_deleted_base_and_keeps_old_answer(world):
    kb = new_base(world, allow=True)
    first = world.post("/chat", json={"text": "上海住宿上限是多少", "kb_id": kb})
    assert first.status_code == 200 and "依据1" in first.json()["reply"]
    assert any("650" in text for text in tool_results(world))
    thread = first.json()["thread_id"]
    old = world.get(f"/chat/sessions/{thread}").json()
    assert old["kb_id"] == kb and old["kb_name"] == "差旅库"
    reference = old["messages"][1]["knowledge"]["references"][0]["reference"]

    assert remove(world, kb).status_code == 200
    world.model.calls.clear()
    # 页面还没刷新，继续带着已删除的库提问：不报错，也检索不到
    second = world.post("/chat", json={"text": "上海住宿上限是多少", "thread_id": thread, "kb_id": kb})
    assert second.status_code == 200 and second.json()["intent"]["kb_removed"] is True
    assert "kb_id" not in second.json()["intent"]
    assert not any("650元" in text for text in tool_results(world))
    assert any("没有选择知识库" in text for text in tool_results(world))

    after = world.get(f"/chat/sessions/{thread}").json()
    assert after["kb_id"] is None
    assert after["messages"][1]["text"] == old["messages"][1]["text"]  # 历史回答不改写
    assert after["messages"][1]["knowledge"]["references"][0]["reference"] == reference
    assert after["messages"][2]["intent"]["kb_removed"] is True
    assert world.get(f"/knowledge-bases/{kb}/references/{reference}").json()["status"] == KB_REMOVED


def test_reopened_session_reports_deleted_base_name(world):
    kb = new_base(world, allow=True)
    thread = world.post("/chat", json={"text": "上海住宿上限是多少", "kb_id": kb}).json()["thread_id"]
    assert remove(world, kb).status_code == 200
    detail = world.get(f"/chat/sessions/{thread}").json()  # 页面据此提示并改为“不查知识库”
    assert detail["kb_id"] == kb and detail["kb_name"] == "差旅库"
    assert kb not in [b["id"] for b in world.get("/knowledge-bases").json()]


def test_base_deleted_during_turn_tells_the_model(world):
    kb = new_base(world, allow=True)
    assert world.post("/chat", json={"text": "你好啊"}).status_code == 200
    agent = world.app_state.agent
    assert remove(world, kb).status_code == 200
    agent.knowledge.base_name = lambda _: "差旅库"  # 本轮开始时库还在，检索时已删除
    world.model.calls.clear()
    response = world.post("/chat", json={"text": "上海住宿上限是多少", "kb_id": kb})
    assert response.status_code == 200
    assert any("已被删除" in text for text in tool_results(world))


def test_relevance_routing_ignores_deleted_base(world):
    kept = new_base(world, "保留库", content="# 报销\n出租车票须在当月提交。", filename="报销.md")
    gone = new_base(world, "删除库")
    world.post("/chat", json={"text": "你好啊"})
    agent = world.app_state.agent
    assert {s["kb_id"] for s in agent.knowledge.library_scores("上海住宿")} == {kept, gone}
    assert remove(world, gone).status_code == 200
    assert {s["kb_id"] for s in agent.knowledge.library_scores("上海住宿")} == {kept}  # 跨库提示不再提到它
    with pytest.raises(KnowledgeNotFound):
        agent.knowledge.relevance_signals(gone, "上海住宿")
    # 意图漏斗的知识库路由遇到已删除的库不出错：探测失败只是不走捷径
    from briefly.intent import IntentFunnel, TurnState

    funnel = IntentFunnel(kb_probe=agent.knowledge.relevance_signals, kb_rivals=agent.knowledge.library_scores)
    funnel.kb_threshold = 0.5
    assert funnel.classify("上海住宿上限是多少？", TurnState(kb_id=gone)).intent != "knowledge"


def test_search_and_answer_on_deleted_base_are_404(world):
    kb = new_base(world, allow=True)
    assert remove(world, kb).status_code == 200
    assert world.post(f"/knowledge-bases/{kb}/search", json={"question": "上海"}).status_code == 404
    assert world.post(f"/knowledge-bases/{kb}/answer", json={"question": "上海住宿上限是多少"}).status_code == 404
    assert world.post("/knowledge-scope", json={"kb_ids": [kb]}).status_code == 404


def test_workspace_link_detection_skips_deleted_base(world):
    kb = new_base(world)
    store = world.app_state.store
    material, _ = store.add_text("差旅制度改稿", CONTENT)
    assert remove(world, kb).status_code == 200
    assert LinkService(world.engine).detect() == 0
    assert world.get(f"/materials/{material.id}").json()["links"] == []


def test_knowledge_only_material_is_not_shown_in_workspace_before_or_after_delete(world):
    kb = new_base(world)
    with Session(world.engine) as s:
        assert s.scalar(select(Material.source)) == KNOWLEDGE
    assert world.get("/materials").json() == []
    assert remove(world, kb).status_code == 200
    assert world.get("/materials").json() == []


# ---- 认证
def test_endpoints_require_login(world, monkeypatch):
    kb = world.post("/knowledge-bases", json={"name": "甲"}).json()["id"]
    monkeypatch.setattr(config, "ACCESS_TOKEN", "管理测试令牌".encode().hex())
    assert world.patch(f"/knowledge-bases/{kb}", json={"name": "乙"}).status_code == 401
    assert world.get(f"/knowledge-bases/{kb}/deletion-preview").status_code == 401
    assert remove(world, kb).status_code == 401
    headers = {"Authorization": f"Bearer {config.ACCESS_TOKEN}"}
    assert world.patch(f"/knowledge-bases/{kb}", json={"name": "乙"}, headers=headers).status_code == 200
    assert world.delete(f"/knowledge-bases/{kb}", params={"confirm": True}, headers=headers).status_code == 200


def test_delete_logs_the_principal(world, caplog):
    kb = world.post("/knowledge-bases", json={"name": "甲"}).json()["id"]
    with caplog.at_level(logging.INFO, logger="briefly.knowledge.api"):
        assert remove(world, kb).status_code == 200
    assert any(kb in r.getMessage() and "principal=local" in r.getMessage() for r in caplog.records)
