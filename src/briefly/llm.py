"""LLM 调用的唯一出口（OpenAI 兼容接口），方便以后加重试/日志/缓存。"""
from openai import OpenAI

from . import config


class LLM:
    def __init__(self) -> None:
        if not config.API_KEY or not config.MODEL:
            raise RuntimeError("请在 .env 中设置 BRIEFLY_API_KEY 和 BRIEFLY_MODEL（可选 BRIEFLY_BASE_URL）")
        self._client = OpenAI(api_key=config.API_KEY, base_url=config.BASE_URL)

    def create(self, messages: list[dict], tools: list[dict] | None = None):
        """返回 assistant message（含 content 和 tool_calls）。"""
        kwargs: dict = dict(model=config.MODEL, messages=messages)
        if tools:
            kwargs["tools"] = tools
        if config.MAX_TOKENS:
            kwargs["max_tokens"] = config.MAX_TOKENS
        return self._client.chat.completions.create(**kwargs).choices[0].message
