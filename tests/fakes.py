"""测试用的假聊天模型：不调用真实 API。"""
import threading
from collections.abc import Callable
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class FakeModel(GenericFakeChatModel):
    """按顺序返回预设响应，并记录每次调用时收到的消息。"""

    calls: list = []

    def bind_tools(self, tools: Any, **kwargs: Any) -> "FakeModel":
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


class FunctionModel(BaseChatModel):
    """按输入内容决定回复的假模型：并发调用时顺序不固定，不能按顺序预设回复。

    respond(messages) 返回 AIMessage，或抛出异常模拟接口出错。线程安全地记录每次调用。
    """

    respond: Callable[[list[BaseMessage]], AIMessage]
    calls: list = []

    @property
    def _llm_type(self) -> str:
        return "function-fake"

    def bind_tools(self, tools: Any, **kwargs: Any) -> "FunctionModel":
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        with _lock:
            self.calls.append(list(messages))
        message = self.respond(list(messages))
        message.usage_metadata = {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}
        return ChatResult(generations=[ChatGeneration(message=message)])


_lock = threading.Lock()


def structured(name: str, **args: Any) -> AIMessage:
    """模拟结构化输出（工具调用方式）的回复。"""
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": "call_1"}])


def fake(*responses: AIMessage) -> FakeModel:
    return FakeModel(messages=iter(responses), calls=[])


def text_msg(text: str) -> AIMessage:
    return AIMessage(text)


def tool_msg(name: str, args: dict, id: str = "t1") -> AIMessage:
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": id}])


class FakeFeishu:
    """模拟飞书开放平台：按 (方法, 路径) 登记响应，记录每次请求。不访问网络。

    routes 的值可以是 dict（作为 data 返回）、callable(request) -> dict | httpx.Response、
    或 list（按顺序依次返回，用完后重复最后一个）。未登记的路径返回 404。
    """

    def __init__(self, routes: dict | None = None) -> None:
        import httpx

        self.routes: dict = dict(routes or {})
        self.requests: list = []
        self.token_calls = 0
        self.transport = httpx.MockTransport(self._handle)

    def client(self, **kwargs):
        from briefly.feishu.client import FeishuClient

        return FeishuClient("cli_test", "secret", transport=self.transport, sleep=lambda s: None, **kwargs)

    def calls(self, method: str, path: str) -> list:
        return [r for r in self.requests if r.method == method and r.url.path == path]

    def _handle(self, request):
        import httpx

        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            self.token_calls += 1
            return httpx.Response(200, json={"code": 0, "msg": "ok", "tenant_access_token": f"t-{self.token_calls}", "expire": 7200})
        self.requests.append(request)
        route = self.routes.get((request.method, request.url.path))
        if route is None:
            return httpx.Response(404, json={"code": 404, "msg": f"no route {request.method} {request.url.path}"})
        if isinstance(route, list):
            route = route.pop(0) if len(route) > 1 else route[0]
        result = route(request) if callable(route) else route
        if isinstance(result, httpx.Response):
            return result
        return httpx.Response(200, json={"code": 0, "msg": "success", "data": result})

    @staticmethod
    def body(request) -> dict:
        import json as _json

        return _json.loads(request.content or b"{}")


def feishu_error(code: int, msg: str = "error", status: int = 400):
    import httpx

    return httpx.Response(status, json={"code": code, "msg": msg})
