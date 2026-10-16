"""待办的接口（/todos/...）：增删改查、状态、父子结构、材料关联（上传或从材料库选择）、进展说明与时间线。

接口函数用 def：上传要解析文件，FastAPI 放进线程池执行。需要用户确认的操作（如完成还有未完成子待办的父待办）返回 409。
"""
from datetime import date, datetime, time
from typing import Annotated

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import api_docs as docs
from .models import STATUS_NAMES, TODO, Todo, TodoActivity
from .service import ConfirmationRequired, LinkedMaterial, TodoService


class TodoIn(BaseModel):
    title: str
    description: str = ""
    status: str = TODO  # todo / doing / done
    due_date: date | None = None
    parent_id: int | None = None  # 只能是顶级待办
    collection_id: int | None = None  # 计入哪个周报计划；为空计入所有计划（子待办跟随父待办）


class TodoPatch(BaseModel):
    """只修改传了的字段。"""

    title: str | None = None
    description: str | None = None
    due_date: date | None = None
    collection_id: int | None = None
    parent_id: int | None = None  # 把没有子待办的顶级待办放到另一个顶级待办下


class StatusIn(BaseModel):
    status: str
    confirm: bool = False  # 父待办还有未完成的子待办时，确认仍要完成
    note: str = ""


class NoteIn(BaseModel):
    text: str


class LinkIn(BaseModel):
    material_id: int


def todo_out(t: Todo, svc: TodoService, progress: tuple[int, int] | None = None, links: int | None = None) -> dict:
    done, total = progress if progress is not None else svc.progress(t.id) if t.parent_id is None else (0, 0)
    return {
        "id": t.id,
        "title": t.title,
        "description": t.description,
        "status": t.status,
        "status_name": STATUS_NAMES.get(t.status, t.status),
        "parent_id": t.parent_id,
        "due_date": t.due_date,
        "collection_id": t.collection_id,
        "created_at": t.created_at,
        "updated_at": t.updated_at,
        "completed_at": t.completed_at,
        "archived_at": t.archived_at,
        "children_done": done,  # 只是数量（“3/4 个子待办已完成”），不代表工作量完成了多少
        "children_total": total,
        "materials": links if links is not None else len(svc.direct_links([t.id])),
    }


def material_out(m: LinkedMaterial) -> dict:
    return m.__dict__


def activity_out(a: TodoActivity) -> dict:
    return {"id": a.id, "event_type": a.event_type, "old_value": a.old_value, "new_value": a.new_value, "note": a.note, "material_id": a.material_id, "created_at": a.created_at}


def window(start: date | None, end: date | None) -> tuple[datetime | None, datetime | None]:
    return (datetime.combine(start, time.min) if start else None, datetime.combine(end, time.max) if end else None)


