"""模型响应缓存：白名单、键因素、事实性和外发授权、回答检查、引用、外发计数、日志、并发和清理。假模型计调用次数。"""
import json
import logging
import threading
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from pydantic import BaseModel
from sqlalchemy.orm import Session

from briefly import config, llm_cache, turn_timing
from briefly.agent import Agent
from briefly.api import create_app, visible_messages
from briefly.conflicts import _Judgments, classify
from briefly.db import create_db_engine
from briefly.intent import IntentDecision
from briefly.knowledge.assistant import KnowledgeAssistant
from briefly.knowledge.ingestion import parse_cache_stat
from briefly.knowledge.models import KnowledgeImportMetrics
from briefly.knowledge.qa import KnowledgeQA
from briefly.llm import CachedCall, create_chat_model
from briefly.llm_cache import (LlmCacheEntry, ResponseCache, active_scope, bind_scope, cache_key, cache_stats, model_identity,
                               record_cache_event, release_scope)
from briefly.materials import MaterialStore
from briefly.summarize import synthesize
from briefly.turn_timing import provider_cache_tokens
from fakes import FunctionModel, fake, structured, text_msg, tool_msg
from test_knowledge_index import indexed  # noqa: F401  夹具
from test_knowledge_retrieval import add, setup

LEAVE = "员工年假为每年5天，须提前3天申请。"
SICK = "病假需提交医院证明，每年累计不超过10天。"
GOOD = "【可答性：可回答】员工年假每年5天〔依据1〕。"
SECRET_QUESTION = "年假有几天"


@pytest.fixture(autouse=True)
def floor(monkeypatch):
    monkeypatch.setattr(config, "INTENT_KB_THRESHOLD", 0.5)


@pytest.fixture
def scope(tmp_path, monkeypatch):
    """模拟一次已登录请求：开关打开，缓存和主体绑定到当前上下文。"""
    monkeypatch.setattr(config, "LLM_CACHE", True)
    engine = create_db_engine("sqlite:///" + (tmp_path / "cache.db").as_posix())
    cache = ResponseCache(engine)
    token = bind_scope(cache, "local")
    yield cache
    release_scope(token)
    engine.dispose()


def answering(text=GOOD, **metadata):
    return FunctionModel(respond=lambda messages: AIMessage(text, response_metadata=dict(metadata)), calls=[])


def knowledge(indexed, model, *contents):
    store, svc, lifecycle, retrieval = setup(indexed)
    kb = svc.create("制度库")
    for i, content in enumerate(contents or (LEAVE,)):
        add(store, svc, lifecycle, kb, f"制度{i + 1}", content)
    svc.set_egress(kb.id, True)
    return SimpleNamespace(qa=KnowledgeQA(retrieval, lambda: model), kb=kb, svc=svc, store=store, lifecycle=lifecycle, retrieval=retrieval)


def entries(cache) -> int:
    return cache.entries()


# ---- 命中
def test_second_identical_question_does_not_call_model(indexed, scope):
    model = answering()
    env = knowledge(indexed, model)
    first = env.qa.answer(env.kb.id, SECRET_QUESTION)
    second = env.qa.answer(env.kb.id, SECRET_QUESTION)
    assert len(model.calls) == 1
    assert first["model_called"] is True and "from_cache" not in first
    assert second["model_called"] is False and second["from_cache"] is True
    assert second["answer"] == first["answer"] and second["status"] == first["status"] == "answered"


def test_hit_restores_references_and_reports_zero_egress(indexed, scope):
    env = knowledge(indexed, answering())
    first = env.qa.answer(env.kb.id, SECRET_QUESTION)
    second = env.qa.answer(env.kb.id, SECRET_QUESTION)
    assert second["references"] == first["references"] and second["references"][0]["title"] == "制度1"
    assert second["citations"] == first["citations"]
    assert first["egress"]["evidence_sent"] == 1
    egress = second["egress"]
    assert egress["evidence_sent"] == 0 and egress["workspace_sent"] == 0 and egress["redactions"] == {}
    assert egress["from_cache"] is True and egress["cached_evidence"] == 1


