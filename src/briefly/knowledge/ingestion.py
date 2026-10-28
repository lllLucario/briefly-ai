"""逐文件持久任务、预览确认与租约；失败重试沿用同一回执。"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from ..materials import MaterialStore
from ..parse_process import ParseFailure, load_isolated
from .lifecycle import IndexLifecycle
from .models import KnowledgeImport, KnowledgeVersion
from .service import KnowledgeConflict, KnowledgeNotFound, KnowledgeService, fingerprint
from .slicing import token_slices


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ImportService:
    LEASE_SECONDS = 60

    def __init__(self, engine, directory, index, encoder_factory, *, parser=load_isolated):
        self.knowledge = KnowledgeService(engine)
        self.store = MaterialStore(engine)
        self.directory = Path(directory).resolve()
        self.lifecycle = IndexLifecycle(engine, index)
        self.encoder_factory = encoder_factory
        self.parser = parser

    def _payload(self, job_id):
        # 只有DB生成的UUID可用于路径，不接收客户端路径。
        if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
            raise KnowledgeNotFound("导入任务不存在")
        return self.directory / "uploads" / job_id

    def enqueue(self, kb_id, filename, data, *, request_key, source_key=None,
                expected_revision=0, business_version=None, tokens=256):
        from .. import config
        if not request_key.strip() or len(request_key) > 200:
            raise ValueError("幂等标识须为1～200字")
        filename = Path(filename).name[:200]
        source_key = source_key or "upload:" + filename
        if not source_key.strip() or len(source_key) > 200 or expected_revision < 0:
            raise ValueError("来源标识或修订号无效")
        self.knowledge._business_version(business_version)
        if not data or len(data) > config.MAX_UPLOAD_MB * 1024 * 1024:
            raise ValueError("上传文件为空或超过单文件上限")
        if type(tokens) is not int or not 32 <= tokens <= 512:
            raise ValueError("切片预算须为32～512token")
        digest = fingerprint(dict(filename=filename, sha256=hashlib.sha256(data).hexdigest(), source=source_key,
            revision=expected_revision, business_version=business_version, tokens=tokens))
        job_id = uuid4().hex
        path = self._payload(job_id)
        try:
            with self.knowledge._session.begin() as s:
                self.knowledge._kb(s, kb_id)
                old = s.scalar(select(KnowledgeImport).where(KnowledgeImport.kb_id == kb_id, KnowledgeImport.request_key == request_key))
                if old:
                    if old.request_fingerprint != digest:
                        raise KnowledgeConflict("幂等标识已用于另一份文件或配置")
                    return old, False
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                job = KnowledgeImport(id=job_id, kb_id=kb_id, request_key=request_key,
                    request_fingerprint=digest, filename=filename, source_key=source_key,
                    expected_revision=expected_revision, business_version=business_version, tokens=tokens)
                s.add(job)
                s.flush()
            return job, True
        except IntegrityError as exc:
            path.unlink(missing_ok=True)
            raise KnowledgeConflict("文件被并发提交，请用相同幂等标识重试") from exc
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def jobs(self, kb_id):
        with self.knowledge._session() as s:
            self.knowledge._kb(s, kb_id)
            return list(s.scalars(select(KnowledgeImport).where(KnowledgeImport.kb_id == kb_id)
                .order_by(KnowledgeImport.created_at.desc(), KnowledgeImport.id).limit(100)))

    def get(self, kb_id, job_id):
        with self.knowledge._session() as s:
            self.knowledge._kb(s, kb_id)
            job = s.get(KnowledgeImport, job_id)
            if job is None or job.kb_id != kb_id:
                raise KnowledgeNotFound("导入任务不在当前知识库")
            return job

    def _guard(self, s, job_id, token):
        result = s.execute(update(KnowledgeImport).where(KnowledgeImport.id == job_id,
            KnowledgeImport.lease_token == token, KnowledgeImport.lease_until > utcnow(),
            KnowledgeImport.status.in_(["parsing", "indexing"]))
            .values(lease_until=utcnow() + timedelta(seconds=self.LEASE_SECONDS)))
        if result.rowcount != 1:
            raise KnowledgeConflict("任务租约已失效，旧执行结果不能写回")

    @contextmanager
    def _lease(self, kb_id, job_id, initial, working):
        token = uuid4().hex
        with self.knowledge._session.begin() as s:
            self.knowledge._kb(s, kb_id)
            changed = s.execute(update(KnowledgeImport).where(KnowledgeImport.id == job_id,
                KnowledgeImport.kb_id == kb_id, KnowledgeImport.status == initial)
                .values(status=working, lease_token=token, lease_until=utcnow() + timedelta(seconds=self.LEASE_SECONDS),
                        error=None, error_code=None))
            if changed.rowcount != 1:
                raise KnowledgeConflict("任务状态已变化，请刷新")
        stopped = Event()
        def renew():
            while not stopped.wait(10):
                try:
                    with self.knowledge._session.begin() as s:
                        self._guard(s, job_id, token)
                except Exception:
                    return  # 写回时必须再次检查；失联任务不能继续覆盖
        thread = Thread(target=renew, daemon=True)
        thread.start()
        try:
            yield token
        except Exception as exc:
            with self.knowledge._session.begin() as s:
                s.execute(update(KnowledgeImport).where(KnowledgeImport.id == job_id,
                    KnowledgeImport.lease_token == token).values(status="failed", lease_token=None,
                    lease_until=None, error_code=getattr(exc, "code", "import_failed"),
                    error=str(exc) if isinstance(exc, ValueError) else "文件处理失败，可单独重试"))
            raise
        finally:
            stopped.set()
            thread.join(timeout=2)

    @staticmethod
    def _summary(material, config, slices, identity):
        return dict(material_id=material.id, content_hash=material.content_hash, config=config,
            embedding_fingerprint=fingerprint(identity), chunks=[dict(index=c.index, section=c.section,
                start=c.start, end=c.end, sha256=hashlib.sha256(c.content.encode()).hexdigest()) for c in slices])

    def parse(self, kb_id, job_id):
        with self._lease(kb_id, job_id, "queued", "parsing") as token:
            job = self.get(kb_id, job_id)
            doc = self.parser(job.filename, self._payload(job.id).read_bytes())
            if not doc.text.strip():
                raise ParseFailure("empty_document", "未提取到正文，未纳入知识库")
            material, _ = self.store.add_text(job.filename, doc.text, source="file",
                kind=Path(job.filename).suffix.lstrip(".").lower(), markdown=doc.markdown, report=doc.report)
            encoder = self.encoder_factory()
            config, slices = token_slices(material.content, job.filename, doc.markdown, encoder, tokens=job.tokens)
            preview = self._summary(material, config, slices, encoder.identity)
            preview["markdown"] = doc.markdown
            preview["digest"] = fingerprint(preview)
            with self.knowledge._session.begin() as s:
                self._guard(s, job_id, token)
                row = s.get(KnowledgeImport, job_id)
                row.material_id, row.preview, row.status = material.id, preview, "preview"
                row.lease_token, row.lease_until = None, None
        return self.get(kb_id, job_id)

    def preview_chunks(self, kb_id, job_id, *, offset=0, limit=10):
        job = self.get(kb_id, job_id)
        if not job.preview or job.material_id is None:
            raise KnowledgeConflict("任务尚无有效预览")
        if offset < 0 or not 1 <= limit <= 50:
            raise ValueError("预览分页无效")
        material = self.store.get(job.material_id)
        if material.content_hash != job.preview["content_hash"]:
            raise KnowledgeConflict("预览源材料已变化")
        from .evidence import constraints
        return [dict(**chunk, content=material.content[chunk["start"]:chunk["end"]],
                     constraints=constraints(material.parse_report, chunk["start"], chunk["end"]))
                for chunk in job.preview["chunks"][offset:offset+limit]]

    def confirm(self, kb_id, job_id, digest):
        job = self.get(kb_id, job_id)
        if job.status == "ready" and job.preview and job.preview["digest"] == digest:
            return job  # 幂等回执，不重复向量化
        if not job.preview or job.preview["digest"] != digest:
            raise KnowledgeConflict("预览已变化，请重新核对后确认")
        with self._lease(kb_id, job_id, "preview", "indexing") as token:
            job = self.get(kb_id, job_id)
            material = self.store.get(job.material_id)
            encoder = self.encoder_factory()
            config, slices = token_slices(material.content, job.filename, job.preview["markdown"], encoder, tokens=job.tokens)
            summary = self._summary(material, config, slices, encoder.identity)
            if any(summary[key] != job.preview[key] for key in summary):
                raise KnowledgeConflict("源材料、模型或切片配置已变化，必须重新预览")
            # 新知识版本提交也必须在同一事务核对任务租约。
            guard = lambda s: self._guard(s, job_id, token)
            if job.version_id:
                with self.knowledge._session() as s:
                    version = s.get(KnowledgeVersion, job.version_id)
                    doc = self.knowledge._document(s, kb_id, version.document_id)
                    if doc.revision != version.number:
                        raise KnowledgeConflict("文档已有新版本，旧任务不能重新发布")
                    if version.content_hash != material.content_hash or version.chunk_config != config:
                        raise KnowledgeConflict("重试版本与新预览不一致，请作为新版本导入")
            else:
                def save_version(s, version_id):
                    s.get(KnowledgeImport, job_id).version_id = version_id
                doc, version, _ = self.knowledge.stage_slices(kb_id, material.id, job.source_key, config, slices,
                    expected_revision=job.expected_revision, business_version=job.business_version,
                    guard=guard, on_version=save_version)
            def save_generation(s, generation_id):
                s.get(KnowledgeImport, job_id).generation_id = generation_id
            self.lifecycle.build(kb_id, doc.id, version.id, encoder, expected_revision=doc.revision,
                guard=guard, on_generation=save_generation)
            with self.knowledge._session.begin() as s:
                self._guard(s, job_id, token)
                row = s.get(KnowledgeImport, job_id)
                row.status, row.lease_token, row.lease_until = "ready", None, None
            self._payload(job_id).unlink(missing_ok=True)
        return self.get(kb_id, job_id)

    def retry(self, kb_id, job_id, *, reparse=False):
        with self.knowledge._session.begin() as s:
            self.get(kb_id, job_id)
            row = s.get(KnowledgeImport, job_id)
            if row.status != "failed" and not (reparse and row.status == "preview"):
                raise KnowledgeConflict("只有失败文件可重试")
            if reparse:
                if not self._payload(job_id).is_file():
                    raise KnowledgeConflict("上传暂存已不存在，请重新上传")
                row.preview = None
            row.status = "preview" if row.preview and row.material_id else "queued"
            row.error, row.error_code = None, None
        return self.get(kb_id, job_id)

    def recover_expired(self):
        """启动及轮询时恢复过期任务；活租约/其他worker不受影响。"""
        with self.knowledge._session.begin() as s:
            expired = list(s.scalars(select(KnowledgeImport).where(KnowledgeImport.status.in_(["parsing", "indexing"]),
                KnowledgeImport.lease_until <= utcnow())))
            recovered = 0
            for row in expired:
                result = s.execute(update(KnowledgeImport).where(KnowledgeImport.id == row.id,
                    KnowledgeImport.lease_token == row.lease_token, KnowledgeImport.lease_until <= utcnow())
                    .values(status="failed", error_code="task_interrupted", error="任务中断，可单独重试", lease_token=None, lease_until=None))
                if result.rowcount:
                    recovered += 1
                    if row.generation_id:
                        from .models import IndexGeneration
                        s.execute(update(IndexGeneration).where(IndexGeneration.id == row.generation_id,
                            IndexGeneration.status == "building").values(status="failed", error_code="build_interrupted"))
        return recovered
