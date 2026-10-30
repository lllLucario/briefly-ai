"""意图日志与纠正（intent_log.py、chat 的 skip_funnel）：判断、实际能力、纠正都记在本机业务库，原文默认不存。"""
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from briefly import config
from briefly.agent import Agent
from briefly.api import create_app
from briefly.db import create_db_engine
from briefly.intent import IntentFunnel, TurnState
from briefly.intent_log import IntentLog, IntentLogEntry, main
from briefly.materials import MaterialStore
from fakes import fake, text_msg
from test_intent import EXAMPLES, KeywordEncoder

sys.path.insert(0, str(Path(__file__).parent / "eval"))
from intent_eval import load_cases, state_of  # noqa: E402


@pytest.fixture
def engine():
    engine = create_db_engine("sqlite://")
    yield engine
    engine.dispose()


def agent_with_log(engine, *responses):
    return Agent(fake(*responses), tools=[], store=MaterialStore(engine))


def test_direct_and_agent_turns_are_logged_without_text_by_default(engine):
    agent = agent_with_log(engine, text_msg("【一句话总结】登录联调已完成"))
    agent.chat("你好")
    agent.chat("帮我看看这段：今天完成了登录联调")
    greeting, handed = IntentLog(engine).entries()
    assert (greeting.intent, greeting.layer, greeting.resolved, greeting.reason) == ("greeting", 1, "greeting", "打招呼或询问能力")
    assert (handed.intent, handed.layer, handed.resolved) == ("agent", 3, "summarize")  # 轮次收尾写回的实际能力
    assert greeting.thread_id == handed.thread_id == agent.thread_id
    # 原文和规则层命中的原文片段都不存，只存长度
    assert (greeting.text, greeting.matched, greeting.text_length) == (None, None, 2)
    assert handed.text is None and handed.text_length == len("帮我看看这段：今天完成了登录联调")
    assert handed.state == dict(has_materials=False, kb_id=None, last_intent="greeting",
                                has_previous_reply=False, earlier_materials=False)
    assert not greeting.corrected and not handed.skip_funnel


def test_text_switch_and_log_switch(engine, monkeypatch):
    agent = agent_with_log(engine)
    monkeypatch.setattr(config, "INTENT_LOG_TEXT", True)
    agent.chat("你好")
    assert (IntentLog(engine).entries()[0].text, IntentLog(engine).entries()[0].matched) == ("你好", "你好")
    monkeypatch.setattr(config, "INTENT_LOG", False)
    agent.chat("你好")
    assert len(IntentLog(engine).entries()) == 1


def test_skip_funnel_goes_to_assistant_and_marks_the_original_corrected(engine):
    agent = agent_with_log(engine, text_msg("你好！今天需要处理什么？"))
    agent.chat("你好")
    original = agent.history[0]
    assert original.additional_kwargs["intent"]["direct"] is True
    reply = agent.chat("你好", skip_funnel=True, corrects=original.id)
    assert reply == "你好！今天需要处理什么？"  # 进了助理，不再是问候的固定答复
    history = agent.history
    assert [m.text for m in history] == ["你好", history[1].text, "你好", reply]  # 原回复保留，结果追加在后面
    intent = history[2].additional_kwargs["intent"]
    assert (intent["intent"], intent["skip_funnel"], intent["corrects"]) == ("agent", True, original.id)
    assert "direct" not in intent
    first, resent = IntentLog(engine).entries()
    assert first.corrected and first.message_id == original.id
    assert resent.skip_funnel and resent.corrects == original.id and not resent.corrected


def test_failed_turn_is_still_logged(engine):
    agent = Agent(fake(), tools=[], store=MaterialStore(engine))  # 没有预设回复，调用模型时报错
    with pytest.raises(Exception):
        agent.chat("帮我看看这段：今天完成了登录联调")
    (entry,) = IntentLog(engine).entries()
    assert (entry.intent, entry.resolved) == ("agent", None)


