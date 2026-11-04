"""访问认证：单一访问令牌＋网页登录，默认拒绝，不建用户表。

用纯 ASGI 中间件强制认证，白名单之外全部要求登录：路由分散在多个 register_* 里，逐个加依赖容易漏，
/docs、/openapi.json 和静态文件也不经过路由依赖。Host 和 Origin 校验不管有没有令牌都生效，
因为绑定本机也可能被 DNS 重绑定和跨站请求利用。

业务代码要知道“谁”，只能通过 current_principal 依赖取得，不能直接读令牌或 Cookie；
以后加用户表时只替换这里的验证实现。
"""
import hashlib
import hmac
import ipaddress
import logging
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
from starlette.datastructures import Headers
from starlette.requests import HTTPConnection

from . import config

log = logging.getLogger(__name__)

COOKIE_NAME = "briefly_session"
AUTH_REQUIRED = "auth_required"  # 前端凭这个机器码跳转登录页；飞书未授权也返回 401，不能只看状态码
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# 只有这些路径不要求登录，按完整路径匹配。/feishu/oauth/callback 靠一次性的 state 防伪，/feishu/oauth/login 不放行
PUBLIC_PATHS = frozenset({
    "/healthz", "/login", "/auth/login", "/feishu/oauth/callback", "/web/login.js", "/web/style.css",
})
MAX_LOGIN_FAILURES = 10
LOGIN_FAILURE_WINDOW_S = 300


@dataclass(frozen=True)
class Principal:
    id: str


LOCAL_PRINCIPAL = Principal(id="local")  # W4 只有这一个主体


def current_principal(request: Request) -> Principal:
    """当前请求的主体：业务代码取得身份的唯一入口。"""
    return request.state.principal


# ---- 凭据
def _session_signature(token: str, expires_at: int) -> str:
    key = hmac.new(token.encode(), b"briefly-session-v1", hashlib.sha256).digest()  # 换令牌后旧会话全部失效
    return hmac.new(key, str(expires_at).encode(), hashlib.sha256).hexdigest()


def session_value(token: str, expires_at: int) -> str:
    return f"{expires_at}.{_session_signature(token, expires_at)}"


def session_valid(token: str, value: str | None) -> bool:
    expires, _, signature = (value or "").partition(".")
    # 先限制长度：超长数字转换时 Python 会抛异常，未登录的人就能刷出 500 和错误日志
    if len(expires) > 20 or not expires.isascii() or not expires.isdigit():
        return False
    expires_at = int(expires)
    return (hmac.compare_digest(signature.encode(), _session_signature(token, expires_at).encode())
            and expires_at > time.time())


def token_matches(token: str, candidate: bytes) -> bool:
    return hmac.compare_digest(token.encode(), candidate)


def _authenticated(scope, token: str) -> bool:
    connection = HTTPConnection(scope)
    authorization = connection.headers.get("authorization")
    if authorization is not None:  # 带了 Authorization 就只看它，不再退回 Cookie
        scheme, _, value = authorization.partition(" ")
        return scheme.lower() == "bearer" and token_matches(token, value.strip().encode("latin-1"))
    return session_valid(token, connection.cookies.get(COOKIE_NAME))


# ---- 请求来源
def _hostname(netloc: str) -> str:
    netloc = netloc.strip().lower()
    if netloc.startswith("["):
        return netloc.partition("]")[0] + "]"
    return netloc.partition(":")[0]


def host_allowed(host: str | None) -> bool:
    return bool(host) and _hostname(host) in config.ALLOWED_HOSTS


def origin_allowed(origin: str, host: str) -> bool:
    """网页和接口同源：Origin 的主机和端口必须和请求的 Host 一致，Host 本身已在允许范围内。"""
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and parts.netloc.lower() == host.strip().lower()


# ---- 中间件
class AuthMiddleware:
    """先校验请求来源，再校验凭据。没设令牌时只校验来源。"""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope["type"] != "http":  # 项目没有 WebSocket 接口，默认拒绝
            await send({"type": "websocket.close", "code": 1008})
            return
        headers = Headers(scope=scope)
        host, origin = headers.get("host"), headers.get("origin")
        rejected = None
        if not host_allowed(host):
            rejected = _error(400, "请求的主机名不被允许。如果这是你自己的访问地址，请把它加入 BRIEFLY_ALLOWED_HOSTS。", "host_not_allowed")
        elif scope["method"] in UNSAFE_METHODS and origin is not None and not origin_allowed(origin, host):
            rejected = _error(403, "拒绝来自其他网站的写操作。", "origin_not_allowed")
        elif not self._permitted(scope):
            if scope["method"] in ("GET", "HEAD") and "text/html" in headers.get("accept", ""):
                rejected = RedirectResponse("/login", status_code=303)
            else:
                rejected = _error(401, "请先登录。", AUTH_REQUIRED, {"WWW-Authenticate": "Bearer"})
        if rejected is not None:
            await rejected(scope, receive, send)
            return
        scope.setdefault("state", {})["principal"] = LOCAL_PRINCIPAL
        await self.app(scope, receive, send)

    @staticmethod
    def _permitted(scope) -> bool:
        token = config.ACCESS_TOKEN
        return not token or scope["path"] in PUBLIC_PATHS or _authenticated(scope, token)


