import json
from types import SimpleNamespace as NS

import pytest

from briefly import config
from briefly.agent import Agent
from briefly.skills import SKILLS
from briefly.tools import Tool


def text_msg(text: str):
    return NS(content=text, tool_calls=None)


def tool_msg(name: str, args: dict, id: str = "t1"):
    call = NS(id=id, function=NS(name=name, arguments=json.dumps(args)))
    return NS(content=None, tool_calls=[call])


class FakeLLM:
    """按顺序返回预设响应，并记录每次调用时收到的内容。"""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, messages, tools=None):
        self.calls.append({"messages": list(messages), "tools": tools})
        return self.responses.pop(0)


def date_tool(fail: bool = False) -> Tool:
    def func(**kw):
        if fail:
            raise RuntimeError("boom")
        return "2026-10-08 星期四"

    return Tool("get_current_date", "d", {"type": "object", "properties": {}}, func)


def test_prompts_exist_for_all_skills():
    for skill in SKILLS.values():
        assert skill.prompt.strip()


def test_system_prompt_contains_base_and_all_skills():
    prompt = Agent(FakeLLM()).system_prompt()
    assert "# 角色" in prompt
    for skill in SKILLS.values():
        assert f"（{skill.name}）" in prompt
    assert "本次任务" not in prompt


def test_system_message_comes_first_and_history_excludes_it():
    llm = FakeLLM(text_msg("ok"))
    agent = Agent(llm, tools=[])
    agent.chat("hi")
    assert llm.calls[0]["messages"][0]["role"] == "system"
    assert all(m["role"] != "system" for m in agent.history)


def test_forced_skill_is_added_to_system_prompt():
    llm = FakeLLM(text_msg("ok"))
    Agent(llm, tools=[]).chat("hi", skill="todo")
    system = llm.calls[0]["messages"][0]["content"]
    assert "本次任务" in system and "待办事项梳理" in system


def test_unknown_skill_rejected():
    with pytest.raises(ValueError):
        Agent(FakeLLM(), tools=[]).chat("hi", skill="nope")


def test_history_is_kept_across_turns():
    llm = FakeLLM(text_msg("第一条回复"), text_msg("第二条回复"))
    agent = Agent(llm, tools=[])
    agent.chat("总结这个")
    agent.chat("再短一点")
    msgs = llm.calls[1]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert msgs[1]["content"] == "总结这个"


def test_reset_clears_history():
    agent = Agent(FakeLLM(text_msg("a")), tools=[])
    agent.chat("x")
    agent.reset()
    assert agent.history == []


def test_tool_loop_runs_tool_and_returns_final_text():
    llm = FakeLLM(tool_msg("get_current_date", {}), text_msg("下周五是 10-16"))
    agent = Agent(llm, tools=[date_tool()])
    assert agent.chat("下周五是几号") == "下周五是 10-16"
    msgs = llm.calls[1]["messages"]
    assert msgs[-2]["tool_calls"][0]["function"]["name"] == "get_current_date"
    assert msgs[-1] == {"role": "tool", "tool_call_id": "t1", "content": "2026-10-08 星期四"}


def test_tool_spec_uses_openai_function_format():
    spec = date_tool().spec()
    assert spec["type"] == "function"
    assert spec["function"]["name"] == "get_current_date"


def test_tool_error_is_reported_to_model_not_raised():
    llm = FakeLLM(tool_msg("get_current_date", {}), text_msg("工具出错了"))
    agent = Agent(llm, tools=[date_tool(fail=True)])
    assert agent.chat("x") == "工具出错了"
    result = llm.calls[1]["messages"][-1]
    assert "工具执行失败" in result["content"] and "boom" in result["content"]


def test_unknown_tool_is_reported_as_error():
    llm = FakeLLM(tool_msg("nope", {}), text_msg("ok"))
    Agent(llm, tools=[]).chat("x")
    assert "未知工具" in llm.calls[1]["messages"][-1]["content"]


def test_bad_tool_arguments_are_reported_as_error():
    call = NS(id="t1", function=NS(name="get_current_date", arguments="{not json"))
    llm = FakeLLM(NS(content=None, tool_calls=[call]), text_msg("ok"))
    Agent(llm, tools=[date_tool()]).chat("x")
    assert "工具执行失败" in llm.calls[1]["messages"][-1]["content"]


def test_tool_rounds_are_capped(monkeypatch):
    monkeypatch.setattr(config, "MAX_TOOL_ROUNDS", 2)
    llm = FakeLLM(*[tool_msg("get_current_date", {}, id=f"t{i}") for i in range(5)])
    reply = Agent(llm, tools=[date_tool()]).chat("x")
    assert len(llm.calls) == 2
    assert "已中止" in reply


def test_trim_history_starts_at_user_message(monkeypatch):
    monkeypatch.setattr(config, "MAX_HISTORY", 4)
    agent = Agent(FakeLLM(), tools=[])
    agent.history = [
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t"}]},
        {"role": "tool", "tool_call_id": "t", "content": "r"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
    ]
    agent._trim_history()
    assert agent.history[0] == {"role": "user", "content": "u2"}
