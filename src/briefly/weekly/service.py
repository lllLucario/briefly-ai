"""一周一份周报：整理审核清单 → 你审核 → 生成。到点自动整理和手动“整理本周工作”走的是同一个流程。

    report, info = weekly.prepare()           # 检查更新 + 文件变化和待办整理成候选记录（本周那一份）
    ...审核清单（weekly.review()）、本周材料、本周说明...
    weekly.generate(report.id)                # 确认清单 + 生成正文（只用已确认的记录、本周材料、本周说明）

周报按自然周（周一到周日）。重新生成仍是同一份周报，被替换的正文存为历史版本。
"""
import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from langchain_core.language_models import BaseChatModel
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from ..clock import local_today
from ..llm import create_chat_model
from ..materials import MaterialNotFound, MaterialStore
from .engine import ReportWriter, strip_source_tags
from .evidence import BuildResult, EvidenceBuilder
from .prepare_lock import PrepareLease
from .models import (
    CONFIRMED,
    CONFIRMED_LIST,
    DONE,
    FAILED,
    PENDING,
    PENDING_GEN,
    RUNNING,
    EvidenceReview,
    WeeklyReport,
    WeeklyReportVersion,
    WeeklyGenerationLease,
    WeeklySettings,
    WorkEvidence,
)
from .review import ReviewService, reopen

log = logging.getLogger(__name__)
GENERATION_TIMEOUT = timedelta(minutes=30)
GENERATION_HEARTBEAT_SECONDS = 60


def generation_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


MAX_NOTE = 2000


class ReportNotFound(MaterialNotFound):
    """周报不存在。复用 MaterialNotFound，接口层统一返回 404。"""


@dataclass
class PrepareInfo:
    scan: object | None  # tracking.scanner.ScanResult
    build: BuildResult
    todos: object  # todos.evidence.TodoBuildResult
    notes: list[str] = field(default_factory=list)


def week_of(day: date | None = None) -> tuple[date, date]:
    """某天所在的自然周：周一到周日。由代码计算，不让模型算日期。"""
    day = day or local_today()
    monday = day - timedelta(days=day.weekday())
    return monday, monday + timedelta(days=6)


