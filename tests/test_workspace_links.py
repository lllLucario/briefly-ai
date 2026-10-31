"""工作区 ↔ 知识库文档关联（二期）：显式关联、疑似重复提示与确认/否认、外发继承、改稿提醒。
假编码器、原创样本，不调用模型。"""
from types import SimpleNamespace

import pytest

pytest.importorskip("sqlite_vec", reason="知识索引回归需安装.[rag]")
from fastapi.testclient import TestClient

from briefly.api import create_app
from briefly.db import create_db_engine
from briefly.knowledge.qa import KnowledgeQA
from briefly.knowledge.retrieval import RetrievalService
from briefly.links import CONFIRMED, DISMISSED, EXPLICIT, SUGGESTED, LinkService, is_suggested, name_similarity, overlap, sentence_keys
from briefly.materials import KNOWLEDGE, EgressRestricted, MaterialStore
from test_knowledge_index import indexed  # noqa: F401  夹具
from test_knowledge_slicing import Encoder
from test_materials import run_tool
from tracking_util import env  # noqa: F401  追踪夹具

POLICY = ("# 门店值班制度\n## 第一章 总则\n第一条 为保证门店营业期间有人负责现场管理，制定本制度。\n"
          "第二条 本制度适用于全部直营门店的店长、副店长和值班主管。\n## 第二章 值班安排\n"
          "第三条 每天开店前三十分钟由值班主管完成设备检查并在系统中签到。\n"
          "第四条 值班期间发生客诉，值班主管须在十五分钟内到场处理并记录。\n"
          "第五条 节假日值班表由店长在前一周的周五之前发布。\n## 第三章 附则\n第六条 本制度由运营管理部负责解释。")


def publish(store, svc, lifecycle, kb, title, content):
    material, _ = store.add_text(title, content, source=KNOWLEDGE)
    doc, version, _ = svc.attach(kb.id, material.id)
    lifecycle.build(kb.id, doc.id, version.id, Encoder(), expected_revision=doc.revision)
    return doc


def test_copy_to_workspace_records_explicit_link_and_inherits_egress(indexed):  # noqa: F811
    engine, store, svc, lifecycle = indexed
    kb = svc.create("运营制度")
    doc = publish(store, svc, lifecycle, kb, "门店值班制度", POLICY)
    links = LinkService(engine)
    copy, added = links.copy_to_workspace(store, kb.id, doc.id)
    assert added and store.get(copy.id).source != KNOWLEDGE  # 进了工作区，知识版本仍引用同一条
    [link] = links.material_links(copy.id)
    assert link["kind"] == EXPLICIT and link["document_title"] == "门店值班制度" and link["version"] == 1
    assert not link["egress_allowed"]
    # 该库未允许外发：副本内容不发给模型，说明怎么解决
    with pytest.raises(EgressRestricted, match="运营制度.*尚未允许外发"):
        store.require_egress([copy.id])
    denied = run_tool(store, "read_material", {"material_id": copy.id})
    assert "尚未允许外发" in denied.content and "十五分钟" not in denied.content
    listed = run_tool(store, "list_materials", {}).content
    assert "内容不可读取" in listed
    svc.set_egress(kb.id, True)
    store.require_egress([copy.id])  # 开启授权后放行
    assert "十五分钟" in run_tool(store, "read_material", {"material_id": copy.id}).content


def test_search_hides_restricted_hits_and_says_why(indexed):  # noqa: F811
    engine, store, svc, lifecycle = indexed
    kb = svc.create("运营制度")
    doc = publish(store, svc, lifecycle, kb, "门店值班制度", POLICY)
    LinkService(engine).copy_to_workspace(store, kb.id, doc.id)
    store.add_text("巡店记录", "周三巡店：值班主管准时签到，客诉处理及时。")
    text = run_tool(store, "search_materials", {"keywords": "值班主管"}).content
    assert "巡店记录" in text and "十五分钟内到场" not in text and "尚未允许外发" in text


