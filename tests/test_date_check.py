"""回答里“具体日期 + 相对说法”的换算核对：不依赖模型是否调用 resolve_date。不调用真实模型。"""
from datetime import date

import pytest
from langchain_core.messages import AIMessage

from briefly.agent import Agent
from briefly.date_check import date_problems
from briefly.db import create_db_engine
from briefly.materials import MaterialStore
from fakes import FunctionModel

TODAY = date(2026, 10, 22)  # 星期四
MEETING = "10月14日下午3点 产品周会\n李强：下周三前能提测。\n王敏：那上线就定10月20日。\n李强：我下周一给工作量评估。"


@pytest.mark.parametrize("answer", [
    "| 提测 | 李强 | 10-21（下周三） |",
    "| 评估 | 李强 | 下周一（2026-10-19） |",  # “下周一”前面最近的日期是上线日 10 月 20 日，但会议日期也是基准
    "提测：下周三前（会议日期10月14日，即10-21）",  # 括号里先说明基准的日期不算换算结果
    "| 压测报告 | 赵琳 | 10-18（原文表述） |",  # 没有相对说法
    "| 回归 | 赵琳 | 10-23（提测后两天） |",  # 相对事件，不核对
])
def test_correct_or_unverifiable_dates_pass(answer):
    assert date_problems(answer, [MEETING], TODAY) == []


@pytest.mark.parametrize(("answer", "expression"), [
    ("| 提测 | 李强 | 10-22（下周三） |", "下周三"),  # 2026-10-22 实测：模型心算错一天
    ("| 评估 | 李强 | 下周一（10-20） |", "下周一"),
    ("| 提测 | 李强 | 下周三（2025-10-22） |", "下周三"),  # 自补了错误年份
    ("| 提测 | 李强 | 10-28（下周三，按今天推算） |", "下周三"),  # 会议里的相对时间不能按今天换算
    ("会议日期：10月14日（周二）", "周二"),  # 星期本身写错
])
def test_wrong_conversions_are_reported(answer, expression):
    [problem] = date_problems(answer, [MEETING], TODAY)
    assert f"“{expression}”" in problem


def test_without_material_date_today_is_the_base():
    task = "下周五前要把季度汇报PPT发给张总"
    assert date_problems("| 发PPT | 待定 | 10-30（下周五） |", [task], TODAY) == []
    assert date_problems("| 发PPT | 待定 | 10-23（下周五） |", [task], TODAY)
    assert date_problems("10-23（明天）", ["明天上午跟小王对一下接口文档"], TODAY) == []


def scripted(*answers):
    queue = list(answers)
    return FunctionModel(respond=lambda messages: AIMessage(queue.pop(0)), calls=[])


@pytest.fixture
def store():
    engine = create_db_engine("sqlite://")
    yield MaterialStore(engine)
    engine.dispose()


def test_wrong_date_is_rewritten_once_with_resolve_date_hint(store):
    m, _ = store.add_text("会议记录", MEETING)
    model = scripted("| 提测 | 李强 | 10-22（下周三） |", "| 提测 | 李强 | 10-21（下周三） |")
    agent = Agent(model, tools=[], store=store)
    assert agent.chat("列出待办", material_ids=[m.id]) == "| 提测 | 李强 | 10-21（下周三） |"
    assert len(model.calls) == 2
    assert "resolve_date" in model.calls[1][-1].text and "10-22（下周三）" in model.calls[1][-1].text
    assert "date_issues" not in agent.history[-1].response_metadata


def test_still_wrong_after_retry_is_shown_to_the_user(store):
    from briefly.api import visible_messages

    m, _ = store.add_text("会议记录", MEETING)
    model = scripted("下周一（10-20）给评估", "仍是下周一（10-20）给评估")
    agent = Agent(model, tools=[], store=store)
    agent.chat("列出待办", material_ids=[m.id])
    assert len(model.calls) == 2  # 只重写一次
    assert agent.history[-1].response_metadata["date_issues"]
    [warning] = visible_messages(agent.history)[-1].warnings
    assert "日期换算可能有误" in warning and "下周一" in warning
