import pytest

from briefly.knowledge.qa import KnowledgeQA
from briefly.knowledge.retrieval import RetrievalService
from test_knowledge_index import indexed
from test_knowledge_retrieval import setup, add
from test_knowledge_slicing import Encoder
from fakes import fake, text_msg


def forbidden():
    raise AssertionError("不应调用生成模型")


def test_unauthorized_base_and_empty_scope_never_create_model(indexed):
    store, svc, lifecycle, retrieval = setup(indexed)
    kb = svc.create("库")
    qa = KnowledgeQA(retrieval, forbidden)
    assert qa.answer(kb.id, "如何审批？")["status"] == "empty_library"
    add(store, svc, lifecycle, kb, "制度", "预订前申请审批。")
    result = qa.answer(kb.id, "如何审批？")
    assert result["status"] == "external_authorization_required" and result["model_called"] is False
    assert qa.answer(kb.id, "总结全文")["status"] == "workflow_required"
    assert qa.answer(kb.id, "总结这份文档")["status"] == "workflow_required"
    assert qa.answer(kb.id, "帮我总结一下")["status"] == "workflow_required"
    assert qa.answer(kb.id, "它什么时候？")["status"] == "clarification_required"
    assert qa.answer(kb.id, "如何审批？", document_ids=[])["status"] == "empty_scope"


def test_complete_egress_package_is_redacted_and_citations_resolve(indexed):
    store, svc, lifecycle, retrieval = setup(indexed)
    kb = svc.create("库")
    add(store, svc, lifecycle, kb, "联系person@example.com", "预订前申请审批。手机号13912345678，password=exampleSecret，项目代号ORION-PRIVATE。")
    model = fake(text_msg("【可答性：可回答】须预订前申请审批。原文：预订前申请审批。〔依据1、依据1〕"))
    qa = KnowledgeQA(retrieval, lambda: model)
    svc.set_egress(kb.id, True)
    result = qa.answer(kb.id, "person@example.com如何申请审批？", private_values=["ORION-PRIVATE"])
    assert result["status"] == "answered" and len(model.calls) == 1
    sent = "\n".join(str(m.content) for m in model.calls[0])
    for value in ("person@example.com", "13912345678", "exampleSecret", "ORION-PRIVATE"):
        assert value not in sent
    assert "[已隐藏" in sent and result["egress"]["redactions"]
    assert svc.resolve_reference(kb.id, result["citations"][0]["reference"])["content"] == result["evidence"][0]["content"]


def test_restricted_evidence_and_incomplete_scope_do_not_generate(indexed):
    store, svc, lifecycle, retrieval = setup(indexed)
    kb = svc.create("库")
    mat, _ = store.add_text("PDF", "表格文字", report={"table_structure_status": "partial"})
    doc, version, _ = svc.attach(kb.id, mat.id)
    lifecycle.build(kb.id, doc.id, version.id, Encoder(), expected_revision=1)
    qa = KnowledgeQA(retrieval, forbidden)
    svc.set_egress(kb.id, True)
    assert qa.answer(kb.id, "表格文字对应谁？")["status"] == "needs_review"
    other = svc.create("其他库")
    add(store, svc, lifecycle, other, "已索引", "预订前审批。")
    add(store, svc, lifecycle, other, "待处理", "其他依据", publish=False)
    svc.set_egress(other.id, True)
    assert qa.answer(other.id, "如何审批？")["status"] == "incomplete_scope"


def test_restricted_evidence_is_withheld_when_clean_evidence_remains(indexed):
    store, svc, lifecycle, retrieval = setup(indexed)
    kb = svc.create("库")
    add(store, svc, lifecycle, kb, "制度", "预订前申请审批。")
    mat, _ = store.add_text("组织图", "审批由乙部门负责。", report={"object_anchors": 1})
    doc, version, _ = svc.attach(kb.id, mat.id)
    lifecycle.build(kb.id, doc.id, version.id, Encoder(), expected_revision=1)
    model = fake(text_msg("【可答性：可回答】须预订前申请审批。原文：预订前申请审批。〔依据1〕"))
    svc.set_egress(kb.id, True)
    result = KnowledgeQA(retrieval, lambda: model).answer(kb.id, "如何申请审批？")
    assert result["model_called"] and len(model.calls) == 1  # 同库另一份文档受限，不再整题拒答
    sent = "\n".join(str(m.content) for m in model.calls[0])
    assert "预订前申请审批" in sent and "乙部门" not in sent  # 受限依据仍不外发
    assert all(not e.get("constraints") for e in result["evidence"])
    assert any("《组织图》" in w and "未用于回答" in w for w in result["warnings"])


def test_model_can_report_insufficient_evidence_without_fake_citations(indexed):
    store, svc, lifecycle, retrieval = setup(indexed)
    kb = svc.create("库")
    add(store, svc, lifecycle, kb, "制度", "预订前审批。")
    model = fake(text_msg("【可答性：依据不足】资料未提及企业定价，无法确定。"))
    svc.set_egress(kb.id, True)
    result = KnowledgeQA(retrieval, lambda: model).answer(kb.id, "预订前审批的企业定价是多少？")
    assert result["status"] == "insufficient_evidence" and result["citations"] == []


def test_egress_grant_is_per_base_and_can_be_revoked(indexed):
    store, svc, lifecycle, retrieval = setup(indexed)
    kb, other = svc.create("库"), svc.create("其他库")
    add(store, svc, lifecycle, kb, "制度", "预订前审批。")
    assert svc.egress_allowed(kb.id) is False
    svc.set_egress(kb.id, True)
    assert svc.egress_allowed(kb.id) and not svc.egress_allowed(other.id) and svc.egress_grants() == {kb.id}
    svc.set_egress(kb.id, False)
    assert KnowledgeQA(retrieval, forbidden).answer(kb.id, "如何审批？")["status"] == "external_authorization_required"


def test_answer_budget_uses_knowledge_context_tokens(indexed, monkeypatch):
    from briefly import config
    store, svc, lifecycle, retrieval = setup(indexed)
    kb = svc.create("库")
    add(store, svc, lifecycle, kb, "制度", "预订前审批。")
    calls = []
    original = retrieval.search
    monkeypatch.setattr(retrieval, "search", lambda *a, **k: calls.append(k) or original(*a, **k))
    KnowledgeQA(retrieval, forbidden).answer(kb.id, "如何审批？")
    assert calls[0]["context_tokens"] == config.KNOWLEDGE_CONTEXT_TOKENS == 8000 and calls[0]["context_budget_only"]
