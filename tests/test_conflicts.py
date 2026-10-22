"""Bug 4：多文件矛盾被错误统一。冲突在合并前由代码检测，归类由模型给出、代码核对。不调用真实模型。"""
from datetime import date

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from briefly import config
from briefly.agent import Agent
from briefly.conflicts import CONTEXT, SUPERSEDED, UNRESOLVED, classify, detect_conflicts, describe
from briefly.db import create_db_engine
from briefly.materials import MaterialStore
from briefly.summarize import WorkItem
from briefly.weekly.service import WeeklyService
from fakes import FunctionModel, structured
from weekly_util import draft

A = "项目A：交付时间为 10 月 20 日。"
B = "项目A 项目：交付时间为 10 月 25 日。"
B_CHANGED = "项目A 交付时间由原定 10 月 20 日推迟到 10 月 25 日。"


def item(quote, value, source, k=1, subject="项目A"):
    return WorkItem("other", f"项目A 交付时间 {value}", quote, source, fact_id=f"{source}.{k}", subject=subject, attribute="date", value=value)


@pytest.fixture
def store():
    engine = create_db_engine("sqlite://")
    yield MaterialStore(engine)
    engine.dispose()


def test_detect_conflict_across_files_with_normalization():
    items = [item(A, "10 月 20 日", "1-1"), item(B, "10月25日", "2-1", subject="项目A项目")]
    [c] = detect_conflicts(items)
    assert (c.subject, c.attribute, c.fact_ids, c.numbers, c.resolution) == ("项目A", "date", ["1-1.1", "2-1.1"], [1, 2], UNRESOLVED)
    same = [item(A, "10 月 20 日", "1-1"), item(B, "10月20日", "2-1")]  # 写法不同、值相同
    assert detect_conflicts(same) == []
    unrelated = [item(A, "10 月 20 日", "1-1"), item(B, "10 月 25 日", "2-1", subject="项目B")]
    assert detect_conflicts(unrelated) == []


def judge(*judgments):
    return FunctionModel(respond=lambda messages: structured("_Judgments", judgments=list(judgments)), calls=[])


def test_superseded_needs_change_words_found_in_original():
    items = [item(A, "10 月 20 日", "1-1"), item(B_CHANGED, "10 月 25 日", "2-1")]
    conflicts = detect_conflicts(items)
    assert classify(judge({"index": 1, "resolution": "superseded", "current": 2, "evidence": "由原定 10 月 20 日推迟到 10 月 25 日", "explanation": "后来推迟了"}), conflicts, items) == 1
    assert conflicts[0].resolution == SUPERSEDED and conflicts[0].current == "2-1.1"
    assert "以 #2 为准" in describe(conflicts, items)


@pytest.mark.parametrize("current,value,evidence,expected", [
    (1, "10月25日", "推迟到 10 月 25 日", UNRESOLVED),
    (2, "10月25日", "推迟到 10 月 25 日", SUPERSEDED),
    (2, "10-25", "推迟到 10 月 25 日", SUPERSEDED),
    (2, "2026-10-25", "推迟到 10 月 25 日", UNRESOLVED),
    (0, "10月25日", "推迟到 10 月 25 日", UNRESOLVED),
    (3, "10月25日", "推迟到 10 月 25 日", UNRESOLVED),
])
def test_superseded_current_value_must_appear_in_verified_change_evidence(current, value, evidence, expected):
    items = [item(A, "10月20日", "1-1"), item(B_CHANGED, value, "2-1")]
    conflicts = detect_conflicts(items)
    # 复用对象时，降级也必须清除旧的“当前说法”和证据。
    conflicts[0].current, conflicts[0].evidence = "1-1.1", "旧的依据"
    classify(judge({"index": 1, "resolution": "superseded", "current": current, "evidence": evidence, "explanation": "发生变更"}), conflicts, items)
    assert conflicts[0].resolution == expected
    description = describe(conflicts, items)
    if expected == UNRESOLVED:
        assert conflicts[0].current == conflicts[0].evidence == ""
        assert "以 #1 为准" not in description
        assert "看不出哪个为准" in description
    else:
        assert conflicts[0].current == "2-1.1"
        assert "以 #2 为准" in description


