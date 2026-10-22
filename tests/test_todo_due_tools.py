"""F10：到期判断使用请求时间，列表和详情一致；不调用真实模型。"""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from briefly import clock
from briefly.agent import Agent
from briefly.db import create_db_engine
from briefly.materials import MaterialStore
from briefly.todos.service import TodoService
from briefly.tools.business import business_tools
from fakes import FunctionModel


@pytest.fixture
def env():
    engine = create_db_engine("sqlite://")
    store = MaterialStore(engine)
    yield store, TodoService(engine, store), next(t for t in business_tools(store) if t.name == "read_todos")
    engine.dispose()


def runtime(now):
    return SimpleNamespace(context=SimpleNamespace(now=now))


@pytest.mark.parametrize("offset,status,label", [
    (-2, "todo", "已逾期 2 天"),
    (-1, "doing", "已逾期 1 天"),
    (0, "todo", "今天到期，本周内到期"),
    (3, "doing", "3 天后到期，本周内到期"),
    (4, "todo", "4 天后到期"),
    (None, "todo", "无期限"),
    (-3, "done", "已完成，不再标记逾期"),
    (3, "done", "已完成，不再标记逾期"),
])
def test_list_and_detail_share_program_due_labels(env, offset, status, label):
    _, todos, read = env
    now = datetime(2026, 10, 22, 23, 59, tzinfo=ZoneInfo("Australia/Sydney"))
    due = now.date() + timedelta(days=offset) if offset is not None else None
    todo = todos.create("接口开发", due_date=due)
    if status != "todo":
        todos.set_status(todo.id, status)
    listing = read.func(runtime=runtime(now))
    detail = read.func(todo_id=todo.id, runtime=runtime(now))
    for result in (listing, detail):
        assert f"到期情况：{label}" in result
        assert "2026-10-22" in result
        assert {"todo": "未开始", "doing": "进行中", "done": "已完成"}[status] in result
        assert (due.isoformat() if due else "待定") in result
        assert f"：{status}" not in result
    if offset == 4:
        assert "本周内到期" not in listing


@pytest.mark.parametrize("now,due,label", [
    (datetime(2026, 12, 31, tzinfo=timezone.utc), date(2027, 1, 1), "1 天后到期，本周内到期"),
    (datetime(2026, 10, 25, tzinfo=timezone.utc), date(2026, 10, 26), "1 天后到期"),
    (datetime(2026, 10, 26, tzinfo=timezone.utc), date(2026, 11, 1), "6 天后到期，本周内到期"),
    (datetime(2026, 10, 4, 0, 1, tzinfo=ZoneInfo("Australia/Sydney")), date(2026, 10, 5), "1 天后到期"),
])
def test_week_and_year_boundaries_use_calendar_days(env, now, due, label):
    _, todos, read = env
    todos.create("边界任务", due_date=due)
    output = read.func(runtime=runtime(now))
    assert f"到期情况：{label}" in output
    if "本周内到期" not in label:
        assert "本周内到期" not in output


def test_agent_injects_request_time_even_when_clock_passes_midnight(env, monkeypatch):
    store, todos, read = env
    todos.create("今晚提交", due_date=date(2026, 10, 22))
    calls = []
    def now():
        calls.append(1)
        return datetime(2026, 10, 22 if len(calls) == 1 else 23, 23, 59, tzinfo=ZoneInfo("Australia/Sydney"))
    monkeypatch.setattr(clock, "now_local", now)
    def respond(messages):
        if isinstance(messages[-1], ToolMessage):
            assert "今天到期" in messages[-1].text
            assert "已逾期" not in messages[-1].text
            return AIMessage("今晚提交今天到期。")
        return AIMessage("", tool_calls=[{"name": "read_todos", "args": {}, "id": "todos"}])
    model = FunctionModel(respond=respond, calls=[])
    Agent(model, tools=[], store=store).chat("哪些待办今天到期")
    assert len(calls) == 1
    assert "runtime" not in read.tool_call_schema.model_json_schema().get("properties", {})


def test_direct_calls_fall_back_to_configured_clock_and_keep_pagination(env, monkeypatch):
    _, todos, read = env
    monkeypatch.setattr(clock, "now_local", lambda: datetime(2026, 10, 26, 0, 30, tzinfo=ZoneInfo("Australia/Sydney")))
    for i in range(21):
        todos.create(f"任务{i:02}", description="原文" * 1600, due_date=date(2026, 10, 26))
    archived = todos.create("已归档的任务", due_date=date(2026, 10, 20))
    todos.archive(archived.id)
    first = read.invoke({})
    assert first.count("今天到期") == 20 and "offset=20" in first
    assert "已归档的任务" not in first
    last = read.invoke({"offset": 20})
    assert "任务20" in last and last.count("今天到期") == 1
    assert "还有更多" not in last
    detail = read.invoke({"todo_id": 1, "page": 2})
    assert "第2/2页" in detail and "今天到期" in detail
    assert "2026-10-26" in detail
