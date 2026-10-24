"""模型实例的唯一出口（OpenAI 兼容接口），服务商、地址、模型由 .env 决定。"""
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI

from . import config


class ModelConfigurationError(RuntimeError):
    """模型配置缺失；可直接向用户显示，不包含配置值。"""


def create_chat_model(*, max_retries: int | None = None, timeout: float | None = None) -> BaseChatModel:
    missing = [name for name, value in (
        ("BRIEFLY_API_KEY（或 OPENAI_API_KEY）", config.API_KEY),
        ("BRIEFLY_MODEL", config.MODEL),
    ) if not value or not value.strip()]
    if missing:
        raise ModelConfigurationError(
            f"模型配置不完整：缺少 {'、'.join(missing)}。请在 .env 中设置后重启服务；材料管理等不需要模型的功能仍可使用。"
        )
    # 必须在创建SDK客户端之前设置；创建后修改模型字段不会重建连接层。
    options = {}
    if max_retries is not None:
        options["max_retries"] = max_retries
    if timeout is not None:
        options["timeout"] = timeout
    return ChatOpenAI(
        model=config.MODEL,
        api_key=config.API_KEY,
        base_url=config.BASE_URL,
        max_tokens=config.MAX_TOKENS,
        extra_body=_thinking_body(),
        **options,
    )


def _thinking_body() -> dict | None:
    """DeepSeek 默认开思考模式。开着时带工具的多轮请求必须回传每轮的 reasoning_content，
    ChatOpenAI 不回传会报 400；思考也让每次调用更慢更贵。所以默认显式关闭。"""
    if config.THINKING is None:
        return None
    return {"thinking": {"type": "enabled" if config.THINKING else "disabled"}}
