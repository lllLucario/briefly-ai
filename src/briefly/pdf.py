"""PDF → 带标题的 Markdown。

流程：pypdf 逐页提取文字行（带字号和位置）→ Layout Cleaner 清理 → 判断扫描件
→ 识别标题（字号 → 书签；都没有时交给 chunking 按中文编号规则猜）→ 合并排版换行 → 输出。
"""
import math
import re
from collections import Counter
from io import BytesIO

from pypdf import PdfReader

from .layout_cleaner import Line, clean_pages, join_text, merge_wrapped, scanned_pages
from .loaders import LoadedDocument, escape_heading_marks

HEADING_SIZE_RATIO = 1.15  # 字号达到正文的这个倍数才可能是标题
SIZE_TOLERANCE = 0.3  # 字号差在这个范围内算同一级
MAX_HEADING_CHARS = 80
WRAPPED_HEADING_GAP = 1.6  # 同一标题拆成多行时，行间距不超过字号的这个倍数
SCANNED_DOC_RATIO = 0.8  # 这个比例以上的页面没有文字，整份视为扫描件
MAX_REPORT_ITEMS = 20


def load_pdf(data: bytes) -> LoadedDocument:
    reader = PdfReader(BytesIO(data))
    pages = [_extract_lines(page, i) for i, page in enumerate(reader.pages)]
    pages, removed = clean_pages(pages)

    empty = scanned_pages(pages)
    total = len(pages)
    if not total or len(empty) >= total * SCANNED_DOC_RATIO:
        raise ValueError(f"这是扫描版 PDF（{total} 页中 {len(empty)} 页没有可提取的文字），需要 OCR，目前暂不支持")

    if _mark_by_font_size(pages):
        structure = "font_size"
    elif _mark_by_bookmarks(reader, pages):
        structure = "bookmarks"
    else:
        structure = None  # 交给 chunking 按规则猜

    lines = merge_wrapped(pages)
    text = _render(lines)
    report = {
        "parser": "pypdf",
        "pages": total,
        "structure": structure,
        "removed_lines": len(removed),
        "removed": _summarize(removed),
        "warnings": [f"第 {_page_list(empty)} 页几乎没有文字（可能是图片、扫描页或封面），这些页的内容可能缺失"]
        if empty
        else [],
    }
    return LoadedDocument(text, markdown=structure is not None, report=report)


def _extract_lines(page, index: int) -> list[Line]:
    """用 pypdf 的 visitor 取每段文字的字号和纵坐标，按纵坐标拼成行。"""
    runs: list[tuple[str, float, float]] = []

    def visit(text, cm, tm, font, size):
        if text:
            scale = math.hypot(tm[0], tm[1]) * math.hypot(cm[0], cm[1])
            y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
            runs.append((text, (size or 0) * scale, y))

    page.extract_text(visitor_text=visit)

    lines: list[Line] = []
    sizes: Counter = Counter()

    def flush(line: Line | None) -> None:
        if line and line.text.strip():
            line.size = sizes.most_common(1)[0][0] if sizes else 0.0
            lines.append(line)
        sizes.clear()

    current: Line | None = None
    for text, size, y in runs:
        for i, part in enumerate(text.split("\n")):
            if current is None or i > 0 or abs(y - current.y) > max(size, 1.0) * 0.5:
                flush(current)
                current = Line("", 0.0, y, index)
            current.text += part
            if part.strip():
                sizes[round(size, 1)] += len(part.strip())
    flush(current)
    return lines


def _heading_like(text: str) -> bool:
    return bool(re.search(r"[^\W\d_]", text)) and len(text) <= MAX_HEADING_CHARS