@pytest.mark.parametrize(
    "judgment",
    [
        {"index": 1, "resolution": "superseded", "current": 2, "evidence": "交付时间为 10 月 25 日", "explanation": "猜的"},  # 没有变更字样
        {"index": 1, "resolution": "superseded", "current": 2, "evidence": "已推迟到 11 月", "explanation": "编的"},  # 原文里没有
        {"index": 1, "resolution": "context_difference", "evidence": "测试环境", "explanation": "编的"},  # 原文里没有
    ],
)
def test_ungrounded_judgment_falls_back_to_unresolved(judgment):
    items = [item(A, "10 月 20 日", "1-1"), item(B, "10 月 25 日", "2-1")]
    conflicts = detect_conflicts(items)
    classify(judge(judgment), conflicts, items)
    assert conflicts[0].resolution == UNRESOLVED and "按未解决处理" in conflicts[0].explanation


def test_context_difference_and_model_failure():
    q1, q2 = "测试环境：交付时间为 10 月 20 日。", "生产环境：交付时间为 10 月 25 日。"
    items = [item(q1, "10 月 20 日", "1-1"), item(q2, "10 月 25 日", "2-1")]
    conflicts = detect_conflicts(items)
    classify(judge({"index": 1, "resolution": "context_difference", "evidence": "生产环境", "explanation": "环境不同"}), conflicts, items)
    assert conflicts[0].resolution == CONTEXT
    broken = FunctionModel(respond=lambda m: (_ for _ in ()).throw(RuntimeError("接口出错")), calls=[])
    conflicts = detect_conflicts(items)
    assert classify(broken, conflicts, items) == 1 and conflicts[0].resolution == UNRESOLVED


def weekly_run(store, reports, merge=None):
    a, _ = store.add_text("文件A", A)
    b, _ = store.add_text("文件B", B)
    queue, calls = list(reports), []

    def respond(messages):
        system, user = messages[0].content, messages[-1].content
        if "撰写周报" in system:
            calls.append(messages)
            return AIMessage(queue.pop(0))
        if "合并一批" in system:
            return AIMessage(merge(user))
        if "说法" in system and "superseded" in system:
            return structured("_Judgments", judgments=[{"index": 1, "resolution": "unresolved", "explanation": "两份文件不一致"}])
        value, quote = ("10 月 20 日", A) if "20 日" in user else ("10 月 25 日", B)
        return structured("Extraction", items=[{"category": "other", "point": f"项目A 交付时间为 {value}", "quote": quote, "subject": "项目A", "attribute": "date", "value": value}])

    weekly = WeeklyService(store, lambda: FunctionModel(respond=respond, calls=[]))
    report = draft(weekly, [a.id, b.id], "", date(2026, 10, 12), date(2026, 10, 18))
    weekly.run_generation(report.id)
    return weekly.report(report.id), calls


ONE = "【本周完成】\n- 项目A 预计 10 月 25 日交付〔#2〕"
BOTH = "【本周完成】\n- 项目A 交付时间：10 月 20 日〔#1〕/ 10 月 25 日〔#2〕，两份材料不一致"


def test_weekly_keeps_both_sides_and_reminds(store):
    done, calls = weekly_run(store, [ONE, BOTH])
    assert "多来源核对结果" in calls[0][-1].content and "#1 写的是 10 月 20 日" in calls[0][-1].content
    assert len(calls) == 2 and "正文缺少 #1（10 月 20 日）" in calls[1][-1].content  # 只写一个说法：重写
    assert "10 月 20 日" in done.content and "10 月 25 日" in done.content
    [c] = done.generation["conflicts"]
    assert c["resolution"] == "unresolved" and c["fact_ids"] == ["1-1.1", "2-1.1"]
    assert any("材料之间说法不一致" in r and "〔1-1〕" in r and "〔2-1〕" in r for r in done.generation["reminders"])


def test_conflict_survives_tree_merge(store, monkeypatch):
    monkeypatch.setattr(config, "REDUCE_BUDGET_TOKENS", 20)  # 强制分批合并
    # 中间层把两种说法当成重复，只留下 25 日
    done, calls = weekly_run(store, [ONE, BOTH], merge=lambda user: "- 【其他】项目A 交付时间为 10 月 25 日〔#2〕")
    assert done.generation["merge_levels"] >= 1
    assert "#1 写的是 10 月 20 日" in calls[0][-1].content  # 冲突记录在合并前生成，最后一层仍看得到原来的两种说法
    assert len(calls) == 2 and "10 月 20 日" in done.content


