"""知识库元数据和稳定原文引用。此处不加载模型、不构建或冒充就绪索引。"""
import hashlib
import json

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import defer, sessionmaker

from ..clock import now_local, system_naive
from ..materials import Material, MaterialChunk
from .models import IndexGeneration, KnowledgeBase, KnowledgeChunk, KnowledgeEgressGrant, KnowledgeDocument, KnowledgePublication, KnowledgeVersion
from .evidence import constraints, quality_snapshot, restricted_answer


class KnowledgeNotFound(ValueError):
    pass


class KnowledgeConflict(ValueError):
    pass


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def embedding_identity(identity):
    """只接受可复现的本地适配器身份；不补猜模型版本或截断预算。"""
    if not isinstance(identity, dict):
        raise ValueError("Embedding身份须为完整对象")
    for key in ("model", "revision", "pooling"):
        if not isinstance(identity.get(key), str) or not identity[key].strip():
            raise ValueError("Embedding身份缺少模型、版本或池化方式")
    for key, maximum in (("dimension", 4096), ("max_tokens", 1048576)):
        if type(identity.get(key)) is not int or not 1 <= identity[key] <= maximum:
            raise ValueError("Embedding维度或输入预算无效")
    if not isinstance(identity.get("query_prefix"), str) or type(identity.get("normalize")) is not bool:
        raise ValueError("Embedding身份缺少查询前缀或归一化配置")
    artifacts = identity.get("artifact_sha256")
    if not isinstance(artifacts, dict) or not artifacts or any(
        not isinstance(name, str) or not name.strip() or not isinstance(digest, str)
        or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
        for name, digest in artifacts.items()
    ):
        raise ValueError("Embedding身份缺少有效制品指纹")
    if "index_contract" in identity:
        raise ValueError("索引来源契约由服务生成，不能传入")
    # 断开调用者持有的嵌套对象；拒绝非JSON配置。
    return json.loads(json.dumps(identity, allow_nan=False))


def source_contract(version):
    return dict(format=1, version_id=version.id, content_hash=version.content_hash,
                chunk_fingerprint=version.chunk_fingerprint, chunk_config=version.chunk_config)


def index_fingerprint(identity):
    return fingerprint(identity)


def chunk_fingerprint(config, manifest):
    if config["strategy"] == "preserved-material-chunks":
        return fingerprint(manifest)
    return fingerprint(dict(config=config, manifest=manifest))


