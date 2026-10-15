"""飞书开放平台 REST 的薄封装（连接层）：应用身份（tenant_access_token）、令牌缓存、限频重试、错误码转中文提示。

只封装用到的接口，不引入官方 SDK：接口少，用 httpx 写清楚、测试时用 httpx.MockTransport 模拟响应。
所有飞书调用都经过这里；业务模块（docx、bitable、messages、contact）只拼路径和参数，工具和追踪模块只调用业务模块。
"""
import copy
import logging
import random
import threading
import time
from collections.abc import Callable

import httpx

from .. import config

log = logging.getLogger(__name__)

# 令牌失效（过期、被重置）：刷新令牌后重试一次
_TOKEN_INVALID = {99991661, 99991663, 99991664, 99991665, 99991668, 99991677}  # 后两个是 user_access_token 失效 / 过期
# 用户身份没权限、但应用身份可能有（文档分享给了应用）：退回应用身份重试
_NO_PERMISSION = {1770032, 91403, 131006, 1063002, 2091005, 99991672, 99991679}
# 限频：应用级 400 + 99991400；单篇文档并发编辑 HTTP 429
_RATE_LIMITED = {99991400}
_TRANSIENT_STATUS = {500, 502, 503, 504}
# 常见错误码的处理建议（错误原文仍保留在消息里）
_HINTS = {
    99991672: "应用缺少接口权限：在开发者后台“权限管理”开通后，需要创建新版本并发布才生效",
    # 用户身份缺权限：多半是授权在开通权限之前做的，令牌里没有（实测妙记）
    99991679: "你的飞书授权里没有这个权限：确认后台已开通对应的“用户身份”权限并发布后，在网页“材料”页断开并重新连接飞书",
    2091005: "没有这条妙记的权限：连接飞书账号后以你的身份读取，或在妙记右上角把应用加为协作者",
    10003: "App ID 或 App Secret 不正确，请检查 .env 中的 FEISHU_APP_ID、FEISHU_APP_SECRET",
    10014: "App Secret 不正确，请检查 .env 中的 FEISHU_APP_SECRET",
    1770002: "文档不存在，请检查链接",
    1770032: "应用没有这份文档的权限：请在文档右上角“…→ 更多 → 添加文档应用”把应用加进来",
    131006: "应用没有这个知识库节点的权限：请把应用添加为知识库或该页面的协作者",
    91402: "多维表格或数据表不存在，请检查链接",
    91403: "应用没有这个多维表格的权限：请在多维表格右上角“…→ 更多 → 添加文档应用”把应用加进来",
    230002: "机器人不在该会话中，或接收人不在应用的可用范围内",
    230013: "接收人不在应用的可用范围内：在开发者后台“版本管理与发布”中调整可用范围",
}
_HTTP_HINTS = {
    403: "应用没有访问权限：请把应用添加为文档协作者（文档右上角“…→ 更多 → 添加文档应用”），或在后台开通对应权限",
    404: "资源不存在，请检查链接",
}


