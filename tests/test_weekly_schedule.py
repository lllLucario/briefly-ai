"""每周定时：到点自动整理这一周的工作（检查更新 + 整理审核清单）、补做、重试、提醒。用假模型，不调用真实 API。"""
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import sessionmaker

from briefly import config
from briefly.api import create_app
from briefly.tracking.scanner import ScanBusy
from briefly.weekly import cli as weekly_cli
from briefly.weekly.models import RUN_DONE, RUN_FAILED, RUN_INTERRUPTED, RUN_RUNNING, ScheduledRun, WeeklySettings
from briefly.weekly.schedule import (
    Schedule,
    latest_occurrence,
    mark_read,
    notifications,
    notify,
    parse_time,
    parse_weekday,
    run_due,
)
from briefly.weekly.service import week_of
from test_tracking_review import make_model
from tracking_util import env  # noqa: F401


@pytest.fixture(autouse=True)
def local_time(monkeypatch):
    monkeypatch.setattr(config, "TRACKING_TIMEZONE", "")  # 用系统时区，与数据库时间戳一致
    monkeypatch.setattr(config, "TRACKING_SCAN_RETRIES", 1)
    monkeypatch.setattr(config, "TRACKING_RETRY_MINUTES", 10)


def now():
    return datetime.now(timezone.utc).astimezone()


def schedule_at(env, minutes_ago: int, backdate: bool = True):
    """把定时设在 minutes_ago 分钟前（今天的星期几），并把设置时间挪到一周前（否则过去的时间点不执行）。"""
    weekly = env.weekly(make_model)
    t = now() - timedelta(minutes=minutes_ago)
    schedule = Schedule(weekly)
    schedule.set(t.weekday(), t.strftime("%H:%M"))
    if backdate:
        with sessionmaker(env.svc.engine)() as s:
            s.execute(update(WeeklySettings).values(schedule_updated_at=datetime.now() - timedelta(days=7)))
            s.commit()
    return weekly, schedule


def tracked_with_change(env):
    env.write("方案.md", "# 方案\n原有内容")
    env.svc.add_paths([str(env.work / "方案.md")])
    env.write("方案.md", "# 方案\n原有内容\n补充灰度发布方案")


def test_parse_weekday_and_time():
    assert [parse_weekday(x) for x in ("1", "5", "周五", "星期日", "周天", "Fri", "7")] == [0, 4, 4, 6, 6, 4, 6]
    with pytest.raises(ValueError, match="无法识别"):
        parse_weekday("8")
    assert parse_time("9:05") == "09:05" and parse_time("16：30") == "16:30"
    with pytest.raises(ValueError, match="HH:MM"):
        parse_time("24:00")


def test_latest_occurrence_and_week():
    s = WeeklySettings(weekday=4, time="16:30")  # 周五 16:30
    tz = timezone(timedelta(hours=8))
    assert latest_occurrence(s, datetime(2026, 10, 16, 16, 30, tzinfo=tz)) == datetime(2026, 10, 16, 16, 30, tzinfo=tz)
    assert latest_occurrence(s, datetime(2026, 10, 16, 16, 29, tzinfo=tz)).date() == date(2026, 10, 9)  # 还没到：上周五
    assert latest_occurrence(s, datetime(2026, 10, 19, 9, 0, tzinfo=tz)).date() == date(2026, 10, 16)  # 周一：上周五
    assert week_of(date(2026, 10, 16)) == (date(2026, 10, 12), date(2026, 10, 18))


def test_due_schedule_prepares_once_and_notifies(env):
    tracked_with_change(env)
    weekly, schedule = schedule_at(env, minutes_ago=1)

    message = schedule.run_due()
    again = schedule.run_due()

    assert "每周自动整理完成：待确认 1 条" in message and again is None
    [run] = schedule.runs()
    assert run.status == RUN_DONE and run.late_minutes == 0 and run.report_id
    report = weekly.week(create=False)
    assert report.id == run.report_id and report.final_scan_id  # 做了检查更新
    assert report.status == "none"  # 只整理清单，不在审核前生成
    [n] = notifications(env.svc.engine)
    assert "1 条待你确认" in n.title and "briefly weekly review" in n.body and n.report_id == report.id


