"""PDF → 带标题的 Markdown。

流程：pdfplumber 逐页识别带框线的表格、提取表格外的文字行（按字符坐标排序，带字号和位置）
→ Layout Cleaner 清理 → 判断扫描件 → 识别标题（字号 → 书签；都没有时交给 chunking 按中文编号规则猜）
→ 放回表格 → 合并排版换行 → 输出。

表格质量：识别出的表格按行列输出为带表头的 Markdown 表；有合并单元格（行列对应可能有歧义）的表格，
以及表格外分栏排列、疑似无框线表格的连续文字行，记入 table_partial_ranges（Markdown 偏移），
单元格归属限制只加在这些范围上，正文段落不受限。
"""
import re
from collections import Counter
from io import BytesIO

import pdfplumber
from pypdf import PdfReader

from .layout_cleaner import Line, clean_pages, join_text, merge_wrapped, scanned_pages
from .loaders import LoadedDocument, escape_heading_marks

HEADING_SIZE_RATIO = 1.15  # 字号达到正文的这个倍数才可能是标题
SIZE_TOLERANCE = 0.3  # 字号差在这个范围内算同一级
MAX_HEADING_CHARS = 80
WRAPPED_HEADING_GAP = 1.6  # 同一标题拆成多行时，行间距不超过字号的这个倍数
SCANNED_DOC_RATIO = 0.8  # 这个比例以上的页面没有文字，整份视为扫描件
MAX_REPORT_ITEMS = 20
COLUMN_GAP_RATIO = 2.0  # 行内两字之间的空白超过字号的这个倍数，视为分栏
MIN_COLUMNAR_LINES = 2  # 连续这么多行分栏排列、且栏位对齐，才视为疑似无框线表格
COLUMN_ALIGN_TOLERANCE = 3.0  # 栏起点横坐标相差在这个范围内算对齐


def load_pdf(data: bytes) -> LoadedDocument:
    reader = PdfReader(BytesIO(data))
    with pdfplumber.open(BytesIO(data)) as pdf:
        extracted = [_extract_page(page, i) for i, page in enumerate(pdf.pages)]
    pages, removed = clean_pages([lines for lines, _ in extracted])
    tables = [found for _, found in extracted]

    empty = [p for p in scanned_pages(pages) if not tables[p]]
    total = len(pages)
    if not total or len(empty) >= total * SCANNED_DOC_RATIO:
        raise ValueError(f"这是扫描版 PDF（{total} 页中 {len(empty)} 页没有可提取的文字），需要 OCR，目前暂不支持")

    if _mark_by_font_size(pages):
        structure = "font_size"
    elif _mark_by_bookmarks(reader, pages):
        structure = "bookmarks"
    else:
        structure = None  # 交给 chunking 按规则猜
    for p, found in enumerate(tables):  # 表格放回所在位置（纵坐标越大越靠上）
        if found:
            pages[p] = sorted([*pages[p], *found], key=lambda line: -line.y)

    lines = _join_split_tables(merge_wrapped(pages))
    text, ranges = _render(lines)
    count = sum(line.kind == "table" for line in lines)  # 跨页接上的表格算一张
    partial_tables = sum(line.kind == "table" and line.size < 0 for line in lines)
    report = {
        "parser": "pdfplumber",
        "pages": total,
        "structure": structure,
        "removed_lines": len(removed),
        "removed": _summarize(removed),
        "tables": count,
        # 只有识别出的完整表格算还原；合并单元格的表格和疑似无框线表格的范围单独限制
        "table_structure_status": "partial" if ranges else "verified" if count else "not_present",
        "table_partial_ranges": ranges,
        "warnings": [f"第 {_page_list(empty)} 页几乎没有文字（可能是图片、扫描页或封面），这些页的内容可能缺失"]
        if empty
        else [],
    }
    if ranges:
        report["warnings"].append(
            f"有 {partial_tables} 张表格含合并单元格、{len(ranges) - partial_tables} 处文字分栏排列疑似无框线表格，"
            "这些位置的行列对应关系未确认；需核对原件")
    return LoadedDocument(text, markdown=structure is not None or count > 0, report=report)


def _extract_page(page, index: int) -> tuple[list[Line], list[Line]]:
    """返回（表格外的文字行，表格）。表格是一个 Line：kind="table"，text 为 Markdown 表，size<0 表示含合并单元格。"""
    tables: list[Line] = []
    page = page.dedupe_chars()  # 叠印多次做出的“假粗体”只保留一份
    outside = page
    for table in page.find_tables():
        outside = outside.outside_bbox(table.bbox)
        block = _table_markdown(table.extract())
        if block:
            markdown, merged = block
            tables.append(Line(markdown, -1.0 if merged else 0.0, page.height - table.bbox[1], index, kind="table"))
    lines = []
    for item in outside.extract_text_lines(return_chars=True):
        chars = [c for c in item["chars"] if c["text"].strip()]
        if not item["text"].strip() or not chars:
            continue
        sizes = Counter(round(c["size"], 1) for c in chars)
        size = sizes.most_common(1)[0][0]
        columns = _columns(chars, size)
        lines.append(Line(item["text"], size, page.height - item["top"], index,
                          kind="columnar" if columns else "", columns=columns))
    return lines, tables


