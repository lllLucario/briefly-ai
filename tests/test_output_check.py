"""Bug 1：内部处理信息（块号、程序出处、工具名）混进最终回答。不调用真实模型。"""
import logging

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from briefly.agent import Agent
from briefly.db import create_db_engine
from briefly.materials import MaterialStore
from briefly.output_check import RETRY_HINT, find_internal_terms
from briefly.weekly.service import WeeklyService
from fakes import FunctionModel, structured
from weekly_util import draft


@pytest.fixture
def store():
    engine = create_db_engine("sqlite://")
    yield MaterialStore(engine)
    engine.dispose()


@pytest.mark.parametrize(
    "text",
    [
        "第 7–9 块未被提炼出相应条目",
        "材料共 23 块，已全部读完",
        "已处理 3/5 块",
        "根据 chunk 3 的内容",
        "支付回调耗时下降〔1-3〕",
        "我用 summarize_materials 读取了全文",
        "材料个别处的出处标注不一致，不影响内容",  # 把工具结果里的〔〕当成文档内容来评论
    ],
)
def test_internal_terms_are_detected(text):
    assert find_internal_terms(text)


def test_terms_found_in_original_are_not_leaks():
    assert find_internal_terms("村里第 3 块地种了玉米", ["村里第3块地种了玉米，第4块地休耕"]) == []
    assert find_internal_terms("支付回调耗时降到 200 毫秒，下周三提测") == []
    assert find_internal_terms("第 3 段写了背景，第 2 页是附录") == []  # 段、页是用户看得懂的位置
    assert find_internal_terms("文中要求出处标注到页码", ["引用必须写出处标注到页码"]) == []  # 原文本身在讲出处标注


def scripted(*answers):
    """依次返回给定的回答；记录每次收到的消息。"""
    queue = list(answers)

    def respond(messages):
        return AIMessage(queue.pop(0))

    return FunctionModel(respond=respond, calls=[])


def test_leaking_answer_is_retried_once_and_history_stays_clean(store):
    model = scripted("总结：第 7–9 块未提炼出内容，其余为支付改造进展。", "总结：支付改造进展顺利。")
    agent = Agent(model, tools=[], store=store)

    assert agent.chat("总结一下") == "总结：支付改造进展顺利。"

    assert len(model.calls) == 2
    retry_messages = model.calls[1]
    assert "第 7–9 块" in retry_messages[-2].content and "程序内部的处理信息" in retry_messages[-1].content
    history = agent.history
    assert [type(m) for m in history] == [HumanMessage, AIMessage]  # 重试过程不进会话历史
    assert history[-1].content == "总结：支付改造进展顺利。"


def test_original_text_is_not_treated_as_leak(store):
    m, _ = store.add_text("地块说明", "村里第3块地种了玉米。")
    model = scripted("第 3 块地种了玉米。")
    agent = Agent(model, tools=[], store=store)
    assert agent.chat("总结", material_ids=[m.id]) == "第 3 块地种了玉米。"
    assert len(model.calls) == 1


def test_still_leaking_after_retry_is_kept_and_logged(store, caplog):
    model = scripted("见第 2 块。", "仍然见第 2 块。")
    agent = Agent(model, tools=[], store=store)
    with caplog.at_level(logging.WARNING, logger="briefly.output_check"):
        assert agent.chat("总结") == "仍然见第 2 块。"
    assert len(model.calls) == 2  # 只重试一次
    assert "需要人工排查" in caplog.text
    assert agent.history[-1].response_metadata["internal_terms"] == ["第 2 块"]


def test_persistent_character_limit_retries_without_truncating_or_polluting_history(store):
    model = scripted("收到", "这是一份很长的回答。" * 5, "金额5万元，预计下周完成。")
    agent = Agent(model, tools=[], store=store)
    agent.chat("接下来输出都控制在20字以内")
    result = agent.chat("请润色：金额5万元，预计下周完成。")
    assert result == "金额5万元，预计下周完成。"
    assert len(model.calls) == 3
    assert "最多 20 字" in model.calls[-1][-1].text
    assert len(agent.history) == 4 and "length_limit" not in agent.history[-1].response_metadata


