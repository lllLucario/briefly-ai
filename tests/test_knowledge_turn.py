"""知识对话管线统一（WP2）：相关度下限、多轮历史不带旧原文、助理路径外发计数、引用由程序确定。"""
from types import SimpleNamespace

import pytest

from langchain_core.messages import ToolMessage

from briefly import config
from briefly.agent import Agent
from briefly.api import visible_messages
from briefly.knowledge.assistant import KnowledgeAssistant
from briefly.knowledge.qa import KnowledgeQA
from briefly.knowledge.retrieval import RetrievalService
from fakes import fake, text_msg, tool_msg
from test_knowledge_index import indexed  # noqa: F401  夹具
from test_knowledge_retrieval import add, setup
from test_knowledge_slicing import Encoder

LEAVE = "员工年假为每年5天，须提前3天申请。"
SICK = "病假需提交医院证明，每年累计不超过10天。"


def forbidden():
    raise AssertionError("不应调用生成模型")


@pytest.fixture(autouse=True)
def floor(monkeypatch):
    """测试用编码器没有登记下限：显式给覆盖值（与 BGE-small 登记值相同），让相关度判断生效。"""
    monkeypatch.setattr(config, "INTENT_KB_THRESHOLD", 0.5)


class FarEncoder(Encoder):
    """查询向量与所有片段几乎正交：语义相似度远低于下限（同一 Embedding 身份，索引可用）。"""
    def embed_query(self, text):
        return [0, 1]


def far_retrieval(indexed):
    store, svc, lifecycle, _ = setup(indexed)
    kb = svc.create("制度库")
    add(store, svc, lifecycle, kb, "休假制度", LEAVE)
    svc.set_egress(kb.id, True)
    return kb, RetrievalService(SimpleNamespace(knowledge=svc, lifecycle=lifecycle, encoder_factory=FarEncoder))


def base_agent(indexed, model, *contents):
    store, svc, lifecycle, retrieval = setup(indexed)
    kb = svc.create("制度库")
    for i, content in enumerate(contents or (LEAVE,)):
        add(store, svc, lifecycle, kb, f"制度{i + 1}", content)
    svc.set_egress(kb.id, True)
    # 默认漏斗只有规则层：下面的追问都交给助理，走 search_knowledge
    return Agent(model, tools=[], knowledge=KnowledgeAssistant(retrieval, lambda: model)), kb


def sent_text(call):
    return "\n".join(str(m.content) for m in call)


def test_low_relevance_never_calls_model_or_egresses(indexed):
    kb, retrieval = far_retrieval(indexed)
    result = KnowledgeQA(retrieval, forbidden).answer(kb.id, "公司食堂几点开饭")
    assert result["status"] == "low_relevance" and result["model_called"] is False
    assert "egress" not in result and "payload_text" not in result
    assert result["references"][0]["label"] == "出处1" and result["references"][0]["title"] == "休假制度"
    assert "没有找到与问题直接相关" in result["answer"] and "《休假制度》" in result["answer"]
    assistant = KnowledgeAssistant(retrieval, forbidden)
    assert assistant.locate(kb.id, "食堂开饭时间")["evidence"] == []
    # 助理路径同样不外发片段原文，只告诉模型最接近的出处
    bundle = assistant.search(kb.id, ["食堂开饭时间"])
    assert bundle["status"] == "low_relevance" and LEAVE not in bundle["answer"] and bundle.get("egress") is None


def test_floor_comes_from_embedding_registration(indexed, monkeypatch, caplog):
    from briefly import relevance_floor as registry
    from briefly.knowledge.service import fingerprint
    kb, retrieval = far_retrieval(indexed)
    qa = KnowledgeQA(retrieval, forbidden)
    # 没有覆盖值、模型也没有登记：不做相关度判断（按授权照常检索），并记一次日志
    monkeypatch.setattr(config, "INTENT_KB_THRESHOLD", None)
    monkeypatch.setattr("briefly.knowledge.retrieval._warned", set())  # 每个模型只记一次：别的用例可能已记过
    with caplog.at_level("WARNING"):
        bundle = qa.bundle(kb.id, ["公司食堂几点开饭"])
    assert bundle["status"] == "ready" and LEAVE in bundle["payload_text"]
    assert "没有登记相关度下限" in caplog.text
    # 按 Embedding 指纹登记后生效
    monkeypatch.setitem(registry.REGISTERED, fingerprint(FarEncoder.identity), 0.5)
    assert qa.bundle(kb.id, ["公司食堂几点开饭"])["status"] == "low_relevance"


