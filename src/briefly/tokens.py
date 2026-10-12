"""Token 估算。

LangChain 的 count_tokens_approximately 默认按 4 字符 ≈ 1 token，是按英文设计的，中文会低估约 2.3 倍。
系数来自 deepseek-flash 实测（API 返回的 usage）：中文约 1.7 字符/token，英文约 4.1。
"""
CJK_CHARS_PER_TOKEN = 1.7
OTHER_CHARS_PER_TOKEN = 4.0


def _is_cjk(ch: str) -> bool:
    return "一" <= ch <= "鿿" or "　" <= ch <= "〿" or "＀" <= ch <= "￯"


def estimate_tokens(text: str) -> int:
    cjk = sum(map(_is_cjk, text))
    return round(cjk / CJK_CHARS_PER_TOKEN + (len(text) - cjk) / OTHER_CHARS_PER_TOKEN)