def test_not_yet_due_and_set_after_time_point(env):
    tracked_with_change(env)
    weekly = env.weekly(make_model)
    t = now() + timedelta(minutes=30)
    if t.date() == now().date():
        Schedule(weekly).set(t.weekday(), t.strftime("%H:%M"))
        assert Schedule(weekly).run_due() is None  # 还没到点（上一次是一周前，早于设置时间）
    _, schedule = schedule_at(env, minutes_ago=5, backdate=False)
    assert schedule.run_due() is None  # 设置时已经过了今天的时间点：等下周，不立即执行
    assert schedule.runs() == []


def test_missed_time_point_is_caught_up_with_lateness(env):
    tracked_with_change(env)
    _, schedule = schedule_at(env, minutes_ago=180)  # 服务三小时前没在运行
    assert "晚了" in schedule.run_due()
    [run] = schedule.runs()
    assert run.late_minutes >= 179
    assert "晚了" in notifications(env.svc.engine)[0].body


def test_no_changes_still_notifies(env):
    env.write("a.md", "# A")
    env.svc.add_paths([str(env.work / "a.md")])
    _, schedule = schedule_at(env, minutes_ago=1)
    schedule.run_due()
    assert "没有发现新的文件变化和待办进展" in notifications(env.svc.engine)[0].title


def test_failure_is_retried_then_reported(env, monkeypatch):
    tracked_with_change(env)
    weekly, schedule = schedule_at(env, minutes_ago=1)

    def busy(*a, **k):
        raise ScanBusy("正在检查更新，请稍后再试")

    monkeypatch.setattr(weekly, "prepare", busy)
    assert "失败（第 1 次）" in schedule.run_due()
    assert schedule.run_due() is None  # 等重试间隔
    with sessionmaker(env.svc.engine)() as s:
        s.execute(update(ScheduledRun).values(finished_at=datetime.now() - timedelta(minutes=11)))
        s.commit()
    assert "失败（第 2 次）" in schedule.run_due()
    [n] = notifications(env.svc.engine)
    assert n.kind == "weekly_failed" and "整理本周工作" in n.body and "briefly weekly prepare" in n.body
    with sessionmaker(env.svc.engine)() as s:
        s.execute(update(ScheduledRun).values(finished_at=datetime.now() - timedelta(minutes=11)))
        s.commit()
    assert schedule.run_due() is None  # 重试次数用完，不再自动执行
    assert [r.status for r in schedule.runs()] == [RUN_FAILED, RUN_FAILED]


def test_running_elsewhere_is_skipped_and_crash_recovered(env, monkeypatch):
    tracked_with_change(env)
    weekly, schedule = schedule_at(env, minutes_ago=1)
    occ = latest_occurrence(weekly.settings(), now()).astimezone().replace(tzinfo=None)
    with sessionmaker(env.svc.engine)() as s:
        s.add(ScheduledRun(occurrence=occ, status=RUN_RUNNING))
        s.commit()
    assert schedule.run_due() is None  # 另一个进程正在执行
    with sessionmaker(env.svc.engine)() as s:  # 那个进程崩溃了，记录停在 running
        s.execute(update(ScheduledRun).values(started_at=datetime.now() - timedelta(hours=1)))
        s.commit()
    monkeypatch.setattr(config, "TRACKING_RETRY_MINUTES", 0)
    assert "每周自动整理完成" in schedule.run_due()
    assert sorted(r.status for r in schedule.runs()) == [RUN_DONE, RUN_INTERRUPTED]