def _mark_by_font_size(pages: list[list[Line]]) -> bool:
    """字号明显大于正文的短行视为标题；字号越大级别越高。"""
    chars: Counter = Counter()
    for line in (l for page in pages for l in page):
        chars[round(line.size, 1)] += len(line.text)
    if not chars:
        return False
    body = chars.most_common(1)[0][0]
    if body <= 0:
        return False

    for p, page in enumerate(pages):
        merged: list[Line] = []
        for line in page:
            candidate = line.size >= body * HEADING_SIZE_RATIO and _heading_like(line.text)
            prev = merged[-1] if merged else None
            # 一个标题被排版拆成多行：同字号、行距正常的连续候选行合并；间距大的是两个相邻标题
            if (
                candidate
                and prev
                and prev.heading
                and abs(prev.size - line.size) <= SIZE_TOLERANCE
                and 0 < prev.y - line.y <= line.size * WRAPPED_HEADING_GAP
            ):
                prev.text = join_text(prev.text, line.text)
                prev.y = line.y
                continue
            line.heading = 1 if candidate else 0
            merged.append(line)
        for line in merged:
            if line.heading and len(line.text) > MAX_HEADING_CHARS:
                line.heading = 0
        heads = [l for l in merged if l.heading]
        # 目录页：大半行都是“标题”，只保留第一行（“目录”本身）
        if len(heads) >= 5 and len(heads) >= len(merged) * 0.5:
            for line in heads[1:]:
                line.heading = 0
        pages[p] = merged

    heads = [l for page in pages for l in page if l.heading]
    if not heads:
        return False
    # 封面：第一页上比正文所有标题都大的字（书名、品牌名）不作为标题
    later = [l.size for l in heads if l.page > 0]
    if later:
        for line in pages[0]:
            if line.heading and line.size > max(later) + SIZE_TOLERANCE:
                line.heading = 0

    levels: dict[float, int] = {}
    level, previous = 0, None
    for size in sorted({l.size for l in heads if l.heading}, reverse=True):
        if previous is None or previous - size > SIZE_TOLERANCE:
            level += 1
        levels[size] = min(level, 6)
        previous = size
    for line in heads:
        if line.heading:
            line.heading = levels[line.size]
    return any(l.heading for l in heads)


def _outline(reader: PdfReader) -> list[tuple[int, str, int]]:
    """读书签：[(深度, 标题, 页码)]。书签损坏、无意义（纯数字）时返回空。"""

    def walk(items, depth):
        for item in items:
            if isinstance(item, list):
                yield from walk(item, depth + 1)
            else:
                title = re.sub(r"\s+", " ", item.title.replace("\x00", "")).strip()
                if re.search(r"[^\W\d_]", title):
                    yield depth, title, reader.get_destination_page_number(item)

    try:
        items = list(walk(reader.outline, 0))
    except Exception:
        return []
    return items if len(items) >= 2 else []


def _mark_by_bookmarks(reader: PdfReader, pages: list[list[Line]]) -> bool:
    items = _outline(reader)
    if not items:
        return False

    def norm(s):
        return re.sub(r"\s+", "", s).lower()

    for depth, title, p in items:
        if not 0 <= p < len(pages):
            continue
        level = min(depth + 1, 6)
        match = next((l for l in pages[p] if not l.heading and norm(l.text) == norm(title)), None)
        if match:
            match.heading = level
        else:  # 书签标题不在页面文字里（如扫描页），在该页开头补一个标题
            pages[p].insert(0, Line(title, 0.0, 0.0, p, level))
    return True


def _render(lines: list[Line]) -> str:
    out: list[str] = []
    for line in lines:
        if line.heading:
            out += ["", f"{'#' * line.heading} {line.text}"]
        else:
            out.append(escape_heading_marks(line.text))
    return "\n".join(out).strip()


def _summarize(removed: list[tuple[str, str]]) -> list[dict]:
    counts: Counter = Counter()
    samples: dict = {}
    for reason, text in removed:
        key = (reason, re.sub(r"\d+", "0", text))
        counts[key] += 1
        samples.setdefault(key, text)
    return [
        {"reason": reason, "text": samples[(reason, k)][:60], "count": c}
        for (reason, k), c in counts.most_common(MAX_REPORT_ITEMS)
    ]


def _page_list(pages: list[int]) -> str:
    shown = "、".join(str(p + 1) for p in pages[:10])
    return shown + (f" 等 {len(pages)} 页" if len(pages) > 10 else "")
