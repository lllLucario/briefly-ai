"""定时任务：每日扫描（到期判断、漏跑补扫、失败重试），以及每周定时准备周报（weekly_schedule.py）。

部署方式（与现有 Agent 一致：单用户、本地常驻）：
- briefly-api 进程内一个后台线程，每分钟调用一次 run_due；
- 没有常驻 API 时，用系统计划任务定时执行 `briefly track run-due`。
不用 APScheduler、Celery、Redis：要做的只是“今天该扫的扫过没有”，状态都在数据库里（scan_runs），
进程重启后自然恢复；多个进程同时运行时由扫描锁（scan_locks）保证同一集合不会同时扫，已成功的日期不会重扫。

漏跑：错过的日期不会逐日补（那几天的文件状态已经无法恢复），只补扫一次，并在扫描记录里写明漏掉的日期，
之后生成的候选记录会标注“扫描中断过，部分变化被合并”。
"""
import logging
import threading
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from langchain_core.language_models import BaseChatModel

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from .. import config
from .models import ACTIVE, DAILY, DONE, FAILED, INTERRUPTED, RUNNING, ScanLock, ScanRun, TrackingSource
from .scanner import ScanBusy

log = logging.getLogger(__name__)


def tzinfo():
    """配置的时区；留空用系统时区。"""
    if config.TRACKING_TIMEZONE:
        return ZoneInfo(config.TRACKING_TIMEZONE)
    return datetime.now().astimezone().tzinfo


def daily_time() -> tuple[int, int]:
    try:
        hour, minute = (int(x) for x in config.TRACKING_DAILY_TIME.split(":"))
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError
    except ValueError:
        raise ValueError(f"BRIEFLY_TRACKING_DAILY_TIME 格式应为 HH:MM，当前为 {config.TRACKING_DAILY_TIME!r}") from None
    return hour, minute


def is_scan_day(d: date) -> bool:
    return not config.TRACKING_WORKDAYS_ONLY or d.weekday() < 5


def latest_due(now_local: datetime) -> date | None:
    """不晚于 now 的最近一个应扫描日期（今天已过设定时间则为今天，否则往前找）。"""
    hour, minute = daily_time()
    d = now_local.date()
    if (now_local.hour, now_local.minute) < (hour, minute):
        d -= timedelta(days=1)
    for _ in range(8):
        if is_scan_day(d):
            return d
        d -= timedelta(days=1)
    return None


def run_due(svc, now: datetime | None = None, model_factory: Callable[[], BaseChatModel] | None = None) -> list[str]:
    """执行到期的每日扫描和每周定时准备。返回给人看的执行记录。"""
    from ..llm import create_chat_model
    from .weekly_schedule import ScheduleService

    messages = run_due_daily(svc, now)
    messages += ScheduleService(svc, model_factory or create_chat_model).run_due(now)
    return messages


def run_due_daily(svc, now: datetime | None = None) -> list[str]:
    """检查所有集合，执行到期的每日扫描。"""
    now_local = (now or datetime.now(timezone.utc)).astimezone(tzinfo())
    target = latest_due(now_local)
    if target is None:
        return ["没有到期的每日扫描"]
    session = sessionmaker(svc.engine, expire_on_commit=False)
    messages = []
    for c in svc.list_collections():
        with session() as s:
            has_source = s.scalar(
                select(TrackingSource.id).where(TrackingSource.collection_id == c.id, TrackingSource.status == ACTIVE).limit(1)
            )
            runs = list(
                s.scalars(
                    select(ScanRun)
                    .where(ScanRun.collection_id == c.id, ScanRun.trigger == DAILY, ScanRun.scheduled_for >= target)
                    .order_by(ScanRun.id)
                )
            )
        if not has_source:
            continue
        if any(r.status == DONE for r in runs):
            continue
        if any(r.status == RUNNING for r in runs) and _locked(session, c.id):
            continue  # 别的进程正在扫；锁已过期的是崩溃残留，下面加锁时会被标为 interrupted
        failures = [r for r in runs if r.status in (FAILED, INTERRUPTED)]
        if len(failures) > config.TRACKING_SCAN_RETRIES:
            if len(failures) == config.TRACKING_SCAN_RETRIES + 1:
                messages.append(f"「{c.name}」{target} 的每日扫描已失败 {len(failures)} 次，不再自动重试：{failures[-1].error[:100]}")
            continue
        if failures:
            last = failures[-1].finished_at or failures[-1].started_at
            if datetime.now() - last < timedelta(minutes=config.TRACKING_RETRY_MINUTES):
                continue  # 等重试间隔
        try:
            result = svc.scanner.scan(c.id, DAILY, scheduled_for=target, attempt=len(failures) + 1)
        except ScanBusy:
            messages.append(f"「{c.name}」正在扫描，稍后再试")
            continue
        gap = result.stats.get("gap")
        note = f"（补扫：漏掉了 {', '.join(gap['missed_days'])}，这期间的变化合并为一次）" if gap else ""
        retry = f"（第 {len(failures) + 1} 次尝试）" if failures else ""
        messages.append(
            f"「{c.name}」{target} 每日扫描{'完成' if result.status == DONE else '失败'}{retry}：新版本 {result.stats.get('new_snapshots', 0)}{note}"
        )
    return messages or [f"{target} 的每日扫描都已完成"]


def _locked(session, collection_id: int) -> bool:
    with session() as s:
        lock = s.get(ScanLock, collection_id)
    return lock is not None and lock.expires_at > datetime.now()


class DailyScheduler:
    """API 进程内的后台线程：每隔 interval 秒检查一次到期的每日扫描和每周定时准备。"""

    def __init__(self, svc, interval: float = 60, model_factory: Callable[[], BaseChatModel] | None = None) -> None:
        self.svc = svc
        self.interval = interval
        self.model_factory = model_factory
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        daily_time()  # 配置有误时启动即报错
        self.svc.blobs.clean_temp()  # 清理上次中断写入留下的临时文件
        self._thread = threading.Thread(target=self._loop, name="briefly-daily-scan", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                for line in run_due(self.svc, model_factory=self.model_factory):
                    ran = any(w in line for w in ("每日扫描完成", "每日扫描失败", "每周定时准备"))
                    log.log(logging.INFO if ran else logging.DEBUG, "每日扫描：%s", line)
            except Exception:
                log.exception("每日扫描检查失败")
            self._stop.wait(self.interval)

    def stop(self, timeout: float = 5) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()
