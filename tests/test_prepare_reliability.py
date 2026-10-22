"""F6/F15：跨进程整理互斥、失效写入保护与配置时区边界。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from threading import Event
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from briefly import clock, config
from briefly.db import create_db_engine
from briefly.materials import MaterialStore
from briefly.todos.service import TodoService
from briefly.tracking.blobs import BlobStore
from briefly.tracking.models import ScanLock
from briefly.tracking.service import TrackingService
from briefly.weekly.evidence import BuildResult, EvidenceBuilder, period_bounds
from briefly.weekly.models import WeeklyReport, WorkEvidence
from briefly.weekly.prepare_lock import PrepareBusy, PrepareLease
from briefly.weekly.service import WeeklyService


@pytest.fixture
def env(tmp_path):
    url = f"sqlite:///{(tmp_path / 'prepare.db').as_posix()}"
    engines = [create_db_engine(url), create_db_engine(url)]
    services = []
    for engine in engines:
        store = MaterialStore(engine)
        tracking = TrackingService(engine, store, BlobStore(tmp_path / "blobs"))
        services.append(WeeklyService(store, lambda: None, tracking=tracking))
    services[0].week()
    services[0].settings()
    yield services
    for engine in engines:
        engine.dispose()


def test_concurrent_prepare_does_not_duplicate_candidates(env, monkeypatch):
    w, other = env
    TodoService(w.engine, w.store).create("今天的任务")
    entered, release = Event(), Event()
    original = EvidenceBuilder.build
    def blocked(builder, report):
        entered.set()
        assert release.wait(10)
        return original(builder, report)
    monkeypatch.setattr(EvidenceBuilder, "build", blocked)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(w.prepare, scan=False)
        assert entered.wait(10)
        try:
            with pytest.raises(PrepareBusy, match="正在整理"):
                other.prepare(scan=False)
        finally:
            release.set()
        report, _ = future.result(10)
    with Session(w.engine) as s:
        assert len(list(s.scalars(select(WorkEvidence).where(WorkEvidence.report_id == report.id)))) == 1
    other.prepare(scan=False)
    with Session(w.engine) as s:
        assert len(list(s.scalars(select(WorkEvidence)))) == 1
        assert not list(s.scalars(select(ScanLock).where(ScanLock.name.like("prepare:%"))))


def test_expired_lease_cannot_write_or_remove_new_owners_lock(env):
    w, other = env
    rid = w.week().id
    old = PrepareLease(w.engine, rid)
    old.__enter__()
    try:
        with Session(w.engine) as s:
            s.execute(update(ScanLock).where(ScanLock.name == old.name).values(expires_at=datetime.now() - timedelta(seconds=1)))
            s.commit()
        with PrepareLease(other.engine, rid) as new:
            with pytest.raises(PrepareBusy, match="失效"):
                with old.session() as s:
                    s.add(WorkEvidence(report_id=rid, title="旧任务不应写入"))
                    s.commit()
            old.__exit__(None, None, None)
            with new.session() as s:
                assert s.get(ScanLock, new.name).owner == new.owner
    finally:
        old.__exit__(None, None, None)


def test_prepare_lock_is_cross_process(env):
    import subprocess
    import sys
    w, _ = env
    rid = w.week().id
    code = """
import sys
from briefly.db import create_db_engine
from briefly.weekly.prepare_lock import PrepareLease, PrepareBusy
engine = create_db_engine(sys.argv[1])
try:
    try:
        with PrepareLease(engine, int(sys.argv[2])):
            raise AssertionError('concurrent lease acquired')
    except PrepareBusy:
        pass
finally:
    engine.dispose()
"""
    with PrepareLease(w.engine, rid):
        result = subprocess.run([sys.executable, "-c", code, str(w.engine.url), str(rid)], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr


def test_prepare_heartbeat_renews_without_business_progress(env, monkeypatch):
    from contextlib import contextmanager
    monkeypatch.setattr(config, "TRACKING_LOCK_SECONDS", 6)
    w, _ = env
    lease = PrepareLease(w.engine, w.week().id)
    renewed = Event()
    original = lease.session
    @contextmanager
    def observe():
        with original() as s:
            yield s
        renewed.set()
    lease.session = observe
    with lease:
        with Session(w.engine) as s:
            before = s.get(ScanLock, lease.name).expires_at
        assert renewed.wait(5)
        with Session(w.engine) as s:
            assert s.get(ScanLock, lease.name).expires_at > before


def test_week_boundaries_convert_configured_midnight_to_storage_timezone(monkeypatch):
    monkeypatch.setattr(config, "TRACKING_TIMEZONE", "Australia/Sydney")
    monkeypatch.setattr(clock, "system_naive", lambda moment: moment.astimezone(timezone.utc).replace(tzinfo=None))
    report = WeeklyReport(period_start=date(2026, 10, 19), period_end=date(2026, 10, 25))
    start, end = period_bounds(report)
    assert start == datetime(2026, 10, 18, 13)
    assert end == datetime(2026, 10, 25, 12, 59, 59, 999999)
    assert start <= datetime(2026, 10, 18, 13, 1) <= end
    assert datetime(2026, 10, 18, 12, 59) < start


def test_dst_week_uses_each_boundarys_actual_offset(monkeypatch):
    monkeypatch.setattr(config, "TRACKING_TIMEZONE", "Australia/Sydney")
    monkeypatch.setattr(clock, "system_naive", lambda moment: moment.astimezone(timezone.utc).replace(tzinfo=None))
    start, end = period_bounds(WeeklyReport(period_start=date(2026, 9, 28), period_end=date(2026, 10, 4)))
    assert start == datetime(2026, 9, 27, 14)
    assert end == datetime(2026, 10, 4, 12, 59, 59, 999999)
    assert end - start + timedelta(microseconds=1) == timedelta(hours=167)


def test_unconfigured_timezone_keeps_storage_wall_clock(monkeypatch):
    monkeypatch.setattr(config, "TRACKING_TIMEZONE", "")
    start, end = period_bounds(WeeklyReport(period_start=date(2026, 1, 5), period_end=date(2026, 1, 11)))
    assert start == datetime(2026, 1, 5)
    assert end == datetime(2026, 1, 11, 23, 59, 59, 999999)


def test_todo_activity_query_uses_converted_boundaries(env, monkeypatch):
    from briefly.todos.evidence import TodoEvidence
    from briefly.todos.models import TodoActivity
    w, _ = env
    monkeypatch.setattr(config, "TRACKING_TIMEZONE", "Australia/Sydney")
    monkeypatch.setattr(clock, "system_naive", lambda moment: moment.astimezone(timezone.utc).replace(tzinfo=None))
    report = w.week(date(2026, 10, 19))
    start, end = period_bounds(report)
    todos = TodoService(w.engine, w.store)
    for title, when in [("前一周", start - timedelta(microseconds=1)), ("周一零点", start), ("周日末尾", end), ("下一周", end + timedelta(microseconds=1))]:
        todo = todos.create(title)
        with Session(w.engine) as s:
            s.execute(update(TodoActivity).where(TodoActivity.todo_id == todo.id).values(created_at=when))
            s.commit()
    result = TodoEvidence(w.engine, w.store).build(report)
    with Session(w.engine) as s:
        titles = set(s.scalars(select(WorkEvidence.title).where(WorkEvidence.id.in_(result.created))))
    assert titles == {"周一零点", "周日末尾"}
