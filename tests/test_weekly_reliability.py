"""周报事实可靠性：纯中文要点、工作记录比较、时间状态疑点。仅使用假模型。"""
from dataclasses import replace
from datetime import date

import pytest
from langchain_core.messages import AIMessage

from briefly.conflicts import classify, detect_conflicts, describe
from briefly.db import create_db_engine
from briefly.materials import MaterialStore
from briefly.summarize import Source, WorkItem, extract_items
from briefly.weekly.engine import ReportWriter, report_check
from briefly.weekly.models import WeeklyReport
from fakes import FunctionModel, structured


@pytest.fixture
def writer():
    engine = create_db_engine("sqlite://")
    yield ReportWriter(MaterialStore(engine))
    engine.dispose()


TEXT = "完成退款校验，并由王敏复核异常分支。"


def extraction_model(**extra):
    fields = dict(category="done", point="完成退款校验", quote=TEXT,
                  importance="high", key_details=["退款校验", "王敏", "异常分支", "不存在的事实", "王敏"])
    fields.update(extra)
    return FunctionModel(respond=lambda _: structured("Extraction", items=[fields]), calls=[])


def test_chinese_anchors_are_grounded_and_find_both_map_and_reduce_omissions():
    result = extract_items(extraction_model(), [Source(1, 1, "虚构记录", TEXT)])
    [item] = result.items
    assert item.key_details == ("退款校验", "王敏", "异常分支")
    assert result.incomplete[0]["missing"] == ["王敏", "异常分支"]
    problems = report_check(result.items)("【本周完成】\n- 完成退款校验〔#1〕")
    assert any("王敏、异常分支" in p for p in problems)
    assert report_check(result.items)("【本周完成】\n- 完成退款校验，由王敏复核异常分支〔#1〕") == []


@pytest.mark.parametrize("subject,value", [("虚构项目", "王敏"), ("退款校验", "李强")])
def test_ungrounded_comparison_fields_do_not_create_conflicts(subject, value):
    result = extract_items(extraction_model(subject=subject, attribute="owner", value=value),
                           [Source(1, 1, "虚构记录", TEXT)])
    assert len(result.items) == 1  # 原始工作项保留，只拒绝无法核实的比较字段
    assert result.items[0].attribute == ""
    assert len(result.unverified_facts) == 1


def fact(source, attribute, value, aspect="", subject="退款改造"):
    point = f"{subject}{aspect}{value}"
    return WorkItem("other", point, point, source, fact_id=f"{source}.1", subject=subject,
                    attribute=attribute, value=value, aspect=aspect)


def test_evidence_facts_are_batched_cached_and_keep_original_body_numbers(writer):
    original = WorkItem("done", "退款改造上线时间10月20日", "退款改造上线时间10月20日", "E7")
    second = WorkItem("other", "看板改造负责人王敏", "看板改造负责人王敏", "E8")
    rows = [dict(category="other", point=i.point, quote=i.quote, subject=subject,
                 attribute=attr, value=value, aspect=aspect)
            for i, subject, attr, value, aspect in [
                (original, "退款改造", "date", "10月20日", "上线"),
                (second, "看板改造", "owner", "王敏", "")]]
    model = FunctionModel(respond=lambda _: structured("Extraction", items=rows), calls=[])
    warnings = []
    result = writer._evidence_facts(model, [original, second], "2026-10-12 至 2026-10-18", warnings)
    cached = writer._evidence_facts(model, [original, second], "2026-10-12 至 2026-10-18", warnings)
    assert result.usage.calls == 1 and cached.usage.calls == 0 and cached.cached == 1
    assert not warnings and original.attribute == "" and original.source == "E7"
    material = fact("3-1", "date", "10月25日", "上线")
    [conflict] = detect_conflicts([material, *result.items], reference_numbers={
        material.fact_id: 3, result.items[0].fact_id: 1, result.items[1].fact_id: 2})
    assert conflict.numbers == [3, 1] and conflict.pairs == ["evidence_vs_material"]
    assert conflict.sources == ["3-1", "E7"]
    assert "正文同时写出各来源" in describe([conflict], [])


def test_ambiguous_evidence_does_not_get_arbitrary_source(writer):
    item = WorkItem("done", "退款改造上线10月20日", "退款改造上线10月20日", "E1")
    model = FunctionModel(respond=lambda _: structured("Extraction", items=[dict(
        category="done", point=item.point, quote=item.quote, subject="退款改造",
        attribute="date", value="10月20日", aspect="上线")]), calls=[])
    warnings = []
    result = writer._evidence_facts(model, [item, replace(item, source="E2")], "本周", warnings)
    assert result.items == [] and any("无法唯一定位" in w for w in warnings)


