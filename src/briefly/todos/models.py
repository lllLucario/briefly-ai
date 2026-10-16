"""待办的数据模型（业务库，与材料、周报同库；全部是新表，旧库启动时由 create_all 补建）。

实体关系：
    Todo 1─n Todo（严格两级：子待办的父待办必须是顶级待办）
    Todo 1─n TodoMaterial ─ Material（材料库里的同一份材料可被多个待办关联；解除关联不删材料）
    Todo 1─n TodoActivity（只追加：状态、结构、材料关联、进展说明）
    WorkEvidence n─n Todo：EvidenceTodoLink（周报 Checklist 里的候选记录关联了哪些待办，及审核时的快照）
"""
from datetime import date, datetime

from sqlalchemy import (
    DDL,
    JSON,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base

# 状态：未开始 / 进行中 / 已完成
TODO, DOING, DONE = "todo", "doing", "done"
STATUS_NAMES = {TODO: "未开始", DOING: "进行中", DONE: "已完成"}
# 材料关联：目前只存直接关联；父待办的共享材料由父子关系实时计算，不存
DIRECT = "direct"
# 候选记录与待办的关系：activity 待办自身的活动；material 直接关联材料的文件变化
ACTIVITY, MATERIAL = "activity", "material"

# 活动类型
CREATED = "created"
UPDATED = "updated"  # 标题、说明、截止日期、周报计划
STATUS = "status"
COMPLETED = "completed"
REOPENED = "reopened"
NOTE = "note"  # 用户的进展说明
CHILD_ADDED = "child_added"  # 记在父待办上
PROMOTED = "promoted"  # 子待办转为独立待办（记在原子待办上）
CHILD_REMOVED = "child_removed"  # 记在原父待办上
MATERIAL_LINKED = "material_linked"
MATERIAL_UNLINKED = "material_unlinked"
MATERIAL_DELETED = "material_deleted"  # 材料库里删除了关联的材料
ARCHIVED = "archived"
RESTORED = "restored"
STATUS_SYNCED = "status_synced"  # 用户在周报 Checklist 里确认同步状态
# 算作“工作”的活动：只有材料关联、结构调整的待办不单独生成周报候选记录
WORK_EVENTS = (CREATED, STATUS, COMPLETED, REOPENED, NOTE, STATUS_SYNCED)


class Todo(Base):
    __tablename__ = "todos"
    __table_args__ = (CheckConstraint("parent_id IS NULL OR parent_id <> id", name="todo_not_own_parent"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(10), default=TODO)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("todos.id", ondelete="RESTRICT"), index=True)
    due_date: Mapped[date | None] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime)  # 软删除：材料、活动、周报关联都保留


class TodoMaterial(Base):
    __tablename__ = "todo_materials"
    __table_args__ = (UniqueConstraint("todo_id", "material_id", "relation"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    todo_id: Mapped[int] = mapped_column(ForeignKey("todos.id", ondelete="CASCADE"), index=True)
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id", ondelete="CASCADE"), index=True)
    relation: Mapped[str] = mapped_column(String(12), default=DIRECT)
    # 追踪键：追踪中的文件（TrackedFile.path_key）或导入的飞书文档（feishu://docx/<token>）。
    # 追踪的文件每个版本是一份新材料，靠它找到最新版本和文件变化；上传、粘贴的副本为空
    track_key: Mapped[str | None] = mapped_column(Text, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class TodoActivity(Base):
    __tablename__ = "todo_activities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    todo_id: Mapped[int] = mapped_column(ForeignKey("todos.id", ondelete="CASCADE"), index=True)
    event_type: Mapped[str] = mapped_column(String(20))
    old_value: Mapped[dict | None] = mapped_column(JSON)
    new_value: Mapped[dict | None] = mapped_column(JSON)
    note: Mapped[str] = mapped_column(Text, default="")
    material_id: Mapped[int | None] = mapped_column(Integer)  # 材料事件引用的材料（只存编号，不复制内容）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)


class EvidenceTodoLink(Base):
    """周报 Checklist 的候选记录（WorkEvidence）关联的待办。

    snapshot：生成候选记录时的待办信息；reviewed_snapshot：确认清单时冻结的记录文案、状态与归属，
    历史周报只读快照，之后改待办标题、状态、层级都不影响旧报告。
    """

    __tablename__ = "evidence_todo_links"
    __table_args__ = (UniqueConstraint("evidence_id", "todo_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    evidence_id: Mapped[int] = mapped_column(ForeignKey("work_evidence.id", ondelete="CASCADE"), index=True)
    todo_id: Mapped[int] = mapped_column(ForeignKey("todos.id", ondelete="CASCADE"), index=True)
    relation: Mapped[str] = mapped_column(String(10))  # activity / material
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    # 用户在 Checklist 里给出的本次报告状态（todo / doing / done）；为空表示沿用系统判断。只影响本次报告
    report_status: Mapped[str | None] = mapped_column(String(10))
    synced_at: Mapped[datetime | None] = mapped_column(DateTime)  # 用户确认把本次状态同步到待办的时间
    reviewed_snapshot: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


# 数据库层的两级约束（SQLite）：父待办本身不能是子待办；有子待办的待办不能变成子待办。
# 服务层另有同样的校验和中文提示；这里防止绕过服务层直接写库。其他数据库由服务层保证。
_DEPTH_GUARD = """
CREATE TRIGGER IF NOT EXISTS todos_two_levels_{op} BEFORE {OP} ON todos
WHEN NEW.parent_id IS NOT NULL AND (
    (SELECT parent_id FROM todos WHERE id = NEW.parent_id) IS NOT NULL
    OR EXISTS (SELECT 1 FROM todos WHERE parent_id = NEW.id)
)
BEGIN SELECT RAISE(ABORT, 'todos: only two levels allowed'); END
"""
for _op in ("insert", "update"):
    event.listen(Todo.__table__, "after_create", DDL(_DEPTH_GUARD.format(op=_op, OP=_op.upper())).execute_if(dialect="sqlite"))
