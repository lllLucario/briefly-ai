"""每周定时准备周报：到设定时间（精确到分钟）自动执行“最终扫描 → 生成候选清单”，然后提醒用户审核。

只做到“准备好候选清单”为止：哪些是自己的工作必须由用户确认，确认后再生成周报（review / report）。
由每日扫描的同一个后台线程（scheduler.run_due）每分钟检查：
- 同一个时间点只执行一次（scheduled_runs 有唯一约束，多进程同时检查也不会重复）；
- 失败按 TRACKING_SCAN_RETRIES 重试，间隔 TRACKING_RETRY_MINUTES，全部失败后提醒用户手动准备；
- 服务没运行、错过了时间点：启动后补做最近一次（只补一次，不补更早的），记下晚了多少分钟；
- 刚设置时，已经过去的时间点不执行（等下一次）。
"""
import logging
import re
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone

from langchain_core.language_models import BaseChatModel
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from .. import config
from ..llm import create_chat_model
from .models import DONE, FAILED, INTERRUPTED, PENDING, RUNNING, Notification, ScheduledRun, WeeklySchedule, WorkEvidence

log = logging.getLogger(__name__)

WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
_WEEKDAY_ALIASES = {
    **{name: i for i, name in enumerate(WEEKDAY_NAMES)},
    **{name.replace("周", "星期"): i for i, name in enumerate(WEEKDAY_NAMES)},
    "周天": 6, "星期天": 6,
    **{name: i for i, name in enumerate(["mon", "tue", "wed", "thu", "fri", "sat", "sun"])},
}  # fmt: skip
LATE_GRACE_MINUTES = 5  # 晚于这个分钟数才算“补做”

# 以后接入飞书等渠道：在这里登记发送函数 send(notification)，notify 会逐个调用（失败只记日志）
CHANNELS: list[Callable[[Notification], None]] = []


def parse_weekday(text: str) -> int:
    """周一到周日：接受 1–7（周一为 1）、“周五”“星期五”、fri 等。返回 0–6。"""
    value = str(text).strip().lower()
    if value.isdigit() and 1 <= int(value) <= 7:
        return int(value) - 1
    if value in _WEEKDAY_ALIASES:
        return _WEEKDAY_ALIASES[value]
    raise ValueError(f"无法识别的星期：{text}（可写 1–7、周五、星期五）")


def parse_time(text: str) -> str:
    m = re.fullmatch(r"(\d{1,2})[:：](\d{2})", str(text).strip())
    if not m or not (0 <= int(m.group(1)) < 24 and 0 <= int(m.group(2)) < 60):
        raise ValueError(f"时间格式应为 HH:MM，例如 16:30（当前为 {text}）")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def describe(schedule: WeeklySchedule) -> str:
    state = "" if schedule.enabled else "（已关闭）"
    return f"每{WEEKDAY_NAMES[schedule.weekday]} {schedule.time}{state}"


