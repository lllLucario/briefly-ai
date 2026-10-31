"""PDF 端到端：用 pypdf 拼出带字号、位置、书签的小 PDF（英文标准字体），走完 提取 → 清理 → 标题 → Markdown。"""
from io import BytesIO

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from briefly.chunking import parse_sections
from briefly.knowledge.evidence import constraints
from briefly.loaders import load_document

BODY = "This body line is long enough to look like a normal paragraph of text"


def make_pdf(pages: list[list[tuple[float, float, str]]], outline: list[tuple[str, int]] = ()) -> bytes:
    """pages: 每页 [(字号, 纵坐标, 文字)]；outline: [(书签标题, 页码)]。"""
    writer = PdfWriter()
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    for lines in pages:
        page = writer.add_blank_page(612, 792)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        stream = DecodedStreamObject()
        stream.set_data("".join(f"BT /F1 {s} Tf 72 {y} Td ({t}) Tj ET\n" for s, y, t in lines).encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    for title, page_index in outline:
        writer.add_outline_item(title, page_index)
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


WORDS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel"]
_counter = iter(range(10**6))


def body(y_top: float, n: int = 4, size: float = 10) -> list[tuple[float, float, str]]:
    """每次生成内容都不同的正文行（只差数字的行会被当成重复的水印）。"""
    return [(size, y_top - 14 * i, f"{BODY} {WORDS[next(_counter) % len(WORDS)]} {WORDS[i]}.") for i in range(n)]


def with_header_footer(page_no: int, lines):
    return [(9, 770, "ACME Internal Use Only"), *lines, (9, 30, str(page_no))]


def load(pdf: bytes):
    return load_document("a.pdf", pdf)


def test_font_size_headings_and_header_footer_removal():
    pdf = make_pdf(
        [
            with_header_footer(1, [(16, 700, "Overview"), *body(680)]),
            with_header_footer(2, [(16, 700, "Payment"), (13, 660, "Callback latency"), *body(640)]),
            with_header_footer(3, [(16, 700, "Risks"), *body(680)]),
        ]
    )
    doc = load(pdf)
    assert doc.markdown is True
    assert doc.report["structure"] == "font_size"
    assert [s.path for s in parse_sections(doc.text, doc.markdown)] == [
        "Overview",
        "Payment",
        "Payment > Callback latency",
        "Risks",
    ]
    assert "ACME" not in doc.text
    assert doc.report["removed_lines"] == 6  # 3 个页眉 + 3 个页码
    assert {r["reason"] for r in doc.report["removed"]} == {"重复出现（页眉页脚或水印）"}


def test_body_lines_starting_with_hash_are_escaped():
    doc = load(make_pdf([[(16, 700, "Prompt"), (10, 680, "# Role"), *body(660)]]))
    assert "\\# Role" in doc.text
    assert [s.title for s in parse_sections(doc.text, doc.markdown)] == ["Prompt"]


def test_cover_title_larger_than_all_headings_is_not_a_heading():
    pdf = make_pdf([[(36, 600, "Big Cover Title")], [(16, 700, "Chapter One"), *body(680)]])
    doc = load(pdf)
    assert [s.title for s in parse_sections(doc.text, doc.markdown)] == ["Chapter One"]


def test_title_wrapped_onto_two_lines_is_merged_but_adjacent_headings_are_not():
    pdf = make_pdf([[(16, 700, "A Long Title That"), (16, 682, "Wraps"), (16, 600, "Next Heading"), *body(580)]])
    doc = load(pdf)
    assert [s.title for s in parse_sections(doc.text, doc.markdown)] == ["A Long Title That Wraps", "Next Heading"]


def test_bookmarks_are_used_when_all_text_has_one_size():
    pdf = make_pdf(
        [[(10, 700, "Introduction"), *body(680)], [(10, 700, "Details"), *body(680)]],
        outline=[("Introduction", 0), ("Details", 1)],
    )
    doc = load(pdf)
    assert doc.report["structure"] == "bookmarks"
    assert [s.title for s in parse_sections(doc.text, doc.markdown)] == ["Introduction", "Details"]


def test_numeric_bookmarks_are_ignored_and_rules_take_over():
    pdf = make_pdf([[(10, 700, "1.1 Scope"), *body(680)]], outline=[("1", 0), ("2", 0)])
    doc = load(pdf)
    assert doc.markdown is False and doc.report["structure"] is None
    assert [s.title for s in parse_sections(doc.text, doc.markdown)] == ["1.1 Scope"]


def test_scanned_pdf_is_rejected_with_clear_message():
    with pytest.raises(ValueError, match="扫描版 PDF（2 页中 2 页"):
        load(make_pdf([[], []]))


def test_mostly_text_pdf_warns_about_empty_pages():
    doc = load(make_pdf([[(16, 700, "Title"), *body(680)], [], [(16, 700, "End"), *body(680)], [(16, 700, "More"), *body(680)], [(16, 700, "Last"), *body(680)]]))
    assert doc.report["warnings"] and "第 2 页" in doc.report["warnings"][0]


def test_repeated_body_is_not_erased_or_reported_as_scan():
    text = "Identical contractual provision repeated on every page with original meaning."
    doc = load(make_pdf([[(10, 700, text)]] * 3))
    assert doc.text.count(text) == 3
    # 没有表格、也没有分栏排列的正文不再整份受限
    assert doc.report["table_structure_status"] == "not_present"
    assert constraints(doc.report, 0, len(doc.text)) == {}


def grid_page(rows: list[list[str]], top: float = 600, widths=(90, 110, 280), height: float = 20,
              merged: tuple[int, int] | None = None, before=(), after=()) -> list[str]:
    """带框线表格的页面内容：每格画矩形并写字；merged=(行, 列) 时该格与右侧一格合并成一个矩形。"""
    ops = []
    for r, row in enumerate(rows):
        y, x = top - height * (r + 1), 72
        c = 0
        while c < len(row):
            span = 2 if merged == (r, c) else 1
            width = sum(widths[c:c + span])
            ops.append(f"{x} {y} {width} {height} re S")
            if row[c]:
                ops.append(f"BT /F1 10 Tf {x + 4} {y + 6} Td ({row[c]}) Tj ET")
            x += width
            c += span
    text = [f"BT /F1 {s} Tf 72 {y} Td ({t}) Tj ET" for s, y, t in [*before, *after]]
    return [*text, *ops]


def make_raw_pdf(pages: list[list[str]]) -> bytes:
    """pages: 每页的内容流指令。"""
    writer = PdfWriter()
    font = writer._add_object(DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                                NameObject("/Subtype"): NameObject("/Type1"),
                                                NameObject("/BaseFont"): NameObject("/Helvetica")}))
    for ops in pages:
        page = writer.add_blank_page(612, 792)
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        stream = DecodedStreamObject()
        stream.set_data("\n".join(ops).encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


TABLE = [["Plan", "Price", "Storage"], ["Basic", "0", "50GB"], ["Pro", "180", "1TB"]]


def test_ruled_table_becomes_markdown_rows_and_prose_stays_unrestricted():
    prose = [(10, 700, "The plan table below lists the yearly price and storage for each plan.")]
    doc = load(make_raw_pdf([grid_page(TABLE, before=prose, after=[(10, 500, "Prices are per person per year.")])]))
    assert "| Plan | Price | Storage |\n|---|---|---|\n| Basic | 0 | 50GB |\n| Pro | 180 | 1TB |" in doc.text
    assert doc.text.index("plan table below") < doc.text.index("| Plan") < doc.text.index("Prices are per person")
    assert doc.report["tables"] == 1 and doc.report["table_structure_status"] == "verified"
    assert constraints(doc.report, 0, len(doc.text)) == {}


def test_merged_cells_restrict_only_that_table():
    rows = [["Item", "Q1", "Q2"], ["Sales", "Shared target", ""], ["Cost", "10", "12"]]
    prose = [(10, 700, "Quarterly figures for the sales team are summarised in the following table.")]
    doc = load(make_raw_pdf([grid_page(rows, merged=(1, 1), before=prose)]))
    assert doc.report["table_structure_status"] == "partial"
    (r,) = doc.report["table_partial_ranges"]
    assert doc.text[r["start"]:r["end"]].startswith("| Item | Q1 | Q2 |")
    assert constraints(doc.report, 0, 20) == {}  # 表格前的正文
    assert constraints(doc.report, r["start"], r["end"])["cell_attribution"] == "unverified"


def test_aligned_columns_without_rules_are_flagged_but_bullets_are_not():
    form = [f"BT /F1 10 Tf 72 {y} Td ({a}) Tj ET BT /F1 10 Tf 300 {y} Td ({b}) Tj ET"
            for y, a, b in [(700, "Name", "Phone"), (686, "Address", "Postcode"), (672, "Employer", "Start date")]]
    bullets = [f"BT /F1 10 Tf 72 {y} Td (-) Tj ET BT /F1 10 Tf 110 {y} Td ({t}) Tj ET"
               for y, t in [(600, "Check the incoming material every morning."), (586, "Record defects in the daily log.")]]
    doc = load(make_raw_pdf([[*form, *bullets]]))
    (r,) = doc.report["table_partial_ranges"]
    flagged = doc.text[r["start"]:r["end"]]
    assert "Name" in flagged and "Employer" in flagged and "Check the incoming" not in flagged


def test_table_split_across_pages_is_joined_and_cut_row_completed():
    first = [["Grade", "Title", "Duty"], ["P7", "Expert", "Leads complex work and improves the"]]
    second = [["", "", "capability of the team."], ["P6", "Contributor", "Owns several modules."]]
    doc = load(make_raw_pdf([grid_page(first, top=200), grid_page(second, top=760)]))
    assert doc.report["tables"] == 1
    assert "| P7 | Expert | Leads complex work and improves the capability of the team. |" in doc.text
    assert doc.text.count("|---|---|---|") == 1 and "| P6 | Contributor | Owns several modules. |" in doc.text


def test_overprinted_fake_bold_is_read_once():
    bold = [f"BT /F1 16 Tf {72 + dx} 700 Td (Annual Leave Policy) Tj ET" for dx in (0, 0.2, 0.4)]
    doc = load(make_raw_pdf([[*bold, *(f"BT /F1 10 Tf 72 {y} Td ({t}) Tj ET" for _, y, t in body(680))]]))
    assert doc.text.count("Annual Leave Policy") == 1
