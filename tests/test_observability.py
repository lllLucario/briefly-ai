"""W4-02：结构化日志、请求追踪号、统一错误格式、健康检查。"""
import io
import json
import logging
import re
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from briefly import config, llm
from briefly.api import create_app
from briefly.db import create_db_engine
from briefly.logs import INTERNAL_ERROR, JsonFormatter, configure_logging
from briefly.materials import MaterialStore
from fakes import FunctionModel

SECRET_TEXT = "这是不该外泄的异常原文"


@pytest.fixture
def app():
    engine = create_db_engine("sqlite://")
    app = create_app(MaterialStore(engine), model_factory=lambda: FunctionModel(respond=None, calls=[]))
    route_log = logging.getLogger("briefly.test_route")

    @app.get("/test/boom")
    def boom():
        route_log.warning("路由内日志")
        raise RuntimeError(SECRET_TEXT)

    @app.get("/test/log")
    async def async_log():
        route_log.warning("异步路由内日志")
        return {"ok": True}

    @app.get("/test/sync-log")
    def sync_log():  # 同步路由经线程池执行，上下文要传进去
        route_log.warning("同步路由内日志")
        return {"ok": True}

    @app.get("/test/http-error")
    def http_error():
        raise HTTPException(status_code=418, detail="给用户看的说明")

    yield app
    engine.dispose()


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def json_logs():
    """按正式格式收集 briefly 下的日志；logs() 返回解析后的记录。"""
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("briefly")
    level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    yield lambda: [json.loads(line) for line in buffer.getvalue().splitlines()]
    logger.removeHandler(handler)
    logger.setLevel(level)


# ---- 追踪号
def test_generated_request_id_on_success_and_error(client):
    ok = client.get("/healthz")
    assert re.fullmatch(r"[0-9a-f]{16}", ok.headers["x-request-id"])
    missing = client.get("/materials/999999")
    assert missing.status_code == 404
    assert missing.json()["request_id"] == missing.headers["x-request-id"]
    assert missing.json()["detail"]


def test_valid_incoming_request_id_is_kept(client):
    response = client.get("/materials/999999", headers={"X-Request-ID": "abc-123_X.y"})
    assert response.headers["x-request-id"] == "abc-123_X.y"
    assert response.json()["request_id"] == "abc-123_X.y"


@pytest.mark.parametrize("bad", ["带中文".encode(), "a" * 65, "a b", "a\tb", "<script>"])
def test_invalid_incoming_request_id_is_replaced(client, bad):
    response = client.get("/healthz", headers={"X-Request-ID": bad})
    assert response.headers["x-request-id"] != bad
    assert re.fullmatch(r"[0-9a-f]{16}", response.headers["x-request-id"])


@pytest.mark.parametrize("path, method, kwargs, status", [
    ("/test/http-error", "get", {}, 418),  # HTTPException
    ("/materials", "post", {"json": {"title": 1}}, 422),  # 请求校验
    ("/no-such-route", "get", {}, 404),  # 框架默认
    ("/healthz", "post", {}, 405),
    ("/materials", "post", {"json": {"title": "", "content": ""}}, 400),  # ValueError 处理器
    ("/todos/999999", "get", {}, 404),  # 其他模块注册的处理器
])
def test_all_error_responses_carry_request_id(client, path, method, kwargs, status):
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == status
    body = response.json()
    assert body["request_id"] == response.headers["x-request-id"]
    assert "detail" in body
    assert response.headers["content-length"] == str(len(response.content))


def test_existing_error_fields_are_kept(app, client):
    from briefly.todos.api import ConfirmationRequired

    @app.get("/test/confirm")
    def confirm():
        raise ConfirmationRequired("需要确认")

    response = client.get("/test/confirm")
    assert response.status_code == 409
    assert response.json()["confirm"] is True and response.json()["detail"] == "需要确认"
    assert response.json()["request_id"]


def test_success_body_is_untouched(client):
    assert client.get("/healthz").json() == {"status": "ok"}


