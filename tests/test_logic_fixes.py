"""W4-03 逻辑漏洞的回归用例。每条都走真实接口；全部用文件库（内存库的共用连接见问题登记表）。"""
import logging
import socket
import threading

import httpx
import openai
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from sqlalchemy import event

from briefly import config
from briefly.agent import Agent, sqlite_checkpointer
from briefly.api import create_app
from briefly.db import create_db_engine
from briefly.materials import MaterialStore
from fakes import FunctionModel

SECRET = "内部地址 http://10.0.0.8/v1 sk-raw-secret"


@pytest.fixture
def store(tmp_path):
    engine = create_db_engine(f"sqlite:///{(tmp_path / 'app.db').as_posix()}")
    yield MaterialStore(engine)
    engine.dispose()


def client_for(store, respond=None, *, raise_server_exceptions=False, tmp_path=None):
    """respond 模拟模型；抛异常就是接口出错。"""
    model = FunctionModel(respond=respond or (lambda messages: AIMessage("好的")), calls=[])
    saver = sqlite_checkpointer(tmp_path / "chat.db") if tmp_path else None

    def factory(s):
        return Agent(model, tools=[], store=s, checkpointer=saver)

    app = create_app(store, model_factory=lambda: model, agent_factory=factory)
    client = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=raise_server_exceptions)
    client.model = model
    return client


def fail_with(error):
    def respond(messages):
        raise error
    return respond


# ---- 1 报错原文不返回前端
def test_model_error_text_stays_in_the_log(store, caplog):
    with client_for(store, fail_with(RuntimeError(SECRET))) as client, caplog.at_level(logging.ERROR):
        response = client.post("/chat", json={"text": "帮我看看"})
    assert response.status_code == 502
    body = response.json()
    assert "sk-raw-secret" not in response.text and "10.0.0.8" not in response.text
    assert body["code"] == "model_error" and body["thread_id"] and body["request_id"]
    assert "请稍后重试" in body["detail"]
    assert "sk-raw-secret" in caplog.text  # 排查时凭追踪号能在日志里看到原文


def test_failed_session_can_be_reopened_and_continued(store):
    replies = iter([RuntimeError(SECRET), AIMessage("恢复了")])

    def respond(messages):
        value = next(replies)
        if isinstance(value, Exception):
            raise value
        return value

    with client_for(store, respond) as client:
        thread = client.post("/chat", json={"text": "帮我看看"}).json()["thread_id"]
        detail = client.get(f"/chat/sessions/{thread}")
        assert detail.status_code == 200 and "sk-raw-secret" not in detail.text
        assert client.post("/chat", json={"text": "再试试", "thread_id": thread}).json()["reply"] == "恢复了"


def test_feishu_connection_text_is_not_shown(store):
    from briefly.feishu.client import FeishuClient, FeishuError

    def down(request):
        raise httpx.ConnectError(SECRET)

    client = FeishuClient("id", "secret", transport=httpx.MockTransport(down), retries=0)
    with client, pytest.raises(FeishuError) as caught:
        client.token()
    assert "sk-raw-secret" not in str(caught.value) and "无法连接飞书" in str(caught.value)


def generate_failing_week(store, factory):
    with TestClient(create_app(store, model_factory=factory), base_url="http://127.0.0.1", raise_server_exceptions=False) as client:
        report = client.post("/weekly/week", json={}).json()
        assert client.put(f"/weekly/{report['id']}/note", json={"note": "本周完成灰度发布"}).status_code == 200
        assert client.post(f"/weekly/{report['id']}/generate", json={}).status_code == 202
        return client.get(f"/weekly/{report['id']}").json()


def test_weekly_failure_has_no_class_name_or_raw_text(store):
    got = generate_failing_week(store, lambda: FunctionModel(respond=fail_with(RuntimeError(SECRET)), calls=[]))
    assert got["status"] == "failed", got
    assert got["error"] and "RuntimeError" not in got["error"] and "sk-raw-secret" not in got["error"]


