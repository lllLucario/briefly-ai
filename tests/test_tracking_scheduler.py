"""文件追踪 Step 5：每日扫描（到期、补扫、重试、恢复、时区、互斥）与存储清理。不调用模型。"""
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import sessionmaker

from briefly import config
from briefly.api import create_app
from briefly.tracking import cli as track_cli
from briefly.tracking.models import DAILY, DONE, FAILED, INTERRUPTED, FileSnapshot, ScanLock, ScanRun, WorkEvidence
from briefly.tracking.scheduler import DailyScheduler, latest_due, run_due
from briefly.tracking.storage import prune
from tracking_util import env  # noqa: F401

SH = ZoneInfo("Asia/Shanghai")


@pytest.fixture(autouse=True)
def schedule(monkeypatch):
    monkeypatch.setattr(config, "TRACKING_DAILY_TIME", "18:00")
    monkeypatch.setattr(config, "TRACKING_TIMEZONE", "Asia/Shanghai")
    monkeypatch.setattr(config, "TRACKING_WORKDAYS_ONLY", True)
    monkeypatch.setattr(config, "TRACKING_SCAN_RETRIES", 2)
    monkeypatch.setattr(config, "TRACKING_RETRY_MINUTES", 10)


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=SH)  # 2026-10-12 是周一


def daily_runs(env, c):
    return [r for r in env.svc.scans(c.id) if r.trigger == DAILY]


def tracked(env):
    env.write("a.md", "# A")
    c = env.svc.create_collection("c")
    env.svc.add_paths(c.id, [str(env.work / "a.md")])
    return c


def set_runs(env, **values):
    with sessionmaker(env.svc.engine)() as s:
        s.execute(update(ScanRun).values(**values))
        s.commit()


def test_latest_due_respects_time_and_workdays():
    assert latest_due(at(14, 18, 0)) == date(2026, 10, 14)  # 周三 18:00 到点
    assert latest_due(at(14, 17, 59)) == date(2026, 10, 13)  # 还没到点：最近一次应扫的是周二
    assert latest_due(at(17, 10)) == date(2026, 10, 16)  # 周六：最近的工作日是周五
    assert latest_due(at(19, 9)) == date(2026, 10, 16)  # 周一早上：上周五


def test_timezone_is_applied(monkeypatch):
    utc = datetime(2026, 10, 14, 9, 30, tzinfo=timezone.utc)  # 上海 17:30，东京 18:30
    assert latest_due(utc.astimezone(SH)) == date(2026, 10, 13)
    monkeypatch.setattr(config, "TRACKING_TIMEZONE", "Asia/Tokyo")
    from briefly.tracking.scheduler import tzinfo

    assert latest_due(utc.astimezone(tzinfo())) == date(2026, 10, 14)


def test_daily_scan_runs_once_per_day(env):
    c = tracked(env)
    env.write("a.md", "# A 改过")
    first = run_due(env.svc, at(14, 18, 5))
    second = run_due(env.svc, at(14, 18, 6))
    assert "每日扫描完成" in first[0] and "新版本 1" in first[0]
    assert second == ["2026-10-14 的每日扫描都已完成"]
    [run] = daily_runs(env, c)
    assert run.scheduled_for == date(2026, 10, 14) and run.status == DONE


def test_bad_daily_time_is_rejected(env, monkeypatch):
    monkeypatch.setattr(config, "TRACKING_DAILY_TIME", "25:99")
    with pytest.raises(ValueError, match="HH:MM"):
        run_due(env.svc, at(14, 18))


def test_failed_scan_is_retried_after_interval_then_given_up(env, monkeypatch):
    c = tracked(env)
    monkeypatch.setattr(env.svc.scanner, "_run", lambda *a, **k: 1 / 0)
    assert "失败" in run_due(env.svc, at(14, 18, 1))[0]
    assert run_due(env.svc, at(14, 18, 2)) == ["2026-10-14 的每日扫描都已完成"]  # 等重试间隔，不立即重试
    assert len(daily_runs(env, c)) == 1

    for attempt in (2, 3):
        set_runs(env, finished_at=datetime.now() - timedelta(minutes=11))
        assert f"第 {attempt} 次尝试" in run_due(env.svc, at(14, 19))[0]
    set_runs(env, finished_at=datetime.now() - timedelta(minutes=11))
    assert "不再自动重试" in run_due(env.svc, at(14, 20))[0]
    assert [r.attempt for r in daily_runs(env, c)] == [3, 2, 1]
    assert all(r.status == FAILED for r in daily_runs(env, c))

    monkeypatch.undo()
    monkeypatch.setattr(config, "TRACKING_SETTLE_SECONDS", 0)
    assert "每日扫描完成" in run_due(env.svc, at(15, 18, 1))[0]  # 第二天恢复正常


def test_catch_up_after_downtime_records_gap(env):
    c = tracked(env)
    set_runs(env, finished_at=datetime(2026, 10, 9, 18, 0), started_at=datetime(2026, 10, 9, 18, 0))  # 上周五之后一直没运行
    env.write("a.md", "# A 离线期间改过")
    [line] = run_due(env.svc, at(15, 9))  # 周四早上启动：补扫周三的
    assert "补扫" in line
    [run] = daily_runs(env, c)
    assert run.scheduled_for == date(2026, 10, 14)
    assert run.stats["gap"]["missed_days"]  # 漏掉的日期如实记录，没有伪装成逐日采集
    assert env.svc.snapshots(env.file(c, "a.md").id)[-1].gap_before


