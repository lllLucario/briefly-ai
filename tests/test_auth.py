"""W4-01：默认拒绝的访问认证。"""
import re
import time
from pathlib import Path

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from starlette.routing import Mount

from briefly import auth, config
from briefly.api import create_app, serve
from briefly.auth import COOKIE_NAME, PUBLIC_PATHS, session_value
from briefly.db import create_db_engine
from briefly.feishu.oauth import UserTokens
from briefly.materials import MaterialStore

TOKEN = "correct-horse-battery-staple"


@pytest.fixture
def app(tmp_path):
    engine = create_db_engine("sqlite://")
    app = create_app(MaterialStore(engine), feishu_tokens=UserTokens(tmp_path / "t.json"))
    yield app
    engine.dispose()


@pytest.fixture
def secured(monkeypatch):
    monkeypatch.setattr(config, "ACCESS_TOKEN", TOKEN)


@pytest.fixture
def client(app, secured):
    return TestClient(app, follow_redirects=False)


def bearer(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path)


# ---- 默认拒绝
def test_every_route_is_rejected_without_login_except_whitelist(app, client):
    """防止以后新增路由时漏保护：遍历全部路由。"""
    checked = 0
    for route in app.routes:
        if isinstance(route, Mount):
            targets = [(f"{route.path}/app.js", "GET"), (f"{route.path}/index.html", "GET")]
        else:
            targets = [(concrete(route.path), m) for m in route.methods]
        for path, method in targets:
            if path in PUBLIC_PATHS:
                continue
            response = client.request(method, path)
            assert response.status_code == 401, (method, path, response.status_code)
            if method != "HEAD":  # HEAD 没有响应体
                assert response.json()["code"] == auth.AUTH_REQUIRED
                assert response.json()["request_id"] == response.headers["x-request-id"]
            html = client.request(method, path, headers={"Accept": "text/html"})
            if method in ("GET", "HEAD"):
                assert (html.status_code, html.headers["location"]) == (303, "/login"), path
            else:
                assert html.status_code == 401, (method, path)
            checked += 1
    assert checked > 100


def test_whitelist_is_exactly_what_was_decided():
    assert PUBLIC_PATHS == {"/healthz", "/login", "/auth/login", "/feishu/oauth/callback", "/web/login.js", "/web/style.css"}


@pytest.mark.parametrize("path", ["/", "/docs", "/redoc", "/openapi.json", "/web/app.js", "/web/index.html", "/web/knowledge.js",
                                  "/web/no-such-file", "/no-such-route", "/readyz", "/feishu/oauth/login",
                                  "/web/style.css/../app.js", "/web/%2e%2e/api.py", "/healthz/", "/LOGIN"])
def test_protected_paths_rejected(client, path):
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Accept": "text/html"}).status_code == 303


@pytest.mark.parametrize("path, status", [("/healthz", 200), ("/login", 200), ("/web/login.js", 200), ("/web/style.css", 200)])
def test_public_paths_reachable(client, path, status):
    assert client.get(path).status_code == status


# ---- 凭据
def test_bearer_correct_wrong_missing(client):
    assert client.get("/materials", headers=bearer()).status_code == 200
    assert client.get("/materials", headers=bearer("wrong")).status_code == 401
    assert client.get("/materials", headers=bearer("")).status_code == 401
    assert client.get("/materials", headers={"Authorization": f"Basic {TOKEN}"}).status_code == 401
    assert client.get("/materials", headers={"Authorization": "Bearer 令牌".encode()}).status_code == 401
    assert client.get("/materials").status_code == 401


def test_bearer_does_not_fall_back_to_valid_cookie(client):
    client.cookies.set(COOKIE_NAME, session_value(TOKEN, int(time.time()) + 60))
    assert client.get("/materials", headers=bearer("wrong")).status_code == 401
    assert client.get("/materials").status_code == 200


def test_login_sets_protected_cookie_then_logout_clears(client):
    response = client.post("/auth/login", json={"token": TOKEN})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "max-age=604800" in cookie and "secure" not in cookie
    assert TOKEN not in response.headers["set-cookie"]
    assert client.get("/materials").status_code == 200
    assert client.get("/login").status_code == 303  # 已登录不再显示登录页
    assert client.post("/auth/logout").status_code == 200
    assert client.get("/materials").status_code == 401


def test_https_login_cookie_is_secure(app, secured):
    https = TestClient(app, base_url="https://testserver")
    assert "secure" in https.post("/auth/login", json={"token": TOKEN}).headers["set-cookie"].lower()


def test_wrong_login_does_not_set_cookie(client):
    response = client.post("/auth/login", json={"token": "wrong"})
    assert response.status_code == 401 and response.json()["code"] == "invalid_token"
    assert response.json()["request_id"] and "set-cookie" not in response.headers


