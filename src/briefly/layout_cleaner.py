"""Layout Cleaner：清理 PDF 逐页提取出的文字行。

处理乱码、页眉页脚、水印、页码、排版造成的换行。只用于 PDF：只有 PDF 有“页”和排版换行。
原则是宁可少删，不可误删正文；每一处删除都记录下来，写进材料的解析报告。
"""
import re
from collections import Counter
from dataclasses import dataclass

EDGE_LINES = 2  # 每页顶部、底部各看几行
EDGE_REPEAT_RATIO = 0.5  # 页面边缘的行，出现在这个比例以上的页面即视为页眉页脚
ANY_REPEAT_RATIO = 0.8  # 页面中间的行要求更高，避免误删正文里常见的短语
MIN_REPEAT_PAGES = 3
GARBLED_RATIO = 0.3
SCANNED_PAGE_CHARS = 20  # 清理后有效字数少于该值的页面视为没有文字
WRAP_WIDTH_RATIO = 0.85  # 行长达到正常行宽的这个比例，才认为是排版造成的换行
MIN_LINES_FOR_WIDTH = 5
PROSE_FULL_LINE_RATIO = 0.4  # 至少这么多行接近行宽，才认为该页是成段排版的正文

_LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"})
_PAGE_NUMBER = re.compile(
    r"^(?:[-–—\s]*\d+[-–—\s]*|第\s*\d+\s*页(?:\s*[/，,]?\s*共\s*\d+\s*页)?|page\s*\d+(?:\s*of\s*\d+)?|\d+\s*/\s*\d+)$",
    re.IGNORECASE,
)
_SENTENCE_END = tuple("。！？；：.!?;:”’」』）)")
_LIST_START = re.compile(r"^\s*(?:[•·▪◦\-*+]|\d+[.、)）]|[（(]\d+[）)]|[一二三四五六七八九十]+、|\\?#|\|)")


@dataclass
class Line:
    text: str
    size: float = 0.0  # 字号
    y: float = 0.0  # 页面纵坐标，越大越靠上
    page: int = 0  # 从 0 开始
    heading: int = 0  # 标题级别，0 表示正文
    kind: str = ""  # "table"：已按行列还原的表格（Markdown）；"columnar"：分栏排列、疑似无框线表格的文字行
    columns: tuple[float, ...] = ()  # 分栏行第二栏起各栏的起始横坐标


def is_garbage_char(ch: str) -> bool:
    code = ord(ch)
    return (
        code == 0
        or 0xD800 <= code <= 0xDFFF  # 代理字符：编码解错的典型痕迹
        or 0xE000 <= code <= 0xF8FF  # 私用区
        or ch == "�"
        or (code < 32 and ch not in "\t\n")
    )


def garbled_ratio(text: str) -> float:
    chars = [c for c in text if not c.isspace()]
    return sum(map(is_garbage_char, chars)) / len(chars) if chars else 0.0


def _repeat_key(text: str) -> str:
    """页码等数字会变，统计重复前先统一成 0。"""
    return re.sub(r"\s+", "", re.sub(r"\d+", "0", text))


def _edge_indexes(page: list[Line]) -> set[int]:
    order = sorted(range(len(page)), key=lambda i: -page[i].y)  # 从上到下；y 都相同时保持原顺序
    return set(order[:EDGE_LINES] + order[-EDGE_LINES:])


