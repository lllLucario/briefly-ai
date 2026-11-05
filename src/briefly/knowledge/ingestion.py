"""逐文件自动解析、索引发布与租约；失败重试沿用同一回执。"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from threading import Event, Thread
from time import perf_counter
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..errors import UserInputError
from ..materials import KNOWLEDGE, MaterialStore
from ..parse_process import ParseFailure, load_isolated
from .lifecycle import IndexLifecycle
from .models import KnowledgeImport, KnowledgeVersion, KnowledgeParseCache, KnowledgeImportMetrics
from .service import KnowledgeConflict, KnowledgeNotFound, KnowledgeService, fingerprint, outline_preview
from .slicing import token_slices


def parse_cache_stat(engine) -> dict:
    """解析缓存的统计：从每次入库已有的指标记录算，不改解析和核验流程。显式重新解析不算未命中。
    不给节省时间：历史解析耗时不能当作这次省下的时间。"""
    with Session(engine) as s:
        rows = list(s.scalars(select(KnowledgeImportMetrics.values)))
    counted = [v for v in rows if isinstance(v, dict) and "cache_hit" in v and not v.get("force_reparse")]
    hits = sum(1 for v in counted if v["cache_hit"])
    return dict(name="parse.document", hits=hits, misses=len(counted) - hits)


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ImportService:
    LEASE_SECONDS = 60

    def __init__(self, engine, directory, index, encoder_factory, *, parser=load_isolated, parser_identity=None):
        self.knowledge = KnowledgeService(engine)
        self.store = MaterialStore(engine)
        self.directory = Path(directory).resolve()
        self.lifecycle = IndexLifecycle(engine, index)
        self.encoder_factory = encoder_factory
        self.parser = parser
        self.parser_identity = parser_identity

    def processing(self, kb_id, job_id):
        self.get(kb_id, job_id)
        with self.knowledge._session() as s:
            row = s.get(KnowledgeImportMetrics, job_id)
            return dict(row.values) if row else {}

    def _metrics(self, job_id, token, values):
        with self.knowledge._session.begin() as s:
            self._guard(s, job_id, token)
            s.merge(KnowledgeImportMetrics(import_id=job_id, values=dict(values)))

    def _payload(self, job_id):
        # 只有DB生成的UUID可用于路径，不接收客户端路径。
        if len(job_id) != 32 or any(c not in "0123456789abcdef" for c in job_id):
            raise KnowledgeNotFound("导入任务不存在")
        return self.directory / "uploads" / job_id

    def enqueue(self, kb_id, filename, data, *, request_key, source_key=None,
                expected_revision=0, business_version=None, tokens=512):
        from .. import config
        if not request_key.strip() or len(request_key) > 200:
            raise UserInputError("幂等标识须为1～200字")
        filename = Path(filename).name[:200]
        source_key = source_key or "upload:" + filename
        if not source_key.strip() or len(source_key) > 200 or expected_revision < 0:
            raise UserInputError("来源标识或修订号无效")
        self.knowledge._business_version(business_version)
        if not data or len(data) > config.MAX_UPLOAD_MB * 1024 * 1024:
            raise UserInputError("上传文件为空或超过单文件上限")
        if type(tokens) is not int or not 32 <= tokens <= 1024:
            raise UserInputError("切片预算须为32～1024token")
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

    def pending_ids(self, kb_id):
        """旧交接任务接续不受网页最近100条记录的展示上限限制。"""
        with self.knowledge._session() as s:
            self.knowledge._kb(s, kb_id)
            return list(s.scalars(select(KnowledgeImport.id).where(KnowledgeImport.kb_id == kb_id,
                KnowledgeImport.status.in_(["queued", "preview"])).order_by(KnowledgeImport.created_at)))

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
                    error=str(exc) if isinstance(exc, UserInputError) else "文件处理失败，可单独重试"))
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
            from .parse_cache import cache_key, parser_identity, read_cached
            started = perf_counter()
            previous_metrics = self.processing(kb_id, job_id)
            metrics = dict(cache_hit=False, force_reparse=previous_metrics.get("force_reparse", False))
            data = self._payload(job.id).read_bytes()
            tick = perf_counter()
            identity = self.parser_identity
            if identity is None and self.parser is load_isolated:
                identity = parser_identity()
            key = cache_key(kb_id, job.filename, data, identity) if identity is not None else None
            with self.knowledge._session() as s:
                doc = read_cached(s, key, kb_id) if key and not metrics["force_reparse"] else None
            metrics["cache_lookup_seconds"] = round(perf_counter() - tick, 6)
            metrics["cache_hit"] = doc is not None
            metrics["parser_fingerprint"] = fingerprint(identity) if identity is not None else None
            tick = perf_counter()
            try:
                if doc is None:
                    doc = self.parser(job.filename, data)
            finally:
                metrics["parser_wall_seconds"] = round(perf_counter() - tick, 6)
                self._metrics(job_id, token, metrics)
            if not doc.text.strip():
                raise ParseFailure("empty_document", "未提取到正文，未纳入知识库")
            metrics["executed_parse_timings"] = {} if metrics["cache_hit"] else doc.report.get("timings", {})
            tick = perf_counter()
            material, _ = self.store.add_text(job.filename, doc.text, source=KNOWLEDGE,
                kind=Path(job.filename).suffix.lstrip(".").lower(), markdown=doc.markdown, report=doc.report)
            if hashlib.sha256(material.content.encode()).hexdigest() != material.content_hash:
                raise KnowledgeConflict("已保存正文与指纹不一致，不能复用或覆盖来源，请核对存储")
            metrics["material_store_seconds"] = round(perf_counter() - tick, 6)
            # 解析成功即持久化，后续切片或索引失败也可复用；租约失效不能写缓存。
            with self.knowledge._session.begin() as s:
                self._guard(s, job_id, token)
                s.get(KnowledgeImport, job_id).material_id = material.id
                if key:
                    try:
                        with s.begin_nested():
                            s.merge(KnowledgeParseCache(id=key, kb_id=kb_id, material_id=material.id,
                                content_hash=material.content_hash, markdown=doc.markdown,
                                report_snapshot=doc.report))
                            s.flush()
                    except IntegrityError:
                        pass  # 同契约另一worker已缓存；不影响本任务解析结果。
            tick = perf_counter()
            encoder = self.encoder_factory()
            metrics["encoder_load_seconds"] = round(perf_counter() - tick, 6)
            tick = perf_counter()
            try:
                config, slices = token_slices(material.content, job.filename, doc.markdown, encoder,
                    tokens=job.tokens, report={**material.parse_report, **doc.report})
            finally:
                metrics["slicing_seconds"] = round(perf_counter() - tick, 6)
                metrics["prepare_total_seconds"] = round(perf_counter() - started, 6)
                metrics["force_reparse"] = False
                self._metrics(job_id, token, metrics)
            prepared = self._summary(material, config, slices, encoder.identity)
            prepared["markdown"] = doc.markdown
            from .evidence import quality_snapshot
            prepared["structure_report"] = quality_snapshot(doc.report)
            prepared["digest"] = fingerprint(prepared)
            with self.knowledge._session.begin() as s:
                self._guard(s, job_id, token)
                row = s.get(KnowledgeImport, job_id)
                row.material_id, row.prepared, row.status = material.id, prepared, "preview"
                row.lease_token, row.lease_until = None, None
        return self.get(kb_id, job_id)

    def process(self, kb_id, job_id):
        """自动接续解析与索引；旧preview状态仅作兼容的内部交接点。"""
        job = self.get(kb_id, job_id)
        if job.status == "ready":
            return job
        if job.status == "queued":
            job = self.parse(kb_id, job_id)
        if job.status == "preview":
            return self.build_index(kb_id, job_id)
        raise KnowledgeConflict("任务正在处理或需要先重试失败文件")

    def outline(self, kb_id, job_id):
        job = self.get(kb_id, job_id)
        if not job.prepared or job.material_id is None:
            return dict(total=0, items=[], status=job.status)
        material = self.store.get(job.material_id, workspace=False)
        if material.content_hash != job.prepared["content_hash"] or hashlib.sha256(material.content.encode()).hexdigest() != material.content_hash:
            raise KnowledgeConflict("解析来源已变化，请重新解析")
        from .evidence import constraints
        items = []
        for chunk in job.prepared["chunks"]:
            limits = {**constraints(job.prepared.get("structure_report", {}), chunk["start"], chunk["end"]),
                      **constraints(material.parse_report, chunk["start"], chunk["end"])}
            section = None if "section_attribution" in limits else chunk["section"]
            items.append(dict(index=chunk["index"], section=section, preview=outline_preview(material.content[chunk["start"]:chunk["end"]], section), constraints=limits))
        return dict(total=len(items), items=items, status=job.status)

    def build_index(self, kb_id, job_id):
        job = self.get(kb_id, job_id)
        if job.status == "ready":
            return job  # 幂等回执，不重复向量化
        with self._lease(kb_id, job_id, "preview", "indexing") as token:
            job = self.get(kb_id, job_id)
            if not job.prepared or fingerprint({k:v for k,v in job.prepared.items() if k != "digest"}) != job.prepared.get("digest"):
                raise KnowledgeConflict("解析交接数据已变化，请重新解析")
            metrics = self.processing(kb_id, job_id)
            material = self.store.get(job.material_id, workspace=False)
            encoder = self.encoder_factory()
            tick = perf_counter()
            config, slices = token_slices(material.content, job.filename, job.prepared["markdown"], encoder,
                tokens=job.tokens, report={**material.parse_report, **job.prepared.get("structure_report", {})})
            metrics["index_slice_seconds"] = round(perf_counter() - tick, 6)
            summary = self._summary(material, config, slices, encoder.identity)
            if any(summary[key] != job.prepared[key] for key in summary):
                raise KnowledgeConflict("源材料、模型或切片配置已变化，必须重新解析")
            # 新知识版本提交也必须在同一事务核对任务租约。
            guard = lambda s: self._guard(s, job_id, token)
            if job.version_id:
                with self.knowledge._session() as s:
                    version = s.get(KnowledgeVersion, job.version_id)
                    doc = self.knowledge._document(s, kb_id, version.document_id)
                    if doc.revision != version.number:
                        raise KnowledgeConflict("文档已有新版本，旧任务不能重新发布")
                    if version.content_hash != material.content_hash or version.chunk_config != config:
                        raise KnowledgeConflict("重试版本与新解析数据不一致，请作为新版本导入")
            else:
                def save_version(s, version_id):
                    s.get(KnowledgeImport, job_id).version_id = version_id
                doc, version, _ = self.knowledge.stage_slices(kb_id, material.id, job.source_key, config, slices,
                    expected_revision=job.expected_revision, business_version=job.business_version,
                    guard=guard, on_version=save_version)
            def save_generation(s, generation_id):
                s.get(KnowledgeImport, job_id).generation_id = generation_id
            tick = perf_counter()
            try:
                self.lifecycle.build(kb_id, doc.id, version.id, encoder, expected_revision=doc.revision,
                    guard=guard, on_generation=save_generation)
            finally:
                metrics["index_seconds"] = round(perf_counter() - tick, 6)
                self._metrics(job_id, token, metrics)
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
                row.prepared = None
                record = s.get(KnowledgeImportMetrics, job_id)
                values = {**(record.values if record else {}), "force_reparse": True}
                s.merge(KnowledgeImportMetrics(import_id=job_id, values=values))
            row.status = "preview" if row.prepared and row.material_id else "queued"
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
