"""删除知识库。业务库里的数据在一个事务内删干净；独立的索引文件和磁盘上的上传暂存进不了这个事务，
提交后清理，失败只记日志，下次启动重试（待清理的清单和删除同一事务写入，不会丢）。

- 有入库任务未结束时拒绝，不和任务租约竞争。开头先改库这一行取得写锁：之后提交的任务看得见删除，
  之前提交的任务这里看得见它；库行真删除，外键也挡住任何晚到的写入。
- 工作区材料不删，只断开关联。只属于这个库的知识库专属材料连同章节、分块一并删除。
- 历史回答不改写：会话记录不动，引用打开原文时由 service.resolve_reference 说明来源已删除。
"""
import logging

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from ..links import DISMISSED, DocumentLink
from ..llm_cache import purge_knowledge_answers
from ..materials import KNOWLEDGE, Material, knowledge_referenced
from ..todos.models import TodoActivity, TodoMaterial
from ..tracking.models import FileSnapshot
from ..weekly.models import WeeklyReport
from .evidence import NOTES, constraints
from .models import (IndexGeneration, KnowledgeBase, KnowledgeBaseRemoval, KnowledgeChunk, KnowledgeDocument,
                     KnowledgeEgressGrant, KnowledgeImport, KnowledgeImportMetrics, KnowledgeParseCache,
                     KnowledgePublication, KnowledgeVersion)
from .service import KnowledgeConflict, KnowledgeNotFound

log = logging.getLogger(__name__)

ACTIVE_IMPORTS = ("queued", "parsing", "preview", "indexing")  # preview 是解析与索引之间的内部交接，任务还没结束
WHOLE_DOCUMENT = 10 ** 12


def deletion_preview(imports, kb_id) -> dict:
    """确认框要写明的内容：文件数、未结束的入库任务数、会被断开的工作区关联数。"""
    svc = imports.knowledge
    imports.recover_expired()
    with svc._session() as s:
        kb = svc._kb(s, kb_id)
        return dict(kb_id=kb.id, name=kb.name,
                    documents=s.scalar(select(func.count()).select_from(KnowledgeDocument).where(
                        KnowledgeDocument.kb_id == kb_id, KnowledgeDocument.deleted_at.is_(None))),
                    active_imports=_active_imports(s, kb_id),
                    workspace_links=s.scalar(select(func.count()).select_from(DocumentLink).where(
                        DocumentLink.kb_id == kb_id, DocumentLink.kind != DISMISSED)))


def delete_base(imports, kb_id) -> dict:
    svc = imports.knowledge
    imports.recover_expired()  # 进程已不在的任务转为失败，不把库永远卡在“入库中”
    try:
        with svc._session.begin() as s:
            locked = s.execute(update(KnowledgeBase).where(KnowledgeBase.id == kb_id, KnowledgeBase.deleted_at.is_(None))
                               .values(revision=KnowledgeBase.revision + 1)
                               .execution_options(synchronize_session=False))
            if locked.rowcount != 1:
                raise KnowledgeNotFound("知识库不存在或已移除")
            if active := _active_imports(s, kb_id):
                raise KnowledgeConflict(f"这个知识库还有 {active} 份文件正在入库，请等任务结束后再删除")
            summary = _delete_rows(s, kb_id)
    except IntegrityError as exc:  # 只有并发写入才会走到这里：事务已回滚，什么都没删
        raise KnowledgeConflict("这个知识库正有新的写入，没有删除，请稍后重试") from exc
    return dict(summary, kb_id=kb_id, cleanup_pending=not finish_removal(imports, kb_id))


def finish_removal(imports, kb_id) -> bool:
    """清理索引文件和上传暂存，把清单更新成还没清掉的部分。返回是否全部清理完。可重复调用。"""
    svc = imports.knowledge
    with svc._session() as s:
        row = s.get(KnowledgeBaseRemoval, kb_id)
        if row is None:
            return True
        generations, jobs = list(row.generation_ids), list(row.job_ids)
    if generations:
        try:
            imports.lifecycle.index.delete(generations)
            generations = []
        except Exception:
            log.warning("知识库 %s 已删除，索引清理失败，下次启动重试", kb_id, exc_info=True)
    pending = []
    for job_id in jobs:
        try:
            imports._payload(job_id).unlink(missing_ok=True)
        except OSError:
            log.warning("知识库 %s 已删除，上传暂存 %s 清理失败，下次启动重试", kb_id, job_id, exc_info=True)
            pending.append(job_id)
    with svc._session.begin() as s:
        row = s.get(KnowledgeBaseRemoval, kb_id)
        row.generation_ids, row.job_ids = generations, pending
    return not generations and not pending


def retry_pending_removals(imports) -> None:
    """启动时重试上次没清理完的部分。"""
    with imports.knowledge._session() as s:
        ids = [row.kb_id for row in s.scalars(select(KnowledgeBaseRemoval)) if row.generation_ids or row.job_ids]
    for kb_id in ids:
        finish_removal(imports, kb_id)


