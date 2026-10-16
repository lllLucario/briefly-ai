"""周报准备流程：最终扫描 → 生成/更新候选工作记录 → 用户审核（review.py）→ 交给现有周报生成器。

这里的函数是 CLI 和以后的定时周报模块共同的入口：
    prep, info = prepare(svc, "项目A", start, end)        # 最终扫描 + 候选清单（草稿）
    ...用户审核、确认清单（review.ReviewService）...
    report = generate_report(svc, weekly, prep.id, brief)  # 只用已确认的记录
"""
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime

from langchain_core.language_models import BaseChatModel
from sqlalchemy import select, update
from sqlalchemy.orm import sessionmaker

from ..llm import create_chat_model
from ..weekly import WeeklyReport, WeeklyService, current_week
from .evidence import BuildResult, EvidenceBuilder
from .models import (
    CHECKLIST_CONFIRMED,
    CONFIRMED,
    DRAFT,
    PENDING,
    PRE_REPORT,
    REPORTED,
    EvidenceReview,
    ReportPrep,
    WorkEvidence,
)
from .scanner import ScanResult


@dataclass
class PrepareInfo:
    scan: ScanResult | None
    build: BuildResult
    notes: list[str]
    todos: "TodoBuildResult | None" = None  # 待办活动生成的记录（todos.evidence）


def get_prep(svc, ref: str | int, start: date | None = None, end: date | None = None, create: bool = True) -> ReportPrep:
    """取某集合某周期的周报准备记录；周期默认本周一到本周日。"""
    collection = svc.collection(ref)
    default_start, default_end = current_week()
    start, end = start or default_start, end or default_end
    if start > end:
        raise ValueError("周期的开始日期不能晚于结束日期")
    session = sessionmaker(svc.engine, expire_on_commit=False)
    with session() as s:
        prep = s.scalar(
            select(ReportPrep).where(
                ReportPrep.collection_id == collection.id, ReportPrep.period_start == start, ReportPrep.period_end == end
            )
        )
        if prep is None:
            if not create:
                raise ValueError(f"集合「{collection.name}」{start}~{end} 还没有周报准备，请先运行 briefly track prepare")
            prep = ReportPrep(collection_id=collection.id, period_start=start, period_end=end)
            s.add(prep)
            s.commit()
    return prep


def prepare(
    svc,
    ref: str | int,
    start: date | None = None,
    end: date | None = None,
    *,
    scan: bool = True,
    model_factory: Callable[[], BaseChatModel] = create_chat_model,
) -> tuple[ReportPrep, PrepareInfo]:
    """最终扫描 + 生成或更新候选清单。重复执行是幂等的：没有新变化时不新增记录，已审核的记录不被覆盖。"""
    prep = get_prep(svc, ref, start, end)
    notes: list[str] = []
    scan_result = None
    if scan:
        scan_result = svc.scanner.scan(prep.collection_id, PRE_REPORT)
        if scan_result.status != "done":
            notes.append("最终扫描失败，候选清单只基于之前已采集的版本")
        if scan_result.stats.get("gap"):
            notes.append(f"{scan_result.stats['gap']['since']} 之后有每日扫描漏跑，这期间的变化被合并成一次")
        if scan_result.stats.get("unreachable"):
            notes.append(f"{scan_result.stats['unreachable']} 个文件暂时无法访问，未能采集最新版本")
        if scan_result.stats.get("unstable"):
            notes.append(f"{len(scan_result.stats['unstable'])} 个文件可能正在写入，本次未采集")
    build = EvidenceBuilder(svc, model_factory).build(prep)
    # 待办：周期内的活动生成记录（不调用模型），文件记录关联到直接材料所属的待办
    from ..todos.evidence import TodoEvidence

    todos = TodoEvidence(svc.engine, svc.store).build(prep)
    session = sessionmaker(svc.engine, expire_on_commit=False)
    with session() as s:
        values: dict = {}
        if (build.created or todos.created) and prep.status != DRAFT:
            values["status"] = DRAFT  # 确认之后又有新变化：清单回到草稿，需要重新确认
            notes.append("清单确认后又有新的变化，已回到待确认状态")
            s.add(EvidenceReview(prep_id=prep.id, action="reopen", after={"new": build.created + todos.created}, note="有新的候选记录"))
        if scan_result:
            values["final_scan_id"] = scan_result.run_id
        values["notes"] = {"messages": notes, "updated_at": datetime.now().isoformat(timespec="minutes")}
        s.execute(update(ReportPrep).where(ReportPrep.id == prep.id).values(**values))
        s.commit()
        prep = s.get(ReportPrep, prep.id)
    return prep, PrepareInfo(scan_result, build, notes, todos)


def confirmed_evidence(svc, prep_id: int) -> list[WorkEvidence]:
    session = sessionmaker(svc.engine, expire_on_commit=False)
    with session() as s:
        return list(
            s.scalars(
                select(WorkEvidence)
                .where(WorkEvidence.prep_id == prep_id, WorkEvidence.review_status == CONFIRMED)
                .order_by(WorkEvidence.id)
            )
        )


def generate_report(
    svc,
    weekly: WeeklyService,
    prep_id: int,
    brief: str = "",
    audience: str | None = None,
    name: str | None = None,
    material_ids: list[int] | None = None,
) -> WeeklyReport:
    """用已确认的工作记录生成周报草稿（同步执行；API 可改为后台）。清单必须先确认。"""
    session = sessionmaker(svc.engine, expire_on_commit=False)
    with session() as s:
        prep = s.get(ReportPrep, prep_id)
    if prep is None:
        raise ValueError(f"周报准备 {prep_id} 不存在")
    if prep.status not in (CHECKLIST_CONFIRMED, REPORTED):
        raise ValueError("候选清单还没有确认，请先运行 briefly track review 审核并确认清单")
    evidence = confirmed_evidence(svc, prep_id)
    with session() as s:
        pending = s.scalars(
            select(WorkEvidence.id).where(WorkEvidence.prep_id == prep_id, WorkEvidence.review_status == PENDING)
        ).all()
    if not evidence and not brief.strip() and not material_ids:
        raise ValueError("没有已确认的工作记录，也没有简述或素材，无法生成周报")
    from ..todos.evidence import TodoEvidence

    # 关联待办的快照写进周报：旧周报按当时的待办标题、状态、层级显示，写作时按父待办分组
    todo_refs = TodoEvidence(svc.engine, svc.store).report_refs([e.id for e in evidence])
    report = weekly.create(
        material_ids or [],
        brief,
        prep.period_start,
        prep.period_end,
        audience,
        name,
        evidence_ids=[e.id for e in evidence],
        todo_refs=todo_refs,
    )
    weekly.generate(report.id)
    with session() as s:
        s.execute(update(ReportPrep).where(ReportPrep.id == prep_id).values(status=REPORTED, weekly_report_id=report.id))
        s.add(
            EvidenceReview(
                prep_id=prep_id,
                action="report",
                after={"weekly_report_id": report.id, "evidence_ids": [e.id for e in evidence], "pending_left": list(pending)},
                note="用已确认的记录生成周报草稿" + (f"；{len(pending)} 条未审核的记录未纳入" if pending else ""),
            )
        )
        s.commit()
    return weekly.get(report.id)