def test_three_turns_do_not_resend_earlier_evidence_and_cap_history(indexed, monkeypatch):
    model = fake(
        tool_msg("search_knowledge", {"questions": ["年假有几天"]}, id="s1"), text_msg("年假5天〔依据1〕"),
        tool_msg("search_knowledge", {"questions": ["病假要什么材料"]}, id="s2"), text_msg("需医院证明〔依据1〕"),
        tool_msg("search_knowledge", {"questions": ["年假要提前几天申请"]}, id="s3"), text_msg("提前3天〔依据1〕"),
    )
    agent, kb = base_agent(indexed, model, LEAVE, SICK)
    agent.chat("帮我查查年假的规定", kb_id=kb.id)
    agent.chat("那病假的规定呢", kb_id=kb.id)
    monkeypatch.setattr(config, "KNOWLEDGE_TURN_TOKENS", 4000)
    agent.chat("再查一下年假申请的提前量", kb_id=kb.id)
    before_search, after_search = model.calls[4], model.calls[5]
    # 第三轮发出前：历史里的检索结果只剩说明，没有第一、二轮的原文
    assert LEAVE not in sent_text(before_search) and SICK not in sent_text(before_search)
    stubs = [m for m in before_search if isinstance(m, ToolMessage)]
    assert stubs and all("原文不保留" in m.text for m in stubs)
    # 本轮依据完整保留；知识轮次总输入不超过上限（超出时先少带历史）
    assert LEAVE in sent_text(after_search)
    rows = [r for r in agent.context_store.usage(thread_id=agent.thread_id) if r.task_type == "chat"]
    capped, earlier = rows[0], rows[2]  # 新的在前：第三轮检索后、第二轮检索后
    # 必须完整的部分（系统提示、工具定义、本轮依据与问题）已超过 4000：历史全部省略，本轮依据不砍
    assert capped.history_tokens == 0 and capped.omitted_turns == 2
    necessary = capped.system_tokens + capped.tool_schema_tokens + capped.current_tokens
    assert capped.budgeted_tokens <= max(4000, necessary + capped.memory_tokens + 50)
    assert earlier.history_turns == 1  # 默认上限下，第二轮仍带着第一轮的对话


def test_assistant_path_records_egress_matching_sent_messages(indexed):
    model = fake(tool_msg("search_knowledge", {"questions": ["年假", "病假"]}), text_msg("年假5天〔依据1〕；病假需证明〔依据2〕"))
    agent, kb = base_agent(indexed, model, LEAVE, SICK)
    agent.chat("年假和病假分别怎么规定", kb_id=kb.id)
    final_call = sent_text(model.calls[-1])
    knowledge = visible_messages(agent.history)[-1].knowledge
    assert knowledge["egress"]["evidence_sent"] == final_call.count("【依据") > 0
    assert knowledge["egress"]["history_sent"] and knowledge["egress"]["history_evidence"] == 0
    assert [r["label"] for r in knowledge["references"]] == ["依据1", "依据2"]
    assert all("section" in r for r in knowledge["references"])


def test_second_search_in_same_turn_continues_numbering(indexed):
    model = fake(tool_msg("search_knowledge", {"questions": ["年假"]}, id="a"),
                 tool_msg("search_knowledge", {"questions": ["病假"]}, id="b"), text_msg("病假〔依据3〕"))
    agent, kb = base_agent(indexed, model, LEAVE, SICK)
    agent.chat("先查年假再查病假", kb_id=kb.id)
    second = [m for m in model.calls[-1] if isinstance(m, ToolMessage)][-1].text
    first_count = [m for m in model.calls[-1] if isinstance(m, ToolMessage)][0].text.count("【依据")
    assert f"【依据{first_count + 1}；" in second and "【依据1；" not in second
    refs = visible_messages(agent.history)[-1].knowledge["references"]
    assert [r["label"] for r in refs] == ["依据3"]