def test_shortcut_reply_metadata_keeps_references_and_marks_cache(indexed, scope):
    """命中后 response_metadata.knowledge 里的引用不能丢，网页读到的外发计数如实。"""
    model = answering()
    env = knowledge(indexed, model)
    funnel = SimpleNamespace(classify=lambda *a, **k: IntentDecision("knowledge", 2, "测试"))
    agent = Agent(model, tools=[], funnel=funnel, knowledge=KnowledgeAssistant(env.retrieval, lambda: model))
    assert agent.chat(SECRET_QUESTION, kb_id=env.kb.id) == GOOD
    assert agent.chat(SECRET_QUESTION, kb_id=env.kb.id) == GOOD
    assert len(model.calls) == 1
    first, second = [m for m in visible_messages(agent.history) if m.role == "assistant"]
    assert first.knowledge["egress"]["evidence_sent"] == 1 and "from_cache" not in first.knowledge
    assert second.knowledge["from_cache"] is True and second.knowledge["egress"]["evidence_sent"] == 0
    assert second.knowledge["references"] == first.knowledge["references"] and second.knowledge["references"]
    assert not second.system_reply


# ---- 键因素
def key_for(*, model=None, messages=(("system", "提示"), ("user", "问题")), principal="local", **kwargs):
    return cache_key("synthesize", model or answering(), list(messages), principal, **kwargs)


def test_every_model_parameter_is_in_the_key(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "not-a-real-key")
    monkeypatch.setattr(config, "MODEL", "model-a")
    monkeypatch.setattr(config, "BASE_URL", "http://localhost:1/v1")
    monkeypatch.setattr(config, "MAX_TOKENS", 100)
    monkeypatch.setattr(config, "THINKING", False)
    def key():
        return key_for(model=create_chat_model())
    base = key()
    assert key() == base
    for name, value in (("MODEL", "model-b"), ("BASE_URL", "http://localhost:2/v1"), ("MAX_TOKENS", 200), ("THINKING", True)):
        with monkeypatch.context() as m:
            m.setattr(config, name, value)
            assert key() != base, name
    model = create_chat_model()
    assert key_for(model=model.model_copy(update={"temperature": 0.9})) != base
    assert "not-a-real-key" not in json.dumps(model_identity(model), default=str)


def test_message_schema_tool_and_principal_change_the_key():
    model = answering()

    class Other(BaseModel):
        value: int

    base = key_for(model=model, schema=_Judgments)
    assert key_for(model=model, schema=_Judgments) == base
    assert key_for(model=model, schema=Other) != base
    assert key_for(model=model, schema=_Judgments, messages=(("system", "改过的提示"), ("user", "问题"))) != base
    assert key_for(model=model, schema=_Judgments, messages=(("system", "提示"), ("user", "问题2"))) != base
    assert key_for(model=model, schema=_Judgments, principal="other") != base
    assert key_for(model=model, schema=_Judgments, tools=[{"name": "t"}]) != base


def test_changed_evidence_misses(indexed, scope):
    """依据内容不同就不能命中：不做语义相似缓存。"""
    model = answering()
    env = knowledge(indexed, model)
    env.qa.answer(env.kb.id, SECRET_QUESTION)
    add(env.store, env.svc, env.lifecycle, env.kb, "补充制度", "年假可以结转到下一年。")
    env.qa.answer(env.kb.id, SECRET_QUESTION)
    assert len(model.calls) == 2


def test_new_version_with_same_text_misses(scope):
    """知识版本变了，即使正文一字不差也重新生成：依据的身份也是依据的一部分。"""
    model = answering()
    item = dict(document_id="d1", version_id="v1")
    first = KnowledgeQA._answer_call(model, "依据包", [item], ())
    second = KnowledgeQA._answer_call(model, "依据包", [dict(item, version_id="v2")], ())
    assert first.key != second.key
    assert KnowledgeQA._answer_call(model, "依据包", [item], ()).key == first.key


