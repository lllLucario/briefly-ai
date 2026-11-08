"""真实索引构建与发布；先完成派生库事务，再以源版本/文档修订号条件激活。"""
from sqlalchemy import select, update

from .models import IndexGeneration, KnowledgeDocument, KnowledgePublication
from .service import BATCH, KnowledgeConflict, KnowledgeNotFound, KnowledgeService


class IndexLifecycle:
    def __init__(self, engine, index):
        self.knowledge = KnowledgeService(engine)
        self.index = index

    @staticmethod
    def rows(s, version, chunks=None):
        """索引的 (index, 文本)：保留原材料切片用正文，版本自有切片用 embedding_text。chunks 为 load_chunks 的结果。"""
        if chunks is None:
            chunks = KnowledgeService.load_chunks(s, [version])[version.id]
        if version.chunk_config["strategy"] == "preserved-material-chunks":
            return [(index, content) for index, _, _, content, _ in chunks]
        return [(index, text) for index, *_, text in chunks]

    def verify_versions(self, items):
        """逐份核对范围内各代次的索引制品（签名、清单、逐片哈希、向量维度），与单份 verify 相同；
        按批取版本、代次和切片，每批只开一次索引连接。items 为 scope()["versions"] 的条目。"""
        from .models import KnowledgeVersion
        with self.knowledge._session() as s:
            for start in range(0, len(items), BATCH):
                batch = items[start:start + BATCH]
                versions = {v.id: v for v in s.scalars(select(KnowledgeVersion).where(KnowledgeVersion.id.in_({i["version_id"] for i in batch})))}
                generations = {g.id: g for g in s.scalars(select(IndexGeneration).where(IndexGeneration.id.in_({i["generation_id"] for i in batch})))}
                if any(i["version_id"] not in versions or i["generation_id"] not in generations for i in batch):
                    raise KnowledgeNotFound("知识库不存在或已移除")  # 取范围之后知识库被删除
                chunks = KnowledgeService.load_chunks(s, list(versions.values()))
                self.index.verify_many([(generations[i["generation_id"]], self.rows(s, versions[i["version_id"]], chunks[i["version_id"]]))
                                        for i in batch])

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

    def validate_scope(self, kb_ids, encoder=None):
        scope = self.knowledge.scope(kb_ids)
        from .service import fingerprint
        expected = fingerprint(encoder.identity) if encoder is not None else None
        if expected is not None and any(item["embedding_fingerprint"] != expected for item in scope["versions"]):
            raise KnowledgeConflict("知识库索引使用其他Embedding配置，请选择对应模型或重建")
        self.verify_versions(scope["versions"])
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
