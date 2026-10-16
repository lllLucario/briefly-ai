"""简述不再是排他过滤、简述与材料冲突（Phase 1），冲突检测的覆盖率与准确性（Phase 2）。确定性逻辑，假模型。"""
from datetime import date
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from briefly.conflicts import (
    NOT_SAME,
    UNRESOLVED,
    brief_conflict_problems,
    classify,
    compare_values,
    detect_conflicts,
    describe,
    value_in,
)
from briefly.db import create_db_engine
from briefly.materials import MaterialStore
from briefly.summarize import WorkItem
from briefly.weekly.engine import REMINDER_HEADING
from briefly.weekly.service import WeeklyService
from fakes import FunctionModel, structured
from weekly_util import draft

PROMPTS = Path(__file__).resolve().parents[1] / "src" / "briefly" / "prompts"


def fact(value, source="1-1", subject="支付改造", attribute="date", aspect="上线", k=1, quote=None):
    return WorkItem("other", f"{subject}{aspect} {value}", quote or f"{subject}{aspect}时间为 {value}。", source,
                    fact_id=f"{source}.{k}", subject=subject, attribute=attribute, value=value, aspect=aspect)


def brief_fact(value, **kw):
    return fact(value, source="简述", **kw)


# ---------- Phase 1：简述是重点，不是过滤条件
def test_extract_prompt_no_longer_filters_by_brief():
    text = (PROMPTS / "extract.md").read_text(encoding="utf-8")
    assert "一律不提取" not in text  # 回归：旧规则“与简述无关的一律不提取”
    assert "不是筛选条件" in text and "不要因为简述没提到就不提取" in text


@pytest.mark.parametrize(
    ("value_b", "value_m", "pairs"),
    [
        ("10 月 25 日", "10 月 20 日", ["brief_vs_material"]),  # 冲突
        ("10月20日", "10 月 20 日", []),  # 非冲突：写法不同、值相同
    ],
)
def test_brief_facts_join_conflict_detection(value_b, value_m, pairs):
    conflicts = detect_conflicts([fact(value_m)], [brief_fact(value_b)])
    assert [c.pairs for c in conflicts] == ([pairs] if pairs else [])
    if pairs:
        c = conflicts[0]
        assert c.numbers == [1, 0] and c.origins == ["material", "brief"] and c.sources == ["1-1", "简述"]
        assert "简述写的是 10 月 25 日" in describe(conflicts, [fact(value_m)]) and "正文只采用其中一种说法" in describe(conflicts, [fact(value_m)])


def test_mixed_sources_keep_both_rules():
    items = [fact("10 月 20 日", "1-1"), fact("10 月 22 日", "2-1")]
    [c] = detect_conflicts(items, [brief_fact("10 月 25 日")])
    assert c.pairs == ["brief_vs_material", "material_vs_material"]
    text = describe([c], items)
    assert "正文同时写出各材料的说法" in text and "正文只采用其中一种说法" in text


BRIEF_CONFLICT = None


def brief_conflict():
    [c] = detect_conflicts([fact("10 月 20 日")], [brief_fact("10 月 25 日")])
    return c


GOOD_BODY = "【本周完成】\n- 支付改造定于 10 月 20 日上线〔#1〕"
GOOD_REMINDER = f"\n\n{REMINDER_HEADING}\n- 简述写的是 10 月 25 日上线〔简述〕，材料〔#1〕写的是 10 月 20 日，请核对"


@pytest.mark.parametrize(
    ("text", "expect"),
    [
        (GOOD_BODY + GOOD_REMINDER, []),  # 正常：正文单方、出处对、提醒完整
        ("【本周完成】\n- 支付改造 10 月 25 日上线〔简述〕" + GOOD_REMINDER, []),  # 正常：正文采用简述
        ("【本周完成】\n- 支付改造上线时间：10 月 20 日〔#1〕/ 10 月 25 日〔简述〕" + GOOD_REMINDER, ["写进了同一条"]),  # 回归：两种说法同一条
        ("【本周完成】\n- 支付改造 10 月 25 日上线〔#1〕" + GOOD_REMINDER, ["没有标〔简述〕"]),  # 出处错：简述的说法标成材料
        ("【本周完成】\n- 支付改造 10 月 20 日上线〔简述〕" + GOOD_REMINDER, ["出处不是对应的材料编号"]),  # 出处错：材料的说法标成简述
        (GOOD_BODY, ["没有完整列出"]),  # 没有提醒
        (GOOD_BODY + f"\n\n{REMINDER_HEADING}\n- 上线时间请核对〔简述〕", ["没有完整列出"]),  # 提醒不完整：没有两种说法
        ("【本周完成】\n- 支付改造定于 10 月 20 日上线〔#1〕，简述写 10 月 25 日〔简述〕", ["写进了同一条", "没有完整列出"]),  # 正文全文有两个值也不算提醒
    ],
)
def test_brief_conflict_body_and_reminder_checked_separately(text, expect):
    problems = brief_conflict_problems([brief_conflict()], text.replace("〔简述〕", "〔简述〕"), REMINDER_HEADING)
    assert len(problems) == len(expect) and all(e in p for e, p in zip(expect, problems))