def test_changed_prompt_misses(indexed, scope, monkeypatch):
    model = answering()
    env = knowledge(indexed, model)
    env.qa.answer(env.kb.id, SECRET_QUESTION)
    monkeypatch.setattr("briefly.agent.build_system_prompt", lambda *a, **k: "改过的提示词")
    env.qa.answer(env.kb.id, SECRET_QUESTION)
    assert len(model.calls) == 2


def test_other_model_misses(indexed, scope):
    class Named(FunctionModel):
        model_name: str = "model-a"

    first = Named(respond=lambda m: AIMessage(GOOD), calls=[])
    second = Named(respond=lambda m: AIMessage(GOOD), calls=[], model_name="model-b")
    env = knowledge(indexed, first)
    env.qa.answer(env.kb.id, SECRET_QUESTION)
    env.qa.model_factory = lambda: second
    env.qa.answer(env.kb.id, SECRET_QUESTION)
    assert len(first.calls) == 1 and len(second.calls) == 1


def test_other_principal_cannot_read_the_answer(indexed, scope):
    model = answering()
    env = knowledge(indexed, model)
    env.qa.answer(env.kb.id, SECRET_QUESTION)
    token = bind_scope(scope, "someone-else")
    try:
        assert env.qa.answer(env.kb.id, SECRET_QUESTION)["model_called"] is True
    finally:
        release_scope(token)
    assert len(model.calls) == 2


def test_next_day_misses(indexed, scope, monkeypatch):
    """系统提示带当前时间，只把日期放进键：同一天可命中，隔天重新生成，问题里的“今天”不会用错。"""
    from datetime import date
    model = answering()
    env = knowledge(indexed, model)
    monkeypatch.setattr("briefly.clock.local_today", lambda: date(2026, 11, 5))
    env.qa.answer(env.kb.id, SECRET_QUESTION)
    env.qa.answer(env.kb.id, SECRET_QUESTION)
    assert len(model.calls) == 1
    monkeypatch.setattr("briefly.clock.local_today", lambda: date(2026, 11, 6))
    env.qa.answer(env.kb.id, SECRET_QUESTION)
    assert len(model.calls) == 2


# ---- 外发授权
def test_revoked_egress_never_returns_cached_answer(indexed, scope):
    model = answering()
    env = knowledge(indexed, model)
    env.qa.answer(env.kb.id, SECRET_QUESTION)
    assert entries(scope) == 1
    env.svc.set_egress(env.kb.id, False)
    blocked = env.qa.answer(env.kb.id, SECRET_QUESTION)
    assert blocked["status"] == "external_authorization_required" and GOOD not in blocked["answer"]
    assert blocked["model_called"] is False and "from_cache" not in blocked
    # 重新开启后的约定：依据完全一致时可以命中（答案只来自同一批依据，这次也没有外发），外发计数为 0 并标明
    env.svc.set_egress(env.kb.id, True)
    again = env.qa.answer(env.kb.id, SECRET_QUESTION)
    assert again["from_cache"] is True and again["egress"]["evidence_sent"] == 0
    assert len(model.calls) == 1


def test_lookup_happens_after_authorization_and_bundle(indexed, scope, monkeypatch):
    env = knowledge(indexed, answering())
    env.qa.answer(env.kb.id, SECRET_QUESTION)
    order = []
    bundle, lookup = KnowledgeQA.bundle, CachedCall.lookup
    monkeypatch.setattr(KnowledgeQA, "bundle", lambda self, *a, **k: (order.append("bundle"), bundle(self, *a, **k))[1])
    monkeypatch.setattr(CachedCall, "lookup", lambda self: (order.append("lookup"), lookup(self))[1])
    env.qa.answer(env.kb.id, SECRET_QUESTION)
    assert order == ["bundle", "lookup"]


