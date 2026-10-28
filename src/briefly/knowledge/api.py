"""知识库元数据、原文引用及显式引用问答；不提供伪造索引就绪的入口。"""
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .service import KnowledgeConflict, KnowledgeNotFound, KnowledgeService, index_fingerprint


class BaseIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class AttachIn(BaseModel):
    material_id: int = Field(gt=0)
    source_key: str | None = Field(default=None, max_length=200)
    business_version: str | None = Field(default=None, max_length=100)


class VersionIn(BaseModel):
    material_id: int = Field(gt=0)
    expected_revision: int = Field(ge=0)
    business_version: str | None = Field(default=None, max_length=100)


class ScopeIn(BaseModel):
    kb_ids: list[str] = Field(max_length=100)


class EvidenceAnswerIn(BaseModel):
    question: str = Field(min_length=1,max_length=4000)
    references: list[str] = Field(min_length=1,max_length=12)


def document_out(doc):
    return dict(id=doc.id, kb_id=doc.kb_id, title=doc.title, source_key=doc.source_key,
                revision=doc.revision, created_at=doc.created_at)


def version_out(version):
    return dict(id=version.id, document_id=version.document_id, number=version.number,
                business_version=version.business_version, material_id=version.material_id,
                content_hash=version.content_hash, source=version.source_snapshot,
                chunk_config=version.chunk_config, chunk_count=len(version.chunk_manifest),
                chunk_fingerprint=version.chunk_fingerprint, created_at=version.created_at)


def register(app: FastAPI,model_factory):
    service = KnowledgeService(app.state.store.engine)
    app.state.knowledge = service

    @app.post("/knowledge-bases/{kb_id}/answer-from-references", tags=["知识库"])
    def answer_from_references(kb_id: str,body: EvidenceAnswerIn):
        try:
            return service.answer_from_references(kb_id,body.question,body.references,model_factory)
        except (KnowledgeConflict,KnowledgeNotFound):
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400,detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502,detail="依据问答失败，请稍后重试") from exc

    @app.exception_handler(KnowledgeNotFound)
    def missing(request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(KnowledgeConflict)
    def conflict(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/knowledge-bases", tags=["知识库"])
    def bases():
        return [dict(id=b.id, name=b.name, revision=b.revision, created_at=b.created_at) for b in service.list_bases()]

    @app.post("/knowledge-bases", status_code=201, tags=["知识库"])
    def create(body: BaseIn):
        try:
            b = service.create(body.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return dict(id=b.id, name=b.name, revision=b.revision, created_at=b.created_at)

    @app.post("/knowledge-scope", tags=["知识库"])
    def scope(body: ScopeIn):
        return service.scope(body.kb_ids)

    @app.get("/knowledge-bases/{kb_id}/documents", tags=["知识库"])
    def documents(kb_id: str):
        return [document_out(d) for d in service.documents(kb_id)]

    @app.post("/knowledge-bases/{kb_id}/documents", tags=["知识库"])
    def attach(kb_id: str, body: AttachIn, response: Response):
        try:
            doc, ver, created = service.attach(kb_id, body.material_id, source_key=body.source_key, business_version=body.business_version)
        except (KnowledgeConflict, KnowledgeNotFound):
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        response.status_code = 201 if created else 200
        return dict(document=document_out(doc), version=version_out(ver), created=created, index_status="not_indexed")

    @app.get("/knowledge-bases/{kb_id}/documents/{doc_id}/versions", tags=["知识库"])
    def versions(kb_id: str, doc_id: str):
        doc, rows, publication = service.versions(kb_id, doc_id)
        return dict(document=document_out(doc), versions=[version_out(v) for v in rows],
                    active_version_id=publication.version_id if publication else None,
                    active_generation_id=publication.generation_id if publication else None)

    @app.post("/knowledge-bases/{kb_id}/documents/{doc_id}/versions", tags=["知识库"])
    def append(kb_id: str, doc_id: str, body: VersionIn, response: Response):
        doc, ver, created = service.append_version(kb_id, doc_id, body.material_id,
            expected_revision=body.expected_revision, business_version=body.business_version)
        response.status_code = 201 if created else 200
        return dict(document=document_out(doc), version=version_out(ver), created=created, index_status="not_indexed")

    @app.delete("/knowledge-bases/{kb_id}/documents/{doc_id}", status_code=204, tags=["知识库"])
    def remove(kb_id: str, doc_id: str, expected_revision: int):
        service.remove_document(kb_id, doc_id, expected_revision=expected_revision)

    @app.get("/knowledge-bases/{kb_id}/documents/{doc_id}/versions/{version_id}/indexes", tags=["知识库"])
    def indexes(kb_id: str, doc_id: str, version_id: str):
        return [dict(id=g.id, version_id=g.version_id, embedding_identity=g.embedding_identity,
                     index_fingerprint=index_fingerprint(g.embedding_identity),
                     dimension=g.dimension, chunk_fingerprint=g.chunk_fingerprint,
                     status=g.status, error_code=g.error_code, created_at=g.created_at)
                for g in service.generations(kb_id, doc_id, version_id)]

    @app.get("/knowledge-bases/{kb_id}/documents/{doc_id}/versions/{version_id}/chunks/{index}", tags=["知识库"])
    def chunk(kb_id: str, doc_id: str, version_id: str, index: int):
        return service.chunk(kb_id, doc_id, version_id, index)

    @app.get("/knowledge-bases/{kb_id}/references/{reference}", tags=["知识库"])
    def reference(kb_id: str, reference: str):
        try:
            return service.resolve_reference(kb_id, reference)
        except KnowledgeNotFound:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