class WeeklyService:
    def __init__(self, store: MaterialStore, model_factory: Callable[[], BaseChatModel] = create_chat_model, tracking=None) -> None:
        """tracking：TrackingService；不传时用到才按默认配置创建（只写周报、不追踪文件时用不到）。"""
        self.store = store
        self.engine = store.engine
        self._model_factory = model_factory
        self._tracking = tracking
        self._session = sessionmaker(store.engine, expire_on_commit=False)

    @property
    def tracking(self):
        if self._tracking is None:
            from ..tracking.service import TrackingService

            self._tracking = TrackingService(self.engine, self.store)
        return self._tracking

    def _fail_interrupted(self) -> None:
        """只有租约失联超过阈值才恢复；旧库无租约的活动任务先获得完整宽限期。"""
        now = generation_now()
        with self._session() as s:
            missing = list(s.scalars(select(WeeklyReport.id).where(
                WeeklyReport.status.in_([PENDING_GEN, RUNNING]),
                ~select(WeeklyGenerationLease.report_id).where(WeeklyGenerationLease.report_id == WeeklyReport.id).exists(),
            )))
        for report_id in missing:
            with self._session() as s:
                s.add(WeeklyGenerationLease(report_id=report_id, token=uuid.uuid4().hex, heartbeat_at=now))
                try:
                    s.commit()
                except IntegrityError:  # 另一个进程已初始化或周报已删除
                    s.rollback()
        with self._session() as s:
            expired = select(WeeklyGenerationLease.report_id).where(
                WeeklyGenerationLease.report_id == WeeklyReport.id,
                WeeklyGenerationLease.heartbeat_at < now - GENERATION_TIMEOUT,
            ).exists()
            s.execute(update(WeeklyReport).where(
                WeeklyReport.status.in_([PENDING_GEN, RUNNING]), expired,
            ).values(status=FAILED, error="生成中断：超过30分钟未收到任务续期，请重新生成"))
            s.commit()

    def _owns_generation(self, report_id, token):
        return select(WeeklyGenerationLease.report_id).where(
            WeeklyGenerationLease.report_id == report_id,
            WeeklyGenerationLease.token == token,
        ).exists()

    def _renew_generation(self, report_id, token) -> bool:
        with self._session() as s:
            result = s.execute(update(WeeklyGenerationLease).where(
                WeeklyGenerationLease.report_id == report_id, WeeklyGenerationLease.token == token,
                select(WeeklyReport.id).where(WeeklyReport.id == report_id, WeeklyReport.status == RUNNING).exists(),
            ).values(heartbeat_at=generation_now()))
            s.commit()
            return result.rowcount == 1

    def _heartbeat_generation(self, report_id, token, stop):
        while not stop.wait(GENERATION_HEARTBEAT_SECONDS):
            try:
                if not self._renew_generation(report_id, token):
                    return
            except Exception:
                log.exception("周报生成续期失败，稍后重试")

    # ---- 设置
    def settings(self) -> WeeklySettings:
        with self._session() as s:
            row = s.get(WeeklySettings, 1)
            if row is None:
                row = WeeklySettings(id=1)
                s.add(row)
                try:
                    s.commit()
                except IntegrityError:
                    # 首次打开页面会并发读取周报与设置，另一个请求可能已创建单例。
                    s.rollback()
                    row = s.get(WeeklySettings, 1)
                    if row is None:
                        raise
            return row

    def update_settings(self, *, audience: str | None = None, author: str | None = None, owner_names: list[str] | None = None) -> WeeklySettings:
        """默认读者、署名、你在文档里的名字。定时见 schedule.py。"""
        self.settings()
        with self._session() as s:
            row = s.get(WeeklySettings, 1)
            if audience is not None:
                row.audience = audience.strip()[:100]
            if author is not None:
                row.author = author.strip()[:100]
            if owner_names is not None:
                row.owner_names = [n.strip() for n in owner_names if n.strip()]
            s.commit()
            return row

    def review(self) -> ReviewService:
        return ReviewService(self.tracking, self._model_factory, self.settings().owner_names)

    # ---- 周
    def week(self, day: date | None = None, create: bool = True) -> WeeklyReport | None:
        """某天所在那一周的周报；还没有时 create=True 新建一份空的（清单为草稿、还没生成）。"""
        start, end = week_of(day)
        with self._session() as s:
            report = s.scalar(select(WeeklyReport).where(WeeklyReport.period_start == start))
            if report is None and create:
                report = WeeklyReport(period_start=start, period_end=end)
                s.add(report)
                try:
                    s.commit()
                except IntegrityError:
                    s.rollback()
                    report = s.scalar(select(WeeklyReport).where(WeeklyReport.period_start == start))
                    if report is None:
                        raise
            return report

    def report(self, report_id: int) -> WeeklyReport:
        self._fail_interrupted()
        with self._session() as s:
            report = s.get(WeeklyReport, report_id)
        if report is None:
            raise ReportNotFound(f"周报 {report_id} 不存在")
        return report

    def list_reports(self) -> list[WeeklyReport]:
        self._fail_interrupted()
        with self._session() as s:
            return list(s.scalars(select(WeeklyReport).order_by(WeeklyReport.period_start.desc())))

    def delete(self, report_id: int) -> None:
        """删除这一周的周报：清单、正文、历史版本一起删除。待办、材料、追踪历史不受影响。"""
        from ..todos.models import EvidenceTodoLink

        self.report(report_id)
        with self._session() as s:
            evidence = select(WorkEvidence.id).where(WorkEvidence.report_id == report_id)
            s.query(EvidenceTodoLink).filter(EvidenceTodoLink.evidence_id.in_(evidence)).delete(synchronize_session=False)
            s.delete(s.get(WeeklyReport, report_id))
            s.commit()

    # ---- 本周说明、本周材料
    def set_note(self, report_id: int, note: str) -> WeeklyReport:
        note = note.strip()
        if len(note) > MAX_NOTE:
            raise ValueError(f"本周说明请控制在 {MAX_NOTE} 字以内")
        return self._set_input(report_id, "note", note, {"note": note})

    def set_materials(self, report_id: int, material_ids: list[int]) -> WeeklyReport:
        ids = list(dict.fromkeys(material_ids))
        for material_id in ids:
            self.store.get(material_id)  # 不存在时 404
        return self._set_input(report_id, "material_ids", ids, {"material_ids": ids})

    def _set_input(self, report_id: int, field_name: str, value, after: dict) -> WeeklyReport:
        before = getattr(self.report(report_id), field_name)
        if before == value:
            return self.report(report_id)
        with self._session() as s:
            s.execute(update(WeeklyReport).where(WeeklyReport.id == report_id).values({field_name: value}))
            s.add(EvidenceReview(report_id=report_id, action=field_name, before={field_name: before}, after=after))
            reopen(s, report_id)
            s.commit()
        return self.report(report_id)

    # ---- 整理审核清单
    def prepare(self, day: date | None = None, *, scan: bool = True) -> tuple[WeeklyReport, PrepareInfo]:
        """检查更新 + 把这一周的文件变化、待办进展整理成候选记录。重复执行是幂等的：没有新变化时不新增，已审核的不覆盖。"""
        report = self.week(day)
        with PrepareLease(self.engine, report.id) as lease:
            return self._prepare(report, scan, lease)

    def _prepare(self, report, scan, lease):
        from ..todos.evidence import TodoEvidence
        from ..tracking.models import PRE_REPORT

        notes: list[str] = []
        scan_result = None
        if scan:
            scan_result = self.tracking.scanner.scan(PRE_REPORT)
            if scan_result.status != "done":
                notes.append("检查更新失败，清单只基于之前已采集的版本")
            if scan_result.stats.get("gap"):
                notes.append(f"{scan_result.stats['gap']['since']} 之后有每日检查漏跑，这期间的变化被合并成一次")
            if scan_result.stats.get("unreachable"):
                notes.append(f"{scan_result.stats['unreachable']} 个文件暂时无法访问，未能采集最新版本")
            if scan_result.stats.get("unstable"):
                notes.append(f"{len(scan_result.stats['unstable'])} 个文件可能正在写入，本次未采集")
        builder = EvidenceBuilder(self.tracking, self._model_factory, self.settings().owner_names)
        builder._session = lease.session  # 每个业务事务先做身份核对，失效旧工作进程不能写入
        build = builder.build(report)
        todo_builder = TodoEvidence(self.engine, self.store)
        todo_builder._session = lease.session
        todos = todo_builder.build(report)
        with lease.session() as s:
            values: dict = {"prepared_at": datetime.now(), "prep_notes": notes}
            if scan_result:
                values["final_scan_id"] = scan_result.run_id
            s.execute(update(WeeklyReport).where(WeeklyReport.id == report.id).values(**values))
            new = build.created + todos.created
            if new:
                current = s.get(WeeklyReport, report.id)
                if current.checklist == CONFIRMED_LIST:
                    notes.append("清单确认后又有新的变化，已回到待确认")
                s.add(EvidenceReview(report_id=report.id, action="prepare", after={"new": new}, note=f"整理出 {len(new)} 条新的候选记录"))
                reopen(s, report.id)
            s.commit()
        return self.report(report.id), PrepareInfo(scan_result, build, todos, notes)

    # ---- 生成
    def start_generation(self, report_id: int, audience: str | None = None, name: str | None = None) -> WeeklyReport:
        """确认清单并登记生成任务（真正的生成由 run_generation 执行，接口里放在后台）。

        还有待确认的记录时拒绝；读者、署名不填时用周报设置里的默认值。
        """
        from ..todos.evidence import TodoEvidence

        report = self.report(report_id)
        if report.status in (PENDING_GEN, RUNNING):
            raise ValueError("这份周报正在生成，请稍等")
        review = self.review()
        items = review.items(report_id)
        pending = [e for e in items if e.review_status == PENDING]
        if pending:
            raise ValueError(f"还有 {len(pending)} 条记录待确认（{', '.join(f'E{e.id}' for e in pending[:10])}），请先确认或排除")
        confirmed = [e.id for e in items if e.review_status == CONFIRMED]
        if not confirmed and not report.material_ids and not report.note:
            raise ValueError("没有已确认的工作记录，也没有本周材料或本周说明，无法生成周报")
        settings = self.settings()
        bridge = TodoEvidence(self.engine, self.store)
        inputs = {
            "audience": (audience or "").strip() or settings.audience or None,
            "name": (name or "").strip() or settings.author or None,
            "evidence_ids": confirmed,
        }
        token = uuid.uuid4().hex
        with self._session() as s:
            claimed = s.execute(
                update(WeeklyReport)
                .where(WeeklyReport.id == report_id, WeeklyReport.status.not_in([PENDING_GEN, RUNNING]))
                .values(checklist=CONFIRMED_LIST, confirmed_at=datetime.now(), inputs=inputs, status=PENDING_GEN, error="")
            )
            if claimed.rowcount != 1:
                raise ValueError("这份周报正在生成，请稍等")
            bridge.freeze(report_id, session=s)
            inputs["todo_refs"] = bridge.report_refs(confirmed, session=s)
            s.execute(update(WeeklyReport).where(WeeklyReport.id == report_id).values(inputs=inputs))
            lease = s.get(WeeklyGenerationLease, report_id)
            if lease is None:
                s.add(WeeklyGenerationLease(report_id=report_id, token=token, heartbeat_at=generation_now()))
            else:
                lease.token, lease.heartbeat_at = token, generation_now()
            s.add(EvidenceReview(report_id=report_id, action="generate", after={"evidence_ids": confirmed}, note=f"确认清单并生成：{len(confirmed)} 条工作记录"))
            s.commit()
        result = self.report(report_id)
        result.generation_token = token
        return result

    def run_generation(self, report_id: int, token: str | None = None) -> None:
        """只有认领本次 pending 的任务可运行；续期或结果写回均核对同一身份。"""
        report = self.report(report_id)
        with self._session() as s:
            lease = s.get(WeeklyGenerationLease, report_id)
            token = token or (lease.token if lease else None)
            if not token:
                return
            claimed = s.execute(update(WeeklyReport).where(
                WeeklyReport.id == report_id, WeeklyReport.status == PENDING_GEN,
                self._owns_generation(report_id, token),
            ).values(status=RUNNING, error=""))
            s.commit()
            if claimed.rowcount != 1:
                return
        self._renew_generation(report_id, token)
        stop = threading.Event()
        heartbeat = threading.Thread(target=self._heartbeat_generation, args=(report_id, token, stop), daemon=True)
        heartbeat.start()
        try:
            content, items, generation = ReportWriter(self.store, self._model_factory).write(report)
            with self._session() as s:
                saved = s.execute(update(WeeklyReport).where(
                    WeeklyReport.id == report_id, WeeklyReport.status == RUNNING,
                    self._owns_generation(report_id, token),
                ).values(status=DONE, content=content, items=items, generation=generation, generated_at=datetime.now()))
                if saved.rowcount == 1 and report.content:
                    s.add(WeeklyReportVersion(report_id=report_id, content=report.content, reason="regenerate"))
                s.commit()
        except Exception as e:
            with self._session() as s:
                s.execute(update(WeeklyReport).where(
                    WeeklyReport.id == report_id, WeeklyReport.status == RUNNING,
                    self._owns_generation(report_id, token),
                ).values(status=FAILED, error=f"{type(e).__name__}: {e}"[:1000]))
                s.commit()
        finally:
            stop.set()
            heartbeat.join()

    def generate(self, report_id: int, audience: str | None = None, name: str | None = None) -> WeeklyReport:
        """同步生成（命令行、测试用）。"""
        report = self.start_generation(report_id, audience, name)
        self.run_generation(report_id, report.generation_token)
        return self.report(report_id)

    # ---- 正文
    def update_content(self, report_id: int, content: str) -> WeeklyReport:
        report = self.report(report_id)
        if report.status != DONE:
            raise ValueError("周报还没有生成完成，不能修改")
        if not content.strip():
            raise ValueError("周报内容不能为空")
        if content.strip() == report.content:
            return report
        with self._session() as s:
            changed = s.execute(update(WeeklyReport).where(WeeklyReport.id == report_id, WeeklyReport.status == DONE, WeeklyReport.content == report.content).values(content=content.strip()))
            if changed.rowcount != 1:
                raise ValueError("周报已变化或正在生成，请刷新后重试")
            s.add(WeeklyReportVersion(report_id=report_id, content=report.content, reason="edit"))
            s.commit()
        return self.report(report_id)

    def versions(self, report_id: int) -> list[WeeklyReportVersion]:
        self.report(report_id)
        with self._session() as s:
            return list(s.scalars(select(WeeklyReportVersion).where(WeeklyReportVersion.report_id == report_id).order_by(WeeklyReportVersion.id.desc())))

    def restore_version(self, report_id: int, version_id: int) -> WeeklyReport:
        """恢复某个历史版本：当前正文也先存为版本，不会丢。"""
        report = self.report(report_id)
        if report.status in (PENDING_GEN, RUNNING):
            raise ValueError("这份周报正在生成，不能恢复版本")
        with self._session() as s:
            version = s.get(WeeklyReportVersion, version_id)
            if version is None or version.report_id != report_id:
                raise ReportNotFound(f"版本 {version_id} 不存在")
            changed = s.execute(update(WeeklyReport).where(WeeklyReport.id == report_id, WeeklyReport.status.not_in([PENDING_GEN, RUNNING]), WeeklyReport.content == report.content).values(content=version.content, status=DONE))
            if changed.rowcount != 1:
                raise ValueError("周报已变化或正在生成，请刷新后重试")
            if report.content:
                s.add(WeeklyReportVersion(report_id=report_id, content=report.content, reason="restore"))
            s.commit()
        return self.report(report_id)

    def export_markdown(self, report_id: int) -> tuple[str, str]:
        """返回 (文件名, 去掉出处标记的正文)。"""
        report = self.report(report_id)
        if report.status != DONE:
            raise ValueError("周报还没有生成完成，不能导出")
        return f"周报_{report.period_start}_{report.period_end}.md", strip_source_tags(report.content) + "\n"

    def _update(self, report_id: int, **values) -> None:
        with self._session() as s:
            s.execute(update(WeeklyReport).where(WeeklyReport.id == report_id).values(**values))
            s.commit()