# ---- 2 只有用户错误才是 400
def test_internal_value_error_is_500_without_its_text(store, monkeypatch, caplog):
    def broken(self, *args, **kwargs):
        raise ValueError(SECRET)

    monkeypatch.setattr(MaterialStore, "add_text", broken)
    with client_for(store) as client, caplog.at_level(logging.ERROR):
        response = client.post("/materials", json={"title": "x", "content": "正文"})
    assert response.status_code == 500 and response.json()["code"] == "internal_error"
    assert "sk-raw-secret" not in response.text
    assert "sk-raw-secret" in caplog.text


def test_model_parse_failure_in_chat_is_not_a_400_with_model_output(store):
    with client_for(store, fail_with(ValueError(SECRET))) as client:
        response = client.post("/chat", json={"text": "帮我看看"})
    assert response.status_code == 502 and "sk-raw-secret" not in response.text


@pytest.mark.parametrize("method,path,payload,message", [
    ("post", "/chat", {"text": "   "}, "输入不能为空"),
    ("post", "/chat", {"text": "x", "skill": "没有"}, "未知能力"),
    ("post", "/materials", {"title": "", "content": "  "}, "材料内容为空"),
    ("post", "/todos", {"title": " "}, "待办标题不能为空"),
    ("post", "/todos", {"title": "x", "status": "nope"}, "状态只能是"),
])
def test_user_errors_stay_400_with_the_same_text(store, method, path, payload, message):
    with client_for(store) as client:
        response = getattr(client, method)(path, json=payload)
    assert response.status_code == 400 and message in response.json()["detail"]


@pytest.mark.parametrize("smtp_user,message", [("", "未配置邮箱"), ("me@example.com", "邮箱地址格式不对")])
def test_mail_input_errors_stay_400(store, monkeypatch, smtp_user, message):
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(config, "SMTP_USER", smtp_user)
    monkeypatch.setattr(config, "SMTP_PASSWORD", "x")
    with client_for(store) as client:
        response = client.post("/weekly/999/email", json={"to": "not-an-address"})
    assert response.status_code == 400 and message in response.json()["detail"]


def test_unsupported_and_corrupt_uploads_stay_400(store):
    with client_for(store) as client:
        wrong = client.post("/materials/upload", files={"file": ("a.exe", b"x")})
        assert wrong.status_code == 400 and "不支持的文件类型" in wrong.json()["detail"]
        broken = client.post("/materials/upload", files={"file": ("a.docx", b"not a zip")})
        assert broken.status_code == 400 and "文件无法解析" in broken.json()["detail"]


def test_library_value_error_while_parsing_is_still_a_user_error(store, monkeypatch):
    """解析库对坏文件常抛 ValueError，文字是英文内部信息：要转成统一说明，不是 500，也不原样返回。"""
    from briefly import loaders

    def broken(kind, data):
        raise ValueError(SECRET)

    monkeypatch.setattr(loaders, "_load", broken)
    with client_for(store) as client:
        response = client.post("/materials/upload", files={"file": ("a.docx", b"x")})
    assert response.status_code == 400 and "文件无法解析" in response.json()["detail"]
    assert "sk-raw-secret" not in response.text


def test_lone_surrogate_text_is_a_400_not_a_500(store):
    with client_for(store) as client:
        response = client.post("/materials", content='{"title": "x", "content": "\\ud800"}',
                               headers={"content-type": "application/json"})
    assert response.status_code == 400 and "无法识别的字符" in response.json()["detail"]


# ---- 3 上传分块读取
def multipart(filename: str, payload: bytes, boundary: str = "BOUNDARY") -> bytes:
    head = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n").encode()
    return head + payload + f"\r\n--{boundary}--\r\n".encode()


@pytest.mark.parametrize("make_path", [lambda todo: "/materials/upload", lambda todo: f"/todos/{todo}/materials/upload"])
def test_oversized_upload_is_413(store, monkeypatch, make_path):
    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 1)
    with client_for(store) as client:
        todo = client.post("/todos", json={"title": "x"}).json()["id"]
        response = client.post(make_path(todo), files={"file": ("a.txt", b"x" * (1024 * 1024 + 1))})
    assert response.status_code == 413 and "1MB" in response.json()["detail"]
    assert response.json()["request_id"]
    assert store.list_materials() == []


