"""删除未允许外发的知识库后，工作区副本继续禁止外发（登记表第 97 条）。假编码器、假模型、原创虚构资料。"""
from datetime import date
from types import SimpleNamespace

import pytest

pytest.importorskip("sqlite_vec", reason="知识库管理回归需安装.[rag]")
from fastapi.testclient import TestClient
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from briefly import config
from briefly.agent import Agent
from briefly.api import create_app
from briefly.db import create_db_engine
from briefly.knowledge import removal
from briefly.knowledge.assistant import KnowledgeAssistant
from briefly.knowledge.index import LocalIndex
from briefly.knowledge.ingestion import ImportService
from briefly.knowledge.qa import KnowledgeQA
from briefly.knowledge.retrieval import RetrievalService
from briefly.links import DocumentLink, EgressHold, LinkService
from briefly.loaders import LoadedDocument
from briefly.materials import KNOWLEDGE, EgressRestricted, MaterialStore
from briefly.weekly.engine import ReportWriter
from briefly.weekly.models import WeeklyReport
from briefly.workspace_index import WorkspaceIndex
from db_util import file_engine
from fakes import FunctionModel, structured
from test_materials import run_tool
from test_workspace_index import BagEncoder
from test_workspace_links import POLICY, publish
from tracking_util import env  # noqa: F401  追踪夹具

REVISED = POLICY.replace("十五分钟", "十分钟").replace("周五之前", "周四之前") + "\n第七条 本制度自下月一日起施行。"


@pytest.fixture
def world(tmp_path):
    engine = create_db_engine("sqlite:///" + (tmp_path / "app.db").as_posix())
    encoder = BagEncoder()
    store = MaterialStore(engine)
    store.workspace_index = WorkspaceIndex(engine, tmp_path / "ws.sqlite", lambda: encoder)
    imports = ImportService(engine, tmp_path / "knowledge", LocalIndex(tmp_path / "kb.sqlite"), lambda: encoder)
    links = LinkService(engine)

    def base(name="运营制度", *, title="门店值班制度", content=POLICY):
        kb = imports.knowledge.create(name)
        return kb, publish_with(kb, title, content)

    def publish_with(kb, title, content):
        material, _ = store.add_text(title, content, source=KNOWLEDGE)
        doc, version, _ = imports.knowledge.attach(kb.id, material.id)
        imports.lifecycle.build(kb.id, doc.id, version.id, encoder, expected_revision=doc.revision)
        return doc

    try:
        yield SimpleNamespace(engine=engine, store=store, imports=imports, svc=imports.knowledge, links=links, base=base,
                              publish=publish_with, encoder=encoder,
                              retrieval=RetrievalService(imports), remove=lambda kb: removal.delete_base(imports, kb.id))
    finally:
        engine.dispose()


def holds(world):
    with Session(world.engine) as s:
        return list(s.scalars(select(EgressHold)))


def held_copy(world, name="运营制度"):
    kb, doc = world.base(name)
    copy, _ = world.links.copy_to_workspace(world.store, kb.id, doc.id)
    return kb, doc, copy


# ---- 什么时候打标记
def test_unauthorized_base_leaves_hold_with_names(world):
    kb, doc, copy = held_copy(world)
    assert removal.deletion_preview(world.imports, kb.id)["egress_holds"] == 1
    result = world.remove(kb)
    assert result["egress_holds"] == 1 and result["workspace_links"] == 1
    [hold] = holds(world)
    assert (hold.material_id, hold.kb_id, hold.kb_name, hold.document_title) == (copy.id, kb.id, "运营制度", doc.title)
    with pytest.raises(EgressRestricted, match="运营制度.*门店值班制度.*解除限制"):
        world.store.require_egress([copy.id])
    with Session(world.engine) as s:
        assert s.scalar(select(func.count()).select_from(DocumentLink)) == 0  # 关联照样断开
    assert world.links.material_links(copy.id) == []
    assert world.links.material_holds(copy.id)[0]["kb_name"] == "运营制度"