def latest_occurrence(schedule: WeeklySchedule, now_local: datetime) -> datetime:
    """不晚于 now 的最近一个设定时间点（带时区）。"""
    hour, minute = (int(x) for x in schedule.time.split(":"))
    days_back = (now_local.weekday() - schedule.weekday) % 7
    occ = (now_local - timedelta(days=days_back)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if occ > now_local:
        occ -= timedelta(days=7)
    return occ


def week_of(day: date) -> tuple[date, date]:
    start = day - timedelta(days=day.weekday())
    return start, start + timedelta(days=6)


def _local_naive(moment: datetime) -> datetime:
    """转成系统本地时间、去掉时区：与数据库里其他时间戳（datetime.now()）一致。"""
    return moment.astimezone().replace(tzinfo=None)


# ---- 提醒
def notify(engine, kind: str, title: str, body: str = "", collection_id: int | None = None, prep_id: int | None = None) -> Notification:
    n = Notification(kind=kind, title=title, body=body, collection_id=collection_id, prep_id=prep_id)
    with sessionmaker(engine, expire_on_commit=False)() as s:
        s.add(n)
        s.commit()
    for send in CHANNELS:
        try:
            send(n)
        except Exception:
            log.exception("提醒发送失败")
    return n


def notifications(engine, unread_only: bool = True, limit: int = 50) -> list[Notification]:
    query = select(Notification).order_by(Notification.id.desc()).limit(limit)
    if unread_only:
        query = query.where(Notification.read_at.is_(None))
    with sessionmaker(engine, expire_on_commit=False)() as s:
        return list(s.scalars(query))


def mark_read(engine, ids: list[int] | None = None) -> int:
    """标为已读；ids 为空表示全部。返回标记数。"""
    query = update(Notification).where(Notification.read_at.is_(None))
    if ids:
        query = query.where(Notification.id.in_(ids))
    with sessionmaker(engine)() as s:
        count = s.execute(query.values(read_at=datetime.now())).rowcount
        s.commit()
    return count


class ScheduleService:
    def __init__(self, svc, model_factory: Callable[[], BaseChatModel] = create_chat_model) -> None:
        self.svc = svc
        self._model_factory = model_factory
        self._session = sessionmaker(svc.engine, expire_on_commit=False)

    # ---- 设置
    def set(self, ref: str | int, weekday: int, time: str) -> WeeklySchedule:
        c = self.svc.collection(ref)
        if not 0 <= weekday <= 6:
            raise ValueError("星期应为 0–6")
        time = parse_time(time)
        with self._session() as s:
            schedule = s.scalar(select(WeeklySchedule).where(WeeklySchedule.collection_id == c.id))
            if schedule is None:
                schedule = WeeklySchedule(collection_id=c.id, weekday=weekday, time=time)
                s.add(schedule)
            else:
                schedule.weekday, schedule.time, schedule.enabled = weekday, time, True
                schedule.updated_at = datetime.now()  # 改设置后，已经过去的时间点不再执行
            s.commit()
            return schedule

    def disable(self, ref: str | int) -> WeeklySchedule:
        schedule = self.get(ref)
        if schedule is None:
            raise ValueError("这个集合还没有设置每周定时准备")
        with self._session() as s:
            s.execute(update(WeeklySchedule).where(WeeklySchedule.id == schedule.id).values(enabled=False))
            s.commit()
        return self.get(ref)

    def get(self, ref: str | int) -> WeeklySchedule | None:
        c = self.svc.collection(ref)
        with self._session() as s:
            return s.scalar(select(WeeklySchedule).where(WeeklySchedule.collection_id == c.id))

    def runs(self, ref: str | int, limit: int = 10) -> list[ScheduledRun]:
        c = self.svc.collection(ref)
        with self._session() as s:
            return list(
                s.scalars(select(ScheduledRun).where(ScheduledRun.collection_id == c.id).order_by(ScheduledRun.id.desc()).limit(limit))
            )

    def next_time(self, schedule: WeeklySchedule, now_local: datetime) -> datetime:
        return latest_occurrence(schedule, now_local) + timedelta(days=7)

    # ---- 执行
    def run_due(self, now: datetime | None = None) -> list[str]:
        from .scheduler import tzinfo

        now_local = (now or datetime.now(timezone.utc)).astimezone(tzinfo())
        with self._session() as s:
            schedules = list(s.scalars(select(WeeklySchedule).where(WeeklySchedule.enabled.is_(True))))
        messages = []
        for schedule in schedules:
            message = self._run_one(schedule, now_local)
            if message:
                messages.append(message)
        return messages

    def _run_one(self, schedule: WeeklySchedule, now_local: datetime) -> str | None:
        occ_local = latest_occurrence(schedule, now_local)
        occ = _local_naive(occ_local)
        if occ < schedule.updated_at.replace(microsecond=0) - timedelta(seconds=59):
            return None  # 设置之前的时间点：不补做
        with self._session() as s:
            runs = list(
                s.scalars(select(ScheduledRun).where(ScheduledRun.schedule_id == schedule.id, ScheduledRun.occurrence == occ).order_by(ScheduledRun.attempt))
            )
            stale = datetime.now() - timedelta(seconds=config.TRACKING_LOCK_SECONDS)
            for r in runs:
                if r.status == RUNNING and r.started_at < stale:  # 执行中的进程崩溃了
                    r.status, r.error, r.finished_at = INTERRUPTED, "执行进程中断", datetime.now()
            s.commit()
        if any(r.status == DONE for r in runs) or any(r.status == RUNNING for r in runs):
            return None
        failures = [r for r in runs if r.status in (FAILED, INTERRUPTED)]
        if len(failures) > config.TRACKING_SCAN_RETRIES:
            return None
        if failures and datetime.now() - (failures[-1].finished_at or failures[-1].started_at) < timedelta(minutes=config.TRACKING_RETRY_MINUTES):
            return None
        late = int((now_local - occ_local).total_seconds() // 60)
        run = ScheduledRun(
            schedule_id=schedule.id,
            collection_id=schedule.collection_id,
            occurrence=occ,
            attempt=len(failures) + 1,
            late_minutes=late if late > LATE_GRACE_MINUTES else 0,
        )
        with self._session() as s:
            s.add(run)
            try:
                s.commit()
            except IntegrityError:
                return None  # 另一个进程刚开始执行同一个时间点
        return self._execute(schedule, run, occ_local.date())

    def _execute(self, schedule: WeeklySchedule, run: ScheduledRun, day: date) -> str:
        from .pipeline import prepare

        collection = self.svc.collection(schedule.collection_id)
        start, end = week_of(day)
        try:
            prep, info = prepare(self.svc, collection.id, start, end, model_factory=self._model_factory)
        except Exception as e:  # 包括正在扫描（ScanBusy）：按失败记录，稍后重试
            log.exception("每周定时准备失败")
            error = f"{type(e).__name__}: {e}"[:1000]
            self._finish(run.id, FAILED, error=error)
            if run.attempt > config.TRACKING_SCAN_RETRIES:
                notify(
                    self.svc.engine,
                    "weekly_failed",
                    f"「{collection.name}」本周周报自动准备失败",
                    f"已重试 {run.attempt} 次：{error[:200]}\n可以手动运行：briefly track prepare {collection.name}",
                    collection.id,
                )
            return f"「{collection.name}」每周定时准备失败（第 {run.attempt} 次）：{error[:100]}"
        self._finish(run.id, DONE, prep_id=prep.id)
        with self._session() as s:
            pending = len(s.scalars(select(WorkEvidence.id).where(WorkEvidence.prep_id == prep.id, WorkEvidence.review_status == PENDING)).all())
        late = f"（服务当时没有运行，晚了 {run.late_minutes} 分钟补做）" if run.late_minutes else ""
        if pending:
            title = f"「{collection.name}」本周周报已准备好：{pending} 条工作记录待你确认"
            body = f"周期 {start}~{end}{late}。\n审核：briefly track review {collection.name}\n确认后生成周报：briefly track report {collection.name}"
        else:
            title = f"「{collection.name}」本周没有发现新的文件变化"
            body = f"周期 {start}~{end}{late}。如有文件体现不了的工作，可以运行 briefly track review {collection.name} 补充后生成周报。"
        body += "".join(f"\n注意：{n}" for n in info.notes)
        notify(self.svc.engine, "weekly_ready", title, body, collection.id, prep.id)
        return f"「{collection.name}」每周定时准备完成：待确认 {pending} 条{late}"

    def _finish(self, run_id: int, status: str, prep_id: int | None = None, error: str = "") -> None:
        with self._session() as s:
            s.execute(
                update(ScheduledRun)
                .where(ScheduledRun.id == run_id)
                .values(status=status, prep_id=prep_id, error=error, finished_at=datetime.now())
            )
            s.commit()
