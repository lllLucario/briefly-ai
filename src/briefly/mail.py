"""邮件：把生成好的周报发给指定收件人（SMTP）。

只由用户在网页上手动触发、发送前确认收件人；不提供给智能体作为工具，避免模型代用户发信。
发送失败不自动重试：SMTP 可能已经把信交出去了，重试会重复发送。每次发送都记录在 mail_sends。
"""
import logging
import re
import smtplib
import ssl
from collections.abc import Callable
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from sqlalchemy import DateTime, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from . import config
from .db import Base

log = logging.getLogger(__name__)
_EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
SENT, FAILED, UNKNOWN = "sent", "failed", "unknown"


class MailNotConfigured(ValueError):
    """没有配置 SMTP。继承 ValueError，接口层返回 400。"""


class MailSend(Base):
    __tablename__ = "mail_sends"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(Integer, index=True)
    to: Mapped[str] = mapped_column(Text)  # 逗号分隔
    cc: Mapped[str] = mapped_column(Text, default="")
    subject: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(10))
    error: Mapped[str] = mapped_column(Text, default="")
    message_id: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


def configured() -> bool:
    return bool(config.SMTP_HOST and config.SMTP_USER and config.SMTP_PASSWORD)


def parse_addresses(value: str | list[str] | None) -> list[str]:
    """“a@x.com, b@y.com；c@z.com” → 列表；格式不对的地址报错（中文说明）。"""
    if not value:
        return []
    items = value if isinstance(value, list) else re.split(r"[,，;；\s]+", value)
    out = []
    for item in (x.strip() for x in items):
        if not item:
            continue
        if not _EMAIL.match(item):
            raise ValueError(f"邮箱地址格式不对：{item}")
        if item.lower() not in {x.lower() for x in out}:
            out.append(item)
    return out


def _connect() -> smtplib.SMTP:
    """465 端口用 SSL，其他端口（587 等）先明文连接再 STARTTLS。"""
    context = ssl.create_default_context()
    if config.SMTP_PORT == 465:
        return smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=30, context=context)
    smtp = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=30)
    smtp.starttls(context=context)
    return smtp


def _uncertain(e: Exception) -> bool:
    return (isinstance(e, (TimeoutError, ConnectionError, smtplib.SMTPServerDisconnected))
            or isinstance(e, OSError) and not isinstance(e, smtplib.SMTPException))


def _describe(e: Exception, send_started: bool = False) -> str:
    if send_started and _uncertain(e):
        return f"发送结果不确定，邮件可能已发出，请先检查已发送邮件再重试（{type(e).__name__}: {e}）"
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return "邮箱登录失败：请检查 SMTP_USER 和 SMTP_PASSWORD（QQ、163 等邮箱要填“授权码”，不是登录密码）"
    if isinstance(e, smtplib.SMTPDataError) and e.smtp_code == 554:
        # 163 等邮箱的反垃圾检查：内容短、像测试的邮件常被退回（554 DT:SPM）
        return f"邮件被服务器当作垃圾邮件拒收（554）：可以先发给自己的邮箱，或把收件人加进通讯录后再试（{e.smtp_error!r}）"
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        return f"收件人被邮件服务器拒绝：{', '.join(e.recipients)}"
    if isinstance(e, (OSError, smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected)):
        return f"连接邮件服务器失败（{config.SMTP_HOST}:{config.SMTP_PORT}）：{e}"
    return f"发送失败：{type(e).__name__}: {e}"


def build_message(subject: str, body: str, to: list[str], cc: list[str], attachment: tuple[str, str] | None) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((config.MAIL_FROM_NAME or "", config.SMTP_USER))
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Message-ID"] = make_msgid(domain=config.SMTP_USER.split("@")[-1] if "@" in config.SMTP_USER else None)
    msg.set_content(body)
    if attachment:
        name, text = attachment
        msg.add_attachment(text.encode("utf-8"), maintype="text", subtype="markdown", filename=name)
    return msg


def send_weekly(
    weekly, report_id: int, to, cc=None, connect: Callable[[], smtplib.SMTP] = _connect
) -> MailSend:
    """发送周报：正文是去掉出处标记的纯文本，附件是同样内容的 Markdown。connect 可替换，测试时不连真实服务器。"""
    if not configured():
        raise MailNotConfigured("未配置邮箱：请在 .env 中填写 SMTP_HOST、SMTP_USER、SMTP_PASSWORD（授权码）")
    to, cc = parse_addresses(to), parse_addresses(cc)
    if not to:
        raise ValueError("请填写收件人")
    report = weekly.report(report_id)
    filename, text = weekly.export_markdown(report_id)  # 未完成的周报在这里报错
    name = (report.inputs or {}).get("name")
    subject = f"{name + ' ' if name else ''}周报 {report.period_start:%m.%d}–{report.period_end:%m.%d}"
    msg = build_message(subject, text, to, cc, (filename, text))
    record = MailSend(report_id=report_id, to=", ".join(to), cc=", ".join(cc), subject=subject, message_id=msg["Message-ID"])
    send_started = False
    try:
        smtp = connect()
        try:
            smtp.login(config.SMTP_USER, config.SMTP_PASSWORD)
            send_started = True
            smtp.send_message(msg)
        finally:
            try:
                smtp.quit()
            except (smtplib.SMTPException, OSError):
                pass
        record.status = SENT
    except (smtplib.SMTPException, OSError) as e:
        log.warning("周报邮件发送失败：%s", e)
        record.status = UNKNOWN if send_started and _uncertain(e) else FAILED
        record.error = _describe(e, send_started)
    with sessionmaker(weekly.store.engine, expire_on_commit=False)() as s:
        s.add(record)
        s.commit()
    return record


def sends_of(engine, report_id: int) -> list[MailSend]:
    with sessionmaker(engine, expire_on_commit=False)() as s:
        return list(s.scalars(select(MailSend).where(MailSend.report_id == report_id).order_by(MailSend.id.desc())))