def _error(status: int, detail: str, code: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail, "code": code}, headers=headers)


# ---- 登录失败限速（进程内）
class LoginThrottle:
    """同一来源在时间窗内失败过多就暂停尝试；登录成功后清零。"""

    def __init__(self) -> None:
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def retry_after(self, client: str) -> int:
        """还要等多少秒；0 表示可以尝试。"""
        now = time.monotonic()
        with self._lock:
            recent = [t for t in self._failures.get(client, []) if now - t < LOGIN_FAILURE_WINDOW_S]
            if len(recent) < MAX_LOGIN_FAILURES:
                return 0
            return max(1, int(LOGIN_FAILURE_WINDOW_S - (now - recent[0])))

    def failed(self, client: str) -> None:
        now = time.monotonic()
        with self._lock:
            for key in [k for k, v in self._failures.items() if now - v[-1] >= LOGIN_FAILURE_WINDOW_S]:
                del self._failures[key]
            self._failures.setdefault(client, []).append(now)

    def succeeded(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)


class LoginIn(BaseModel):
    token: str


def register(app: FastAPI, web_dir) -> None:
    throttle = LoginThrottle()

    @app.get("/login", include_in_schema=False)
    def login_page(request: Request):
        token = config.ACCESS_TOKEN
        if not token or session_valid(token, request.cookies.get(COOKIE_NAME)):
            return RedirectResponse("/", status_code=303)
        return FileResponse(web_dir / "login.html")

    @app.post("/auth/login", include_in_schema=False)
    def login(body: LoginIn, request: Request, response: Response):
        token = config.ACCESS_TOKEN
        if not token:
            return {"enabled": False}
        client = request.client.host if request.client else ""
        wait = throttle.retry_after(client)
        if wait:
            return _error(429, f"尝试次数过多，请约 {wait} 秒后再试。", "too_many_attempts", {"Retry-After": str(wait)})
        # JSON 里的单独代理字符无法按 UTF-8 编码；替换后走“令牌错误”，同样计入限速，不抛异常
        if not token_matches(token, body.token.strip().encode("utf-8", "replace")):
            throttle.failed(client)
            log.warning("登录失败：访问令牌不正确")
            return _error(401, "访问令牌不正确。", "invalid_token")
        throttle.succeeded(client)
        max_age = int(config.SESSION_DAYS * 86400)
        response.set_cookie(COOKIE_NAME, session_value(token, int(time.time()) + max_age), max_age=max_age, path="/",
                            httponly=True, samesite="lax", secure=request.url.scheme == "https")
        return {"enabled": True}

    @app.post("/auth/logout", include_in_schema=False)
    def logout(request: Request, response: Response):
        response.delete_cookie(COOKIE_NAME, path="/", httponly=True, samesite="lax", secure=request.url.scheme == "https")
        return {"ok": True}

    @app.get("/auth/status", include_in_schema=False)
    def auth_status():
        return {"enabled": bool(config.ACCESS_TOKEN)}


# ---- 启动检查
def is_loopback_host(host: str) -> bool:
    """只比较字符串会漏掉 127.0.0.2、::1 这些写法，也可能把 0.0.0.0 当成本机。"""
    host = host.strip().lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False  # 其他主机名可能解析到任何地址，按非本机处理


def startup_refusal(host: str, token: str) -> str | None:
    """不能启动时返回原因。"""
    if not token and not is_loopback_host(host):
        return (f"拒绝启动：服务绑定到非本机地址 {host}，但没有设置 BRIEFLY_ACCESS_TOKEN，任何能连上这台机器的人都能读取数据。"
                "请在 .env 里设置访问令牌，或把 BRIEFLY_HOST 改回 127.0.0.1。")
    return None


def log_startup_state(host: str, token: str) -> None:
    if not token:
        log.warning("未设置 BRIEFLY_ACCESS_TOKEN：不要求登录，只允许本机访问地址；主机名和跨站写操作的校验仍然生效")
    if not is_loopback_host(host) and _hostname(host) not in config.ALLOWED_HOSTS:
        log.warning("服务绑定到 %s，但访问用的主机名需要写进 BRIEFLY_ALLOWED_HOSTS，否则请求会被拒绝", host)