def test_grant_turned_off_counts_as_unauthorized(world):
    kb, _, copy = held_copy(world)
    world.svc.set_egress(kb.id, True)
    world.svc.set_egress(kb.id, False)
    world.remove(kb)
    assert world.store.egress_blocked([copy.id])


def test_authorized_base_leaves_no_hold(world):
    kb, _, copy = held_copy(world)
    world.svc.set_egress(kb.id, True)
    assert removal.deletion_preview(world.imports, kb.id)["egress_holds"] == 0
    assert world.remove(kb)["egress_holds"] == 0
    assert holds(world) == []
    world.store.require_egress([copy.id])
    assert "十五分钟" in run_tool(world.store, "read_material", {"material_id": copy.id}).content


def test_suggested_and_dismissed_links_get_no_hold(world):
    kb, _ = world.base()
    mine, _ = world.store.add_text("门店值班制度（修订稿）.docx", REVISED)
    dismissed, _ = world.store.add_text("门店值班制度（再修订）.docx", REVISED + "\n第八条 另行通知。")
    assert world.links.detect() == 2
    world.links.decide(world.links.material_links(dismissed.id)[0]["id"], False)
    preview = removal.deletion_preview(world.imports, kb.id)
    assert preview["egress_holds"] == 0 and preview["workspace_links"] == 1
    assert world.remove(kb)["egress_holds"] == 0
    assert holds(world) == []
    assert world.store.egress_blocked([mine.id, dismissed.id]) == {}


def test_confirmed_link_gets_hold(world):
    kb, _ = world.base()
    mine, _ = world.store.add_text("门店值班制度（修订稿）.docx", REVISED)
    assert world.links.detect() == 1
    world.links.decide(world.links.material_links(mine.id)[0]["id"], True)
    world.remove(kb)
    assert world.store.egress_blocked([mine.id])


def test_hold_is_in_the_same_transaction_as_delete(world, monkeypatch):
    kb, _, copy = held_copy(world)

    def broken(session):
        raise RuntimeError("中途失败")

    monkeypatch.setattr(removal, "purge_knowledge_answers", broken)
    with pytest.raises(RuntimeError):
        world.remove(kb)
    assert holds(world) == []  # 回滚：库和关联还在
    assert world.links.material_links(copy.id)


def test_hold_survives_other_base_being_authorized(world):
    kb, _, copy = held_copy(world)
    other = world.svc.create("另一个库")
    doc = world.publish(other, "门店值班制度", POLICY)
    world.links.link_explicit(copy.id, other.id, doc.id)
    world.svc.set_egress(other.id, True)
    world.remove(kb)
    assert world.store.egress_blocked([copy.id])  # 另一个库已授权也不放开
    world.links.release_holds(copy.id)
    assert world.store.egress_blocked([copy.id]) == {}


def test_deleting_material_removes_its_hold(world):
    kb, _, copy = held_copy(world)
    world.remove(kb)
    world.store.delete(copy.id)
    assert holds(world) == []


def test_material_shared_with_another_base_returns_to_it_without_hold(world):
    """材料和另一个库共用一条记录：在工作区删除只是移出，标记跟着去掉，知识库一侧照常。"""
    kb, _, copy = held_copy(world)
    other = world.svc.create("另一个库")
    world.publish(other, "门店值班制度", POLICY)
    world.remove(kb)
    assert holds(world)
    world.store.delete(copy.id)
    assert holds(world) == []
    assert world.store.get(copy.id, workspace=False).source == KNOWLEDGE


