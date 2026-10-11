"""模型实例的唯一出口（OpenAI 兼容接口），服务商、地址、模型由 .env 决定。"""
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from . import config


def create_chat_model() -> BaseChatModel:
    if not config.API_KEY or not config.MODEL:
        raise RuntimeError("请在 .env 中设置 BRIEFLY_API_KEY 和 BRIEFLY_MODEL（可选 BRIEFLY_BASE_URL）")
    return ChatOpenAI(
        model=config.MODEL,
        api_key=config.API_KEY,
        base_url=config.BASE_URL,
        max_tokens=config.MAX_TOKENS,
    )
