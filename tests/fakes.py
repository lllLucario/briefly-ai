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
