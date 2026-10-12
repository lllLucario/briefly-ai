"""Token 计数：统一接口，后端可替换，用 API 返回的实际用量校准。

后端：
- heuristic：按字符类别估算。LangChain 的 count_tokens_approximately 默认按 4 字符 ≈ 1 token，是按英文设计的，
  中文会低估约 2.3 倍。系数来自 deepseek-flash 实测（API 返回的 usage）：中文约 1.7 字符/token，英文约 4.1。
- tiktoken：OpenAI 模型用（gpt-*、o1/o3/o4 等），按模型名取编码。不把它当成其他服务商的通用 tokenizer。

无法精确计数（heuristic、图片等非文本内容）时，用 calibrate() 记下的“实际 / 估算”比例往保守方向放大，
预算本身另留安全余量（config.SAFETY_MARGIN_TOKENS）。
"""
import json
import logging
import re
import threading
from functools import lru_cache

from . import config

log = logging.getLogger(__name__)

CJK_CHARS_PER_TOKEN = 1.7
OTHER_CHARS_PER_TOKEN = 4.0
PER_MESSAGE_OVERHEAD = 4  # 角色标记等
IMAGE_TOKENS = 1500  # 文本 tokenizer 量不了图片，按固定值保守计
_OPENAI_MODEL = re.compile(r"^(gpt-|o\d|text-embedding|chatgpt)", re.I)


def _is_cjk(ch: str) -> bool:
    return "一" <= ch <= "鿿" or "　" <= ch <= "〿" or "＀" <= ch <= "￯"


def estimate_tokens(text: str) -> int:
    """按实测系数估算（heuristic 后端）。"""
    cjk = sum(map(_is_cjk, text))
    return round(cjk / CJK_CHARS_PER_TOKEN + (len(text) - cjk) / OTHER_CHARS_PER_TOKEN)


def _tiktoken_counter(model: str):
    try:
        import tiktoken
    except ImportError:
        return None
    try:
        encoding = tiktoken.encoding_for_model(model)
    except KeyError:
        return None
    return lambda text: len(encoding.encode(text, disallowed_special=()))


class TokenCounter:
    """统一的计数接口：Context Builder、工具预算、压缩、Map-Reduce 共用。

    count_* 返回原始估算；budgeted() 把估算按校准比例放大，用于预算判断。
    """

    def __init__(self, model: str = "", backend: str | None = None) -> None:
        backend = (backend or config.TOKENIZER).lower()
        self.model = model
        self._encode = None
        if backend == "tiktoken" or (backend == "auto" and _OPENAI_MODEL.match(model or "")):
            self._encode = _tiktoken_counter(model)
            if self._encode is None and backend == "tiktoken":
                log.warning("tiktoken 不可用或不认识模型 %s，改用估算", model)
        self.backend = "tiktoken" if self._encode else "heuristic"
        self.scale = 1.0  # 实际 / 估算，只取 >= 1（低估才危险）
        self._lock = threading.Lock()
        self._count_text = lru_cache(maxsize=4096)(self._encode or estimate_tokens)

    @property
    def exact(self) -> bool:
        return self.backend != "heuristic"

    def count_text(self, text: str) -> int:
        return self._count_text(text) if text else 0

    def count_content(self, content) -> int:
        """消息正文：字符串，或多模态的内容块列表（文字计数，图片等按固定值）。"""
        if isinstance(content, str):
            return self.count_text(content)
        total = 0
        for block in content or []:
            if isinstance(block, str):
                total += self.count_text(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                total += self.count_text(block.get("text", ""))
            elif isinstance(block, dict) and block.get("type") in {"image", "image_url"}:
                total += IMAGE_TOKENS
            else:
                total += self.count_text(json.dumps(block, ensure_ascii=False, default=str))
        return total

    def count_message(self, message) -> int:
        total = self.count_content(message.content) + PER_MESSAGE_OVERHEAD
        for call in getattr(message, "tool_calls", None) or []:
            total += self.count_text(call.get("name", "")) + self.count_text(
                json.dumps(call.get("args", {}), ensure_ascii=False, default=str)
            )
        return total

    def count_messages(self, messages) -> int:
        return sum(self.count_message(m) for m in messages)

    def count_tools(self, tools) -> int:
        """工具定义（名称、描述、参数 schema）按发给接口的 JSON 计。"""
        if not tools:
            return 0
        from langchain_core.utils.function_calling import convert_to_openai_tool

        total = 0
        for t in tools:
            try:
                schema = convert_to_openai_tool(t)
            except Exception:  # 无法转换的（如服务商内置工具）按字面计
                schema = t if isinstance(t, dict) else str(t)
            total += self.count_text(json.dumps(schema, ensure_ascii=False, default=str)) + PER_MESSAGE_OVERHEAD
        return total

    def budgeted(self, tokens: int) -> int:
        """预算判断用的数：估算 × 校准比例。"""
        return int(tokens * self.scale + 0.999)

    def calibrate(self, estimated: int, actual: int | None) -> None:
        """用 API 返回的实际输入 tokens 校准。样本太小时噪声大，忽略。"""
        if not actual or estimated < 200:
            return
        ratio = actual / estimated
        with self._lock:
            self.scale = min(config.TOKEN_SCALE_MAX, max(1.0, 0.7 * self.scale + 0.3 * ratio))


_default = TokenCounter(backend="heuristic")


def count_message_tokens(messages) -> int:
    """兼容旧接口：按实测系数估算一组消息。"""
    return _default.count_messages(messages)