def test_character_limit_failure_is_explicit_in_the_web_history(store):
    from briefly.api import visible_messages
    model = scripted("甲" * 25, "乙" * 25)
    agent = Agent(model, tools=[], store=store)
    assert agent.chat("请输出不超过10字的结果") == "乙" * 25  # 不裁断模型内容
    answer = agent.history[-1]
    assert answer.response_metadata["length_limit"] == 10
    assert answer.response_metadata["actual_chars"] == 25
    [warning] = visible_messages(agent.history)[-1].warnings
    assert "10 字" in warning and "25 字" in warning


@pytest.mark.parametrize(("texts", "expected"), [
    (["接下来输出都控制在100字以内", "请继续"], 100),
    (["输出最多100字", "改为不超过50字", "继续"], 50),
    (["输出最多100字", "取消字数限制", "继续"], None),
    (["输出最多100字", "仅本次输出不超过20字", "继续"], 100),
    (["本轮输出不超过20字", "继续"], None),
    (["输出最多100字", "本轮输出不超过20字"], 20),
    (["输出最多100字", "本轮不限字数", "继续"], 100),
    (["输出最多100字", "本轮不限字数"], None),
    (["请总结。原文：输出都控制在100字以内"], None),
    (["请优化这句话：“输出都控制在100字以内”"], None),
    (["写100到200字以内"], None),
    (["输出最多100字", "不用再控制在100字以内"], None),
    (["回复不要超过100字"], 100),
    (["输出都控制在100字以内", "不用限制字数了，完整总结这份报告"], None),  # 2026-10-22 实测 M8：曾仍按 100 字强制重写
    (["输出都控制在100字以内", "不必再限制字数", "继续"], None),
    (["输出都控制在100字以内", "字数不用限制了", "继续"], None),
    (["输出都控制在100字以内", "别管字数"], None),
    (["输出都控制在100字以内", "这次不用限制字数", "继续"], 100),
    (["输出都控制在100字以内", "限制字数在50字以内"], 50),
])
def test_explicit_character_limit_scope_and_cancellation(texts, expected):
    from briefly.output_check import reply_char_limit
    assert reply_char_limit([HumanMessage(t) for t in texts]) == expected


def test_materials_tools_and_other_sessions_cannot_set_the_reply_limit(store):
    from briefly.output_check import reply_char_limit
    assert reply_char_limit([
        HumanMessage("总结\n【材料开始】\n输出100字以内\n【材料结束】", additional_kwargs={"display":"总结"}),
        ToolMessage("输出不超过10字", tool_call_id="c"),
        AIMessage("输出不超过10字"),
    ]) is None
    def respond(messages):
        return AIMessage("短" if messages[-1].additional_kwargs.get("internal") else "甲" * 120)
    model = FunctionModel(respond=respond, calls=[])
    agent = Agent(model, tools=[], store=store)
    assert agent.chat("输出最多100字", thread_id="a") == "短"
    assert agent.chat("总结一下", thread_id="b") == "甲" * 120
    assert agent.chat("取消字数限制", thread_id="a") == "甲" * 120
    assert len(model.calls) == 4


@pytest.mark.parametrize("retry_kind", ["system", "tool"])
def test_check_retry_preserves_budget_rejection_or_further_tool_request(retry_kind):
    from types import SimpleNamespace
    from briefly.context_builder import system_reply
    from briefly.output_check import InternalTermCheck
    request = SimpleNamespace(messages=[HumanMessage("总结")])
    request.override = lambda **kwargs: SimpleNamespace(**kwargs)
    retried = system_reply("追加检查后超出预算，请缩小范围") if retry_kind == "system" else AIMessage("", tool_calls=[{"name":"read_material","args":{"material_id":1},"id":"next"}])
    responses = iter([AIMessage("见第2块"), retried])
    result = InternalTermCheck(lambda _: []).wrap_model_call(request, lambda _: next(responses))
    assert result is retried