def test_upload_at_the_limit_still_works(store, monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 1)
    with client_for(store) as client:
        response = client.post("/materials/upload", files={"file": ("a.txt", b"x" * (1024 * 1024))})
    assert response.status_code == 201


def test_upload_without_content_length_is_also_limited(store, monkeypatch):
    """分块传输没有 Content-Length：不能只靠请求头。"""
    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 1)

    def chunks():
        body = multipart("a.txt", b"x" * (3 * 1024 * 1024))
        for start in range(0, len(body), 64 * 1024):
            yield body[start:start + 64 * 1024]

    with client_for(store) as client:
        response = client.post("/materials/upload", content=chunks(),
                               headers={"content-type": "multipart/form-data; boundary=BOUNDARY"})
    assert response.status_code == 413
    assert store.list_materials() == []


def test_handler_reads_in_blocks_and_stops_at_the_limit(store, monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 1)
    from fastapi import HTTPException

    from briefly.uploads import read_upload

    class Recording:
        def __init__(self):
            self.sizes = []

        def read(self, size=-1):
            assert size > 0, "不能一次读完"
            self.sizes.append(size)
            return b"x" * size

    source = Recording()
    with pytest.raises(HTTPException) as caught:
        read_upload(type("Upload", (), {"file": source, "size": None})())
    assert caught.value.status_code == 413
    assert sum(source.sizes) <= 1024 * 1024 + 2 * max(source.sizes)  # 不会把后面的内容全读进来


# ---- 4 模型调用超时
def test_model_gets_timeout_and_retry_defaults(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "k")
    monkeypatch.setattr(config, "MODEL", "m")
    monkeypatch.setattr(config, "BASE_URL", "http://127.0.0.1:9/v1")
    from briefly.llm import create_chat_model
    model = create_chat_model()
    assert model.request_timeout == config.MODEL_TIMEOUT_S and model.max_retries == config.MODEL_MAX_RETRIES
    assert model.root_client.timeout == config.MODEL_TIMEOUT_S
    assert model.root_client.max_retries == config.MODEL_MAX_RETRIES
    monkeypatch.setattr(config, "MODEL_TIMEOUT_S", 7.0)
    monkeypatch.setattr(config, "MODEL_MAX_RETRIES", 0)
    model = create_chat_model()
    assert model.root_client.timeout == 7.0 and model.root_client.max_retries == 0
    # 调用点传的值优先
    model = create_chat_model(timeout=3.0, max_retries=1)
    assert model.root_client.timeout == 3.0 and model.root_client.max_retries == 1


@pytest.fixture
def silent_server():
    """接受连接但永远不回应的本地端口：真实的 SDK 超时，不是模拟异常。"""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    connections, stop = [], threading.Event()
    server.settimeout(0.05)

    def accept():
        while not stop.is_set():
            try:
                connections.append(server.accept()[0])
            except TimeoutError:
                continue

    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.getsockname()[1]}/v1"
    stop.set()
    thread.join(2)
    for connection in connections:
        connection.close()
    server.close()


def test_real_sdk_timeout_closes_the_turn_and_reports_timeout(store, tmp_path, monkeypatch, silent_server):
    monkeypatch.setattr(config, "API_KEY", "k")
    monkeypatch.setattr(config, "MODEL", "m")
    monkeypatch.setattr(config, "BASE_URL", silent_server)
    monkeypatch.setattr(config, "MODEL_TIMEOUT_S", 0.3)
    monkeypatch.setattr(config, "MODEL_MAX_RETRIES", 0)
    from briefly.llm import create_chat_model

    saver = sqlite_checkpointer(tmp_path / "chat.db")
    app = create_app(store, agent_factory=lambda s: Agent(create_chat_model(), tools=[], store=s, checkpointer=saver))
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as client:
        response = client.post("/chat", json={"text": "帮我看看"})
        assert response.status_code == 504
        body = response.json()
        assert body["code"] == "model_timeout" and "超时" in body["detail"] and body["thread_id"]
        detail = client.get(f"/chat/sessions/{body['thread_id']}").json()
        assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]  # 轮次已闭合
    saver.conn.close()