def test_revision_without_search_carries_previous_references(indexed):
    model = fake(tool_msg("search_knowledge", {"questions": ["年假"]}), text_msg("年假5天〔依据1〕"),
                 text_msg("| 项目 | 规定 |\n|---|---|\n| 年假 | 5天〔依据1〕 |"))
    agent, kb = base_agent(indexed, model)
    agent.chat("年假怎么规定", kb_id=kb.id)
    first = visible_messages(agent.history)[-1].knowledge
    agent.chat("换成表格", kb_id=kb.id)
    revised = visible_messages(agent.history)[-1]
    assert revised.knowledge["status"] == "carried_over"
    assert revised.knowledge["references"] == first["references"] and revised.knowledge["egress"] is None
    assert any("沿用上一轮" in w for w in revised.warnings)


def test_read_tool_result_refuses_earlier_knowledge_results(indexed):
    model = fake(tool_msg("search_knowledge", {"questions": ["年假"]}, id="old"), text_msg("年假5天〔依据1〕"),
                 tool_msg("read_tool_result", {"result_id": "old"}, id="r"), text_msg("请重新检索"))
    agent, kb = base_agent(indexed, model)
    agent.chat("年假怎么规定", kb_id=kb.id)
    agent.chat("把刚才的原文再给我看看", kb_id=kb.id)
    result = next(m for m in agent.history if isinstance(m, ToolMessage) and m.tool_call_id == "r")
    assert "重新检索" in result.text and LEAVE not in result.text


def test_shortcut_references_carry_section(indexed):
    store, svc, lifecycle, retrieval = setup(indexed)
    kb = svc.create("库")
    add(store, svc, lifecycle, kb, "制度", "预订前审批。")
    svc.set_egress(kb.id, True)
    model = fake(text_msg("【可答性：可回答】预订前审批〔依据1〕"))
    result = KnowledgeQA(retrieval, lambda: model).answer(kb.id, "如何审批？")
    assert result["status"] == "answered" and result["references"][0]["label"] == "依据1"
    assert set(result["references"][0]) == {"label", "reference", "title", "section"}
    assert result["egress"]["evidence_sent"] == len(result["evidence"])


def _hit(sim=None, lexical=False, context=False):
    routes = ([dict(route="semantic", rank=1, raw_score=1 - sim)] if sim is not None else []) + \
             ([dict(route="lexical", rank=1, raw_score=-5.0)] if lexical else [])
    return dict(retrieval=dict(rrf_score=None if context else 0.03, routes=routes or [dict(route="document_context")]))


def test_relevance_needs_both_routes_to_agree():
    from briefly.knowledge.retrieval import relevant
    # 语义达到下限，前 5 条有两路同时命中的：相关
    assert relevant([_hit(0.6, lexical=True), _hit(0.55)], 0.5)
    # 语义达到下限，但前 5 条没有一条被两路同时命中（只有单路词法命中也不算）：库外“食堂开饭”这类
    assert not relevant([_hit(0.52), _hit(0.51), _hit(lexical=True)], 0.5)
    # 低于下限，但前 5 条全部两路同时命中：两路一致，相关（库内“资产搬离谁授权”这类）
    assert relevant([_hit(0.49, lexical=True)] * 5, 0.5)
    assert not relevant([_hit(0.49, lexical=True)] * 4 + [_hit(0.45)], 0.5)
    # 文档开头补充片段不参与；没有下限或没有语义分数时不判断
    assert relevant([_hit(0.6, lexical=True), _hit(context=True)], 0.5)
    assert relevant([_hit(0.3)], None) and relevant([_hit(lexical=True)], 0.5)


def _searches_of(agent):
    return [m.artifact["knowledge"] for m in agent.history if isinstance(m, ToolMessage) and m.name == "search_knowledge"]


