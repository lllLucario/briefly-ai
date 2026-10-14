"""Phase 3：Agent 二次改写的诊断（只测量，不改输出）。用构造的各阶段文本测试计算逻辑。"""
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from briefly.diagnostics import ConflictSpec, Fact, Stages, diagnose, stages_from_history

FACTS = [
    Fact("登录", ["32 条"], "周志"),
    Fact("耗时", ["1.8 秒", "200 毫秒"], "周志"),
    Fact("预算", ["8000 元"], "预算表"),
]
CONFLICT = [ConflictSpec("交付日期", ["10 月 20 日", "10 月 25 日"])]
MATERIALS = {"周志": "联调用例 32 条全部通过。耗时从 1.8 秒降到 200 毫秒。交付 10 月 20 日。", "预算表": "压测环境 8000 元。交付 10 月 25 日。"}


def test_no_loss():
    text = "用例 32 条；耗时 1.8 秒降到 200 毫秒；预算 8000 元；交付 10 月 20 日 / 10 月 25 日"
    d = diagnose(FACTS, CONFLICT, Stages(text, text, text), MATERIALS)
    assert d.retention == {"map_reduce": 1.0, "tool_message": 1.0, "answer": 1.0}
    assert d.lost_at == {} and d.conflict_retention["answer"] == 1.0 and d.unsupported == []


def test_loss_is_attributed_to_the_first_missing_stage():
    mr = "用例 32 条；耗时 1.8 秒降到 200 毫秒；交付 10 月 20 日 / 10 月 25 日"  # 预算在 Map-Reduce 就丢了
    tool = "用例 32 条；耗时 1.8 秒降到 200 毫秒"  # 截断：冲突丢了
    answer = "用例 32 条"  # 改写：耗时丢了
    d = diagnose(FACTS, CONFLICT, Stages(mr, tool, answer, truncated=True), MATERIALS)
    assert d.lost_at == {"Map-Reduce": ["预算"], "Agent 改写": ["耗时"]}
    assert d.conflicts_lost_at == {"工具预算截断": ["交付日期"]}
    assert d.retention["answer"] == round(1 / 3, 3)


def test_truncated_but_read_back_is_recovered_not_lost():
    full = "用例 32 条；耗时 1.8 秒降到 200 毫秒；预算 8000 元"
    d = diagnose(FACTS, [], Stages(full, "用例 32 条", full, truncated=True), MATERIALS)
    assert d.lost_at == {} and sorted(d.recovered) == ["耗时", "预算"]


def test_list_ordinals_and_shared_facts_are_not_false_positives():
    answer = "1. 《周志》：用例 32 条\n2. 《预算表》：压测环境 8000 元\n3. 交付 10 月 25 日"
    materials = {**MATERIALS, "预算表": MATERIALS["预算表"] + "联调用例 32 条。"}
    d = diagnose(FACTS, [], Stages(answer, answer, answer), materials)
    assert d.unsupported == [] and d.attribution_errors == []


def test_conflict_with_one_value_is_not_retained():
    d = diagnose([], CONFLICT, Stages("交付 10 月 20 日", "交付 10 月 20 日", "交付 10 月 20 日"), MATERIALS)
    assert d.conflict_retention["map_reduce"] == 0.0


def test_attribution_errors_and_unsupported_claims():
    answer = "《预算表》显示用例 32 条全部通过。耗时降到 150 毫秒。"
    d = diagnose(FACTS, [], Stages(answer, answer, answer), MATERIALS)
    assert d.attribution_errors == ["登录：写在标注《预算表》的句子里，实际出自《周志》"]
    assert "150 毫秒" in d.unsupported and not any("32" in u for u in d.unsupported)


def test_stages_from_history_uses_full_artifact_and_detects_truncation():
    tool = ToolMessage("【工具结果过长：……】开头部分", tool_call_id="t1", name="summarize_materials", artifact={"content_full": "完整结果 32 条"})
    history = [HumanMessage("总结"), AIMessage("", tool_calls=[{"name": "summarize_materials", "args": {}, "id": "t1"}]), tool, AIMessage("最终回答")]
    st = stages_from_history(history)
    assert (st.map_reduce, st.truncated, st.answer, st.tool_called) == ("完整结果 32 条", True, "最终回答", True)
    assert stages_from_history([HumanMessage("你好"), AIMessage("你好")]).tool_called is False  # 边界：没有调用工具