def test_long_running_prepare_is_not_interrupted_while_lease_is_alive(env, monkeypatch):
    from briefly.weekly.prepare_lock import PrepareLease
    tracked_with_change(env)
    weekly, schedule = schedule_at(env, minutes_ago=1)
    report = weekly.week()
    occ = latest_occurrence(weekly.settings(), now()).astimezone().replace(tzinfo=None)
    with PrepareLease(env.svc.engine, report.id):
        with sessionmaker(env.svc.engine)() as s:
            s.add(ScheduledRun(report_id=report.id, occurrence=occ, status=RUN_RUNNING, started_at=datetime.now() - timedelta(hours=1)))
            s.commit()
        assert schedule.run_due() is None
        assert schedule.runs()[0].status == RUN_RUNNING
    monkeypatch.setattr(config, "TRACKING_RETRY_MINUTES", 0)
    assert "自动整理完成" in schedule.run_due()
    assert sorted(r.status for r in schedule.runs()) == [RUN_DONE, RUN_INTERRUPTED]


def test_disable_and_update_schedule(env):
    tracked_with_change(env)
    weekly, schedule = schedule_at(env, minutes_ago=1)
    schedule.disable()
    assert schedule.run_due() is None and not weekly.settings().schedule_enabled
    updated = schedule.set(4, "16:30")
    assert updated.schedule_enabled and (updated.weekday, updated.time) == (4, "16:30")


def test_run_due_runs_daily_scan_and_weekly_start(env, monkeypatch):
    tracked_with_change(env)
    weekly, _ = schedule_at(env, minutes_ago=1)
    monkeypatch.setattr(config, "TRACKING_WORKDAYS_ONLY", False)
    monkeypatch.setattr(config, "TRACKING_DAILY_TIME", "23:59")  # 每日扫描不到期
    messages = run_due(weekly)
    assert any("每周自动整理完成" in m for m in messages)


def test_cli_schedule_settings_and_inbox(env, capsys):
    tracked_with_change(env)
    weekly = env.weekly(make_model)
    run = lambda *argv: weekly_cli.main(list(argv), service=weekly)  # noqa: E731
    assert run("schedule") == 0
    assert "还没有开启" in capsys.readouterr().out
    assert run("schedule", "--weekday", "周五", "--time", "16:30") == 0
    out = capsys.readouterr().out
    assert "每周五 16:30" in out and "下次" in out
    assert run("schedule", "--time", "16:30") == 1
    assert "同时指定" in capsys.readouterr().err
    assert run("settings", "--audience", "导师", "--owner", "张三", "--owner", "zhangsan") == 0
    assert "默认读者：导师" in capsys.readouterr().out and weekly.settings().owner_names == ["张三", "zhangsan"]

    _, schedule = schedule_at(env, minutes_ago=1)
    schedule.run_due()
    assert run("inbox") == 0
    assert "待你确认" in capsys.readouterr().out
    assert run("inbox") == 0 and "没有未读提醒" in capsys.readouterr().out  # 看过即已读
    assert run("schedule") == 0 and "[完成]" in capsys.readouterr().out
    assert run("schedule", "--off") == 0 and "已关闭" in capsys.readouterr().out


def test_api_settings_and_notifications(env):
    report = env.weekly().week()
    notify(env.svc.engine, "weekly_ready", "本周周报的审核清单已整理好", "审核：briefly weekly review", report.id)
    with TestClient(create_app(env.store)) as client:
        settings = client.put("/weekly-settings/schedule", json={"weekday": 4, "time": "16:30"}).json()
        assert settings["schedule"]["text"] == "每周五 16:30" and settings["schedule"]["next"]
        assert client.put("/weekly-settings/schedule", json={"weekday": 4, "time": "25:00"}).status_code == 400
        assert client.put("/weekly-settings", json={"audience": "导师"}).json()["audience"] == "导师"
        assert client.delete("/weekly-settings/schedule").json()["schedule"]["enabled"] is False
        [n] = client.get("/notifications").json()
        assert n["report_id"] == report.id
        assert client.post("/notifications/read", json={"ids": [n["id"]]}).json() == {"marked": 1}
        assert client.get("/notifications").json() == []
        assert len(client.get("/notifications", params={"unread_only": False}).json()) == 1
    assert mark_read(env.svc.engine) == 0
