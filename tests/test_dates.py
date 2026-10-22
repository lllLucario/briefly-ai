"""相对日期由程序换算（resolve_date）：材料里的“下周一”以材料日期为基准，认不出时保留原话。"""
from datetime import date

import pytest

from briefly.tools.builtin import parse_base_date, relative_date, resolve_date

MEETING = date(2026, 10, 14)  # 星期三


@pytest.mark.parametrize(("expression", "expected"), [
    ("下周一", date(2026, 10, 19)),  # 2026-10-22 实测：模型曾写成 10-20
    ("下周三前", date(2026, 10, 21)),
    ("本周五", date(2026, 10, 16)),
    ("周日", date(2026, 10, 18)),
    ("上周三", date(2026, 10, 7)),
    ("下下周二", date(2026, 10, 27)),
    ("下个星期五", date(2026, 10, 23)),
    ("明天", date(2026, 10, 15)),
    ("大后天", date(2026, 10, 17)),
    ("3天后", date(2026, 10, 17)),
    ("3天前", date(2026, 10, 11)),
    ("两周后", date(2026, 10, 28)),
    ("两周内", date(2026, 10, 28)),
    ("月底前", date(2026, 10, 31)),
    ("下个月5号", date(2026, 11, 5)),
    ("15号", date(2026, 10, 15)),
])
def test_relative_dates_from_material_date(expression, expected):
    assert relative_date(expression, MEETING) == expected


def test_month_end_and_next_month_cross_year():
    assert relative_date("下个月底", date(2026, 12, 3)) == date(2027, 1, 31)
    assert relative_date("下月5号", date(2026, 12, 3)) == date(2027, 1, 5)


@pytest.mark.parametrize("text", ["2026-10-14", "2026/10/14", "2026年10月14日", "10月14日", "10-14", " 10 月 14 日 "])
def test_base_date_formats(text):
    assert parse_base_date(text, date(2026, 1, 1)) == MEETING


def test_tool_reports_base_and_keeps_unknown_expressions_verbatim():
    out = resolve_date.invoke({"expression": "下周一", "base_date": "10月14日"})
    assert "2026-10-19 星期一" in out and "基准日期 2026-10-14 星期三" in out
    assert "保留原话" in resolve_date.invoke({"expression": "等忙完这阵", "base_date": "10月14日"})
    assert "无法识别基准日期" in resolve_date.invoke({"expression": "下周一", "base_date": "上次开会那天"})
    # 不带“本/下”的周几早于基准日期：提示可能是下周，不擅自改成下周
    assert "原意可能是下周" in resolve_date.invoke({"expression": "周一", "base_date": "2026-10-14"})
    assert "（今天）" in resolve_date.invoke({"expression": "明天"})
    # 2026-10-22 实测：材料只写“10月14日”，模型自行补成 2025 年，算出错误星期
    assert "不是今年" in resolve_date.invoke({"expression": "下周一", "base_date": f"{date.today().year - 1}-10-14"})
    assert "不是今年" not in resolve_date.invoke({"expression": "下周一", "base_date": "10月14日"})


def test_agent_has_date_tools():
    from briefly.tools import DEFAULT_TOOLS

    assert {t.name for t in DEFAULT_TOOLS} >= {"get_current_date", "resolve_date"}