def test_tracked_file_hold_follows_new_versions(env, tmp_path):  # noqa: F811
    store, engine = env.store, env.store.engine
    imports = ImportService(engine, tmp_path / "knowledge", LocalIndex(tmp_path / "kb.db"), BagEncoder)
    kb = imports.knowledge.create("运营制度")
    material, _ = store.add_text("门店值班制度", POLICY, source=KNOWLEDGE)
    doc, version, _ = imports.knowledge.attach(kb.id, material.id)
    imports.lifecycle.build(kb.id, doc.id, version.id, BagEncoder(), expected_revision=doc.revision)
    env.write("值班制度.md", POLICY)
    env.svc.add_paths([str(env.work / "值班制度.md")])
    links = LinkService(engine)
    links.link_explicit(store.list_materials()[0].id, kb.id, doc.id)
    removal.delete_base(imports, kb.id)
    env.write("值班制度.md", POLICY.replace("十五分钟", "十分钟"))
    env.svc.scan()
    latest = store.list_materials()[0].id
    with pytest.raises(EgressRestricted, match="运营制度"):
        store.require_egress([latest])  # 追踪文件的新版本同样受限
    assert links.release_holds(latest) == 1
    store.require_egress([latest])


def test_weekly_review_skips_held_tracked_file_until_released(env, tmp_path):  # noqa: F811
    from test_tracking_evidence import WEEK, fake_model

    store, engine = env.store, env.store.engine
    imports = ImportService(engine, tmp_path / "knowledge", LocalIndex(tmp_path / "kb.db"), BagEncoder)
    kb = imports.knowledge.create("运营制度")
    material, _ = store.add_text("门店值班制度", POLICY, source=KNOWLEDGE)
    doc, version, _ = imports.knowledge.attach(kb.id, material.id)
    imports.lifecycle.build(kb.id, doc.id, version.id, BagEncoder(), expected_revision=doc.revision)
    env.write("值班制度.md", POLICY)
    env.svc.add_paths([str(env.work / "值班制度.md")])
    links = LinkService(engine)
    links.link_explicit(store.list_materials()[0].id, kb.id, doc.id)
    removal.delete_base(imports, kb.id)
    env.write("值班制度.md", POLICY + "\n第七条 值班主管每周五提交值班总结。")
    model = fake_model()
    _, info = env.weekly(lambda: model).prepare(WEEK[0], scan=True)
    assert all("值班总结" not in str(call) for call in model.calls)
    assert "该库删除时未允许外发" in str(info)


# ---- 外发入口：解除前后
def test_material_tools_before_and_after_release(world):
    kb, _, copy = held_copy(world)
    world.store.add_text("巡店记录", "周三巡店：值班主管准时签到，客诉处理及时。")
    world.remove(kb)
    run = lambda name, args: run_tool(world.store, name, args).content

    assert "内容不可读取" in run("list_materials", {})
    for name, args in (("get_outline", {"material_id": copy.id}), ("read_material", {"material_id": copy.id}),
                       ("summarize_materials", {"material_ids": [copy.id]})):
        denied = run_tool(world.store, name, args, material_ids=[copy.id]).content
        assert "未允许外发" in denied and "十五分钟" not in denied, name
    found = run("search_materials", {"keywords": "值班主管"})
    assert "巡店记录" in found and "十五分钟内到场" not in found and "未允许外发" in found

    world.links.release_holds(copy.id)
    assert "内容不可读取" not in run("list_materials", {})
    assert "章节大纲" in run("get_outline", {"material_id": copy.id})
    assert "十五分钟" in run("read_material", {"material_id": copy.id})
    assert "十五分钟内到场" in run("search_materials", {"keywords": "值班主管"})
    world.store.require_egress([copy.id])  # summarize_materials 的同一道检查


def test_attachment_before_and_after_release(world):
    kb, _, copy = held_copy(world)
    world.remove(kb)
    untouched = GenericFakeChatModel(messages=iter(["不应调用"]))
    assert "未允许外发" in Agent(untouched, tools=[], store=world.store).chat("总结一下", material_ids=[copy.id])
    world.links.release_holds(copy.id)
    model = FunctionModel(respond=lambda _: AIMessage("好的"), calls=[])
    assert Agent(model, tools=[], store=world.store).chat("总结一下", material_ids=[copy.id]) == "好的"
    assert any("十五分钟" in str(call) for call in model.calls)


