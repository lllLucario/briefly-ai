"""助理结果的两种明确交接：行动项预览保存、原始输入加入正式周报。

预览不写业务对象；提交核对会话版本、材料快照并在同一事务内保存回执。
不解析 Markdown，不把助手回复当作原始事实，也不让模型直接写待办或周报。
"""
import hashlib
import json
import re
import uuid
from datetime import date

from fastapi import FastAPI, HTTPException
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import BaseModel, Field
from sqlalchemy import JSON, String, select, update
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from .context_builder import input_limit
from .db import Base
from .skills import read_prompt
from .sources import conversation_material_ids
from .todos.service import TodoService
from .todos.models import Todo
from .tokens import estimate_tokens
from .weekly.models import EvidenceReview, WeeklyReport
from .weekly.review import reopen


class TodoImportDraft(Base):
    __tablename__ = "todo_import_drafts"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(32), index=True)
    revision: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict] = mapped_column(JSON)
    receipt: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Candidate(BaseModel):
    title: str = Field(min_length=1, max_length=200, description="当前回答中明确要做的事，动词加对象，不新增任务")
    source_id: str = Field(description="支撑任务的原始来源编号，如 u1 或 m3")
    quote: str = Field(min_length=1, description="从该原始来源逐字摘录的任务依据")
    owner: str = Field(default="", description="原文明示的负责人；未提及留空，不默认是用户本人")
    owner_source_id: str = ""
    owner_quote: str = ""
    deadline: str = Field(default="", description="原文中的期限，逐字摘录；没有留空，不换算相对日期")
    deadline_source_id: str = ""


class TodoCandidates(BaseModel):
    items: list[Candidate] = Field(default_factory=list, max_length=30)


class PreviewIn(BaseModel):
    message_id: str


class EditedTodo(BaseModel):
    index: int = Field(ge=0)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    due_date: date | None = None
    parent_id: int | None = None


class ImportIn(BaseModel):
    items: list[EditedTodo] = Field(min_length=1, max_length=30)


class WeeklyPreviewIn(BaseModel):
    day: date
    thread_id: str | None = None
    message_id: str | None = None
    material_ids: list[int] = Field(default_factory=list, max_length=100)


class WeeklyCommitIn(WeeklyPreviewIn):
    revision: str
    target_revision: str
    note: str = Field(default="", max_length=6000)
    request_id: str = Field(min_length=1, max_length=64)


class WeeklyInputReceipt(Base):
    __tablename__ = "weekly_input_receipts"
    request_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    report_id: Mapped[int]


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def conversation_input(agent, store, thread_id: str, message_id: str) -> dict:
    """只读取当前会话明确选中或工具确实读到的来源，不把整库或助手回复纳入原始依据。"""
    messages = agent.history_of(thread_id)
    latest = next((m for m in reversed(messages) if isinstance(m, AIMessage) and not m.tool_calls and m.text), None)
    if latest is None or latest.id != message_id:
        raise HTTPException(409, "会话已有新回答，请回到最新回答重新预览")
    sources, turn = [], 0
    for m in messages:
        if isinstance(m, HumanMessage) and not m.additional_kwargs.get("internal"):
            turn += 1
            sources.append({"id": f"u{turn}", "title": f"用户第 {turn} 轮原始输入", "content": m.additional_kwargs.get("display", m.text)})
    for mid in conversation_material_ids(messages):
        material = store.get(mid)  # 删除后不使用旧消息里的副本偷偷回填
        sources.append({"id": f"m{mid}", "title": material.title, "content": material.content, "material_id": mid})
    return {"revision": digest([message_id, sources]), "message_id": message_id, "answer": latest.text, "sources": sources}


