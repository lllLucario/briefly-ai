"""Bug 2：待办被写进【问题与风险】。分类在提取阶段由结构化输出决定，问题风险要有原文依据，写完后由代码检查。不调用真实模型。"""
from datetime import date

import pytest
from langchain_core.messages import AIMessage

from briefly.db import create_db_engine
from briefly.materials import MaterialStore
from briefly.summarize import Source, WorkItem, extract_items, format_items
from briefly.weekly.engine import report_check
from briefly.weekly.service import WeeklyService
from fakes import FunctionModel, structured
from weekly_util import draft

LOG = (
    "## 周三\n确认上线时间定为 10 月 20 日，上线前需要补齐压测报告。\n"
    "## 周四\n下周需要完成接口测试，但测试环境尚未恢复，可能影响上线。\n"
)
PLAN = {"category": "plan", "point": "上线前补齐压测报告", "quote": "确认上线时间定为 10 月 20 日，上线前需要补齐压测报告。"}
TODO = {"category": "plan", "point": "下周完成接口测试", "quote": "下周需要完成接口测试，但测试环境尚未恢复，可能影响上线。"}
RISK = {
    "category": "issue",
    "point": "测试环境未恢复，可能影响上线",
    "quote": "下周需要完成接口测试，但测试环境尚未恢复，可能影响上线。",
    "risk_reason": "测试环境尚未恢复，可能影响上线",
}


@pytest.fixture
def store():
    engine = create_db_engine("sqlite://")
    yield MaterialStore(engine)
    engine.dispose()


def extract(*items):
    model = FunctionModel(respond=lambda messages: structured("Extraction", items=list(items)), calls=[])
    return extract_items(model, [Source(1, 1, "周报", LOG)]).items


def test_risk_needs_reason_found_in_original():
    no_reason = {**RISK, "risk_reason": ""}
    made_up = {**RISK, "risk_reason": "数据库容量不足"}  # 原文没有
    items = extract(RISK, no_reason, made_up)
    assert [i.needs_review for i in items] == [False, True, True]
    assert items[0].verified_risk and not items[1].verified_risk
    assert extract(PLAN)[0].needs_review is False  # 非问题类不需要依据


def test_format_marks_review_reason_and_related_items():
    items = [WorkItem("plan", "下周完成接口测试", TODO["quote"], "1-1"), WorkItem("issue", RISK["point"], RISK["quote"], "1-1", RISK["risk_reason"])]
    items.append(WorkItem("issue", "数据库可能有问题", "数据库连接偶尔慢", "1-2", "", True))
    text = format_items(items)
    assert "#1 【计划】下周完成接口测试" in text
    assert "#2 【问题风险】" in text and "风险依据：测试环境尚未恢复，可能影响上线" in text
    assert "（与 #1 出自同一句原文，但是另一项内容，同样要写进周报" in text  # 复合事实拆成两条关联记录
    assert "#3 【待复核】" in text and text.count("风险依据") == 1


@pytest.mark.parametrize(
    ("line", "ok"),
    [
        ("- 测试环境尚未恢复，可能影响上线〔#2〕", True),
        ("- 上线前需补齐压测报告〔#1〕", False),  # 待办写进风险
        ("- 数据库可能有问题〔#3〕", False),  # 待复核的不能写
        ("- 接口测试与测试环境〔#1、#2〕", True),  # 引用了已核实的风险
        ("- 需要运维支持〔简述〕", True),
        ("无", True),
    ],
)
def test_risk_section_check(line, ok):
    items = [
        WorkItem("plan", "补齐压测报告", PLAN["quote"], "1-1"),
        WorkItem("issue", RISK["point"], RISK["quote"], "1-1", RISK["risk_reason"]),
        WorkItem("issue", "数据库可能有问题", "数据库连接偶尔慢", "1-2", "", True),
    ]
    text = f"【本周完成】\n- 无\n【问题与风险】\n{line}\n【下周计划】\n- 补齐压测报告〔#1〕\n\n【给用户的提醒】\n- 风险〔#1〕"
    problems = report_check(items)(text)
    assert (problems == []) is ok
    if not ok:
        assert "请移到对应栏目" in problems[0]


