"""待办：增删改查、状态、严格两级的父子结构、材料关联、活动记录与时间线。

- 父子关系是工作结构，不是周次结构：父子各自有状态和截止日期，子待办完成不会自动完成父待办。
- 删除即归档（软删除）：材料、关联、活动、周报关联都保留，可以恢复。
- 材料只存一份（材料库）：待办里上传的文件走材料库的上传流程，按内容去重；解除关联不删除材料。
- 活动只追加，按时间窗口查询，供周报准备使用；文件内容的版本和变化仍在文件追踪模块，这里只引用。
"""
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import Engine, delete, func, or_, select, update
from sqlalchemy.orm import sessionmaker

from ..materials import TRACKED, Material, MaterialNotFound, MaterialStore
from .models import (
    ARCHIVED,
    CHILD_ADDED,
    CHILD_REMOVED,
    COMPLETED,
    CREATED,
    DIRECT,
    DONE,
    MATERIAL_DELETED,
    MATERIAL_LINKED,
    MATERIAL_UNLINKED,
    NOTE,
    PROMOTED,
    REOPENED,
    RESTORED,
    STATUS,
    STATUS_NAMES,
    TODO,
    UPDATED,
    Todo,
    TodoActivity,
    TodoMaterial,
)

MAX_NOTE = 500  # 进展说明是简短的文字，不是日报
_UNSET = object()


class TodoNotFound(MaterialNotFound):
    """待办不存在。复用 MaterialNotFound，接口层统一返回 404。"""


class ConfirmationRequired(ValueError):
    """操作会影响其他待办（如父待办还有未完成的子待办），需要用户明确确认。接口返回 409。"""


@dataclass
class LinkedMaterial:
    """待办关联的一份材料：relation 为 direct（直接关联）或 shared（来自父待办的共享材料）。"""

    material_id: int  # 关联时的材料
    current_id: int | None  # 现在应打开的材料：追踪中的文件换成最新版本；材料已删除时为空
    title: str
    file_type: str | None
    source: str
    relation: str
    track_key: str | None
    tracking: list[dict]  # 追踪状态：[{plan_id, plan, file_id, status, last_error}]
    linked_at: datetime
    from_todo: int  # 关联在哪个待办上（共享材料是父待办）


def track_key_of(s, material: Material) -> str | None:
    """材料的追踪键：追踪中文件的版本 → 文件的 path_key；导入的飞书文档 → feishu://docx/<token>；其他副本为空。"""
    from ..tracking.models import FileSnapshot, TrackedFile

    if material.source == TRACKED:
        return s.scalar(
            select(TrackedFile.path_key)
            .join(FileSnapshot, FileSnapshot.file_id == TrackedFile.id)
            .where(FileSnapshot.material_id == material.id)
            .order_by(TrackedFile.id)
            .limit(1)
        )
    feishu = (material.parse_report or {}).get("feishu") or {}
    if feishu.get("kind") == "docx" and feishu.get("token"):
        return f"feishu://docx/{feishu['token']}"
    return None