# ---- 回答检查
@pytest.mark.parametrize("model", [
    pytest.param(lambda: answering("年假5天〔依据1〕。"), id="no_declared_state_needs_review"),
    pytest.param(lambda: answering("【可答性：可回答】年假5天。"), id="no_citation_needs_review"),
    pytest.param(lambda: answering("【可答性：可回答】年假5天〔依据9〕。"), id="bad_citation_needs_review"),
    pytest.param(lambda: answering(GOOD, finish_reason="length"), id="truncated"),
    pytest.param(lambda: answering("【可答性：可回答】年假每年380天〔依据1〕。"), id="number_flag_after_check"),
])
def test_unclean_results_are_not_cached(indexed, scope, model):
    model = model()
    env = knowledge(indexed, model)
    env.qa.answer(env.kb.id, SECRET_QUESTION)
    assert entries(scope) == 0
    calls = len(model.calls)
    assert env.qa.answer(env.kb.id, SECRET_QUESTION)["model_called"] is True
    assert len(model.calls) == calls * 2


def test_failed_call_is_not_cached(indexed, scope):
    def boom(messages):
        raise RuntimeError("接口出错")
    model = FunctionModel(respond=boom, calls=[])
    env = knowledge(indexed, model)
    with pytest.raises(RuntimeError):
        env.qa.answer(env.kb.id, SECRET_QUESTION)
    assert entries(scope) == 0


def test_hit_still_shows_current_warnings(indexed, scope, monkeypatch):
    """提醒按本轮依据包重新算：命中不会盖掉后来才出现的提醒。"""
    env = knowledge(indexed, answering())
    env.qa.answer(env.kb.id, SECRET_QUESTION)
    bundle = KnowledgeQA.bundle
    monkeypatch.setattr(KnowledgeQA, "bundle", lambda self, *a, **k: (lambda b: {**b, "warnings": ["范围内有新提醒"]})(bundle(self, *a, **k)))
    second = env.qa.answer(env.kb.id, SECRET_QUESTION)
    assert second["from_cache"] is True and second["status"] == "needs_review" and second["warnings"] == ["范围内有新提醒"]


# ---- 智能体循环不走缓存
def test_agent_loop_never_uses_cache(indexed, scope):
    model = fake(text_msg("第一次"), text_msg("第二次"))
    agent = Agent(model, tools=[])
    assert agent.chat("你觉得这周工作怎么安排合适") == "第一次"
    assert agent.chat("你觉得这周工作怎么安排合适") == "第二次"
    assert len(model.calls) == 2 and entries(scope) == 0


def test_assistant_path_with_search_tool_is_not_cached(indexed, scope):
    model = fake(tool_msg("search_knowledge", {"questions": ["年假有几天"]}, id="s1"), text_msg("年假5天〔依据1〕"),
                 tool_msg("search_knowledge", {"questions": ["年假有几天"]}, id="s2"), text_msg("年假5天〔依据1〕"))
    env = knowledge(indexed, model)
    agent = Agent(model, tools=[], knowledge=KnowledgeAssistant(env.retrieval, lambda: model))
    agent.chat("帮我查查年假的规定", kb_id=env.kb.id)
    agent.chat("帮我查查年假的规定", kb_id=env.kb.id, thread_id="other")
    assert len(model.calls) == 4 and entries(scope) == 0


def test_only_whitelisted_sites_are_accepted(scope):
    for site in ("agent", "compaction", "handoff_todos", "extract"):
        with pytest.raises(ValueError, match="白名单"):
            CachedCall(site, answering(), [("user", "x")])


