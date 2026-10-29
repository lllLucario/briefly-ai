"""请求级时间：每次请求取一次带时区的当前时间，本轮提示词、日期工具、日期校验共用；周报“本周”按配置时区。不调用真实模型。"""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from langchain_core.messages import AIMessage, SystemMessage

from briefly import clock, config
from briefly.agent import Agent
from briefly.date_check import review_dates
from briefly.db import create_db_engine
from briefly.materials import MaterialStore
from briefly.tools.builtin import get_current_date
from fakes import FunctionModel, fake, text_msg, tool_msg

SYDNEY = ZoneInfo("Australia/Sydney")


@pytest.fixture
def store():
    engine = create_db_engine("sqlite://")
    yield MaterialStore(engine)
    engine.dispose()


@pytest.fixture
def sydney(monkeypatch):
    monkeypatch.setattr(config, "TRACKING_TIMEZONE", "Australia/Sydney")


def clock_at(monkeypatch, *moments):
    """依次返回给定时间的时钟（模拟跨午夜）；记录被调用的次数。"""
    queue = list(moments)
    calls = []

    def now():
        calls.append(1)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    monkeypatch.setattr(clock, "now_local", now)
    return calls


def system_prompts(model) -> list[str]:
    return [m.content for call in model.calls for m in call if isinstance(m, SystemMessage)]


def recording(*answers):
    queue = list(answers)
    return FunctionModel(respond=lambda messages: queue.pop(0), calls=[])


def test_time_context_is_injected_without_extra_calls_or_history(store, sydney, monkeypatch):
    clock_at(monkeypatch, datetime(2026, 10, 22, 18, 22, tzinfo=SYDNEY))
    model = recording(AIMessage("你好，有什么可以帮你？"))
    agent = Agent(model, tools=[], store=store)
    agent.chat("帮我看看")
    assert len(model.calls) == 1  # 普通对话不增加模型调用
    [prompt] = system_prompts(model)
    assert "Current datetime: 2026-10-22T18:22:00+11:00" in prompt
    assert "Current date: 2026-10-22（星期四）" in prompt and "Current weekday: Thursday" in prompt
    assert "Timezone: Australia/Sydney" in prompt
    assert all("Current datetime" not in m.text for m in agent.history)  # 不写进会话历史


def test_each_request_gets_fresh_time_across_midnight(store, sydney, monkeypatch):
    clock_at(monkeypatch, datetime(2026, 10, 22, 23, 59, tzinfo=SYDNEY), datetime(2026, 10, 23, 0, 1, tzinfo=SYDNEY))
    model = recording(AIMessage("一"), AIMessage("二"))
    agent = Agent(model, tools=[], store=store)
    agent.chat("第一问")
    agent.chat("第二问")
    first, second = system_prompts(model)
    assert "Current date: 2026-10-22" in first and "Current date: 2026-10-23" in second


def test_one_turn_shares_one_time_even_if_the_clock_passes_midnight(store, sydney, monkeypatch):
    calls = clock_at(monkeypatch, datetime(2026, 10, 22, 23, 59, 59, tzinfo=SYDNEY), datetime(2026, 10, 23, 0, 0, 5, tzinfo=SYDNEY))
    model = fake(tool_msg("get_current_date", {}), text_msg("今天是 10-22"))
    agent = Agent(model, tools=[get_current_date], store=store)
    agent.chat("今天几号？")
    tool_result = next(m for m in agent.history if m.type == "tool")
    assert tool_result.content == "2026-10-22 星期四"  # 工具用的是本次请求开始时的日期
    assert len(calls) == 1  # 本轮只取一次时间


def test_dst_and_unconfigured_timezone_labels(monkeypatch):
    before = clock.RequestTime(datetime(2026, 10, 3, 12, tzinfo=SYDNEY))
    after = clock.RequestTime(datetime(2026, 10, 5, 12, tzinfo=SYDNEY))
    assert "+10:00" in before.prompt() and "+11:00" in after.prompt()  # 夏令时切换
    monkeypatch.setattr(config, "TRACKING_TIMEZONE", "")
    label = clock.tz_label(datetime(2026, 10, 22, 12, tzinfo=timezone(timedelta(hours=8))))
    assert label == "系统本地时区（UTC+08:00）"  # 未配置时不假定任何地区


def test_weekly_current_week_uses_configured_timezone(sydney, monkeypatch):
    from briefly.weekly.engine import current_week
    from briefly.weekly.service import week_of

    # 悉尼已是周一 10-26 凌晨，而 UTC 仍是周日 10-25
    clock_at(monkeypatch, datetime(2026, 10, 25, 14, 30, tzinfo=timezone.utc).astimezone(SYDNEY))
    assert week_of() == current_week() == (date(2026, 10, 26), date(2026, 11, 1))
    assert week_of(date(2026, 12, 31)) == (date(2026, 12, 28), date(2027, 1, 3))  # 跨年


def test_ambiguous_base_is_counted_not_corrected():
    sources = ["周报记录：10月14日完成联调；10月20日上线。", "请整理成待办"]
    review = review_dates("| 复盘 | 待定 | 10-29（下周二） |", sources, date(2026, 10, 22))
    # “下周二”原文没有出现，基准可能是今天或原文任一日期，且都对不上：不确定，不判错
    assert review.problems == [] and review.uncertain == 1 and review.checked == 1


def test_weekday_label_is_checked_without_any_base():
    review = review_dates("会议日期：10月14日（周二）", [], date(2026, 10, 22))
    assert review.problems == ["“周二”写成了 10月14日（周二）"]
    assert review_dates("会议日期：10月14日（周三）", [], date(2026, 10, 22)).problems == []