class TodoService:
    def __init__(self, engine: Engine, store: MaterialStore) -> None:
        self.engine = engine
        self.store = store
        self._session = sessionmaker(engine, expire_on_commit=False)

    # ---- 查询
    def get(self, todo_id: int) -> Todo:
        with self._session() as s:
            todo = s.get(Todo, todo_id)
        if todo is None:
            raise TodoNotFound(f"待办 {todo_id} 不存在")
        return todo

    def list(self, include_archived: bool = False) -> list[Todo]:
        query = select(Todo).order_by(Todo.id)
        if not include_archived:
            query = query.where(Todo.archived_at.is_(None))
        with self._session() as s:
            return list(s.scalars(query))

    def children(self, todo_id: int, include_archived: bool = False) -> list[Todo]:
        query = select(Todo).where(Todo.parent_id == todo_id).order_by(Todo.id)
        if not include_archived:
            query = query.where(Todo.archived_at.is_(None))
        with self._session() as s:
            return list(s.scalars(query))

    def plan_of(self, todo: Todo) -> int | None:
        """待办计入的周报计划：子待办跟随父待办。"""
        if todo.parent_id is None:
            return todo.collection_id
        return self.get(todo.parent_id).collection_id

    def progress(self, todo_id: int) -> tuple[int, int]:
        """(已完成的子待办数, 子待办总数)：只是数量，不代表工作量完成了多少。"""
        kids = self.children(todo_id)
        return sum(1 for k in kids if k.status == DONE), len(kids)

    # ---- 新建与修改
    def create(
        self,
        title: str,
        description: str = "",
        *,
        status: str = TODO,
        due_date: date | None = None,
        parent_id: int | None = None,
        collection_id: int | None = None,
    ) -> Todo:
        title = title.strip()
        if not title:
            raise ValueError("待办标题不能为空")
        _check_status(status)
        with self._session() as s:
            if parent_id is not None:
                parent = self._valid_parent(s, parent_id)
                if collection_id is not None:
                    raise ValueError("子待办跟随父待办计入周报计划，不能单独设置")
                collection_id = None
            elif collection_id is not None:
                _check_collection(s, collection_id)
            todo = Todo(
                title=title[:200],
                description=description.strip(),
                status=status,
                due_date=due_date,
                parent_id=parent_id,
                collection_id=collection_id,
                completed_at=datetime.now() if status == DONE else None,
            )
            s.add(todo)
            s.flush()
            s.add(TodoActivity(todo_id=todo.id, event_type=CREATED, new_value=_values(todo)))
            if parent_id is not None:
                s.add(TodoActivity(todo_id=parent.id, event_type=CHILD_ADDED, new_value={"child_id": todo.id, "title": todo.title}))
            s.commit()
            return todo

    def update(
        self,
        todo_id: int,
        *,
        title=_UNSET,
        description=_UNSET,
        due_date=_UNSET,
        collection_id=_UNSET,
        parent_id=_UNSET,
    ) -> Todo:
        """修改标题、说明、截止日期、周报计划，或把没有子待办的顶级待办放到另一个顶级待办下。状态用 set_status。"""
        with self._session() as s:
            todo = self._active(s, todo_id)
            before = _values(todo)
            if title is not _UNSET:
                if not (title or "").strip():
                    raise ValueError("待办标题不能为空")
                todo.title = title.strip()[:200]
            if description is not _UNSET:
                todo.description = (description or "").strip()
            if due_date is not _UNSET:
                todo.due_date = due_date
            if parent_id is not _UNSET and parent_id != todo.parent_id:
                if parent_id is None:
                    raise ValueError("要把子待办变成独立待办，请使用“转为独立待办”")
                if todo.parent_id is not None:
                    raise ValueError("子待办不能直接换到别的父待办下，请先转为独立待办")
                if s.scalar(select(func.count()).select_from(Todo).where(Todo.parent_id == todo.id)):
                    raise ValueError("这个待办下面有子待办，不能再放到别的待办下（最多两级）")
                parent = self._valid_parent(s, parent_id, child_id=todo.id)
                todo.parent_id, todo.collection_id = parent.id, None
                s.add(TodoActivity(todo_id=parent.id, event_type=CHILD_ADDED, new_value={"child_id": todo.id, "title": todo.title}))
            if collection_id is not _UNSET and collection_id != todo.collection_id:
                if todo.parent_id is not None:
                    raise ValueError("子待办跟随父待办计入周报计划，不能单独设置")
                if collection_id is not None:
                    _check_collection(s, collection_id)
                todo.collection_id = collection_id
            after = _values(todo)
            changed = {k: v for k, v in after.items() if before.get(k) != v}
            if changed:
                todo.updated_at = datetime.now()
                s.add(TodoActivity(todo_id=todo.id, event_type=UPDATED, old_value={k: before.get(k) for k in changed}, new_value=changed))
            s.commit()
            return todo

    def set_status(self, todo_id: int, status: str, *, confirm: bool = False, note: str = "", event: str | None = None, extra: dict | None = None) -> tuple[Todo, list[str]]:
        """改状态，返回 (待办, 给用户的提示)。不会连带修改父待办或子待办的状态。

        - 完成父待办但还有未完成的子待办：需要 confirm=True（子待办状态不变）。
        - 子待办全部完成：提示用户考虑完成父待办（不自动完成）。
        - 重新打开：清空完成时间，原来的完成时间记在活动里。
        event / extra：由其他流程触发时（如周报 Checklist 同步状态）记的活动类型与附加信息。
        """
        _check_status(status)
        notices: list[str] = []
        with self._session() as s:
            todo = self._active(s, todo_id)
            if todo.status == status:
                return todo, notices
            if status == DONE and todo.parent_id is None:
                open_kids = [k for k in s.scalars(select(Todo).where(Todo.parent_id == todo.id, Todo.archived_at.is_(None))) if k.status != DONE]
                if open_kids and not confirm:
                    names = "、".join(f"「{k.title}」" for k in open_kids[:5])
                    raise ConfirmationRequired(f"还有 {len(open_kids)} 个子待办没有完成（{names}）。确定要先完成父待办吗？子待办的状态不会改变")
            old = {"status": todo.status, "completed_at": _iso(todo.completed_at)}
            now = datetime.now()
            todo.status = status
            if status == DONE:
                todo.completed_at = now
                kind = COMPLETED
            elif old["status"] == DONE:
                todo.completed_at = None
                kind = REOPENED
            else:
                kind = STATUS
            todo.updated_at = now
            new = {"status": status, "completed_at": _iso(todo.completed_at), **(extra or {})}
            s.add(TodoActivity(todo_id=todo.id, event_type=event or kind, old_value=old, new_value=new, note=note.strip()))
            if status == DONE and todo.parent_id is not None:
                siblings = list(s.scalars(select(Todo).where(Todo.parent_id == todo.parent_id, Todo.archived_at.is_(None))))
                parent = s.get(Todo, todo.parent_id)
                if all(k.status == DONE for k in siblings) and parent.status != DONE:
                    notices.append(f"「{parent.title}」的 {len(siblings)} 个子待办都已完成，可以考虑把它也标为完成")
            s.commit()
            return todo, notices

    def promote(self, todo_id: int) -> Todo:
        """子待办转为独立待办：保留编号、状态、直接关联的材料、活动和周报关联；父待办的共享材料不复制。"""
        with self._session() as s:
            todo = self._active(s, todo_id)
            if todo.parent_id is None:
                raise ValueError("这已经是独立待办")
            parent = s.get(Todo, todo.parent_id)
            todo.parent_id = None
            todo.collection_id = parent.collection_id  # 原来跟随父待办计入的周报计划，转为独立后保持不变
            todo.updated_at = datetime.now()
            s.add(TodoActivity(todo_id=todo.id, event_type=PROMOTED, old_value={"parent_id": parent.id, "parent_title": parent.title}, new_value={"parent_id": None}))
            s.add(TodoActivity(todo_id=parent.id, event_type=CHILD_REMOVED, new_value={"child_id": todo.id, "title": todo.title}))
            s.commit()
            return todo

    def archive(self, todo_id: int, children: str | None = None) -> Todo:
        """删除 = 归档（可恢复），关联的材料和历史都保留。

        还有未归档的子待办时必须选择：children="archive" 一起归档，children="promote" 把它们转为独立待办。
        """
        with self._session() as s:
            todo = self._active(s, todo_id)
            kids = list(s.scalars(select(Todo).where(Todo.parent_id == todo.id, Todo.archived_at.is_(None))))
        if kids:
            if children not in ("archive", "promote"):
                raise ConfirmationRequired(f"「{todo.title}」下还有 {len(kids)} 个子待办。请选择：一起归档，或把它们转为独立待办")
            for k in kids:
                if children == "promote":
                    self.promote(k.id)
                else:
                    self._archive_one(k.id, f"随父待办「{todo.title}」一起归档")
        return self._archive_one(todo.id)

    def _archive_one(self, todo_id: int, note: str = "") -> Todo:
        with self._session() as s:
            todo = s.get(Todo, todo_id)
            todo.archived_at = datetime.now()
            s.add(TodoActivity(todo_id=todo.id, event_type=ARCHIVED, note=note))
            s.commit()
            return todo

    def restore(self, todo_id: int) -> Todo:
        with self._session() as s:
            todo = s.get(Todo, todo_id)
            if todo is None:
                raise TodoNotFound(f"待办 {todo_id} 不存在")
            if todo.archived_at is None:
                return todo
            if todo.parent_id is not None and s.get(Todo, todo.parent_id).archived_at is not None:
                raise ValueError("父待办已归档，请先恢复父待办")
            todo.archived_at = None
            s.add(TodoActivity(todo_id=todo.id, event_type=RESTORED))
            s.commit()
            return todo

    # ---- 进展说明与活动
    def add_note(self, todo_id: int, text: str) -> TodoActivity:
        text = text.strip()
        if not text:
            raise ValueError("进展说明不能为空")
        if len(text) > MAX_NOTE:
            raise ValueError(f"进展说明请控制在 {MAX_NOTE} 字以内")
        with self._session() as s:
            self._active(s, todo_id)
            row = TodoActivity(todo_id=todo_id, event_type=NOTE, note=text)
            s.add(row)
            s.execute(update(Todo).where(Todo.id == todo_id).values(updated_at=datetime.now()))
            s.commit()
            return row

    def activities(self, todo_ids: list[int] | None = None, start: datetime | None = None, end: datetime | None = None) -> list[TodoActivity]:
        """按时间窗口查活动（含两端），按时间先后排列。"""
        query = select(TodoActivity).order_by(TodoActivity.created_at, TodoActivity.id)
        if todo_ids is not None:
            query = query.where(TodoActivity.todo_id.in_(todo_ids))
        if start is not None:
            query = query.where(TodoActivity.created_at >= start)
        if end is not None:
            query = query.where(TodoActivity.created_at <= end)
        with self._session() as s:
            return list(s.scalars(query))

    # ---- 材料
    def link(self, todo_id: int, material_id: int) -> tuple[TodoMaterial, bool]:
        """从材料库选择：只建关联，不复制材料。重复关联返回已有的那条。"""
        material = self.store.get(material_id)
        with self._session() as s:
            self._active(s, todo_id)
            key = track_key_of(s, material)
            existing = s.scalar(select(TodoMaterial).where(TodoMaterial.todo_id == todo_id, TodoMaterial.relation == DIRECT, _same_material(material_id, key)))
            if existing is not None:
                if material_id != existing.material_id and key:  # 同一个追踪文件的新版本：关联指向最新版本
                    existing.material_id = material_id
                    s.commit()
                return existing, False
            link = TodoMaterial(todo_id=todo_id, material_id=material_id, relation=DIRECT, track_key=key)
            s.add(link)
            s.add(TodoActivity(todo_id=todo_id, event_type=MATERIAL_LINKED, material_id=material_id, new_value={"title": material.title}))
            s.commit()
            return link, True

    def upload(self, todo_id: int, filename: str, data: bytes) -> tuple[Material, bool, TodoMaterial]:
        """在待办里上传：走材料库的上传流程（内容相同的文件只存一份），再建关联。返回 (材料, 是否新材料, 关联)。"""
        self.get(todo_id)
        material, created = self.store.add_file(filename, data)
        link, _ = self.link(todo_id, material.id)
        return material, created, link

    def unlink(self, todo_id: int, material_id: int) -> None:
        """解除关联：材料、文件、追踪历史都不受影响。"""
        with self._session() as s:
            self._active(s, todo_id)
            link = s.scalar(select(TodoMaterial).where(TodoMaterial.todo_id == todo_id, TodoMaterial.material_id == material_id))
            if link is None:
                raise TodoNotFound(f"待办 {todo_id} 没有关联材料 {material_id}")
            material = s.get(Material, material_id)
            s.delete(link)
            s.add(TodoActivity(todo_id=todo_id, event_type=MATERIAL_UNLINKED, material_id=material_id, old_value={"title": material.title if material else ""}))
            s.commit()

    def todos_of_material(self, material_id: int, same_file: bool = True) -> list[Todo]:
        """直接关联了这份材料的待办，含已归档的。same_file：同一个追踪文件 / 飞书文档的其他版本也算。"""
        with self._session() as s:
            material = s.get(Material, material_id)
            key = track_key_of(s, material) if material and same_file else None
            ids = s.scalars(select(TodoMaterial.todo_id).where(_same_material(material_id, key))).all()
            return list(s.scalars(select(Todo).where(Todo.id.in_(ids)).order_by(Todo.id)))

    def forget_material(self, material_id: int) -> None:
        """材料库里删除了材料（用户已确认）：去掉关联，每个待办记一条活动。"""
        with self._session() as s:
            material = s.get(Material, material_id)
            links = list(s.scalars(select(TodoMaterial).where(TodoMaterial.material_id == material_id)))
            for link in links:
                s.add(TodoActivity(todo_id=link.todo_id, event_type=MATERIAL_DELETED, material_id=material_id, old_value={"title": material.title if material else ""}))
            s.execute(delete(TodoMaterial).where(TodoMaterial.material_id == material_id))
            s.commit()

    def materials(self, todo_id: int) -> list[LinkedMaterial]:
        """直接关联的材料，加上来自父待办的共享材料（只读，随当前父子关系变化）。"""
        todo = self.get(todo_id)
        owners = [(todo.id, DIRECT)] + ([(todo.parent_id, "shared")] if todo.parent_id else [])
        out: list[LinkedMaterial] = []
        with self._session() as s:
            latest, tracking = _tracking_info(s)
            for owner, relation in owners:
                links = s.scalars(select(TodoMaterial).where(TodoMaterial.todo_id == owner).order_by(TodoMaterial.id))
                for link in links:
                    current = latest.get(link.track_key) if link.track_key else None
                    material = s.get(Material, current or link.material_id)
                    out.append(
                        LinkedMaterial(
                            material_id=link.material_id,
                            current_id=material.id if material else None,
                            title=material.title if material else "（材料已删除）",
                            file_type=material.file_type if material else None,
                            source=material.source if material else "",
                            relation=relation,
                            track_key=link.track_key,
                            tracking=tracking.get(link.track_key, []) if link.track_key else [],
                            linked_at=link.created_at,
                            from_todo=owner,
                        )
                    )
        return out

    def direct_links(self, todo_ids: list[int] | None = None) -> list[TodoMaterial]:
        query = select(TodoMaterial).where(TodoMaterial.relation == DIRECT)
        if todo_ids is not None:
            query = query.where(TodoMaterial.todo_id.in_(todo_ids))
        with self._session() as s:
            return list(s.scalars(query))

    def link_counts(self) -> dict[int, int]:
        with self._session() as s:
            return dict(s.execute(select(TodoMaterial.todo_id, func.count()).group_by(TodoMaterial.todo_id)).all())

    def timeline(self, todo_id: int, start: datetime | None = None, end: datetime | None = None) -> list[dict]:
        """时间窗口内这个待办发生了什么：待办活动 + 关联材料的文件变化（只引用版本，不复制内容）。

        文件变化区分 direct（直接关联的材料）和 shared（父待办的共享材料，只是背景，不算这个待办的进展）。
        """
        from ..tracking.models import FileChange, FileSnapshot, TrackedFile, TrackingCollection

        todo = self.get(todo_id)
        out = [
            {"at": a.created_at, "kind": "activity", "event_type": a.event_type, "old_value": a.old_value, "new_value": a.new_value, "note": a.note, "material_id": a.material_id}
            for a in self.activities([todo_id], start, end)
        ]
        keys: dict[str, str] = {}
        for link in self.direct_links([todo.id]):
            if link.track_key:
                keys[link.track_key] = DIRECT
        if todo.parent_id:
            for link in self.direct_links([todo.parent_id]):
                if link.track_key:
                    keys.setdefault(link.track_key, "shared")
        if keys:
            query = (
                select(FileChange, TrackedFile, TrackingCollection.name)
                .join(TrackedFile, TrackedFile.id == FileChange.file_id)
                .join(TrackingCollection, TrackingCollection.id == FileChange.collection_id)
                .where(TrackedFile.path_key.in_(list(keys)))
            )
            if start is not None:
                query = query.where(FileChange.detected_at >= start)
            if end is not None:
                query = query.where(FileChange.detected_at <= end)
            with self._session() as s:
                rows = s.execute(query).all()
                versions = {x.id: x.version for x in s.scalars(select(FileSnapshot).where(FileSnapshot.id.in_([i for ch, _, _ in rows for i in (ch.from_snapshot_id, ch.to_snapshot_id) if i])))}
            seen = set()
            for ch, f, plan in rows:
                # 多个周报计划追踪同一个文件时，同一次变化会各记一条：按文件和版本去重
                marker = (f.path_key, ch.change_type, versions.get(ch.to_snapshot_id), ch.detected_at.replace(second=0, microsecond=0))
                if marker in seen:
                    continue
                seen.add(marker)
                out.append(
                    {"at": ch.detected_at, "kind": "file_change", "change_type": ch.change_type, "path": f.path, "file_id": f.id, "plan": plan,
                     "from_version": versions.get(ch.from_snapshot_id), "to_version": versions.get(ch.to_snapshot_id), "relation": keys[f.path_key]}
                )  # fmt: skip
        out.sort(key=lambda x: x["at"])
        return out

    # ---- 内部
    def _active(self, s, todo_id: int) -> Todo:
        todo = s.get(Todo, todo_id)
        if todo is None:
            raise TodoNotFound(f"待办 {todo_id} 不存在")
        if todo.archived_at is not None:
            raise ValueError(f"待办「{todo.title}」已归档，请先恢复")
        return todo

    def _valid_parent(self, s, parent_id: int, child_id: int | None = None) -> Todo:
        if parent_id == child_id:
            raise ValueError("待办不能是自己的子待办")
        parent = s.get(Todo, parent_id)
        if parent is None:
            raise TodoNotFound(f"父待办 {parent_id} 不存在")
        if parent.archived_at is not None:
            raise ValueError(f"父待办「{parent.title}」已归档")
        if parent.parent_id is not None:
            raise ValueError(f"「{parent.title}」是子待办，子待办不能再有子待办（最多两级）。可以先把它转为独立待办")
        return parent


