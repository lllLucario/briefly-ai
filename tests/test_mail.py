"""邮件发送周报：地址校验、邮件内容、发送记录、失败不重试、接口。用模拟的 SMTP，不连真实服务器。"""
import smtplib
from datetime import date

import pytest
from fastapi.testclient import TestClient

from briefly import config, mail
from briefly.api import create_app
from briefly.db import create_db_engine
from briefly.materials import MaterialStore
from briefly.weekly import DONE, WeeklyService


class FakeSMTP:
    """记录登录和发出的邮件；fail 设为异常时在对应步骤抛出。"""

    def __init__(self, fail_login: Exception | None = None, fail_send: Exception | None = None) -> None:
        self.fail_login, self.fail_send = fail_login, fail_send
        self.logins, self.sent, self.quit_called, self.connects = [], [], False, 0

    def __call__(self):
        self.connects += 1
        return self

    def login(self, user, password):
        if self.fail_login:
            raise self.fail_login
        self.logins.append((user, password))

    def send_message(self, msg):
        if self.fail_send:
            raise self.fail_send
        self.sent.append(msg)

    def quit(self):
        self.quit_called = True


@pytest.fixture(autouse=True)
def smtp_config(monkeypatch):
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(config, "SMTP_USER", "me@example.com")
    monkeypatch.setattr(config, "SMTP_PASSWORD", "auth-code")
    monkeypatch.setattr(config, "MAIL_FROM_NAME", "王五")


@pytest.fixture
def weekly():
    engine = create_db_engine("sqlite://")
    w = WeeklyService(MaterialStore(engine), model_factory=lambda: None)
    yield w
    engine.dispose()


def done_report(w: WeeklyService) -> int:
    r = w.create([], "联调", date(2026, 10, 12), date(2026, 10, 18), "", "王五")
    w._update(r.id, status=DONE, content="【本周完成】\n- 支付回调降到 200ms〔1-1〕\n【下周计划】\n- 上线〔简述〕")
    return r.id


def test_parse_addresses():
    assert mail.parse_addresses("a@x.com， b@y.com;A@x.com  c@z.cn") == ["a@x.com", "b@y.com", "c@z.cn"]
    assert mail.parse_addresses("") == []
    with pytest.raises(ValueError, match="格式不对"):
        mail.parse_addresses("a@x.com, 导师")


def test_send_weekly(weekly):
    smtp = FakeSMTP()
    rid = done_report(weekly)
    record = mail.send_weekly(weekly, rid, "mentor@example.com", "me@example.com", connect=smtp)
    assert record.status == mail.SENT and record.subject == "王五 周报 10.12–10.18"
    assert smtp.logins == [("me@example.com", "auth-code")] and smtp.quit_called
    msg = smtp.sent[0]
    assert msg["To"] == "mentor@example.com" and msg["Cc"] == "me@example.com"
    assert "王五" in msg["From"] and "me@example.com" in msg["From"]
    body = msg.get_body(("plain",)).get_content()
    assert "支付回调降到 200ms" in body and "〔" not in body  # 出处标记不发出去
    attachment = next(msg.iter_attachments())
    assert attachment.get_filename() == "周报_2026-10-12_2026-10-18.md"
    assert [x.status for x in mail.sends_of(weekly.store.engine, rid)] == [mail.SENT]


def test_auth_failure_is_recorded_and_not_retried(weekly):
    smtp = FakeSMTP(fail_login=smtplib.SMTPAuthenticationError(535, b"auth failed"))
    rid = done_report(weekly)
    record = mail.send_weekly(weekly, rid, "mentor@example.com", connect=smtp)
    assert record.status == mail.FAILED and "授权码" in record.error
    assert smtp.connects == 1 and not smtp.sent and smtp.quit_called


def test_connection_failure(weekly):
    def refuse():
        raise ConnectionRefusedError("refused")

    rid = done_report(weekly)
    record = mail.send_weekly(weekly, rid, "mentor@example.com", connect=refuse)
    assert record.status == mail.FAILED and "连接邮件服务器失败" in record.error


def test_rejected_recipient(weekly):
    smtp = FakeSMTP(fail_send=smtplib.SMTPRecipientsRefused({"bad@example.com": (550, b"no such user")}))
    record = mail.send_weekly(weekly, done_report(weekly), "bad@example.com", connect=smtp)
    assert record.status == mail.FAILED and "bad@example.com" in record.error


def test_checks_before_sending(weekly, monkeypatch):
    smtp = FakeSMTP()
    r = weekly.create([], "联调", date(2026, 10, 12), date(2026, 10, 18), "", "")
    with pytest.raises(ValueError, match="还没有生成完成"):
        mail.send_weekly(weekly, r.id, "mentor@example.com", connect=smtp)
    with pytest.raises(ValueError, match="收件人"):
        mail.send_weekly(weekly, done_report(weekly), "", connect=smtp)
    monkeypatch.setattr(config, "SMTP_PASSWORD", "")
    with pytest.raises(mail.MailNotConfigured, match="SMTP"):
        mail.send_weekly(weekly, done_report(weekly), "mentor@example.com", connect=smtp)
    assert smtp.connects == 0


def test_api(monkeypatch):
    engine = create_db_engine("sqlite://")
    monkeypatch.setattr(config, "MAIL_DEFAULT_TO", "mentor@example.com")
    app = create_app(store=MaterialStore(engine), model_factory=lambda: None)
    smtp = FakeSMTP()
    app.state.mail_connect = smtp
    with TestClient(app) as client:
        assert client.get("/mail/status").json() == {"configured": True, "sender": "me@example.com", "default_to": "mentor@example.com"}
        rid = done_report(app.state.weekly)
        out = client.post(f"/weekly-reports/{rid}/email", json={"to": "mentor@example.com"})
        assert out.status_code == 201 and out.json()["status"] == "sent"
        assert client.post(f"/weekly-reports/{rid}/email", json={"to": "导师"}).status_code == 400
        assert [x["to"] for x in client.get(f"/weekly-reports/{rid}/email").json()] == ["mentor@example.com"]
        assert client.post("/weekly-reports/999/email", json={"to": "a@b.com"}).status_code == 404
    assert len(smtp.sent) == 1
    engine.dispose()


def test_spam_rejection_is_explained(weekly):
    smtp = FakeSMTP(fail_send=smtplib.SMTPDataError(554, b"DT:SPM 163 smtp14"))
    record = mail.send_weekly(weekly, done_report(weekly), "mentor@example.com", connect=smtp)
    assert record.status == mail.FAILED and "垃圾邮件" in record.error and "DT:SPM" in record.error