def verified_candidates(model, snapshot: dict) -> tuple[list[dict], list[str]]:
    payload = json.dumps({"原始来源": snapshot["sources"], "当前回答（仅用于确定要保存哪些任务，不是事实依据）": snapshot["answer"]}, ensure_ascii=False)
    prompt = read_prompt("todo_import")
    if estimate_tokens(prompt + payload) > input_limit():
        raise ValueError("原始信息过长，无法完整核对；请选定相关材料另开会话后重试")
    result = model.with_structured_output(TodoCandidates, method="function_calling").invoke([("system", prompt), ("user", payload)])
    sources = {s["id"]: s for s in snapshot["sources"]}
    items, warnings = [], []
    for item in result.items:
        source = sources.get(item.source_id)
        if not source or item.quote not in source["content"]:
            warnings.append(f"“{item.title}”的任务摘录无法核对，未纳入候选")
            continue
        owner_source = sources.get(item.owner_source_id or item.source_id)
        owner_ok = not item.owner or bool(owner_source and item.owner_quote and item.owner in item.owner_quote and item.owner_quote in owner_source["content"])
        deadline_source = sources.get(item.deadline_source_id or item.source_id)
        deadline_ok = not item.deadline or bool(deadline_source and item.deadline in deadline_source["content"])
        owner = item.owner if owner_ok else ""
        deadline = item.deadline if deadline_ok else ""
        if not owner_ok or not deadline_ok:
            warnings.append(f"“{item.title}”有无法核对的负责人或期限，相关字段已置为待定")
        due = None
        # 模型可逐字保留“截止2026-10-23”；只接受单个完整日期，
        # 不从日期范围或相对期限中擅自挑一个日期。
        match = re.fullmatch(r"(?:截止(?:时间|日期)?[：:]?\s*)?(\d{4})[-年/](\d{1,2})[-月/](\d{1,2})日?", deadline)
        if match:
            try:
                due = date(*(int(v) for v in match.groups())).isoformat()
            except ValueError:
                pass
        refs = [source]
        if owner and owner_source not in refs:
            refs.append(owner_source)
        if deadline and deadline_source not in refs:
            refs.append(deadline_source)
        description = f"负责人：{owner or '待定'}\n原文期限：{deadline or '待定'}\n来源：{source['title']}\n原话：{item.quote}"
        if owner and owner_source != source:
            description += f"\n负责人依据（{owner_source['title']}）：{item.owner_quote}"
        if deadline and deadline_source != source:
            description += f"\n期限依据：{deadline_source['title']}"
        items.append({"title": item.title, "description": description, "owner": owner or "待定", "deadline": deadline or "待定", "due_date": due,
                      "source_id": source["id"], "quote": item.quote, "material_ids": list(dict.fromkeys(s["material_id"] for s in refs if s.get("material_id")))})
    return items, warnings


