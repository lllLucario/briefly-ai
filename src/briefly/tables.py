"""表格文件（xlsx、csv）→ Markdown 表格。

xlsx 的每个工作表作为一个章节（# 工作表名）；第一行非空行作表头。分块时长表格每块重复表头（见 chunking）。

合并单元格（xlsx 与 docx 共用同一规则，见 merged_value）：
- 纵向合并：值向下填到每一行（几个任务共用一个负责人，拆开后每行都要有负责人）。
- 横向合并：值只留在第一格，其余留空（否则“截止”列会出现负责人的名字，误导）。
- 例外：表头行的横向合并每列都填，否则多级表头下的各列看不出属于哪一组。
"""
import csv
from datetime import date, datetime, time
from io import BytesIO, StringIO

from .loaders import LoadedDocument


def _cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == time(0) else value.isoformat(sep=" ", timespec="minutes")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))  # Excel 把整数存成 16.0
    return " ".join(str(value).split()).replace("|", "\\|")


def merged_value(value, row: int, col: int, first_row: int, first_col: int, header: bool):
    """合并区域内 (row, col) 这一格应显示的值；(first_row, first_col) 是合并区域左上角。"""
    if col == first_col or header:
        return value
    return None


def _display(cell):
    """按单元格的显示格式取值：百分比按 Excel 里看到的样子输出（0.15 → 15%），避免误读。"""
    value = cell.value
    fmt = cell.number_format or ""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and "%" in fmt:
        decimals = len(fmt.split(".")[1].split("%")[0]) if "." in fmt else 0
        return f"{value * 100:.{decimals}f}%"
    return value


def to_markdown_table(rows) -> str:
    cells = [[_cell(v) for v in row] for row in rows]
    cells = [row for row in cells if any(row)]
    if not cells:
        return ""
    width = max(max(i + 1 for i, v in enumerate(row) if v) for row in cells)
    cells = [(row + [""] * width)[:width] for row in cells]
    lines = ["| " + " | ".join(cells[0]) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(row) + " |" for row in cells[1:]]
    return "\n".join(lines)


def load_xlsx(data: bytes) -> LoadedDocument:
    from openpyxl import load_workbook

    # data_only 读 Excel 保存时缓存的计算结果（不用只读模式：要读合并区域和单元格格式）；
    # 另读一份公式，用来发现没有计算结果的公式单元格
    values = load_workbook(BytesIO(data), data_only=True)
    formulas = load_workbook(BytesIO(data), read_only=True)
    parts, missing = [], 0
    try:
        for sheet in values.worksheets:
            width = sheet.max_column
            rows = [[_display(c) for c in row] for row in sheet.iter_rows(min_row=1, min_col=1, max_col=width)]
            formula_rows = formulas[sheet.title].iter_rows(min_row=1, min_col=1, max_col=width, values_only=True)
            for row, formula_row in zip(rows, formula_rows):
                missing += sum(
                    v is None and isinstance(f, str) and f.startswith("=") for v, f in zip(row, formula_row)
                )
            _apply_merges(rows, sheet.merged_cells.ranges)
            if table := to_markdown_table(rows):
                parts.append(f"# {sheet.title}\n\n{table}")
    finally:
        values.close()
        formulas.close()
    warnings = (
        [f"有 {missing} 个公式单元格没有计算结果（文件可能不是用 Excel 保存的），这些单元格显示为空"] if missing else []
    )
    report = {"parser": "openpyxl", "structure": "sheets", "warnings": warnings}
    return LoadedDocument("\n\n".join(parts), markdown=True, report=report)


def _apply_merges(rows: list[list], ranges) -> None:
    """openpyxl 只在合并区域左上角存值，按 merged_value 的规则补到区域内各格。"""
    header_row = next((i for i, row in enumerate(rows) if any(v is not None for v in row)), 0)
    for rng in ranges:
        top, left = rng.min_row - 1, rng.min_col - 1
        value = rows[top][left]
        for r in range(top, rng.max_row):
            for c in range(left, rng.max_col):
                if (r, c) != (top, left):
                    rows[r][c] = merged_value(value, r, c, top, left, header=r == header_row)


def load_csv(text: str) -> LoadedDocument:
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    table = to_markdown_table(csv.reader(StringIO(text), dialect))
    return LoadedDocument(table, markdown=True, report={"parser": "csv"})
