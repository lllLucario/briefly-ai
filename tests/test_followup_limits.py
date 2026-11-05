"""登记表第 84、85、83 条：内部锁名不和用户传的值相撞；粘贴、JSON 请求体和知识库批量上传的大小上限。全部走真实接口和文件库。"""
import threading

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from briefly import config
from briefly.agent import Agent, sqlite_checkpointer
from briefly.api import create_app
from briefly.db import create_db_engine
from briefly.materials import MaterialStore
from fakes import FunctionModel


@pytest.fixture
def store(tmp_path):
    engine = create_db_engine(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    yield MaterialStore(engine)
    engine.dispose()


@pytest.fixture
def app(store, tmp_path):
    model = FunctionModel(respond=lambda messages: AIMessage("好的"), calls=[])
    saver = sqlite_checkpointer(tmp_path / "chat.db")
    yield create_app(store, model_factory=lambda: model, agent_factory=lambda s: Agent(model, tools=[], store=s, checkpointer=saver))
    saver.conn.close()


def client_of(app):
    return TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)


# ---- 84 内部锁名
def post_with_timeout(client, path, payload, seconds=5.0):
    """请求卡住时不拖住整个测试进程：放在守护线程里，到时间还没回来就算卡住。"""
    box = {}
    worker = threading.Thread(target=lambda: box.update(response=client.post(path, json=payload)), daemon=True)
    worker.start()
    worker.join(seconds)
    assert not worker.is_alive(), f"{path} 卡住了（{seconds} 秒没有返回）"
    return box["response"]


@pytest.mark.parametrize("thread_id", ["weekly-input-commit", "weekly-input"])
def test_thread_id_equal_to_an_internal_lock_name_does_not_deadlock(app, thread_id):
    client = client_of(app)  # 不进 lifespan：修复前这个请求永远不返回，不能让收尾跟着卡住
    transfer = dict(thread_id=thread_id, message_id="x", request_id=f"r-{thread_id}", revision="a",
                    target_revision="b", day="2026-11-05", note="x", material_ids=[])
    assert post_with_timeout(client, "/weekly/input-transfer", transfer).status_code == 404  # 这个会话不存在
    # 锁已释放，不带会话的交接还能走
    preview = post_with_timeout(client, "/weekly/input-preview", dict(day="2026-11-05", material_ids=[]))
    assert preview.status_code == 200
    assert len(app.state.thread_locks) == 0
