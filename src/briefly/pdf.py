"""PDF → 带标题的 Markdown。

流程：pdfplumber 逐页去掉页面外的文字 → 识别带框线的表格（矢量框线；没有矢量框线、页面以图片为主时
从渲染图里找框线）→ 去掉表格里和表格边上渲染后看不见的文字（跨页合并单元格被裁掉的部分）
→ 提取表格外的文字行（按字符坐标排序，带字号和位置）
→ Layout Cleaner 清理 → 判断扫描件 → 识别标题（字号 → 书签；都没有时交给 chunking 按中文编号规则猜）
→ 放回表格 → 合并排版换行 → 输出。

表格质量：识别出的表格按行列输出为带表头的 Markdown 表；合并单元格按框线几何定位，纵向合并的内容
重复写进它覆盖的每一行，横向合并的其余格留空。缺边框、无法确认范围的合并单元格（多是跨页截断）所在的行，
以及表格外分栏排列、疑似无框线表格的连续文字行，记入 table_partial_ranges（Markdown 偏移），
单元格归属限制只加在这些范围上，正文段落和已确认的表格行不受限。
"""
import re
from collections import Counter
from dataclasses import dataclass
from io import BytesIO

import numpy as np
import pdfplumber
import pypdfium2
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
RENDER_SCALE = 2.0  # 渲染页面判断文字是否可见、找图片里的框线时，每磅的像素数
INK_LEVEL = 200  # 灰度低于这个值算有墨迹
MIN_INK_SIZE = 4.0  # 小于这个字号（磅）的字渲染出来只有几个像素，不按墨迹判断可见性
TABLE_OVERFLOW = 72.0  # 表格上下这个距离（磅）内看不见的字，视为合并单元格被裁掉的溢出文字
RULE_LEVEL = 160  # 灰度低于这个值算框线（图片缩放后的细线是抗锯齿的灰线）
RULE_MIN_LENGTH = 30.0  # 图片里的框线至少这么长（磅）
RULE_MAX_WIDTH = 3.0  # 图片里的框线最多这么粗（磅）；更粗的要足够长（双线、粗线），否则是色块
RULE_MIN_ASPECT = 20  # 粗于 RULE_MAX_WIDTH 的线，长度至少是粗细的这个倍数
RULE_IMAGE_RATIO = 0.2  # 图片占页面这个比例以上，才从渲染图里找框线


def load_pdf(data: bytes) -> LoadedDocument:
    reader = PdfReader(BytesIO(data))
    raster = pypdfium2.PdfDocument(data)
    try:
        with pdfplumber.open(BytesIO(data)) as pdf:
            extracted = [_extract_page(page, i, raster[i]) for i, page in enumerate(pdf.pages)]
    finally:
        raster.close()
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
    text, ranges, columnar = _render(lines)
    count = sum(line.kind == "table" for line in lines)  # 跨页接上的表格算一张
    partial_tables = sum(bool(getattr(line, "unconfirmed_rows", ())) for line in lines)
    report = {
        "parser": "pdfplumber",
        "pages": total,
        "structure": structure,
        "removed_lines": len(removed),
        "removed": _summarize(removed),
        "tables": count,
        # 只有识别出的完整表格算还原；定位不了的合并单元格和疑似无框线表格的范围单独限制
        "table_structure_status": "partial" if ranges else "verified" if count else "not_present",
        "table_partial_ranges": ranges,
        "warnings": [f"第 {_page_list(empty)} 页几乎没有文字（可能是图片、扫描页或封面），这些页的内容可能缺失"]
        if empty
        else [],
    }
    if ranges:
        report["warnings"].append(
            f"有 {partial_tables} 张表格含无法定位或跨页断开的合并单元格、"
            f"{columnar} 处文字分栏排列疑似无框线表格，"
            "这些位置的行列对应关系未确认；需核对原件")
    return LoadedDocument(text, markdown=structure is not None or count > 0, report=report)


@dataclass
class _Table(Line):
    """kind="table" 的行：text 为 Markdown 表（首行为表头）。"""
    unconfirmed_rows: tuple[int, ...] = ()  # 单元格归属未确认的数据行（从 0 起；-1 表示表头）


