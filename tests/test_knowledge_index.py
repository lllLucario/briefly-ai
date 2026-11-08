import math

import pytest
pytest.importorskip("sqlite_vec", reason="本地索引回归需安装.[rag]")
from sqlalchemy.orm import Session

from briefly.db import create_db_engine
from briefly.knowledge.index import LocalIndex, vector_blob
from briefly.knowledge.lifecycle import IndexLifecycle
from briefly.knowledge.models import IndexGeneration
from briefly.knowledge.service import KnowledgeConflict, KnowledgeService
from briefly.materials import MaterialStore
from test_knowledge import purge
from test_knowledge_slicing import Encoder


@pytest.fixture
def indexed(tmp_path):
    engine = create_db_engine("sqlite:///" + (tmp_path / "app.db").as_posix())
    index = LocalIndex(tmp_path / "index.db")
    try:
        yield engine, MaterialStore(engine), KnowledgeService(engine), IndexLifecycle(engine, index)
    finally:
        engine.dispose()


def test_real_sqlite_vec_build_reopen_scope_and_delete(indexed):
    engine, store, svc, lifecycle = indexed
    mat, _ = store.add_text("制度", "住宿上限650元。")
    kb = svc.create("制度库")
    doc, version, _ = svc.attach(kb.id, mat.id)
    generation = lifecycle.build(kb.id, doc.id, version.id, Encoder(), expected_revision=1)
    assert lifecycle.validate_scope([kb.id], Encoder())["status"] == "ready"
    index = LocalIndex(lifecycle.index.path)  # 新连接读取同一磁盘制品
    dense, lexical = index.search("住宿", [1, 1], 2, [generation])
    assert dense[0][:2] == (generation, 1) and lexical[0][:2] == (generation, 1)
    assert index.search("住宿", [1, 1], 2, []) == ([], [])
    assert index.search("住宿", [1, 1], 2, ["other"]) == ([], [])
    svc.remove_document(kb.id, doc.id, expected_revision=1)
    assert svc.scope([kb.id])["versions"] == []
    assert lifecycle.clean_removed() == 1
    assert lifecycle.clean_removed() == 1  # 派生清理可重复
    assert index.search("住宿", [1, 1], 2, [generation]) == ([], [])
    assert store.get(mat.id).content == "住宿上限650元。"


def test_failed_build_and_new_version_keep_previous_publication(indexed):
    engine, store, svc, lifecycle = indexed
    mat, _ = store.add_text("旧", "原制度500元")
    kb = svc.create("库")
    doc, version, _ = svc.attach(kb.id, mat.id)
    active = lifecycle.build(kb.id, doc.id, version.id, Encoder(), expected_revision=1)
    newmat, _ = store.add_text("新", "新制度650元")
    _, new, _ = svc.append_version(kb.id, doc.id, newmat.id, expected_revision=1)
    class Invalid(Encoder):
        def embed_documents(self, texts):
            return [[float("nan"), 1] for _ in texts]
    with pytest.raises(ValueError):
        lifecycle.build(kb.id, doc.id, new.id, Invalid(), expected_revision=2)
    assert svc.scope([kb.id])["versions"][0]["generation_id"] == active
    assert svc.generations(kb.id, doc.id, new.id)[0].status == "failed"
    lifecycle.build(kb.id, doc.id, new.id, Encoder(), expected_revision=2)
    assert svc.scope([kb.id])["material_ids"] == [newmat.id]


def test_source_deleted_during_build_cannot_publish(indexed):
    _, store, svc, lifecycle = indexed
    mat, _ = store.add_text("资料", "原文")
    kb = svc.create("库")
    doc, version, _ = svc.attach(kb.id, mat.id)
    class DeletesSource(Encoder):
        def embed_documents(self, texts):
            purge(store, mat.id)
            return super().embed_documents(texts)
    with pytest.raises(KnowledgeConflict):
        lifecycle.build(kb.id, doc.id, version.id, DeletesSource(), expected_revision=1)
    assert svc.scope([kb.id])["status"] == "not_ready"
    assert svc.versions(kb.id, doc.id)[2] is None