# ---- 开关
def test_switch_off_behaves_like_no_cache(indexed, scope, monkeypatch):
    monkeypatch.setattr(config, "LLM_CACHE", False)
    model = answering()
    env = knowledge(indexed, model)
    first, second = env.qa.answer(env.kb.id, SECRET_QUESTION), env.qa.answer(env.kb.id, SECRET_QUESTION)
    assert len(model.calls) == 2
    assert first["model_called"] and second["model_called"] and "from_cache" not in second
    assert second["egress"]["evidence_sent"] == 1
    assert entries(scope) == 0
    assert cache_stats(scope.engine)["caches"] == []


def test_no_request_scope_means_no_cache(indexed, monkeypatch):
    """定时任务、后台线程、命令行没有当前主体：不缓存，而不是落到一个默认主体上。"""
    monkeypatch.setattr(config, "LLM_CACHE", True)
    assert active_scope() is None
    model = answering()
    env = knowledge(indexed, model)
    env.qa.answer(env.kb.id, SECRET_QUESTION)
    env.qa.answer(env.kb.id, SECRET_QUESTION)
    assert len(model.calls) == 2


def test_new_thread_without_context_gets_no_scope(scope):
    seen = []
    thread = threading.Thread(target=lambda: seen.append(active_scope()))
    thread.start()
    thread.join()
    assert active_scope() is not None and seen == [None]


# ---- 其他调用点
SUMMARY = "项目按期完成"


def test_synthesis_cached_only_when_checks_pass_and_retry_bypasses(scope):
    model = answering(SUMMARY)
    first = synthesize(model, "系统提示", "总结一下", [])
    second = synthesize(model, "系统提示", "总结一下", [])
    assert len(model.calls) == 1 and first.text == second.text
    assert first.usage.calls == 1 and second.usage.calls == 0 and second.usage.input_tokens == 0

    replies = iter(["有问题的初稿", "改好的稿子"])
    rewrite = FunctionModel(respond=lambda m: AIMessage(next(replies)), calls=[])
    result = synthesize(rewrite, "另一个提示", "总结", [], check=lambda text: ["问题"] if "有问题" in text else [])
    assert result.text == "改好的稿子" and len(rewrite.calls) == 2 and entries(scope) == 1  # 只有上面那条
    replies = iter(["有问题的初稿", "改好的稿子"])
    synthesize(rewrite, "另一个提示", "总结", [], check=lambda text: ["问题"] if "有问题" in text else [])
    assert len(rewrite.calls) == 4  # 没通过检查的初稿不缓存，重写也不缓存


def test_truncated_synthesis_is_not_cached(scope):
    model = answering(SUMMARY, finish_reason="length")
    synthesize(model, "系统提示", "总结一下", [])
    synthesize(model, "系统提示", "总结一下", [])
    assert len(model.calls) == 2 and entries(scope) == 0


class Draft(BaseModel):
    title: str
    score: int = 0


def structured_model(**args):
    return FunctionModel(respond=lambda m: structured("Draft", **args), calls=[])


def test_structured_result_round_trips_as_schema(scope):
    model = structured_model(title="标题", score=3)
    calls = [CachedCall("evidence_compose", model, [("user", "输入")], schema=Draft) for _ in range(2)]
    first = calls[0].structured()
    calls[0].accept()
    second = calls[1].structured()
    assert isinstance(second, Draft) and second == first and calls[1].hit and len(model.calls) == 1


def test_structured_not_accepted_is_not_cached(scope):
    model = structured_model(title="标题")
    call = CachedCall("evidence_compose", model, [("user", "输入")], schema=Draft)
    call.structured()
    call.reject()
    assert entries(scope) == 0


def conflict(**kwargs):
    from briefly.conflicts import FactConflict
    return FactConflict(**{**dict(subject="项目", attribute="date", fact_ids=["a", "b"], values=["1日", "2日"], numbers=[1, 2],
                                  origins=["material", "material"], subjects=["项目"]), **kwargs})


