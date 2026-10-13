"""每周定时准备周报：设置、到点执行、补做、重试、提醒。用假模型，不调用真实 API。"""
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import sessionmaker

from briefly import config
from briefly.api import create_app
from briefly.tracking import cli as track_cli
from briefly.tracking import pipeline
from briefly.tracking.models import DONE, FAILED, INTERRUPTED, RUNNING, ScheduledRun, WeeklySchedule
from briefly.tracking.scanner import ScanBusy
from briefly.tracking.scheduler import run_due
from briefly.tracking.weekly_schedule import (
    ScheduleService,
    latest_occurrence,
    mark_read,
    notifications,
    parse_time,
    parse_weekday,
    week_of,
)
from test_tracking_review import make_model
from tracking_util import env  # noqa: F401


@pytest.fixture(autouse=True)
def local_time(monkeypatch):
    monkeypatch.setattr(config, "TRACKING_TIMEZONE", "")  # 用系统时区，与数据库时间戳一致
    monkeypatch.setattr(config, "TRACKING_SCAN_RETRIES", 1)
    monkeypatch.setattr(config, "TRACKING_RETRY_MINUTES", 10)


def now():
    return datetime.now(timezone.utc).astimezone()


def schedule_at(env, c, minutes_ago: int, backdate: bool = True):
    """把定时设在 minutes_ago 分钟前（今天的星期几），并把设置时间挪到一周前（否则过去的时间点不执行）。"""
    t = now() - timedelta(minutes=minutes_ago)
    svc = ScheduleService(env.svc, make_model)
    schedule = svc.set(c.id, t.weekday(), t.strftime("%H:%M"))
    if backdate:
        with sessionmaker(env.svc.engine)() as s:
            s.execute(update(WeeklySchedule).values(updated_at=datetime.now() - timedelta(days=7)))
            s.commit()
    return svc, schedule


def tracked_with_change(env):
    env.write("方案.md", "# 方案\n原有内容")
    c = env.svc.create_collection("项目A")
    env.svc.add_paths(c.id, [str(env.work / "方案.md")])
    env.write("方案.md", "# 方案\n原有内容\n补充灰度发布方案")
    return c


def runs(env, c):
    return ScheduleService(env.svc).runs(c.id)


def test_parse_weekday_and_time():
    assert [parse_weekday(x) for x in ("1", "5", "周五", "星期日", "周天", "Fri", "7")] == [0, 4, 4, 6, 6, 4, 6]
    with pytest.raises(ValueError, match="无法识别"):
        parse_weekday("8")
    assert parse_time("9:05") == "09:05" and parse_time("16：30") == "16:30"
    with pytest.raises(ValueError, match="HH:MM"):
        parse_time("24:00")


def test_latest_occurrence_and_week():
    s = WeeklySchedule(weekday=4, time="16:30")  # 周五 16:30
    tz = timezone(timedelta(hours=8))
    assert latest_occurrence(s, datetime(2026, 10, 16, 16, 30, tzinfo=tz)) == datetime(2026, 10, 16, 16, 30, tzinfo=tz)
    assert latest_occurrence(s, datetime(2026, 10, 16, 16, 29, tzinfo=tz)).date() == date(2026, 10, 9)  # 还没到：上周五
    assert latest_occurrence(s, datetime(2026, 10, 19, 9, 0, tzinfo=tz)).date() == date(2026, 10, 16)  # 周一：上周五
    assert week_of(date(2026, 10, 16)) == (date(2026, 10, 12), date(2026, 10, 18))


def test_due_schedule_prepares_once_and_notifies(env):
    c = tracked_with_change(env)
    svc, _ = schedule_at(env, c, minutes_ago=1)

    messages = svc.run_due()
    again = svc.run_due()

    assert "每周定时准备完成：待确认 1 条" in messages[0] and again == []
    [run] = runs(env, c)
    assert run.status == DONE and run.late_minutes == 0 and run.prep_id
    prep = pipeline.get_prep(env.svc, c.id, *week_of(date.today()), create=False)
    assert prep.id == run.prep_id and prep.final_scan_id  # 做了最终扫描
    [n] = notifications(env.svc.engine)
    assert "1 条工作记录待你确认" in n.title and "briefly track review 项目A" in n.body and n.prep_id == prep.id


def test_not_yet_due_and_set_after_time_point(env):
    c = tracked_with_change(env)
    t = now() + timedelta(minutes=30)
    svc = ScheduleService(env.svc, make_model)
    if t.date() == now().date():
        svc.set(c.id, t.weekday(), t.strftime("%H:%M"))
        assert svc.run_due() == []  # 还没到点（上一次是一周前，早于设置时间）
    svc2, _ = schedule_at(env, c, minutes_ago=5, backdate=False)
    assert svc2.run_due() == []  # 设置时已经过了今天的时间点：等下周，不立即执行
    assert runs(env, c) == []


def test_missed_time_point_is_caught_up_with_lateness(env):
    c = tracked_with_change(env)
    svc, _ = schedule_at(env, c, minutes_ago=180)  # 服务三小时前没在运行
    assert "晚了" in svc.run_due()[0]
    [run] = runs(env, c)
    assert run.late_minutes >= 179
    assert "晚了" in notifications(env.svc.engine)[0].body


def test_no_changes_still_notifies(env):
    env.write("a.md", "# A")
    c = env.svc.create_collection("c")
    env.svc.add_paths(c.id, [str(env.work / "a.md")])
    svc, _ = schedule_at(env, c, minutes_ago=1)
    svc.run_due()
    assert "没有发现新的文件变化" in notifications(env.svc.engine)[0].title