_LIST_MARK = re.compile(r"^(?:[●•·▪◦■□◆◇★☆\-*+]|\d{1,3}[.、)）]?|[（(]\d{1,3}[）)]|[一二三四五六七八九十]{1,3}、)$")


def _columns(chars: list[dict], size: float) -> tuple[float, ...]:
    """行内被大段空白隔开的第二栏起各栏起点；不分栏时为空。项目符号、编号与正文之间的悬挂缩进不算分栏。"""
    segments, current, starts = [], chars[0]["text"], []
    for a, b in zip(chars, chars[1:]):
        if b["x0"] - a["x1"] > size * COLUMN_GAP_RATIO:
            segments.append(current)
            starts.append(b["x0"])
            current = ""
        current += b["text"]
    segments.append(current)
    if starts and _LIST_MARK.match(segments[0].strip()):
        starts = starts[1:]
    return tuple(round(x, 1) for x in starts)


def _aligned(run: list[Line]) -> bool:
    """连续分栏行中至少两行有栏起点对齐，才像表格；两端对齐正文拉开的字距位置是随机的。"""
    seen: list[tuple[int, float]] = []
    for i, line in enumerate(run):
        for x in line.columns:
            if any(j != i and abs(x - other) <= COLUMN_ALIGN_TOLERANCE for j, other in seen):
                return True
        seen += [(i, x) for x in line.columns]
    return False


def _table_markdown(rows: list[list[str | None]]) -> tuple[str, bool] | None:
    """表格转 Markdown（首行为表头）。合并单元格在 pdfplumber 里是 None，记为空格并返回 merged=True。"""
    rows = [row for row in rows if any(cell and cell.strip() for cell in row)]
    if len(rows) < 2 or max(len(row) for row in rows) < 2:
        return None
    merged = any(cell is None for row in rows for cell in row)

    def cell(value):
        if not value:
            return ""
        text = ""
        for part in value.split("\n"):
            text = join_text(text, part.strip()) if text else part.strip()
        return text.replace("|", "\\|")

    width = max(len(row) for row in rows)
    body = [[cell(value) for value in row] + [""] * (width - len(row)) for row in rows]
    out = ["| " + " | ".join(body[0]) + " |", "|" + "---|" * width]
    out += ["| " + " | ".join(row) + " |" for row in body[1:]]
    return "\n".join(out), merged


def _cells(row: str) -> list[str]:
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", row.strip())[1:-1]]


def _join_split_tables(lines: list[Line]) -> list[Line]:
    """跨页表格：上一页以表格结尾、下一页以列数相同的表格开头时接成一张表。
    续表首行若前面几格为空，是上一页最后一行被分页截断的后半，补进那一行的对应格。"""
    out: list[Line] = []
    for line in lines:
        prev = out[-1] if out else None
        if prev and prev.kind == "table" and line.kind == "table" and line.page == prev.page + 1:
            head, rows = prev.text.split("\n"), line.text.split("\n")
            first = _cells(rows[0])
            if len(first) == len(_cells(head[0])):
                rest = rows[2:]
                if first and not first[0] and any(first):
                    last = _cells(head[-1])
                    head[-1] = "| " + " | ".join(join_text(a, b) if b else a for a, b in zip(last, first)) + " |"
                else:
                    rest = [rows[0], *rest]
                prev.text = "\n".join([*head, *rest])
                prev.size = min(prev.size, line.size)
                prev.page = line.page
                continue
        out.append(line)
    return out


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


def _render(lines: list[Line]) -> tuple[str, list[dict]]:
    """返回 Markdown 文本，以及行列对应未确认的范围（含合并单元格的表格、连续分栏的文字行）。"""
    blocks: list[tuple[str, bool]] = []  # （文字，是否属于未确认范围）
    run: list[Line] = []

    def flush_run():
        if run:
            table = len(run) >= MIN_COLUMNAR_LINES and _aligned(run)
            blocks.append(("\n".join(escape_heading_marks(line.text) for line in run), table))
            run.clear()

    for line in lines:
        if line.kind == "columnar" and not line.heading:
            run.append(line)
            continue
        flush_run()
        if line.heading:
            blocks += [("", False), (f"{'#' * line.heading} {line.text}", False)]
        elif line.kind == "table":
            blocks += [("", False), (line.text, line.size < 0), ("", False)]
        else:
            blocks.append((escape_heading_marks(line.text), False))
    flush_run()
    text = "\n".join(text for text, _ in blocks)
    lead = len(text) - len(text.lstrip())
    ranges, offset = [], 0
    for chunk, flagged in blocks:
        if flagged:
            ranges.append({"start": offset - lead, "end": offset - lead + len(chunk)})
        offset += len(chunk) + 1
    return text.strip(), ranges


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
