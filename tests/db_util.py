"""接上后台线程的用例用的数据库。

内存库（sqlite://）所有线程共用一条连接：create_app 接上的工作区索引、定时任务等后台线程结束会话时的回滚，
会撤销测试线程还没提交的写入，用例偶发撞外键或丢数据。文件库和正式部署一样每个线程一条连接，没有这个问题。"""
import shutil
from itertools import count
from pathlib import Path

from briefly.db import create_db_engine

_directory: Path | None = None
_factory = None
_template: Path | None = None
_engines: list = []
_numbers = count(1)


def start_test(directory: Path, factory) -> None:
    """conftest 在每个用例开始时调用：引擎建在这个用例的临时目录里。"""
    global _directory, _factory
    _directory, _factory = directory, factory


def finish_test() -> None:
    while _engines:
        _engines.pop().dispose()


def file_engine():
    """用例结束时自动释放；不需要传临时目录，所以普通辅助函数里也能用。
    建表逐条落盘很慢，所以只建一次模板，之后复制；create_db_engine 看到表已存在就不再建。"""
    global _template
    if _template is None:
        template = _factory.mktemp("db-template") / "template.db"
        create_db_engine(f"sqlite:///{template.as_posix()}").dispose()
        _template = template
    path = _directory / f"app-{next(_numbers)}.db"
    shutil.copyfile(_template, path)
    engine = create_db_engine(f"sqlite:///{path.as_posix()}")
    _engines.append(engine)
    return engine
