"""登记表第 87 条：外发幂等。同一次发送动作只发一次，重复请求返回第一次的结果；结果未知不算没发，不自动重发。

SMTP 和飞书都用替身；并发用真实线程，发送途中用事件卡住，第二个请求一定落在第一个发送的过程中。
"""
import threading
from datetime import date

import pytest
from fastapi.testclient import TestClient

from briefly import config
from briefly.api import create_app
from briefly.db import create_db_engine
from briefly.materials import MaterialStore
from briefly.weekly.models import DONE
from briefly.weekly.service import WeeklyService
from fakes import FakeFeishu
from test_feishu import API, DOC, write_routes


class HoldingSMTP:
    """假 SMTP：连接工厂。hold 设置后，发信时等放行。"""

    def __init__(self, hold=False):
        self.sent, self.failures = [], []
        self.entered = threading.Event()
        self.release = threading.Event()
        if not hold:
            self.release.set()

    def __call__(self):
        return self

    def login(self, user, password):
        pass

    def send_message(self, msg):
        self.entered.set()
        assert self.release.wait(20)
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append(msg)

    def quit(self):
        pass


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(config, "SMTP_USER", "me@example.com")
    monkeypatch.setattr(config, "SMTP_PASSWORD", "auth-code")
    monkeypatch.setattr(config, "FEISHU_NOTIFY_USER", "")
    monkeypatch.setattr(config, "FEISHU_FOLDER_TOKEN", "fld")
    engine = create_db_engine(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    feishu = FakeFeishu(write_routes([]))
    app = create_app(store=MaterialStore(engine), model_factory=lambda: None, feishu_factory=feishu.client)
    report = app.state.weekly.week(date(2026, 10, 14))
    app.state.weekly._update(report.id, status=DONE, content="【本周完成】\n- 完成支付回调")
    yield app, feishu, report.id
    engine.dispose()


def client_of(app):
    return TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)


def documents_created(feishu):
    return len(feishu.calls("POST", f"{API}/docx/v1/documents"))


def concurrent(*requests):
    """依次发出的并发请求：前一个进入外部调用后，才发下一个。"""
    results = [None] * len(requests)

    def run(i, request):
        results[i] = request()

    threads = [threading.Thread(target=run, args=(i, r), daemon=True) for i, r in enumerate(requests)]
    return threads, results


# ---- 邮件
def test_two_tabs_sending_the_same_report_send_one_mail(env):
    app, _, rid = env
    smtp = HoldingSMTP(hold=True)
    app.state.mail_connect = smtp
    with client_of(app) as client:
        payload = dict(to="mentor@example.com")
        threads, results = concurrent(lambda: client.post(f"/weekly/{rid}/email", json=payload),
                                      lambda: client.post(f"/weekly/{rid}/email", json=payload))
        threads[0].start()
        assert smtp.entered.wait(20)
        threads[1].start()
        threads[1].join(0.5)  # 第二个请求要么已被拦下在等，要么（修复前）已进入发送
        smtp.release.set()
        for t in threads:
            t.join(30)
        assert len(smtp.sent) == 1
        assert sorted(r.status_code for r in results) == [200, 201]
        assert results[0].json()["id"] == results[1].json()["id"]
        assert [r.json()["status"] for r in results] == ["sent", "sent"]
        assert len(client.get(f"/weekly/{rid}/email").json()) == 1


def test_repeating_a_send_returns_the_first_result(env):
    app, _, rid = env
    smtp = HoldingSMTP()
    app.state.mail_connect = smtp
    with client_of(app) as client:
        first = client.post(f"/weekly/{rid}/email", json=dict(to="mentor@example.com", request_id="click-1"))
        again = client.post(f"/weekly/{rid}/email", json=dict(to="mentor@example.com", request_id="click-1"))
        assert first.status_code == 201 and again.status_code == 200
        assert again.json()["id"] == first.json()["id"] and again.json()["duplicate"] is True
        assert first.json()["duplicate"] is False
    assert len(smtp.sent) == 1