def test_summarize_materials_lists_unresolved_conflicts(store):
    store.add_text("文件A", A)
    store.add_text("文件B", B)

    def respond(messages):
        system, user = messages[0].content, messages[-1].content
        if isinstance(messages[-1], ToolMessage):
            return AIMessage("项目A 交付时间两份材料不一致：10 月 20 日 / 10 月 25 日。")
        if "提取要点" in system:
            value, quote = ("10 月 20 日", A) if "20 日" in user else ("10 月 25 日", B)
            return structured("Extraction", items=[{"category": "other", "point": f"项目A 交付时间为 {value}", "quote": quote, "subject": "项目A", "attribute": "date", "value": value}])
        if "说法" in system and "superseded" in system:
            return structured("_Judgments", judgments=[{"index": 1, "resolution": "unresolved", "explanation": "不一致"}])
        if "整理成一份完整的要点清单" in system:
            return AIMessage("- 项目A 交付时间：10 月 20 日〔#1〕/ 10 月 25 日〔#2〕")
        return AIMessage("", tool_calls=[{"name": "summarize_materials", "args": {}, "id": "t1"}])

    agent = Agent(FunctionModel(respond=respond, calls=[]), store=store)
    agent.chat("总结这两份材料")
    tool = next(m for m in agent.history if isinstance(m, ToolMessage))
    assert "不同材料说法不一致" in tool.content and "〔《文件A》〕" in tool.content and "〔《文件B》〕" in tool.content
    assert tool.artifact["conflicts"][0]["resolution"] == "unresolved"
    # 必须保留的信息放在要点清单前面：结果过长被截断时截掉的是末尾
    assert tool.content.index("不同材料说法不一致") < tool.content.index("要点清单")


def test_agent_answer_that_unifies_conflict_is_retried(store):
    store.add_text("文件A", A)
    store.add_text("文件B", B)
    answers = ["项目A 预计 10 月 25 日交付。", "项目A 交付时间两份材料不一致：10 月 20 日 / 10 月 25 日。"]
    finals = []

    def respond(messages):
        system, user = messages[0].content, messages[-1].content
        if "提取要点" in system:
            value, quote = ("10 月 20 日", A) if "20 日" in user else ("10 月 25 日", B)
            return structured("Extraction", items=[{"category": "other", "point": f"项目A 交付时间为 {value}", "quote": quote, "subject": "项目A", "attribute": "date", "value": value}])
        if "说法" in system and "superseded" in system:
            return structured("_Judgments", judgments=[{"index": 1, "resolution": "unresolved", "explanation": "不一致"}])
        if "整理成一份完整的要点清单" in system:
            return AIMessage("- 项目A 交付时间：10 月 20 日〔#1〕/ 10 月 25 日〔#2〕")
        if any(isinstance(m, ToolMessage) for m in messages):
            finals.append(messages[-1].content)
            return AIMessage(answers.pop(0))
        return AIMessage("", tool_calls=[{"name": "summarize_materials", "args": {}, "id": "t1"}])

    agent = Agent(FunctionModel(respond=respond, calls=[]), store=store)
    assert agent.chat("总结这两份材料") == "项目A 交付时间两份材料不一致：10 月 20 日 / 10 月 25 日。"
    assert len(finals) == 2 and "回答里缺少：项目A 的日期“10 月 20 日”" in finals[1]
    assert sum(1 for m in agent.history if isinstance(m, AIMessage) and not m.tool_calls) == 1  # 重试过程不进会话历史


def test_conflict_needs_each_value_written_not_just_cited():
    """两个编号标在同一行、却只写了一个日期：取值没写出来，要求重写。"""
    from briefly.conflicts import FactConflict, conflict_problems

    c = FactConflict(subject="项目A", attribute="date", values=["10 月 20 日", "10 月 25 日"], sources=["1-1", "2-1"],
                     numbers=[1, 2], origins=["material", "material"], fact_ids=["1-1.1", "2-1.1"])
    one_line = {1: ["- 项目A 交付时间为 10 月 25 日〔#1、#2〕"], 2: ["- 项目A 交付时间为 10 月 25 日〔#1、#2〕"]}
    assert conflict_problems([c], lambda n: one_line.get(n, [])) and "#1（10 月 20 日）" in conflict_problems([c], lambda n: one_line.get(n, []))[0]
    both = {1: ["- 交付时间：10 月 20 日〔#1〕/ 10 月 25 日〔#2〕"], 2: ["- 交付时间：10 月 20 日〔#1〕/ 10 月 25 日〔#2〕"]}
    assert conflict_problems([c], lambda n: both.get(n, [])) == []
    # 写了这件事、却一个日期都没写（2026-10-22 实测 W6）：两个说法都要补
    neither = {1: ["- 项目A：完成接口联调〔#1〕"], 2: ["- 项目A：确认验收标准〔#2〕"]}
    [problem] = conflict_problems([c], lambda n: neither.get(n, []))
    assert "#1（10 月 20 日）" in problem and "#2（10 月 25 日）" in problem
    assert conflict_problems([c], lambda n: []) == []  # 整件事都没写：由漏写提醒处理
