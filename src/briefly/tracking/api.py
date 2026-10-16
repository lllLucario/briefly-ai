"""文件追踪的接口（/tracking/...）：集合与追踪源、扫描、每周定时、周报准备、审核、生成周报。

网页上集合叫“周报计划”，在“周报”页使用；文件追踪的本周准备、追踪的文件、设置都在计划里。

接口函数用 def：扫描、生成候选记录要读文件、调模型，FastAPI 放进线程池执行。集合可以用编号或名称指定。
"""
from collections.abc import Callable
from datetime import date

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel

from .. import api_docs as docs
from ..weekly import WeeklyService
from .collection import TrackingNotFound, TrackingService
from .diff import Differ, format_hunk
from .models import CONFIRMED, PENDING, REJECTED, ReportPrep, WorkEvidence
from .pipeline import generate_report, get_prep, prepare
from .review import ReviewService
from .scanner import ScanBusy
from .weekly_schedule import WEEKDAY_NAMES, ScheduleService, describe


class CollectionIn(BaseModel):
    name: str
    owner_names: list[str] = []


class PathsIn(BaseModel):
    paths: list[str]
    recursive: bool = True


class ScheduleIn(BaseModel):
    weekday: int  # 0 = 周一 … 6 = 周日
    time: str  # HH:MM


class PeriodIn(BaseModel):
    start: date | None = None
    end: date | None = None
    scan: bool = True


class ReviewIn(BaseModel):
    action: str  # confirm / exclude / reset
    ids: list[int]
    note: str = ""


class EvidenceIn(BaseModel):
    title: str | None = None
    description: str | None = None
    category: str | None = None


class MergeIn(BaseModel):
    ids: list[int]
    title: str | None = None
    description: str | None = None


class SplitIn(BaseModel):
    parts: list[EvidenceIn]


class ReportIn(BaseModel):
    brief: str = ""
    audience: str | None = None
    name: str | None = None
    material_ids: list[int] = []  # 额外勾选的材料，与已确认的记录一起写进周报


class SettingsIn(BaseModel):
    audience: str = ""  # 默认读者
    author: str = ""  # 默认署名（写在周报上的名字）


def _source(x, folders: dict | None = None) -> dict:
    out = {"id": x.id, "kind": x.kind, "path": x.path, "recursive": x.recursive, "status": x.status, "last_error": x.last_error}
    info = (folders or {}).get(x.id)
    if info is not None:  # 飞书文件夹：名称、追踪的文档数、跳过的非文档项
        out["folder"] = {"name": info.name, "docs": info.docs, "skipped": info.skipped, "listed_at": info.listed_at}
    return out


def _scan(result) -> dict | None:
    return None if result is None else {"run_id": result.run_id, "status": result.status, "stats": result.stats}


def _evidence(e: WorkEvidence) -> dict:
    return {
        "id": e.id,
        "title": e.title,
        "description": e.description,
        "category": e.category,
        "origin": e.origin,
        "source_refs": e.source_refs,
        "attribution": e.attribution,
        "attribution_basis": e.attribution_basis,
        "review_status": e.review_status,
        "flags": e.flags,
        "user_edited": e.user_edited,
        "has_update": e.has_update,
    }


def _prep(prep: ReportPrep, review: ReviewService) -> dict:
    items = review.items(prep.id)
    counts = {s: sum(1 for e in items if e.review_status == s) for s in (PENDING, CONFIRMED, REJECTED)}
    return {
        "id": prep.id,
        "collection_id": prep.collection_id,
        "period_start": prep.period_start,
        "period_end": prep.period_end,
        "status": prep.status,
        "weekly_report_id": prep.weekly_report_id,
        "notes": (prep.notes or {}).get("messages", []),
        "counts": counts,
        "items": [_evidence(e) for e in items],
    }


