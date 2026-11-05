"""用户明确要求重做时不读模型缓存，结果通过检查后仍写入。全部走真实接口，假模型每次回答都不同，内容才能看出变化。"""
import itertools
import re

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from briefly import config
from briefly.agent import Agent
from briefly.api import create_app
from briefly.conflicts import FactConflict
from briefly.llm import fresh_model_calls
from briefly.skills import read_prompt
from fakes import FunctionModel, structured
from test_tracking_evidence import WEEK
from tracking_util import env  # noqa: F401  夹具


@pytest.fixture(autouse=True)
def cache_on(monkeypatch):
    monkeypatch.setattr(config, "LLM_CACHE", True)


class Counting:
    """按调用点计数的假模型：描述和周报每次调用都写出不同的内容。"""

    def __init__(self):
        self.compose, self.report, self.judge = itertools.count(1), itertools.count(1), itertools.count(1)
        self.calls = dict(compose=0, report=0, judge=0)

    def respond(self, messages):
        system, user = messages[0].content, messages[-1].content
        if "变化记录中，提取" in system:
            body = user.split("【材料开始】\n", 1)[1].split("\n【材料结束】", 1)[0]
            quotes = [line.split("：", 1)[1] for line in body.splitlines() if line.startswith(("新增［", "  改为："))]
            return structured("Extraction", items=[dict(category="other", point=f"补充：{q}", quote=q) for q in quotes])
        if "候选工作记录" in system:
            self.calls["compose"] += 1
            return structured("EvidenceDraft", title="更新文档", description=f"描述第{next(self.compose)}版", category="other")
        if "撰写周报" in system:
            self.calls["report"] += 1
            refs = re.findall(r"#(\d+) (【[^】]*】[^\n]*)", user)
            lines = "\n".join(f"- {text}〔#{n}〕" for n, text in refs)
            return AIMessage(f"【本周完成】\n周报第{next(self.report)}版\n{lines}")
        if "判断几处" in system:
            self.calls["judge"] += 1
            next(self.judge)
            return structured("_Judgments", judgments=[dict(index=1, resolution="unresolved", explanation="", evidence="", current=0)])
        return structured("Extraction", items=[])


def stats(client) -> dict:
    return {r["name"]: r for r in client.get("/cache/stats").json()["caches"]}


def generate(client, report_id) -> dict:
    """TestClient 等后台任务跑完才返回，所以这里读到的就是生成结果。"""
    assert client.post(f"/weekly/{report_id}/generate", json={}).status_code == 202
    report = client.get(f"/weekly/{report_id}").json()
    assert report["status"] == "done"
    return report


@pytest.fixture
def weekly(env):
    env.write("项目/方案.md", "# 方案\n原有内容")
    counting = Counting()
    model = FunctionModel(respond=counting.respond, calls=[])
    with TestClient(create_app(env.store, model_factory=lambda: model), base_url="http://127.0.0.1") as client:
        client.put("/weekly-settings", json={"owner_names": ["张三"], "audience": "导师", "author": "王五"})
        client.post("/tracking/sources", json={"paths": [str(env.work / "项目")]})
        env.write("项目/方案.md", "# 方案\n原有内容\n补充灰度发布方案")
        client.post("/tracking/scan")
        report = client.post("/weekly/prepare", json={"day": WEEK[0].isoformat()}).json()
        yield client, counting, report["id"], report["checklist_items"][0]["id"]


def test_regenerate_description_calls_model_again_and_content_changes(weekly):
    client, counting, report_id, item = weekly
    assert counting.calls["compose"] == 1
    descriptions = []
    for _ in range(2):
        response = client.post(f"/weekly/{report_id}/items/{item}/regenerate")
        assert response.status_code == 200
        descriptions.append(next(i for i in response.json()["checklist_items"] if i["id"] == item)["description"])
    assert counting.calls["compose"] == 3
    assert descriptions[0] != descriptions[1] and "第3版" in descriptions[1]
    stat = stats(client)["llm.evidence_compose"]
    assert stat["hits"] == 0 and stat["refreshed"] == 2
    assert stat["stores"] == 3  # 重做的结果通过检查后仍然写入


def test_regenerate_report_calls_model_again_and_content_changes(weekly):
    client, counting, report_id, item = weekly
    client.post(f"/weekly/{report_id}/review", json={"action": "confirm", "ids": [item]})
    first, second = generate(client, report_id), generate(client, report_id)
    assert counting.calls["report"] == 2
    assert "第1版" in first["content"] and "第2版" in second["content"]
    stat = stats(client)["llm.synthesize"]
    assert stat["hits"] == 0 and stat["refreshed"] == 2 and stat["stores"] == 2


def test_conflict_judgment_on_generate_path_is_not_read_from_cache(weekly, monkeypatch):
    client, counting, report_id, item = weekly
    client.post(f"/weekly/{report_id}/review", json={"action": "confirm", "ids": [item]})
    conflict = lambda *a, **k: [FactConflict(subject="方案", attribute="date", fact_ids=["a", "b"], values=["1日", "2日"], numbers=[1, 1],  # noqa: E731
                                             origins=["material", "material"], subjects=["方案"], sources=["1-1", "1-2"])]
    monkeypatch.setattr("briefly.weekly.engine.detect_conflicts", conflict)
    monkeypatch.setattr("briefly.conflicts._member_quotes", lambda c, items, brief: [])
    generate(client, report_id)
    generate(client, report_id)
    assert counting.calls["judge"] == 2
    stat = stats(client)["llm.conflict_judge"]
    assert stat["hits"] == 0 and stat["refreshed"] == 2 and stat["stores"] == 2