def test_conflict_judgment_hits_only_after_evidence_verified(scope, monkeypatch):
    monkeypatch.setattr("briefly.conflicts._member_quotes", lambda c, items, brief: [])
    grounded = dict(index=1, resolution="unresolved", explanation="", evidence="", current=0)
    model = FunctionModel(respond=lambda m: structured("_Judgments", judgments=[grounded]), calls=[])
    assert classify(model, [conflict()], []) == 1
    assert classify(model, [conflict()], []) == 0  # 命中：调用次数如实是 0
    assert len(model.calls) == 1

    unverifiable = dict(index=1, resolution="superseded", explanation="说明", evidence="原文没有这句", current=1)
    other = FunctionModel(respond=lambda m: structured("_Judgments", judgments=[unverifiable]), calls=[])
    other_conflict = lambda: conflict(subject="另一个项目", subjects=["另一个项目"])  # noqa: E731
    classify(other, [other_conflict()], [])
    classify(other, [other_conflict()], [])
    assert len(other.calls) == 2  # 依据核对不上的判断不缓存


# ---- 日志
def test_logs_only_contain_key_hash(indexed, scope, caplog):
    env = knowledge(indexed, answering())
    with caplog.at_level(logging.DEBUG, logger="briefly"):
        env.qa.answer(env.kb.id, SECRET_QUESTION)
        env.qa.answer(env.kb.id, SECRET_QUESTION)
    assert "模型缓存命中" in caplog.text
    for secret in (SECRET_QUESTION, LEAVE, "员工年假每年5天"):
        for record in caplog.records:
            if record.name in {"briefly.llm", "briefly.llm_cache", "briefly.agent"}:
                assert secret not in record.getMessage()


def test_failed_write_only_logs(indexed, scope, monkeypatch, caplog):
    model = answering()
    env = knowledge(indexed, model)
    monkeypatch.setattr(Session, "merge", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("磁盘满了，内容不能进日志")))
    with caplog.at_level(logging.WARNING, logger="briefly"):
        assert env.qa.answer(env.kb.id, SECRET_QUESTION)["status"] == "answered"
    assert "写模型缓存失败" in caplog.text and "磁盘满了" not in caplog.text


def test_stored_answer_is_the_redacted_text(indexed, scope):
    model = answering("【可答性：可回答】年假5天，联系 hr@example.com〔依据1〕。")
    env = knowledge(indexed, model)
    first = env.qa.answer(env.kb.id, SECRET_QUESTION)
    assert "hr@example.com" not in first["answer"]
    with Session(scope.engine) as s:
        assert "hr@example.com" not in s.query(LlmCacheEntry).one().value["answer"]
    assert env.qa.answer(env.kb.id, SECRET_QUESTION)["answer"] == first["answer"]


def test_values_are_json_not_pickle(indexed, scope):
    env = knowledge(indexed, answering())
    env.qa.answer(env.kb.id, SECRET_QUESTION)
    with Session(scope.engine) as s:
        row = s.query(LlmCacheEntry).one()
    assert row.value == {"answer": GOOD} and row.principal_id == "local" and row.site == "knowledge_answer"
    assert isinstance(LlmCacheEntry.__table__.c.value.type, llm_cache.JSON)


# ---- 并发和清理
def test_concurrent_writes_to_same_key_do_not_raise(scope):
    errors = []

    def write(i):
        try:
            assert scope.put("k" * 64, "synthesize", "local", dict(text=f"结果{i}")) is True
            record_cache_event(scope.engine, "llm.synthesize", stores=1)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and entries(scope) == 1
    assert scope.get("k" * 64).value["text"].startswith("结果")
    assert cache_stats(scope.engine)["caches"][0]["stores"] == 16


def test_expired_entries_miss_and_get_pruned(scope, monkeypatch):
    scope.put("a" * 64, "synthesize", "local", dict(text="旧"))
    with Session(scope.engine) as s, s.begin():
        s.get(LlmCacheEntry, "a" * 64).expires_at = datetime.now() - timedelta(seconds=1)
    assert scope.get("a" * 64) is None
    assert entries(scope) == 1
    scope.prune()
    assert entries(scope) == 0


