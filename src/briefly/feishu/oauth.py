"""飞书用户授权（OAuth 2.0 授权码 + PKCE）：以用户本人身份访问云空间，不用逐篇“添加文档应用”。

流程：网页 /feishu/oauth/login → 飞书授权页 → /feishu/oauth/callback?code&state → v3 令牌端点换 user_access_token
（带 refresh_token，需要 offline_access）→ 存到本地 JSON 文件（单用户、本机程序，data/ 不进 Git）。
access_token 过期前 5 分钟用 refresh_token 刷新；refresh_token 失效（过期、被撤销）时清掉授权，提示重新连接。
"""
import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import httpx

from .. import config
from .client import FeishuError

log = logging.getLogger(__name__)

AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
TOKEN_URL = "https://accounts.feishu.cn/oauth/v3/token"
STATE_TTL = 600  # 授权页停留超过 10 分钟，state 失效


class FeishuNotAuthorized(FeishuError):
    """用户没有连接飞书账号，或授权已失效。"""


def redirect_uri() -> str:
    return config.FEISHU_REDIRECT_URI or f"http://{config.HOST}:{config.PORT}/feishu/oauth/callback"


# ---- 授权请求（state → code_verifier，进程内保存）
_pending: dict[str, tuple[str, float]] = {}
_pending_lock = threading.Lock()


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def authorize_url() -> str:
    """生成授权页地址；state 防跨站请求伪造，code_verifier 用于 PKCE。"""
    state, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
    now = time.time()
    with _pending_lock:
        for key in [k for k, (_, t) in _pending.items() if now - t > STATE_TTL]:
            del _pending[key]
        _pending[state] = (verifier, now)
    params = {
        "client_id": config.FEISHU_APP_ID,
        "redirect_uri": redirect_uri(),
        "scope": config.FEISHU_OAUTH_SCOPES,
        "state": state,
        "code_challenge": _challenge(verifier),
        "code_challenge_method": "S256",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def take_verifier(state: str) -> str:
    with _pending_lock:
        item = _pending.pop(state, None)
    if item is None or time.time() - item[1] > STATE_TTL:
        raise FeishuNotAuthorized("授权请求已过期或无效，请重新点击“连接飞书”")
    return item[0]


def fetch_user_info(client, access_token: str) -> dict:
    """用刚拿到的 user_access_token 查当前用户的姓名和 open_id（走应用客户端的连接池和重试）。"""
    body = client._call(
        "GET", "/open-apis/authen/v1/user_info", headers={"Authorization": f"Bearer {access_token}"}, idempotent=True
    )
    data = body.get("data") or {}
    return {"name": data.get("name", ""), "open_id": data.get("open_id", "")}


# ---- 令牌
def _token_request(http: httpx.Client, form: dict) -> dict:
    """调用 v3 令牌端点（表单格式，client_secret 放在请求体里）。返回字段直接在顶层，不在 data 里。"""
    form = {"client_id": config.FEISHU_APP_ID, "client_secret": config.FEISHU_APP_SECRET, **form}
    try:
        r = http.post(TOKEN_URL, data=form)
    except httpx.HTTPError as e:
        raise FeishuError(f"无法连接飞书授权服务：{type(e).__name__}: {e}") from e
    try:
        body = r.json()
    except ValueError:
        body = {"code": None, "error_description": r.text[:200]}
    if body.get("code") == 0 and body.get("access_token"):
        return body
    message = body.get("error_description") or body.get("error") or body.get("msg") or f"HTTP {r.status_code}"
    raise FeishuError(f"飞书授权失败（{body.get('code')}）：{message}", body.get("code"), r.status_code)


class UserTokens:
    """用户令牌：存在本地 JSON 文件；读取时自动刷新。多线程共用一把锁，同一时刻只刷新一次。"""

    _lock = threading.Lock()

    def __init__(self, path: str | Path | None = None, transport: httpx.BaseTransport | None = None) -> None:
        self.path = Path(path or config.FEISHU_USER_TOKEN_FILE)
        self.transport = transport  # 测试时注入模拟响应

    def _http(self) -> httpx.Client:
        return httpx.Client(transport=self.transport, timeout=20.0)

    def load(self) -> dict | None:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)  # 写完再替换，中途崩溃不会留下半个文件

    def clear(self) -> None:
        with self._lock:
            self.path.unlink(missing_ok=True)

    def connected(self) -> dict | None:
        """已连接时返回 {name, open_id, scope, refresh_expires_at}（不含令牌）。"""
        data = self.load()
        if not data or data.get("refresh_expires_at", 0) < time.time():
            return None
        return {k: data.get(k) for k in ("name", "open_id", "scope", "refresh_expires_at")}

    def _store(self, body: dict, extra: dict | None = None) -> dict:
        now = time.time()
        data = {
            **(extra or {}),
            "access_token": body["access_token"],
            "expires_at": now + int(body.get("expires_in", 7200)),
            "refresh_token": body.get("refresh_token", ""),
            "refresh_expires_at": now + int(body.get("refresh_token_expires_in", 0)) if body.get("refresh_token") else 0,
            "scope": body.get("scope", ""),
            "updated_at": now,
        }
        if not data["refresh_token"]:
            # 没有 offline_access 权限时拿不到 refresh_token：只能用到 access_token 过期
            data["refresh_expires_at"] = data["expires_at"]
        self._save(data)
        return data

    def exchange(self, code: str, state: str, user_info) -> dict:
        """授权码换令牌并保存。user_info(access_token) 返回 {name, open_id}。"""
        verifier = take_verifier(state)
        with self._http() as http:
            body = _token_request(
                http,
                {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(), "code_verifier": verifier},
            )
        info = user_info(body["access_token"])
        with self._lock:
            return self._store(body, {"name": info.get("name", ""), "open_id": info.get("open_id", "")})

    def access_token(self, refresh: bool = False, stale: str = "") -> str:
        """当前可用的 access_token；快过期或被判定失效时用 refresh_token 刷新。"""
        with self._lock:
            data = self.load()
            if not data:
                raise FeishuNotAuthorized("还没有连接飞书账号：请在网页“材料”页点击“连接飞书”")
            if refresh and stale and data["access_token"] != stale:
                return data["access_token"]  # 别的线程已经刷新过
            if not refresh and data["expires_at"] - 300 > time.time():
                return data["access_token"]
            if not data.get("refresh_token") or data.get("refresh_expires_at", 0) < time.time():
                self.path.unlink(missing_ok=True)
                raise FeishuNotAuthorized("飞书授权已过期，请在网页“材料”页重新连接飞书")
            try:
                with self._http() as http:
                    body = _token_request(http, {"grant_type": "refresh_token", "refresh_token": data["refresh_token"]})
            except FeishuError as e:
                if e.status is not None and 400 <= e.status < 500:  # refresh_token 失效、被撤销
                    self.path.unlink(missing_ok=True)
                    raise FeishuNotAuthorized(f"飞书授权已失效，请重新连接飞书（{e}）") from e
                raise
            return self._store(body, {"name": data.get("name", ""), "open_id": data.get("open_id", "")})["access_token"]
