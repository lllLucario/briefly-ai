"""F4/F5：虚报成功拦截和证据缺口程序提示；不用真实模型。"""
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from briefly import config
from briefly.agent import Agent
from briefly.api import visible_messages
from briefly.db import create_db_engine
from briefly.evidence_gaps import turn_evidence_gaps
from briefly.materials import MaterialStore
from briefly.output_check import false_success_claims
from fakes import FunctionModel


@pytest.fixture
def store():
    engine = create_db_engine("sqlite://")
    yield MaterialStore(engine)
    engine.dispose()


@pytest.mark.parametrize("text", ["我已保存待办", "我已经发送邮件", "已为你创建以下待办任务", "已经帮你更新任务状态", "我已把邮件发送给老板", "我已将任务添加到待办列表"])
def test_first_person_completed_writes_are_detected(text):
    assert false_success_claims(text)


@pytest.mark.parametrize("text", ["张三已发送邮件。", "请在预览中确认保存待办。", "我可以为你整理待办草稿。", "我没有保存待办。", "原文：‘张三已发送邮件’", "原话：“我已发送邮件”", "> 我已发送邮件", "```\n我已发送邮件\n```", "我已整理好待办，请确认后保存。", "我已说明如何保存待办。", "我已整理好待办但尚未保存。", "我已把邮件准备好稍后发送。"])
def test_quotes_third_person_and_instructions_are_allowed(text):
    assert false_success_claims(text) == []


def test_false_success_retries_once_and_persistent_claim_has_visible_warning(store):
    responses = iter([AIMessage("我已保存待办任务。"), AIMessage("已为你保存待办任务。")])
    model = FunctionModel(respond=lambda _: next(responses), calls=[])
    agent = Agent(model, tools=[], store=store)
    agent.chat("帮我加到待办")
    assert len(model.calls) == 2
    assert "只有只读工具" in model.calls[-1][-1].text
    assert any("未执行" in w for w in visible_messages(agent.history)[-1].warnings)
    assert len(agent.history) == 2


def test_corrected_success_claim_needs_no_warning(store):
    responses = iter([AIMessage("我已发送邮件。"), AIMessage("请核对邮件草稿后自行发送。")])
    agent = Agent(FunctionModel(respond=lambda _: next(responses), calls=[]), tools=[], store=store)
    agent.chat("发给老板")
    assert not visible_messages(agent.history)[-1].warnings


def test_gap_scan_is_turn_local_and_preserves_independent_failures():
    messages = [HumanMessage("旧任务"), ToolMessage("失败", tool_call_id="old", status="error"), HumanMessage("本轮"), ToolMessage("部分内容", tool_call_id="part", artifact={"failed": [{"source": "1-2"}], "dropped": [{"quote": "不在原文"}]})]
    gaps = turn_evidence_gaps(messages, 2)
    assert len(gaps) == 3
    assert not any("工具未执行" in g for g in gaps)
    assert "有 2 轮历史未纳入上下文" in gaps


def test_truncated_result_requires_all_missing_ranges_not_just_a_read_call():
    preview = ToolMessage("开头", tool_call_id="dump", artifact={"truncation": {"result_id": "r_a", "preview_chars": 10, "total_chars": 100}})
    def read(start, end, rid="r_a", error=False):
        return ToolMessage("内容", tool_call_id=f"read-{start}", status="error" if error else "success", artifact={"result_read": {"result_id": rid, "start": start, "end": end}})
    messages = [HumanMessage("总结"), preview]
    assert turn_evidence_gaps(messages + [read(50, 100)])
    assert turn_evidence_gaps(messages + [read(10, 100, "r_other")])
    assert turn_evidence_gaps(messages + [read(10, 100, error=True)])
    assert turn_evidence_gaps(messages + [read(10, 50), read(50, 100)]) == []


def test_budget_refusal_is_shown_without_extra_model_retry(store, monkeypatch):
    monkeypatch.setattr(config, "TURN_TOOL_TOKENS", 1)
    def respond(messages):
        if isinstance(messages[-1], ToolMessage):
            return AIMessage("目前只能根据用户输入回答。")
        return AIMessage("", tool_calls=[{"name": "list_materials", "args": {}, "id": "list"}])
    model = FunctionModel(respond=respond, calls=[])
    agent = Agent(model, store=store)
    agent.chat("列出材料")
    assert len(model.calls) == 2
    assert any("本次回答基于部分内容" in w and "未执行" in w for w in visible_messages(agent.history)[-1].warnings)


def test_truncation_metadata_and_unread_warning_survive_output_retry(store, monkeypatch):
    monkeypatch.setattr(config, "TOOL_RESULT_TOKENS", 400)
    @tool
    def dump() -> str:
        """返回很长的结果。"""
        return "完整原始内容。" * 2000
    def respond(messages):
        if messages[-1].additional_kwargs.get("internal"):
            return AIMessage("简短总结。")
        if isinstance(messages[-1], ToolMessage):
            return AIMessage("见第2块。")
        return AIMessage("", tool_calls=[{"name": "dump", "args": {}, "id": "dump"}])
    model = FunctionModel(respond=respond, calls=[])
    agent = Agent(model, tools=[dump], store=store)
    agent.chat("总结")
    assert len(model.calls) == 3
    message = next(m for m in agent.history if isinstance(m, ToolMessage))
    assert message.artifact["truncation"]["total_chars"] == len("完整原始内容。" * 2000)
    assert any("尚有内容未读回" in w for w in visible_messages(agent.history)[-1].warnings)


def test_partial_summary_header_does_not_claim_all_content_processed(store, monkeypatch):
    from briefly.tools import materials as module
    from briefly.summarize import ExtractionResult, WorkItem
    material, _ = store.add_text("会议", "已完成接口开发。")
    extracted = ExtractionResult([WorkItem("done", "已完成接口开发", "已完成接口开发", f"{material.id}-1")], 2, failed=[{"source": f"{material.id}-2", "error": "测试失败"}])
    monkeypatch.setattr(module, "extract_items", lambda *_, **__: extracted)
    monkeypatch.setattr(module, "synthesize", lambda *_, **__: SimpleNamespace(raw="已完成接口开发〔#1〕", text="", uncovered=[], invalid_refs=[], coverage_checked=True))
    def respond(messages):
        if isinstance(messages[-1], ToolMessage):
            assert "已处理 1/2 部分" in messages[-1].text
            assert "程序已读完全部内容" not in messages[-1].text
            return AIMessage("接口开发已完成。")
        return AIMessage("", tool_calls=[{"name": "summarize_materials", "args": {}, "id": "sum"}])
    model = FunctionModel(respond=respond, calls=[])
    agent = Agent(model, store=store)
    agent.chat("总结")
    assert len(model.calls) == 2
    assert any("部分材料处理失败" in w for w in visible_messages(agent.history)[-1].warnings)