def test_first_search_adds_original_question_and_records_queries(indexed):
    # 智能体把问题改写偏了（只查病假），原话本来能命中年假原文：第一次检索并入原话，第二次不再并入
    model = fake(tool_msg("search_knowledge", {"questions": ["病假材料"]}, id="a"),
                 tool_msg("search_knowledge", {"questions": ["病假天数"]}, id="b"), text_msg("提前3天〔依据1〕"))
    agent, kb = base_agent(indexed, model, LEAVE, SICK)
    agent.chat("年假要提前几天申请？", kb_id=kb.id)
    first, second = _searches_of(agent)
    assert first["questions"] == ["年假要提前几天申请？", "病假材料"] and second["questions"] == ["病假天数"]
    first_result = next(m for m in agent.history if isinstance(m, ToolMessage) and m.tool_call_id == "a").text
    assert LEAVE in first_result and first_result.count("【依据") == len(first["references"])
    assert visible_messages(agent.history)[-1].knowledge["queries"] == [first["questions"], second["questions"]]


def test_original_question_not_duplicated_or_added_to_short_follow_up(indexed):
    model = fake(tool_msg("search_knowledge", {"questions": ["年假有几天？"]}, id="a"), text_msg("年假5天〔依据1〕"),
                 tool_msg("search_knowledge", {"questions": ["病假需要什么材料"]}, id="b"), text_msg("需医院证明〔依据1〕"),
                 tool_msg("search_knowledge", {"questions": ["年假", "病假", "事假"]}, id="c"),
                 tool_msg("search_knowledge", {"questions": ["年假", "病假", "事假", "婚假"]}, id="d"), text_msg("见依据〔依据1〕"))
    agent, kb = base_agent(indexed, model, LEAVE, SICK)
    agent.chat("年假有几天？", kb_id=kb.id)
    agent.chat("那病假呢？", kb_id=kb.id)  # 只有原话检索不到：用智能体补全后的检索词
    agent.chat("年假、病假和事假各有几天，分别怎么申请？", kb_id=kb.id)
    queries = [s["questions"] for s in _searches_of(agent)]
    # 并入原话不挤掉智能体的检索词（第一次检索可到 4 个）；智能体自己的检索词仍最多 3 个
    assert queries == [["年假有几天？"], ["病假需要什么材料"], ["年假、病假和事假各有几天，分别怎么申请？", "年假", "病假", "事假"],
                       ["年假", "病假", "事假"]]


def test_original_question_with_task_word_is_still_added(indexed):
    from briefly.intent import knowledge_question
    question = "项目周报每周几点前交？"
    assert not knowledge_question(question)  # 句式判断把“周报”当写作任务；智能体已主动检索，仍并入原话
    model = fake(tool_msg("search_knowledge", {"questions": ["项目周报提交时间"]}), text_msg("周五前〔依据1〕"))
    agent, kb = base_agent(indexed, model, LEAVE, "项目周报须在每周五18:00前提交。")
    agent.chat(question, kb_id=kb.id)
    assert _searches_of(agent)[0]["questions"] == [question, "项目周报提交时间"]


def test_original_question_keeps_evidence_budget_and_egress_authorization(indexed, monkeypatch):
    # 智能体给满 3 个检索词时并入原话共 4 个，都检索；依据包总额不变（测试编码器按字符计 token；材料总量超过额度）
    monkeypatch.setattr(config, "KNOWLEDGE_CONTEXT_TOKENS", 1000)
    model = fake(tool_msg("search_knowledge", {"questions": ["病假材料", "病假天数", "年假天数"]}), text_msg("提前3天〔依据1〕"))
    agent, kb = base_agent(indexed, model, *[LEAVE * 8, SICK * 8, "事假须提前一天申请，每年不超过5天。" * 8] * 3)
    agent.chat("年假要提前几天申请？", kb_id=kb.id)
    (search,) = _searches_of(agent)
    sent = next(m for m in agent.history if isinstance(m, ToolMessage)).text
    assert search["questions"] == ["年假要提前几天申请？", "病假材料", "病假天数", "年假天数"]
    assert "【依据" in sent and len(sent) <= 1000
    # 未授权外发时并入原话也不发原文
    model = fake(tool_msg("search_knowledge", {"questions": ["病假材料"]}), text_msg("请先开启授权"))
    agent, kb = base_agent(indexed, model, LEAVE, SICK)
    agent.knowledge.knowledge.set_egress(kb.id, False)
    agent.chat("年假要提前几天申请？", kb_id=kb.id)
    sent = next(m for m in agent.history if isinstance(m, ToolMessage)).text
    assert LEAVE not in sent and SICK not in sent
