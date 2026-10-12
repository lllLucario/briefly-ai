import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from briefly import config
from briefly.agent import CONTEXT_OVERFLOW, Agent, _clear_old_tool_results, build_system_prompt, trim_history
from briefly.skills import SKILLS
from briefly.tokens import count_message_tokens
from fakes import FunctionModel, fake, text_msg, tool_msg


def date_tool(fail: bool = False):
    @tool
    def get_current_date() -> str:
        """d"""
        if fail:
            raise RuntimeError("boom")
        return "2026-10-08 星期四"

    return get_current_date


def test_prompts_exist_for_all_skills():
    for skill in SKILLS.values():
        assert skill.prompt.strip()


def test_system_prompt_contains_base_and_all_skills():
    prompt = build_system_prompt()
    assert "# 角色" in prompt
    for skill in SKILLS.values():
        assert f"（{skill.name}）" in prompt
    assert "本次任务" not in prompt


def test_system_message_comes_first_and_history_excludes_it():
    model = fake(text_msg("ok"))
    agent = Agent(model, tools=[])
    agent.chat("hi")
    assert isinstance(model.calls[0][0], SystemMessage)
    assert not any(isinstance(m, SystemMessage) for m in agent.history)


def test_forced_skill_is_added_to_system_prompt():
    model = fake(text_msg("ok"))
    Agent(model, tools=[]).chat("hi", skill="todo")
    system = model.calls[0][0].content
    assert "本次任务" in system and "待办事项梳理" in system


def test_forced_skill_applies_only_to_that_turn():
    model = fake(text_msg("a"), text_msg("b"))
    agent = Agent(model, tools=[])
    agent.chat("x", skill="todo")
    agent.chat("y")
    assert "本次任务" not in model.calls[1][0].content


def test_unknown_skill_rejected():
    with pytest.raises(ValueError):
        Agent(fake(), tools=[]).chat("hi", skill="nope")


def test_history_is_kept_across_turns():
    model = fake(text_msg("第一条回复"), text_msg("第二条回复"))
    agent = Agent(model, tools=[])
    agent.chat("总结这个")
    agent.chat("再短一点")
    msgs = model.calls[1]
    assert [type(m) for m in msgs] == [SystemMessage, HumanMessage, AIMessage, HumanMessage]
    assert msgs[1].content == "总结这个"


def test_reset_starts_new_thread():
    agent = Agent(fake(text_msg("a")), tools=[])
    old = agent.thread_id
    agent.chat("x")
    agent.reset()
    assert agent.thread_id != old
    assert agent.history == []


def test_history_survives_new_agent_with_same_thread():
    saver = InMemorySaver()
    Agent(fake(text_msg("a")), tools=[], checkpointer=saver, thread_id="s1").chat("x")
    model = fake(text_msg("b"))
    Agent(model, tools=[], checkpointer=saver, thread_id="s1").chat("y")
    assert [m.content for m in model.calls[0][1:]] == ["x", "a", "y"]


def test_tool_loop_runs_tool_and_returns_final_text():
    model = fake(tool_msg("get_current_date", {}), text_msg("下周五是 10-16"))
    agent = Agent(model, tools=[date_tool()])
    assert agent.chat("下周五是几号") == "下周五是 10-16"
    msgs = model.calls[1]
    assert msgs[-2].tool_calls[0]["name"] == "get_current_date"
    assert isinstance(msgs[-1], ToolMessage)
    assert msgs[-1].tool_call_id == "t1" and msgs[-1].content == "2026-10-08 星期四"


def test_tool_error_is_reported_to_model_not_raised():
    model = fake(tool_msg("get_current_date", {}), text_msg("工具出错了"))
    agent = Agent(model, tools=[date_tool(fail=True)])
    assert agent.chat("x") == "工具出错了"
    result = model.calls[1][-1]
    assert "工具执行失败" in result.content and "boom" in result.content


def test_unknown_tool_is_reported_as_error():
    model = fake(tool_msg("nope", {}), text_msg("ok"))
    Agent(model, tools=[date_tool()]).chat("x")
    result = model.calls[1][-1]
    assert isinstance(result, ToolMessage) and "nope" in result.content


def test_bad_tool_arguments_are_reported_as_error():
    @tool
    def add_days(days: int) -> str:
        """d"""
        return str(days)

    model = fake(tool_msg("add_days", {"days": "abc"}), text_msg("ok"))
    Agent(model, tools=[add_days]).chat("x")
    result = model.calls[1][-1]
    assert isinstance(result, ToolMessage) and result.status == "error"