# ---- 未处理的异常
def test_unhandled_exception_returns_generic_500_and_logs_stack(client, json_logs):
    response = client.get("/test/boom")
    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == INTERNAL_ERROR and body["code"] == "internal_error"
    assert SECRET_TEXT not in response.text and "RuntimeError" not in response.text
    request_id = response.headers["x-request-id"]
    assert body["request_id"] == request_id
    records = [r for r in json_logs() if r["request_id"] == request_id]
    failed = next(r for r in records if "exception" in r)
    assert failed["level"] == "ERROR" and SECRET_TEXT in failed["exception"] and "Traceback" in failed["exception"]
    assert any(r["message"] == "路由内日志" for r in records)
    access = next(r for r in records if r["module"] == "briefly.access")
    assert (access["path"], access["status"]) == ("/test/boom", 500)


def test_exception_after_response_started_is_not_swallowed():
    from briefly.logs import RequestContextMiddleware

    async def broken(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("中途失败")

    with pytest.raises(RuntimeError):
        TestClient(RequestContextMiddleware(broken)).get("/")


# ---- 日志带追踪号
@pytest.mark.parametrize("path", ["/test/log", "/test/sync-log"])
def test_route_logs_carry_request_id_without_mixing(client, json_logs, path):
    first = client.get(path, headers={"X-Request-ID": "first"})
    second = client.get(path, headers={"X-Request-ID": "second"})
    assert first.status_code == second.status_code == 200
    routed = [r["request_id"] for r in json_logs() if r["module"] == "briefly.test_route"]
    assert routed == ["first", "second"]
    logging.getLogger("briefly.test_route").warning("请求之外")
    assert json_logs()[-1]["request_id"] is None


def test_access_log_has_no_query_string(client, json_logs):
    client.get("/feishu/oauth/callback?code=secret-code&state=secret-state", follow_redirects=False)
    client.get("/materials/library?q=敏感搜索词")
    text = json.dumps(json_logs(), ensure_ascii=False)
    assert "secret-code" not in text and "secret-state" not in text and "敏感搜索词" not in text


def test_formatter_is_one_json_line_and_masks_secrets(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "sk-very-secret")
    logger = logging.getLogger("briefly.mask")
    try:
        raise ValueError("第一行\n用了 sk-very-secret")
    except ValueError:
        record = logger.makeRecord(logger.name, logging.ERROR, __file__, 1, "Authorization: Bearer abc.def %s", ("sk-very-secret",), sys.exc_info())
    line = JsonFormatter().format(record)
    assert "\n" not in line
    assert "sk-very-secret" not in line and "abc.def" not in line
    data = json.loads(line)
    assert data["module"] == "briefly.mask" and data["level"] == "ERROR" and "time" in data and "exception" in data


def test_database_errors_do_not_carry_sql_parameters():
    """数据库异常的文字可能进日志，不能带材料正文。"""
    engine = create_db_engine("sqlite://")
    with engine.connect() as connection, pytest.raises(Exception) as caught:
        connection.exec_driver_sql("INSERT INTO no_such_table VALUES (?)", ("敏感正文",))
    assert "敏感正文" not in str(caught.value)
    engine.dispose()


# ---- 日志配置
def test_configure_logging_is_idempotent_and_leaves_other_handlers(tmp_path, capsys):
    root = logging.getLogger()
    other = logging.NullHandler()
    root.addHandler(other)
    log_file = tmp_path / "logs" / "app.log"
    try:
        configure_logging(level="INFO", log_file=str(log_file))
        configure_logging(level="INFO", log_file=str(log_file))
        ours = [h for h in root.handlers if getattr(h, "_briefly", False)]
        assert len(ours) == 2 and other in root.handlers
        logging.getLogger("briefly.x").info("写入一条")
        assert json.loads(capsys.readouterr().out)["message"] == "写入一条"
        assert json.loads(log_file.read_text(encoding="utf-8"))["message"] == "写入一条"
    finally:
        root.removeHandler(other)


def test_configure_logging_keeps_third_party_quiet(capsys):
    configure_logging(level="DEBUG", log_file="")
    logging.getLogger("httpx").info("HTTP Request: POST https://x/y?token=secret")
    logging.getLogger("openai._base_client").debug("请求正文")
    logging.getLogger("briefly.x").debug("本项目")
    out = capsys.readouterr().out
    assert "secret" not in out and "请求正文" not in out and "本项目" in out


def test_configure_logging_rejects_unknown_level():
    with pytest.raises(ValueError, match="BRIEFLY_LOG_LEVEL"):
        configure_logging(level="LOUD", log_file="")


def test_console_handler_survives_unencodable_text():
    from briefly.logs import _ConsoleHandler

    stream = io.TextIOWrapper(io.BytesIO(), encoding="gbk")
    sys_stdout, sys.stdout = sys.stdout, stream
    try:
        handler = _ConsoleHandler("stdout")
        handler.setFormatter(JsonFormatter())
        handler.emit(logging.makeLogRecord({"name": "briefly.x", "levelname": "INFO", "msg": "中文😀"}))
        stream.flush()
    finally:
        sys.stdout = sys_stdout
    assert "中文" in stream.buffer.getvalue().decode("gbk")


def test_create_app_does_not_configure_logging():
    root = logging.getLogger()
    before = list(root.handlers), root.level
    engine = create_db_engine("sqlite://")
    create_app(MaterialStore(engine))
    assert (list(root.handlers), root.level) == before
    engine.dispose()


def test_caplog_still_works_with_request_context(client, caplog):
    with caplog.at_level(logging.WARNING):
        client.get("/test/log")
    assert "异步路由内日志" in caplog.text


# ---- 健康检查
def real_model_app(monkeypatch, store=None):
    """用默认模型工厂；ChatOpenAI 一旦被创建就失败，证明健康检查没有碰模型。"""
    def forbidden(*args, **kwargs):
        raise AssertionError("健康检查不能创建或调用模型")

    monkeypatch.setattr(llm, "ChatOpenAI", forbidden)
    return create_app(store or MaterialStore(create_db_engine("sqlite://")))


def test_healthz_checks_nothing(client, app):
    app.state.store = SimpleNamespace(engine=None)
    assert client.get("/healthz").status_code == 200


def test_readyz_ok_without_calling_model(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "sk-value-should-not-appear")
    monkeypatch.setattr(config, "MODEL", "model-value-should-not-appear")
    app = real_model_app(monkeypatch)
    response = TestClient(app).get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"database": True, "model": True}}
    app.state.store.engine.dispose()


