"""周报的数据模型：一周一份周报，周报里有审核清单（候选工作记录）和生成的正文。

实体关系：
    WeeklyReport（一周一行：本周说明、本周材料、清单状态、正文、生成报告）
    WeeklyReport 1─n WorkEvidence（审核清单里的候选工作记录：文件变化、待办、你补充的）
    WorkEvidence 1─n EvidenceReview（审核记录，只追加；清单级的操作 evidence_id 为空）
    WeeklyReport 1─n WeeklyReportVersion（重新生成或手动修改前的正文）
    WeeklySettings（只有一行：每周定时、默认读者与署名、你在文档里的名字）
    ScheduledRun（定时开始的执行记录）、Notification（给用户的提醒）
"""
from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base

# 审核清单的状态：草稿（可以继续审核）/ 已确认（生成时确认；之后有修改回到草稿）
DRAFT, CONFIRMED_LIST = "draft", "confirmed"
# 正文的生成状态
NONE, PENDING_GEN, RUNNING, DONE, FAILED = "none", "pending", "running", "done", "failed"
# 候选记录的审核状态：PENDING 待确认；CONFIRMED 用户确认是自己的；REJECTED 用户排除；
# MERGED / SPLIT 已被合并或拆分成别的记录（保留以便追溯）；SUPERSEDED 未审核时被新生成的记录替代
PENDING, CONFIRMED, REJECTED, MERGED, SPLIT, SUPERSEDED = "PENDING", "CONFIRMED", "REJECTED", "MERGED", "SPLIT", "SUPERSEDED"
# 定时执行记录的状态
RUN_RUNNING, RUN_DONE, RUN_FAILED, RUN_INTERRUPTED = "running", "done", "failed", "interrupted"


class WeeklyReport(Base):
    __tablename__ = "weekly_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period_start: Mapped[date] = mapped_column(Date, unique=True)  # 周一
    period_end: Mapped[date] = mapped_column(Date)  # 周日
    note: Mapped[str] = mapped_column(Text, default="")  # 本周说明：重点、材料里没有的工作（原“简述”）
    material_ids: Mapped[list] = mapped_column(JSON, default=list)  # 本周材料：整份纳入，生成时逐块提取
    removed_materials: Mapped[list] = mapped_column(JSON, default=list)  # 从材料库删除的本周材料 [{id, title, removed_at}]
    checklist: Mapped[str] = mapped_column(String(10), default=DRAFT)
    prepared_at: Mapped[datetime | None] = mapped_column(DateTime)  # 最近一次整理清单
    prep_notes: Mapped[list] = mapped_column(JSON, default=list)  # 整理清单时的提示：扫描失败、无法访问等
    final_scan_id: Mapped[int | None] = mapped_column(ForeignKey("scan_runs.id", ondelete="SET NULL"))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(10), default=NONE)  # 正文生成状态
    content: Mapped[str] = mapped_column(Text, default="")  # 正文，带出处标记；用户修改后覆盖（修改前的存为版本）
    items: Mapped[list] = mapped_column(JSON, default=list)  # 通过核对的工作项（含原文），材料删了也能查来源
    generation: Mapped[dict] = mapped_column(JSON, default=dict)  # 生成报告
    # 生成时的输入快照：读者、署名、用到的已确认记录、关联待办（审核时的标题、状态、层级）
    inputs: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    generated_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class WeeklyReportVersion(Base):
    """被替换掉的正文：重新生成前、手动修改前各存一份，可以查看和恢复。"""

    __tablename__ = "weekly_report_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("weekly_reports.id", ondelete="CASCADE"), index=True)
    content: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(String(20))  # regenerate / edit / restore
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class WorkEvidence(Base):
    __tablename__ = "work_evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("weekly_reports.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(16), default="other")  # 同 summarize 的工作项类别
    origin: Mapped[str] = mapped_column(String(10), default="generated")  # generated / todo / manual / merged / split
    # 来源：[{file_id, path, file_type, from_snapshot_id, to_snapshot_id, from_version, to_version, locations, quotes}]
    source_refs: Mapped[list] = mapped_column(JSON, default=list)
    attribution: Mapped[str] = mapped_column(String(16), default="UNKNOWN")
    attribution_basis: Mapped[list] = mapped_column(JSON, default=list)  # 每条判断依据（中文说明）
    review_status: Mapped[str] = mapped_column(String(12), default=PENDING)
    flags: Mapped[list] = mapped_column(JSON, default=list)  # parse_failed / diff_uncertain / first_seen / todo / ...
    fingerprint: Mapped[str] = mapped_column(String(64), default="", index=True)  # 生成依据的哈希，用于幂等
    user_edited: Mapped[bool] = mapped_column(Boolean, default=False)
    has_update: Mapped[bool] = mapped_column(Boolean, default=False)  # 已审核后又有新变化（另有新的待确认项）
    related_ids: Mapped[list] = mapped_column(JSON, default=list)  # 合并/拆分/更新关系
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class EvidenceReview(Base):
    """审核记录：只追加，记录每次操作前后的内容，可追溯。"""

    __tablename__ = "evidence_reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    evidence_id: Mapped[int | None] = mapped_column(ForeignKey("work_evidence.id", ondelete="CASCADE"), index=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("weekly_reports.id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(String(20))  # confirm / exclude / edit / add / merge / split / regenerate / ...
    before: Mapped[dict] = mapped_column(JSON, default=dict)
    after: Mapped[dict] = mapped_column(JSON, default=dict)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class WeeklySettings(Base):
    """周报设置，只有一行（id = 1）。"""

    __tablename__ = "weekly_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 每周定时：到点自动检查更新并整理审核清单，然后提醒你审核
    schedule_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    weekday: Mapped[int] = mapped_column(Integer, default=4)  # 0 = 周一 … 6 = 周日
    time: Mapped[str] = mapped_column(String(5), default="16:30")  # HH:MM（时区见 BRIEFLY_TRACKING_TIMEZONE）
    schedule_updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)  # 之前的时间点不补做
    audience: Mapped[str] = mapped_column(String(100), default="")  # 默认读者
    author: Mapped[str] = mapped_column(String(100), default="")  # 写在周报上的署名
    owner_names: Mapped[list] = mapped_column(JSON, default=list)  # 你在文档里的名字（Word 修订作者等），归属判断用


class ScheduledRun(Base):
    """定时开始的执行记录：同一个时间点只执行一次（失败按次数重试），可查历史。"""

    __tablename__ = "scheduled_runs"
    __table_args__ = (UniqueConstraint("occurrence", "attempt"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    occurrence: Mapped[datetime] = mapped_column(DateTime)  # 本该执行的时间点（配置时区下的本地时间）
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(12), default=RUN_RUNNING)
    report_id: Mapped[int | None] = mapped_column(ForeignKey("weekly_reports.id", ondelete="SET NULL"))
    late_minutes: Mapped[int] = mapped_column(Integer, default=0)  # 错过时间点后补做，晚了多少分钟
    error: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)


class Notification(Base):
    """给用户的提醒（命令行 inbox、网页提示条显示；配置了飞书时同时发机器人消息）。"""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))  # weekly_ready / weekly_failed
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text, default="")
    report_id: Mapped[int | None] = mapped_column(ForeignKey("weekly_reports.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    read_at: Mapped[datetime | None] = mapped_column(DateTime)
