"""表格文件（xlsx、csv）→ Markdown 表格。

xlsx 的每个工作表作为一个章节（# 工作表名）；第一行非空行作表头。分块时长表格每块重复表头（见 chunking）。
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

    # data_only 读 Excel 保存时缓存的计算结果；另读一份公式，用来发现没有计算结果的公式单元格
    values = load_workbook(BytesIO(data), read_only=True, data_only=True)
    formulas = load_workbook(BytesIO(data), read_only=True)
    parts, missing = [], 0
    try:
        for sheet in values.worksheets:
            rows = [list(r) for r in sheet.iter_rows(values_only=True)]
            for row, formula_row in zip(rows, formulas[sheet.title].iter_rows(values_only=True)):
                missing += sum(
                    v is None and isinstance(f, str) and f.startswith("=") for v, f in zip(row, formula_row)
                )
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


def load_csv(text: str) -> LoadedDocument:
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    table = to_markdown_table(csv.reader(StringIO(text), dialect))
    return LoadedDocument(table, markdown=True, report={"parser": "csv"})