class KnowledgeService:
    def __init__(self, engine):
        self._session = sessionmaker(engine, expire_on_commit=False)

    @staticmethod
    def _kb(s, kb_id):
        kb = s.get(KnowledgeBase, kb_id)
        if kb is None or kb.deleted_at is not None:
            raise KnowledgeNotFound("知识库不存在或已移除")
        return kb

    def _document(self, s, kb_id, document_id, *, include_removed=False):
        self._kb(s, kb_id)
        doc = s.get(KnowledgeDocument, document_id)
        if doc is None or doc.kb_id != kb_id or (doc.deleted_at and not include_removed):
            raise KnowledgeNotFound("文档不在当前知识库或已移除")
        return doc

    def _version(self, s, kb_id, doc_id, version_id, *, include_removed=False):
        doc = self._document(s, kb_id, doc_id, include_removed=include_removed)
        version = s.get(KnowledgeVersion, version_id)
        if version is None or version.document_id != doc.id:
            raise KnowledgeNotFound("版本不属于指定文档")
        return doc, version

    @staticmethod
    def _material(s, version):
        material = s.get(Material, version.material_id, options=[defer(Material.content, raiseload=True)]) if version.material_id else None
        if material is None:
            return None, "source_deleted"
        if material.content_hash != version.content_hash:
            return None, "source_changed"
        return material, None

    @staticmethod
    def _business_version(value):
        if value is not None and (not value.strip() or len(value) > 100):
            raise ValueError("业务版本标签须为1～100字；未知时不填写")

    def create(self, name):
        name = name.strip()
        if not name or len(name) > 200:
            raise ValueError("知识库名称须为1～200字")
        with self._session.begin() as s:
            kb = KnowledgeBase(name=name)
            s.add(kb)
            s.flush()
            return kb

    def list_bases(self):
        with self._session() as s:
            return list(s.scalars(select(KnowledgeBase).where(KnowledgeBase.deleted_at.is_(None)).order_by(KnowledgeBase.created_at, KnowledgeBase.id)))

    def egress_allowed(self, kb_id):
        with self._session() as s:
            self._kb(s, kb_id)
            grant = s.get(KnowledgeEgressGrant, kb_id)
            return bool(grant and grant.allowed)

    def set_egress(self, kb_id, allowed):
        """按知识库一次性授权外发，可随时关闭；之后的问答不再逐次确认。"""
        with self._session.begin() as s:
            self._kb(s, kb_id)
            grant = s.get(KnowledgeEgressGrant, kb_id) or KnowledgeEgressGrant(kb_id=kb_id)
            grant.allowed = bool(allowed)
            s.add(grant)
        return bool(allowed)

    def egress_grants(self):
        with self._session() as s:
            return {g.kb_id for g in s.scalars(select(KnowledgeEgressGrant).where(KnowledgeEgressGrant.allowed.is_(True)))}

    def documents(self, kb_id):
        with self._session() as s:
            self._kb(s, kb_id)
            return list(s.scalars(select(KnowledgeDocument).where(KnowledgeDocument.kb_id == kb_id, KnowledgeDocument.deleted_at.is_(None)).order_by(KnowledgeDocument.created_at, KnowledgeDocument.id)))

    def document_states(self, kb_id):
        with self._session() as s:
            self._kb(s, kb_id)
            rows = s.execute(select(KnowledgeDocument, KnowledgePublication, IndexGeneration)
                .outerjoin(KnowledgePublication, KnowledgePublication.document_id == KnowledgeDocument.id)
                .outerjoin(IndexGeneration, IndexGeneration.id == KnowledgePublication.generation_id)
                .where(KnowledgeDocument.kb_id == kb_id, KnowledgeDocument.deleted_at.is_(None))
                .order_by(KnowledgeDocument.created_at, KnowledgeDocument.id)).all()
            return [(doc, publication.version_id if publication else None,
                     generation.status if generation else "not_indexed") for doc, publication, generation in rows]

    @staticmethod
    def _append(s, doc, material_id, business_version, *, number=None, slices=None, chunk_config=None):
        KnowledgeService._business_version(business_version)
        material = s.get(Material, material_id, options=[defer(Material.content, raiseload=True)])
        if material is None:
            raise KnowledgeNotFound("源材料不存在")
        chunks = slices if slices is not None else list(s.scalars(select(MaterialChunk).where(MaterialChunk.material_id == material_id).order_by(MaterialChunk.index)))
        if not chunks:
            raise KnowledgeConflict("源材料没有可引用切片")
        chunk_config = chunk_config or dict(strategy="preserved-material-chunks", version=1, parameters=None)
        manifest = [dict(index=c.index, section=c.section, start=c.start, end=c.end,
                         constraints=constraints(material.parse_report,c.start,c.end),
                         **(dict(storage="version", embedding_sha256=hashlib.sha256(c.embedding_text.encode()).hexdigest()) if slices is not None else {}),
                         sha256=hashlib.sha256(c.content.encode()).hexdigest()) for c in chunks]
        digest = chunk_fingerprint(chunk_config, manifest)
        old = s.scalar(select(KnowledgeVersion).where(KnowledgeVersion.document_id == doc.id,
            KnowledgeVersion.content_hash == material.content_hash, KnowledgeVersion.chunk_fingerprint == digest,
            KnowledgeVersion.material_id == material.id))
        if old:
            if business_version is not None and business_version != old.business_version:
                raise KnowledgeConflict("相同内容已有不同版本标签，请核对，不覆盖原标签")
            return old, False
        number = doc.revision + 1 if number is None else number
        version = KnowledgeVersion(document_id=doc.id, number=number,
            business_version=business_version, material_id=material.id, content_hash=material.content_hash,
            source_snapshot=dict(title=material.title, source=material.source, file_type=material.file_type,
                                 feishu=material.parse_report.get("feishu"),parse_quality=quality_snapshot(material.parse_report)),
            chunk_config=chunk_config,
            chunk_manifest=manifest, chunk_fingerprint=digest)
        doc.revision = number
        s.add(version)
        s.flush()
        if slices is not None:
            s.add_all(KnowledgeChunk(version_id=version.id, index=c.index, section=c.section,
                start=c.start, end=c.end, content=c.content, embedding_text=c.embedding_text) for c in slices)
            s.flush()
        return version, True

    def stage_slices(self, kb_id, material_id, source_key, chunk_config, slices, *,
                     expected_revision=0, business_version=None, guard=None, on_version=None):
        """确认预览后的新知识版本；复用源材料，绝不原地重切旧材料。"""
        if not source_key.strip() or len(source_key) > 200:
            raise ValueError("来源标识须为1～200字")
        with self._session.begin() as s:
            self._kb(s, kb_id)
            if guard:
                guard(s)
            material = s.get(Material, material_id)
            if material is None:
                raise KnowledgeNotFound("源材料不存在")
            if not slices or [c.index for c in slices] != list(range(1, len(slices) + 1)):
                raise ValueError("切片序号无效")
            if any(c.start < 0 or c.end <= c.start or c.end > len(material.content)
                   or c.content != material.content[c.start:c.end] for c in slices):
                raise KnowledgeConflict("预览原文范围已变化")
            if chunk_config.get("strategy") != "title-token-offsets" or chunk_config.get("version") not in {1, 2, 3, 4}:
                raise ValueError("未知切片配置")
            doc = s.scalar(select(KnowledgeDocument).where(
                KnowledgeDocument.kb_id == kb_id, KnowledgeDocument.source_key == source_key))
            if doc is None:
                if expected_revision != 0:
                    raise KnowledgeConflict("文档修订号不匹配")
                doc = KnowledgeDocument(kb_id=kb_id, source_key=source_key, title=material.title)
                s.add(doc)
                try:
                    s.flush()
                except IntegrityError as exc:
                    raise KnowledgeConflict("来源被并发创建，请刷新") from exc
            else:
                result = s.execute(update(KnowledgeDocument).where(
                    KnowledgeDocument.id == doc.id, KnowledgeDocument.revision == expected_revision,
                    KnowledgeDocument.deleted_at.is_(None)).values(revision=expected_revision + 1),
                    execution_options={"synchronize_session": False})
                if result.rowcount != 1:
                    raise KnowledgeConflict("文档已变化，请刷新后重新确认")
                s.refresh(doc)
            version, created = self._append(s, doc, material_id, business_version,
                number=expected_revision + 1, slices=slices, chunk_config=chunk_config)
            if not created:
                doc.revision = expected_revision
            if on_version:
                on_version(s, version.id)
            return doc, version, created

    def attach(self, kb_id, material_id, *, source_key=None, business_version=None):
        """显式纳入既有材料；同一材料可多库关联，技术版本与业务标签分开。"""
        try:
            with self._session.begin() as s:
                self._kb(s, kb_id)
                material = s.get(Material, material_id, options=[defer(Material.content, raiseload=True)])
                if material is None:
                    raise KnowledgeNotFound("源材料不存在")
                key = source_key.strip() if source_key is not None else f"material:{material_id}:{material.content_hash}"
                if not key or len(key) > 200:
                    raise ValueError("来源标识须为1～200字")
                doc = s.scalar(select(KnowledgeDocument).where(KnowledgeDocument.kb_id == kb_id, KnowledgeDocument.source_key == key))
                if doc:
                    if doc.deleted_at:
                        raise KnowledgeConflict("该来源已移除；重新纳入请使用新的来源标识")
                    version = s.scalar(select(KnowledgeVersion).where(KnowledgeVersion.document_id == doc.id, KnowledgeVersion.number == 1))
                    if version and version.content_hash == material.content_hash and version.material_id == material.id:
                        if business_version is not None and business_version != version.business_version:
                            raise KnowledgeConflict("该来源已有不同版本标签，请核对，不覆盖原标签")
                        return doc, version, False
                    raise KnowledgeConflict("来源标识已存在，请通过版本接口更新并核对文档修订号")
                doc = KnowledgeDocument(kb_id=kb_id, source_key=key, title=material.title)
                s.add(doc)
                s.flush()
                version, _ = self._append(s, doc, material_id, business_version)
                return doc, version, True
        except IntegrityError as exc:
            raise KnowledgeConflict("来源已被并发纳入，请刷新后核对") from exc

    def append_version(self, kb_id, doc_id, material_id, *, expected_revision, business_version=None):
        with self._session.begin() as s:
            doc = self._document(s, kb_id, doc_id)
            # 条件写入先取得写权限，防止两个请求分配相同版本号；不提前激活新版本。
            result = s.execute(update(KnowledgeDocument).where(KnowledgeDocument.id == doc.id,
                KnowledgeDocument.revision == expected_revision, KnowledgeDocument.deleted_at.is_(None)).values(revision=expected_revision + 1), execution_options={"synchronize_session": False})
            if result.rowcount != 1:
                raise KnowledgeConflict("文档已变化，请刷新后重试")
            s.refresh(doc)
            version, created = self._append(s, doc, material_id, business_version, number=expected_revision + 1)
            if not created:
                doc.revision = expected_revision
            return doc, version, created

    def versions(self, kb_id, doc_id):
        with self._session() as s:
            doc = self._document(s, kb_id, doc_id)
            rows = list(s.scalars(select(KnowledgeVersion).where(KnowledgeVersion.document_id == doc.id).order_by(KnowledgeVersion.number)))
            publication = s.get(KnowledgePublication, doc.id)
            return doc, rows, publication

    def generations(self, kb_id, doc_id, version_id):
        with self._session() as s:
            self._version(s, kb_id, doc_id, version_id)
            return list(s.scalars(select(IndexGeneration).where(IndexGeneration.version_id == version_id).order_by(IndexGeneration.created_at, IndexGeneration.id)))

    def remove_document(self, kb_id, doc_id, *, expected_revision):
        with self._session.begin() as s:
            doc = self._document(s, kb_id, doc_id)
            now = system_naive(now_local())
            result = s.execute(update(KnowledgeDocument).where(KnowledgeDocument.id == doc.id,
                KnowledgeDocument.revision == expected_revision, KnowledgeDocument.deleted_at.is_(None))
                .values(deleted_at=now, revision=expected_revision + 1))
            if result.rowcount != 1:
                raise KnowledgeConflict("文档已变化，请刷新后重试")

    def plan_index(self, kb_id, doc_id, version_id, identity):
        """供后续入库任务使用；HTTP不提供伪造就绪/发布状态的入口。"""
        identity = embedding_identity(identity)
        with self._session.begin() as s:
            _, version = self._version(s, kb_id, doc_id, version_id)
            material, reason = self._material(s, version)
            if material is None:
                raise KnowledgeConflict("源材料已删除或变化，不能规划索引")
            self._verify_chunks(s, version)
            identity["index_contract"] = dict(source=source_contract(version), embedding_fingerprint=fingerprint(identity))
            generation = IndexGeneration(version_id=version.id, embedding_identity=identity, dimension=identity["dimension"],
                                         chunk_fingerprint=version.chunk_fingerprint)
            s.add(generation)
            s.flush()
            return generation

    @staticmethod
    def _verify_chunks(s, version):
        if version.chunk_config.get("strategy") not in {"preserved-material-chunks", "title-token-offsets"}:
            raise KnowledgeConflict("未知版本切片配置，不能读取索引")
        native = version.chunk_config["strategy"] != "preserved-material-chunks"
        query = select(KnowledgeChunk).where(KnowledgeChunk.version_id == version.id).order_by(KnowledgeChunk.index) if native else select(MaterialChunk).where(
            MaterialChunk.material_id == version.material_id).order_by(MaterialChunk.index)
        chunks = list(s.scalars(query))
        actual = [(c.index, c.start, c.end, hashlib.sha256(c.content.encode()).hexdigest()) for c in chunks]
        expected = [(c["index"], c["start"], c["end"], c["sha256"]) for c in version.chunk_manifest]
        encoded_ok = not native or [hashlib.sha256(c.embedding_text.encode()).hexdigest() for c in chunks] == [c.get("embedding_sha256") for c in version.chunk_manifest]
        if actual != expected or not encoded_ok or chunk_fingerprint(version.chunk_config, version.chunk_manifest) != version.chunk_fingerprint:
            raise KnowledgeConflict("源切片已变化，不能使用原版本建立或读取索引")

    @staticmethod
    def _compatible(generation, version):
        identity = dict(generation.embedding_identity)
        contract = identity.pop("index_contract", None)
        try:
            embedding_identity(identity)
        except (ValueError, TypeError):
            return False
        return (contract == dict(source=source_contract(version), embedding_fingerprint=fingerprint(identity))
                and generation.dimension == identity["dimension"]
                and generation.chunk_fingerprint == version.chunk_fingerprint)

    def recover_interrupted_indexes(self, generation_ids):
        """维护入口：确认这些构建任务已停止后调用；不在每个服务实例启动时全表改状态。"""
        if not generation_ids:
            return 0
        with self._session.begin() as s:
            result = s.execute(update(IndexGeneration).where(
                IndexGeneration.id.in_(set(generation_ids)), IndexGeneration.status == "building"
            ).values(status="failed", error_code="build_interrupted"))
            return result.rowcount

    def scope(self, kb_ids):
        """显式知识库范围；空列表永不退回全库。未就绪项与可查范围分别返回。"""
        if not kb_ids:
            return dict(status="empty_scope", material_ids=[], versions=[], unavailable=[])
        with self._session() as s:
            for kb_id in set(kb_ids):
                self._kb(s, kb_id)
            docs = list(s.scalars(select(KnowledgeDocument).where(KnowledgeDocument.kb_id.in_(kb_ids), KnowledgeDocument.deleted_at.is_(None)).order_by(KnowledgeDocument.id)))
            ready, unavailable = [], []
            for doc in docs:
                publication = s.get(KnowledgePublication, doc.id)
                if publication is None:
                    unavailable.append(dict(document_id=doc.id, reason="not_indexed"))
                    continue
                version = s.get(KnowledgeVersion, publication.version_id)
                generation = s.get(IndexGeneration, publication.generation_id)
                material, reason = self._material(s, version)
                if not reason:
                    try:
                        self._verify_chunks(s, version)
                    except KnowledgeConflict:
                        reason = "source_changed"
                if reason or generation.status != "ready" or not self._compatible(generation, version):
                    unavailable.append(dict(document_id=doc.id, reason=reason or "index_not_ready"))
                    continue
                ready.append(dict(kb_id=doc.kb_id, document_id=doc.id, version_id=version.id,
                                  generation_id=generation.id, material_id=material.id,
                                  embedding_fingerprint=generation.embedding_identity["index_contract"]["embedding_fingerprint"],
                                  index_fingerprint=index_fingerprint(generation.embedding_identity)))
            status = "partial" if ready and unavailable else "not_ready" if unavailable else "ready" if ready else "empty_library"
            return dict(status=status, material_ids=sorted({v["material_id"] for v in ready}), versions=ready, unavailable=unavailable)

    def chunk(self, kb_id, doc_id, version_id, index, *, expected_digest=None):
        with self._session() as s:
            doc, version = self._version(s, kb_id, doc_id, version_id, include_removed=True)
            original = next((c for c in version.chunk_manifest if c["index"] == index), None)
            if original is None or (expected_digest is not None and expected_digest != original["sha256"]):
                raise KnowledgeNotFound("引用切片不存在或指纹不匹配")
            material, reason = self._material(s, version)
            if original.get("storage") == "version":
                chunk = s.get(KnowledgeChunk, (version.id, index)) if material else None
            else:
                chunk = s.scalar(select(MaterialChunk).where(MaterialChunk.material_id == material.id, MaterialChunk.index == index)) if material else None
            if not reason and (chunk is None or hashlib.sha256(chunk.content.encode()).hexdigest() != original["sha256"] or chunk.start != original["start"] or chunk.end != original["end"]):
                reason = "source_changed"
            publication = s.get(KnowledgePublication, doc.id)
            status = reason or ("document_removed" if doc.deleted_at else "current" if publication and publication.version_id == version.id else "historical" if publication else "not_indexed")
            limits={**original.get("constraints",{}),
                    **constraints(version.source_snapshot.get("parse_quality",{}),original["start"],original["end"]),
                    **(constraints(material.parse_report,original["start"],original["end"]) if material else {})}
            return dict(reference=f"kbref:{version.id}:{index}:{original['sha256']}", kb_id=kb_id,
                document_id=doc.id, version_id=version.id, number=version.number, business_version=version.business_version,
                status=status, source=version.source_snapshot, chunk=index,
                section=None if "section_attribution" in limits else original["section"],constraints=limits,
                start=original["start"], end=original["end"], page=None, content=chunk.content if chunk and not reason else None)

    def answer_from_references(self,kb_id,question,references,model_factory):
        """已召回稳定引用→核验→回答；不伪装为已交付向量召回。"""
        evidence=[self.resolve_reference(kb_id,r) for r in dict.fromkeys(references)]
        if any(e["status"] in {"source_deleted","source_changed","document_removed"} or e["content"] is None for e in evidence):
            raise KnowledgeConflict("引用来源已删除、移除或变化，请重新选择依据")
        if sum(len(e["content"]) for e in evidence)>24000:
            raise KnowledgeConflict("引用内容过长，请缩小本次依据范围")
        restricted=restricted_answer(evidence)
        if restricted is not None:
            return {**restricted,"evidence":evidence}
        # Reuse the single-agent implementation and its one-rewrite checking chain.
        from ..agent import Agent
        from .. import config
        from ..materials import wrap_material
        body="\n\n".join(wrap_material(f"【依据《{e['source']['title']}》】",e["content"]) for e in evidence)
        prompt="只依据以下已核验引用回答；引用没有的内容写未提及，不推断章节、上下级或单元格关系。\n问题："+question+"\n\n"+body
        agent=Agent(model=model_factory(),tools=[])
        if agent.counter.count_text(prompt)>config.INPUT_INLINE_TOKENS:
            raise KnowledgeConflict("引用上下文超过本次问答预算，请缩小依据范围")
        answer=agent.chat(prompt)
        return dict(status="answered",answer=answer,evidence=evidence,model_called=True,blocked_capabilities=[])

    def resolve_reference(self, kb_id, reference):
        parts = reference.split(":")
        if len(parts) != 4 or parts[0] != "kbref":
            raise ValueError("引用格式无效")
        try:
            index = int(parts[2])
        except ValueError as exc:
            raise ValueError("引用块号无效") from exc
        with self._session() as s:
            version = s.get(KnowledgeVersion, parts[1])
            if version is None:
                raise KnowledgeNotFound("引用版本不存在")
            doc_id = version.document_id
        return self.chunk(kb_id, doc_id, parts[1], index, expected_digest=parts[3])
