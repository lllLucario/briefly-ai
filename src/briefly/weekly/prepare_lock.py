"""按周的跨进程整理租约；业务事务写入前同时核对租约身份。"""
import logging
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from .. import config
from ..tracking.models import ScanLock

log = logging.getLogger(__name__)


class PrepareBusy(ValueError):
    pass


class PrepareLease:
    def __init__(self, engine, report_id):
        self.factory = sessionmaker(engine, expire_on_commit=False)
        self.name, self.owner = f"prepare:{report_id}", uuid.uuid4().hex
        self.stop = threading.Event()

    def __enter__(self):
        now = datetime.now()
        with self.factory() as s:
            s.execute(delete(ScanLock).where(ScanLock.name == self.name, ScanLock.expires_at < now))
            s.add(ScanLock(name=self.name, owner=self.owner, expires_at=now + timedelta(seconds=config.TRACKING_LOCK_SECONDS)))
            try:
                s.commit()
            except IntegrityError as exc:
                raise PrepareBusy("这一周正在整理，请稍后再试") from exc
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)
        self.thread.start()
        return self

    @contextmanager
    def session(self):
        with self.factory() as s:
            now = datetime.now()
            owned = s.execute(update(ScanLock).where(
                ScanLock.name == self.name, ScanLock.owner == self.owner, ScanLock.expires_at >= now,
            ).values(expires_at=now + timedelta(seconds=config.TRACKING_LOCK_SECONDS)))
            if owned.rowcount != 1:
                raise PrepareBusy("整理租约已失效，本次已停止写入，请重新整理")
            yield s

    def _heartbeat(self):
        while not self.stop.wait(min(60, max(config.TRACKING_LOCK_SECONDS / 3, 0.01))):
            try:
                with self.session() as s:
                    s.commit()
            except PrepareBusy:
                return
            except Exception:
                log.exception("整理租约续期失败，稍后重试")

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join()
        with self.factory() as s:
            s.execute(delete(ScanLock).where(ScanLock.name == self.name, ScanLock.owner == self.owner))
            s.commit()
