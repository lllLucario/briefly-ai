"""飞书开放平台 REST 的薄封装：应用身份（tenant_access_token）、令牌缓存、限频重试、错误码转中文提示。

只封装用到的接口，不引入官方 SDK：接口少，用 httpx 写清楚、测试时用 httpx.MockTransport 模拟响应。
"""
import logging
import threading
import time
from collections.abc import Callable

import httpx

from .. import config

log = logging.getLogger(__name__)

# 令牌失效（过期、被重置）：刷新令牌后重试一次
_TOKEN_INVALID = {99991661, 99991663, 99991664, 99991665}
# 限频：应用级 400 + 99991400；单篇文档并发编辑 HTTP 429
_RATE_LIMITED = {99991400}
# 常见错误码的处理建议（错误原文仍保留在消息里）
_HINTS = {
    99991672: "应用缺少接口权限：在开发者后台“权限管理”开通后，需要创建新版本并发布才生效",
    99991679: "应用缺少接口权限：在开发者后台“权限管理”开通后，需要创建新版本并发布才生效",
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
    ) -> None:
        """transport 用于测试时注入模拟响应；sleep 可替换，测试中不真的等待。"""
        if not app_id or not app_secret:
            raise FeishuNotConfigured("未配置飞书应用：请在 .env 中填写 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
        self.app_id, self.app_secret = app_id, app_secret
        self.base_url = base_url.rstrip("/")
        self.retries = retries
        self.sleep = sleep
        self._http = httpx.Client(base_url=self.base_url, transport=transport, timeout=timeout)
        self._token = ""
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "FeishuClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- 令牌
    def token(self, refresh: bool = False) -> str:
        """tenant_access_token 最长 2 小时有效；提前 5 分钟刷新。"""
        with self._lock:
            if refresh or not self._token or time.time() > self._expires_at:
                try:
                    r = self._http.post(
                        "/open-apis/auth/v3/tenant_access_token/internal",
                        json={"app_id": self.app_id, "app_secret": self.app_secret},
                    )
                except httpx.HTTPError as e:
                    raise FeishuError(f"无法连接飞书：{type(e).__name__}: {e}") from e
                body = _json(r)
                if body.get("code") != 0:
                    raise FeishuError(describe_error(body.get("code"), body.get("msg", ""), r.status_code), body.get("code"), r.status_code)
                self._token = body["tenant_access_token"]
                self._expires_at = time.time() + max(int(body.get("expire", 7200)) - 300, 60)
            return self._token

    # ---- 请求
    def request(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None) -> dict:
        """调用接口，返回 data 字段。令牌失效刷新后重试一次；限频按指数退避重试。"""
        refreshed = False
        attempt = 0
        while True:
            headers = {"Authorization": f"Bearer {self.token()}"}
            try:
                r = self._http.request(method, path, params=params, json=json, headers=headers)
            except httpx.HTTPError as e:
                if attempt < self.retries:
                    attempt += 1
                    self.sleep(2 ** (attempt - 1))
                    continue
                raise FeishuError(f"无法连接飞书：{type(e).__name__}: {e}") from e
            body = _json(r)
            code = body.get("code")
            if code == 0 and r.status_code < 400:
                return body.get("data") or {}
            if code in _TOKEN_INVALID and not refreshed:
                refreshed = True
                self.token(refresh=True)
                continue
            if (code in _RATE_LIMITED or r.status_code in (429, 500, 502, 503, 504)) and attempt < self.retries:
                attempt += 1
                log.info("飞书接口限频或暂时出错（%s %s），%s 秒后重试", r.status_code, code, 2 ** (attempt - 1))
                self.sleep(2 ** (attempt - 1))
                continue
            raise FeishuError(describe_error(code, body.get("msg", r.text[:200]), r.status_code), code, r.status_code)

    def get(self, path: str, **params) -> dict:
        return self.request("GET", path, params={k: v for k, v in params.items() if v is not None})

    def post(self, path: str, json: dict, **params) -> dict:
        return self.request("POST", path, json=json, params={k: v for k, v in params.items() if v is not None})

    def paginate(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None, limit: int = 100) -> list[dict]:
        """按 page_token 翻页，合并各页的 items。limit 是最多翻几页（防止异常数据导致死循环）。"""
        items: list[dict] = []
        params = dict(params or {})
        for _ in range(limit):
            data = self.request(method, path, params=params, json=json)
            items += data.get("items") or []
            if not data.get("has_more") or not data.get("page_token"):
                return items
            params["page_token"] = data["page_token"]
        raise FeishuError(f"分页超过 {limit} 页，内容过多，未读取完")


def _json(r: httpx.Response) -> dict:
    try:
        body = r.json()
    except ValueError:
        return {"code": None, "msg": r.text[:200]}
    return body if isinstance(body, dict) else {"code": None, "msg": str(body)[:200]}


def client_from_config(**kwargs) -> FeishuClient:
    return FeishuClient(config.FEISHU_APP_ID, config.FEISHU_APP_SECRET, config.FEISHU_BASE_URL, **kwargs)


def configured() -> bool:
    return bool(config.FEISHU_APP_ID and config.FEISHU_APP_SECRET)
