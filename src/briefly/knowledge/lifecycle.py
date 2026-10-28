"""真实索引构建与发布；先完成派生库事务，再以源版本/文档修订号条件激活。"""
import hashlib

from sqlalchemy import select, update

from ..materials import MaterialChunk
from .models import IndexGeneration, KnowledgeChunk, KnowledgeDocument, KnowledgePublication
from .service import KnowledgeConflict, KnowledgeService


class IndexLifecycle:
    def __init__(self, engine, index):
        self.knowledge = KnowledgeService(engine)
        self.index = index

    @staticmethod
    def rows(s, version):
        if version.chunk_config["strategy"] == "preserved-material-chunks":
            chunks = s.scalars(select(MaterialChunk).where(MaterialChunk.material_id == version.material_id).order_by(MaterialChunk.index))
            return [(c.index, c.content) for c in chunks]
        chunks = s.scalars(select(KnowledgeChunk).where(KnowledgeChunk.version_id == version.id).order_by(KnowledgeChunk.index))
        return [(c.index, c.embedding_text) for c in chunks]

    def build(self, kb_id, doc_id, version_id, encoder, *, expected_revision, guard=None, on_generation=None):
        svc = self.knowledge
        generation = svc.plan_index(kb_id, doc_id, version_id, encoder.identity)
        try:
            with svc._session.begin() as s:
                doc, version = svc._version(s, kb_id, doc_id, version_id)
                if doc.revision != expected_revision:
                    raise KnowledgeConflict("文档已变化，不能开始构建")
                if guard:
                    guard(s)
                if on_generation:
                    on_generation(s, generation.id)
                active = s.get(KnowledgePublication, doc.id)
                previous = active.generation_id if active else None
                s.get(IndexGeneration, generation.id).status = "building"
                svc._verify_chunks(s, version)
                rows = self.rows(s, version)
            vectors = []
            for start in range(0, len(rows), 16):
                vectors.extend(encoder.embed_documents([text for _, text in rows[start:start+16]]))
            self.index.build(generation, rows, vectors)
            self.index.verify(generation, rows)
            with svc._session.begin() as s:
                # 条件写入先取得文档写锁；删除、追加或另一次发布先发生则旧构建失败。
                locked = s.execute(update(KnowledgeDocument).where(KnowledgeDocument.id == doc_id,
                    KnowledgeDocument.revision == expected_revision, KnowledgeDocument.deleted_at.is_(None))
                    .values(revision=expected_revision))
                if locked.rowcount != 1:
                    raise KnowledgeConflict("文档已变化，构建结果未发布")
                _, version = svc._version(s, kb_id, doc_id, version_id)
                if guard:
                    guard(s)
                material, reason = svc._material(s, version)
                if reason:
                    raise KnowledgeConflict("源材料已失效，构建结果未发布")
                svc._verify_chunks(s, version)
                current = s.get(KnowledgePublication, doc_id)
                if (current.generation_id if current else None) != previous:
                    raise KnowledgeConflict("已有另一代次发布，请刷新后重建")
                row = s.get(IndexGeneration, generation.id)
                if row.status != "building" or not svc._compatible(row, version):
                    raise KnowledgeConflict("构建身份已失效，结果未发布")
                row.status = "ready"
                if current:
                    current.version_id, current.generation_id = version.id, generation.id
                else:
                    s.add(KnowledgePublication(document_id=doc_id, version_id=version.id, generation_id=generation.id))
            return generation.id
        except BaseException:
            with svc._session.begin() as s:
                s.execute(update(IndexGeneration).where(IndexGeneration.id == generation.id,
                    IndexGeneration.status.in_(["planned", "building"])).values(status="failed", error_code="build_failed"))
            # 未发布的派生数据不参与查询；清理失败可由后续维护重试。
            try:
                self.index.delete([generation.id])
            except Exception:
                pass
            raise

    def validate_scope(self, kb_ids, encoder):
        scope = self.knowledge.scope(kb_ids)
        from .service import fingerprint
        expected = fingerprint(encoder.identity)
        with self.knowledge._session() as s:
            for item in scope["versions"]:
                if item["embedding_fingerprint"] != expected:
                    raise KnowledgeConflict("知识库索引使用其他Embedding配置，请选择对应模型或重建")
                from .models import KnowledgeVersion
                version = s.get(KnowledgeVersion, item["version_id"])
                generation = s.get(IndexGeneration, item["generation_id"])
                self.index.verify(generation, self.rows(s, version))
        return scope

    def clean_removed(self):
        """源删除立即由scope排除；派生清理可幂等重试，不影响仍共享的原材料。"""
        svc = self.knowledge
        with svc._session() as s:
            from .models import KnowledgeVersion
            rows = s.execute(select(IndexGeneration, KnowledgeVersion, KnowledgeDocument)
                .join(KnowledgeVersion, IndexGeneration.version_id == KnowledgeVersion.id)
                .join(KnowledgeDocument, KnowledgeVersion.document_id == KnowledgeDocument.id)).all()
            removed = [g.id for g, v, d in rows if d.deleted_at or svc._material(s, v)[1]]
        if removed:
            self.index.delete(removed)
        return len(removed)