def test_resolved_brief_conflicts_are_not_checked():
    c = brief_conflict()
    c.resolution = "superseded"
    assert brief_conflict_problems([c], GOOD_BODY, REMINDER_HEADING) == []


def test_value_in_handles_formats():
    assert value_in("10月20日上线", "10 月 20 日") and value_in("定于 10 月 20 日", "10月20日")
    assert not value_in("10 月 2 日", "10 月 20 日")  # 边界：数字要完整匹配
    assert not value_in("110 月 20 日", "10 月 20 日")


def weekly_run(store, brief, reports):
    m, _ = store.add_text("排期", "支付改造上线时间定为 10 月 20 日。另外整理了需求池。")
    queue, calls, extract_users = list(reports), [], []

    def respond(messages):
        system, user = messages[0].content, messages[-1].content
        if "撰写周报" in system:
            calls.append(messages)
            return AIMessage(queue.pop(0))
        if "superseded" in system:
            return structured("_Judgments", judgments=[{"index": 1, "resolution": "unresolved", "explanation": "不一致"}])
        extract_users.append(user)
        if "用户简述" in user:
            return structured("Extraction", items=[{"category": "other", "point": "支付改造 10 月 25 日上线", "quote": brief, "subject": "支付改造", "attribute": "date", "aspect": "上线", "value": "10 月 25 日"}])
        return structured("Extraction", items=[
            {"category": "other", "point": "支付改造上线时间定为 10 月 20 日", "quote": "支付改造上线时间定为 10 月 20 日。", "subject": "支付改造", "attribute": "date", "aspect": "上线", "value": "10 月 20 日"},
            {"category": "done", "point": "整理了需求池", "quote": "另外整理了需求池。", "importance": "low"},
        ])

    weekly = WeeklyService(store, lambda: FunctionModel(respond=respond, calls=[]))
    report = draft(weekly, [m.id], brief, date(2026, 10, 12), date(2026, 10, 18))
    weekly.run_generation(report.id)
    return weekly.report(report.id), calls, extract_users


@pytest.fixture
def store():
    engine = create_db_engine("sqlite://")
    yield MaterialStore(engine)
    engine.dispose()


BOTH = "【本周完成】\n- 支付改造上线：10 月 20 日〔#1〕/ 10 月 25 日〔简述〕\n- 整理需求池〔#2〕"
ONE = "【本周完成】\n- 支付改造 10 月 25 日上线〔简述〕\n- 整理需求池〔#2〕" + GOOD_REMINDER


def test_weekly_brief_conflict_rewritten_and_reminded(store):
    done, calls, extract_users = weekly_run(store, "支付改造 10 月 25 日上线", [BOTH, ONE])
    request = calls[0][-1].content
    assert "简述写的是 10 月 25 日" in request
    assert request.count("10 月 25 日") == 2  # 简述原文一次 + 冲突记录一次：简述事实没有作为工作项进入正文列表
    assert "#3" not in request  # 只有材料的两项（#1、#2）
    assert len(calls) == 2 and "写进了同一条" in calls[1][-1].content
    [c] = done.generation["conflicts"]
    assert c["pairs"] == ["brief_vs_material"] and c["sources"] == [f"{store.list_materials()[0].id}-1", "简述"]
    assert any("简述与材料说法不一致" in r and "〔简述〕" in r for r in done.generation["reminders"])
    assert "需求池" in done.content  # 简述以外的工作照样写进周报


def test_brief_without_checkable_values_costs_no_extra_call(store):
    done, calls, extract_users = weekly_run(store, "这周主要做支付改造", ["【本周完成】\n- 支付改造上线定于 10 月 20 日〔#1〕\n- 整理需求池〔#2〕"])
    assert not any("用户简述" in u for u in extract_users)  # 简述没有可比对的取值：不额外调用
    assert done.generation["conflicts"] == []


