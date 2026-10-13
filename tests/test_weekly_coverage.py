"""Bug 3：周报只覆盖了一个要点的部分内容。区分提取遗漏（Map）与合并遗漏（Reduce），事实带稳定编号和原文位置。不调用真实模型。"""
from datetime import date

import pytest
from langchain_core.messages import AIMessage

from briefly.db import create_db_engine
from briefly.materials import MaterialStore
from briefly.summarize import Source, WorkItem, _cache_key, _extract_messages, extract_items, format_items, map_omissions
from briefly.weekly import WEEKLY_PURPOSE, WeeklyService, fact_trace, report_check
from fakes import FunctionModel, structured

TEXT = "本周完成用户登录模块，修复 Token 过期问题，并新增 3 个权限校验测试。\n支付回调耗时从 1.8 秒降到 200 毫秒。\n参加组内例会。"
LOGIN = {"category": "done", "point": "完成用户登录模块", "quote": "本周完成用户登录模块，修复 Token 过期问题，并新增 3 个权限校验测试。", "importance": "high"}
LOGIN_FULL = {**LOGIN, "point": "完成用户登录模块，修复 Token 过期问题，新增 3 个权限校验测试"}
PAY = {"category": "done", "point": "支付回调耗时从 1.8 秒降到 200 毫秒", "quote": "支付回调耗时从 1.8 秒降到 200 毫秒。", "importance": "high"}
MEETING = {"category": "other", "point": "参加组内例会", "quote": "参加组内例会。", "importance": "low"}


@pytest.fixture
def store():
    engine = create_db_engine("sqlite://")
    yield MaterialStore(engine)
    engine.dispose()


def extract(*items):
    model = FunctionModel(respond=lambda messages: structured("Extraction", items=list(items)), calls=[])
    return extract_items(model, [Source(1, 2, "周报", TEXT, offset=100)])


def test_facts_get_stable_ids_spans_and_importance():
    result = extract(LOGIN_FULL, PAY, MEETING)
    assert [i.fact_id for i in result.items] == ["1-2.1", "1-2.2", "1-2.3"]
    assert result.items[1].span_start == 100 + TEXT.index(PAY["quote"])  # 材料全文中的位置
    assert [i.importance for i in result.items] == ["high", "high", "low"]
    assert result.incomplete == []


def test_map_omission_is_detected_and_shown_to_writer():
    result = extract(LOGIN, PAY)
    assert result.incomplete == [{"fact_ids": ["1-2.1"], "quote": LOGIN["quote"], "missing": ["Token", "3"]}]
    assert "（原文中还写了 Token、3，概括里没有" in format_items(result.items)
    # 同一句拆成几项、合起来写全了，就不算遗漏
    split = [WorkItem("done", "完成用户登录模块", LOGIN["quote"], "1-1"), WorkItem("done", "修复 Token 过期问题，新增 3 个测试", LOGIN["quote"], "1-1")]
    assert map_omissions(split) == []


def test_purpose_goes_into_prompt_and_cache_key():
    source = Source(1, 1, "周报", TEXT)
    assert "用途：" + WEEKLY_PURPOSE in _extract_messages(source, "", None, purpose=WEEKLY_PURPOSE)[1][1]
    assert _cache_key(source, "", None) != _cache_key(source, "", None, purpose=WEEKLY_PURPOSE)
    assert "用途" not in _extract_messages(source, "", None)[1][1]  # 不传用途时与原来一致（完整总结等）


def items_of(*dicts):
    return [WorkItem(d["category"], d["point"], d["quote"], "1-1", fact_id=f"1-1.{k}", importance=d["importance"]) for k, d in enumerate(dicts, 1)]


def test_check_flags_partial_and_missing_high_importance():
    items = items_of(LOGIN_FULL, PAY, MEETING)
    check = report_check(items)
    full = "【本周完成】\n- 完成用户登录模块，修复 Token 过期问题，新增 3 个权限校验测试〔#1〕\n- 支付回调耗时从 1.8 秒降到 200 毫秒〔#2〕"
    assert check(full) == []  # 低重要度的例会没写不算问题
    partial = "【本周完成】\n- 完成用户登录模块并修复 Token 过期问题〔#1〕\n- 支付回调耗时降到 200 毫秒〔#2〕"
    problems = check(partial)
    assert any("#1" in p and "缺少 3" in p for p in problems)
    assert any("#2" in p and "缺少 1.8" in p for p in problems)
    missing = check("【本周完成】\n- 支付回调耗时从 1.8 秒降到 200 毫秒〔#2〕")
    assert any("#1（" in p and "是重要内容，没有写进周报" in p for p in missing)


