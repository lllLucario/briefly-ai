"""定时：每周设定的时间（精确到分钟）自动整理这一周的工作——检查更新、整理审核清单，然后提醒你审核。

和手动点“整理本周工作”走同一个流程（WeeklyService.prepare），只是到点自动整理；不会在你审核之前生成正文。
后台线程每分钟检查一次，同时负责追踪文件的每日扫描（tracking.scheduler）：
- 同一个时间点只执行一次（scheduled_runs 有唯一约束，多进程同时检查也不会重复）；
- 失败按 TRACKING_SCAN_RETRIES 重试，间隔 TRACKING_RETRY_MINUTES，全部失败后提醒你手动开始；
- 服务没运行、错过了时间点：启动后补做最近一次（只补一次），记下晚了多少分钟；
- 刚设置时，已经过去的时间点不执行（等下一次）。
没有常驻 briefly-api 时，可以用系统计划任务定时执行 `briefly weekly run-due`。
"""
import logging
import re
import threading
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from .. import config
from ..clock import system_naive, tz_label
from ..tracking.models import ScanLock
from ..tracking.scheduler import daily_time, run_due_daily, tzinfo
from .models import (
    PENDING,
    RUN_DONE,
    RUN_FAILED,
    RUN_INTERRUPTED,
    RUN_RUNNING,
    Notification,
    ScheduledRun,
    WeeklySettings,
    WorkEvidence,
)

log = logging.getLogger(__name__)

WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
_WEEKDAY_ALIASES = {
    **{name: i for i, name in enumerate(WEEKDAY_NAMES)},
    **{name.replace("周", "星期"): i for i, name in enumerate(WEEKDAY_NAMES)},
    "周天": 6, "星期天": 6,
    **{name: i for i, name in enumerate(["mon", "tue", "wed", "thu", "fri", "sat", "sun"])},
}  # fmt: skip
LATE_GRACE_MINUTES = 5  # 晚于这个分钟数才算“补做”

# 提醒渠道：登记发送函数 send(notification)，notify 会逐个调用（失败只记日志）。飞书机器人见 feishu/messages.py
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


def describe(settings: WeeklySettings) -> str:
    if not settings.schedule_enabled:
        return "未开启定时"
    return f"每{WEEKDAY_NAMES[settings.weekday]} {settings.time}"


