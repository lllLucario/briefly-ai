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


def _tables(text):
    lines, pos, groups, group = text.splitlines(keepends=True), 0, [], []
    for line in [*lines, ""]:
        if line.lstrip().startswith("|"):
            group.append((pos, pos + len(line), line))
        else:
            if len(group) >= 2 and re.fullmatch(r"[\s|:-]+", group[1][2]):
                groups.append((group[0][0], group[-1][1], group[1][1], "".join(x[2] for x in group[:2]), group))
            group = []
        pos += len(line)
    return groups


def _fit_end(text, start, stop, prefix, encoder, tokens):
    low, high, best = start + 1, stop, start
    while low <= high:
        middle = (low + high) // 2
        if encoder.token_length(prefix + text[start:middle]) <= tokens:
            best, low = middle, middle + 1
        else:
            high = middle - 1
    return best


def _protected(text, report):
    regions = []
    textbox = (report or {}).get("textbox_markdown_range")
    if textbox:
        regions.append((textbox["start"], textbox["end"], "graphic"))
    for match in re.finditer(r"(?ms)^```[^\n]*\n.*?^```[^\n]*(?:\n|$)", text):
        regions.append((match.start(), match.end(), "graphic"))
    for match in re.finditer(r"(?m)^.*!\[[^\]]*\]\([^\n]+\).*(?:\n|$)", text):
        regions.append((match.start(), match.end(), "graphic"))
    merged = []
    for start, end, kind in sorted(regions):
        if not 0 <= start < end <= len(text):
            raise ValueError("图形来源范围无效，未切片")
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]), kind)
        else:
            merged.append((start, end, kind))
    return merged


def token_slices(text, title, markdown, encoder, *, tokens=512, overlap=None,
                 context="", report=None):
    """标题/段落优先；正文有界重叠，完整表格行与已知图形范围不滑窗。

    content始终是原文连续范围；只有embedding_text加入标题路径/上下文/续表表头。
    """
    if type(tokens) is not int or not 32 <= tokens <= encoder.identity["max_tokens"]:
        raise ValueError("切片预算须至少32token且不超过Embedding输入上限")
    if overlap is None:
        overlap = min(50, tokens // 4)
    if type(overlap) is not int or not 0 <= overlap < tokens or not isinstance(context, str):
        raise ValueError("重叠预算须为非负整数且小于切片预算，上下文须为文字")
    sections = parse_sections(text, markdown)
    tables = _tables(text)
    graphics = _protected(text, report)
    # 图形范围可能含表格文本，以原图形整体为单位，不能拆成正文或独立行。
    tables = [t for t in tables if not any(a < t[1] and t[0] < b for a, b, _ in graphics)]
    structures = sorted([*( (t[0], t[1], "table") for t in tables), *graphics])
    config = dict(strategy="title-token-offsets", version=4,
                  parameters=dict(tokens=tokens, overlap=overlap, include_title=True,
                                  context=context, structure_policy="rows-and-graphics-no-overlap",
                                  protected_ranges=[dict(start=a, end=b) for a, b, _ in graphics],
                                  tokenizer_sha256=encoder.identity["artifact_sha256"]["tokenizer.json"]))
    boundaries = {0, len(text), *(s.start for s in sections)}
    boundaries.update(n for a, b, _ in structures for n in (a, b))
    boundaries = sorted(n for n in boundaries if not any(a < n < b for a, b, _ in structures))
    result = []

    def append(start, end, prefix, section):
        content = text[start:end]
        encoded = prefix + content
        if encoder.token_length(encoded) > tokens:
            raise ValueError("切片超过分词预算，未静默截断")
        if content.strip():
            result.append(Slice(len(result) + 1, section, start, end, content, encoded))

    for start, stop in zip(boundaries, boundaries[1:]):
        graphic = any(a <= start < b for a, b, _ in graphics)
        section = "" if graphic else next((s.path for s in reversed(sections) if s.start <= start < s.end), "")
        prefix = title + ("\n" + section if section else "") + ("\n" + context if context else "") + "\n"
        if encoder.token_length(prefix) >= tokens - 8:
            raise ValueError("标题、章节或补充上下文过长，切片预算不足")
        if graphic:
            if encoder.token_length(prefix + text[start:stop]) > tokens:
                raise ValueError("图形/文本框整体超过切片预算，请核对结构；未套用正文滑窗")
            append(start, stop, prefix, section)
            continue
        table = next((t for t in tables if t[0] == start), None)
        if table:
            pos = start
            while pos < stop:
                current_prefix = prefix + (table[3] if pos >= table[2] else "")
                row_ends = [r[1] for r in table[4] if r[1] > pos]
                # 首块必须同时容纳完整表头与至少一个数据行（如存在）。
                minimum = next((r[1] for r in table[4][2:] if r[1] > pos), stop)
                best = pos
                for end in row_ends:
                    if encoder.token_length(current_prefix + text[pos:end]) > tokens:
                        break
                    best = end
                if best < minimum:
                    raise ValueError("表格单行或表头超过切片预算，请增大预算或人工拆分；未截断行列关系")
                append(pos, best, current_prefix, section)
                pos = best
            continue
        pos = start
        paragraph_ends = [m.end() + start for m in re.finditer(r"\n[ \t]*\n", text[start:stop])]
        while pos < stop:
            best = _fit_end(text, pos, stop, prefix, encoder, tokens)
            if best == pos:
                raise ValueError("无法在切片预算内编码原文，未截断")
            if best < stop:
                paragraph = [n for n in paragraph_ends if pos + (best - pos) // 2 <= n <= best]
                if paragraph:
                    best = paragraph[-1]
                else:
                    floor = pos + max(1, (best - pos) * 3 // 4)
                    endings = [m.end() + pos for m in re.finditer(r"[。！？；.!?;\n]", text[pos:best]) if m.end() + pos >= floor]
                    if endings:
                        best = endings[-1]
                if not any(c.isalnum() for c in text[best:stop]) and best - pos > 8:
                    best -= 8
            append(pos, best, prefix, section)
            if best == stop:
                break
            next_pos = best
            if overlap:
                low, high = pos + 1, best
                while low <= high:
                    middle = (low + high) // 2
                    if encoder.token_length(text[middle:best]) <= overlap:
                        next_pos, high = middle, middle - 1
                    else:
                        low = middle + 1
                # 让新正文至少占一半，极小自定义预算也必须持续前进。
                next_pos = max(next_pos, pos + max(1, (best - pos) // 2))
            pos = next_pos
    if not result:
        raise ValueError("没有可用正文切片")
    return config, result
