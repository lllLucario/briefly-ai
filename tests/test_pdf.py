"""PDF 端到端：用 pypdf 拼出带字号、位置、书签的小 PDF（英文标准字体），走完 提取 → 清理 → 标题 → Markdown。"""
from io import BytesIO

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from briefly.chunking import parse_sections
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
    assert doc.report["table_structure_status"] == "partial"
    from briefly.knowledge.evidence import constraints
    assert constraints(doc.report, 0, len(doc.text))["cell_attribution"] == "unverified"