def clean_pages(pages: list[list[Line]]) -> tuple[list[list[Line]], list[tuple[str, str]]]:
    """去掉乱码行、重复行（页眉页脚、水印）和页码。返回 (清理后的页, [(原因, 被删的文字)])。"""
    removed: list[tuple[str, str]] = []
    cleaned: list[list[Line]] = []
    for page in pages:
        kept = []
        for line in page:
            line.text = line.text.translate(_LIGATURES).strip()
            if not line.text:
                continue
            if garbled_ratio(line.text) > GARBLED_RATIO:
                removed.append(("乱码", line.text))
                continue
            line.text = "".join(c for c in line.text if not is_garbage_char(c)).strip()
            if line.text:
                kept.append(line)
        cleaned.append(kept)

    n = len(cleaned)
    edges = [_edge_indexes(page) for page in cleaned]
    if n >= MIN_REPEAT_PAGES:
        edge_count = Counter(k for page, idx in zip(cleaned, edges) for k in {_repeat_key(page[i].text) for i in idx})
        any_count = Counter(k for page in cleaned for k in {_repeat_key(line.text) for line in page})
        edge_min = max(MIN_REPEAT_PAGES, n * EDGE_REPEAT_RATIO)
        any_min = max(MIN_REPEAT_PAGES, n * ANY_REPEAT_RATIO)
    result = []
    for page, idx in zip(cleaned, edges):
        kept = []
        repeated = []
        page_removed = []
        for i, line in enumerate(page):
            key = _repeat_key(line.text)
            edge = i in idx
            if n >= MIN_REPEAT_PAGES and ((edge and edge_count[key] >= edge_min) or any_count[key] >= any_min):
                repeated.append(line)
                page_removed.append(("重复出现（页眉页脚或水印）", line.text))
            elif edge and _PAGE_NUMBER.match(line.text):
                removed.append(("页码", line.text))
            else:
                kept.append(line)
        # 整页重复可能是合同条款/重复附录，而非水印。不能删空后再冒充扫描件。
        if not kept and sum(len(line.text.replace(" ", "")) for line in repeated) >= SCANNED_PAGE_CHARS:
            kept = [line for line in repeated if not _PAGE_NUMBER.match(line.text)]
            page_removed = [("页码", line.text) for line in repeated if _PAGE_NUMBER.match(line.text)]
        removed.extend(page_removed)
        result.append(kept)
    return result, removed


def scanned_pages(pages: list[list[Line]]) -> list[int]:
    """清理后几乎没有文字的页（从 0 开始）：扫描页、图片页或封面。"""
    return [
        i for i, page in enumerate(pages) if sum(len(l.text.replace(" ", "")) for l in page) < SCANNED_PAGE_CHARS
    ]


def _is_cjk(ch: str) -> bool:
    return "一" <= ch <= "鿿" or "　" <= ch <= "〿" or "＀" <= ch <= "￯"


def join_text(a: str, b: str) -> str:
    """把被换行拆开的两段接起来：中文直接拼，英文补空格。

    英文行末的连字符保留并直接拼接：分不清是排版断词（lace-making）还是复合词（fear-stricken），
    去掉可能改错原文，保留最多是多一个连字符。
    """
    a, b = a.rstrip(), b.lstrip()
    if not a or not b:
        return a + b
    if re.search(r"[A-Za-z]-$", a) and b[0].islower():
        return a + b
    if _is_cjk(a[-1]) or _is_cjk(b[0]):
        return a + b
    return f"{a} {b}"


def _typical_widths(pages: list[list[Line]]) -> dict[int, float]:
    """每页正常行宽。只有成段排版的页（相当一部分行都接近行宽）才有；诗歌、列表等行长参差的页没有，不做合并。"""
    widths = {}
    for page in pages:
        lengths = sorted(len(l.text) for l in page if not l.heading and l.kind != "table")
        if len(lengths) < MIN_LINES_FOR_WIDTH:
            continue
        width = lengths[int(len(lengths) * 0.8)]
        full = sum(n >= width * WRAP_WIDTH_RATIO for n in lengths)
        if full >= len(lengths) * PROSE_FULL_LINE_RATIO:
            widths[page[0].page] = width
    return widths


def merge_wrapped(pages: list[list[Line]]) -> list[Line]:
    """合并排版造成的换行：上一行不以句末标点结尾、且长度接近正常行宽，才与下一行合并。

    要求接近行宽，是为了不误合并诗歌、短列表、标题这类本来就短的行。可跨页合并。
    """
    widths = _typical_widths(pages)
    out: list[Line] = []
    tail_len = 0  # 上一条输出中最后一个物理行的长度
    for line in (l for page in pages for l in page):
        prev = out[-1] if out else None
        width = widths.get(prev.page) if prev else None
        if (
            prev
            and width
            and not prev.heading
            and not line.heading
            and not prev.kind
            and not line.kind
            and abs(prev.size - line.size) <= 0.5
            and tail_len >= width * WRAP_WIDTH_RATIO
            and not prev.text.endswith(_SENTENCE_END)
            and not _LIST_START.match(line.text)
        ):
            prev.text = join_text(prev.text, line.text)
            prev.page = line.page
        else:
            out.append(Line(line.text, line.size, line.y, line.page, line.heading, line.kind, line.columns))
        tail_len = len(line.text)
    return out