def register(app: FastAPI, model_factory: Callable[[], BaseChatModel], feishu_factory: Callable | None = None) -> None:
    def svc() -> TrackingService:
        if getattr(app.state, "tracking", None) is None:
            app.state.tracking = TrackingService(app.state.store.engine, app.state.store, feishu_factory=feishu_factory)
        return app.state.tracking

    def review() -> ReviewService:
        return ReviewService(svc(), model_factory)

    @app.exception_handler(ScanBusy)
    def busy(request, exc: ScanBusy):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    def _this_week(c) -> dict | None:
        """本周的准备情况（网页左栏显示待确认数量）；还没准备时为空。"""
        try:
            prep = get_prep(svc(), c.id, create=False)
        except ValueError:
            return None
        items = review().items(prep.id)
        return {"id": prep.id, "status": prep.status, "pending": sum(1 for e in items if e.review_status == PENDING)}

    # ---- 集合（网页上叫“周报计划”）
    @app.get("/tracking/collections", tags=[docs.TRACKING], summary="集合（周报计划）列表")
    def list_collections():
        s, schedules = svc(), ScheduleService(svc())
        out = []
        for c in s.list_collections():
            schedule = schedules.get(c.id)
            settings = s.plan_settings(c.id)
            out.append(
                {
                    "id": c.id,
                    "name": c.name,
                    "owner_names": c.owner_names,
                    "sources": len(s.sources(c.id)),
                    "files": len(s.files(c.id)),
                    "schedule": None
                    if schedule is None
                    else {"weekday": schedule.weekday, "time": schedule.time, "enabled": schedule.enabled, "text": describe(schedule)},
                    "settings": {"audience": settings.audience, "author": settings.author},
                    "this_week": _this_week(c),
                    "last_scan": s.last_scan(c.id),
                }
            )
        return out

    @app.put("/tracking/collections/{ref}/settings", tags=[docs.TRACKING], summary="周报计划的默认读者和署名")
    def set_settings(ref: str, body: SettingsIn):
        row = svc().set_plan_settings(ref, body.audience, body.author)
        return {"audience": row.audience, "author": row.author}

    @app.post("/tracking/collections", tags=[docs.TRACKING], status_code=201, summary="创建集合")
    def create_collection(body: CollectionIn):
        c = svc().create_collection(body.name, body.owner_names)
        return {"id": c.id, "name": c.name, "owner_names": c.owner_names}

    @app.put("/tracking/collections/{ref}/owners", tags=[docs.TRACKING], summary="设置署名（归属判断用）")
    def set_owners(ref: str, body: CollectionIn):
        c = svc().set_owners(ref, body.owner_names)
        return {"id": c.id, "name": c.name, "owner_names": c.owner_names}

    @app.delete("/tracking/collections/{ref}", tags=[docs.TRACKING], status_code=204, summary="删除集合（不影响原文件）")
    def delete_collection(ref: str):
        svc().delete_collection(ref)

    # ---- 追踪源与文件
    @app.get("/tracking/collections/{ref}/sources", tags=[docs.TRACKING], summary="追踪源列表")
    def sources(ref: str):
        folders = svc().feishu_folders(ref)
        return [_source(x, folders) for x in svc().sources(ref)]

    @app.post("/tracking/collections/{ref}/sources", tags=[docs.TRACKING], summary="添加文件或文件夹（可多个），立即采集基线")
    def add_sources(ref: str, body: PathsIn):
        result = svc().add_paths(ref, body.paths, recursive=body.recursive)
        folders = svc().feishu_folders(ref)
        return {"added": [_source(x, folders) for x in result.added], "messages": result.messages, "scan": _scan(result.scan)}

    @app.post("/tracking/sources/{source_id}/{action}", tags=[docs.TRACKING], summary="暂停 / 恢复 / 移除追踪源")
    def source_action(source_id: int, action: str):
        s = svc()
        if action == "pause":
            return {"source": _source(s.pause_source(source_id)), "scan": None}
        if action == "resume":
            source, scan = s.resume_source(source_id)
            return {"source": _source(source), "scan": _scan(scan)}
        if action == "remove":
            return {"source": _source(s.remove_source(source_id)), "scan": None}
        raise ValueError("操作只能是 pause / resume / remove")

    @app.get("/tracking/collections/{ref}/files", tags=[docs.TRACKING], summary="被追踪的文件")
    def files(ref: str):
        s = svc()
        counts, changes, members = s.snapshot_counts(ref), s.last_changes(ref), s.folder_members(ref)
        return [
            {"id": f.id, "path": f.path, "file_type": f.file_type, "status": f.status, "versions": counts.get(f.id, 0),
             "last_change": changes.get(f.id), "last_error": f.last_error, "moved_from_id": f.moved_from_id,
             "folders": [{"source_id": sid, "rel_path": rel} for sid, rel in members.get(f.path_key, [])]}
            for f in s.files(ref)
        ]  # fmt: skip

    @app.get("/tracking/files/{file_id}/versions", tags=[docs.TRACKING], summary="一个文件的版本历史（新的在前）")
    def file_versions(file_id: int):
        snaps = svc().snapshots(file_id)
        kinds = {"baseline": "初始版本", "first_seen": "首次发现", "incremental": "检查更新", "final": "周报前检查"}
        return [
            {"id": x.id, "version": x.version, "kind": kinds.get(x.kind, x.kind), "captured_at": x.captured_at, "size": x.size,
             "parse_error": x.parse_error, "gap_before": x.gap_before}
            for x in reversed(snaps)
        ]  # fmt: skip

    @app.get("/tracking/files/{file_id}/diff", tags=[docs.TRACKING], summary="某个版本与上一个版本的差异")
    def file_diff(file_id: int, version: int):
        snaps = svc().snapshots(file_id)
        current = next((x for x in snaps if x.version == version), None)
        if current is None:
            raise TrackingNotFound(f"文件 {file_id} 没有第 {version} 个版本")
        previous = next((x for x in reversed(snaps) if x.version < version), None)
        result = Differ(svc()).diff(previous, current)
        return {
            "from_version": previous.version if previous else None,
            "to_version": current.version,
            "hunks": [format_hunk(h, 600) for h in result.hunks],
            "notes": result.notes,
            "truncated": result.truncated,
        }

    @app.post("/tracking/collections/{ref}/scan", tags=[docs.TRACKING], summary="手动扫描")
    def scan(ref: str):
        return _scan(svc().scan(ref))

    # ---- 每周定时
    @app.get("/tracking/collections/{ref}/schedule", tags=[docs.SCHEDULE], summary="每周定时准备：设置与最近执行记录")
    def get_schedule(ref: str):
        schedules = ScheduleService(svc())
        schedule = schedules.get(ref)
        runs = [
            {"occurrence": r.occurrence, "attempt": r.attempt, "status": r.status, "late_minutes": r.late_minutes, "error": r.error, "prep_id": r.prep_id}
            for r in schedules.runs(ref)
        ]
        if schedule is None:
            return {"schedule": None, "runs": runs, "weekdays": WEEKDAY_NAMES}
        return {
            "schedule": {"weekday": schedule.weekday, "time": schedule.time, "enabled": schedule.enabled, "text": describe(schedule)},
            "runs": runs,
            "weekdays": WEEKDAY_NAMES,
        }

    @app.put("/tracking/collections/{ref}/schedule", tags=[docs.SCHEDULE], summary="设置每周定时准备")
    def set_schedule(ref: str, body: ScheduleIn):
        schedule = ScheduleService(svc()).set(ref, body.weekday, body.time)
        return {"weekday": schedule.weekday, "time": schedule.time, "enabled": schedule.enabled, "text": describe(schedule)}

    @app.delete("/tracking/collections/{ref}/schedule", tags=[docs.SCHEDULE], summary="关闭每周定时准备")
    def disable_schedule(ref: str):
        schedule = ScheduleService(svc()).disable(ref)
        return {"weekday": schedule.weekday, "time": schedule.time, "enabled": schedule.enabled, "text": describe(schedule)}

    # ---- 周报准备与审核
    @app.post("/tracking/collections/{ref}/prepare", tags=[docs.REVIEW], summary="周报准备：最终扫描 + 生成候选工作记录（会调用模型）")
    def run_prepare(ref: str, body: PeriodIn):
        prep, info = prepare(svc(), ref, body.start, body.end, scan=body.scan, model_factory=model_factory)
        out = _prep(prep, review())
        out["build"] = {"created": len(info.build.created), "unchanged": len(info.build.unchanged), "calls": info.build.calls}
        out["scan"] = _scan(info.scan)
        out["notes"] = info.notes
        return out

    @app.get("/tracking/collections/{ref}/prep", tags=[docs.REVIEW], summary="某周期的周报准备（默认本周）；还没准备时返回 null")
    def current_prep(ref: str, start: date | None = None, end: date | None = None):
        try:
            prep = get_prep(svc(), ref, start, end, create=False)
        except ValueError:
            svc().collection(ref)  # 集合不存在时 404
            return None
        return _prep(prep, review())

    @app.post("/tracking/preps/{prep_id}/review", tags=[docs.REVIEW], summary="确认 / 排除 / 撤销（批量）")
    def review_items(prep_id: int, body: ReviewIn):
        r = review()
        if body.action == "confirm":
            r.confirm(prep_id, body.ids, body.note)
        elif body.action == "exclude":
            r.exclude(prep_id, body.ids, body.note)
        elif body.action == "reset":
            r.reset(prep_id, body.ids)
        else:
            raise ValueError("操作只能是 confirm / exclude / reset")
        return _prep(r.prep(prep_id), r)

    @app.put("/tracking/preps/{prep_id}/evidence/{evidence_id}", tags=[docs.REVIEW], summary="修改一条记录（修改不等于确认）")
    def edit_evidence(prep_id: int, evidence_id: int, body: EvidenceIn):
        return _evidence(review().edit(prep_id, evidence_id, body.title, body.description, body.category))

    @app.post("/tracking/preps/{prep_id}/evidence", tags=[docs.REVIEW], status_code=201, summary="补充文件没体现的工作（直接算确认）")
    def add_evidence(prep_id: int, body: EvidenceIn):
        return _evidence(review().add(prep_id, body.title or "", body.description or "", body.category or "other"))

    @app.post("/tracking/preps/{prep_id}/merge", tags=[docs.REVIEW], summary="合并几条记录")
    def merge(prep_id: int, body: MergeIn):
        return _evidence(review().merge(prep_id, body.ids, body.title, body.description))

    @app.post("/tracking/preps/{prep_id}/evidence/{evidence_id}/split", tags=[docs.REVIEW], summary="拆分一条记录")
    def split(prep_id: int, evidence_id: int, body: SplitIn):
        parts = [(p.title or "", p.description or "") for p in body.parts]
        return [_evidence(e) for e in review().split(prep_id, evidence_id, parts)]

    @app.post("/tracking/preps/{prep_id}/evidence/{evidence_id}/regenerate", tags=[docs.REVIEW], summary="重新生成描述（force 覆盖你改过的描述）")
    def regenerate(prep_id: int, evidence_id: int, force: bool = False):
        return _evidence(review().regenerate(prep_id, evidence_id, force=force))

    @app.get("/tracking/evidence/{evidence_id}/sources", tags=[docs.REVIEW], summary="一条记录的依据：文件、版本、位置、原文、变化")
    def evidence_sources(evidence_id: int):
        return [v.__dict__ for v in review().sources(evidence_id)]

    @app.get("/tracking/preps/{prep_id}/history", tags=[docs.REVIEW], summary="审核记录")
    def history(prep_id: int):
        return [{"action": r.action, "evidence_id": r.evidence_id, "note": r.note, "created_at": r.created_at} for r in review().history(prep_id)]

    @app.post("/tracking/preps/{prep_id}/confirm", tags=[docs.REVIEW], summary="确认清单（还有待确认的记录时会拒绝）")
    def confirm_checklist(prep_id: int):
        r = review()
        return _prep(r.confirm_checklist(prep_id), r)

    @app.post("/tracking/preps/{prep_id}/report", tags=[docs.REVIEW], summary="用已确认的记录生成周报草稿（会调用模型），之后在“周报”页查看")
    def report(prep_id: int, body: ReportIn):
        weekly = WeeklyService(app.state.store, model_factory)
        result = generate_report(svc(), weekly, prep_id, body.brief, body.audience, body.name, body.material_ids or None)
        return {"weekly_report_id": result.id, "status": result.status, "error": result.error}