def _same_material(material_id: int, key: str | None):
    """同一份材料：编号相同，或属于同一个追踪文件 / 飞书文档。"""
    cond = TodoMaterial.material_id == material_id
    return or_(cond, TodoMaterial.track_key == key) if key else cond


def _tracking_info(s) -> tuple[dict[str, int], dict[str, list[dict]]]:
    """追踪键 → 最新版本的材料编号；追踪键 → 各周报计划里的追踪状态。"""
    from ..tracking.models import TrackedFile, TrackingCollection, latest_versions

    latest: dict[str, int] = {}
    for material_id, _file_id, _plan_id, key in s.execute(latest_versions()):
        latest.setdefault(key, material_id)
    plans = {c.id: c.name for c in s.scalars(select(TrackingCollection))}
    tracking: dict[str, list[dict]] = {}
    for f in s.scalars(select(TrackedFile).order_by(TrackedFile.id)):
        tracking.setdefault(f.path_key, []).append(
            {"plan_id": f.collection_id, "plan": plans.get(f.collection_id, ""), "file_id": f.id, "status": f.status, "last_error": f.last_error}
        )
    return latest, tracking


def _values(todo: Todo) -> dict:
    return {
        "title": todo.title,
        "description": todo.description,
        "status": todo.status,
        "due_date": todo.due_date.isoformat() if todo.due_date else None,
        "parent_id": todo.parent_id,
        "collection_id": todo.collection_id,
    }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") if value else None


def _check_status(status: str) -> None:
    if status not in STATUS_NAMES:
        raise ValueError(f"状态只能是 {' / '.join(STATUS_NAMES)}（{' / '.join(STATUS_NAMES.values())}）")


def _check_collection(s, collection_id: int) -> None:
    from ..tracking.models import TrackingCollection

    if s.get(TrackingCollection, collection_id) is None:
        raise TodoNotFound(f"周报计划 {collection_id} 不存在")
