"""文件追踪的数据模型（业务库，与材料、周报同库）。追踪是材料（文件）自己的属性：全局一份，不分组。

实体关系：
    TrackingSource（文件、文件夹、飞书文档或飞书文件夹）
    TrackingSource(feishu_folder) 1─1 FeishuFolder、1─n FeishuFolderMember（文件夹里当前的文档）
    TrackedFile（被追踪的文件；id 是永久身份，路径可变；同一路径只有一条）
    TrackedFile 1─n FileSnapshot（版本；原始字节按 SHA-256 存在版本库，解析文本存为材料）
    ScanRun（每次扫描的触发方式、状态、统计）1─n FileChange（文件级变化，引用前后快照）
周报的审核清单（候选工作记录）在 weekly/，引用这里的文件和版本。
"""
from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    and_,
    func,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base

# 追踪源状态
ACTIVE, PAUSED, REMOVED = "active", "paused", "removed"
# 文件状态：present 正常；missing 所在文件夹可访问但文件不在；unreachable 路径或权限暂时无法访问；
# untracked 已不在任何启用的追踪源范围内
PRESENT, MISSING, UNREACHABLE, UNTRACKED = "present", "missing", "unreachable", "untracked"
# 快照类型
BASELINE, FIRST_SEEN, INCREMENTAL, FINAL = "baseline", "first_seen", "incremental", "final"
# 文件级变化
CREATED, MODIFIED, DELETED, UNCHANGED, POSSIBLY_MOVED = "CREATED", "MODIFIED", "DELETED", "UNCHANGED", "POSSIBLY_MOVED"
# 扫描触发方式与状态
REGISTER, MANUAL, DAILY, PRE_REPORT = "register", "manual", "daily", "pre_report"
RUNNING, DONE, FAILED, INTERRUPTED = "running", "done", "failed", "interrupted"
# 归属状态
ATTRIBUTED, USER_CONFIRMED, INFERRED, UNKNOWN, EXCLUDED = (
    "ATTRIBUTED",
    "USER_CONFIRMED",
    "INFERRED",
    "UNKNOWN",
    "EXCLUDED",
)
SCAN_LOCK = "scan"  # 扫描锁只有一把：同一时间只有一个扫描


class FeishuFolder(Base):
    """飞书文件夹追踪源（kind=feishu_folder）的名称与最近一次列出的统计。"""

    __tablename__ = "feishu_folders"

    source_id: Mapped[int] = mapped_column(ForeignKey("tracking_sources.id", ondelete="CASCADE"), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    docs: Mapped[int] = mapped_column(Integer, default=0)  # 追踪的文档数
    skipped: Mapped[int] = mapped_column(Integer, default=0)  # 不能追踪的项（多维表格、电子表格、文件等）
    listed_at: Mapped[datetime | None] = mapped_column(DateTime)


class FeishuFolderMember(Base):
    """飞书文件夹里最近一次列出的文档：决定哪些文档属于这个追踪源，rel_path 用于按文件夹结构显示。
    列出失败时保留上一次的名单，文档不会被误标为“已不追踪”。"""

    __tablename__ = "feishu_folder_members"

    source_id: Mapped[int] = mapped_column(ForeignKey("tracking_sources.id", ondelete="CASCADE"), primary_key=True)
    file_key: Mapped[str] = mapped_column(String(200), primary_key=True)  # feishu://docx/<id>
    rel_path: Mapped[str] = mapped_column(Text, default="")  # 子文件夹/文档标题


class TrackingSource(Base):
    __tablename__ = "tracking_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # file / folder / feishu / feishu_folder
    path: Mapped[str] = mapped_column(Text)  # 绝对路径（添加时 resolve）或 feishu://…
    recursive: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(10), default=ACTIVE)
    last_error: Mapped[str] = mapped_column(Text, default="")  # 最近一次扫描时的访问问题
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class TrackedFile(Base):
    __tablename__ = "tracked_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    path: Mapped[str] = mapped_column(Text)  # 当前路径（会变）
    path_key: Mapped[str] = mapped_column(Text, unique=True)  # 用于去重的规范化路径；同一路径只有一条记录
    path_history: Mapped[list] = mapped_column(JSON, default=list)  # [{path, until}]
    file_type: Mapped[str] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(12), default=PRESENT)
    size: Mapped[int | None] = mapped_column(Integer)  # 最近一次看到的大小和修改时间，用于快速筛选
    mtime: Mapped[float | None] = mapped_column(Float)
    last_error: Mapped[str] = mapped_column(Text, default="")
    # 可能由某个消失的文件移动或改名而来（Hash 相同的推断，不合并身份）
    moved_from_id: Mapped[int | None] = mapped_column(ForeignKey("tracked_files.id", ondelete="SET NULL"))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime)
    missing_since: Mapped[datetime | None] = mapped_column(DateTime)


