import hashlib

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from briefly.db import create_db_engine
from briefly.knowledge.models import KnowledgeChunk
from briefly.knowledge.service import KnowledgeConflict, KnowledgeService, index_fingerprint
from briefly.knowledge.slicing import token_slices
from briefly.materials import MaterialChunk, MaterialStore
from test_knowledge import fake_identity


class Encoder:
    identity = fake_identity(dimension=2)

    @staticmethod
    def token_length(text):
        return len(text)

    def embed_documents(self, texts):
        return [[len(text), 1] for text in texts]

    def embed_query(self, text):
        return [len(text), 1]


def test_title_budget_offsets_and_no_punctuation_only_tail():
    text = "# 范围\n" + "业务原文" * 200 + "。"
    config, chunks = token_slices(text, "标题", True, Encoder(), tokens=64)
    assert config["parameters"]["tokens"] == 64
    assert chunks[0].start == 0 and chunks[-1].end == len(text)
    assert all(len(c.embedding_text) <= 64 for c in chunks)
    assert all(c.content == text[c.start:c.end] for c in chunks)
    assert all(any(c.isalnum() for c in row.content) for row in chunks)
    assert all(a.end == b.start for a, b in zip(chunks, chunks[1:]))
    assert "".join(c.content for c in chunks) == text


def test_new_slice_versions_keep_old_material_and_reference(tmp_path):
    engine = create_db_engine("sqlite:///" + (tmp_path / "slices.db").as_posix())
    try:
        store, svc = MaterialStore(engine), KnowledgeService(engine)
        text = "原文依据" * 100
        material, _ = store.add_text("资料", text)
        kb = svc.create("库")
        doc, old, _ = svc.attach(kb.id, material.id, source_key="fixture")
        ref = svc.chunk(kb.id, doc.id, old.id, 1)["reference"]
        with Session(engine) as s:
            before = [(c.id, c.index, c.content) for c in s.scalars(select(MaterialChunk))]
        config, chunks = token_slices(text, "资料", False, Encoder(), tokens=64)
        _, new, created = svc.stage_slices(kb.id, material.id, "fixture", config, chunks, expected_revision=1)
        assert created and new.number == 2 and new.id != old.id
        generation = svc.plan_index(kb.id, doc.id, new.id, Encoder.identity)
        assert generation.status == "planned"
        newref = svc.chunk(kb.id, doc.id, new.id, 1)
        assert newref["content"] == text[:chunks[0].end] and newref["reference"] != ref
        assert svc.resolve_reference(kb.id, ref)["content"] == text
        # 同样切片重复确认不重复创建版本；配置改变必须创建新身份。
        _, same, created = svc.stage_slices(kb.id, material.id, "fixture", config, chunks, expected_revision=2)
        assert not created and same.id == new.id
        other_config, other_chunks = token_slices(text, "资料", False, Encoder(), tokens=96)
        _, other, created = svc.stage_slices(kb.id, material.id, "fixture", other_config, other_chunks, expected_revision=2)
        assert created and other.number == 3
        other_generation = svc.plan_index(kb.id, doc.id, other.id, Encoder.identity)
        assert index_fingerprint(generation.embedding_identity) != index_fingerprint(other_generation.embedding_identity)
        with Session(engine) as s:
            assert before == [(c.id, c.index, c.content) for c in s.scalars(select(MaterialChunk))]
        with pytest.raises(KnowledgeConflict):
            svc.stage_slices(kb.id, material.id, "fixture", config, chunks, expected_revision=1)
        with Session(engine) as s, s.begin():
            s.get(KnowledgeChunk, (new.id, 1)).embedding_text = "被改写"
        with pytest.raises(KnowledgeConflict):
            svc.plan_index(kb.id, doc.id, new.id, Encoder.identity)
        assert hashlib.sha256(chunks[0].content.encode()).hexdigest() == new.chunk_manifest[0]["sha256"]
    finally:
        engine.dispose()
