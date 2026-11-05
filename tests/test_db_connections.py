"""文件库每个线程一条连接：后台线程结束会话，不能影响别的线程还没提交的写入。"""
import threading

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from briefly.db import create_db_engine
from briefly.materials import Material


def test_background_session_cannot_discard_another_threads_uncommitted_write(tmp_path):
    engine = create_db_engine(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")

    def background_read():
        with Session(engine) as s:
            s.scalar(select(func.count()).select_from(Material))

    with Session(engine) as s:
        s.add(Material(title="扫描中写入", source="paste", content="正文", char_count=2, content_hash="digest"))
        s.flush()
        worker = threading.Thread(target=background_read)
        worker.start()
        worker.join(10)
        assert not worker.is_alive()
        s.commit()

    with Session(engine) as s:
        assert s.scalar(select(func.count()).select_from(Material)) == 1
    engine.dispose()