def latest_occurrence(settings: WeeklySettings, now_local: datetime) -> datetime:
    """不晚于 now 的最近一个设定时间点（带时区）。"""
    hour, minute = (int(x) for x in settings.time.split(":"))
    days_back = (now_local.weekday() - settings.weekday) % 7
    occ = (now_local - timedelta(days=days_back)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if occ > now_local:
        occ -= timedelta(days=7)
    return occ


def next_time(settings: WeeklySettings, now: datetime | None = None) -> datetime | None:
    if not settings.schedule_enabled:
        return None
    now_local = (now or datetime.now(timezone.utc)).astimezone(tzinfo())
    return latest_occurrence(settings, now_local) + timedelta(days=7)


def _local_naive(moment: datetime) -> datetime:
    """转成系统本地时间、去掉时区：与数据库里其他时间戳（datetime.now()）一致。"""
    return system_naive(moment)


# ---- 提醒
def notify(engine, kind: str, title: str, body: str = "", report_id: int | None = None) -> Notification:
    n = Notification(kind=kind, title=title, body=body, report_id=report_id)
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


class Schedule:
    """每周定时：设置、执行记录、到点执行。weekly 是 WeeklyService。"""

    def __init__(self, weekly) -> None:
        self.weekly = weekly
        self._session = sessionmaker(weekly.engine, expire_on_commit=False)

    def set(self, weekday: int, time: str) -> WeeklySettings:
        if not 0 <= weekday <= 6:
            raise ValueError("星期应为 0–6")
        time = parse_time(time)
        self.weekly.settings()
        with self._session() as s:
            row = s.get(WeeklySettings, 1)
            row.weekday, row.time, row.schedule_enabled = weekday, time, True
            row.schedule_updated_at = datetime.now()  # 改设置后，已经过去的时间点不再执行
            s.commit()
            return row

    def disable(self) -> WeeklySettings:
        self.weekly.settings()
        with self._session() as s:
            row = s.get(WeeklySettings, 1)
            row.schedule_enabled = False
            s.commit()
            return row

    def runs(self, limit: int = 10) -> list[ScheduledRun]:
        with self._session() as s:
            return list(s.scalars(select(ScheduledRun).order_by(ScheduledRun.id.desc()).limit(limit)))

    def run_due(self, now: datetime | None = None) -> str | None:
        settings = self.weekly.settings()
        if not settings.schedule_enabled:
            return None
        now_local = (now or datetime.now(timezone.utc)).astimezone(tzinfo())
        occ_local = latest_occurrence(settings, now_local)
        occ = _local_naive(occ_local)
        if occ < settings.schedule_updated_at.replace(microsecond=0) - timedelta(seconds=59):
            return None  # 设置之前的时间点：不补做
        report = self.weekly.week(occ_local.date())
        with self._session() as s:
            now_db = datetime.now()
            active = select(ScanLock.name).where(ScanLock.name == f"prepare:{report.id}", ScanLock.expires_at >= now_db).exists()
            s.execute(update(ScheduledRun).where(
                ScheduledRun.occurrence == occ, ScheduledRun.status == RUN_RUNNING,
                ScheduledRun.started_at < now_db - timedelta(seconds=config.TRACKING_LOCK_SECONDS), ~active,
            ).values(status=RUN_INTERRUPTED, error="执行进程中断", finished_at=now_db))
            s.commit()
            runs = list(s.scalars(select(ScheduledRun).where(ScheduledRun.occurrence == occ).order_by(ScheduledRun.attempt)))
        if any(r.status in (RUN_DONE, RUN_RUNNING) for r in runs):
            return None
        failures = [r for r in runs if r.status in (RUN_FAILED, RUN_INTERRUPTED)]
        if len(failures) > config.TRACKING_SCAN_RETRIES:
            return None
        if failures and datetime.now() - (failures[-1].finished_at or failures[-1].started_at) < timedelta(minutes=config.TRACKING_RETRY_MINUTES):
            return None
        late = int((now_local - occ_local).total_seconds() // 60)
        run = ScheduledRun(report_id=report.id, occurrence=occ, attempt=len(failures) + 1, late_minutes=late if late > LATE_GRACE_MINUTES else 0)
        with self._session() as s:
            s.add(run)
            try:
                s.commit()
            except IntegrityError:
                return None  # 另一个进程刚开始执行同一个时间点
        log.info("每周自动整理：时区 %s，计划时间 %s，整理 %s 所在的一周", tz_label(occ_local), occ_local.isoformat(), occ_local.date())
        return self._execute(run, occ_local.date())

    def _execute(self, run: ScheduledRun, day: date) -> str:
        try:
            report, info = self.weekly.prepare(day)
        except Exception as e:  # 包括正在扫描（ScanBusy）：按失败记录，稍后重试
            log.exception("每周自动整理失败")
            error = f"{type(e).__name__}: {e}"[:1000]
            if not self._finish(run.id, RUN_FAILED, error=error):
                return "原整理任务已中断，本次不再覆盖执行记录"
            if run.attempt > config.TRACKING_SCAN_RETRIES:
                notify(
                    self.weekly.engine,
                    "weekly_failed",
                    "本周工作自动整理失败",
                    f"已重试 {run.attempt} 次：{error[:200]}\n可以在周报页点“整理本周工作”，或运行：briefly weekly prepare",
                )
            return f"每周自动整理失败（第 {run.attempt} 次）：{error[:100]}"
        if not self._finish(run.id, RUN_DONE, report_id=report.id):
            return "原整理任务已中断，本次不再覆盖执行记录"
        with self._session() as s:
            pending = len(s.scalars(select(WorkEvidence.id).where(WorkEvidence.report_id == report.id, WorkEvidence.review_status == PENDING)).all())
        late = f"（服务当时没有运行，晚了 {run.late_minutes} 分钟补做）" if run.late_minutes else ""
        period = f"{report.period_start}~{report.period_end}"
        if pending:
            title = f"本周周报的审核清单已整理好：{pending} 条待你确认"
            body = f"周期 {period}{late}。确认后生成周报（周报页，或 briefly weekly review / briefly weekly generate）。"
        else:
            title = "本周没有发现新的文件变化和待办进展"
            body = f"周期 {period}{late}。可以补充文件体现不了的工作、加入材料或写本周说明后生成周报。"
        body += "".join(f"\n注意：{n}" for n in info.notes)
        notify(self.weekly.engine, "weekly_ready", title, body, report.id)
        return f"每周自动整理完成：待确认 {pending} 条{late}"

    def _finish(self, run_id: int, status: str, report_id: int | None = None, error: str = "") -> bool:
        with self._session() as s:
            result = s.execute(
                update(ScheduledRun)
                .where(ScheduledRun.id == run_id, ScheduledRun.status == RUN_RUNNING)
                .values(status=status, report_id=report_id, error=error, finished_at=datetime.now())
            )
            s.commit()
            return result.rowcount == 1


def run_due(weekly, now: datetime | None = None) -> list[str]:
    """到期的每日扫描和每周自动整理。返回给人看的执行记录。"""
    weekly._fail_interrupted()
    messages = run_due_daily(weekly.tracking, now)
    message = Schedule(weekly).run_due(now)
    return messages + ([message] if message else [])


class BackgroundScheduler:
    """API 进程内的后台线程：每隔 interval 秒检查一次到期的每日扫描和每周自动整理。"""

    def __init__(self, weekly, interval: float = 60) -> None:
        self.weekly = weekly
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        daily_time()  # 配置有误时启动即报错
        self.weekly.tracking.blobs.clean_temp()  # 清理上次中断写入留下的临时文件
        self._thread = threading.Thread(target=self._loop, name="briefly-scheduler", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                for line in run_due(self.weekly):
                    ran = any(w in line for w in ("每日扫描完成", "每日扫描失败", "每周自动整理"))
                    log.log(logging.INFO if ran else logging.DEBUG, "定时：%s", line)
            except Exception:
                log.exception("定时检查失败")
            self._stop.wait(self.interval)

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