def _extract_page(page, index: int, raster) -> tuple[list[Line], list[_Table]]:
    """返回（表格外的文字行，表格）。"""
    tables: list[_Table] = []
    deduped = page.dedupe_chars()  # 叠印多次做出的“假粗体”只保留一份
    width, height = deduped.width, deduped.height
    on_page = deduped.filter(lambda obj: _on_page(obj, width, height) if obj["object_type"] == "char"
                             else not _blank_rect(obj))
    settings = None
    found = on_page.find_tables()
    imaged = _image_area(on_page) >= RULE_IMAGE_RATIO * width * height
    ink = _ink(on_page, raster) if found or imaged else None
    if not found and imaged and ink is not None:
        rules = _image_rules(ink)
        if rules:
            horizontal, vertical = rules
            settings = dict(vertical_strategy="explicit", horizontal_strategy="explicit",
                            explicit_vertical_lines=vertical, explicit_horizontal_lines=horizontal)
            found = on_page.find_tables(settings)
    page = on_page
    # 成表的表格里和表格边上看不见的字：跨页合并单元格的文字在两页各画一次、各被裁掉一部分，看不见的那部分不提取
    boxes = [table.bbox for table in found if _table(table, height, index)] if ink is not None else []
    if boxes:
        page = on_page.filter(lambda obj: obj["object_type"] != "char" or not _near(obj, boxes) or _inked(obj, height, ink))
        found = page.find_tables(settings)
    outside = page
    for table in found:
        line = _table(table, page.height, index)
        if line:  # 不成表的（不足两行两列）文字留在正文里
            tables.append(line)
            outside = outside.outside_bbox(table.bbox)
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


def _ink(page, raster):
    """把页面渲染成灰度图（numpy 数组，每磅 RENDER_SCALE 像素）；渲染坐标与 pdfplumber 坐标对不上时返回 None。"""
    w, h = raster.get_size()
    if abs(w - page.width) > 0.5 or abs(h - page.height) > 0.5 or page.bbox[:2] != (0, 0):
        return None
    bitmap = raster.render(scale=RENDER_SCALE, grayscale=True)
    try:
        array = bitmap.to_numpy()
        return array.reshape(array.shape[:2]).copy()
    finally:
        bitmap.close()


def _on_page(char: dict, width: float, height: float) -> bool:
    return char["x1"] > 0 and char["bottom"] > 0 and char["x0"] < width and char["top"] < height


def _blank_rect(obj: dict) -> bool:
    """不描边、填白色的矩形（Excel 导出时给每个单元格铺的底色）看不出边界，不能当作框线；否则合并单元格会被切开。"""
    if obj["object_type"] != "rect" or obj.get("stroke"):
        return False
    if not obj.get("fill"):
        return True
    color = obj.get("non_stroking_color")
    if isinstance(color, (int, float)):
        color = (color,)
    if not isinstance(color, (tuple, list)) or not color or not all(isinstance(v, (int, float)) for v in color):
        return False
    return all(v >= 0.99 for v in color) if len(color) in (1, 3) else len(color) == 4 and all(v <= 0.01 for v in color)


def _near(char: dict, boxes: list[tuple]) -> bool:
    return any(x0 <= char["x0"] and char["x1"] <= x1 and top - TABLE_OVERFLOW <= char["top"]
               and char["bottom"] <= bottom + TABLE_OVERFLOW for x0, top, x1, bottom in boxes)


def _inked(char: dict, height: float, ink) -> bool:
    """渲染后字的位置上有没有墨迹；被裁剪区域截掉、被遮住或白色的字没有。太小、旋转的字不判断，算有。"""
    if not char["text"].strip() or not char.get("upright", True) or char["size"] < MIN_INK_SIZE:
        return True
    # 字框来自字体描述，下降值写错的字体会把字框整体挪到字形下方；同时查按基线推算的字形位置
    baseline = height - char["matrix"][5]
    x0, x1 = max(int(char["x0"] * RENDER_SCALE), 0), max(int(char["x1"] * RENDER_SCALE) + 1, 0)
    for upper, lower in ((char["top"], char["bottom"]), (baseline - char["size"], baseline + char["size"] * 0.25)):
        box = ink[max(int(upper * RENDER_SCALE), 0):max(int(lower * RENDER_SCALE) + 1, 0), x0:x1]
        if box.size and int(box.min()) < INK_LEVEL:
            return True
    return False


def _image_area(page) -> float:
    def overlap(lo, hi, size):
        return max(0.0, min(hi, size) - max(lo, 0))
    return sum(overlap(i["x0"], i["x1"], page.width) * overlap(i["top"], i["bottom"], page.height) for i in page.images)