def test_fact_trace_records_where_each_fact_went():
    items = items_of(LOGIN_FULL, PAY, MEETING)
    raw = "【本周完成】\n- 登录模块与 Token 修复，新增 3 个测试；支付回调从 1.8 秒降到 200 毫秒〔#1、#2〕\n\n【给用户的提醒】\n- 例会〔#3〕"
    trace, partial = fact_trace(items, raw)
    assert [(t["fact_id"], t["status"]) for t in trace] == [("1-1.1", "merged"), ("1-1.2", "merged"), ("1-1.3", "uncovered")]
    assert trace[0]["merged_with"] == ["1-1.2"] and partial == []  # 提醒里提到的不算写进周报


def weekly_with(store, reports, items):
    material, _ = store.add_text("周报素材", TEXT)
    queue = list(reports)
    calls = []

    def respond(messages):
        if "撰写周报" in messages[0].content:
            calls.append(messages)
            return AIMessage(queue.pop(0))
        return structured("Extraction", items=list(items))

    weekly = WeeklyService(store, lambda: FunctionModel(respond=respond, calls=[]))
    report = weekly.create([material.id], "", date(2026, 10, 12), date(2026, 10, 18))
    weekly.generate(report.id)
    return weekly.get(report.id), calls, material


PARTIAL = "【本周完成】\n- 完成用户登录模块，修复 Token 过期问题，新增 3 个权限校验测试〔#1〕\n- 支付回调耗时降到 200 毫秒〔#2〕"
FULL = "【本周完成】\n- 完成用户登录模块，修复 Token 过期问题，新增 3 个权限校验测试〔#1〕\n- 支付回调耗时从 1.8 秒降到 200 毫秒〔#2〕"


def test_reduce_omission_is_rewritten(store):
    done, calls, _ = weekly_with(store, [PARTIAL, FULL], [LOGIN_FULL, PAY, MEETING])
    assert len(calls) == 2 and "缺少 1.8" in calls[1][-1].content
    assert "1.8 秒" in done.content
    statuses = {f["fact_id"]: f["status"] for f in done.generation["facts"]}
    assert list(statuses.values()) == ["written", "written", "uncovered"]


def test_reduce_omission_left_after_rewrite_is_reminded_with_stage(store):
    done, calls, _ = weekly_with(store, [PARTIAL, PARTIAL], [LOGIN_FULL, PAY, MEETING])
    assert any("只写了一部分，缺少 1.8（合成周报时遗漏）" in r for r in done.generation["reminders"])
    assert done.generation["facts"][1]["missing"] == ["1.8"]


def test_map_omission_missing_from_report_is_reminded_with_stage(store):
    report = "【本周完成】\n- 完成用户登录模块〔#1〕\n- 支付回调耗时从 1.8 秒降到 200 毫秒〔#2〕"
    done, calls, material = weekly_with(store, [report, report], [LOGIN, PAY])
    assert any("原文中的 Token、3 没有写进周报（提取要点时遗漏）" in r and f"〔{material.id}-1〕" in r for r in done.generation["reminders"])
    assert done.generation["map_omissions"][0]["missing"] == ["Token", "3"]


def test_map_omission_fixed_by_writer_is_not_reminded(store):
    done, calls, _ = weekly_with(store, [FULL], [LOGIN, PAY])  # 写作时按原文写全了
    assert not any("提取要点时遗漏" in r for r in done.generation["reminders"])


def test_secondary_fields_are_parsed_leniently():
    from pydantic import ValidationError

    from briefly.summarize import Extraction

    raw = {"category": "done", "point": "完成联调", "quote": "完成联调", "importance": "中", "attribute": "deadline", "subject": None, "value": 20}
    [item] = Extraction.model_validate({"items": [raw]}).items
    assert (item.importance, item.attribute, item.subject, item.value) == ("medium", "", "", "20")  # 次要字段不合法不影响整条
    with pytest.raises(ValidationError):
        Extraction.model_validate({"items": [{**raw, "category": "完成"}]})  # 核心字段仍严格校验


def test_bad_secondary_field_does_not_drop_chunk():
    bad = {**PAY, "importance": "很重要", "attribute": "time"}
    result = extract(bad)
    assert result.failed == [] and [i.point for i in result.items] == [PAY["point"]]


def test_confirmed_evidence_is_not_checked_for_map_omission():
    evidence = WorkItem("done", "更新进度表：接口联调 100%", "接口联调进度由 50% 更新为 100%（进度.xlsx）", "E4")  # 没有 fact_id
    assert map_omissions([evidence]) == []


def test_hint_text_copied_into_report_is_rewritten():
    items = items_of(LOGIN_FULL)
    leaked = "【本周完成】\n- 完成用户登录模块，修复 Token 过期问题，新增 3 个权限校验测试，原文中还写了 xlsx〔#1〕"
    assert any("给你看的说明文字（原文中还写了）" in p for p in report_check(items)(leaked))
