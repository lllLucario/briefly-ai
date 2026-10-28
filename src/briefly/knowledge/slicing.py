"""本地分词预算切片；原文范围与用于编码的标题前缀分别保存。"""
from dataclasses import dataclass
import re

from ..chunking import parse_sections


@dataclass(frozen=True)
class Slice:
    index: int
    section: str
    start: int
    end: int
    content: str
    embedding_text: str


def token_slices(text, title, markdown, encoder, *, tokens=256):
    if type(tokens) is not int or not 32 <= tokens <= encoder.identity["max_tokens"]:
        raise ValueError("切片预算须至少32token且不超过Embedding输入上限")
    sections = parse_sections(text, markdown)
    config = dict(strategy="title-token-offsets", version=1,
                  parameters=dict(tokens=tokens, overlap=0, include_title=True,
                                  tokenizer_sha256=encoder.identity["artifact_sha256"]["tokenizer.json"]))
    # 章节作为边界，但不额外复制原文标题；层级路径只进入编码前缀。
    boundaries = sorted({0, len(text), *(s.start for s in sections)})
    result = []
    for start, stop in zip(boundaries, boundaries[1:]):
        section = next((s.path for s in reversed(sections) if s.start <= start < s.end), "")
        prefix = title + ("\n" + section if section else "") + "\n"
        if encoder.token_length(prefix) >= tokens - 8:
            raise ValueError("标题或章节过长，切片预算不足；请修改显示标题或增大预算")
        pos = start
        while pos < stop:
            low, high = pos + 1, stop
            best = pos
            while low <= high:
                middle = (low + high) // 2
                if encoder.token_length(prefix + text[pos:middle]) <= tokens:
                    best, low = middle, middle + 1
                else:
                    high = middle - 1
            if best == pos:
                raise ValueError("无法在切片预算内编码原文，未截断")
            if best < stop:
                # 优先保留句末标点；不在旧评测题上调参。
                floor = pos + max(1, (best - pos) * 3 // 4)
                endings = [m.end() + pos for m in re.finditer(r"[。！？；.!?;\n]", text[pos:best]) if m.end() + pos >= floor]
                if endings:
                    best = endings[-1]
                # 最后几个字符只有标点时，向前让出一些正文，避免产生孤立标点块。
                if not any(c.isalnum() for c in text[best:stop]) and best - pos > 8:
                    best -= 8
            content = text[pos:best]
            encoded = prefix + content
            if encoder.token_length(encoded) > tokens:
                raise ValueError("切片超过分词预算，未静默截断")
            if content.strip():
                result.append(Slice(len(result) + 1, section, pos, best, content, encoded))
            pos = best
    if not result:
        raise ValueError("没有可用正文切片")
    return config, result
