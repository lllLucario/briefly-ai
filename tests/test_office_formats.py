"""PowerPoint 与旧版 Excel/PowerPoint：原创构造的文件，旧格式的本地转换用替身，不调用 LibreOffice。"""
from io import BytesIO
from pathlib import Path

import pytest
from openpyxl import Workbook
from pptx import Presentation
from pptx.util import Inches

from briefly import config, legacy_word
from briefly.loaders import file_type, load_document


def pptx_bytes():
    deck = Presentation()
    first = deck.slides.add_slide(deck.slide_layouts[1])
    first.shapes.title.text = "虚构差旅制度"
    first.placeholders[1].text = "住宿上限650元，须事前审批。"
    first.notes_slide.notes_text_frame.text = "讲解时强调事前审批。"
    second = deck.slides.add_slide(deck.slide_layouts[6])
    second.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(1)).text_frame.text = "出差超过7天须分管副总审批。"
    third = deck.slides.add_slide(deck.slide_layouts[5])
    third.shapes.title.text = "城市标准"
    table = third.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(6), Inches(1)).table
    for (r, c), value in {(0, 0): "城市", (0, 1): "上限", (1, 0): "杭州", (1, 1): "480元"}.items():
        table.cell(r, c).text = value
    for slide in deck.slides:  # 每页都有的页脚
        slide.shapes.add_textbox(Inches(1), Inches(6.5), Inches(4), Inches(0.5)).text_frame.text = "虚构公司人力资源部"
    stream = BytesIO()
    deck.save(stream)
    return stream.getvalue()


def xlsx_bytes():
    book = Workbook()
    sheet = book.active
    sheet.title = "标准"
    sheet.append(["城市", "上限"])
    sheet.append(["杭州", "480元"])
    stream = BytesIO()
    book.save(stream)
    return stream.getvalue()


def test_pptx_slides_become_sections_without_repeated_footers():
    doc = load_document("制度.pptx", pptx_bytes())
    assert file_type("制度.PPTX") == "pptx" and doc.markdown is True
    assert "# 第1页 虚构差旅制度" in doc.text and "住宿上限650元" in doc.text
    assert "# 第2页" in doc.text and "分管副总审批" in doc.text  # 没有标题的页也有页标题，便于引用
    assert "# 第3页 城市标准" in doc.text and "| 杭州 | 480元 |" in doc.text
    assert "虚构公司人力资源部" not in doc.text  # 每页重复的页脚去掉
    assert "备注：讲解时强调事前审批。" in doc.text
    assert doc.report["structure"] == "pptx_slides" and doc.report["slides"] == 3 and doc.report["notes"] == 1


@pytest.fixture
def converter(tmp_path, monkeypatch):
    executable = tmp_path / "soffice.com"
    executable.write_bytes(b"fake")
    monkeypatch.setattr(config, "LIBREOFFICE_PATH", str(executable))
    outputs = {"xls": xlsx_bytes(), "ppt": pptx_bytes()}
    commands = []

    def run(command, **kwargs):
        commands.append((command, kwargs))
        source = Path(command[-1])
        kind = source.suffix.lstrip(".")
        assert source.read_bytes().startswith(legacy_word.OLE_HEADER)
        target = legacy_word.LEGACY[kind][2]
        (Path(command[command.index("--outdir") + 1]) / f"source.{target}").write_bytes(outputs[kind])
    monkeypatch.setattr(legacy_word, "_run", run)
    return commands


def test_legacy_excel_and_powerpoint_convert_then_parse(converter):
    sheet = load_document("标准.xls", legacy_word.OLE_HEADER + b"fixture")
    assert "| 杭州 | 480元 |" in sheet.text
    assert sheet.report["original_format"] == "xls" and sheet.report["conversion"] == "libreoffice-to-xlsx"
    assert any("旧版 .xls" in w for w in sheet.report["warnings"])
    deck = load_document("制度.ppt", legacy_word.OLE_HEADER + b"fixture")
    assert "# 第1页 虚构差旅制度" in deck.text and deck.report["conversion"] == "libreoffice-to-pptx"
    filters = [command[command.index("--convert-to") + 1] for command, _ in converter]
    assert filters == ["xlsx:Calc MS Excel 2007 XML", "pptx:Impress MS PowerPoint 2007 XML"]
    assert [kwargs["label"] for _, kwargs in converter] == ["旧版 .xls", "旧版 .ppt"]


@pytest.mark.parametrize("name", ["a.xls", "a.ppt"])
@pytest.mark.parametrize("payload", [b"wrong", b"PK\x03\x04renamed", b"{\\rtf1 renamed}"])
def test_renamed_legacy_files_are_not_success(name, payload):
    with pytest.raises(ValueError, match="仅更改后缀"):
        load_document(name, payload)


def test_conversion_output_must_be_the_expected_format(converter, monkeypatch):
    def wrong(command, **_):  # 转出的是 Word 而不是工作簿
        (Path(command[command.index("--outdir") + 1]) / "source.xlsx").write_bytes(pptx_bytes())
    monkeypatch.setattr(legacy_word, "_run", wrong)
    with pytest.raises(ValueError, match="不是有效的Excel文档"):
        load_document("a.xls", legacy_word.OLE_HEADER)
