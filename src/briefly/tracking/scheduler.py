"""每日扫描：到期判断、漏跑补扫、失败重试。由周报的后台线程（weekly.schedule）每分钟调用一次。

状态都在数据库里（scan_runs），进程重启后自然恢复；多个进程同时运行时由扫描锁（scan_locks）保证不会同时扫，
已成功的日期不会重扫。

漏跑：错过的日期不会逐日补（那几天的文件状态已经无法恢复），只补扫一次，并在扫描记录里写明漏掉的日期，
之后生成的候选记录会标注“扫描中断过，部分变化被合并”。
"""
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from .. import config
from .models import ACTIVE, DAILY, DONE, FAILED, INTERRUPTED, RUNNING, SCAN_LOCK, ScanLock, ScanRun, TrackingSource
from .scanner import ScanBusy


from ..clock import tzinfo  # noqa: E402  配置的时区；留空用系统时区（定义在 clock，请求时间与定时任务共用）


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


def run_due_daily(svc, now: datetime | None = None) -> list[str]:
    """到期的每日扫描。返回给人看的执行记录。"""
    now_local = (now or datetime.now(timezone.utc)).astimezone(tzinfo())
    target = latest_due(now_local)
    if target is None:
        return ["没有到期的每日扫描"]
    session = sessionmaker(svc.engine, expire_on_commit=False)
    with session() as s:
        has_source = s.scalar(select(TrackingSource.id).where(TrackingSource.status == ACTIVE).limit(1))
        runs = list(s.scalars(select(ScanRun).where(ScanRun.trigger == DAILY, ScanRun.scheduled_for >= target).order_by(ScanRun.id)))
    if not has_source:
        return ["没有追踪中的文件"]
    if any(r.status == DONE for r in runs):
        return [f"{target} 的每日扫描已完成"]
    if any(r.status == RUNNING for r in runs) and _locked(session):
        return ["正在扫描"]  # 别的进程正在扫；锁已过期的是崩溃残留，下面加锁时会被标为 interrupted
    failures = [r for r in runs if r.status in (FAILED, INTERRUPTED)]
    if len(failures) > config.TRACKING_SCAN_RETRIES:
        if len(failures) == config.TRACKING_SCAN_RETRIES + 1:
            return [f"{target} 的每日扫描已失败 {len(failures)} 次，不再自动重试：{failures[-1].error[:100]}"]
        return []
    if failures:
        last = failures[-1].finished_at or failures[-1].started_at
        if datetime.now() - last < timedelta(minutes=config.TRACKING_RETRY_MINUTES):
            return []  # 等重试间隔
    try:
        result = svc.scanner.scan(DAILY, scheduled_for=target, attempt=len(failures) + 1)
    except ScanBusy:
        return ["正在扫描，稍后再试"]
    gap = result.stats.get("gap")
    note = f"（补扫：漏掉了 {', '.join(gap['missed_days'])}，这期间的变化合并为一次）" if gap else ""
    retry = f"（第 {len(failures) + 1} 次尝试）" if failures else ""
    return [f"{target} 每日扫描{'完成' if result.status == DONE else '失败'}{retry}：新版本 {result.stats.get('new_snapshots', 0)}{note}"]


def _locked(session) -> bool:
    with session() as s:
        lock = s.get(ScanLock, SCAN_LOCK)
    return lock is not None and lock.expires_at > datetime.now()