def test_confirmed_resend_is_a_new_action_and_itself_idempotent(env):
    app, _, rid = env
    smtp = HoldingSMTP()
    app.state.mail_connect = smtp
    with client_of(app) as client:
        client.post(f"/weekly/{rid}/email", json=dict(to="mentor@example.com"))
        resend = dict(to="mentor@example.com", request_id="click-2", resend=True)
        assert client.post(f"/weekly/{rid}/email", json=resend).status_code == 201
        assert client.post(f"/weekly/{rid}/email", json=resend).status_code == 200
    assert len(smtp.sent) == 2


def test_resend_without_request_id_is_rejected(env):
    app, _, rid = env
    app.state.mail_connect = HoldingSMTP()
    with client_of(app) as client:
        response = client.post(f"/weekly/{rid}/email", json=dict(to="mentor@example.com", resend=True))
    assert response.status_code == 400 and "request_id" in response.json()["detail"]


def test_other_recipients_or_changed_content_are_a_new_send(env):
    app, _, rid = env
    smtp = HoldingSMTP()
    app.state.mail_connect = smtp
    with client_of(app) as client:
        client.post(f"/weekly/{rid}/email", json=dict(to="a@example.com"))
        assert client.post(f"/weekly/{rid}/email", json=dict(to="b@example.com")).status_code == 201
        app.state.weekly._update(rid, content="【本周完成】\n- 改过的内容")
        assert client.post(f"/weekly/{rid}/email", json=dict(to="a@example.com")).status_code == 201
    assert len(smtp.sent) == 3


def test_definite_failure_can_be_retried_by_a_new_click_but_replay_gets_the_first_result(env):
    import smtplib

    app, _, rid = env
    smtp = HoldingSMTP()
    smtp.failures.append(smtplib.SMTPRecipientsRefused({"a@example.com": (550, b"no")}))
    app.state.mail_connect = smtp
    with client_of(app) as client:
        first = client.post(f"/weekly/{rid}/email", json=dict(to="a@example.com", request_id="click-1"))
        assert first.json()["status"] == "failed"
        replay = client.post(f"/weekly/{rid}/email", json=dict(to="a@example.com", request_id="click-1"))
        assert replay.json()["id"] == first.json()["id"] and not smtp.sent
        retry = client.post(f"/weekly/{rid}/email", json=dict(to="a@example.com", request_id="click-2"))
        assert retry.status_code == 201 and retry.json()["status"] == "sent" and len(smtp.sent) == 1
        # 失败和成功都留记录
        assert [x["status"] for x in client.get(f"/weekly/{rid}/email").json()] == ["sent", "failed"]


def test_unknown_result_is_not_resent_without_confirmation(env):
    app, _, rid = env
    smtp = HoldingSMTP()
    smtp.failures.append(TimeoutError("发送途中超时"))
    app.state.mail_connect = smtp
    with client_of(app) as client:
        first = client.post(f"/weekly/{rid}/email", json=dict(to="a@example.com", request_id="click-1"))
        assert first.json()["status"] == "unknown"
        # 用户又点了一次（新的动作）：超时不等于没发，不能自动重发
        again = client.post(f"/weekly/{rid}/email", json=dict(to="a@example.com", request_id="click-2"))
        assert again.status_code == 200 and again.json()["id"] == first.json()["id"] and again.json()["status"] == "unknown"
        assert again.json()["duplicate"] is True and not smtp.sent
        # 用户核对后明确确认再发
        confirmed = client.post(f"/weekly/{rid}/email", json=dict(to="a@example.com", request_id="click-3", resend=True))
        assert confirmed.status_code == 201 and len(smtp.sent) == 1


def test_mail_record_exists_before_the_send_starts(env):
    """发送中进程崩溃也要有记录：发之前就写一条“发送中”，不是发完才写。"""
    app, _, rid = env
    smtp = HoldingSMTP(hold=True)
    app.state.mail_connect = smtp
    with client_of(app) as client:
        worker = threading.Thread(target=lambda: client.post(f"/weekly/{rid}/email", json=dict(to="a@example.com")), daemon=True)
        worker.start()
        assert smtp.entered.wait(20)
        assert [x["status"] for x in client.get(f"/weekly/{rid}/email").json()] == ["sending"]
        smtp.release.set()
        worker.join(30)
        assert [x["status"] for x in client.get(f"/weekly/{rid}/email").json()] == ["sent"]


