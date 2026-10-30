"""轮次结果记录（WP1）：resolved 与 result 按实际工具和产出写回，界面与路由读记录，旧消息不报错。"""
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from briefly import config
from briefly.agent import Agent, _last_intent, turn_state
from briefly.api import visible_messages
from briefly.db import create_db_engine
from briefly.materials import MaterialStore
from briefly.turn_record import turn_record
from fakes import fake, text_msg, tool_msg

TODO_TABLE = "可以加到待办的有：\n| 任务 | 负责人 | 截止时间 |\n|---|---|---|\n| 提交报销单 | 本人 | 10-31 |\n| 更新通讯录 | 待定 | 待定 |\n请用“预览并保存待办”确认后保存。"


@pytest.fixture
def store():
    engine = create_db_engine("sqlite://")
    yield MaterialStore(engine)
    engine.dispose()


def turn(history):
    human = next(m for m in reversed(history) if isinstance(m, HumanMessage) and not m.additional_kwargs.get("internal"))
    reply = history[-1]
    return human.additional_kwargs["intent"], reply.response_metadata["result"]


def test_business_tool_call_is_recorded_as_actual_capability(store):
    agent = Agent(fake(tool_msg("read_todos", {}), text_msg("目前没有未完成的待办。")), store=store)
    agent.chat("我还有哪些待办没做完")
    intent, result = turn(agent.history)
    assert intent["resolved"] == "business_query"
    assert intent["intent"] == agent.funnel.classify("我还有哪些待办没做完").intent  # 漏斗原判断保留，不被改写
    assert result == {"kinds": [], "items": []}
    assert _last_intent(agent.history) == "business_query"
    assert turn_state(agent.history, None, None).last_intent == "business_query"


def test_unmarked_todo_table_gets_todo_kind_and_item_ids(store):
    agent = Agent(fake(text_msg(TODO_TABLE)), store=store)
    agent.chat("把里面要我自己做的几条加到我的待办")
    intent, result = turn(agent.history)
    reply = agent.history[-1]
    assert intent["resolved"] == "todo"
    assert result["kinds"] == ["todo"]
    assert result["items"] == [{"index": 1, "id": f"{reply.id}:todo:1", "kind": "todo"},
                               {"index": 2, "id": f"{reply.id}:todo:2", "kind": "todo"}]
    assert visible_messages(agent.history)[-1].result_kinds == ["todo"]  # 网页按记录给出“预览并保存待办”


def test_minutes_with_action_items_records_both_kinds_and_explicit_skill_decides(store):
    text = "【会议信息】例会\n【行动项】\n| 事项 | 负责人 | 截止时间 |\n|---|---|---|\n| 出设计稿 | 张三 | 周五 |\n【待办清单】\n- 出设计稿\n【遗留问题】无"
    agent = Agent(fake(text_msg(text)), store=store)
    agent.chat("整理纪要并列出待办", skill="minutes")
    intent, result = turn(agent.history)
    assert result["kinds"] == ["minutes", "todo"] and intent["resolved"] == "minutes"
    assert [(i["kind"], i["index"]) for i in result["items"]] == [("action_item", 1), ("todo", 1)]


def test_system_reply_records_nothing(store, monkeypatch):
    monkeypatch.setattr(config, "MAX_TOOL_ROUNDS", 1)
    agent = Agent(fake(tool_msg("read_todos", {}), tool_msg("read_todos", {}, id="t2")), store=store)
    agent.chat("我还有哪些待办")
    intent, result = turn(agent.history)
    assert intent["resolved"] is None and result == {"kinds": [], "items": []}
    assert visible_messages(agent.history)[-1].system_reply


def test_direct_reply_records_funnel_handler_as_resolved(store):
    agent = Agent(fake(), store=store)
    agent.chat("你好")
    intent, result = turn(agent.history)
    assert intent["resolved"] == intent["intent"] == "greeting"
    assert result == {"kinds": [], "items": []}


def test_old_messages_without_record_are_unknown_and_fall_back_to_sections():
    old = [HumanMessage("整理待办", id="u", additional_kwargs={"intent": {"intent": "todo"}}),
           AIMessage("【待办清单】\n| 任务 | 负责人 |\n|---|---|\n| 发报价 | 待定 |", id="a")]
    assert _last_intent(old) is None  # 没有 resolved：未知，不回退到漏斗当时的判断
    assert _last_intent([HumanMessage("旧消息")]) is None
    assert visible_messages(old)[-1].result_kinds == ["todo"]
    # 有记录时以记录为准，不再看栏目
    recorded = [old[0], AIMessage(old[1].text, id="a", response_metadata={"result": {"kinds": [], "items": []}})]
    assert visible_messages(recorded)[-1].result_kinds == []


@pytest.mark.parametrize(("text", "kwargs", "resolved", "kinds", "count"), [
    ("根据制度：\n| 事项 | 负责人 |\n|---|---|\n| 审批 | 部门经理 |", dict(tools=["search_knowledge"]), "knowledge", [], 0),
    ("- 提交报销单\n- 更新通讯录", dict(intent={"intent": "business_action"}, user_text="把这两条加到待办"), "todo", ["todo"], 2),
    ("- 接口联调完成\n- 测试记录已整理", dict(intent={"intent": "business_action"}, user_text="把这段写进周报"), None, [], 0),
    ("【优化后】已润色\n【主要修改】措辞", {}, "polish", ["polish"], 0),
    ("【待办清单】无\n【待确认】无", {}, "todo", ["todo"], 0),
    ("请补充会议记录。", dict(skill="minutes"), None, [], 0),
    ("【一句话总结】x\n【周报标题】y", dict(intent={"intent": "agent"}), None, ["summarize", "weekly"], 0),
])
def test_capability_table(text, kwargs, resolved, kinds, count):
    got, result = turn_record(text, **{"intent": None, "skill": None, "tools": [], **kwargs})
    assert got == resolved and result["kinds"] == kinds and len(result["items"]) == count