def register(app: FastAPI, get_agent, thread_locks, model_factory) -> None:
    store = app.state.store
    sessions = sessionmaker(store.engine, expire_on_commit=False)
    todos = TodoService(store.engine, store)

    def snapshot(thread, message):
        app.state.sessions.get(thread)
        return conversation_input(get_agent(), store, thread, message)

    @app.post("/chat/sessions/{thread_id}/todo-preview", tags=["对话"], summary="按当前回答预览待办候选，不创建任务")
    def preview_todos(thread_id: str, body: PreviewIn):
        with thread_locks[thread_id]:
            snap = snapshot(thread_id, body.message_id)
            try:
                items, warnings = verified_candidates(model_factory(), snap)
            except ValueError:
                raise
            except Exception as exc:
                raise HTTPException(502, "待办候选提取失败，未保存任何任务，请重试") from exc
            draft = TodoImportDraft(id=uuid.uuid4().hex, thread_id=thread_id, revision=snap["revision"], data={"message_id": body.message_id, "items": items, "warnings": warnings})
            with sessions() as s:
                titles = set(s.scalars(select(Todo.title).where(Todo.archived_at.is_(None))))
                for item in items:
                    item["possible_duplicate"] = item["title"] in titles
                s.add(draft)
                s.commit()
            return {"draft_id": draft.id, **draft.data}

    @app.post("/chat/sessions/{thread_id}/todo-import/{draft_id}", tags=["对话"], summary="用户确认后保存所选候选；重试返回同一次回执")
    def import_todos(thread_id: str, draft_id: str, body: ImportIn):
        with thread_locks[thread_id], sessions() as s:
            app.state.sessions.get(thread_id)
            draft = s.get(TodoImportDraft, draft_id)
            if draft is None or draft.thread_id != thread_id:
                raise HTTPException(404, "待办预览不存在")
            fingerprint = digest(body.model_dump(mode="json"))
            if draft.receipt:
                if draft.receipt["fingerprint"] != fingerprint:
                    raise HTTPException(409, "这次预览已保存，请重新预览后导入不同内容")
                return draft.receipt
            snap = snapshot(thread_id, draft.data["message_id"])
            if snap["revision"] != draft.revision:
                raise HTTPException(409, "来源材料或会话已变化，请重新预览")
            # 读取外部会话/材料后再锁定预览；写入期间不再开启其他数据库会话。
            s.execute(update(TodoImportDraft).where(TodoImportDraft.id == draft_id).values(revision=TodoImportDraft.revision))
            s.refresh(draft)
            if draft.receipt:
                if draft.receipt["fingerprint"] != fingerprint:
                    raise HTTPException(409, "这次预览已保存，请重新预览后导入不同内容")
                return draft.receipt
            indices = [x.index for x in body.items]
            candidates = draft.data["items"]
            if len(set(indices)) != len(indices) or any(i >= len(candidates) for i in indices):
                raise ValueError("候选编号重复或不存在")
            ids, duplicates = [], []
            for edited in body.items:
                if not edited.title.strip():
                    raise ValueError("待办标题不能为空")
                if s.scalar(select(Todo.id).where(Todo.title == edited.title.strip(), Todo.archived_at.is_(None))):
                    duplicates.append(edited.title.strip())
                original = candidates[edited.index]
                # 编辑后的说明不能抹掉原始负责人和出处；用户更改以补充信息明确保留。
                description = original["description"]
                if edited.description.strip() and edited.description.strip() != description:
                    description += "\n用户补充：" + edited.description.strip()
                todo = todos.create(edited.title, description, due_date=edited.due_date, parent_id=edited.parent_id, session=s)
                for mid in original["material_ids"]:
                    todos.link(todo.id, mid, session=s)
                ids.append(todo.id)
            draft.receipt = {"fingerprint": fingerprint, "todo_ids": ids, "count": len(ids), "duplicates": duplicates}
            s.commit()
            return draft.receipt

    def weekly_snapshot(body):
        if body.thread_id:
            if not body.message_id:
                raise ValueError("请选择要带入的最新回答")
            snap = snapshot(body.thread_id, body.message_id)
        else:
            snap = {"revision": "", "sources": []}
        available = {s["material_id"] for s in snap["sources"] if s.get("material_id")}
        if body.thread_id and not set(body.material_ids) <= available:
            raise ValueError("只能带入这次会话明确使用的材料")
        materials = [store.get(mid) for mid in dict.fromkeys(body.material_ids)]
        revision = digest(snap["revision"]) if body.thread_id else digest([(m.id, m.content) for m in materials])
        return snap, materials, revision

    def target_state(day):
        r = app.state.weekly.week(day, create=False)
        return r, digest([r.note, r.material_ids] if r else ["", []])

    @app.post("/weekly/input-preview", tags=["周报"], summary="预览来源、目标周及已有说明，不修改周报")
    def weekly_preview(body: WeeklyPreviewIn):
        from .weekly.service import week_of
        lock = thread_locks[body.thread_id or "weekly-input"]
        with lock:
            snap, materials, revision = weekly_snapshot(body)
            r, target_revision = target_state(body.day)
            start, end = week_of(body.day)
            return {"revision": revision, "target_revision": target_revision, "period_start": start, "period_end": end,
                    "existing_note": r.note if r else "", "existing_material_ids": r.material_ids if r else [],
                    "materials": [{"id": m.id, "title": m.title} for m in materials],
                    "available_materials": [{"id": s["material_id"], "title": s["title"]} for s in snap["sources"] if s.get("material_id")] if body.thread_id else [{"id": m.id, "title": m.title} for m in materials],
                    "source_inputs": [s for s in snap["sources"] if s["id"].startswith("u")]}

    @app.post("/weekly/input-transfer", tags=["周报"], summary="追加用户确认的原始输入；保留旧正文、审核和版本规则")
    def weekly_transfer(body: WeeklyCommitIn):
        from .weekly.service import MAX_NOTE
        with thread_locks[body.thread_id or "weekly-input"], thread_locks["weekly-input-commit"], sessions() as s:
            fingerprint = digest(body.model_dump(mode="json", exclude={"request_id"}))
            receipt = s.get(WeeklyInputReceipt, body.request_id)
            if receipt:
                if receipt.fingerprint != fingerprint:
                    raise HTTPException(409, "提交编号已使用，请重新预览")
                return {"report_id": receipt.report_id}
            _, materials, revision = weekly_snapshot(body)
            r, target_revision = target_state(body.day)
            if revision != body.revision or target_revision != body.target_revision:
                raise HTTPException(409, "来源或本周输入已变化，请重新预览，已有内容未覆盖")
            if not materials and not body.note.strip():
                raise ValueError("请至少选择一份材料或填写要追加的说明")
            if r and r.status in {"pending", "running"}:
                raise HTTPException(409, "周报正在生成，请稍后再加入输入")
            note = "\n\n".join(x for x in [r.note if r else "", body.note.strip()] if x)
            if len(note) > MAX_NOTE:
                raise ValueError(f"追加后本周说明超过 {MAX_NOTE} 字，请精简要追加的部分")
            before = {"note": r.note if r else "", "material_ids": r.material_ids if r else []}
            r = r or app.state.weekly.week(body.day)
            after = {"note": note, "material_ids": list(dict.fromkeys([*before["material_ids"], *(m.id for m in materials)]))}
            # 预览检查之外，在实际写入时再次比较输入；其他页面或进程的编辑不能被覆盖。
            changed = s.execute(update(WeeklyReport).where(
                WeeklyReport.id == r.id,
                WeeklyReport.note == before["note"],
                WeeklyReport.material_ids == before["material_ids"],
                WeeklyReport.status.not_in(["pending", "running"]),
            ).values(**after))
            if changed.rowcount != 1:
                raise HTTPException(409, "本周输入已变化，请重新预览，已有内容未覆盖")
            row = s.get(WeeklyReport, r.id)
            if before != after:
                reopen(s, row.id)
                s.add(EvidenceReview(report_id=row.id, action="input_transfer", before=before, after=after))
            s.add(WeeklyInputReceipt(request_id=body.request_id, fingerprint=fingerprint, report_id=row.id))
            s.commit()
            return {"report_id": row.id}