def test_stale_send_counts_as_unknown_and_is_not_resent(env, monkeypatch):
    """进程在发送途中死掉，记录停在“发送中”：过了时限算结果未知，不能当作没发。"""
    from sqlalchemy import update
    from sqlalchemy.orm import Session

    from briefly.mail import MailSend
    from briefly.outbound import OutboundClaim

    app, _, rid = env
    smtp = HoldingSMTP()
    app.state.mail_connect = smtp
    with client_of(app) as client:
        first = client.post(f"/weekly/{rid}/email", json=dict(to="a@example.com"))
        with Session(app.state.store.engine) as s:  # 还原成进程死掉时留下的样子
            s.execute(update(MailSend).values(status="sending"))
            s.execute(update(OutboundClaim).values(status="sending"))
            s.commit()
        monkeypatch.setattr("briefly.outbound.STALE_SECONDS", -1)
        again = client.post(f"/weekly/{rid}/email", json=dict(to="a@example.com"))
        assert again.status_code == 200 and again.json()["id"] == first.json()["id"]
        assert again.json()["status"] == "unknown" and len(smtp.sent) == 1


# ---- 飞书
def test_two_tabs_exporting_the_same_report_create_one_document(env):
    app, feishu, rid = env
    entered, release = threading.Event(), threading.Event()

    def hold(request):
        entered.set()
        assert release.wait(20)
        return {"document": {"document_id": DOC}}

    feishu.routes[("POST", f"{API}/docx/v1/documents")] = hold
    with client_of(app) as client:
        threads, results = concurrent(lambda: client.post(f"/weekly/{rid}/feishu"), lambda: client.post(f"/weekly/{rid}/feishu"))
        threads[0].start()
        assert entered.wait(20)
        threads[1].start()
        threads[1].join(0.5)
        release.set()
        for t in threads:
            t.join(30)
        assert documents_created(feishu) == 1
        assert sorted(r.status_code for r in results) == [200, 201]
        assert results[0].json()["id"] == results[1].json()["id"]
        assert len(client.get(f"/weekly/{rid}/feishu").json()) == 1


def test_export_again_needs_confirmation(env):
    app, feishu, rid = env
    with client_of(app) as client:
        first = client.post(f"/weekly/{rid}/feishu")
        again = client.post(f"/weekly/{rid}/feishu")
        assert first.status_code == 201 and again.status_code == 200 and again.json()["id"] == first.json()["id"]
        assert again.json()["duplicate"] is True and documents_created(feishu) == 1
        resend = dict(request_id="click-2", resend=True)
        assert client.post(f"/weekly/{rid}/feishu", json=resend).status_code == 201
        assert client.post(f"/weekly/{rid}/feishu", json=resend).status_code == 200
        assert documents_created(feishu) == 2


def test_failed_export_is_not_retried_without_confirmation(env):
    """飞书写入失败时可能已经建了文档：结果不明，新的点击也不自动再写。"""
    import httpx

    app, feishu, rid = env
    feishu.routes[("POST", f"{API}/docx/v1/documents/{DOC}/blocks/{DOC}/descendant")] = lambda r: httpx.Response(500, json={"code": 1, "msg": "写入失败"})
    with client_of(app) as client:
        first = client.post(f"/weekly/{rid}/feishu", json=dict(request_id="click-1"))
        assert first.json()["status"] == "failed"
        again = client.post(f"/weekly/{rid}/feishu", json=dict(request_id="click-2"))
        assert again.status_code == 200 and again.json()["id"] == first.json()["id"] and again.json()["status"] == "failed"
        assert documents_created(feishu) == 1


def test_error_after_the_document_was_created_keeps_a_record_and_blocks_repeat(env, monkeypatch):
    """文档建好后程序出错：接口 500，但记录要在，重复请求不能再建一篇。"""
    app, feishu, rid = env

    def broken(*args, **kwargs):
        raise RuntimeError("建好文档之后的程序错误")

    monkeypatch.setattr("briefly.feishu.service.document_url", broken)
    with client_of(app) as client:
        first = client.post(f"/weekly/{rid}/feishu")
        assert first.status_code == 500
        monkeypatch.undo()
        again = client.post(f"/weekly/{rid}/feishu")
        assert again.status_code == 200 and again.json()["duplicate"] is True
        assert documents_created(feishu) == 1
        assert client.get(f"/weekly/{rid}/feishu").json()[0]["id"] == again.json()["id"]
