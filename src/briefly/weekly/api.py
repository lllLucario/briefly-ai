"""周报的接口（/weekly/...）：一周一份周报——整理审核清单、审核、本周材料与说明、生成、正文与历史版本；
周报设置（定时、读者、署名、你在文档里的名字）与提醒。

到点自动整理（schedule.py）和手动“整理本周工作”调用的是同一组接口背后的同一个流程。
接口函数用 def：整理清单要扫描文件、调模型，FastAPI 放进线程池执行；生成正文放在后台，前端轮询详情。
"""
from datetime import date
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from .. import api_docs as docs
from ..todos.evidence import TodoEvidence
from .models import CONFIRMED, PENDING, REJECTED, WeeklyReport, WorkEvidence
from .schedule import WEEKDAY_NAMES, Schedule, describe, mark_read, next_time, notifications
from .service import WeeklyService, week_of


class DayIn(BaseModel):
    day: date | None = None  # 这一天所在的那一周；不填为本周


class PrepareIn(DayIn):
    scan: bool = True  # 先检查更新


class NoteIn(BaseModel):
    note: str


class MaterialsIn(BaseModel):
    material_ids: list[int]


class ReviewIn(BaseModel):
    action: str  # confirm / exclude / reset
    ids: list[int]
    note: str = ""


class ItemIn(BaseModel):
    title: str | None = None
    description: str | None = None
    category: str | None = None


class MergeIn(BaseModel):
    ids: list[int]
    title: str | None = None
    description: str | None = None


class SplitIn(BaseModel):
    parts: list[ItemIn]


class GenerateIn(BaseModel):
    audience: str | None = None  # 不填用周报设置里的默认读者
    name: str | None = None  # 不填用周报设置里的署名


class ContentIn(BaseModel):
    content: str


class TodoStatusIn(BaseModel):
    status: str | None  # 本次周报里这个待办的状态（todo / doing / done）；为空恢复按记录判断


class SyncIn(BaseModel):
    confirm: bool = False  # 用户明确同意把本次状态同步到待办


class SettingsIn(BaseModel):
    audience: str | None = None
    author: str | None = None
    owner_names: list[str] | None = None


class ScheduleIn(BaseModel):
    weekday: int  # 0 = 周一 … 6 = 周日
    time: str  # HH:MM


class ReadIn(BaseModel):
    ids: list[int] = []


def item_out(e: WorkEvidence, todos: dict[int, list[dict]]) -> dict:
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
        "todos": todos.get(e.id, []),  # 关联的待办：生成时的快照、现在的状态、本次状态、是否已同步
    }


def summary_out(r: WeeklyReport, pending: int = 0) -> dict:
    return {
        "id": r.id,
        "period_start": r.period_start,
        "period_end": r.period_end,
        "checklist": r.checklist,  # draft / confirmed
        "status": r.status,  # none / pending / running / done / failed（正文）
        "prepared_at": r.prepared_at,
        "generated_at": r.generated_at,
        "pending": pending,
    }


