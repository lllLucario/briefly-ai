"""F2/F3：独立数据库连接竞争、超时与旧任务回写保护；不用真实模型。"""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.orm import Session

from briefly.db import create_db_engine
from briefly.materials import MaterialStore
from briefly.weekly import service as module
from briefly.weekly.models import DONE, FAILED, PENDING_GEN, RUNNING, WeeklyGenerationLease, WeeklyReport, WeeklyReportVersion
from briefly.weekly.service import WeeklyService, generation_now


@pytest.fixture
def env(tmp_path):
    url = f"sqlite:///{(tmp_path / 'generation.db').as_posix()}"
    engines = [create_db_engine(url), create_db_engine(url)]
    services = [WeeklyService(MaterialStore(e)) for e in engines]
    w = services[0]
    report = w.week()
    w.set_note(report.id, "已完成接口开发。")
    w.settings()
    yield services, report.id
    for engine in engines:
        engine.dispose()


def expire(w, report_id):
    with Session(w.engine) as s:
        s.execute(update(WeeklyGenerationLease).where(WeeklyGenerationLease.report_id == report_id).values(heartbeat_at=generation_now() - timedelta(minutes=31)))
        s.commit()


def test_other_service_preserves_pending_and_running_but_recovers_expired(env):
    (w, other), rid = env
    report = w.start_generation(rid)
    assert other.report(rid).status == PENDING_GEN
    with Session(w.engine) as s:
        s.execute(update(WeeklyReport).where(WeeklyReport.id == rid).values(status=RUNNING))
        s.commit()
    expire(w, rid)
    assert w._renew_generation(rid, report.generation_token)
    assert other.report(rid).status == RUNNING
    expire(w, rid)
    assert other.report(rid).status == FAILED
    assert "生成中断" in other.report(rid).error


def test_legacy_active_report_receives_grace_period(env):
    (w, _), rid = env
    with Session(w.engine) as s:
        s.execute(update(WeeklyReport).where(WeeklyReport.id == rid).values(status=RUNNING))
        s.commit()
    assert w.report(rid).status == RUNNING
    expire(w, rid)
    assert w.report(rid).status == FAILED


def test_concurrent_start_has_one_winner_and_one_audit_event(env):
    services, rid = env
    barrier = Barrier(2)
    def synchronize(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE weekly_reports SET checklist="):
            barrier.wait(timeout=10)
    for w in services:
        event.listen(w.engine, "before_cursor_execute", synchronize)
    def start(w):
        try:
            return w.start_generation(rid).status
        except ValueError as exc:
            assert "正在生成" in str(exc)
            return "busy"
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(start, services)) == ["busy", PENDING_GEN]
    finally:
        for w in services:
            event.remove(w.engine, "before_cursor_execute", synchronize)
    from briefly.weekly.models import EvidenceReview
    with Session(services[0].engine) as s:
        assert len(list(s.scalars(select(EvidenceReview).where(EvidenceReview.action == "generate")))) == 1


@pytest.mark.parametrize("fail_old", [False, True])
def test_expired_worker_cannot_overwrite_new_generation(env, monkeypatch, fail_old):
    (w, other), rid = env
    old = w.start_generation(rid)
    entered, release = Event(), Event()
    def write(writer, report):
        entered.set()
        assert release.wait(10)
        if fail_old:
            raise RuntimeError("旧任务失败")
        return "旧任务正文", [], {}
    monkeypatch.setattr(module.ReportWriter, "write", write)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(w.run_generation, rid, old.generation_token)
        assert entered.wait(10)
        try:
            assert WeeklyService(other.store).report(rid).status == RUNNING
            expire(w, rid)
            assert other.report(rid).status == FAILED
            new = other.start_generation(rid)
            w.run_generation(rid, old.generation_token)  # 迟到的旧后台回调也不能认领新任务
            assert other.report(rid).status == PENDING_GEN
        finally:
            release.set()
        future.result(10)
    assert other.report(rid).status == PENDING_GEN
    assert not other.report(rid).content
    monkeypatch.setattr(module.ReportWriter, "write", lambda *_: ("新任务正文", [], {}))
    other.run_generation(rid, new.generation_token)
    other.run_generation(rid, new.generation_token)  # 重复回调不会重复生成/写版本
    assert other.report(rid).status == DONE
    assert other.report(rid).content == "新任务正文"
    assert other.versions(rid) == []


def test_restore_and_edit_refuse_active_generation(env):
    (w, _), rid = env
    with Session(w.engine) as s:
        v = WeeklyReportVersion(report_id=rid, content="历史正文", reason="edit")
        s.add(v)
        s.commit()
        vid = v.id
    w.start_generation(rid)
    with pytest.raises(ValueError, match="正在生成"):
        w.restore_version(rid, vid)
    with pytest.raises(ValueError, match="没有生成完成"):
        w.update_content(rid, "修改正文")
    assert w.report(rid).status == PENDING_GEN


def test_generation_renews_while_writer_is_blocked(env, monkeypatch):
    (w, _), rid = env
    renewed = Event()
    original = w._renew_generation
    calls = []
    def renew(*args):
        calls.append(1)
        result = original(*args)
        if len(calls) > 1:
            renewed.set()
        return result
    monkeypatch.setattr(w, "_renew_generation", renew)
    monkeypatch.setattr(module, "GENERATION_HEARTBEAT_SECONDS", 0.02)
    def write(*_):
        assert renewed.wait(5)
        return "完成", [], {}
    monkeypatch.setattr(module.ReportWriter, "write", write)
    assert w.generate(rid).status == DONE