def test_readyz_503_when_model_not_configured(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "")
    monkeypatch.setattr(config, "MODEL", "model-value-should-not-appear")
    app = real_model_app(monkeypatch)
    response = TestClient(app).get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["checks"] == {"database": True, "model": False}
    assert "模型配置" in body["detail"] and body["request_id"]
    assert "model-value-should-not-appear" not in response.text
    app.state.store.engine.dispose()


def test_readyz_503_when_database_unavailable(monkeypatch, tmp_path, json_logs):
    monkeypatch.setattr(config, "API_KEY", "sk-value-should-not-appear")
    monkeypatch.setattr(config, "MODEL", "m")
    app = real_model_app(monkeypatch)
    memory = app.state.store.engine
    secret_url = f"sqlite:///{(tmp_path / 'no-such-dir' / 'secret-name.db').as_posix()}"
    broken = create_engine(secret_url)
    app.state.store = SimpleNamespace(engine=broken)
    response = TestClient(app).get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"] == {"database": False, "model": True}
    assert "业务数据库" in response.json()["detail"]
    assert "secret-name" not in response.text and "sk-value" not in response.text
    assert any("exception" in r and r["request_id"] == response.headers["x-request-id"] for r in json_logs())
    broken.dispose()
    memory.dispose()


def test_health_routes_are_not_in_docs(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "/healthz" not in paths and "/readyz" not in paths