def _image_rules(ink) -> tuple[list[dict], list[dict]] | None:
    """从渲染图里找框线（框线画在图片里、没有矢量线时）：足够长、足够细的深色横线和竖线。"""
    dark = ink < RULE_LEVEL
    min_length = int(RULE_MIN_LENGTH * RENDER_SCALE)

    def segments(mask):
        """每一像素行里足够长的深色连续段，相邻像素行上重叠的段合成一条线：[(位置, 起, 止)]。"""
        steps = np.diff(np.pad(mask, ((0, 0), (1, 1))).astype(np.int8), axis=1)
        rows, starts = np.nonzero(steps == 1)
        _, ends = np.nonzero(steps == -1)
        lines: list[list[int]] = []  # [首行, 末行, 起, 止]
        for row, start, end in zip(rows.tolist(), starts.tolist(), ends.tolist()):
            if end - start < min_length:
                continue
            for line in lines:
                if line[1] >= row - 1 and start < line[3] and end > line[2]:
                    line[1], line[2], line[3] = row, min(line[2], start), max(line[3], end)
                    break
            else:
                lines.append([row, row, start, end])
        return [((first + last + 1) / 2 / RENDER_SCALE, start / RENDER_SCALE, end / RENDER_SCALE)
                for first, last, start, end in lines
                if last - first + 1 <= max(RULE_MAX_WIDTH * RENDER_SCALE, (end - start) / RULE_MIN_ASPECT)]

    horizontal = [dict(object_type="line", x0=a, x1=b, top=y, bottom=y, width=b - a, height=0.0)
                  for y, a, b in segments(dark)]
    vertical = [dict(object_type="line", x0=x, x1=x, top=a, bottom=b, width=0.0, height=b - a)
                for x, a, b in segments(dark.T)]
    return (horizontal, vertical) if len(horizontal) >= 2 and len(vertical) >= 2 else None


def _table(table, page_height: float, index: int) -> _Table | None:
    """表格转 Markdown（首行为表头）。合并单元格在 pdfplumber 里是 None：按框线几何找覆盖它的单元格，
    纵向合并重复其内容，横向合并留空。没有单元格覆盖的空位（合并单元格缺下边框，多是延续到下一页被截断），
    同一列连续的几格合成一格取文字，这几行记为未确认；表头单元格向下延伸进数据行时，表头和这几行也记为未确认。"""
    raw = table.extract()
    xs = sorted({c[0] for c in table.cells}) + [table.bbox[2]]
    ys = sorted({c[1] for c in table.cells}) + [table.bbox[3]]
    position = {(c[1], c[0]): (ys.index(c[1]), xs.index(c[0])) for c in table.cells}
    unconfirmed: set[int] = set()  # 原始行号；0 是表头
    rows: list[list[str | None]] = []
    for i, row in enumerate(raw):
        filled: list[str | None] = []
        for j, value in enumerate(row):
            if value is None:
                cx, cy = (xs[j] + xs[j + 1]) / 2, (ys[i] + ys[i + 1]) / 2
                owner = next((c for c in table.cells if c[0] <= cx <= c[2] and c[1] <= cy <= c[3]), None)
                if owner is not None:
                    oi, oj = position[(owner[1], owner[0])]
                    value = raw[oi][oj] if oj == j else ""
            filled.append(value)
        rows.append(filled)
    for j in range(len(xs) - 1):  # 没有单元格覆盖的空位
        i = 0
        while i < len(rows):
            if rows[i][j] is not None:
                i += 1
                continue
            end = i
            while end + 1 < len(rows) and rows[end + 1][j] is None:
                end += 1
            box = (xs[j], ys[i], xs[j + 1], ys[end + 1])
            text = table.page.filter(lambda obj: obj["object_type"] == "char" and _centered(obj, box)).extract_text()
            for k in range(i, end + 1):
                rows[k][j] = text
            unconfirmed |= set(range(i, end + 1))
            i = end + 1
    for c in table.cells:  # 表头单元格向下延伸进数据行：表头与数据的分界无从确认
        if ys.index(c[1]) == 0 and c[3] > ys[1] + 1:
            unconfirmed |= {0, *(i for i in range(1, len(ys) - 1) if ys[i] < c[3] - 1)}
    cells = [[_cell(value) for value in row] for row in rows]

    kept = [i for i, row in enumerate(cells) if any(row)]
    if len(kept) < 2 or len(cells[0]) < 2:
        return None
    data = {i: n for n, i in enumerate(kept[1:])}  # 原始行号 → 数据行号；表头为 -1
    data[kept[0]] = -1
    width = len(cells[0])
    out = ["| " + " | ".join(cells[kept[0]]) + " |", "|" + "---|" * width]
    out += ["| " + " | ".join(cells[i]) + " |" for i in kept[1:]]
    return _Table("\n".join(out), 0.0, page_height - table.bbox[1], index, kind="table",
                  unconfirmed_rows=tuple(sorted({data[i] for i in unconfirmed if i in data})))