class FeishuError(Exception):
    """飞书接口返回错误。message 是给用户看的中文说明。"""

    def __init__(self, message: str, code: int | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


class FeishuNotConfigured(FeishuError):
    """没有配置应用凭证。"""


def describe_error(code: int | None, msg: str, status: int | None = None) -> str:
    hint = _HINTS.get(code or 0) or _HTTP_HINTS.get(status or 0)
    raw = f"飞书接口错误 {code}：{msg}" if code else f"飞书接口错误（HTTP {status}）：{msg}"
    return f"{hint}（{raw}）" if hint else raw


class FeishuClient:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        base_url: str = "https://open.feishu.cn",
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 20.0,
        retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
        shared: bool = False,
    ) -> None:
        """transport 用于测试时注入模拟响应；sleep 可替换，测试中不真的等待。

        shared：进程内共用的实例（client_from_config 返回的那个），with 结束时不关闭，令牌和连接池跨请求复用。
        用同步的 httpx.Client：接口函数是 def（在线程池里执行）、扫描在后台线程、CLI 是同步的；
        httpx.Client 可以被多个线程同时使用，连接池由它管理。
        """
        if not app_id or not app_secret:
            raise FeishuNotConfigured("未配置飞书应用：请在 .env 中填写 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
        self.app_id, self.app_secret = app_id, app_secret
        self.base_url = base_url.rstrip("/")
        self.retries = retries
        self.sleep = sleep
        self.shared = shared
        self._http = httpx.Client(base_url=self.base_url, transport=transport, timeout=timeout)
        self._token = ""
        self._expires_at = 0.0
        self._lock = threading.Lock()
        self._user_tokens = None  # as_user 返回的视图里是 UserTokens
        self.user_mode = False

    def as_user(self, tokens) -> "FeishuClient":
        """以用户身份调用的视图：共用连接池和重试逻辑，只换令牌来源。关闭视图不会关闭连接池。"""
        view = copy.copy(self)
        view._user_tokens, view.user_mode, view.shared = tokens, True, True
        return view

    def close(self, force: bool = False) -> None:
        """共用实例只在 force 时关闭（进程退出时由 close_shared 调用）。"""
        if force or not self.shared:
            self._http.close()

    def __enter__(self) -> "FeishuClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- 令牌
    def token(self, refresh: bool = False, stale: str = "") -> str:
        """tenant_access_token 最长 2 小时有效；缓存在实例里，提前 5 分钟刷新。

        stale：调用方刚用过、被飞书判定失效的令牌。多个线程同时遇到失效时只有第一个真的去刷新，
        其余发现令牌已经换过，直接用新的。
        """
        if self._user_tokens is not None:  # 用户身份：令牌来自 OAuth 授权
            return self._user_tokens.access_token(refresh=refresh, stale=stale)
        with self._lock:
            if refresh and stale and self._token != stale:
                return self._token
            if refresh or not self._token or time.time() > self._expires_at:
                body = self._call(
                    "POST",
                    "/open-apis/auth/v3/tenant_access_token/internal",
                    json={"app_id": self.app_id, "app_secret": self.app_secret},
                    idempotent=True,
                )
                self._token = body["tenant_access_token"]
                self._expires_at = time.time() + max(int(body.get("expire", 7200)) - 300, 60)
            return self._token

    # ---- 请求
    def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        headers: dict | None = None,
        idempotent: bool,
    ) -> dict:
        """发请求并处理重试，返回完整响应体（成功时 code 为 0）。

        成功要同时满足 HTTP 状态码 < 400 和业务错误码 code == 0：飞书有 HTTP 200 + code 非 0 的业务错误，
        也有 HTTP 4xx/5xx 的网关错误（响应可能不是 JSON）。
        重试：限频（99991400、HTTP 429）表示请求被拒、没有执行，总是重试；网络错误和 5xx 时请求可能已经执行，
        只有 idempotent（读取，或带了去重标识的写入）才重试，避免重复建文档、重复发消息。
        """
        attempt = 0
        while True:
            r = None
            try:
                r = self._http.request(method, path, params=params, json=json, headers=headers)
            except httpx.TimeoutException as e:
                retry, error = idempotent, f"连接飞书超时：{e}"
            except httpx.HTTPError as e:
                retry, error = idempotent, f"无法连接飞书：{type(e).__name__}: {e}"
            if r is not None:
                body = _json(r)
                code = body.get("code")
                if code == 0 and r.status_code < 400:
                    return body
                limited = code in _RATE_LIMITED or r.status_code == 429
                retry = limited or (idempotent and r.status_code in _TRANSIENT_STATUS)
                error = describe_error(code, body.get("msg", r.text[:200]), r.status_code)
                if not retry or attempt >= self.retries:
                    raise FeishuError(error, code, r.status_code)
            elif not retry or attempt >= self.retries:
                raise FeishuError(error)
            attempt += 1
            wait = _backoff(attempt, r)
            log.info("飞书接口暂时不可用（%s），%.1f 秒后第 %s 次重试", error, wait, attempt)
            self.sleep(wait)

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        idempotent: bool | None = None,
    ) -> dict:
        """调用接口，返回 data 字段。idempotent 不填时：GET 可重试；其他方法遇到网络错误、5xx 不重试。
        令牌失效（被重置、提前过期）时刷新后重发一次：失效的请求没有执行，重发是安全的。"""
        idempotent = method == "GET" if idempotent is None else idempotent
        token = self.token()
        try:
            body = self._call(method, path, params=params, json=json, idempotent=idempotent,
                              headers={"Authorization": f"Bearer {token}"})  # fmt: skip
        except FeishuError as e:
            if e.code not in _TOKEN_INVALID:
                raise
            token = self.token(refresh=True, stale=token)
            body = self._call(method, path, params=params, json=json, idempotent=idempotent,
                              headers={"Authorization": f"Bearer {token}"})  # fmt: skip
        return body.get("data") or {}

    def get(self, path: str, **params) -> dict:
        return self.request("GET", path, params={k: v for k, v in params.items() if v is not None})

    def post(self, path: str, json: dict, *, idempotent: bool = False, **params) -> dict:
        """idempotent=True：查询类 POST，或带了去重标识（client_token、uuid）的写入。"""
        params = {k: v for k, v in params.items() if v is not None}
        return self.request("POST", path, json=json, params=params, idempotent=idempotent)

    def download(self, path: str, **params) -> bytes:
        """下载接口（成功时返回文件流而不是 JSON，如妙记文字稿）。失败时飞书仍返回 JSON 错误，按普通接口处理。"""
        params = {k: v for k, v in params.items() if v is not None}
        token, refreshed, attempt = self.token(), False, 0
        while True:
            r = None
            try:
                r = self._http.get(path, params=params, headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as e:
                error = f"无法连接飞书：{type(e).__name__}: {e}"
            if r is not None:
                if r.status_code < 400 and "application/json" not in r.headers.get("content-type", ""):
                    return r.content
                body = _json(r)
                code = body.get("code")
                if code == 0 and r.status_code < 400:  # 成功但给的是 JSON：原样返回
                    return r.content
                if code in _TOKEN_INVALID and not refreshed:
                    token, refreshed = self.token(refresh=True, stale=token), True
                    continue
                error = describe_error(code, body.get("msg", r.text[:200]), r.status_code)
                retry = code in _RATE_LIMITED or r.status_code in {429} | _TRANSIENT_STATUS
                if not retry or attempt >= self.retries:
                    raise FeishuError(error, code, r.status_code)
            elif attempt >= self.retries:  # 下载是读取，网络错误可以重试
                raise FeishuError(error)
            attempt += 1
            self.sleep(_backoff(attempt, r))

    def paginate(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None, limit: int = 100) -> list[dict]:
        """按 page_token 翻页，合并各页的 items。limit 是最多翻几页（防止异常数据导致死循环）。翻页都是读取，可重试。"""
        items: list[dict] = []
        params = dict(params or {})
        for _ in range(limit):
            data = self.request(method, path, params=params, json=json, idempotent=True)
            items += data.get("items") or []
            if not data.get("has_more") or not data.get("page_token"):
                return items
            params["page_token"] = data["page_token"]
        raise FeishuError(f"分页超过 {limit} 页，内容过多，未读取完")


def _backoff(attempt: int, r: httpx.Response | None) -> float:
    """指数退避加随机抖动（约 1、2、4 秒，最多 10 秒）；响应带了限频重置时间时按它等。"""
    if r is not None:
        for header in ("x-ogw-ratelimit-reset", "retry-after"):
            try:
                return min(max(float(r.headers.get(header, "")), 0.0), 10.0)
            except ValueError:
                continue
    return min(2 ** (attempt - 1) * (1 + random.random() * 0.25), 10.0)


def _json(r: httpx.Response) -> dict:
    try:
        body = r.json()
    except ValueError:
        return {"code": None, "msg": r.text[:200]}
    return body if isinstance(body, dict) else {"code": None, "msg": str(body)[:200]}


_shared: dict[tuple, FeishuClient] = {}
_shared_lock = threading.Lock()


def client_from_config() -> FeishuClient:
    """进程内共用的客户端：令牌缓存和连接池在所有请求间复用。凭证或地址变了换一个新实例。"""
    key = (config.FEISHU_APP_ID, config.FEISHU_APP_SECRET, config.FEISHU_BASE_URL)
    with _shared_lock:
        client = _shared.get(key)
        if client is None:
            client = FeishuClient(*key, shared=True)
            for old in _shared.values():
                old.close(force=True)
            _shared.clear()
            _shared[key] = client
        return client


class FallbackClient:
    """先以用户身份调用，用户没权限（或授权失效）时退回应用身份。接口与 FeishuClient 相同。

    用于“读取”和“以用户身份新建文档”：用户自己能看到的文档不用再逐篇添加应用；用户看不到、但分享给了应用的，照常可读。
    """

    def __init__(self, user: FeishuClient, app: FeishuClient) -> None:
        self.user, self.app = user, app
        self.sleep = app.sleep
        self.user_mode = True  # 写入时以用户身份为准（新建的文档归用户所有）

    def _fallback(self, call: Callable[[FeishuClient], dict | list]):
        from .oauth import FeishuNotAuthorized

        try:
            return call(self.user)
        except FeishuNotAuthorized:
            self.user_mode = False
        except FeishuError as e:
            if e.code not in _NO_PERMISSION and e.status != 403:
                raise
            log.info("用户身份没有权限（%s），改用应用身份", e.code or e.status)
        return call(self.app)

    def request(self, method: str, path: str, **kwargs) -> dict:
        return self._fallback(lambda c: c.request(method, path, **kwargs))

    def get(self, path: str, **params) -> dict:
        return self._fallback(lambda c: c.get(path, **params))

    def post(self, path: str, json: dict, *, idempotent: bool = False, **params) -> dict:
        return self._fallback(lambda c: c.post(path, json, idempotent=idempotent, **params))

    def paginate(self, method: str, path: str, **kwargs) -> list[dict]:
        return self._fallback(lambda c: c.paginate(method, path, **kwargs))

    def download(self, path: str, **params) -> bytes:
        return self._fallback(lambda c: c.download(path, **params))

    def close(self) -> None:
        self.app.close()  # 共用实例不会真的关闭；用户视图共用同一个连接池

    def __enter__(self) -> "FallbackClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def with_user(app: FeishuClient, tokens) -> FeishuClient | FallbackClient:
    """连接了飞书账号（tokens 有效）时返回“先用户、后应用”的客户端，否则原样返回应用身份的客户端。"""
    if tokens is None or tokens.connected() is None:
        return app
    return FallbackClient(app.as_user(tokens), app)


def default_client() -> FeishuClient | FallbackClient:
    """业务代码默认用的客户端（命令行、追踪、对话工具）：连接了飞书账号时先用用户身份，否则只用应用身份。"""
    from .oauth import UserTokens

    return with_user(client_from_config(), UserTokens())


def close_shared() -> None:
    """进程退出前关闭共用客户端的连接池。"""
    with _shared_lock:
        for client in _shared.values():
            client.close(force=True)
        _shared.clear()


def configured() -> bool:
    return bool(config.FEISHU_APP_ID and config.FEISHU_APP_SECRET)