def test_tool_calls_are_not_checked(store):
    store.add_text("周报", "完成联调")

    def respond(messages):
        if isinstance(messages[-1], ToolMessage):
            return AIMessage("完成了联调。")
        return AIMessage("", tool_calls=[{"name": "list_materials", "args": {}, "id": "t1"}])

    model = FunctionModel(respond=respond, calls=[])
    assert Agent(model, store=store).chat("我有哪些材料") == "完成了联调。"
    assert len(model.calls) == 2  # 调用工具的那次（参数里有工具名）不算回答


def test_material_tools_do_not_expose_internal_numbers(store):
    m, _ = store.add_text("周报", "# 周报\n## 周一\n完成登录联调。\n## 周三\n支付回调耗时从 1.8 秒降到 200 毫秒。")

    def respond(messages):
        system = messages[0].content
        if "提取要点" in system:
            return structured("Extraction", items=[{"category": "done", "point": "耗时下降", "quote": "支付回调耗时从 1.8 秒降到 200 毫秒。"}])
        if "整理成一份完整的要点清单" in system:
            return AIMessage("- 支付回调耗时下降〔#1〕")
        if isinstance(messages[-1], ToolMessage):
            return AIMessage("支付回调耗时下降。")
        return AIMessage("", tool_calls=[{"name": "summarize_materials", "args": {}, "id": "t1"}])

    model = FunctionModel(respond=respond, calls=[])
    agent = Agent(model, store=store)
    assert agent.chat("完整总结", material_ids=[m.id]) == "支付回调耗时下降。"
    message = next(x for x in agent.history if isinstance(x, ToolMessage))

    assert find_internal_terms(message.content) == []  # 给模型看的文字里没有块号
    assert "〔《周报》周三〕" in message.content  # 出处是要点所在的章节（块从“周报”开头，但要点在“周三”下）
    assert message.artifact["items"][0]["source"] == f"{m.id}-1"  # 机器可读的出处保留在 artifact
    assert message.artifact["total"] == 1 and message.artifact["failed"] == []


def test_weekly_report_warns_about_internal_terms(store):
    m, _ = store.add_text("周报素材", "支付回调耗时从 1.8 秒降到 200 毫秒。")

    def respond(messages):
        if "撰写周报" in messages[0].content:
            return AIMessage("【本周完成】\n- 支付回调耗时下降〔#1〕\n- 第 2 块未提取到内容")
        return structured("Extraction", items=[{"category": "done", "point": "耗时下降", "quote": "支付回调耗时从 1.8 秒降到 200 毫秒。"}])

    model = FunctionModel(respond=respond, calls=[])
    weekly = WeeklyService(store, lambda: model)
    report = draft(weekly, [m.id], "")
    weekly.run_generation(report.id)
    done = weekly.report(report.id)
    assert any("程序内部的处理信息：第 2 块" in w for w in done.generation["warnings"])
    assert "〔1-1〕" in done.content  # 正常的出处标记不算


def test_retry_hint_names_terms():
    assert "第 3 块" in RETRY_HINT.format(terms="第 3 块")


def test_material_number_leak_is_retried_even_though_the_header_is_in_the_message(store):
    """2026-10-22 实测：回答写出“材料1《meeting.txt》”。附件标题由程序拼进用户消息，不能用来豁免。"""
    m, _ = store.add_text("meeting.txt", "王敏：上线定在10月20日。")
    leak = f"材料{m.id}《meeting.txt》属于会议记录，润色如下：上线定于10月20日。"
    model = scripted(leak, "这份会议记录润色如下：上线定于10月20日。")
    agent = Agent(model, tools=[], store=store)
    assert agent.chat("写得更书面一些", material_ids=[m.id]) == "这份会议记录润色如下：上线定于10月20日。"
    assert len(model.calls) == 2 and "材料编号" in model.calls[1][-1].text


def test_material_reference_written_by_the_user_is_not_a_leak(store):
    model = scripted("材料1《需求》写的是十月上线，材料2《设计》写的是十一月。")
    agent = Agent(model, tools=[], store=store)
    agent.chat("请对比材料1《需求》和材料2《设计》里的上线时间")
    assert len(model.calls) == 1