def test_failure_is_retried_then_reported(env, monkeypatch):
    c = tracked_with_change(env)
    svc, _ = schedule_at(env, c, minutes_ago=1)

    def busy(*a, **k):
        raise ScanBusy("该集合正在扫描，请稍后再试")

    monkeypatch.setattr(pipeline, "prepare", busy)
    assert "失败（第 1 次）" in svc.run_due()[0]
    assert svc.run_due() == []  # 等重试间隔
    with sessionmaker(env.svc.engine)() as s:
        s.execute(update(ScheduledRun).values(finished_at=datetime.now() - timedelta(minutes=11)))
        s.commit()
    assert "失败（第 2 次）" in svc.run_due()[0]
    [n] = notifications(env.svc.engine)
    assert n.kind == "weekly_failed" and "briefly track prepare 项目A" in n.body
    with sessionmaker(env.svc.engine)() as s:
        s.execute(update(ScheduledRun).values(finished_at=datetime.now() - timedelta(minutes=11)))
        s.commit()
    assert svc.run_due() == []  # 重试次数用完，不再自动执行
    assert [r.status for r in runs(env, c)] == [FAILED, FAILED]


def test_running_elsewhere_is_skipped_and_crash_recovered(env, monkeypatch):
    c = tracked_with_change(env)
    svc, schedule = schedule_at(env, c, minutes_ago=1)
    occ = latest_occurrence(schedule, now()).astimezone().replace(tzinfo=None)
    with sessionmaker(env.svc.engine)() as s:
        s.add(ScheduledRun(schedule_id=schedule.id, collection_id=c.id, occurrence=occ, status=RUNNING))
        s.commit()
    assert svc.run_due() == []  # 另一个进程正在执行
    with sessionmaker(env.svc.engine)() as s:  # 那个进程崩溃了，记录停在 running
        s.execute(update(ScheduledRun).values(started_at=datetime.now() - timedelta(hours=1)))
        s.commit()
    monkeypatch.setattr(config, "TRACKING_RETRY_MINUTES", 0)
    assert "每周定时准备完成" in svc.run_due()[0]
    assert sorted(r.status for r in runs(env, c)) == [DONE, INTERRUPTED]


def test_disable_and_update_schedule(env):
    c = tracked_with_change(env)
    svc, _ = schedule_at(env, c, minutes_ago=1)
    svc.disable(c.id)
    assert svc.run_due() == [] and not svc.get(c.id).enabled
    updated = svc.set(c.id, 4, "16:30")
    assert updated.enabled and (updated.weekday, updated.time) == (4, "16:30")
    with pytest.raises(ValueError, match="还没有设置"):
        ScheduleService(env.svc).disable(env.svc.create_collection("另一个").id)


def test_run_due_includes_weekly_and_scheduler_thread_uses_model_factory(env, monkeypatch):
    c = tracked_with_change(env)
    schedule_at(env, c, minutes_ago=1)
    monkeypatch.setattr(config, "TRACKING_WORKDAYS_ONLY", False)
    monkeypatch.setattr(config, "TRACKING_DAILY_TIME", "23:59")  # 每日扫描不到期
    messages = run_due(env.svc, model_factory=make_model)
    assert any("每周定时准备完成" in m for m in messages)


def test_delete_collection_removes_schedule(env):
    c = tracked_with_change(env)
    svc, _ = schedule_at(env, c, minutes_ago=1)
    svc.run_due()
    env.svc.delete_collection(c.id)
    with sessionmaker(env.svc.engine)() as s:
        assert s.query(WeeklySchedule).count() == 0 and s.query(ScheduledRun).count() == 0
    assert notifications(env.svc.engine, unread_only=False) == []


def test_cli_schedule_and_inbox(env, capsys):
    c = tracked_with_change(env)
    run = lambda *argv: track_cli.main(list(argv), service=env.svc)  # noqa: E731
    assert run("schedule", "项目A") == 0
    assert "还没有设置" in capsys.readouterr().out
    assert run("schedule", "项目A", "--weekday", "周五", "--time", "16:30") == 0
    out = capsys.readouterr().out
    assert "每周五 16:30" in out and "下次" in out
    assert run("schedule", "项目A", "--time", "16:30") == 1
    assert "同时指定" in capsys.readouterr().err

    svc, _ = schedule_at(env, c, minutes_ago=1)
    svc.run_due()
    assert run("list") == 0
    assert "有 1 条未读提醒" in capsys.readouterr().out
    assert run("inbox") == 0
    out = capsys.readouterr().out
    assert "待你确认" in out
    assert run("inbox") == 0 and "没有未读提醒" in capsys.readouterr().out  # 看过即已读
    assert run("schedule", "项目A") == 0 and "[完成]" in capsys.readouterr().out
    assert run("schedule", "项目A", "--off") == 0 and "已关闭" in capsys.readouterr().out


def test_api_notifications(env):
    from briefly.tracking.weekly_schedule import notify

    notify(env.svc.engine, "weekly_ready", "「项目A」本周周报已准备好", "审核：briefly track review 项目A", 1, 1)
    with TestClient(create_app(env.store)) as client:
        [n] = client.get("/notifications").json()
        assert n["title"].startswith("「项目A」")
        assert client.post("/notifications/read", json={"ids": [n["id"]]}).json() == {"marked": 1}
        assert client.get("/notifications").json() == []
        assert len(client.get("/notifications", params={"unread_only": False}).json()) == 1
    assert mark_read(env.svc.engine) == 0