def test_entry_limit_drops_least_recently_used(scope, monkeypatch):
    monkeypatch.setattr(config, "LLM_CACHE_MAX_ENTRIES", 2)
    now = datetime.now()
    for i, name in enumerate("abc"):
        scope.put(name * 64, "synthesize", "local", dict(text=name))
        with Session(scope.engine) as s, s.begin():
            s.get(LlmCacheEntry, name * 64).last_used_at = now + timedelta(seconds=i)
    assert entries(scope) == 2
    assert scope.get("a" * 64) is None and scope.get("c" * 64) is not None


# ---- 统计
def test_stats_by_site_with_saved_tokens_and_time(indexed, scope):
    env = knowledge(indexed, answering())
    for _ in range(3):
        env.qa.answer(env.kb.id, SECRET_QUESTION)
    stat = next(c for c in cache_stats(scope.engine)["caches"] if c["name"] == "llm.knowledge_answer")
    assert stat["hits"] == 2 and stat["misses"] == 1 and stat["stores"] == 1 and stat["hit_rate"] == round(2 / 3, 4)
    assert stat["saved_input_tokens"] == 200 and stat["saved_output_tokens"] == 20  # 假模型每次 100/10


def test_rejected_results_are_counted(indexed, scope):
    env = knowledge(indexed, answering("年假5天〔依据1〕。"))
    env.qa.answer(env.kb.id, SECRET_QUESTION)
    stat = next(c for c in cache_stats(scope.engine)["caches"] if c["name"] == "llm.knowledge_answer")
    assert stat["rejected"] == 1 and stat["stores"] == 0


def test_parse_cache_stat_reads_import_metrics_and_skips_forced(tmp_path):
    engine = create_db_engine("sqlite:///" + (tmp_path / "m.db").as_posix())
    with engine.connect() as connection:  # 指标行的外键指向入库任务，这里只测统计
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        with Session(connection) as s:
            for i, values in enumerate((dict(cache_hit=True), dict(cache_hit=False), dict(cache_hit=False, force_reparse=True), {})):
                s.add(KnowledgeImportMetrics(import_id=f"job{i}", values=values))
            s.commit()
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    assert parse_cache_stat(engine) == dict(name="parse.document", hits=1, misses=1)
    engine.dispose()


def test_extraction_cache_reports_hits_and_misses(tmp_path):
    from briefly.context_store import ContextStore
    from briefly.summarize import Source, extract_items
    engine = create_db_engine("sqlite:///" + (tmp_path / "e.db").as_posix())
    store = ContextStore(engine)
    model = FunctionModel(respond=lambda m: structured("Extraction", items=[]), calls=[])
    sources = [Source(material_id=1, title="材料", chunk=1, content="内容")]
    extract_items(model, sources, cache=store)
    extract_items(model, sources, cache=store)
    stat = next(c for c in cache_stats(engine)["caches"] if c["name"] == "extract.chunk")
    assert stat["hits"] == 1 and stat["misses"] == 1
    engine.dispose()


# ---- 接到请求上
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LLM_CACHE", True)
    engine = create_db_engine("sqlite:///" + (tmp_path / "app.db").as_posix())
    app = create_app(MaterialStore(engine), model_factory=answering)
    with TestClient(app) as c:
        yield c
    engine.dispose()


def test_principal_comes_from_current_principal_in_sync_routes(client):
    """同步路由在线程池里运行，作用域要跟过去；主体来自认证中间件写入的 current_principal。"""
    client.app.add_api_route("/_probe", lambda: dict(principal=active_scope().principal_id))
    client.app.middleware_stack = None  # 让路由表变更生效
    assert client.get("/_probe").json() == {"principal": "local"}
    assert active_scope() is None  # 请求结束后作用域不留在当前上下文