def register(app: FastAPI) -> None:
    def weekly() -> WeeklyService:
        return app.state.weekly

    def detail(r: WeeklyReport | None, day: date | None = None) -> dict:
        """一周的全部内容。这一周还没有周报时返回空的那一周（id 为空）。"""
        if r is None:
            start, end = week_of(day)
            empty = {"id": None, "period_start": start, "period_end": end, "checklist": "draft", "status": "none", "prepared_at": None,
                     "generated_at": None, "pending": 0, "note": "", "material_ids": [], "removed_materials": [], "prep_notes": [],
                     "content": "", "items": [], "generation": {}, "inputs": {}, "error": "", "checklist_items": [],
                     "counts": {PENDING: 0, CONFIRMED: 0, REJECTED: 0}, "todo_material_ids": []}  # fmt: skip
            return empty
        bridge = TodoEvidence(app.state.store.engine, app.state.store)
        bridge.ensure_links(r)  # 合并、拆分出的记录继承原记录关联的待办（幂等）
        todos = bridge.links_for_report(r.id)
        items = weekly().review().items(r.id)
        counts = {s: sum(1 for e in items if e.review_status == s) for s in (PENDING, CONFIRMED, REJECTED)}
        return summary_out(r, counts[PENDING]) | {
            "note": r.note,
            "material_ids": r.material_ids,
            "removed_materials": r.removed_materials,
            "prep_notes": r.prep_notes,
            "content": r.content,  # 带出处标记〔E3〕〔1-3〕〔简述〕，导出时去掉
            "items": r.items,  # 写作用的工作项及原文，用于查看出处
            "generation": r.generation,  # 生成报告：警告（warnings）、给用户的提醒（reminders，不进入周报）等
            "inputs": r.inputs,  # 生成时的读者、署名、用到的记录、关联待办的快照
            "error": r.error,
            "checklist_items": [item_out(e, todos) for e in items],
            "counts": counts,
            "todo_material_ids": bridge.suggested_materials(r),  # 本周有记录的待办关联的材料副本，建议加入
        }

    # ---- 周
    @app.get("/weekly", tags=[docs.WEEKLY], summary="周报历史（每周一份，新的在前）")
    def list_reports():
        w = weekly()
        out = []
        for r in w.list_reports():
            pending = sum(1 for e in w.review().items(r.id) if e.review_status == PENDING)
            out.append(summary_out(r, pending))
        return out

    @app.get("/weekly/week", tags=[docs.WEEKLY], summary="某一天所在那一周的周报（默认本周）；还没有时返回空的一周（id 为空）")
    def get_week(day: date | None = None):
        return detail(weekly().week(day, create=False), day)

    @app.post("/weekly/week", tags=[docs.WEEKLY], summary="取得某一周的周报，没有就新建一份空的（加入材料、写说明前调用）")
    def ensure_week(body: DayIn):
        return detail(weekly().week(body.day))

    @app.post("/weekly/prepare", tags=[docs.WEEKLY], summary="开始：检查更新，把这一周的文件变化和待办进展整理进审核清单（文件变化会调用模型）")
    def prepare(body: PrepareIn):
        report, info = weekly().prepare(body.day, scan=body.scan)
        out = detail(report)
        out["build"] = {
            "created": len(info.build.created) + len(info.todos.created),
            "unchanged": len(info.build.unchanged) + len(info.todos.unchanged),
            "from_todos": len(info.todos.created),
            "calls": info.build.calls,
        }
        out["scan"] = None if info.scan is None else {"run_id": info.scan.run_id, "status": info.scan.status, "stats": info.scan.stats}
        out["notes"] = info.notes
        return out

    @app.get("/weekly/{report_id}", tags=[docs.WEEKLY], summary="一周的周报：审核清单、本周材料与说明、正文、生成报告（生成时轮询这个）")
    def get_report(report_id: int):
        return detail(weekly().report(report_id))

    @app.delete("/weekly/{report_id}", tags=[docs.WEEKLY], status_code=204, summary="删除这一周的周报（清单、正文、历史版本；待办、材料、追踪历史不受影响）")
    def delete_report(report_id: int):
        weekly().delete(report_id)

    @app.put("/weekly/{report_id}/note", tags=[docs.WEEKLY], summary="本周说明：重点、材料里没有的工作")
    def set_note(report_id: int, body: NoteIn):
        return detail(weekly().set_note(report_id, body.note))

    @app.put("/weekly/{report_id}/materials", tags=[docs.WEEKLY], summary="本周材料：整份纳入，生成时逐块提取")
    def set_materials(report_id: int, body: MaterialsIn):
        return detail(weekly().set_materials(report_id, body.material_ids))

    # ---- 审核清单
    @app.post("/weekly/{report_id}/review", tags=[docs.WEEKLY], summary="确认 / 排除 / 撤销（批量）")
    def review_items(report_id: int, body: ReviewIn):
        r = weekly().review()
        if body.action == "confirm":
            r.confirm(report_id, body.ids, body.note)
        elif body.action == "exclude":
            r.exclude(report_id, body.ids, body.note)
        elif body.action == "reset":
            r.reset(report_id, body.ids)
        else:
            raise ValueError("操作只能是 confirm / exclude / reset")
        return detail(weekly().report(report_id))

    @app.put("/weekly/{report_id}/items/{evidence_id}", tags=[docs.WEEKLY], summary="修改一条记录（修改不等于确认）")
    def edit_item(report_id: int, evidence_id: int, body: ItemIn):
        weekly().review().edit(report_id, evidence_id, body.title, body.description, body.category)
        return detail(weekly().report(report_id))

    @app.post("/weekly/{report_id}/items", tags=[docs.WEEKLY], status_code=201, summary="补充文件和待办体现不了的工作（直接算确认）")
    def add_item(report_id: int, body: ItemIn):
        weekly().review().add(report_id, body.title or "", body.description or "", body.category or "other")
        return detail(weekly().report(report_id))

    @app.post("/weekly/{report_id}/merge", tags=[docs.WEEKLY], summary="合并几条记录")
    def merge(report_id: int, body: MergeIn):
        weekly().review().merge(report_id, body.ids, body.title, body.description)
        return detail(weekly().report(report_id))

    @app.post("/weekly/{report_id}/items/{evidence_id}/split", tags=[docs.WEEKLY], summary="拆分一条记录")
    def split(report_id: int, evidence_id: int, body: SplitIn):
        weekly().review().split(report_id, evidence_id, [(p.title or "", p.description or "") for p in body.parts])
        return detail(weekly().report(report_id))

    @app.post("/weekly/{report_id}/items/{evidence_id}/regenerate", tags=[docs.WEEKLY], summary="重新生成描述（force 覆盖你改过的描述）")
    def regenerate(report_id: int, evidence_id: int, force: bool = False):
        weekly().review().regenerate(report_id, evidence_id, force=force)
        return detail(weekly().report(report_id))

    @app.get("/weekly/items/{evidence_id}/sources", tags=[docs.WEEKLY], summary="一条记录的依据：文件、版本、位置、原文、变化")
    def item_sources(evidence_id: int):
        return [v.__dict__ for v in weekly().review().sources(evidence_id)]

    @app.get("/weekly/{report_id}/history", tags=[docs.WEEKLY], summary="审核记录")
    def history(report_id: int):
        return [{"action": r.action, "evidence_id": r.evidence_id, "note": r.note, "created_at": r.created_at} for r in weekly().review().history(report_id)]

    @app.put("/weekly/{report_id}/items/{evidence_id}/todos/{todo_id}", tags=[docs.WEEKLY], summary="本次周报里关联待办的状态（只影响这一份周报，不改待办）")
    def todo_status(report_id: int, evidence_id: int, todo_id: int, body: TodoStatusIn):
        TodoEvidence(app.state.store.engine, app.state.store).set_report_status(report_id, evidence_id, todo_id, body.status)
        return detail(weekly().report(report_id))

    @app.post("/weekly/{report_id}/items/{evidence_id}/todos/{todo_id}/sync", tags=[docs.WEEKLY], summary="把本次状态同步到待办（必须 confirm=true，否则 409 说明会改什么）")
    def todo_sync(report_id: int, evidence_id: int, todo_id: int, body: SyncIn):
        _, notices = TodoEvidence(app.state.store.engine, app.state.store).sync_status(report_id, evidence_id, todo_id, confirm=body.confirm)
        return detail(weekly().report(report_id)) | {"notices": notices}

    @app.get("/weekly/{report_id}/todos", tags=[docs.WEEKLY], summary="周报关联的待办（生成时的快照，不随待办变化）")
    def report_todos(report_id: int):
        return (weekly().report(report_id).inputs or {}).get("todo_refs", [])

    # ---- 生成与正文
    @app.post("/weekly/{report_id}/generate", tags=[docs.WEEKLY], status_code=202, summary="确认清单并生成正文（后台生成，轮询详情看进度；会调用模型）")
    def generate(report_id: int, body: GenerateIn, background: BackgroundTasks):
        report = weekly().start_generation(report_id, body.audience, body.name)
        background.add_task(weekly().run_generation, report.id, report.generation_token)
        return detail(report)

    @app.put("/weekly/{report_id}/content", tags=[docs.WEEKLY], summary="保存修改后的正文（修改前的存为历史版本）")
    def update_content(report_id: int, body: ContentIn):
        return detail(weekly().update_content(report_id, body.content))

    @app.get("/weekly/{report_id}/versions", tags=[docs.WEEKLY], summary="正文的历史版本（重新生成、手动修改前的）")
    def versions(report_id: int):
        return [{"id": v.id, "reason": v.reason, "created_at": v.created_at, "content": v.content} for v in weekly().versions(report_id)]

    @app.post("/weekly/{report_id}/versions/{version_id}/restore", tags=[docs.WEEKLY], summary="恢复某个历史版本（当前正文也会存为版本）")
    def restore(report_id: int, version_id: int):
        return detail(weekly().restore_version(report_id, version_id))

    @app.get("/weekly/{report_id}/export.md", tags=[docs.WEEKLY], response_class=PlainTextResponse, summary="导出 Markdown（去掉出处标记）")
    def export(report_id: int):
        filename, text = weekly().export_markdown(report_id)
        return PlainTextResponse(
            text,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
        )

    # ---- 设置与提醒
    def settings_out() -> dict:
        s = weekly().settings()
        runs = Schedule(weekly()).runs()
        upcoming = next_time(s)
        return {
            "audience": s.audience,
            "author": s.author,
            "owner_names": s.owner_names,
            "schedule": {"enabled": s.schedule_enabled, "weekday": s.weekday, "time": s.time, "text": describe(s), "next": upcoming.isoformat(timespec="minutes") if upcoming else None},
            "runs": [{"occurrence": r.occurrence, "attempt": r.attempt, "status": r.status, "late_minutes": r.late_minutes, "error": r.error, "report_id": r.report_id} for r in runs],
            "weekdays": WEEKDAY_NAMES,
        }

    @app.get("/weekly-settings", tags=[docs.SCHEDULE], summary="周报设置：每周定时、默认读者与署名、你在文档里的名字，以及定时的执行记录")
    def get_settings():
        return settings_out()

    @app.put("/weekly-settings", tags=[docs.SCHEDULE], summary="修改默认读者、署名、你在文档里的名字（只改传了的字段）")
    def update_settings(body: SettingsIn):
        weekly().update_settings(audience=body.audience, author=body.author, owner_names=body.owner_names)
        return settings_out()

    @app.put("/weekly-settings/schedule", tags=[docs.SCHEDULE], summary="设置每周定时：到点自动检查更新、整理审核清单并提醒你")
    def set_schedule(body: ScheduleIn):
        Schedule(weekly()).set(body.weekday, body.time)
        return settings_out()

    @app.delete("/weekly-settings/schedule", tags=[docs.SCHEDULE], summary="关闭每周定时")
    def disable_schedule():
        Schedule(weekly()).disable()
        return settings_out()

    @app.get("/notifications", tags=[docs.SCHEDULE], summary="提醒（审核清单已整理好、自动整理失败等）")
    def list_notifications(unread_only: bool = True):
        return [
            {"id": n.id, "kind": n.kind, "title": n.title, "body": n.body, "report_id": n.report_id, "created_at": n.created_at, "read_at": n.read_at}
            for n in notifications(app.state.store.engine, unread_only)
        ]

    @app.post("/notifications/read", tags=[docs.SCHEDULE], summary="标为已读（不传 ids 表示全部）")
    def read_notifications(body: ReadIn):
        return {"marked": mark_read(app.state.store.engine, body.ids or None)}
