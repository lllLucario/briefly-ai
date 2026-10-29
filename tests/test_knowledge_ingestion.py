from datetime import timedelta

import pytest
pytest.importorskip("sqlite_vec", reason="知识入库回归需安装.[rag]")
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


def test_parse_cache_survives_restart_and_budget_change(imports):
    engine, svc, kb = imports
    calls = []
    def parser(filename, data):
        calls.append(data)
        return LoadedDocument(data.decode(), False, {})
    svc.parser, svc.parser_identity = parser, "fixture-parser/v1"
    first, _ = svc.enqueue(kb.id, "a.txt", b"actual raw source " * 80, request_key="one", tokens=256)
    a = svc.parse(kb.id, first.id)
    svc.confirm(kb.id, first.id, a.preview["digest"])
    reopened = ImportService(engine, svc.directory, svc.lifecycle.index, Encoder,
                             parser=parser, parser_identity="fixture-parser/v1")
    second, _ = reopened.enqueue(kb.id, "a.txt", b"actual raw source " * 80,
        request_key="two", tokens=512, expected_revision=1)
    b = reopened.parse(kb.id, second.id)
    assert len(calls) == 1 and reopened.processing(kb.id, second.id)["cache_hit"]
    assert a.preview["content_hash"] == b.preview["content_hash"]
    assert a.preview["config"] != b.preview["config"]
    assert reopened.confirm(kb.id, second.id, b.preview["digest"]).status == "ready"
    assert svc.knowledge.resolve_reference(kb.id, svc.knowledge.chunk(kb.id,
        svc.knowledge.documents(kb.id)[0].id, svc.get(kb.id, first.id).version_id, 1)["reference"])["status"] == "historical"


def test_parse_cache_respects_input_parser_library_and_explicit_reparse(imports):
    _, svc, kb = imports
    calls = []
    def parser(filename, data):
        calls.append(data)
        return LoadedDocument(data.decode(), False, {})
    svc.parser, svc.parser_identity = parser, "fixture/v1"
    def run(library, key, data=b"original"):
        job, _ = svc.enqueue(library.id, "a.txt", data, request_key=key)
        svc.parse(library.id, job.id)
        return job
    first = run(kb, "one")
    run(kb, "two")
    assert len(calls) == 1
    run(kb, "changed", b"different source")
    other = svc.knowledge.create("另一库")
    run(other, "other")
    svc.parser_identity = "fixture/v2"
    run(kb, "parser-change")
    assert len(calls) == 4
    svc.retry(kb.id, first.id, reparse=True)
    svc.parse(kb.id, first.id)
    assert len(calls) == 5 and not svc.processing(kb.id, first.id)["cache_hit"]


def test_valid_parse_cached_even_when_slicing_fails_and_keeps_quality(imports):
    _, svc, kb = imports
    calls = []
    text = "| A | B |\n|---|---|\n| " + "x" * 150 + " | b |\n"
    def parser(filename, data):
        calls.append(data)
        return LoadedDocument(text, True, {"table_structure_status": "partial"})
    svc.parser, svc.parser_identity = parser, "fixture/v1"
    first, _ = svc.enqueue(kb.id, "a.md", b"source", request_key="one", tokens=64)
    with pytest.raises(ValueError, match="表格单行"):
        svc.parse(kb.id, first.id)
    assert svc.get(kb.id, first.id).material_id
    second, _ = svc.enqueue(kb.id, "a.md", b"source", request_key="two", tokens=512)
    preview = svc.parse(kb.id, second.id)
    assert len(calls) == 1 and svc.processing(kb.id, second.id)["cache_hit"]
    assert svc.preview_chunks(kb.id, second.id)[0]["constraints"]["cell_attribution"] == "unverified"
    assert preview.status == "preview"


def test_cache_inherits_new_quality_and_refuses_corrupt_material(imports):
    from briefly.materials import Material
    engine, svc, kb = imports
    svc.parser_identity = "fixture/v1"
    one, _ = svc.enqueue(kb.id, "a.txt", b"original", request_key="one")
    first = svc.parse(kb.id, one.id)
    with Session(engine) as s, s.begin():
        s.get(Material, first.material_id).parse_report = {"table_structure_status": "partial"}
    two, _ = svc.enqueue(kb.id, "a.txt", b"original", request_key="two")
    svc.parse(kb.id, two.id)
    assert svc.preview_chunks(kb.id, two.id)[0]["constraints"]["cell_attribution"] == "unverified"
    with Session(engine) as s, s.begin():
        s.get(Material, first.material_id).content = "tampered"
    three, _ = svc.enqueue(kb.id, "a.txt", b"original", request_key="three")
    with pytest.raises(KnowledgeConflict, match="指纹不一致"):
        svc.parse(kb.id, three.id)


def test_lost_parse_lease_cannot_populate_cache(imports):
    from briefly.knowledge.models import KnowledgeParseCache
    engine, svc, kb = imports
    job, _ = svc.enqueue(kb.id, "a.txt", b"source", request_key="one")
    def parser(filename, data):
        with Session(engine) as s, s.begin():
            s.get(KnowledgeImport, job.id).lease_until = utcnow() - timedelta(seconds=1)
        return LoadedDocument("original", False)
    svc.parser, svc.parser_identity = parser, "fixture/v1"
    with pytest.raises(KnowledgeConflict, match="租约"):
        svc.parse(kb.id, job.id)
    with Session(engine) as s:
        assert not list(s.scalars(select(KnowledgeParseCache)))


def test_native_parser_contract_changes_with_backend(monkeypatch):
    from briefly import config
    from briefly.knowledge.parse_cache import parser_identity
    monkeypatch.setattr(config, "DOC_PARSER", "auto")
    one = parser_identity()
    monkeypatch.setattr(config, "DOC_PARSER", "convert")
    assert parser_identity() != one