def test_attachment_and_weekly_respect_inherited_restriction(indexed):  # noqa: F811
    from briefly.agent import Agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    engine, store, svc, lifecycle = indexed
    kb = svc.create("运营制度")
    doc = publish(store, svc, lifecycle, kb, "门店值班制度", POLICY)
    copy, _ = LinkService(engine).copy_to_workspace(store, kb.id, doc.id)
    model = GenericFakeChatModel(messages=iter(["不应调用"]))
    reply = Agent(model, tools=[], store=store).chat("总结一下", material_ids=[copy.id])
    assert "尚未允许外发" in reply  # 没有调用模型，直接说明

    from datetime import date
    from langchain_core.messages import AIMessage
    from briefly.weekly.engine import ReportWriter
    from briefly.weekly.models import WeeklyReport
    from fakes import FunctionModel
    model = FunctionModel(respond=lambda _: AIMessage("【本周完成】\n- 无"), calls=[])
    writer = ReportWriter(store)
    writer._model_factory = lambda: model
    report = WeeklyReport(period_start=date(2026, 10, 26), period_end=date(2026, 11, 1), note="",
                          material_ids=[copy.id], inputs={})
    _, _, generation = writer.write(report)
    assert all("十五分钟" not in str(call) for call in model.calls)  # 内容没有发给模型
    assert any("尚未允许外发" in w and "本次未纳入" in w for w in generation["warnings"])


def test_suggested_duplicate_confirm_and_dismiss(indexed):  # noqa: F811
    engine, store, svc, lifecycle = indexed
    kb = svc.create("运营制度")
    doc = publish(store, svc, lifecycle, kb, "门店值班制度", POLICY)
    links = LinkService(engine)
    revised = POLICY.replace("十五分钟", "十分钟").replace("周五之前", "周四之前") + "\n第七条 本制度自下月一日起施行。"
    mine, _ = store.add_text("门店值班制度（修订稿）.docx", revised)
    other, _ = store.add_text("巡店记录", "周三巡店：冷柜温度偏高，已报修。")
    assert links.detect() == 1
    [link] = links.material_links(mine.id)
    assert link["kind"] == SUGGESTED and 0.4 <= link["score"] < 1
    assert links.material_links(other.id) == []
    store.require_egress([mine.id])  # 未确认前不继承外发限制
    links.decide(link["id"], True)
    assert links.material_links(mine.id)[0]["kind"] == CONFIRMED
    with pytest.raises(EgressRestricted):
        store.require_egress([mine.id])
    links.decide(link["id"], False)  # 解除
    assert links.material_links(mine.id) == []
    assert links.detect() == 0  # 否认过的这一对不再提示
    store.require_egress([mine.id])


def test_change_reminder_for_tracked_copy_and_answers(env, tmp_path):  # noqa: F811
    from briefly.knowledge.index import LocalIndex
    from briefly.knowledge.lifecycle import IndexLifecycle
    from briefly.knowledge.service import KnowledgeService

    store, engine = env.store, env.store.engine
    svc, lifecycle = KnowledgeService(engine), IndexLifecycle(engine, LocalIndex(tmp_path / "kb.db"))
    kb = svc.create("运营制度")
    doc = publish(store, svc, lifecycle, kb, "门店值班制度", POLICY)
    links = LinkService(engine)
    env.write("值班制度.md", POLICY)  # 追踪的本地文件与发布版相同
    env.svc.add_paths([str(env.work / "值班制度.md")])
    assert links.detect() == 1
    links.decide(links.material_links(store.list_materials()[0].id)[0]["id"], True)
    assert links.document_updates([doc.id]) == {}  # 内容一致，不提醒
    env.write("值班制度.md", POLICY.replace("十五分钟", "十分钟"))
    env.svc.scan()
    update = links.document_updates([doc.id])[doc.id]
    assert update["version"] == 1 and "值班制度" in update["title"]
    # 关联跟着追踪文件走：新版本同样继承外发限制
    latest = store.list_materials()[0].id
    with pytest.raises(EgressRestricted):
        store.require_egress([latest])
    # 相关回答里提醒“工作区有更新，知识库仍为第 1 版”（依据包警告），不自动发布
    svc.set_egress(kb.id, True)
    retrieval = RetrievalService(SimpleNamespace(knowledge=svc, lifecycle=lifecycle, encoder_factory=Encoder))
    bundle = KnowledgeQA(retrieval, lambda: None).bundle(kb.id, ["客诉多久到场"])
    assert bundle["status"] == "ready"
    assert any("工作区有更新" in w and "知识库仍为第 1 版" in w for w in bundle["warnings"])
    assert svc.scope([kb.id])["versions"][0]["version_id"]  # 知识库版本未变


