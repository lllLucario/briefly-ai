"""F12–F14：跨工具重写限额、失败历史闭合和数字提示。"""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage

from briefly.agent import Agent, sqlite_checkpointer
from briefly.api import create_app, visible_messages
from briefly.db import create_db_engine
from briefly.materials import MaterialStore
from briefly.number_check import number_issues
from fakes import FunctionModel


@pytest.fixture
def store():
    engine = create_db_engine("sqlite://")
    yield MaterialStore(engine)
    engine.dispose()


def test_retry_budget_and_hint_survive_tool_loop_and_reset_next_turn(store):
    responses = iter([
        AIMessage("见第2块。"),
        AIMessage("", tool_calls=[{"name": "get_current_date", "args": {}, "id": "clock"}]),
        AIMessage("仍见第2块。"),
        AIMessage("见第3块。"), AIMessage("正确回答。"),
    ])
    model = FunctionModel(respond=lambda _: next(responses), calls=[])
    agent = Agent(model, store=store)
    assert agent.chat("总结") == "仍见第2块。"
    assert len(model.calls) == 3
    assert "本轮纠正要求" in model.calls[2][0].text
    assert "见第2块" in model.calls[2][0].text
    assert agent.history[-1].response_metadata["internal_terms"]
    assert not any(m.additional_kwargs.get("internal") for m in agent.history)
    assert agent.chat("再总结") == "正确回答。"
    assert len(model.calls) == 5


def test_failed_new_session_is_reachable_and_retry_keeps_paired_history(store):
    state = {"fail": True}
    def respond(messages):
        if state["fail"]:
            raise RuntimeError("模拟接口失败")
        assert any("本轮调用失败" in m.text for m in messages)
        return AIMessage("恢复后的回答")
    agent = Agent(FunctionModel(respond=respond, calls=[]), store=store)
    with TestClient(create_app(store, agent_factory=lambda _: agent)) as client:
        failed = client.post("/chat", json={"text": "总结"})
        assert failed.status_code == 502
        thread = failed.json()["thread_id"]
        detail = client.get(f"/chat/sessions/{thread}").json()["messages"]
        assert len(detail) == 2 and detail[-1]["system_reply"]
        assert "本轮调用失败" in detail[-1]["text"]
        assert agent.graph.get_state({"configurable": {"thread_id": thread}}).next == ()
        state["fail"] = False
        assert client.post("/chat", json={"thread_id": thread, "text": "总结"}).status_code == 200
        history = client.get(f"/chat/sessions/{thread}").json()["messages"]
        assert [m["role"] for m in history] == ["user", "assistant", "user", "assistant"]


def test_failure_after_successful_tool_result_does_not_reexecute_it(store):
    state = {"n": 0}
    def respond(messages):
        state["n"] += 1
        if state["n"] == 1:
            return AIMessage("", tool_calls=[{"name": "list_materials", "args": {}, "id": "list"}])
        if state["n"] == 2:
            raise RuntimeError("读取后断线")
        return AIMessage("正常回答")
    agent = Agent(FunctionModel(respond=respond, calls=[]), store=store)
    with pytest.raises(RuntimeError):
        agent.chat("列出材料")
    assert visible_messages(agent.history)[-1].system_reply
    agent.chat("再试")
    assert sum(len(m.tool_calls) for m in agent.history if isinstance(m, AIMessage)) == 1


def test_failed_turn_stays_closed_after_reopening_sqlite_checkpoint(store, tmp_path):
    path = tmp_path / "chat.db"
    saver = sqlite_checkpointer(path)
    def fail(_):
        raise RuntimeError("模拟断线")
    try:
        agent = Agent(FunctionModel(respond=fail, calls=[]), store=store, checkpointer=saver, thread_id="failed-thread")
        with pytest.raises(RuntimeError):
            agent.chat("原始输入")
    finally:
        saver.conn.close()
    reopened = sqlite_checkpointer(path)
    try:
        agent = Agent(FunctionModel(respond=lambda _: AIMessage("新回答"), calls=[]), store=store, checkpointer=reopened, thread_id="failed-thread")
        assert visible_messages(agent.history)[-1].system_reply
        assert agent.graph.get_state({"configurable": {"thread_id": "failed-thread"}}).next == ()
        assert agent.chat("重试") == "新回答"
        assert len(visible_messages(agent.history)) == 4
    finally:
        reopened.conn.close()


def test_numbers_only_warn_without_model_retry(store):
    model = FunctionModel(respond=lambda _: AIMessage("预算为99万元，完成率88%。"), calls=[])
    agent = Agent(model, tools=[], store=store)
    agent.chat("原文：预算为5万元，完成率80%。")
    assert len(model.calls) == 1
    assert agent.history[-1].response_metadata["number_issues"] == ["99", "88"]
    assert any("数字" in w for w in visible_messages(agent.history)[-1].warnings)


def test_number_formats_known_dates_and_ordinals_do_not_warn():
    now = datetime(2026, 10, 22, tzinfo=timezone.utc)
    assert number_issues("1. 金额12000元\n2. 时间2026-10-23，今天2026-10-22。", ["金额1.2万元，时间2026年10月23日。"], now) == []
    assert number_issues("金额99万元", ["预算5万元"], now) == ["99"]
    assert number_issues("执行顺序：1. 开发 2. 测试 3. 上线 4. 回归 5. 复盘", [], now) == []
    assert number_issues("1. 金额1.20万元", ["金额1.2万元"], now) == []
    assert number_issues("金额12000万元", ["金额1.2万元"], now) == ["12000"]
    assert number_issues("数量5个", ["原始数量25个，预算12.5万元"], now) == ["5"]
    assert number_issues("下周三（10-28）", ["下周三提交"], now) == []
    assert number_issues("交付日期10-28", ["下周三提交"], now) == ["10-28"]
    assert number_issues("2天后整理35条", ["两天后整理三十五条"], now) == []