# ---- 助理工具：用户在对话里要求重新总结
def digest_app(env):
    calls = {"digest": 0, "reply": 0}
    system_digest = read_prompt("digest")
    counter = itertools.count(1)

    def respond(messages):
        system = str(messages[0].content)
        if system == system_digest:
            calls["digest"] += 1
            return AIMessage(f"汇总第{next(counter)}版〔#1〕")
        if "提取要点" in system or "提取" in system and "【材料开始】" in str(messages[-1].content):
            return structured("Extraction", items=[dict(category="done", point="完成灰度发布", quote="完成灰度发布")])
        if isinstance(messages[-1], ToolMessage):
            calls["reply"] += 1
            return AIMessage("已整理好要点。")
        assert isinstance(messages[-1], HumanMessage)
        return AIMessage("", tool_calls=[dict(name="summarize_materials", args=dict(material_ids=[material.id]), id="s1", type="tool_call")])

    model = FunctionModel(respond=respond, calls=[])
    material, _ = env.store.add_text("周记", "本周完成灰度发布，后续继续观察。")
    app = create_app(env.store, model_factory=lambda: model, agent_factory=lambda store: Agent(model, store=store))
    return app, calls


@pytest.mark.parametrize("redo", ["请重新总结一遍周记", "再总结一次周记", "重做一下周记的总结"])
def test_assistant_digest_is_regenerated_when_user_asks(env, redo):
    app, calls = digest_app(env)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        assert client.post("/chat", json=dict(text="帮我梳理周记的全部要点")).status_code == 200
        assert calls["digest"] == 1
        assert client.post("/chat", json=dict(text="帮我梳理周记的全部要点")).status_code == 200
        assert calls["digest"] == 1  # 没有要求重做：同样的材料和关注点继续读缓存
        assert stats(client)["llm.synthesize"]["hits"] == 1
        response = client.post("/chat", json=dict(text=redo))
        assert response.status_code == 200 and calls["digest"] == 2
        assert stats(client)["llm.synthesize"]["refreshed"] == 1
        # 重做只管这一轮：下一次普通提问读到的是刚写入的最新结果，不再调用
        assert client.post("/chat", json=dict(text="帮我梳理周记的全部要点")).status_code == 200
        assert calls["digest"] == 2 and stats(client)["llm.synthesize"]["hits"] == 2


def test_handing_over_to_assistant_does_not_read_digest_cache(env):
    """“交给助理处理”是用户认为上一次不对：同样不读缓存。"""
    app, calls = digest_app(env)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.post("/chat", json=dict(text="帮我梳理周记的全部要点"))
        client.post("/chat", json=dict(text="帮我梳理周记的全部要点", skip_funnel=True))
        assert calls["digest"] == 2


def test_resending_after_retry_button_does_not_read_cache(env):
    """网页“修改要求后重试”原样重发时带 fresh：同一句话不读缓存，结果仍写入；下一次普通提问读到新结果。"""
    app, calls = digest_app(env)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        client.post("/chat", json=dict(text="帮我梳理周记的全部要点"))
        client.post("/chat", json=dict(text="帮我梳理周记的全部要点"))
        assert calls["digest"] == 1
        assert client.post("/chat", json=dict(text="帮我梳理周记的全部要点", fresh=True)).status_code == 200
        assert calls["digest"] == 2
        client.post("/chat", json=dict(text="帮我梳理周记的全部要点"))
        assert calls["digest"] == 2


# ---- 单问捷径
def test_shortcut_repeat_with_redo_words_calls_model_again(tmp_path):
    """同一句话带“重新”时不读缓存；不带时才是普通重复，继续命中。"""
    from types import SimpleNamespace

    from briefly.db import create_db_engine
    from briefly.intent import IntentDecision
    from briefly.knowledge.assistant import KnowledgeAssistant
    from briefly.knowledge.index import LocalIndex
    from briefly.knowledge.lifecycle import IndexLifecycle
    from briefly.knowledge.service import KnowledgeService
    from briefly.llm_cache import ResponseCache, bind_scope, release_scope
    from briefly.materials import MaterialStore
    from test_knowledge_retrieval import add, setup

    engine = create_db_engine("sqlite:///" + (tmp_path / "app.db").as_posix())
    token = bind_scope(ResponseCache(engine), "local")
    try:
        store, svc, lifecycle, retrieval = setup((engine, MaterialStore(engine), KnowledgeService(engine),
                                                  IndexLifecycle(engine, LocalIndex(tmp_path / "index.db"))))
        kb = svc.create("制度库")
        add(store, svc, lifecycle, kb, "休假制度", "员工年假为每年5天。")
        svc.set_egress(kb.id, True)
        model = FunctionModel(respond=lambda m: AIMessage("【可答性：可回答】年假5天〔依据1〕。"), calls=[])
        funnel = SimpleNamespace(classify=lambda *a, **k: IntentDecision("knowledge", 2, "测试"))
        agent = Agent(model, tools=[], funnel=funnel, knowledge=KnowledgeAssistant(retrieval, lambda: model))
        for _ in range(2):
            agent.chat("年假有几天", kb_id=kb.id)
        assert len(model.calls) == 1
        for expected in (2, 3):
            agent.chat("重新查一下年假有几天", kb_id=kb.id)
            assert len(model.calls) == expected
        # /chat 的 fresh 就是这个上下文：同一句话原样重发也不读缓存
        agent.chat("年假有几天", kb_id=kb.id)
        assert len(model.calls) == 3
        with fresh_model_calls():
            agent.chat("年假有几天", kb_id=kb.id)
        assert len(model.calls) == 4
    finally:
        release_scope(token)
        engine.dispose()
