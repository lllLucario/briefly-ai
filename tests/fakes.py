"""测试用的假聊天模型：不调用真实 API。"""
from typing import Any

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage


class FakeModel(GenericFakeChatModel):
    """按顺序返回预设响应，并记录每次调用时收到的消息。"""

    calls: list = []

    def bind_tools(self, tools: Any, **kwargs: Any) -> "FakeModel":
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def fake(*responses: AIMessage) -> FakeModel:
    return FakeModel(messages=iter(responses), calls=[])


def text_msg(text: str) -> AIMessage:
    return AIMessage(text)


def tool_msg(name: str, args: dict, id: str = "t1") -> AIMessage:
    return AIMessage("", tool_calls=[{"name": name, "args": args, "id": id}])
