from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from briefly.db import create_db_engine
from briefly.knowledge.index import LocalIndex
from briefly.knowledge.ingestion import ImportService, utcnow
from briefly.knowledge.models import KnowledgeImport, KnowledgeVersion
from briefly.knowledge.service import KnowledgeConflict, KnowledgeNotFound
from briefly.loaders import LoadedDocument
from test_knowledge_slicing import Encoder


@pytest.fixture
def imports(tmp_path):
    engine = create_db_engine("sqlite:///" + (tmp_path / "app.db").as_posix())
    def parser(filename, data):
        if filename == "broken.docx":
            raise ValueError("文档损坏")
        return LoadedDocument(data.decode(), markdown=True)
    service = ImportService(engine, tmp_path, LocalIndex(tmp_path / "index.db"), Encoder, parser=parser)
    try:
        yield engine, service, service.knowledge.create("资料库")
    finally:
        engine.dispose()


def test_mixed_files_preview_confirm_and_receipts(imports):
    engine, svc, kb = imports
    job, created = svc.enqueue(kb.id, "policy.md", "# 制度\n住宿上限650元。".encode(), request_key="request:0")
    same, created_again = svc.enqueue(kb.id, "policy.md", "# 制度\n住宿上限650元。".encode(), request_key="request:0")
    assert created and not created_again and same.id == job.id
    bad, _ = svc.enqueue(kb.id, "broken.docx", b"bad", request_key="request:1")
    assert svc.parse(kb.id, job.id).status == "preview"
    with pytest.raises(ValueError, match="文档损坏"):
        svc.parse(kb.id, bad.id)
    assert svc.get(kb.id, bad.id).status == "failed"
    preview = svc.get(kb.id, job.id)
    assert "住宿上限650元" in svc.preview_chunks(kb.id, job.id)[0]["content"]
    assert svc.knowledge.scope([kb.id])["status"] == "empty_library"  # 确认前不纳入知识库
    with pytest.raises(KnowledgeConflict):
        svc.confirm(kb.id, job.id, "stale")
    result = svc.confirm(kb.id, job.id, preview.preview["digest"])
    assert result.status == "ready" and result.version_id and result.generation_id
    assert svc.confirm(kb.id, job.id, preview.preview["digest"]).generation_id == result.generation_id
    assert svc.lifecycle.validate_scope([kb.id], Encoder())["status"] == "ready"
    assert svc.retry(kb.id, bad.id).status == "queued"
    with Session(engine) as s:
        assert len(list(s.scalars(select(KnowledgeVersion)))) == 1
    with pytest.raises(KnowledgeConflict):
        svc.enqueue(kb.id, "different.md", b"text", request_key="request:0")
    other = svc.knowledge.create("其他库")
    with pytest.raises(KnowledgeNotFound):
        svc.preview_chunks(other.id, job.id)


def test_index_retry_reuses_version_and_keeps_preview(imports):
    engine, svc, kb = imports
    job, _ = svc.enqueue(kb.id, "a.txt", b"original source content", request_key="one")
    preview = svc.parse(kb.id, job.id)
    class Failing(Encoder):
        def embed_documents(self, texts):
            raise RuntimeError("fixture failure")
    svc.encoder_factory = Failing
    with pytest.raises(RuntimeError):
        svc.confirm(kb.id, job.id, preview.preview["digest"])
    failed = svc.get(kb.id, job.id)
    assert failed.status == "failed" and failed.version_id
    assert svc.retry(kb.id, job.id).status == "preview"
    svc.encoder_factory = Encoder
    assert svc.confirm(kb.id, job.id, preview.preview["digest"]).status == "ready"
    with Session(engine) as s:
        assert len(list(s.scalars(select(KnowledgeVersion)))) == 1


def test_expired_tasks_recover_without_overwriting_active_worker(imports):
    engine, svc, kb = imports
    one, _ = svc.enqueue(kb.id, "one.txt", b"one", request_key="one")
    two, _ = svc.enqueue(kb.id, "two.txt", b"two", request_key="two")
    with Session(engine) as s, s.begin():
        a, b = s.get(KnowledgeImport, one.id), s.get(KnowledgeImport, two.id)
        a.status, a.lease_token, a.lease_until = "parsing", "old", utcnow() - timedelta(seconds=1)
        b.status, b.lease_token, b.lease_until = "parsing", "live", utcnow() + timedelta(seconds=60)
    assert svc.recover_expired() == 1
    assert svc.recover_expired() == 0
    with Session(engine) as s, s.begin(), pytest.raises(KnowledgeConflict):
        svc._guard(s, one.id, "old")
    assert svc.get(kb.id, one.id).status == "failed"
    assert svc.get(kb.id, two.id).status == "parsing"


def test_model_changed_after_preview_requires_new_preview(imports):
    _, svc, kb = imports
    job, _ = svc.enqueue(kb.id, "one.txt", b"original", request_key="one")
    preview = svc.parse(kb.id, job.id)
    class Changed(Encoder):
        identity = {**Encoder.identity, "revision": "changed"}
    svc.encoder_factory = Changed
    with pytest.raises(KnowledgeConflict, match="重新预览"):
        svc.confirm(kb.id, job.id, preview.preview["digest"])
    assert svc.get(kb.id, job.id).status == "failed"
