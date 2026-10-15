"""Bug 1：内部处理信息（块号、程序出处、工具名）混进最终回答。不调用真实模型。"""
import logging

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from briefly.agent import Agent
from briefly.db import create_db_engine
from briefly.materials import MaterialStore
from briefly.output_check import RETRY_HINT, find_internal_terms
from briefly.weekly import WeeklyService
from fakes import FunctionModel, structured


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
    report = weekly.create([m.id], "")
    weekly.generate(report.id)
    done = weekly.get(report.id)
    assert any("程序内部的处理信息：第 2 块" in w for w in done.generation["warnings"])
    assert "〔1-1〕" in done.content  # 正常的出处标记不算


def test_retry_hint_names_terms():
    assert "第 3 块" in RETRY_HINT.format(terms="第 3 块")