def _active_imports(s, kb_id) -> int:
    return s.scalar(select(func.count()).select_from(KnowledgeImport).where(
        KnowledgeImport.kb_id == kb_id, KnowledgeImport.status.in_(ACTIVE_IMPORTS)))


def _delete_rows(s, kb_id) -> dict:
    name = s.scalar(select(KnowledgeBase.name).where(KnowledgeBase.id == kb_id))
    documents = select(KnowledgeDocument.id).where(KnowledgeDocument.kb_id == kb_id)
    versions = select(KnowledgeVersion.id).where(KnowledgeVersion.document_id.in_(documents))
    generation_ids = list(s.scalars(select(IndexGeneration.id).where(IndexGeneration.version_id.in_(versions))))
    job_ids = list(s.scalars(select(KnowledgeImport.id).where(KnowledgeImport.kb_id == kb_id)))
    count = s.scalar(select(func.count()).select_from(KnowledgeDocument).where(
        KnowledgeDocument.kb_id == kb_id, KnowledgeDocument.deleted_at.is_(None)))
    links = s.scalar(select(func.count()).select_from(DocumentLink).where(
        DocumentLink.kb_id == kb_id, DocumentLink.kind != DISMISSED))

    limits: dict[int, dict] = {}
    for version in s.scalars(select(KnowledgeVersion).where(KnowledgeVersion.id.in_(versions))):
        if version.material_id is not None:
            limits.setdefault(version.material_id, {}).update(_version_limits(version))
    material_ids = set(limits)
    for model, query in ((KnowledgeImport, KnowledgeImport.kb_id == kb_id), (KnowledgeParseCache, KnowledgeParseCache.kb_id == kb_id)):
        material_ids.update(i for i in s.scalars(select(model.material_id).where(query)) if i is not None)

    s.execute(delete(DocumentLink).where(DocumentLink.kb_id == kb_id))
    s.execute(delete(KnowledgeImportMetrics).where(KnowledgeImportMetrics.import_id.in_(
        select(KnowledgeImport.id).where(KnowledgeImport.kb_id == kb_id))))
    s.execute(delete(KnowledgeImport).where(KnowledgeImport.kb_id == kb_id))
    s.execute(delete(KnowledgeParseCache).where(KnowledgeParseCache.kb_id == kb_id))
    s.execute(delete(KnowledgePublication).where(KnowledgePublication.document_id.in_(documents)))
    s.execute(delete(IndexGeneration).where(IndexGeneration.version_id.in_(versions)))
    s.execute(delete(KnowledgeChunk).where(KnowledgeChunk.version_id.in_(versions)))
    s.execute(delete(KnowledgeVersion).where(KnowledgeVersion.document_id.in_(documents)))
    s.execute(delete(KnowledgeDocument).where(KnowledgeDocument.kb_id == kb_id))
    s.execute(delete(KnowledgeEgressGrant).where(KnowledgeEgressGrant.kb_id == kb_id))
    removed_materials = _retire_materials(s, material_ids, limits)
    purge_knowledge_answers(s)
    s.add(KnowledgeBaseRemoval(kb_id=kb_id, name=name, generation_ids=generation_ids, job_ids=job_ids))
    s.flush()
    s.execute(delete(KnowledgeBase).where(KnowledgeBase.id == kb_id))
    return dict(name=name, documents=count, workspace_links=links, removed_materials=removed_materials)


def _version_limits(version) -> dict:
    limits = constraints(version.source_snapshot.get("parse_quality", {}), 0, WHOLE_DOCUMENT)
    for chunk in version.chunk_manifest:
        limits.update(chunk.get("constraints", {}))
    return limits


def _retire_materials(s, material_ids, limits) -> int:
    """只属于这个库的知识库专属材料删除，其余保留。保留的材料把知识版本上的质量限制写回自己：
    材料工具原本会查知识版本的限制（evidence.material_quality_notice），版本删掉后不能因此放宽。"""
    if not material_ids:
        return 0
    used = knowledge_referenced(s, material_ids)
    used.update(s.scalars(select(FileSnapshot.material_id).where(FileSnapshot.material_id.in_(material_ids))))
    used.update(s.scalars(select(TodoMaterial.material_id).where(TodoMaterial.material_id.in_(material_ids))))
    for ids in s.scalars(select(WeeklyReport.material_ids)):
        used.update(ids or [])
    removed = 0
    for material in list(s.scalars(select(Material).where(Material.id.in_(material_ids)))):
        if material.source == KNOWLEDGE and material.id not in used:
            s.execute(update(TodoActivity).where(TodoActivity.material_id == material.id).values(material_id=None))
            s.delete(material)
            removed += 1
        elif limit := limits.get(material.id):
            report = dict(material.parse_report or {})
            report["inherited_quality_constraints"] = {**report.get("inherited_quality_constraints", {}), **limit}
            report["warnings"] = list(dict.fromkeys([*report.get("warnings", []), *(NOTES[k] for k in limit if k in NOTES)]))
            material.parse_report = report
    s.flush()
    return removed