def test_weekly_before_and_after_release(world):
    kb, _, copy = held_copy(world)
    world.remove(kb)
    model = FunctionModel(respond=lambda _: AIMessage("【本周完成】\n- 无"), calls=[])
    writer = ReportWriter(world.store)
    writer._model_factory = lambda: model
    report = WeeklyReport(period_start=date(2026, 10, 26), period_end=date(2026, 11, 1), note="", material_ids=[copy.id], inputs={})
    _, _, generation = writer.write(report)
    assert all("十五分钟" not in str(call) for call in model.calls)
    assert any("未允许外发" in w and "本次未纳入" in w for w in generation["warnings"])
    world.links.release_holds(copy.id)
    _, _, generation = writer.write(report)
    assert not any("未允许外发" in w for w in generation["warnings"])


def joint_world(world):
    """被删的未授权库留下一份副本；另一个已授权的库用来提问。"""
    kb, _, copy = held_copy(world)
    world.remove(kb)
    other = world.svc.create("客服手册")
    world.publish(other, "客服手册", "# 客服手册\n## 投诉\n客诉升级后由值班主管记录并在当日回访顾客。")
    world.svc.set_egress(other.id, True)
    world.store.workspace_index.sync(world.encoder)
    return other, copy


QUESTION = "值班主管客诉多久到场"


def test_workspace_index_keeps_the_copy_but_joint_bundle_drops_it(world):
    other, copy = joint_world(world)
    assert any(e["material_id"] == copy.id for e in world.store.workspace_index.evidence(QUESTION))  # 本地索引不变
    bundle = KnowledgeQA(world.retrieval, lambda: None, workspace=world.store).bundle(other.id, [QUESTION])
    assert "十五分钟" not in bundle.get("payload_text", "")
    assert all(e.get("material_id") != copy.id for e in bundle["evidence"])
    assert any("该库删除时未允许外发" in w for w in bundle["warnings"])
    world.links.release_holds(copy.id)
    bundle = KnowledgeQA(world.retrieval, lambda: None, workspace=world.store).bundle(other.id, [QUESTION])
    assert "十五分钟" in bundle["payload_text"]
    assert not any("未允许外发" in w for w in bundle["warnings"])


def test_shortcut_answer_before_and_after_release(world):
    other, copy = joint_world(world)
    model = FunctionModel(respond=lambda _: AIMessage("【可答性：依据不足】未提及"), calls=[])
    qa = KnowledgeQA(world.retrieval, lambda: model, workspace=world.store)
    held = qa.answer(other.id, QUESTION)
    assert all("十五分钟" not in str(call) for call in model.calls)
    assert any("该库删除时未允许外发" in w for w in held["warnings"])
    world.links.release_holds(copy.id)
    model.calls.clear()
    qa.answer(other.id, QUESTION)
    assert any("十五分钟" in str(call) for call in model.calls)


def test_assistant_search_before_and_after_release(world):
    other, copy = joint_world(world)
    assistant = KnowledgeAssistant(world.retrieval, lambda: None, workspace=world.store)
    held = assistant.search(other.id, [QUESTION])
    assert "十五分钟" not in held.get("payload_text", "") and any("未允许外发" in w for w in held["warnings"])
    world.links.release_holds(copy.id)
    assert "十五分钟" in assistant.search(other.id, [QUESTION])["payload_text"]


# ---- 接口
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
    engine = file_engine()
    respond = lambda messages: (structured("TodoCandidates", items=[]) if "TodoCandidates" in messages[0].content
                                else AIMessage("整理好了"))
    model = FunctionModel(respond=respond, calls=[])
    app = create_app(MaterialStore(engine), model_factory=lambda: model, agent_factory=lambda s: Agent(model, store=s))
    app.state.knowledge_imports.encoder_factory = BagEncoder
    app.state.knowledge_imports.parser = lambda filename, data: LoadedDocument(data.decode(), True)
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.model = model
        yield c
    engine.dispose()