def _centered(char: dict, box: tuple) -> bool:
    """字的中心落在框内（与 pdfplumber 把字分进单元格的规则一致）。"""
    x, y = (char["x0"] + char["x1"]) / 2, (char["top"] + char["bottom"]) / 2
    return box[0] <= x < box[2] and box[1] <= y < box[3]


def _cell(value: str | None) -> str:
    if not value:
        return ""
    text = ""
    for part in value.split("\n"):
        text = join_text(text, part.strip()) if text else part.strip()
    return text.replace("|", "\\|")


def _cells(row: str) -> list[str]:
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", row.strip())[1:-1]]


def _join_split_tables(lines: list[Line]) -> list[Line]:
    """跨页表格：上一页以表格结尾、下一页以列数相同的表格开头时接成一张表；续页重复的表头去掉。
    续表首行若前面几格为空，是上一页最后一行被分页截断的后半，补进那一行的对应格。
    各页表格里未确认的行（缺边框的合并单元格）随之换算到合并后的行号。"""
    out: list[Line] = []
    for line in lines:
        prev = out[-1] if out else None
        if isinstance(prev, _Table) and isinstance(line, _Table) and line.page == prev.page + 1:
            head, rows = prev.text.split("\n"), line.text.split("\n")
            first = _cells(rows[0])
            if len(first) == len(_cells(head[0])):
                base = len(head) - 2  # 上一页的数据行数
                # 续页重复的表头；表头格里可能混进跨页合并单元格被截断的文字，按前缀比较
                repeated = any(first) and all(h.startswith(f) or f.startswith(h) for f, h in zip(first, _cells(head[0])))
                shift = base if repeated else base + 1  # 续表数据行号 → 合并后数据行号
                incoming = rows[2:] if repeated else [rows[0], *rows[2:]]
                cells = _cells(incoming[0]) if incoming else []
                fused = bool(cells) and not cells[0] and any(cells)
                if fused:
                    last = _cells(head[-1])
                    head[-1] = "| " + " | ".join(join_text(a, b) if b else a for a, b in zip(last, cells)) + " |"
                    incoming, shift = incoming[1:], shift - 1

                # 续表表头（-1）未确认时：表头去掉了就落到上一页最后一行，没去掉就是它作为数据行的位置
                moved = {r + shift for r in line.unconfirmed_rows}
                prev.text = "\n".join([*head, *incoming])
                prev.page = line.page
                prev.unconfirmed_rows = tuple(sorted({*prev.unconfirmed_rows, *moved}))
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


def _render(lines: list[Line]) -> tuple[str, list[dict], int]:
    """返回 Markdown 文本、行列对应未确认的范围（表格中未确认的行、连续分栏的文字行），以及疑似无框线表格的处数。"""
    blocks: list[tuple[str, bool]] = []  # （文字，是否属于未确认范围）
    run: list[Line] = []
    columnar = 0

    def flush_run():
        nonlocal columnar
        if run:
            table = len(run) >= MIN_COLUMNAR_LINES and _aligned(run)
            columnar += table
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
            rows = line.text.split("\n")
            blocks += [("", False), ("\n".join(rows[:2]), -1 in line.unconfirmed_rows)]
            blocks += [(row, i in line.unconfirmed_rows) for i, row in enumerate(rows[2:])]
            blocks.append(("", False))
        else:
            blocks.append((escape_heading_marks(line.text), False))
    flush_run()
    text = "\n".join(text for text, _ in blocks)
    lead = len(text) - len(text.lstrip())
    ranges, offset = [], 0
    for chunk, flagged in blocks:
        if flagged and ranges and ranges[-1]["end"] == offset - lead - 1 and chunk.startswith("|"):
            ranges[-1]["end"] = offset - lead + len(chunk)  # 同一张表相邻的未确认行合成一个范围
        elif flagged:
            ranges.append({"start": offset - lead, "end": offset - lead + len(chunk)})
        offset += len(chunk) + 1
    return text.strip(), ranges, columnar


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
