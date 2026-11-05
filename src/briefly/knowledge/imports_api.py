"""知识导入API；有界逐文件读取、异步处理、自动解析并建立索引。"""
from pathlib import Path
import json
import logging
from threading import Lock
from typing import Annotated

from fastapi import BackgroundTasks, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from ..errors import UserInputError
from .. import config
from ..llm import create_local_embeddings
from .index import LocalIndex
from .ingestion import ImportService
from .service import KnowledgeConflict, KnowledgeNotFound


log = logging.getLogger(__name__)


class RetryIn(BaseModel):
    reparse: bool = False


def job_out(job):
    return dict(id=job.id, kb_id=job.kb_id, filename=job.filename, source_key=job.source_key,
        relative_path=job.source_key.removeprefix("upload:") if job.source_key.startswith("upload:") else None,
        status="indexing" if job.status == "preview" else job.status, error_code=job.error_code, error=job.error,
        material_id=job.material_id, version_id=job.version_id, generation_id=job.generation_id,
        tokens=job.tokens, expected_revision=job.expected_revision,
        chunk_count=len(job.prepared["chunks"]) if job.prepared else None,
        chunk_config=job.prepared["config"] if job.prepared else None)


def register(app):
    encoder, lock = None, Lock()
    def encoder_factory():
        nonlocal encoder
        with lock:
            if encoder is None:
                if not config.EMBEDDING_DIR:
                    raise UserInputError("尚未配置本地Embedding目录BRIEFLY_EMBEDDING_DIR，请先准备离线模型")
                encoder = create_local_embeddings(config.EMBEDDING_DIR)
            return encoder
    service = ImportService(app.state.store.engine, config.KNOWLEDGE_DIR,
        LocalIndex(Path(config.KNOWLEDGE_DIR) / "index.sqlite"), encoder_factory)
    app.state.knowledge_imports = service

    def run(action, *args):
        try:
            result = action(*args)
            if result is not None and result.status == "ready":
                app.state.store.notify_changed()
        except Exception:
            # 失败状态已由持久任务记录；不让一个文件阻止后台批次后续文件。
            log.exception("知识库后台导入任务失败")

    @app.post("/knowledge-bases/{kb_id}/imports", status_code=202, tags=["知识库"])
    def upload(kb_id: str, background: BackgroundTasks,
               files: Annotated[list[UploadFile], File()],
               request_key: Annotated[str, Form(min_length=1, max_length=160)],
               tokens: Annotated[int, Form(ge=32, le=1024)] = 512,
               relative_paths: Annotated[str | None, Form(max_length=10000)] = None,
               expected_revision: Annotated[int, Form(ge=0)] = 0,
               source_key: Annotated[str | None, Form(max_length=200)] = None,
               business_version: Annotated[str | None, Form(max_length=100)] = None):
        try:
            service.knowledge._business_version(business_version)
        except UserInputError as exc:
            raise HTTPException(400, str(exc)) from exc
        with service.knowledge._session() as s:
            service.knowledge._kb(s, kb_id)
        if not 1 <= len(files) <= 16 or source_key and len(files) != 1:
            raise HTTPException(400, "每批1～16份文件，显式更新来源时每次一份")
        if sum(file.size or 0 for file in files) > 50 * 1024 * 1024:
            raise HTTPException(413, "批次超过50MB，请分批提交")
        paths = [None] * len(files)
        if relative_paths is not None:
            try:
                paths = json.loads(relative_paths)
                if not isinstance(paths, list) or len(paths) != len(files):
                    raise ValueError
                for file, path in zip(files, paths):
                    if not isinstance(path, str):
                        raise ValueError
                    if path and (len(path) > 193 or "\\" in path or ":" in path or "\x00" in path
                                 or any(part in {"", ".", ".."} for part in path.split("/"))
                                 or path.split("/")[-1] != Path(file.filename or "").name):
                        raise ValueError
            except (ValueError, TypeError):
                raise HTTPException(400, "文件相对路径无效：须与文件名对应，不含绝对路径或上级目录，长度不超过193字符") from None
            if source_key and any(paths):
                raise HTTPException(400, "更新已有来源时不能另行指定目录")
        result, total = [], 0
        for number, file in enumerate(files):
            data = bytearray()
            while block := file.file.read(64 * 1024):
                total += len(block)
                if total > 50 * 1024 * 1024:
                    raise HTTPException(413, "批次超过50MB；已接收文件可在任务列表核对，未接收文件请另批提交")
                data.extend(block)
                if len(data) > config.MAX_UPLOAD_MB * 1024 * 1024:
                    # 单文件拒绝不终止整个批次，也不无限读取超大文件。
                    break
            try:
                job, _ = service.enqueue(kb_id, file.filename or "未命名", bytes(data),
                    request_key=f"{request_key}:{number}", tokens=tokens, source_key=source_key or ("upload:" + paths[number] if paths[number] else None),
                    expected_revision=expected_revision, business_version=business_version)
                result.append(job_out(job))
                if job.status in {"queued", "preview"}:
                    background.add_task(run, service.process, kb_id, job.id)
            except UserInputError as exc:
                if isinstance(exc, KnowledgeConflict):
                    raise
                result.append(dict(filename=file.filename, status="rejected", error=str(exc)))
        return dict(files=result)

    @app.get("/knowledge-bases/{kb_id}/imports", tags=["知识库"])
    def jobs(kb_id: str, background: BackgroundTasks):
        service.recover_expired()
        rows = service.jobs(kb_id)
        for job_id in service.pending_ids(kb_id):
            background.add_task(run, service.process, kb_id, job_id)
        return [job_out(job) for job in rows]

    @app.get("/knowledge-bases/{kb_id}/imports/{job_id}/outline", tags=["知识库"])
    def outline(kb_id: str, job_id: str):
        return service.outline(kb_id, job_id)

    @app.post("/knowledge-bases/{kb_id}/imports/{job_id}/retry", tags=["知识库"])
    def retry(kb_id: str, job_id: str, body: RetryIn, background: BackgroundTasks):
        job = service.retry(kb_id, job_id, reparse=body.reparse)
        if job.status in {"queued", "preview"}:
            background.add_task(run, service.process, kb_id, job_id)
        return job_out(job)