def held_material(client, *, allow=False):
    kb = client.post("/knowledge-bases", json={"name": "运营制度"}).json()["id"]
    assert client.post(f"/knowledge-bases/{kb}/imports", data={"request_key": "r", "tokens": "64"},
                       files=[("files", ("制度.md", POLICY.encode()))]).status_code == 202
    assert client.get(f"/knowledge-bases/{kb}/imports").json()[0]["status"] == "ready"
    doc = client.get(f"/knowledge-bases/{kb}/documents").json()[0]
    material = client.post(f"/knowledge-bases/{kb}/documents/{doc['id']}/copy-to-workspace").json()["material_id"]
    if allow:
        client.put(f"/knowledge-bases/{kb}/egress", json={"allowed": True})
    assert client.delete(f"/knowledge-bases/{kb}", params={"confirm": True}).status_code == 200
    return material


def test_release_needs_confirmation_and_logs(client, caplog):
    import logging

    material = held_material(client)
    detail = client.get(f"/materials/{material}").json()
    assert detail["egress_holds"][0]["kb_name"] == "运营制度" and detail["egress_holds"][0]["document_title"] == "制度.md"
    url = f"/materials/{material}/egress-holds/release"
    refused = client.post(url)
    assert refused.status_code == 409 and refused.json().get("confirm") is True
    assert client.get(f"/materials/{material}").json()["egress_holds"]  # 未确认不解除
    assert client.post("/materials/99999/egress-holds/release", params={"confirm": True}).status_code == 404
    with caplog.at_level(logging.INFO, logger="briefly.api"):
        released = client.post(url, params={"confirm": True})
    assert released.status_code == 200 and released.json()["released"] == 1
    assert any(f"material_id={material}" in r.getMessage() and "principal=local" in r.getMessage() for r in caplog.records)
    assert client.get(f"/materials/{material}").json()["egress_holds"] == []
    assert client.post(url, params={"confirm": True}).json()["released"] == 0  # 重复解除不报错


def test_release_requires_login(client, monkeypatch):
    material = held_material(client)
    monkeypatch.setattr(config, "ACCESS_TOKEN", "abcdef0123456789abcdef0123456789")
    url = f"/materials/{material}/egress-holds/release"
    assert client.post(url, params={"confirm": True}).status_code == 401
    monkeypatch.setattr(config, "ACCESS_TOKEN", "")
    assert client.get(f"/materials/{material}").json()["egress_holds"]


def test_chat_attachment_and_todo_preview_through_api(client):
    material = held_material(client)
    reply = client.post("/chat", json={"text": "总结一下", "material_ids": [material]})
    assert reply.status_code == 200 and "未允许外发" in reply.json()["reply"]
    assert all("十五分钟" not in str(call) for call in client.model.calls)

    # 解除后才能附带；再次打标记模拟“回答之后才受限”，待办预览同样拒绝
    client.post(f"/materials/{material}/egress-holds/release", params={"confirm": True})
    answered = client.post("/chat", json={"text": "总结一下", "material_ids": [material]}).json()
    thread = answered["thread_id"]
    message = client.get(f"/chat/sessions/{thread}").json()["messages"][-1]["message_id"]
    assert client.post(f"/chat/sessions/{thread}/todo-preview", json={"message_id": message}).status_code == 200
    with Session(client.app.state.store.engine) as s, s.begin():
        s.add(EgressHold(material_id=material, kb_id="k", kb_name="运营制度", document_id="d", document_title="制度.md"))
    calls = len(client.model.calls)
    denied = client.post(f"/chat/sessions/{thread}/todo-preview", json={"message_id": message})
    assert denied.status_code == 400 and "未允许外发" in denied.json()["detail"]
    assert len(client.model.calls) == calls


def test_authorized_base_copy_has_no_hold_in_detail(client):
    material = held_material(client, allow=True)
    assert client.get(f"/materials/{material}").json()["egress_holds"] == []
