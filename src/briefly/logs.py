"""结构化日志与请求追踪号：任何一次失败的请求，都能凭响应里的追踪号在日志中查到记录和堆栈。

日志是防数据泄露的一部分：不能写入文档正文、问题原文、模型回答、密钥和令牌；排查需要时只记长度、哈希、ID。
第三方库（httpx、openai 等）的 INFO、DEBUG 日志会带上请求地址甚至请求体，所以调高日志级别只对本项目生效。
"""
import contextvars
import json
import logging
import re
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from starlette.datastructures import MutableHeaders

from . import config

log = logging.getLogger(__name__)
access_log = logging.getLogger("briefly.access")

REQUEST_ID_HEADER = "X-Request-ID"
# 只接受字母、数字和少量分隔符：追踪号会原样进日志和响应头，不能带换行或控制字符
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_RECORD_FIELDS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime", "taskName"}
INTERNAL_ERROR = "服务内部出错，请稍后重试；如果持续出现，请把追踪号发给维护人员。"

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("briefly_request_id", default=None)


def current_request_id() -> str | None:
    return request_id_var.get()


def _secret_values() -> list[str]:
    return [s for s in (config.API_KEY, config.FEISHU_APP_SECRET, config.SMTP_PASSWORD, config.ACCESS_TOKEN) if s]


class JsonFormatter(logging.Formatter):
    """每条日志一行 JSON；异常堆栈放在 exception 字段里，也不换行。已知密钥在输出前统一遮盖。"""

    def format(self, record: logging.LogRecord) -> str:
        data = {
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "module": record.name,
            "message": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        data |= {k: v for k, v in record.__dict__.items() if k not in _RECORD_FIELDS}  # extra= 传入的字段
        if record.exc_info:
            data["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            data["stack"] = self.formatStack(record.stack_info)
        line = _BEARER.sub("Bearer ***", json.dumps(data, ensure_ascii=False, default=str))
        for secret in _secret_values():
            line = line.replace(secret, "***")
        return line


class _ConsoleHandler(logging.StreamHandler):
    """每次输出时才取 sys.stdout / sys.stderr，并避免 GBK 控制台遇到编不了的字符时丢掉整行。"""

    def __init__(self, name: str) -> None:
        logging.Handler.__init__(self)  # 不走 StreamHandler.__init__：stream 是下面的只读属性
        self._stream_name = name

    @property
    def stream(self):
        return getattr(sys, self._stream_name)

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        encoding = getattr(self.stream, "encoding", None)
        return text.encode(encoding, "backslashreplace").decode(encoding) if encoding else text


def configure_logging(*, level: str | None = None, stream: str = "stdout", log_file: str | None = None) -> str:
    """配置全局日志，可重复调用；只替换自己加过的处理器，不动 pytest 等已有的处理器。返回生效的级别名。

    只由 serve() 和命令行入口调用，create_app() 不调用，否则会影响测试。
    """
    name = (level or config.LOG_LEVEL or "INFO").strip().upper()
    number = logging.getLevelNamesMapping().get(name)
    if number is None:
        raise ValueError(f"BRIEFLY_LOG_LEVEL 取值无效：{name}，可选 DEBUG、INFO、WARNING、ERROR")
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, "_briefly", False)]:
        root.removeHandler(handler)
        handler.close()
    handlers: list[logging.Handler] = [_ConsoleHandler(stream)]
    path = config.LOG_FILE if log_file is None else log_file
    if path:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path, encoding="utf-8", delay=True))
    formatter = JsonFormatter()
    for handler in handlers:
        handler.setFormatter(formatter)
        handler._briefly = True
        root.addHandler(handler)
    root.setLevel(max(number, logging.WARNING))
    logging.getLogger("briefly").setLevel(number)
    return name


def _incoming_request_id(scope) -> str | None:
    for key, value in scope["headers"]:
        if key == b"x-request-id":
            text = value.decode("latin-1")
            return text if _VALID_REQUEST_ID.fullmatch(text) else None
    return None


class RequestContextMiddleware:
    """请求追踪号、统一错误格式的兜底：纯 ASGI 中间件，放在所有中间件的最外层。

    - 沿用合法的 X-Request-ID，否则生成新的；响应头都带上，路由里的日志自动带上。
    - 所有 JSON 错误响应都在这里补上 request_id，各个异常处理器不用分别处理，以后新增的也不会漏。
    - 未处理的异常在这里兜底：FastAPI 的 Exception 处理器排在用户中间件外层，响应带不上追踪号，日志也拿不到它。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = _incoming_request_id(scope) or secrets.token_hex(8)
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status: int | None = None
        sent_start = False
        held_start = None  # 错误响应的开头先留着，等读完响应体补上 request_id 再发
        held_body = bytearray()

        async def wrapped_send(message) -> None:
            nonlocal status, sent_start, held_start
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = MutableHeaders(scope=message)
                headers[REQUEST_ID_HEADER] = request_id
                if status >= 400 and headers.get("content-type", "").startswith("application/json"):
                    held_start = message
                    return
                sent_start = True
            elif message["type"] == "http.response.body" and held_start is not None:
                held_body.extend(message.get("body", b""))
                if message.get("more_body"):
                    return
                body = _with_request_id(bytes(held_body), request_id)
                MutableHeaders(scope=held_start)["content-length"] = str(len(body))
                start, held_start = held_start, None
                sent_start = True
                await send(start)
                await send({"type": "http.response.body", "body": body})
                return
            await send(message)

        try:
            await self.app(scope, receive, wrapped_send)
        except Exception:
            log.exception("未处理的异常：%s %s", scope["method"], scope["path"])
            if sent_start:  # 响应已经开始发了，只能断开连接
                status = 500
                raise
            status = 500
            body = json.dumps({"detail": INTERNAL_ERROR, "code": "internal_error", "request_id": request_id},
                              ensure_ascii=False).encode()
            await send({"type": "http.response.start", "status": 500, "headers": [
                (b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                (REQUEST_ID_HEADER.lower().encode(), request_id.encode()),
            ]})
            await send({"type": "http.response.body", "body": body})
        finally:
            if status != 200 or scope["path"] not in ("/healthz", "/readyz"):  # 探活一直成功时不刷日志
                access_log.log(
                    logging.ERROR if status is None or status >= 500 else logging.INFO,
                    "请求完成", extra=dict(method=scope["method"], path=scope["path"], status=status,
                                       duration_ms=round((time.perf_counter() - started) * 1000)),
                )  # 只记路径：查询参数里可能有搜索词和授权码
            request_id_var.reset(token)


def _with_request_id(body: bytes, request_id: str) -> bytes:
    try:
        data = json.loads(body)
    except ValueError:
        return body
    if not isinstance(data, dict):
        return body
    data.setdefault("request_id", request_id)
    return json.dumps(data, ensure_ascii=False).encode()
