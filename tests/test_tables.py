from datetime import datetime
from io import BytesIO

import pytest
from openpyxl import Workbook

from briefly import config
from briefly.chunking import chunk_text, parse_sections
from briefly.loaders import load_document
from briefly.tables import to_markdown_table


def xlsx_bytes(sheets: dict[str, list[list]]) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_cells_are_normalized():
    table = to_markdown_table(
        [
            ["任务", "工时", "日期", "备注"],
            ["支付|改造", 16.0, datetime(2026, 10, 20), "第一行\n第二行"],
            [None, None, None, None],  # 空行跳过
            ["压测", 8.5, datetime(2026, 10, 18, 9, 30), None],
        ]
    )
    assert table == (
        "| 任务 | 工时 | 日期 | 备注 |\n"
        "|---|---|---|---|\n"
        "| 支付\\|改造 | 16 | 2026-10-20 | 第一行 第二行 |\n"
        "| 压测 | 8.5 | 2026-10-18 09:30 |  |"
    )


def test_trailing_empty_columns_are_dropped():
    assert to_markdown_table([["a", "b", None, None], ["1", None, None, None]]) == "| a | b |\n|---|---|\n| 1 |  |"


def test_xlsx_sheets_become_sections():
    data = xlsx_bytes({"任务进度": [["任务", "负责人"], ["支付改造", "李经理"]], "会议记录": [["日期", "主题"], ["10-08", "评审"]]})
    doc = load_document("进度.xlsx", data)
    assert doc.markdown is True and doc.report["structure"] == "sheets"
    assert [s.title for s in parse_sections(doc.text, doc.markdown)] == ["任务进度", "会议记录"]
    assert "| 支付改造 | 李经理 |" in doc.text


def test_formula_without_cached_value_is_reported():
    data = xlsx_bytes({"工时": [["任务", "工时"], ["a", 1], ["b", 2], ["合计", "=SUM(B2:B3)"]]})
    doc = load_document("t.xlsx", data)
    assert "1 个公式单元格没有计算结果" in doc.report["warnings"][0]


def test_xlsx_merged_cells_follow_the_same_rule_as_docx():
    wb = Workbook()
    ws = wb.active
    for row in [["工时", None, "任务", "负责人"], ["计划", "实际", None, None], [8, 10, "支付", "李经理"], [4, None, "压测", None], ["备注：按计划", None, None, None]]:
        ws.append(row)
    ws.merge_cells("A1:B1")  # 表头横向：每列都填
    ws.merge_cells("D3:D4")  # 纵向：向下填
    ws.merge_cells("A5:D5")  # 正文横向：只留第一格
    buf = BytesIO()
    wb.save(buf)
    lines = load_document("t.xlsx", buf.getvalue()).text.splitlines()
    assert lines[2] == "| 工时 | 工时 | 任务 | 负责人 |"
    assert lines[6] == "| 4 |  | 压测 | 李经理 |"
    assert lines[7] == "| 备注：按计划 |  |  |  |"


def test_xlsx_percent_is_shown_as_in_excel():
    wb = Workbook()
    ws = wb.active
    ws.append(["指标", "完成率", "增长"])
    ws.append(["支付", 0.15, 0.1234])
    ws["B2"].number_format = "0%"
    ws["C2"].number_format = "0.00%"
    buf = BytesIO()
    wb.save(buf)
    assert "| 支付 | 15% | 12.34% |" in load_document("t.xlsx", buf.getvalue()).text


@pytest.mark.parametrize(
    "text",
    ["任务,负责人\n支付改造,李经理\n", "任务;负责人\n支付改造;李经理\n", "任务\t负责人\n支付改造\t李经理\n"],
)
def test_csv_delimiters(text):
    doc = load_document("a.csv", text.encode("utf-8"))
    assert doc.text == "| 任务 | 负责人 |\n|---|---|\n| 支付改造 | 李经理 |"


def test_gbk_csv_from_excel():
    doc = load_document("a.csv", "任务,负责人\n压测,小王\n".encode("gbk"))
    assert "| 压测 | 小王 |" in doc.text


def test_long_table_chunks_repeat_the_header(monkeypatch):
    monkeypatch.setattr(config, "CHUNK_SIZE", 120)
    rows = [["任务", "负责人", "状态"]] + [[f"任务{i:02d}", "某人", "进行中"] for i in range(30)]
    text = "# 进度\n\n说明文字。\n\n" + to_markdown_table(rows)
    chunks = chunk_text(text, parse_sections(text, markdown=True))
    assert len(chunks) > 3
    header = "| 任务 | 负责人 | 状态 |\n|---|---|---|\n"
    assert all(c.content.count("| 任务 | 负责人 | 状态 |") == 1 for c in chunks)
    assert all(c.content.startswith(header) for c in chunks[1:])
    body = "".join(c.content.removeprefix(header) for c in chunks[1:])
    assert all(f"任务{i:02d}" in chunks[0].content + body for i in range(30))  # 每一行都在
