from types import SimpleNamespace

import pytest

from briefly.knowledge.retrieval import RetrievalService
from briefly.knowledge.service import KnowledgeConflict, KnowledgeNotFound
from test_knowledge_index import indexed
from test_knowledge_slicing import Encoder


def setup(indexed):
    _, store, svc, lifecycle = indexed
    imports = SimpleNamespace(knowledge=svc, lifecycle=lifecycle, encoder_factory=Encoder)
    return store, svc, lifecycle, RetrievalService(imports)


def add(store, svc, lifecycle, kb, title, content, *, publish=True):
    mat, _ = store.add_text(title, content)
    doc, version, _ = svc.attach(kb.id, mat.id)
    if publish:
        lifecycle.build(kb.id, doc.id, version.id, Encoder(), expected_revision=1)
    return doc, version


def test_routes_fuse_stable_references_and_scope_before_topk(indexed):
    store, svc, lifecycle, retrieval = setup(indexed)
    kb, outside = svc.create("制度"), svc.create("其他库")
    doc, version = add(store, svc, lifecycle, kb, "目标制度", "上海住宿上限650元，预订前审批。")
    for number in range(22):
        add(store, svc, lifecycle, outside, "外库", f"上海住宿上限999元，干扰编号{number}。")
    result = retrieval.search(kb.id, "上海住宿")
    assert result["status"] == "candidates" and len(result["evidence"]) == 1
    hit = result["evidence"][0]
    assert hit["version_id"] == version.id and "650" in hit["content"]
    assert {r["route"] for r in hit["retrieval"]["routes"]} == {"semantic", "lexical"}
    assert svc.resolve_reference(kb.id, hit["reference"])["content"] == hit["content"]
    assert result["diagnostics"]["answerability"] == "not_assessed"
    assert retrieval.search(kb.id, "上海住宿", document_ids=[])["status"] == "empty_scope"
    assert retrieval.search(kb.id, "上海住宿", document_ids=[doc.id])["evidence"]
    foreign_doc = svc.documents(outside.id)[0]
    with pytest.raises(KnowledgeNotFound):
        retrieval.search(kb.id, "上海住宿", document_ids=[foreign_doc.id])


def test_missing_index_is_not_no_answer_and_partial_is_visible(indexed):
    store, svc, lifecycle, retrieval = setup(indexed)
    kb = svc.create("库")
    one, version = add(store, svc, lifecycle, kb, "可用", "审批在预订之前。")
    two, _ = add(store, svc, lifecycle, kb, "未建索引", "另外的依据", publish=False)
    result = retrieval.search(kb.id, "审批")
    assert result["status"] == "partial" and result["unavailable"][0]["document_id"] == two.id
    assert retrieval.search(kb.id, "审批", document_ids=[two.id])["status"] == "not_ready"
    generation = svc.scope([kb.id])["versions"][0]["generation_id"]
    lifecycle.index.delete([generation])
    with pytest.raises(KnowledgeConflict, match="制品缺失"):
        retrieval.search(kb.id, "审批")


def test_removed_during_query_fails_snapshot_check(indexed):
    store, svc, lifecycle, retrieval = setup(indexed)
    kb = svc.create("库")
    doc, _ = add(store, svc, lifecycle, kb, "制度", "需审批")
    class Removes(Encoder):
        def embed_query(self, text):
            svc.remove_document(kb.id, doc.id, expected_revision=1)
            return super().embed_query(text)
    retrieval.imports.encoder_factory = Removes
    with pytest.raises(KnowledgeConflict, match="期间已变化"):
        retrieval.search(kb.id, "审批")


def test_document_context_adds_same_version_intro_within_scope(indexed):
    from briefly.knowledge.slicing import token_slices
    store, svc, lifecycle, retrieval = setup(indexed)
    kb = svc.create("库")
    text = "# 制度\n仅适用于正式员工国内出差，不适用于实习生。\n\n" + "".join(
        f"## 住宿条款{i}\n住宿需预订前审批，按实际发票报销。\n" for i in range(20))
    material, _ = store.add_text("制度", text, markdown=True)
    config, slices = token_slices(material.content, "制度", True, Encoder(), tokens=64)
    doc, version, _ = svc.stage_slices(kb.id, material.id, "fixture", config, slices)
    lifecycle.build(kb.id, doc.id, version.id, Encoder(), expected_revision=1)
    base = retrieval.search(kb.id, "住宿")
    contextual = retrieval.search(kb.id, "住宿", include_document_context=True)
    assert len(base["evidence"]) <= 5 and len(contextual["evidence"]) <= 7
    assert any(e["start"] == 0 for e in contextual["evidence"])
    assert all(e["document_id"] == doc.id and e["version_id"] == version.id for e in contextual["evidence"])
    assert contextual["parameters"]["version"] == 2


@pytest.mark.parametrize("question", ["两字", "编号E403 2026", '" OR * : 日本語', "🙂 ???"])
def test_queries_are_data_and_do_not_escape_scope(indexed, question):
    store, svc, lifecycle, retrieval = setup(indexed)
    kb = svc.create("库")
    add(store, svc, lifecycle, kb, "资料", "两字词，编号E403 2026。")
    result = retrieval.search(kb.id, question)
    assert result["evidence"] and result["evidence"][0]["kb_id"] == kb.id