def test_missing_artifact_and_model_mismatch_are_explicit(indexed):
    engine, store, svc, lifecycle = indexed
    mat, _ = store.add_text("资料", "原文依据")
    kb = svc.create("库")
    doc, version, _ = svc.attach(kb.id, mat.id)
    generation = lifecycle.build(kb.id, doc.id, version.id, Encoder(), expected_revision=1)
    class Other(Encoder):
        identity = {**Encoder.identity, "revision": "v2"}
    with pytest.raises(KnowledgeConflict, match="其他Embedding"):
        lifecycle.validate_scope([kb.id], Other())
    lifecycle.index.delete([generation])
    with pytest.raises(KnowledgeConflict, match="制品缺失"):
        lifecycle.validate_scope([kb.id], Encoder())


def test_batched_scope_and_index_checks_still_catch_each_tampered_document(indexed, monkeypatch):
    """范围和索引核对按批取数；批边界两侧、两种切片存储，篡改哪一份就只拦哪一份。"""
    import sqlite3
    from sqlalchemy import update
    from briefly import knowledge
    from briefly.knowledge.models import KnowledgeChunk
    from briefly.knowledge.slicing import token_slices
    from briefly.materials import MaterialChunk
    monkeypatch.setattr(knowledge.service, "BATCH", 2)
    monkeypatch.setattr(knowledge.lifecycle, "BATCH", 2)
    engine, store, svc, lifecycle = indexed
    kb = svc.create("库")
    docs = []
    for n in range(5):
        mat, _ = store.add_text(f"资料{n}", f"第{n}份制度，住宿上限{n}00元。")
        doc, version, _ = svc.attach(kb.id, mat.id)
        docs.append((doc.id, mat.id, lifecycle.build(kb.id, doc.id, version.id, Encoder(), expected_revision=1)))
    text = "原文依据" * 60
    native_mat, _ = store.add_text("切片资料", text)
    native_doc, version, _ = svc.attach(kb.id, native_mat.id, source_key="fixture")
    config, chunks = token_slices(text, "切片资料", False, Encoder(), tokens=64)
    _, native, _ = svc.stage_slices(kb.id, native_mat.id, "fixture", config, chunks, expected_revision=1)
    native_generation = lifecycle.build(kb.id, native_doc.id, native.id, Encoder(), expected_revision=2)
    assert len(lifecycle.validate_scope([kb.id], Encoder())["versions"]) == 6

    with Session(engine) as s, s.begin():
        s.execute(update(MaterialChunk).where(MaterialChunk.material_id == docs[2][1]).values(content="被改写"))
        s.get(KnowledgeChunk, (native.id, 1)).embedding_text = "被改写"
    scope = svc.scope([kb.id])
    assert scope["status"] == "partial"
    assert sorted((u["document_id"], u["reason"]) for u in scope["unavailable"]) == sorted(
        [(docs[2][0], "source_changed"), (native_doc.id, "source_changed")])
    assert {v["generation_id"] for v in lifecycle.validate_scope([kb.id], Encoder())["versions"]} == {
        g for i, (_, _, g) in enumerate(docs) if i != 2}
    assert native_generation not in {v["generation_id"] for v in scope["versions"]}

    if hasattr(lifecycle.index, "engine"):  # 索引制品里最后一批的一份被改写（PostgreSQL 模式下是 PostgresIndex）
        from sqlalchemy import text
        with lifecycle.index.engine.begin() as connection:
            connection.execute(text("UPDATE kb_index_chunks SET content='被改写' WHERE generation=:g"), dict(g=docs[4][2]))
    else:
        with sqlite3.connect(lifecycle.index.path) as db:
            db.execute("UPDATE chunks SET content='被改写' WHERE generation=?", (docs[4][2],))
    with pytest.raises(KnowledgeConflict, match="制品缺失"):
        lifecycle.validate_scope([kb.id], Encoder())


def test_vectors_are_finite_normalized_float32():
    import struct
    assert struct.unpack("<2f", vector_blob([1e-200, 1e-200], 2))[0] == pytest.approx(math.sqrt(0.5))
    for values in ([0, 0], [1, float("inf")], [1]):
        with pytest.raises(ValueError):
            vector_blob(values, 2)
