"""待办与周报 Checklist 的联动：复用文件追踪的候选记录（WorkEvidence）和审核流程，不另建审核系统。

周报准备（tracking.pipeline.prepare）时：
1. 待办活动 → 候选记录（origin="todo"）：每个待办每个周期一条，文字由活动确定性拼出（不调用模型），
   只写周期内发生的事（开始、完成、重新打开、进展说明），不写当前状态（当前状态在关联快照里单独显示）。
   类别：周期内完成且仍是完成 → done；有进展或开始 → in_progress；只是新建 → plan。
   只有材料关联、结构调整的待办不单独成记录。幂等：同样的活动不重复生成；未审核的旧记录被替换；
   已审核的保留并标“有更新”，只为之后的新活动生成增量记录。
2. 文件候选记录 → 关联待办：变化的文件是某待办的直接材料（按追踪键或版本材料匹配）就关联它。
   父待办的共享材料只关联父待办本身，不算每个子待办的进展；一个文件变化只有一条记录，多个待办关联也不重复。
   文件被修改不改变待办状态，也不算完成。

Checklist 里用户可以给出“本次报告的状态”（只改本次记录的类别，不改待办），
另外明确确认“同步到待办”后才修改待办状态并记活动。确认清单时冻结快照；生成周报时快照复制进周报，
历史周报只读快照，不受之后待办标题、状态、层级变化的影响。
"""
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Engine, or_, select, update
from sqlalchemy.orm import sessionmaker

from ..materials import MaterialStore
from ..tracking.evidence import _is_reviewed, period_bounds
from ..tracking.models import (
    ATTRIBUTED,
    CONFIRMED,
    PENDING,
    REJECTED,
    SUPERSEDED,
    EvidenceReview,
    FileSnapshot,
    ReportPrep,
    TrackedFile,
    WorkEvidence,
)
from .models import (
    ACTIVITY,
    COMPLETED,
    CREATED,
    DIRECT,
    DOING,
    DONE,
    MATERIAL,
    NOTE,
    REOPENED,
    STATUS,
    STATUS_NAMES,
    STATUS_SYNCED,
    TODO,
    WORK_EVENTS,
    EvidenceTodoLink,
    Todo,
    TodoActivity,
    TodoMaterial,
)
from .service import ConfirmationRequired, TodoNotFound, TodoService

TODO_ALGO = "todo-evidence-v1"


@dataclass
class TodoBuildResult:
    created: list[int] = field(default_factory=list)
    unchanged: list[int] = field(default_factory=list)
    superseded: list[int] = field(default_factory=list)
    updated_reviewed: list[int] = field(default_factory=list)
    linked: int = 0  # 新关联到待办的文件记录数


def in_plan(todo: Todo, parents: dict[int, Todo], collection_id: int) -> bool:
    """待办是否计入这个周报计划：子待办跟随父待办；没设置计划的计入所有计划。"""
    owner = parents.get(todo.parent_id, todo) if todo.parent_id else todo
    return owner.collection_id is None or owner.collection_id == collection_id


def event_text(a: TodoActivity) -> str:
    """一条活动的文字（进周报的内容，不带日期：日期在 Checklist 的明细里显示）。"""
    new = a.new_value or {}
    old = a.old_value or {}
    if a.event_type == CREATED:
        return "新建待办" if new.get("status", TODO) == TODO else f"新建待办（{STATUS_NAMES.get(new.get('status'), '')}）"
    if a.event_type == COMPLETED:
        return "标记为已完成"
    if a.event_type == REOPENED:
        return f"重新打开（改为{STATUS_NAMES.get(new.get('status'), '')}）"
    if a.event_type == STATUS:
        if new.get("status") == DOING:
            return "开始推进"
        return f"状态由{STATUS_NAMES.get(old.get('status'), '')}改为{STATUS_NAMES.get(new.get('status'), '')}"
    if a.event_type == STATUS_SYNCED:
        return f"在周报审核中确认为{STATUS_NAMES.get(new.get('status'), '')}"
    if a.event_type == NOTE:
        return f"进展：{a.note.rstrip('。')}"
    return ""


def description_parts(events: list[TodoActivity], todo: Todo) -> list[str]:
    """记录的描述：周期内发生的事。只是新建的待办写“新建待办”和截止日期；有其他进展时不再写“新建待办”。"""
    texts = [t for t in (event_text(a) for a in events if a.event_type != CREATED) if t]
    if texts:
        return texts
    created = next((a for a in events if a.event_type == CREATED), None)
    text = event_text(created) if created else "新建待办"
    return [text + (f"，截止日期 {todo.due_date.isoformat()}" if todo.due_date else "")]