# ---------- Phase 2：值的解析与比较
@pytest.mark.parametrize(
    ("attribute", "a", "b", "expect"),
    [
        ("date", "10 月 20 日", "10/20", "same"),
        ("date", "2026年10月20日", "10月20日", "same"),
        ("date", "10 月 20 日", "10 月 25 日", "different"),
        ("date", "10 月", "10 月 20 日", "unknown"),  # 精度不同
        ("date", "10 月下旬", "11 月 3 日", "different"),  # 月份就不同
        ("date", "下周三", "10 月 20 日", "unknown"),  # 无法解析
        ("amount", "1.2 万元", "12000 元", "same"),
        ("amount", "8000 元", "9000 元", "different"),
        ("amount", "8000 元", "8000 美元", "unknown"),  # 币种不同不换算
        ("quantity", "32 条", "30 条", "different"),
        ("quantity", "32 条", "32 个", "unknown"),  # 单位不同
        ("progress", "60%", "3/5", "same"),
        ("progress", "60%", "过半", "unknown"),
        ("status", "已完成", "完成", "same"),
        ("status", "已完成", "进行中", "different"),
        ("status", "已提测", "测试中", "unknown"),  # 自由文本
        ("owner", "张三", "李四", "different"),
        ("owner", "张三、李四", "张三", "unknown"),  # 有重合
    ],
)
def test_compare_values(attribute, a, b, expect):
    assert compare_values(attribute, a, b) == expect


def test_unknown_values_are_not_conflicts():
    assert detect_conflicts([fact("10 月"), fact("10 月 20 日", "2-1")]) == []


def test_aspect_separates_different_dates():
    """回归：attribute=date 太宽泛，提测日期和上线日期被当成同一属性。"""
    items = [fact("10 月 15 日", aspect="提测"), fact("10 月 20 日", "2-1", aspect="上线")]
    assert detect_conflicts(items) == []


def test_candidate_subjects_need_model_confirmation():
    items = [fact("10 月 20 日", subject="支付改造"), fact("10 月 25 日", "2-1", subject="支付系统改造")]
    [c] = detect_conflicts(items)
    assert c.needs_identity and c.subjects == ["支付改造", "支付系统改造"]

    def judged(resolution):
        cs = detect_conflicts(items)
        model = FunctionModel(respond=lambda m: structured("_Judgments", judgments=[{"index": 1, "resolution": resolution, "explanation": "x"}]), calls=[])
        assert classify(model, cs, items) == 1 and "需先确认是否同一对象" in model.calls[0][-1].content
        return cs

    assert judged(NOT_SAME) == []  # 不是同一对象：移除
    assert [c.resolution for c in judged(UNRESOLVED)] == [UNRESOLVED]
    broken = FunctionModel(respond=lambda m: (_ for _ in ()).throw(RuntimeError("x")), calls=[])
    cs = detect_conflicts(items)
    classify(broken, cs, items)
    assert cs == []  # 无法确认是同一对象：不强行判冲突


def test_exact_conflicts_survive_model_failure():
    items = [fact("10 月 20 日"), fact("10 月 25 日", "2-1")]
    cs = detect_conflicts(items)
    classify(FunctionModel(respond=lambda m: (_ for _ in ()).throw(RuntimeError("x")), calls=[]), cs, items)
    assert [c.resolution for c in cs] == [UNRESOLVED]


def test_not_same_is_ignored_for_exact_groups():
    items = [fact("10 月 20 日"), fact("10 月 25 日", "2-1")]
    cs = detect_conflicts(items)
    classify(FunctionModel(respond=lambda m: structured("_Judgments", judgments=[{"index": 1, "resolution": "not_same", "explanation": "x"}]), calls=[]), cs, items)
    assert [c.resolution for c in cs] == [UNRESOLVED]  # 主体完全相同的组不接受“不是同一对象”


def test_unrelated_subjects_are_not_candidates():
    items = [fact("10 月 20 日", subject="支付改造"), fact("10 月 25 日", "2-1", subject="订单导出")]
    assert detect_conflicts(items) == []


def test_candidate_count_is_bounded():
    items = [fact(f"10 月 {d} 日", f"{d}-1", subject="支付改造" + "系统" * (d % 2), aspect="上线" if d % 3 else "") for d in range(1, 29)]
    assert sum(c.needs_identity for c in detect_conflicts(items)) <= 10