def register(app: FastAPI) -> None:
    def svc() -> TodoService:
        if getattr(app.state, "todos", None) is None:
            app.state.todos = TodoService(app.state.store.engine, app.state.store)
        return app.state.todos

    @app.exception_handler(ConfirmationRequired)
    def needs_confirmation(request, exc: ConfirmationRequired):
        return JSONResponse(status_code=409, content={"detail": str(exc), "confirm": True})

    def detail(t: Todo, notices: list[str] | None = None) -> dict:
        s = svc()
        out = todo_out(t, s)
        out["parent"] = None if t.parent_id is None else todo_out(s.get(t.parent_id), s)
        out["children"] = [todo_out(k, s) for k in s.children(t.id)] if t.parent_id is None else []
        out["plan_id"] = s.plan_of(t)  # 实际计入的周报计划（子待办跟随父待办）
        out["material_list"] = [material_out(m) for m in s.materials(t.id)]
        out["notices"] = notices or []
        return out

    @app.get("/todos", tags=[docs.TODOS], summary="待办列表（父子都在，按 parent_id 组织成两级）")
    def list_todos(archived: bool = False):
        s = svc()
        rows = s.list(include_archived=archived)
        counts = s.link_counts()
        kids: dict[int, list[Todo]] = {}
        for t in rows:
            if t.parent_id is not None and t.archived_at is None:
                kids.setdefault(t.parent_id, []).append(t)
        out = []
        for t in rows:
            mine = kids.get(t.id, [])
            out.append(todo_out(t, s, (sum(1 for k in mine if k.status == "done"), len(mine)), counts.get(t.id, 0)))
        return out

    @app.post("/todos", tags=[docs.TODOS], status_code=201, summary="新建待办（parent_id 指定父待办即为子待办）")
    def create_todo(body: TodoIn):
        t = svc().create(body.title, body.description, status=body.status, due_date=body.due_date, parent_id=body.parent_id, collection_id=body.collection_id)
        return detail(t)

    @app.get("/todos/{todo_id}", tags=[docs.TODOS], summary="待办详情：父待办、子待办、直接与共享材料（含追踪状态）")
    def get_todo(todo_id: int):
        return detail(svc().get(todo_id))

    @app.put("/todos/{todo_id}", tags=[docs.TODOS], summary="修改标题、说明、截止日期、周报计划、父待办（只改传了的字段）")
    def update_todo(todo_id: int, body: TodoPatch):
        fields = {k: getattr(body, k) for k in body.model_fields_set}
        if "title" in fields and fields["title"] is None:
            raise ValueError("待办标题不能为空")
        return detail(svc().update(todo_id, **fields))

    @app.post("/todos/{todo_id}/status", tags=[docs.TODOS], summary="改状态（不连带父子待办；完成有未完成子待办的父待办需 confirm）")
    def set_status(todo_id: int, body: StatusIn):
        t, notices = svc().set_status(todo_id, body.status, confirm=body.confirm, note=body.note)
        return detail(t, notices)

    @app.post("/todos/{todo_id}/promote", tags=[docs.TODOS], summary="子待办转为独立待办（保留编号、材料、活动和周报关联）")
    def promote(todo_id: int):
        return detail(svc().promote(todo_id))

    @app.delete("/todos/{todo_id}", tags=[docs.TODOS], summary="归档（软删除，可恢复）；有子待办时 children=archive 或 promote")
    def archive(todo_id: int, children: str | None = None):
        return detail(svc().archive(todo_id, children))

    @app.post("/todos/{todo_id}/restore", tags=[docs.TODOS], summary="恢复已归档的待办")
    def restore(todo_id: int):
        return detail(svc().restore(todo_id))

    # ---- 材料
    @app.get("/todos/{todo_id}/materials", tags=[docs.TODOS], summary="关联的材料：直接关联 + 来自父待办的共享材料")
    def materials(todo_id: int):
        return [material_out(m) for m in svc().materials(todo_id)]

    @app.post("/todos/{todo_id}/materials", tags=[docs.TODOS], summary="从材料库选择材料关联（不复制材料，重复关联不报错）")
    def link(todo_id: int, body: LinkIn):
        _, created = svc().link(todo_id, body.material_id)
        return {"created": created, "materials": [material_out(m) for m in svc().materials(todo_id)]}

    @app.post("/todos/{todo_id}/materials/upload", tags=[docs.TODOS], status_code=201, summary="上传本地文件：登记到材料库并关联")
    def upload(todo_id: int, file: Annotated[UploadFile, File(description="txt/md/docx/pdf/xlsx/csv")]):
        if not file.filename:
            raise HTTPException(status_code=400, detail="缺少文件名")
        material, created, _ = svc().upload(todo_id, file.filename, file.file.read())
        return {"material_id": material.id, "title": material.title, "new_material": created, "materials": [material_out(m) for m in svc().materials(todo_id)]}

    @app.delete("/todos/{todo_id}/materials/{material_id}", tags=[docs.TODOS], summary="解除关联（材料、文件、追踪历史都保留）")
    def unlink(todo_id: int, material_id: int):
        svc().unlink(todo_id, material_id)
        return [material_out(m) for m in svc().materials(todo_id)]

    @app.get("/materials/{material_id}/todos", tags=[docs.TODOS], summary="关联了这份材料的待办")
    def todos_of_material(material_id: int):
        s = svc()
        return [todo_out(t, s) for t in s.todos_of_material(material_id)]

    # ---- 活动
    @app.post("/todos/{todo_id}/notes", tags=[docs.TODOS], status_code=201, summary="添加进展说明（简短、带时间，可选）")
    def add_note(todo_id: int, body: NoteIn):
        return activity_out(svc().add_note(todo_id, body.text))

    @app.get("/todos/{todo_id}/activities", tags=[docs.TODOS], summary="活动记录（可按日期范围查询）")
    def activities(todo_id: int, start: date | None = None, end: date | None = None):
        svc().get(todo_id)
        return [activity_out(a) for a in svc().activities([todo_id], *window(start, end))]

    @app.get("/todos/{todo_id}/timeline", tags=[docs.TODOS], summary="时间线：活动 + 关联材料的文件变化（区分直接材料与父待办共享材料）")
    def timeline(todo_id: int, start: date | None = None, end: date | None = None):
        return svc().timeline(todo_id, *window(start, end))
