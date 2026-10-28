import pytest

from briefly.knowledge.qa import KnowledgeQA
from briefly.knowledge.retrieval import RetrievalService
from test_knowledge_index import indexed
from test_knowledge_retrieval import setup, add
from test_knowledge_slicing import Encoder
from fakes import fake, text_msg


def forbidden():
    raise AssertionError("不应调用生成模型")


def test_external_confirmation_and_empty_scope_never_create_model(indexed):
    store, svc, lifecycle, retrieval = setup(indexed)
    kb = svc.create("库")
    qa = KnowledgeQA(retrieval, forbidden)
    assert qa.answer(kb.id, "如何审批？")["status"] == "empty_library"
    add(store, svc, lifecycle, kb, "制度", "预订前申请审批。")
    result = qa.answer(kb.id, "如何审批？")
    assert result["status"] == "external_confirmation_required" and result["model_called"] is False
    assert qa.answer(kb.id, "总结全文")["status"] == "workflow_required"
    assert qa.answer(kb.id, "如何审批？", document_ids=[])["status"] == "empty_scope"


def test_complete_egress_package_is_redacted_and_citations_resolve(indexed):
    store, svc, lifecycle, retrieval = setup(indexed)
    kb = svc.create("库")
    add(store, svc, lifecycle, kb, "联系person@example.com", "预订前申请审批。手机号13912345678，password=exampleSecret，项目代号ORION-PRIVATE。")
    model = fake(text_msg("【可答性：可回答】须预订前申请审批。原文：预订前申请审批。〔依据1、依据1〕"))
    qa = KnowledgeQA(retrieval, lambda: model)
    result = qa.answer(kb.id, "person@example.com如何申请审批？", allow_external=True, private_values=["ORION-PRIVATE"])
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
    assert qa.answer(kb.id, "对应谁？", allow_external=True)["status"] == "needs_review"
    other = svc.create("其他库")
    add(store, svc, lifecycle, other, "已索引", "预订前审批。")
    add(store, svc, lifecycle, other, "待处理", "其他依据", publish=False)
    assert qa.answer(other.id, "如何审批？", allow_external=True)["status"] == "incomplete_scope"


def test_model_can_report_insufficient_evidence_without_fake_citations(indexed):
    store, svc, lifecycle, retrieval = setup(indexed)
    kb = svc.create("库")
    add(store, svc, lifecycle, kb, "制度", "预订前审批。")
    model = fake(text_msg("【可答性：依据不足】资料未提及企业定价，无法确定。"))
    result = KnowledgeQA(retrieval, lambda: model).answer(kb.id, "企业定价是多少？", allow_external=True)
    assert result["status"] == "insufficient_evidence" and result["citations"] == []