@pytest.mark.parametrize("make", [
    lambda: session_value(TOKEN, int(time.time()) - 1),  # 过期
    lambda: session_value("old-token", int(time.time()) + 60),  # 换令牌后的旧会话
    lambda: session_value(TOKEN, int(time.time()) + 60)[:-1] + "x",  # 篡改签名
    lambda: f"{int(time.time()) + 9999}." + session_value(TOKEN, int(time.time()) + 60).split(".")[1],  # 篡改过期时间
    lambda: "garbage", lambda: ".", lambda: "-1.abc",
])
def test_bad_cookies_rejected(client, make):
    client.cookies.set(COOKIE_NAME, make())
    assert client.get("/materials").status_code == 401


def test_non_ascii_session_value_rejected():
    assert auth.session_valid(TOKEN, "１２３.abc") is False
    assert auth.session_valid(TOKEN, None) is False


def test_oversized_cookie_is_401_not_500(client, caplog):
    """超长数字超过 Python 的整数位数上限：不能让未登录的人刷出 500 和错误日志。"""
    client.cookies.set(COOKIE_NAME, "9" * 5000 + ".x")
    response = client.get("/materials")
    assert response.status_code == 401
    assert not [r for r in caplog.records if r.levelno >= 40]


def test_lone_surrogate_login_token_is_not_500(client):
    response = client.post("/auth/login", content=b'{"token": "\\ud800"}', headers={"Content-Type": "application/json"})
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"


def test_rotating_token_invalidates_existing_session(client, monkeypatch):
    client.post("/auth/login", json={"token": TOKEN})
    assert client.get("/materials").status_code == 200
    monkeypatch.setattr(config, "ACCESS_TOKEN", "new-token")
    assert client.get("/materials").status_code == 401


def test_login_failures_are_throttled_then_recover(client, monkeypatch):
    monkeypatch.setattr(auth, "MAX_LOGIN_FAILURES", 3)
    for _ in range(3):
        assert client.post("/auth/login", json={"token": "wrong"}).status_code == 401
    blocked = client.post("/auth/login", json={"token": TOKEN})  # 限速期间正确令牌也要等
    assert blocked.status_code == 429 and int(blocked.headers["retry-after"]) >= 1
    assert blocked.json()["code"] == "too_many_attempts"
    monkeypatch.setattr(auth, "LOGIN_FAILURE_WINDOW_S", 0)
    assert client.post("/auth/login", json={"token": TOKEN}).status_code == 200


def test_token_never_reaches_logs(client, caplog):
    client.post("/auth/login", json={"token": TOKEN})
    client.post("/auth/login", json={"token": "wrong-token-value"})
    client.get("/materials", headers=bearer())
    assert TOKEN not in caplog.text and "wrong-token-value" not in caplog.text


# ---- 主体
def test_current_principal_is_the_single_local_subject(app, client):
    @app.get("/test/who")
    def who(principal: auth.Principal = Depends(auth.current_principal)):
        return {"id": principal.id}

    assert client.get("/test/who", headers=bearer()).json() == {"id": "local"}


def test_principal_available_without_token(app):
    @app.get("/test/who")
    def who(principal: auth.Principal = Depends(auth.current_principal)):
        return {"id": principal.id}

    assert TestClient(app).get("/test/who").json() == {"id": "local"}


# ---- 请求来源
@pytest.mark.parametrize("token", ["", TOKEN])
@pytest.mark.parametrize("host", ["evil.example", "evil.example:8000", "127.0.0.1.evil.example", "localhost.evil.example"])
def test_wrong_host_rejected_with_or_without_token(app, monkeypatch, token, host):
    monkeypatch.setattr(config, "ACCESS_TOKEN", token)
    response = TestClient(app).get("/healthz", headers={"Host": host, **bearer()})
    assert response.status_code == 400 and response.json()["code"] == "host_not_allowed"
    assert response.json()["request_id"]


@pytest.mark.parametrize("host", ["localhost", "localhost:8000", "127.0.0.1:8000", "[::1]:8000", "[::1]", "LOCALHOST:8000"])
def test_local_hosts_allowed(app, monkeypatch, host):
    monkeypatch.setattr(config, "ALLOWED_HOSTS", config.DEFAULT_ALLOWED_HOSTS.split(","))
    assert TestClient(app).get("/healthz", headers={"Host": host}).status_code == 200


def test_production_default_does_not_allow_testserver():
    assert "testserver" not in config.DEFAULT_ALLOWED_HOSTS


@pytest.mark.parametrize("token", ["", TOKEN])
@pytest.mark.parametrize("origin", ["https://evil.example", "null", "http://testserver.evil.example", "http://testserver:9999", "not a url"])
def test_cross_site_writes_rejected(app, monkeypatch, token, origin):
    monkeypatch.setattr(config, "ACCESS_TOKEN", token)
    client = TestClient(app)
    response = client.post("/materials", json={"title": "x", "content": "正文"}, headers={"Origin": origin, **bearer()})
    assert response.status_code == 403 and response.json()["code"] == "origin_not_allowed"
    upload = client.post("/materials/upload", files={"file": ("a.txt", b"hello")}, headers={"Origin": origin, **bearer()})
    assert upload.status_code == 403
    assert client.get("/materials", headers=bearer()).json() == []  # 什么也没写进去
    assert client.post("/auth/login", json={"token": TOKEN}, headers={"Origin": origin}).status_code == 403