def test_timeout_does_not_rerun_tools_with_side_effects(store, tmp_path):
    """工具已经执行、之后的模型调用超时：不能因为超时把工具再跑一遍。"""
    from langchain_core.tools import tool

    runs = []

    @tool
    def send_thing() -> str:
        """假装外发"""
        runs.append(1)
        return "已发送"

    timeout = openai.APITimeoutError(httpx.Request("POST", "http://127.0.0.1/v1/chat/completions"))
    replies = iter([AIMessage("", tool_calls=[dict(name="send_thing", args={}, id="t1", type="tool_call")]), timeout])

    def respond(messages):
        value = next(replies)
        if isinstance(value, Exception):
            raise value
        return value

    model = FunctionModel(respond=respond, calls=[])
    saver = sqlite_checkpointer(tmp_path / "chat.db")
    app = create_app(store, agent_factory=lambda s: Agent(model, tools=[send_thing], store=s, checkpointer=saver))
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as client:
        response = client.post("/chat", json={"text": "发出去"})
        assert response.status_code == 504
        assert len(runs) == 1 and len(model.calls) == 2  # 模型没有被应用层自动重发
    saver.conn.close()


def test_shortcut_failure_keeps_the_question_in_the_session(store, tmp_path):
    """单问捷径在写入历史之前调用模型：失败或超时后重新打开会话，要能看到问题和失败说明。"""
    from types import SimpleNamespace

    from briefly.intent import IntentDecision
    from briefly.knowledge.service import KnowledgeNotFound

    timeout = openai.APITimeoutError(httpx.Request("POST", "http://127.0.0.1/v1/chat/completions"))
    saver = sqlite_checkpointer(tmp_path / "chat.db")
    model = FunctionModel(respond=lambda messages: AIMessage("好的"), calls=[])
    app = create_app(store, model_factory=lambda: model, agent_factory=lambda s: Agent(model, tools=[], store=s, checkpointer=saver))
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as client:
        client.post("/chat", json={"text": "先建好智能体"})
        agent = app.state.agent
        agent.funnel = SimpleNamespace(classify=lambda *a, **k: IntentDecision("knowledge", 2, "测试"))

        def answer(kb_id, question):
            raise timeout

        agent.knowledge = SimpleNamespace(answer=answer, base_name=lambda kb_id: "制度库")
        response = client.post("/chat", json={"text": "年假几天", "kb_id": "k1"})
        assert response.status_code == 504
        messages = client.get(f"/chat/sessions/{response.json()['thread_id']}").json()["messages"]
        assert [(m["role"], m["text"]) for m in messages][0] == ("user", "年假几天")
        assert messages[1]["role"] == "assistant" and messages[1]["system_reply"] is True

        def missing(kb_id, question):
            raise KnowledgeNotFound("知识库不存在")

        agent.knowledge = SimpleNamespace(answer=missing, base_name=lambda kb_id: "制度库")
        response = client.post("/chat", json={"text": "年假几天", "kb_id": "k2"})
        assert response.status_code == 404  # 用户错误照旧，不留下会话
        assert len(client.get("/chat/sessions").json()) == 2
    saver.conn.close()


# ---- 5 会话锁
def test_session_locks_are_released_after_use():
    from briefly.keyed_locks import KeyedLocks

    locks = KeyedLocks()
    for key in range(1000):
        with locks[str(key)]:
            pass
    assert len(locks) == 0


def test_same_key_is_serial_and_lock_survives_while_waited_on():
    from briefly.keyed_locks import KeyedLocks

    locks = KeyedLocks()
    inside, peak = 0, 0
    guard, barrier = threading.Lock(), threading.Barrier(8)

    def work():
        nonlocal inside, peak
        barrier.wait()
        for _ in range(200):
            with locks["a"]:
                with guard:
                    inside += 1
                    peak = max(peak, inside)
                with guard:
                    inside -= 1

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert peak == 1 and len(locks) == 0