class FileSnapshot(Base):
    __tablename__ = "file_snapshots"
    __table_args__ = (UniqueConstraint("file_id", "version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("tracked_files.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)  # 该文件的第几个版本，从 1 开始
    kind: Mapped[str] = mapped_column(String(12))  # baseline / first_seen / incremental / final
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size: Mapped[int] = mapped_column(Integer)
    mtime: Mapped[float] = mapped_column(Float)  # 原文件修改时间（不代表真实工作时间）
    captured_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)  # 采集时间
    path: Mapped[str] = mapped_column(Text)  # 采集时的路径
    # 解析出的文本存为材料（按内容去重）；解析失败时为空，原始字节仍在版本库，可重新解析
    material_id: Mapped[int | None] = mapped_column(ForeignKey("materials.id", ondelete="SET NULL"))
    parse_error: Mapped[str] = mapped_column(Text, default="")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)  # 文档属性、修订记录等（归属用）
    scan_run_id: Mapped[int | None] = mapped_column(ForeignKey("scan_runs.id", ondelete="SET NULL"))
    # 与上一个快照之间扫描有中断（离线、漏跑），这段时间的变化被合并成一次
    gap_before: Mapped[bool] = mapped_column(Boolean, default=False)


class ScanRun(Base):
    __tablename__ = "scan_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    trigger: Mapped[str] = mapped_column(String(12))  # register / manual / daily / pre_report
    scheduled_for: Mapped[date | None] = mapped_column(Date)  # 每日扫描对应的日期（用于判断是否已扫、补扫）
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(12), default=RUNNING)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class ScanLock(Base):
    """扫描互斥（跨进程）：插入成功即持有，租约过期后可被接手。只有一行（name = SCAN_LOCK）。"""

    __tablename__ = "scan_locks"

    name: Mapped[str] = mapped_column(String(20), primary_key=True)
    owner: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class FileChange(Base):
    __tablename__ = "file_changes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    file_id: Mapped[int] = mapped_column(ForeignKey("tracked_files.id", ondelete="CASCADE"), index=True)
    scan_run_id: Mapped[int | None] = mapped_column(ForeignKey("scan_runs.id", ondelete="SET NULL"))
    change_type: Mapped[str] = mapped_column(String(16))
    from_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("file_snapshots.id", ondelete="SET NULL"))
    to_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("file_snapshots.id", ondelete="SET NULL"))
    details: Mapped[dict] = mapped_column(JSON, default=dict)  # 旧路径、新路径、推断依据等
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)


class SnapshotDiff(Base):
    """两个快照之间的内容级 Diff 缓存：确定性计算，可随时重算；算法版本变了自动失效。"""

    __tablename__ = "snapshot_diffs"
    __table_args__ = (UniqueConstraint("from_snapshot_id", "to_snapshot_id", "algo"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("file_snapshots.id", ondelete="CASCADE"))
    to_snapshot_id: Mapped[int] = mapped_column(ForeignKey("file_snapshots.id", ondelete="CASCADE"), index=True)
    algo: Mapped[str] = mapped_column(String(20))
    result: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


def latest_versions():
    """仍在追踪的每个文件最近一个解析出文字的版本：(material_id, file_id, path_key)。

    材料库只显示这个版本（随追踪更新），旧版本在“追踪的文件”里查看历史和差异。
    """
    last = (
        select(FileSnapshot.file_id, func.max(FileSnapshot.version).label("version"))
        .where(FileSnapshot.material_id.is_not(None))
        .group_by(FileSnapshot.file_id)
        .subquery()
    )
    return (
        select(FileSnapshot.material_id, TrackedFile.id, TrackedFile.path_key)
        .join(last, and_(FileSnapshot.file_id == last.c.file_id, FileSnapshot.version == last.c.version))
        .join(TrackedFile, TrackedFile.id == FileSnapshot.file_id)
        .where(TrackedFile.status != UNTRACKED)
    )
