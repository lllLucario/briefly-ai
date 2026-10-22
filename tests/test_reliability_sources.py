"""审计 F1/F7：依据与可访问范围分别处理，不调用真实模型。"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from briefly import clock
from briefly.agent import Agent
from briefly.db import create_db_engine
from briefly.materials import MaterialStore
from briefly.sources import conversation_material_ids
from fakes import FunctionModel


@pytest.fixture
def store():
    engine = create_db_engine("sqlite://")
    yield MaterialStore(engine)
    engine.dispose()


def test_unread_library_cannot_exempt_wrong_date_or_internal_terms(store, monkeypatch):
    monkeypatch.setattr(clock, "now_local", lambda: datetime(2026, 10, 22, tzinfo=timezone.utc))
    store.add_text("无关记录", "2026年10月14日，第3块地的记录。")
    answers = iter([AIMessage("下周三（10-27），见第3块。"), AIMessage("下周三（10-28）。")])
    model = FunctionModel(respond=lambda _: next(answers), calls=[])
    agent = Agent(model, tools=[], store=store)
    assert agent.chat("下周的会安排一下") == "下周三（10-28）。"
    assert len(model.calls) == 2
    assert "日期不对" in model.calls[-1][-1].text
    assert "第3块" in model.calls[-1][-1].text


def test_only_conversation_materials_are_loaded_once_per_final_check(store, monkeypatch):
    used, _ = store.add_text("相关原文", "第3块地种了玉米。")
    store.add_text("无关", "第4块地休耕。")
    calls = []
    original = store.get
    monkeypatch.setattr(store, "get", lambda mid: (calls.append(mid), original(mid))[1])
    agent = Agent(FunctionModel(respond=lambda _: AIMessage("第3块地种了玉米。"), calls=[]), tools=[], store=store)
    request = SimpleNamespace(messages=[HumanMessage("总结"), AIMessage("", tool_calls=[{"name": "read_material", "args": {"material_id": used.id}, "id": "r"}]), ToolMessage("第3块地种了玉米。", tool_call_id="r")])
    assert "第3块地种了玉米。" in agent._check_sources(request)
    assert calls == [used.id]
    calls.clear()
    agent.chat("总结一下")
    assert calls == []


def test_shared_collector_handles_artifact_sources_and_selection():
    messages = [HumanMessage("总结", additional_kwargs={"material_ids": [1]}), ToolMessage("内容", tool_call_id="x", artifact={"material_ids": [2], "items": [{"source": "3-1"}, {"source": "说明"}]})]
    assert conversation_material_ids(messages) == [1, 2, 3]


@pytest.mark.parametrize("selected", [False, True])
def test_long_input_preserves_unrestricted_scope_and_explicit_selection(store, selected):
    old, _ = store.add_text("以前的项目说明", "这里是原来的项目。")
    other, _ = store.add_text("其他材料", "另一份记录。")
    def respond(messages):
        if isinstance(messages[-1], ToolMessage):
            text = messages[-1].text
            assert "以前的项目说明" in text
            assert ("其他材料" in text) == (not selected)
            return AIMessage("已找到相关材料。")
        return AIMessage("", tool_calls=[{"name": "list_materials", "args": {}, "id": "list"}])
    agent = Agent(FunctionModel(respond=respond, calls=[]), store=store)
    agent.chat("对比之前的项目。" + "长文本内容。" * 4000, material_ids=[old.id] if selected else None)
    assert other.id not in agent.history[0].additional_kwargs["material_ids"]