def test_tool_rounds_are_capped(monkeypatch):
    monkeypatch.setattr(config, "MAX_TOOL_ROUNDS", 2)
    model = fake(*[tool_msg("get_current_date", {}, id=f"t{i}") for i in range(5)])
    reply = Agent(model, tools=[date_tool()]).chat("x")
    assert len(model.calls) == 2
    assert "已中止" in reply


def test_tool_round_cap_resets_each_turn(monkeypatch):
    monkeypatch.setattr(config, "MAX_TOOL_ROUNDS", 2)
    model = fake(tool_msg("get_current_date", {}), text_msg("a"), text_msg("b"))
    agent = Agent(model, tools=[date_tool()])
    agent.chat("x")
    assert agent.chat("y") == "b"


def turn(n: int) -> list:
    return [HumanMessage(f"第{n}轮的问题内容" * 3), AIMessage(f"第{n}轮的回答内容" * 3)]


def test_trim_history_keeps_recent_turns_within_token_budget():
    history = turn(1) + turn(2) + turn(3)
    per_turn = count_message_tokens(turn(1))
    assert trim_history(history, per_turn * 3) == history
    assert trim_history(history, per_turn * 2) == history[2:]
    assert trim_history(history, per_turn * 2 - 1) == history[4:]


def test_trim_history_starts_at_user_message():
    history = [
        HumanMessage("第一个问题"),
        AIMessage("", tool_calls=[{"name": "t", "args": {}, "id": "t"}]),
        ToolMessage("工具结果" * 20, tool_call_id="t"),
        AIMessage("第一个回答"),
        HumanMessage("第二个问题"),
        AIMessage("第二个回答"),
    ]
    budget = count_message_tokens(history[3:])  # 够放下 a1 起的消息，但不能从 a1 或工具结果开始
    assert trim_history(history, budget) == history[4:]


def test_trim_history_keeps_current_turn_when_it_is_long():
    history = [*turn(1), HumanMessage("当前问题"), *[AIMessage("很长的工具往返" * 50)] * 3]
    assert trim_history(history, 10)[0].content == "当前问题"


def test_model_receives_trimmed_history(monkeypatch):
    monkeypatch.setattr(config, "HISTORY_TOKENS", 12)  # 每条短消息约 4 tokens，只够最近的 3 条
    model = fake(text_msg("a1"), text_msg("a2"), text_msg("a3"))
    agent = Agent(model, tools=[])
    for text in ["u1", "u2", "u3"]:
        agent.chat(text)
    assert [m.content for m in model.calls[2][1:]] == ["u2", "a2", "u3"]
    assert len(agent.history) == 6  # checkpointer 中仍是完整历史


def test_old_tool_results_are_cleared_but_recent_three_kept(monkeypatch):
    monkeypatch.setattr(config, "CONTEXT_CLEAR_TOKENS", 100)
    messages = [HumanMessage("问题")]
    for i in range(5):
        messages += [
            AIMessage("", tool_calls=[{"name": "read_material", "args": {"chunk": i}, "id": f"c{i}"}]),
            ToolMessage(f"第{i}块材料原文" * 30, tool_call_id=f"c{i}"),
        ]
    edit = _clear_old_tool_results().edits[0]
    edit.apply(messages, count_tokens=count_message_tokens)
    results = [m.content for m in messages if isinstance(m, ToolMessage)]
    assert all(r.startswith("[已清理") for r in results[:2])
    assert results[2:] == [f"第{i}块材料原文" * 30 for i in range(2, 5)]


def test_saved_history_keeps_cleared_tool_results(monkeypatch):
    monkeypatch.setattr(config, "CONTEXT_CLEAR_TOKENS", 50)
    big = "很长的日期说明" * 40

    @tool
    def get_current_date() -> str:
        """d"""
        return big

    model = fake(*[tool_msg("get_current_date", {}, id=f"t{i}") for i in range(4)], text_msg("ok"))
    agent = Agent(model, tools=[get_current_date])
    agent.chat("x")
    assert any(isinstance(m, ToolMessage) and m.content.startswith("[已清理") for m in model.calls[-1])
    assert all(m.content == big for m in agent.history if isinstance(m, ToolMessage))  # 只影响发给模型的内容


def test_context_overflow_gives_friendly_message():
    def respond(messages):
        raise ValueError("This model's maximum context length is 1000000 tokens, but you requested 1200000")

    assert Agent(FunctionModel(respond=respond), tools=[]).chat("x") == CONTEXT_OVERFLOW


def test_other_model_errors_still_raise():
    def respond(messages):
        raise ValueError("invalid api key")

    with pytest.raises(ValueError, match="invalid api key"):
        Agent(FunctionModel(respond=respond), tools=[]).chat("x")