def test_lock_is_released_when_the_body_raises():
    from briefly.keyed_locks import KeyedLocks

    locks = KeyedLocks()
    with pytest.raises(RuntimeError):
        with locks["a"]:
            raise RuntimeError
    assert len(locks) == 0
    assert locks["a"].__enter__() is not None and len(locks) == 1
    locks["a"].__exit__(None, None, None)
    assert len(locks) == 0


def test_chat_does_not_keep_a_lock_per_session(store):
    with client_for(store) as client:
        for _ in range(5):
            assert client.post("/chat", json={"text": "帮我看看"}).status_code == 200
        assert len(client.app.state.thread_locks) == 0


def test_same_session_requests_stay_serial_over_http(store):
    inside, peak = 0, 0
    guard = threading.Lock()

    def respond(messages):
        nonlocal inside, peak
        with guard:
            inside += 1
            peak = max(peak, inside)
        threading.Event().wait(0.05)
        with guard:
            inside -= 1
        return AIMessage("好的")

    with client_for(store, respond) as client:
        thread = client.post("/chat", json={"text": "第一句"}).json()["thread_id"]
        peak = 0
        results = []
        workers = [threading.Thread(target=lambda: results.append(client.post("/chat", json={"text": f"第{i}句", "thread_id": thread}).status_code))
                   for i in range(4)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(30)
    assert results == [200] * 4 and peak == 1


# ---- 6 异常目录
def test_percent_encoded_url_does_not_create_stray_directories(tmp_path, monkeypatch):
    """str(engine.url) 把路径里的特殊字符编码了（Windows 的盘符冒号就是一例）：
    建目录要用解析后的路径，否则会在当前目录或旁边多出一棵 C%3A/... 的空目录，真正的目录反而没有建。"""
    from sqlalchemy.engine import make_url

    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    target = tmp_path / "数据#1" / "deep" / "app.db"
    encoded = str(make_url(f"sqlite:///{target.as_posix()}"))
    assert "%" in encoded
    engine = create_db_engine(encoded)
    engine.dispose()
    assert target.is_file()
    assert list(cwd.iterdir()) == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cwd", "数据#1"]


# ---- 56 并发提交相同内容
def test_concurrent_same_material_returns_the_existing_one(store):
    """在 SELECT 没查到、INSERT 之前，另一个连接写入同样的内容：和串行时的结果一样，不是 500。"""
    other = create_db_engine(str(store.engine.url))
    raced = []

    def race(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO materials") and not raced:
            raced.append(1)
            MaterialStore(other).add_text("别的标签页", "相同内容")

    event.listen(store.engine, "before_cursor_execute", race)
    try:
        with client_for(store) as client:
            response = client.post("/materials", json={"title": "这个标签页", "content": "相同内容"})
    finally:
        event.remove(store.engine, "before_cursor_execute", race)
        other.dispose()
    assert raced and response.status_code == 200, response.text
    assert response.json()["created"] is False
    assert response.json()["material"]["title"] == "别的标签页"
    assert len(store.list_materials()) == 1


def test_real_threads_posting_the_same_material_never_500(store):
    barrier, results = threading.Barrier(6), []

    with client_for(store) as client:
        def post():
            barrier.wait()
            results.append(client.post("/materials", json={"title": "x", "content": "并发的内容"}).status_code)

        workers = [threading.Thread(target=post) for _ in range(6)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(30)
    assert sorted(results)[0] in (200, 201) and all(code in (200, 201) for code in results), results
    assert results.count(201) == 1 and len(store.list_materials()) == 1


# ---- 57 待办标题长度
@pytest.mark.parametrize("size,code", [(200, 201), (201, 422)])
def test_todo_title_limit_on_create(store, size, code):
    with client_for(store) as client:
        response = client.post("/todos", json={"title": "长" * size})
        assert response.status_code == code
        if code == 201:
            assert response.json()["title"] == "长" * size
        else:
            assert client.get("/todos").json() == []
            assert "200" in response.json()["detail"]


def test_todo_title_limit_on_update_and_import_is_not_silent(store):
    with client_for(store) as client:
        todo = client.post("/todos", json={"title": "原标题"}).json()
        response = client.put(f"/todos/{todo['id']}", json={"title": "长" * 201})
        assert response.status_code == 422
        assert client.get(f"/todos/{todo['id']}").json()["title"] == "原标题"


# ---- 58 校验错误
def test_validation_error_is_chinese_without_echoing_input(store):
    with client_for(store) as client:
        response = client.post("/todos", json={"title": "x", "due_date": "保密内容不是日期"})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error" and body["request_id"]
    assert "保密内容" not in response.text and "input" not in body
    assert isinstance(body["detail"], str) and "due_date" in body["detail"] and "日期" in body["detail"]
    assert body["errors"] == [{"field": "due_date", "message": "不是有效的日期"}]


@pytest.mark.parametrize("request_args,field,word", [
    (dict(method="POST", url="/materials", json={"title": "x"}), "content", "缺少"),
    (dict(method="GET", url="/materials/abc"), "material_id", "整数"),
    (dict(method="GET", url="/materials/library", params={"q": "长" * 201}), "q", "200"),
    (dict(method="POST", url="/materials", content="{不是json", headers={"content-type": "application/json"}), "", "JSON"),
])
def test_validation_error_kinds(store, request_args, field, word):
    with client_for(store) as client:
        response = client.request(**request_args)
    assert response.status_code == 422, response.text
    body = response.json()
    assert word in body["detail"] and "{不是json" not in response.text
    assert body["errors"][0]["field"] == field


def test_extra_fields_of_a_framework_error_are_not_exposed(store):
    """pydantic 的 ctx 里可能带原始异常文字（如 value_error）：一律不透传。"""
    with client_for(store) as client:
        response = client.post("/weekly/week", json={"day": "不是日期"})
    assert response.status_code == 422 and "不是日期" not in response.text and "ctx" not in response.text


# ---- 59 模型未配置
def test_model_not_configured_has_a_code(store, monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "")
    monkeypatch.setattr(config, "MODEL", "")
    with TestClient(create_app(store), base_url="http://127.0.0.1", raise_server_exceptions=False) as client:
        response = client.post("/chat", json={"text": "你好吗，帮我看看"})
    assert response.status_code == 503 and response.json()["code"] == "model_not_configured"
    assert "模型配置不完整" in response.json()["detail"]


def test_todo_preview_reports_missing_model_config_not_502(store, tmp_path, monkeypatch):
    from briefly.llm import ModelConfigurationError

    def missing():
        raise ModelConfigurationError("模型配置不完整")

    model = FunctionModel(respond=lambda m: AIMessage("【待办清单】\n|任务|负责人|截止时间|\n|---|---|---|\n|写文档|李强|待定|"), calls=[])
    saver = sqlite_checkpointer(tmp_path / "chat.db")
    app = create_app(store, model_factory=missing, agent_factory=lambda s: Agent(model, tools=[], store=s, checkpointer=saver))
    with TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False) as client:
        thread = client.post("/chat", json={"text": "李强负责写文档，帮我列待办"}).json()["thread_id"]
        message = [m for m in client.get(f"/chat/sessions/{thread}").json()["messages"] if m["role"] == "assistant"][-1]
        response = client.post(f"/chat/sessions/{thread}/todo-preview", json={"message_id": message["message_id"]})
    assert response.status_code == 503 and response.json()["code"] == "model_not_configured"
    saver.conn.close()


def test_weekly_failure_keeps_the_config_explanation(store):
    from briefly.llm import ModelConfigurationError

    def missing():
        raise ModelConfigurationError("模型配置不完整：缺少 BRIEFLY_MODEL")

    got = generate_failing_week(store, missing)
    assert got["status"] == "failed" and got["error"].startswith("模型配置不完整")
    assert "ModelConfigurationError" not in got["error"]
