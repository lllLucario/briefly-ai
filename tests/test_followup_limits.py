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


# ---- 85 粘贴和 JSON 请求体
@pytest.fixture
def small_limits(monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 1)
    monkeypatch.setattr(config, "MAX_JSON_MB", 1)  # 实际上限是上传上限的 3 倍：3MB


def test_paste_shares_the_upload_limit(app, store, small_limits):
    with client_of(app) as client:
        response = client.post("/materials", json={"title": "x", "content": "长" * (1024 * 1024 // 3 + 1)})  # 字节超限，字数没超
        assert response.status_code == 413 and "1MB" in response.json()["detail"]
        assert store.list_materials() == []
        assert client.post("/materials", json={"title": "x", "content": "长" * (1024 * 1024 // 3)}).status_code == 201


def test_long_chat_input_cannot_bypass_the_limit_by_being_stored_as_material(app, store, small_limits, monkeypatch):
    monkeypatch.setattr(config, "INPUT_INLINE_TOKENS", 100)
    with client_of(app) as client:
        response = client.post("/chat", json={"text": "长" * (1024 * 1024 // 3 + 1)})
        assert response.status_code == 413 and "1MB" in response.json()["detail"]
        assert store.list_materials() == [] and client.get("/chat/sessions").json() == []
        # 没超限的长输入照旧转存为材料
        assert client.post("/chat", json={"text": "长" * 2000}).status_code == 200
        assert len(store.list_materials()) == 1


def chunked(body: bytes):
    for start in range(0, len(body), 64 * 1024):
        yield body[start:start + 64 * 1024]


def test_json_body_has_a_total_limit(app, small_limits):
    body = b'{"title": "x", "description": "' + b"x" * (3 * 1024 * 1024 + 1) + b'"}'
    with client_of(app) as client:
        for content in (body, chunked(body)):  # 带 Content-Length 和分块传输都要拦住
            response = client.post("/todos", content=content, headers={"content-type": "application/json"})
            assert response.status_code == 413 and "3MB" in response.json()["detail"], response.text
        assert client.get("/todos").json() == []


def test_json_limit_does_not_block_a_paste_within_the_upload_limit(app, small_limits):
    """全是换行的文字转义后变长，也不能被总上限挡住。"""
    with client_of(app) as client:
        response = client.post("/materials", json={"title": "x", "content": "一\n" * (1024 * 1024 // 4)})
    assert response.status_code == 201


def test_unauthenticated_oversized_request_is_401(app, monkeypatch):
    monkeypatch.setattr(config, "ACCESS_TOKEN", "t" * 32)
    monkeypatch.setattr(config, "MAX_JSON_MB", 1)
    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 1)
    with client_of(app) as client:
        response = client.post("/todos", content=b"x" * (4 * 1024 * 1024), headers={"content-type": "application/json"})
    assert response.status_code == 401


# ---- 83 知识库批量上传
def import_form(boundary: str, payloads: list[bytes]) -> bytes:
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="request_key"\r\n\r\nk1\r\n'.encode()]
    for i, payload in enumerate(payloads):
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{i}.txt"\r\n\r\n'.encode() + payload + b"\r\n")
    return b"".join(parts) + f"--{boundary}--\r\n".encode()


@pytest.mark.parametrize("use_chunks", [False, True])
def test_import_batch_over_the_limit_is_413_before_the_form_is_parsed(app, monkeypatch, use_chunks):
    monkeypatch.setattr(config, "MAX_IMPORT_BATCH_MB", 1)
    parsed = []
    original = app.state.knowledge_imports.enqueue
    monkeypatch.setattr(app.state.knowledge_imports, "enqueue", lambda *a, **k: parsed.append(1) or original(*a, **k))
    body = import_form("B", [b"x" * (512 * 1024)] * 3)
    with client_of(app) as client:
        kb = client.post("/knowledge-bases", json={"name": "库"}).json()["id"]
        response = client.post(f"/knowledge-bases/{kb}/imports", content=chunked(body) if use_chunks else body,
                               headers={"content-type": "multipart/form-data; boundary=B"})
    assert response.status_code == 413 and "批次超过 1MB" in response.json()["detail"]
    assert parsed == []


def test_import_batch_within_the_limit_still_reaches_the_endpoint(app, monkeypatch):
    monkeypatch.setattr(config, "MAX_IMPORT_BATCH_MB", 1)
    with client_of(app) as client:
        kb = client.post("/knowledge-bases", json={"name": "库"}).json()["id"]
        response = client.post(f"/knowledge-bases/{kb}/imports", content=import_form("B", [b"x" * 1024] * 2),
                               headers={"content-type": "multipart/form-data; boundary=B"})
    assert response.status_code == 202 and len(response.json()["files"]) == 2


def test_default_import_limit_is_above_the_endpoints_own_batch_limit():
    """接口里的 50MB 限制还在：两个限制不能倒挂，否则页面正常的 50MB 批次会被中间件挡住。"""
    assert config.MAX_IMPORT_BATCH_MB >= 50


def test_declared_oversize_is_rejected_without_reading_the_body(monkeypatch):
    """不读请求体：框架没有机会把它写到临时文件。"""
    import asyncio

    from briefly.uploads import UploadLimitMiddleware

    monkeypatch.setattr(config, "MAX_IMPORT_BATCH_MB", 1)
    calls, sent = [], []

    async def inner(scope, receive, send):
        calls.append("inner")

    async def receive():
        calls.append("receive")
        return dict(type="http.request", body=b"", more_body=False)

    async def send(message):
        sent.append(message)

    scope = dict(type="http", method="POST", path="/knowledge-bases/k/imports",
                 headers=[(b"content-length", str(2 * 1024 * 1024).encode())])
    asyncio.run(UploadLimitMiddleware(inner)(scope, receive, send))
    assert calls == [] and sent[0]["status"] == 413