def test_failed_evidence_extraction_warns_and_keeps_original_record(writer):
    item = WorkItem("done", "退款改造上线10月20日", "退款改造上线10月20日", "E1")
    warnings = []
    result = writer._evidence_facts(FunctionModel(respond=lambda _: AIMessage("错误格式"), calls=[]),
                                     [item], "本周", warnings)
    assert result.items == [] and any("核对未完成" in w for w in warnings)
    assert item.point == "退款改造上线10月20日"


def test_single_record_needs_no_comparison_call_and_remaining_checks_are_visible(writer, monkeypatch):
    item = WorkItem("done", "完成退款校验", "完成退款校验", "E1", importance="high")
    monkeypatch.setattr(writer, "_evidence_items", lambda *args: [item])
    model = FunctionModel(respond=lambda _: AIMessage("【本周完成】\n- 完成退款校验，需保留的原文要点〔#1〕"), calls=[])
    writer._model_factory = lambda: model
    report = WeeklyReport(period_start=date(2026, 10, 12), period_end=date(2026, 10, 18),
                          note="", material_ids=[], inputs={"evidence_ids": [1]})
    _, items, generation = writer.write(report)
    assert generation["evidence_fact_calls"] == 0 and generation["calls"] == 2
    assert items[0]["point"] == item.point
    assert any("重写后仍未通过核对" in w for w in generation["warnings"])


def test_active_passive_rewording_of_chinese_actions_is_not_reported_as_loss():
    result = extract_items(extraction_model(key_details=["王敏", "复核异常分支", "退款校验"]),
                           [Source(1, 1, "虚构记录", TEXT)])
    assert result.items[0].key_details == ("王敏", "异常分支", "退款校验")
    assert not report_check(result.items)("【本周完成】\n- 完成退款校验，异常分支由王敏复核〔#1〕")


def test_two_records_compare_without_materials(writer, monkeypatch):
    items = [WorkItem("plan", "退款改造上线10月20日", "退款改造上线10月20日", "E1", importance="high"),
             WorkItem("plan", "退款改造上线10月25日", "退款改造上线10月25日", "E2", importance="high")]
    monkeypatch.setattr(writer, "_evidence_items", lambda *args: items)

    def respond(messages):
        if "提取要点" in messages[0].content:
            return structured("Extraction", items=[dict(category="plan", point=i.point, quote=i.quote,
                subject="退款改造", attribute="date", value=v, aspect="上线")
                for i, v in zip(items, ["10月20日", "10月25日"])])
        if "判断几处" in messages[0].content:
            return structured("_Judgments", judgments=[])
        return AIMessage("【下周计划】\n- 退款改造上线：10月20日〔#1〕/10月25日〔#2〕，日期待核对")

    writer._model_factory = lambda: FunctionModel(respond=respond, calls=[])
    report = WeeklyReport(period_start=date(2026, 10, 12), period_end=date(2026, 10, 18),
                          note="", material_ids=[], inputs={"evidence_ids": [1, 2]})
    _, _, generation = writer.write(report)
    assert generation["evidence_fact_calls"] == 1
    assert generation["conflicts"][0]["pairs"] == ["evidence_vs_evidence"]
    assert any("已确认工作记录之间" in w and "〔E1〕" in w and "〔E2〕" in w for w in generation["reminders"])


def test_future_launch_and_launched_status_are_suspected_without_inventing_explanation():
    items = [fact("1-1", "date", "2026-10-25", "上线"), fact("简述", "status", "已上线")]
    conflicts = detect_conflicts(items, as_of=date(2026, 10, 18))
    assert len(conflicts) == 1 and conflicts[0].possible
    model = FunctionModel(respond=lambda _: structured("_Judgments", judgments=[dict(
        index=1, resolution="superseded", current=2, evidence="已上线", explanation="提前完成了")]), calls=[])
    classify(model, conflicts, items)
    assert conflicts[0].resolution == "unresolved"
    assert "不能宣称已证明矛盾" in describe(conflicts, items)


@pytest.mark.parametrize("when,aspect,source,as_of", [
    ("2026-10-15", "上线", "1-1", date(2026, 10, 18)),
    ("2025-10-25", "上线", "1-1", date(2026, 10, 18)),
    ("11月25日", "上线", "1-1", date(2026, 10, 18)),
    ("10月25日", "提测", "1-1", date(2026, 10, 18)),
    ("10月25日", "上线", "简述", date(2026, 10, 18)),
    ("10月25日", "上线", "1-1", None),
])
def test_timeline_does_not_guess_year_phase_or_same_source(when, aspect, source, as_of):
    assert not detect_conflicts([fact(source, "date", when, aspect), fact("简述", "status", "已上线")], as_of=as_of)


def test_completed_testing_does_not_question_future_delivery():
    assert not detect_conflicts([fact("1-1", "date", "2026-10-25", "交付"),
                                 fact("简述", "status", "已完成", "测试")], as_of=date(2026, 10, 18))
