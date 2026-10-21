"""材料库浏览：标题搜索、来源 / 更新方式 / 关联待办筛选、排序和分页，以及列表用的只读摘要。

只读，不改材料、关联和追踪。全部由现有数据计算（不新增表或列）：
- 来源分类：本机文件 / 粘贴文字 / 飞书；与追踪状态是两个维度（追踪的版本按原文件归类）。
- 更新方式：固定副本、追踪中、已暂停、异常；按追踪文件和追踪源的当前状态判断。
- 关联待办：语义同 TodoService.todos_of_material(same_file=True) 与 materials()，
  同一追踪文件 / 飞书文档的其他版本上的关联也算；父待办直接关联的材料对子待办共享。
查询次数固定，不随材料或条目数增长；正文只为当页条目截取开头。
"""
import re
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from .materials import TRACKED, Material, visible

PREVIEW_CHARS = 120
MAX_IDS = 100
SOURCES = ("file", "paste", "feishu")
FEISHU_TYPES = ("feishu", "bitable", "minutes")  # 飞书导入 / 追踪的材料类型
SHARED = "shared"


@dataclass
class Row:
    id: int
    title: str
    source: str
    file_type: str | None
    created_at: datetime
    feishu_kind: str | None
    feishu_token: str | None


@dataclass
class Info:
    source_category: str  # file / paste / feishu
    tracking_state: str  # fixed / tracked / paused / error
    tracking_note: str  # 暂停、异常的原因；没有时为空
    related_todos: list[dict]  # 默认不含已归档待办
    linked: bool  # 有任何直接关联（含已归档待办）
    related_ids: set[int] = field(default_factory=set)  # 直接或共享关联的全部待办（含已归档）


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _rows(s: Session, q: str = "", ids: list[int] | None = None, only_visible: bool = True) -> list[Row]:
    feishu = Material.parse_report["feishu"]
    query = select(
        Material.id, Material.title, Material.source, Material.file_type, Material.created_at,
        feishu["kind"].as_string(), feishu["token"].as_string(),
    )  # fmt: skip
    if only_visible:
        query = query.where(visible())
    if q:
        query = query.where(Material.title.ilike(f"%{_escape_like(q)}%", escape="\\"))
    if ids is not None:
        query = query.where(Material.id.in_(ids))
    return [Row(*r) for r in s.execute(query.order_by(Material.id))]


def source_category(row: Row) -> str:
    if row.source == "feishu" or row.feishu_kind or row.file_type in FEISHU_TYPES:
        return "feishu"
    return "paste" if row.source == "paste" else "file"


def _todo_keys(s: Session, rows: list[Row]) -> dict[int, str]:
    """材料 → 关联用的追踪键，与 todos.service.track_key_of 相同（批量）。"""
    from .tracking.models import FileSnapshot, TrackedFile

    out: dict[int, str] = {}
    tracked = [r.id for r in rows if r.source == TRACKED]
    if tracked:
        query = (
            select(FileSnapshot.material_id, TrackedFile.path_key)
            .join(TrackedFile, TrackedFile.id == FileSnapshot.file_id)
            .where(FileSnapshot.material_id.in_(tracked))
            .order_by(TrackedFile.id.desc())
        )
        out.update({material_id: key for material_id, key in s.execute(query)})  # 同一份材料多个文件时取编号最小的
    for r in rows:
        if r.source != TRACKED and r.feishu_kind == "docx" and r.feishu_token:
            out[r.id] = f"feishu://docx/{r.feishu_token}"
    return out


def _tracking_states(s: Session, keys: set[str]) -> dict[str, tuple[str, str]]:
    """追踪键 → (tracked / paused / error, 说明)；没有在追踪的键不出现（固定副本）。"""
    from .tracking.models import MISSING, PAUSED, REMOVED, UNREACHABLE, UNTRACKED, TrackedFile, TrackingExclusion, TrackingSource
    from .tracking.scanner import attach_members, covers

    if not keys:
        return {}
    files = {f.path_key: f for f in s.scalars(select(TrackedFile).where(TrackedFile.path_key.in_(keys)))}
    excluded = set(s.scalars(select(TrackingExclusion.path_key).where(TrackingExclusion.path_key.in_(keys))))
    sources = list(s.scalars(select(TrackingSource).where(TrackingSource.status != REMOVED)))
    attach_members(s, sources)
    out: dict[str, tuple[str, str]] = {}
    for key, f in files.items():
        covering = [x for x in sources if covers(x, key)]
        if f.status == UNTRACKED or key in excluded or not covering:
            continue
        if all(x.status == PAUSED for x in covering):
            out[key] = ("paused", "追踪已暂停，恢复后才会检查原文件的修改")
        elif f.status in (MISSING, UNREACHABLE):
            reason = "原文件已不在追踪位置" if f.status == MISSING else "原文件暂时无法访问"
            out[key] = ("error", f"{reason}{'：' + f.last_error if f.last_error else ''}")
        else:
            out[key] = ("tracked", "")
    return out


