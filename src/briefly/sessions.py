"""对话会话清单（业务库）：网页的会话列表用。对话内容本身在 checkpointer 里，这里只存编号、标题和时间。"""
from datetime import datetime

from sqlalchemy import DateTime, Engine, String, select
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from .db import Base
from .materials import MaterialNotFound

TITLE_CHARS = 30


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    thread_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class SessionNotFound(MaterialNotFound):
    """会话不存在。复用 MaterialNotFound，接口层统一返回 404。"""


def make_title(text: str) -> str:
    """会话标题取第一句提问的开头。"""
    line = " ".join(text.split())
    return line[:TITLE_CHARS] + ("…" if len(line) > TITLE_CHARS else "") or "新会话"


class SessionStore:
    def __init__(self, engine: Engine) -> None:
        self._session = sessionmaker(engine, expire_on_commit=False)

    def list_sessions(self) -> list[ChatSession]:
        with self._session() as s:
            return list(s.scalars(select(ChatSession).order_by(ChatSession.updated_at.desc())))

    def get(self, thread_id: str) -> ChatSession:
        with self._session() as s:
            session = s.get(ChatSession, thread_id)
        if session is None:
            raise SessionNotFound(f"会话 {thread_id} 不存在")
        return session

    def touch(self, thread_id: str, first_text: str) -> ChatSession:
        """记录一次对话：新会话按第一句提问建标题，已有会话只更新时间。"""
        with self._session() as s:
            session = s.get(ChatSession, thread_id)
            if session is None:
                session = ChatSession(thread_id=thread_id, title=make_title(first_text))
                s.add(session)
            session.updated_at = datetime.now()
            s.commit()
            return session

    def delete(self, thread_id: str) -> None:
        with self._session() as s:
            session = s.get(ChatSession, thread_id)
            if session is None:
                raise SessionNotFound(f"会话 {thread_id} 不存在")
            s.delete(session)
            s.commit()