def category_of(events: list[TodoActivity], end_status: str) -> str:
    kinds = {a.event_type for a in events}
    completed = any(a.event_type in (COMPLETED, STATUS_SYNCED, CREATED) and (a.new_value or {}).get("status") == DONE for a in events)
    if completed and end_status == DONE:
        return "done"
    if kinds - {CREATED}:
        return "in_progress"
    return "plan"


def _todo_info(todo: Todo, parent: Todo | None) -> dict:
    return {
        "todo_id": todo.id,
        "title": todo.title,
        "status": todo.status,
        "due_date": todo.due_date.isoformat() if todo.due_date else None,
        "parent_id": parent.id if parent else None,
        "parent_title": parent.title if parent else None,
    }


def group_path(info: dict) -> str:
    return f"{info['parent_title']} › {info['title']}" if info.get("parent_title") else info["title"]


class TodoEvidence:
    def __init__(self, engine: Engine, store: MaterialStore) -> None:
        self.engine = engine
        self.todos = TodoService(engine, store)
        self._session = sessionmaker(engine, expire_on_commit=False)

    # ---- 周报准备
    def build(self, prep: ReportPrep) -> TodoBuildResult:
        result = TodoBuildResult()
        start, end = period_bounds(prep)
        with self._session() as s:
            all_todos = {t.id: t for t in s.scalars(select(Todo))}
            scope = {i: t for i, t in all_todos.items() if in_plan(t, all_todos, prep.collection_id)}
            history = list(
                s.scalars(
                    select(TodoActivity)
                    .where(TodoActivity.todo_id.in_(list(scope)), TodoActivity.created_at <= end)
                    .order_by(TodoActivity.created_at, TodoActivity.id)
                )
            )
            acts = [a for a in history if a.created_at >= start]
            existing = list(
                s.execute(
                    select(WorkEvidence, EvidenceTodoLink)
                    .join(EvidenceTodoLink, EvidenceTodoLink.evidence_id == WorkEvidence.id)
                    .where(WorkEvidence.prep_id == prep.id, WorkEvidence.origin == "todo", EvidenceTodoLink.relation == ACTIVITY)
                )
            )
        by_todo: dict[int, list[TodoActivity]] = {}
        for a in acts:
            if a.event_type not in WORK_EVENTS:
                continue
            if a.event_type == STATUS_SYNCED and (a.new_value or {}).get("prep_id") == prep.id:
                continue  # 本次审核里同步的状态：已经在本次记录里，不再生成新记录
            by_todo.setdefault(a.todo_id, []).append(a)
        for todo_id, events in by_todo.items():
            todo = scope[todo_id]
            prior = [(e, link) for e, link in existing if link.todo_id == todo_id and e.review_status != SUPERSEDED]
            reviewed = [(e, link) for e, link in prior if _is_reviewed(e)]
            if reviewed:
                covered = max(link.snapshot.get("last_activity_id", 0) for _, link in reviewed)
                events = [a for a in events if a.id > covered]
                if not events:
                    continue
            fp = hashlib.sha256(json.dumps([TODO_ALGO, todo_id, prep.id, [a.id for a in events]]).encode()).hexdigest()
            same = next((e for e, _ in prior if e.fingerprint == fp), None)
            if same:
                result.unchanged.append(same.id)
                continue
            parent = all_todos.get(todo.parent_id) if todo.parent_id else None
            end_status = _status_after([a for a in history if a.todo_id == todo_id], todo.status)
            texts = description_parts(events, todo)
            info = _todo_info(todo, parent) | {
                "status_in_period": end_status,
                "activity_ids": [a.id for a in events],
                "last_activity_id": events[-1].id,
                "events": [{"at": a.created_at.isoformat(timespec="minutes"), "type": a.event_type, "text": event_text(a)} for a in events],
            }
            evidence = WorkEvidence(
                prep_id=prep.id,
                title=todo.title[:200],
                description="；".join(texts) + "。",
                category=category_of(events, end_status),
                origin="todo",
                source_refs=[],
                attribution=ATTRIBUTED,
                attribution_basis=["来自你的待办记录"],
                review_status=PENDING,
                flags=["todo"] + (["increment"] if reviewed else []),
                fingerprint=fp,
            )
            with self._session() as s:
                s.add(evidence)
                s.flush()
                s.add(EvidenceTodoLink(evidence_id=evidence.id, todo_id=todo_id, relation=ACTIVITY, snapshot=info))
                for e, _ in prior:
                    if _is_reviewed(e):
                        s.execute(update(WorkEvidence).where(WorkEvidence.id == e.id).values(has_update=True, related_ids=list(dict.fromkeys([*e.related_ids, evidence.id]))))
                        result.updated_reviewed.append(e.id)
                    elif e.review_status == PENDING:
                        s.execute(update(WorkEvidence).where(WorkEvidence.id == e.id).values(review_status=SUPERSEDED))
                        s.add(EvidenceReview(evidence_id=e.id, prep_id=prep.id, action="supersede", after={"by": evidence.id}, note="待办有新活动，未审核的候选已替换"))
                        result.superseded.append(e.id)
                s.commit()
            existing.append((evidence, EvidenceTodoLink(todo_id=todo_id, relation=ACTIVITY, snapshot=info)))
            result.created.append(evidence.id)
        result.linked = self.ensure_links(prep)
        return result

    def ensure_links(self, prep: ReportPrep) -> int:
        """补齐候选记录与待办的关联（幂等）：文件记录按直接材料匹配；合并、拆分出的记录继承原记录的关联。返回新建数。"""
        with self._session() as s:
            all_todos = {t.id: t for t in s.scalars(select(Todo))}
            scope = {i for i, t in all_todos.items() if in_plan(t, all_todos, prep.collection_id)}
            evidence = list(s.scalars(select(WorkEvidence).where(WorkEvidence.prep_id == prep.id, WorkEvidence.review_status != SUPERSEDED).order_by(WorkEvidence.id)))
            ids = [e.id for e in evidence]
            links = list(s.scalars(select(EvidenceTodoLink).where(EvidenceTodoLink.evidence_id.in_(ids))))
            have = {(x.evidence_id, x.todo_id) for x in links}
            todo_links = list(s.scalars(select(TodoMaterial).where(TodoMaterial.relation == DIRECT, TodoMaterial.todo_id.in_(list(scope)))))
            file_ids = {r["file_id"] for e in evidence for r in e.source_refs if r.get("file_id")}
            keys = dict(s.execute(select(TrackedFile.id, TrackedFile.path_key).where(TrackedFile.id.in_(file_ids))).all())
            snap_ids = {r.get(k) for e in evidence for r in e.source_refs for k in ("from_snapshot_id", "to_snapshot_id") if r.get(k)}
            snap_materials = dict(s.execute(select(FileSnapshot.id, FileSnapshot.material_id).where(FileSnapshot.id.in_(snap_ids))).all())
            old_links: dict[int, list[EvidenceTodoLink]] = {}
            if any(e.related_ids for e in evidence):
                related = {i for e in evidence for i in e.related_ids}
                for x in s.scalars(select(EvidenceTodoLink).where(EvidenceTodoLink.evidence_id.in_(related))):
                    old_links.setdefault(x.evidence_id, []).append(x)
            new = 0
            for e in evidence:
                wanted: dict[int, tuple[str, dict]] = {}
                if e.origin in ("merged", "split"):
                    for original in e.related_ids:
                        for x in old_links.get(original, []):
                            wanted.setdefault(x.todo_id, (x.relation, dict(x.snapshot)))
                for ref in e.source_refs:
                    key = keys.get(ref.get("file_id"))
                    materials = {snap_materials.get(ref.get(k)) for k in ("from_snapshot_id", "to_snapshot_id")} - {None}
                    for link in todo_links:
                        if (key and link.track_key == key) or link.material_id in materials:
                            todo = all_todos[link.todo_id]
                            parent = all_todos.get(todo.parent_id) if todo.parent_id else None
                            info = _todo_info(todo, parent) | {"material_id": link.material_id, "path": ref.get("path", "")}
                            if todo.parent_id is None and any(t.parent_id == todo.id and t.archived_at is None for t in all_todos.values()):
                                info["shared_with_children"] = True  # 父待办的共享材料：只是背景，不算每个子待办的进展
                            wanted.setdefault(todo.id, (MATERIAL, info))
                for todo_id, (relation, info) in wanted.items():
                    if (e.id, todo_id) in have:
                        continue
                    s.add(EvidenceTodoLink(evidence_id=e.id, todo_id=todo_id, relation=relation, snapshot=info))
                    have.add((e.id, todo_id))
                    new += 1
            s.commit()
        return new

    # ---- 查询（Checklist 显示）
    def links_for_prep(self, prep_id: int) -> dict[int, list[dict]]:
        """{记录编号: [关联的待办]}：生成时的快照 + 待办现在的标题、状态（两者分开显示）。"""
        with self._session() as s:
            rows = list(
                s.execute(
                    select(EvidenceTodoLink, Todo)
                    .join(WorkEvidence, WorkEvidence.id == EvidenceTodoLink.evidence_id)
                    .join(Todo, Todo.id == EvidenceTodoLink.todo_id)
                    .where(WorkEvidence.prep_id == prep_id)
                    .order_by(EvidenceTodoLink.id)
                )
            )
            parents = {t.id: t for t in s.scalars(select(Todo).where(Todo.id.in_([t.parent_id for _, t in rows if t.parent_id])))}
        out: dict[int, list[dict]] = {}
        for link, todo in rows:
            parent = parents.get(todo.parent_id)
            out.setdefault(link.evidence_id, []).append(
                {
                    "todo_id": todo.id,
                    "relation": link.relation,
                    "snapshot": link.snapshot,
                    "title": todo.title,
                    "status": todo.status,
                    "parent_id": todo.parent_id,
                    "parent_title": parent.title if parent else None,
                    "archived": todo.archived_at is not None,
                    "report_status": link.report_status,
                    "synced_at": link.synced_at,
                }
            )
        return out

    def suggested_materials(self, prep: ReportPrep) -> list[int]:
        """本周期有记录的待办直接关联的、不随追踪更新的材料（上传、粘贴、导入的副本）：建议加选进周报。

        追踪中的文件已经有文件变化记录，不重复建议。
        """
        with self._session() as s:
            todo_ids = s.scalars(
                select(EvidenceTodoLink.todo_id)
                .join(WorkEvidence, WorkEvidence.id == EvidenceTodoLink.evidence_id)
                .where(WorkEvidence.prep_id == prep.id, WorkEvidence.review_status.in_([PENDING, CONFIRMED]))
            ).all()
            rows = s.scalars(
                select(TodoMaterial.material_id)
                .where(TodoMaterial.todo_id.in_(set(todo_ids)), or_(TodoMaterial.track_key.is_(None), TodoMaterial.track_key.not_in(select(TrackedFile.path_key))))
                .order_by(TodoMaterial.id)
            ).all()
        return list(dict.fromkeys(rows))

    # ---- Checklist 操作
    def _link(self, s, prep_id: int, evidence_id: int, todo_id: int) -> tuple[EvidenceTodoLink, WorkEvidence]:
        e = s.get(WorkEvidence, evidence_id)
        if e is None or e.prep_id != prep_id:
            raise TodoNotFound(f"记录 E{evidence_id} 不存在")
        link = s.scalar(select(EvidenceTodoLink).where(EvidenceTodoLink.evidence_id == evidence_id, EvidenceTodoLink.todo_id == todo_id))
        if link is None:
            raise TodoNotFound(f"记录 E{evidence_id} 没有关联待办 {todo_id}")
        if e.review_status not in (PENDING, CONFIRMED, REJECTED):
            raise ValueError(f"记录 E{evidence_id} 已被合并、拆分或替换，不能再修改")
        return link, e

    def set_report_status(self, review, prep_id: int, evidence_id: int, todo_id: int, status: str | None) -> EvidenceTodoLink:
        """本次报告里这个待办的状态判断：单独保存（不改记录的描述、类别，不算“你改过”），也不改待办。

        写周报时按这个状态放栏目并写明状态（weekly._evidence_items）。和其他审核操作一样留审核记录；
        清单已确认的，回到待确认（周报内容变了）。
        """
        if status is not None and status not in STATUS_NAMES:
            raise ValueError(f"状态只能是 {' / '.join(STATUS_NAMES)}")
        with self._session() as s:
            link, _ = self._link(s, prep_id, evidence_id, todo_id)
            before = link.report_status
            if before == status:
                return link
            link.report_status = status
            link.synced_at = None
            s.add(EvidenceReview(evidence_id=evidence_id, prep_id=prep_id, action="todo_status", before={"todo_id": todo_id, "report_status": before}, after={"todo_id": todo_id, "report_status": status}, note="本次报告的状态判断"))
            review._reopen(s, prep_id)
            s.commit()
        return link

    def sync_status(self, prep_id: int, evidence_id: int, todo_id: int, confirm: bool = False) -> tuple[Todo, list[str]]:
        """把本次报告的状态同步到待办：必须 confirm=True（用户明确同意），否则返回需要确认的说明。"""
        with self._session() as s:
            link, _ = self._link(s, prep_id, evidence_id, todo_id)
            target = link.report_status
        if target is None:
            raise ValueError("请先在本次报告里选择状态，再同步到待办")
        todo = self.todos.get(todo_id)
        if todo.status == target:
            raise ValueError(f"待办「{todo.title}」已经是{STATUS_NAMES[target]}")
        if not confirm:
            extra = ""
            if target == DONE and todo.parent_id is None:
                done, total = self.todos.progress(todo.id)
                if done < total:
                    extra = f"（它还有 {total - done} 个子待办没有完成，子待办不受影响）"
            raise ConfirmationRequired(f"把待办「{todo.title}」的状态从{STATUS_NAMES[todo.status]}改为{STATUS_NAMES[target]}{extra}，并记一条活动。确定同步吗？")
        todo, notices = self.todos.set_status(
            todo_id, target, confirm=True, note="在周报审核中确认", event=STATUS_SYNCED, extra={"prep_id": prep_id, "evidence_id": evidence_id}
        )
        with self._session() as s:
            s.execute(update(EvidenceTodoLink).where(EvidenceTodoLink.evidence_id == evidence_id, EvidenceTodoLink.todo_id == todo_id).values(synced_at=datetime.now()))
            s.add(EvidenceReview(evidence_id=evidence_id, prep_id=prep_id, action="sync_todo_status", after={"todo_id": todo_id, "status": target}, note="用户确认同步到待办"))
            s.commit()
        return todo, notices

    # ---- 历史快照
    def freeze(self, prep_id: int) -> None:
        """确认清单时冻结：每条记录（已确认、已排除）的文案、审核状态，以及关联待办此刻的标题、状态、层级。"""
        now = datetime.now().isoformat(timespec="seconds")
        with self._session() as s:
            rows = list(
                s.execute(
                    select(EvidenceTodoLink, WorkEvidence, Todo)
                    .join(WorkEvidence, WorkEvidence.id == EvidenceTodoLink.evidence_id)
                    .join(Todo, Todo.id == EvidenceTodoLink.todo_id)
                    .where(WorkEvidence.prep_id == prep_id, WorkEvidence.review_status.in_([CONFIRMED, REJECTED]))
                )
            )
            for link, e, todo in rows:
                parent = s.get(Todo, todo.parent_id) if todo.parent_id else None
                link.reviewed_snapshot = {
                    "evidence": {"title": e.title, "description": e.description, "category": e.category, "review_status": e.review_status},
                    "todo": _todo_info(todo, parent),
                    "relation": link.relation,
                    "report_status": link.report_status,
                    "synced": link.synced_at is not None,
                    "frozen_at": now,
                }
            s.commit()

    def report_refs(self, evidence_ids: list[int]) -> list[dict]:
        """生成周报时写进周报的关联快照（只含已确认的记录）：之后待办怎么改，旧周报都按这份显示。"""
        if not evidence_ids:
            return []
        with self._session() as s:
            rows = list(s.scalars(select(EvidenceTodoLink).where(EvidenceTodoLink.evidence_id.in_(evidence_ids)).order_by(EvidenceTodoLink.id)))
            todos = {t.id: t for t in s.scalars(select(Todo).where(Todo.id.in_([x.todo_id for x in rows])))}
            parents = {t.id: t for t in s.scalars(select(Todo).where(Todo.id.in_([t.parent_id for t in todos.values() if t.parent_id])))}
        out = []
        for link in rows:
            frozen = link.reviewed_snapshot or {}
            todo = todos[link.todo_id]
            info = frozen.get("todo") or _todo_info(todo, parents.get(todo.parent_id))
            out.append(
                {
                    "evidence_id": link.evidence_id,
                    "todo_id": link.todo_id,
                    "relation": link.relation,
                    "todo": info,
                    "group": group_path(info),
                    "report_status": link.report_status,
                    "synced": link.synced_at is not None,
                    "evidence": frozen.get("evidence"),
                    "events": link.snapshot.get("events", []),
                }
            )
        return out


def reports_of_todo(engine: Engine, todo_id: int) -> list[dict]:
    """关联了这个待办的周报（读周报里保存的快照）。"""
    from ..weekly import WeeklyReport

    with sessionmaker(engine)() as s:
        reports = list(s.scalars(select(WeeklyReport).order_by(WeeklyReport.id.desc())))
    out = []
    for r in reports:
        refs = [x for x in (r.options or {}).get("todo_refs", []) if x.get("todo_id") == todo_id]
        if refs:
            out.append({"id": r.id, "period_start": r.period_start, "period_end": r.period_end, "status": r.status, "refs": refs})
    return out


def _status_after(events: list[TodoActivity], current: str) -> str:
    """周期结束时的状态：截至周期结束的最后一次状态变化（没有记录时用现在的状态）。"""
    for a in reversed(events):
        status = (a.new_value or {}).get("status")
        if a.event_type in (CREATED, STATUS, COMPLETED, REOPENED, STATUS_SYNCED) and status:
            return status
    return current