def weekly_with(store, reports: list[str], items: list[dict]):
    material, _ = store.add_text("周报素材", LOG)
    queue = list(reports)
    calls = []

    def respond(messages):
        if "撰写周报" in messages[0].content:
            calls.append(messages)
            return AIMessage(queue.pop(0))
        return structured("Extraction", items=items)

    weekly = WeeklyService(store, lambda: FunctionModel(respond=respond, calls=[]))
    report = draft(weekly, [material.id], "", date(2026, 10, 12), date(2026, 10, 18))
    weekly.run_generation(report.id)
    return weekly.report(report.id), calls, material


BAD = "【本周完成】\n无\n【进行中】\n无\n【问题与风险】\n- 上线前需补齐压测报告〔#1〕\n【下周计划】\n- 下周完成接口测试〔#2〕；测试环境未恢复〔#3〕"
GOOD = "【本周完成】\n无\n【进行中】\n无\n【问题与风险】\n- 测试环境尚未恢复，可能影响上线〔#3〕\n【下周计划】\n- 上线前补齐压测报告〔#1〕\n- 下周完成接口测试〔#2〕"


def test_todo_in_risk_section_is_rewritten(store):
    done, calls, material = weekly_with(store, [BAD, GOOD], [PLAN, TODO, RISK])
    assert done.status == "done", done.error
    assert len(calls) == 2  # 检查不通过，带着问题重写一次
    assert "请移到对应栏目" in calls[1][-1].content and "#1（计划）" in calls[1][-1].content
    risk = done.content.split("【问题与风险】")[1].split("【下周计划】")[0]
    assert "压测报告" not in risk and "测试环境" in risk
    assert done.generation["warnings"] == []


def test_still_wrong_after_rewrite_is_warned(store):
    done, calls, _ = weekly_with(store, [BAD, BAD], [PLAN, TODO, RISK])
    assert len(calls) == 2
    assert any("不是已核实的问题或风险" in w and "请核对" in w for w in done.generation["warnings"])


def test_unsupported_risk_goes_to_reminder_not_risk_section(store):
    vague = {"category": "issue", "point": "压测报告可能有风险", "quote": PLAN["quote"], "risk_reason": ""}
    report = "【本周完成】\n无\n【问题与风险】\n无\n【下周计划】\n- 补齐压测报告〔#1〕"
    done, calls, material = weekly_with(store, [report], [PLAN, vague])
    assert len(calls) == 1
    reminders = done.generation["reminders"]
    assert any("可能是问题或风险，但原文没有写明" in r and "压测报告可能有风险" in r for r in reminders)
    assert not any("没有写进周报，如需要请补充：压测报告可能有风险" in r for r in reminders)  # 不重复提醒
    assert done.items[1]["needs_review"] is True


DONE_PART = {"category": "done", "point": "与李经理确认上线时间定为 10 月 20 日", "quote": PLAN["quote"]}
HALF = "【本周完成】\n无\n【问题与风险】\n无\n【下周计划】\n- 上线前补齐压测报告〔#2〕"
FULL = "【本周完成】\n- 与李经理确认 10 月 20 日上线〔#1〕\n【问题与风险】\n无\n【下周计划】\n- 上线前补齐压测报告〔#2〕"


def test_half_written_split_sentence_is_rewritten(store):
    done, calls, _ = weekly_with(store, [HALF, FULL], [DONE_PART, PLAN])
    assert len(calls) == 2
    assert "#1（与李经理确认上线时间定为 10 月 20 日）与 #2 出自同一句原文" in calls[1][-1].content
    assert "李经理" in done.content and done.generation["reminders"] == []


def test_half_written_after_rewrite_is_reminded_not_warned(store):
    done, calls, _ = weekly_with(store, [HALF, HALF], [DONE_PART, PLAN])
    assert len(calls) == 2
    assert done.generation["warnings"] == []  # 漏写由输出覆盖列进提醒
    assert any("没有写进周报" in r and "李经理" in r for r in done.generation["reminders"])


def test_rewrite_keeps_reminders_from_first_draft(store):
    first = BAD + "\n\n【给用户的提醒】\n- 简述说已上线，材料写 10 月 20 日上线，请核对"
    done, calls, _ = weekly_with(store, [first, GOOD], [PLAN, TODO, RISK])  # 重写稿没有提醒
    assert "提醒部分也要保留" not in calls[1][-1].content and "【给用户的提醒】部分也要保留" in calls[1][-1].content
    assert any("请核对" in r for r in done.generation["reminders"])