def test_crashed_scan_is_recovered_after_lock_expires(env):
    c = tracked(env)
    with pytest.raises(KeyboardInterrupt):
        env.svc.scanner.scan(c.id, DAILY, scheduled_for=date(2026, 10, 14), interrupt_after=0)
    assert run_due(env.svc, at(14, 18, 5)) == ["2026-10-14 的每日扫描都已完成"]  # 锁还有效：当作正在扫
    with sessionmaker(env.svc.engine)() as s:
        s.execute(update(ScanLock).values(expires_at=datetime.now() - timedelta(seconds=1)))
        s.commit()
    assert "每日扫描完成" in run_due(env.svc, at(14, 18, 6))[0]
    statuses = [r.status for r in daily_runs(env, c)]
    assert statuses == [DONE, INTERRUPTED]


def test_busy_collection_is_skipped(env):
    c = tracked(env)
    with sessionmaker(env.svc.engine)() as s:
        s.add(ScanLock(collection_id=c.id, owner="other-process", expires_at=datetime.now() + timedelta(minutes=5)))
        s.commit()
    assert "正在扫描" in run_due(env.svc, at(14, 18, 5))[0]
    assert daily_runs(env, c) == []


def test_collections_without_active_sources_are_skipped(env):
    c = env.svc.create_collection("空集合")
    assert run_due(env.svc, at(14, 18, 5)) == ["2026-10-14 的每日扫描都已完成"]
    assert env.svc.scans(c.id) == []


def test_background_scheduler_thread(env):
    c = tracked(env)
    scheduler = DailyScheduler(env.svc, interval=0.05)
    scheduler.start()
    deadline = time.time() + 5
    while time.time() < deadline and not [r for r in daily_runs(env, c) if r.status == DONE]:
        time.sleep(0.05)
    scheduler.stop()
    assert not scheduler.running
    assert [r.status for r in daily_runs(env, c)] == [DONE]


def test_api_starts_and_stops_scheduler(env):
    app = create_app(env.store, tracking_scheduler=True)
    with TestClient(app):
        assert app.state.scheduler.running
    assert not app.state.scheduler.running
    plain = create_app(env.store)
    with TestClient(plain):
        assert plain.state.scheduler is None


def test_cli_run_due(env, capsys, monkeypatch):
    tracked(env)
    monkeypatch.setattr(config, "TRACKING_WORKDAYS_ONLY", False)
    monkeypatch.setattr(config, "TRACKING_DAILY_TIME", "00:00")
    assert track_cli.main(["run-due"], service=env.svc) == 0
    assert "每日扫描完成" in capsys.readouterr().out


# ---- 存储清理
def test_prune_keeps_referenced_latest_and_baseline_versions(env):
    c = tracked(env)
    f = env.file(c, "a.md")
    for i in range(2, 6):
        env.write("a.md", f"# A 第 {i} 版")
        env.svc.scan(c.id)
    snaps = env.svc.snapshots(f.id)  # v1..v5
    with sessionmaker(env.svc.engine)() as s:
        # v1~v4 是 90 天前的，v5 是现在的；v2 被一条工作记录引用
        for snap in snaps[:4]:
            s.execute(update(FileSnapshot).where(FileSnapshot.id == snap.id).values(captured_at=datetime.now() - timedelta(days=90)))
        s.add(WorkEvidence(prep_id=1, title="t", source_refs=[{"file_id": f.id, "from_snapshot_id": None, "to_snapshot_id": snaps[1].id}]))
        s.commit()
    old_digests = [snap.sha256 for snap in snaps]

    report = prune(env.svc.engine, env.svc.blobs, keep_days=30)

    left = [snap.version for snap in env.svc.snapshots(f.id)]
    assert left == [2, 4, 5]  # 引用的 v2、保留期前最后一版 v4（作基线）、最新 v5
    assert report["snapshots"] == 2 and report["protected"] == 2
    assert not env.svc.blobs.has(old_digests[0]) and env.svc.blobs.has(old_digests[1])
    assert report["materials"] == 2


def test_prune_never_deletes_user_materials(env):
    m, _ = env.store.add_text("用户材料", "# A")  # 与追踪文件内容相同
    c = tracked(env)
    f = env.file(c, "a.md")
    v1_material = env.svc.snapshots(f.id)[0].material_id
    assert v1_material == m.id  # 追踪版本复用了用户的同内容材料
    env.write("a.md", "# A 新版")
    env.svc.scan(c.id)
    with sessionmaker(env.svc.engine)() as s:
        s.execute(update(FileSnapshot).where(FileSnapshot.version.in_([1, 2])).values(captured_at=datetime.now() - timedelta(days=90)))
        s.commit()
    env.write("a.md", "# A 第三版")
    env.svc.scan(c.id)
    report = prune(env.svc.engine, env.svc.blobs, keep_days=30)
    assert [s.version for s in env.svc.snapshots(f.id)] == [2, 3]  # v1 被清理
    assert report["snapshots"] == 1 and report["materials"] == 0
    assert env.store.get(m.id).content == "# A"  # 用户自己的材料不删