def test_funnel_scores_are_recorded_even_when_below_threshold():
    """调参要看差一点没过门槛的分数：未达门槛交给助理时，语义层第一名和知识库路由相似度也记下。"""
    low = IntentFunnel(KeywordEncoder, EXAMPLES, threshold=0.99, margin=0.05, kb_probe=lambda kb, text: 0.3)
    low.kb_threshold = 0.5  # 假编码器没有模型身份，显式给定下限（按指纹取值见 test_relevance_floor）
    d = low.classify("年假有几天", TurnState(kb_id="kb"))
    assert d.intent == "agent" and d.kb_score == 0.3
    assert d.semantic["intent"] == "knowledge" and d.semantic["score"] > 0.9 and d.semantic["gap"] > 0
    assert d.to_dict()["semantic"] == d.semantic
    assert IntentFunnel().classify("你好").semantic is None  # 规则层结束时后两层没有运行


def test_export_matches_intent_eval_case_format(engine, monkeypatch, tmp_path, capsys):
    agent = agent_with_log(engine, text_msg("好的"), text_msg("助理回答"))
    agent.chat("没存原文的一句")
    monkeypatch.setattr(config, "INTENT_LOG_TEXT", True)
    agent.chat("你好")
    first = agent.history[2]
    agent.chat("你好", skip_funnel=True, corrects=first.id)
    cases, skipped = IntentLog(engine).export_cases()
    assert skipped == 1  # 没存原文的记录不能当用例；纠正重发不是漏斗判断，不导出
    (case,) = cases
    assert case["text"] == "你好" and case["expected_intent"] == "" and case["acceptable_intents"] == []
    assert case["logged"]["intent"] == "greeting" and case["logged"]["corrected"] is True
    assert "纠正" in case["note"]

    monkeypatch.setattr(config, "DATABASE_URL", "sqlite:///" + (tmp_path / "app.db").as_posix())
    file_engine = create_db_engine()
    IntentLog(file_engine).record("t1", "m1", "年假有几天", dict(intent="agent", layer=3, reason="r"),
                                  TurnState(kb_id="kb", has_previous_reply=True, last_intent="knowledge"))
    file_engine.dispose()
    out = tmp_path / "cases.jsonl"
    assert main(["export", "-o", str(out)]) == 0
    assert "导出 1 条" in capsys.readouterr().err
    (loaded,) = load_cases(out)
    assert loaded == json.loads(out.read_text(encoding="utf-8"))
    state = state_of(loaded, "kb-eval")  # intent_eval 能直接按记录时的会话状态重放
    assert (state.kb_id, state.has_previous_reply, state.last_intent) == ("kb-eval", True, "knowledge")


def test_chat_api_skip_funnel_and_session_knowledge_base(engine):
    model = fake(text_msg("助理回答"))
    client = TestClient(create_app(MaterialStore(engine), agent_factory=lambda s: Agent(model, tools=[], store=s)))
    with client:
        first = client.post("/chat", json={"text": "你好"}).json()
        thread = first["thread_id"]
        assert first["intent"]["direct"] is True
        detail = client.get(f"/chat/sessions/{thread}").json()
        user = detail["messages"][0]
        assert user["message_id"] and detail["kb_id"] is None
        again = client.post("/chat", json={"text": "你好", "thread_id": thread, "skip_funnel": True,
                                           "corrects": user["message_id"]}).json()
        assert again["reply"] == "助理回答" and again["intent"]["skip_funnel"] is True
        detail = client.get(f"/chat/sessions/{thread}").json()
        assert [m["text"] for m in detail["messages"]][-2:] == ["你好", "助理回答"]
        assert detail["messages"][2]["intent"]["corrects"] == user["message_id"]
    with Session(engine) as s:
        assert s.query(IntentLogEntry).filter_by(message_id=user["message_id"]).one().corrected