def test_stats_endpoint_is_read_only_and_requires_login(client, monkeypatch):
    body = client.get("/cache/stats").json()
    assert body["llm_cache"]["enabled"] is True and body["llm_cache"]["entries"] == 0
    assert {c["name"] for c in body["caches"]} >= {"parse.document", "embedding.workspace"}
    assert "不代表" in body["note"]
    assert client.post("/cache/stats").status_code == 405
    monkeypatch.setattr(config, "ACCESS_TOKEN", "test-token")
    assert client.get("/cache/stats").status_code == 401


def test_cache_table_is_created_by_create_all(tmp_path):
    engine = create_db_engine("sqlite:///" + (tmp_path / "fresh.db").as_posix())
    from sqlalchemy import inspect
    assert {"llm_cache_entries", "cache_stats"} <= set(inspect(engine).get_table_names())
    engine.dispose()


# ---- 耗时拆分和服务商缓存字段
def test_turn_timing_splits_and_records_numbers_only(indexed, scope):
    model = answering()
    env = knowledge(indexed, model)
    funnel = SimpleNamespace(classify=lambda *a, **k: IntentDecision("knowledge", 2, "测试"))
    agent = Agent(model, tools=[], funnel=funnel, knowledge=KnowledgeAssistant(env.retrieval, lambda: model))
    agent.chat(SECRET_QUESTION, kb_id=env.kb.id)
    agent.chat(SECRET_QUESTION, kb_id=env.kb.id)
    rows = agent.context_store.turns(thread_id=agent.thread_id)
    assert len(rows) == 2  # 捷径内部的智能体不重复记一行
    hit, miss = (json.loads(r.note) for r in rows)
    for summary in (hit, miss):
        assert set(summary) >= {"total_ms", "intent_ms", "retrieval_ms", "model_ms", "check_ms"}
    assert miss["model_calls"] == 1 and miss["input_tokens"] == 100 and rows[1].run_id
    assert hit["model_calls"] == 0 and hit["input_tokens"] == 0 and hit["model_ms"] == 0
    assert SECRET_QUESTION not in rows[0].note and LEAVE not in rows[0].note
    # 这一行不算模型调用：/context、token 校准和按调用读的 usage 都看不到
    assert all(r.task_type != "turn" for r in agent.context_store.usage(thread_id=agent.thread_id))
    assert rows[0].estimated_tokens == 0


def test_timing_noop_outside_turn():
    with turn_timing.span("model"):
        pass
    turn_timing.add_usage(AIMessage("x"))
    assert turn_timing.current() is None


def test_deepseek_cache_fields_are_read_from_real_response_shape(monkeypatch):
    """ChatOpenAI 把 DeepSeek 响应的 usage 原样放进 llm_output.token_usage，框架再合并到 response_metadata。"""
    monkeypatch.setattr(config, "API_KEY", "not-a-real-key")
    monkeypatch.setattr(config, "MODEL", "deepseek-test")
    monkeypatch.setattr(config, "BASE_URL", "http://localhost:1/v1")
    usage = dict(prompt_tokens=120, completion_tokens=8, total_tokens=128, prompt_cache_hit_tokens=100, prompt_cache_miss_tokens=20)
    result = create_chat_model()._create_chat_result(dict(
        model="deepseek-test", usage=usage, choices=[dict(message=dict(role="assistant", content="好"), finish_reason="stop")]))
    message = result.generations[0].message
    message.response_metadata = {**result.llm_output, **message.response_metadata}
    assert provider_cache_tokens(message) == (100, 20)
    assert provider_cache_tokens(AIMessage("x", usage_metadata=dict(input_tokens=5, output_tokens=1, total_tokens=6,
                                                                    input_token_details=dict(cache_read=3)))) == (3, 0)
    assert provider_cache_tokens(AIMessage("x")) == (0, 0)