def test_same_origin_write_and_upload_allowed(app, monkeypatch):
    client = TestClient(app)
    origin = {"Origin": "http://testserver"}
    assert client.post("/materials", json={"title": "x", "content": "正文"}, headers=origin).status_code == 201
    assert client.post("/materials/upload", files={"file": ("a.txt", b"hello")}, headers=origin).status_code == 201


def test_cross_site_get_is_not_origin_checked_but_needs_token(client):
    assert client.get("/materials", headers={"Origin": "https://evil.example"}).status_code == 401


def test_write_without_origin_still_needs_token(client):
    assert client.post("/materials", json={"title": "x", "content": "y"}).status_code == 401
    assert client.post("/materials", json={"title": "x", "content": "y"}, headers=bearer()).status_code == 201


# ---- 不设令牌：和现在一样
def test_no_token_needs_no_login(app):
    client = TestClient(app, follow_redirects=False)
    assert client.get("/materials").status_code == 200
    assert client.get("/docs").status_code == 200
    assert client.get("/web/app.js").status_code == 200
    assert client.get("/auth/status").json() == {"enabled": False}
    assert client.get("/login").status_code == 303


# ---- 启动检查
@pytest.mark.parametrize("host, loopback", [
    ("127.0.0.1", True), ("127.0.0.2", True), ("::1", True), ("[::1]", True), ("localhost", True), ("LOCALHOST", True),
    ("0.0.0.0", False), ("::", False), ("[::]", False), ("192.168.1.5", False), ("10.0.0.1", False), ("example.com", False),
    ("localhost.evil.example", False), ("", False),
])
def test_is_loopback_host(host, loopback):
    assert auth.is_loopback_host(host) is loopback
    assert (auth.startup_refusal(host, "") is None) is loopback  # 没设令牌：非本机拒绝
    assert auth.startup_refusal(host, TOKEN) is None  # 设了令牌都能启动


def run_serve(monkeypatch, host, token):
    import uvicorn

    started = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: started.append(k))
    monkeypatch.setattr("briefly.api.create_app", lambda **k: object())
    monkeypatch.setattr(config, "HOST", host)
    monkeypatch.setattr(config, "ACCESS_TOKEN", token)
    monkeypatch.setattr(config, "LOG_FILE", "")
    serve()
    return started


def test_serve_refuses_public_bind_without_token(monkeypatch):
    with pytest.raises(SystemExit, match="BRIEFLY_ACCESS_TOKEN"):
        run_serve(monkeypatch, "0.0.0.0", "")


def test_serve_starts_with_warning_on_loopback_without_token(monkeypatch, capsys):
    assert run_serve(monkeypatch, "127.0.0.1", "")
    assert "未设置 BRIEFLY_ACCESS_TOKEN" in capsys.readouterr().out


def test_serve_starts_on_public_bind_with_token_and_hints_hosts(monkeypatch, capsys):
    assert run_serve(monkeypatch, "0.0.0.0", TOKEN)
    out = capsys.readouterr().out
    assert "BRIEFLY_ALLOWED_HOSTS" in out and TOKEN not in out


# ---- 飞书回调
def test_feishu_callback_reachable_and_forged_state_rejected(secured, tmp_path):
    """注入假飞书客户端：否则会先走“未配置飞书”的分支，测不到 state 校验。"""
    from urllib.parse import unquote

    from fakes import FakeFeishu

    fake = FakeFeishu()
    engine = create_db_engine("sqlite://")
    path = tmp_path / "token.json"
    app = create_app(MaterialStore(engine), feishu_factory=fake.client, feishu_tokens=UserTokens(path, transport=fake.transport))
    response = TestClient(app, follow_redirects=False).get("/feishu/oauth/callback", params={"code": "c", "state": "forged"})
    assert response.status_code == 307
    assert "授权请求已过期或无效" in unquote(response.headers["location"])
    assert not path.exists()  # 没有换令牌，也没有保存
    assert not [r for r in fake.requests if "oauth/token" in str(r.url)]
    engine.dispose()


def test_feishu_login_is_not_public(client):
    assert client.get("/feishu/oauth/login").status_code == 401


def test_feishu_unauthorized_401_is_not_auth_required(app, secured):
    """前端靠 code 区分：飞书未连接也是 401，不能把用户踢回登录页。"""
    from briefly.feishu.oauth import FeishuNotAuthorized

    @app.get("/test/feishu")
    def feishu():
        raise FeishuNotAuthorized("还没有连接飞书账号")

    response = TestClient(app).get("/test/feishu", headers=bearer())
    assert response.status_code == 401 and response.json().get("code") != auth.AUTH_REQUIRED


# ---- 页面文件
def test_login_page_has_no_dependency_outside_whitelist():
    html = (Path(auth.__file__).parent / "web" / "login.html").read_text(encoding="utf-8")
    assert {*re.findall(r'(?:href|src)="([^"]+)"', html)} <= PUBLIC_PATHS