def test_calibrated_threshold_on_original_samples():
    revision = sentence_keys(POLICY.replace("十五分钟", "十分钟") + "\n第七条 本制度自下月一日起施行。")
    assert is_suggested(overlap(sentence_keys(POLICY), revision), 0.0)
    template = POLICY.replace("值班", "盘点").replace("客诉", "差异").replace("设备检查", "库存清点")
    score = overlap(sentence_keys(POLICY), sentence_keys(template))
    assert not is_suggested(score, name_similarity("门店值班制度.docx", "门店盘点制度.docx"))
    assert name_similarity("门店值班制度.docx", "门店值班制度（修订稿）v2.docx") == 1.0


@pytest.fixture
def client(tmp_path, monkeypatch):
    from briefly import config
    monkeypatch.setattr(config, "KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
    engine = create_db_engine("sqlite:///" + (tmp_path / "api.db").as_posix())
    store = MaterialStore(engine)
    app = create_app(store)
    app.state.knowledge_imports.encoder_factory = Encoder
    try:
        with TestClient(app) as client:
            yield client, app, store
    finally:
        engine.dispose()


def test_api_links_hint_confirm_copy_and_document_update(client):
    client, app, store = client
    svc, lifecycle = app.state.knowledge, app.state.knowledge_imports.lifecycle
    kb = svc.create("运营制度")
    doc = publish(store, svc, lifecycle, kb, "门店值班制度", POLICY)
    # 工作区上传了改稿：后台检测后材料页提示
    mine = client.post("/materials", json={"title": "门店值班制度-修订稿", "content": POLICY.replace("十五分钟", "十分钟")}).json()["material"]
    store.workspace_index.wait()
    detail = client.get(f"/materials/{mine['id']}").json()
    [link] = detail["links"]
    assert link["kind"] == SUGGESTED and link["document_title"] == "门店值班制度"
    assert client.post(f"/links/{link['id']}/confirm").status_code == 200
    assert client.get(f"/materials/{mine['id']}").json()["links"][0]["kind"] == CONFIRMED
    documents = client.get(f"/knowledge-bases/{kb.id}/documents").json()
    assert documents[0]["workspace_update"]["version"] == 1  # 知识库文档页的改稿提醒
    assert client.post("/links/999/confirm").status_code == 404
    # 复制到工作区：显式关联
    copied = client.post(f"/knowledge-bases/{kb.id}/documents/{doc.id}/copy-to-workspace").json()
    assert copied["added"] and client.get(f"/materials/{copied['material_id']}").json()["links"][0]["kind"] == EXPLICIT
    assert client.post(f"/links/{link['id']}/dismiss").status_code == 200
    assert client.get(f"/knowledge-bases/{kb.id}/documents").json()[0]["workspace_update"] is None


def test_attaching_workspace_material_to_knowledge_writes_explicit_link(client):
    client, app, store = client
    kb = client.post("/knowledge-bases", json={"name": "项目资料"}).json()["id"]
    mine = client.post("/materials", json={"title": "项目章程", "content": "项目目标：三个月内完成门店点单系统改造。"}).json()["material"]
    created = client.post(f"/knowledge-bases/{kb}/documents", json={"material_id": mine["id"]}).json()
    links = client.get(f"/materials/{mine['id']}").json()["links"]
    assert links[0]["kind"] == EXPLICIT and links[0]["document_id"] == created["document"]["id"]
    assert links[0]["kind"] != DISMISSED


def test_weekly_review_list_skips_changes_of_restricted_tracked_copy(env, tmp_path):  # noqa: F811
    from briefly.knowledge.index import LocalIndex
    from briefly.knowledge.lifecycle import IndexLifecycle
    from briefly.knowledge.service import KnowledgeService
    from test_tracking_evidence import WEEK, fake_model

    store, engine = env.store, env.store.engine
    svc, lifecycle = KnowledgeService(engine), IndexLifecycle(engine, LocalIndex(tmp_path / "kb.db"))
    kb = svc.create("运营制度")
    doc = publish(store, svc, lifecycle, kb, "门店值班制度", POLICY)
    env.write("值班制度.md", POLICY)
    env.svc.add_paths([str(env.work / "值班制度.md")])
    LinkService(engine).link_explicit(store.list_materials()[0].id, kb.id, doc.id)
    env.write("值班制度.md", POLICY + "\n第七条 值班主管每周五提交值班总结。")
    model = fake_model()
    prep, info = env.weekly(lambda: model).prepare(WEEK[0], scan=True)
    assert all("值班总结" not in str(call) for call in model.calls)  # 改动内容没有发给模型
    assert "尚未允许外发" in str(info)