def annotate(s: Session, rows: list[Row], include_archived: bool = False) -> dict[int, Info]:
    """一批材料的来源分类、更新方式和关联待办。查询次数固定。"""
    from .todos.models import Todo, TodoMaterial
    from .tracking.models import latest_versions

    latest = {material_id: key for material_id, _file_id, key in s.execute(latest_versions())}
    todo_keys = _todo_keys(s, rows)
    state_keys = {r.id: latest.get(r.id) or todo_keys.get(r.id) for r in rows}
    states = _tracking_states(s, {k for k in state_keys.values() if k})

    ids = [r.id for r in rows]
    keys = set(todo_keys.values())
    links = s.execute(select(TodoMaterial.todo_id, TodoMaterial.material_id, TodoMaterial.track_key)).all() if ids else []
    by_material: dict[int, set[int]] = {}
    by_key: dict[str, set[int]] = {}
    wanted = set(ids)
    for todo_id, material_id, key in links:
        if material_id in wanted:
            by_material.setdefault(material_id, set()).add(todo_id)
        if key and key in keys:
            by_key.setdefault(key, set()).add(todo_id)
    todos = {t.id: t for t in s.scalars(select(Todo))} if links else {}
    children: dict[int, list[int]] = {}
    for t in sorted(todos.values(), key=lambda t: t.id):
        if t.parent_id is not None:
            children.setdefault(t.parent_id, []).append(t.id)

    out: dict[int, Info] = {}
    for r in rows:
        direct = set(by_material.get(r.id, ()))
        if (key := todo_keys.get(r.id)) is not None:
            direct |= by_key.get(key, set())
        direct &= todos.keys()
        relations: dict[int, tuple[str, int]] = {t: ("direct", t) for t in direct}
        for owner in sorted(direct):
            for child in children.get(owner, ()):
                relations.setdefault(child, (SHARED, owner))  # 直接关联优先
        related = []
        for todo_id in sorted(relations, key=lambda t: (relations[t][0] != "direct", t)):
            t = todos[todo_id]
            if t.archived_at is not None and not include_archived:
                continue
            parent = todos.get(t.parent_id) if t.parent_id else None
            relation, origin = relations[todo_id]
            related.append({
                "id": t.id, "title": t.title, "parent_id": t.parent_id, "parent_title": parent.title if parent else None,
                "archived": t.archived_at is not None, "relation": relation, "from_todo_id": origin,
            })  # fmt: skip
        state, note = states.get(state_keys[r.id] or "", ("fixed", ""))
        out[r.id] = Info(source_category(r), state, note, related, bool(direct), set(relations))
    return out


def previews(s: Session, ids: list[int]) -> dict[int, str]:
    """正文开头：合并连续空白后取前 PREVIEW_CHARS 个字符（确定性截取，不调用模型）。"""
    if not ids:
        return {}
    head = func.substr(Material.content, 1, PREVIEW_CHARS * 4)
    out = {}
    for material_id, text in s.execute(select(Material.id, head).where(Material.id.in_(ids))):
        text = re.sub(r"\s+", " ", text or "").strip()
        out[material_id] = text[:PREVIEW_CHARS] + ("…" if len(text) > PREVIEW_CHARS else "")
    return out


def describe(engine: Engine, ids: list[int], include_archived: bool = False) -> tuple[dict[int, Info], dict[int, str]]:
    """指定材料（不限默认可见）的摘要和正文预览，供材料列表、详情复用。"""
    with Session(engine) as s:
        rows = _rows(s, ids=ids, only_visible=False)
        return annotate(s, rows, include_archived), previews(s, [r.id for r in rows])


def browse(
    engine: Engine,
    *,
    q: str = "",
    source: str = "all",
    tracking: str = "all",
    relation: str = "all",
    todo_id: int | None = None,
    include_archived: bool = False,
    sort: str = "newest",
    offset: int = 0,
    limit: int = 50,
    material_ids: list[int] | None = None,
) -> tuple[list[int], int, dict[int, Info], dict[int, str]]:
    """材料库的一页：(当页材料编号, 符合条件的总数, 摘要, 正文预览)。各条件取交集，只在默认可见的材料里查。"""
    from .todos.models import Todo
    from .todos.service import TodoNotFound

    q = q.strip()
    if relation == "todo" and todo_id is None:
        raise ValueError("按具体待办筛选时需要指定待办（todo_id）")
    if relation != "todo" and todo_id is not None:
        raise ValueError("只有按具体待办筛选（relation=todo）时才能指定 todo_id")
    if material_ids is not None and len(material_ids) > MAX_IDS:
        raise ValueError(f"一次最多查看 {MAX_IDS} 份指定材料")
    with Session(engine) as s:
        if todo_id is not None and s.get(Todo, todo_id) is None:
            raise TodoNotFound(f"待办 {todo_id} 不存在")
        rows = _rows(s, q, material_ids)
        if source != "all":
            rows = [r for r in rows if source_category(r) == source]
        info = annotate(s, rows, include_archived)

        def keep(r: Row) -> bool:
            x = info[r.id]
            if tracking == "fixed" and x.tracking_state != "fixed" or tracking == "tracked" and x.tracking_state == "fixed":
                return False
            if relation == "unlinked":
                return not x.linked
            if relation == "todo":
                return todo_id in x.related_ids
            return True

        rows = [r for r in rows if keep(r)]
        if sort == "title":
            rows.sort(key=lambda r: (r.title.casefold(), r.id))
        else:
            rows.sort(key=lambda r: (r.created_at, r.id), reverse=sort == "newest")
        page = [r.id for r in rows[offset : offset + limit]]
        return page, len(rows), {i: info[i] for i in page}, previews(s, page)
