"""外发（邮件、写入飞书）的幂等回执：同一次发送动作只发一次，重复请求返回第一次的结果。

“同一次动作”按内容判断：同一份周报的同样内容发给同样的目标。所以两个标签页、连点、网络重试都不会多发。
用户确认要再发一次时带 resend 和 request_id，才算新的动作。

结果未知不算没发（见 architecture.md 的可靠性章节）：只有明确失败、对方确实没收到的（调用方传 retryable）才允许新的点击直接重试；
其余情况不自动重发，由用户核对后确认。占位在外部调用之前提交，靠主键的唯一约束，多个进程也只有一个能抢到。
"""
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from .db import Base
from .errors import UserInputError

SENDING, DONE, FAILED = "sending", "done", "failed"
WAIT_SECONDS = 45  # 重复请求等第一个发完。比 SMTP 单步超时（30 秒）长，等不到就说明还在发
STALE_SECONDS = 900  # 占位这么久还没结束：进程多半中途退出了，结果算未知


class SendInProgress(UserInputError):
    """同样的内容正在发送，等了一会还没结束。接口返回 409。"""


class OutboundClaim(Base):
    __tablename__ = "outbound_claims"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(10))  # mail / feishu
    report_id: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(10), default=SENDING)
    record_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # mail_sends 或 feishu_exports 的编号
    request_id: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


@dataclass
class Claim:
    key: str
    owned: bool  # True：由这个请求发送；False：返回 record_id 那条的结果
    record_id: int | None = None
    stale: bool = False  # 第一次发送中途断了，结果未知


def fingerprint(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def begin(engine, kind: str, report_id: int, content: str, request_id: str | None, resend: bool) -> Claim:
    """外部调用之前占位。别的请求正在发同样的内容时，等它结束再返回它的结果。"""
    if resend and not request_id:
        raise UserInputError("确认再次发送时需要 request_id")
    key = fingerprint(kind, report_id, content, request_id if resend else "")
    sessions = sessionmaker(engine, expire_on_commit=False)
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        with sessions() as s:
            s.add(OutboundClaim(key=key, kind=kind, report_id=report_id, request_id=request_id or ""))
            try:
                s.commit()
                return Claim(key, True)
            except IntegrityError:
                s.rollback()
            row = s.get(OutboundClaim, key)
            if row.status != SENDING and row.record_id is None:  # 记录是发送前写的，没有记录就是还没发
                if _take_over(s, key, row.status, request_id):
                    return Claim(key, True)
                continue
            if row.status == DONE or row.status == FAILED and request_id and row.request_id == request_id:
                return Claim(key, False, row.record_id)
            if row.status == FAILED and _take_over(s, key, FAILED, request_id):
                return Claim(key, True)
            if row.status == SENDING and (datetime.now() - row.updated_at).total_seconds() > STALE_SECONDS:
                if row.record_id is None:  # 记录是发送前写的，没有记录就是还没发
                    if _take_over(s, key, SENDING, request_id):
                        return Claim(key, True)
                else:
                    s.execute(update(OutboundClaim).where(OutboundClaim.key == key, OutboundClaim.status == SENDING).values(status=DONE))
                    s.commit()
                    return Claim(key, False, row.record_id, stale=True)
        if time.monotonic() > deadline:
            raise SendInProgress("同样的内容正在发送，请稍后查看发送记录，不用重复发送")
        time.sleep(0.1)


def _take_over(session, key: str, status: str, request_id: str | None) -> bool:
    taken = session.execute(update(OutboundClaim).where(OutboundClaim.key == key, OutboundClaim.status == status)
                            .values(status=SENDING, record_id=None, request_id=request_id or "", updated_at=datetime.now()))
    session.commit()
    return taken.rowcount == 1


def attach(engine, key: str, record_id: int) -> None:
    """记录已写入：之后不管发生什么，重复请求都能查到它。"""
    with sessionmaker(engine)() as s:
        s.execute(update(OutboundClaim).where(OutboundClaim.key == key).values(record_id=record_id))
        s.commit()


def finish(engine, key: str, *, retryable: bool = False) -> None:
    """retryable：明确失败、外部确实没有收到，新的点击可以直接重试。"""
    with sessionmaker(engine)() as s:
        s.execute(update(OutboundClaim).where(OutboundClaim.key == key).values(status=FAILED if retryable else DONE))
        s.commit()
