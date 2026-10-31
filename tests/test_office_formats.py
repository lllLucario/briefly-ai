"""PowerPoint 与旧版 Excel/PowerPoint：原创构造的文件，旧格式的本地转换用替身，不调用 LibreOffice。"""
from io import BytesIO
import json
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
    monkeypatch.setattr(config, "PPT_PARSER", "convert")  # 本机 Java 直读另有测试
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


def xls_and_xlsx_bytes():
    """同一份原创工作簿分别存成 .xls（xlwt）和 .xlsx（openpyxl），用来比对直读与新格式解析。"""
    import xlwt
    from datetime import datetime

    cells = {(0, 0): "虚构部门费用表", (1, 0): "部门", (1, 1): "费用", (2, 1): "预算", (2, 2): "实际",
             (3, 0): "研发", (3, 1): 1200, (3, 2): 1150.5, (4, 1): 800, (4, 2): 640,
             (5, 0): "合计", (5, 1): 2000, (5, 2): 1790.5}
    merges = [(1, 2, 0, 0), (1, 1, 1, 2), (3, 4, 0, 0)]  # (行起, 行止, 列起, 列止)，含止
    old, new = xlwt.Workbook(), Workbook()
    sheet, other = old.add_sheet("费用"), new.active
    other.title = "费用"
    for (r, c), v in cells.items():
        sheet.write(r, c, v)
        other.cell(r + 1, c + 1, v)
    for r1, r2, c1, c2 in merges:
        sheet.merge(r1, r2, c1, c2)
        other.merge_cells(start_row=r1 + 1, end_row=r2 + 1, start_column=c1 + 1, end_column=c2 + 1)
    extra, more = old.add_sheet("其他"), new.create_sheet("其他")
    rows = [["项目", "完成率", "日期", "通过"], ["上线", 0.856, datetime(2026, 3, 5), True]]
    styles = [None, xlwt.easyxf(num_format_str="0.0%"), xlwt.easyxf(num_format_str="yyyy-mm-dd"), None]
    for r, row in enumerate(rows):
        for c, v in enumerate(row):
            extra.write(r, c, v, *([styles[c]] if r and styles[c] else []))
            cell = more.cell(r + 1, c + 1, v)
            if r and c == 1:
                cell.number_format = "0.0%"
            if r and c == 2:
                cell.number_format = "yyyy-mm-dd"
    a, b = BytesIO(), BytesIO()
    old.save(a)
    new.save(b)
    return a.getvalue(), b.getvalue()


def test_xls_is_read_directly_with_the_same_output_as_xlsx(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("能直接读取的 .xls 不应启动转换")
    monkeypatch.setattr(legacy_word, "_run", forbidden)
    old, new = xls_and_xlsx_bytes()
    direct, modern = load_document("费用.xls", old), load_document("费用.xlsx", new)
    assert direct.text == modern.text
    assert "| 研发 | 1200 | 1150.5 |" in direct.text and "| 研发 | 800 | 640 |" in direct.text  # 纵向合并向下填
    assert "| 部门 | 费用/预算 | 费用/实际 |" in direct.text  # 两层表头
    assert "| 上线 | 85.6% | 2026-03-05 | True |" in direct.text
    assert direct.report["parser"] == "xlrd" and direct.report["conversion"] is None
    assert direct.report["original_format"] == "xls" and "direct_seconds" in direct.report["timings"]


def test_xls_that_cannot_be_read_directly_falls_back_to_conversion(converter):
    sheet = load_document("标准.xls", legacy_word.OLE_HEADER + b"not a workbook")
    assert "| 杭州 | 480元 |" in sheet.text and sheet.report["conversion"] == "libreoffice-to-xlsx"
    assert any("已使用本地转换路径" in w for w in sheet.report["warnings"])
    assert "direct_attempt_seconds" in sheet.report["timings"] and len(converter) == 1


def slides_json():
    """原创构造的 HSLF 直读输出：标题、编号列表、表格、页码域、备注、每页重复页脚和一张隐藏页。"""
    footer = {"t": "p", "x": "虚构公司培训部", "lv": 0, "num": False, "start": 1}
    item = lambda t, x, lv=0, num=False, start=1: {"t": t, "x": x, "lv": lv, "num": num, "start": start}
    cell = lambda x, rs=1, cs=1: {"x": x, "rs": rs, "cs": cs}
    return {"slides": [
        {"hidden": False, "pictures": 1, "notes": ["讲解时强调\n事前审批。"], "items": [
            item("title", "虚构差旅制度"), item("p", "# 不是标题"), item("li", "填写申请", num=True),
            item("li", "部门审批", num=True), item("li", "超过7天另报分管副总", lv=1), footer]},
        {"hidden": False, "pictures": 0, "notes": [], "items": [
            item("title", "城市标准"), {"t": "table", "rows": [[cell("城市"), cell("上限")], [cell("杭州"), cell("480元")]]},
            footer]},
        {"hidden": True, "pictures": 0, "notes": [], "items": [item("title", "草稿"), item("p", "不展示的内容"), footer]},
        {"hidden": False, "pictures": 0, "notes": [], "items": [item("title", "谢谢"), footer]},
    ]}


def test_ppt_slides_are_rebuilt_with_the_pptx_conventions():
    from briefly.direct_ppt import slide_documents
    from briefly.loaders import _slides_markdown

    doc = _slides_markdown(slide_documents(slides_json()))
    assert "# 第1页 虚构差旅制度" in doc.text and "\\# 不是标题" in doc.text
    assert "1. 填写申请\n2. 部门审批\n    - 超过7天另报分管副总" in doc.text
    assert "# 第2页 城市标准" in doc.text and "| 杭州 | 480元 |" in doc.text
    assert "# 第3页" in doc.text and "不展示的内容" not in doc.text  # 隐藏页与 Docling 一样不进正文
    assert "虚构公司培训部" not in doc.text  # 每页重复的页脚去掉
    assert "备注：讲解时强调\n事前审批。" in doc.text
    assert doc.report["slides"] == 4 and doc.report["notes"] == 1 and doc.report["pictures"] == 1


@pytest.fixture
def java_bridge(tmp_path, monkeypatch):
    """Java 桥用替身：按 outcome 写出 slides.json 或错误码，不启动真实 Java。"""
    from briefly import direct_ppt

    java, jar = tmp_path / "java.exe", tmp_path / "tika.jar"
    java.write_bytes(b"fake")
    jar.write_bytes(b"fake")
    monkeypatch.setattr(config, "JAVA_PATH", str(java))
    monkeypatch.setattr(config, "TIKA_APP_JAR", str(jar))
    monkeypatch.setattr(config, "PPT_PARSER", "auto")
    state = {"outcome": "ok", "commands": []}

    def run(command, **kwargs):
        state["commands"].append(command)
        target = Path(command[-2]) / "0"
        target.mkdir(parents=True)
        if state["outcome"] == "ok":
            (target / "slides.json").write_text(json.dumps(slides_json(), ensure_ascii=False), encoding="utf-8")
        elif state["outcome"] == "crash":
            raise ValueError("旧版 .ppt 直接解析失败；请检查文件是否损坏或受密码保护")
        else:
            (target / "error.code").write_text(state["outcome"], encoding="utf-8")
    monkeypatch.setattr(direct_ppt, "_run", run)
    return state


def test_ppt_is_read_directly_without_conversion(java_bridge, monkeypatch):
    monkeypatch.setattr(legacy_word, "_run", lambda *_a, **_k: pytest.fail("能直接读取的 .ppt 不应启动转换"))
    doc = load_document("制度.ppt", legacy_word.OLE_HEADER + b"fixture")
    assert "# 第1页 虚构差旅制度" in doc.text and doc.report["parser"] == "poi-hslf"
    assert doc.report["conversion"] is None and doc.report["original_format"] == "ppt"
    assert {"direct_seconds", "document_parse_seconds"} <= set(doc.report["timings"])
    assert java_bridge["commands"][0][-3] == "LegacyPptParser"


@pytest.mark.parametrize("outcome", ["unsupported_structure", "parse_failed", "crash"])
def test_ppt_that_cannot_be_read_directly_falls_back_to_conversion(java_bridge, converter, monkeypatch, outcome):
    monkeypatch.setattr(config, "PPT_PARSER", "auto")
    java_bridge["outcome"] = outcome
    doc = load_document("制度.ppt", legacy_word.OLE_HEADER + b"fixture")
    assert doc.report["conversion"] == "libreoffice-to-pptx" and "# 第1页 虚构差旅制度" in doc.text
    assert any("已使用本地转换路径" in w for w in doc.report["warnings"]) and len(converter) == 1
    assert "direct_attempt_seconds" in doc.report["timings"]


def test_pptx_split_by_slide_matches_page_by_page_export(monkeypatch):
    from briefly import loaders

    calls = []
    split = loaders._split_slides
    monkeypatch.setattr(loaders, "_split_slides", lambda doc: calls.append(split(doc)) or calls[-1])
    fast = load_document("制度.pptx", pptx_bytes())
    assert calls and calls[0] is not None and len(calls[0]) == 3  # 走了按页拆分
    monkeypatch.setattr(loaders, "_split_slides", lambda doc: None)  # 结构核对不过时退回逐页导出
    slow = load_document("制度.pptx", pptx_bytes())
    assert fast.text == slow.text and fast.report["notes"] == slow.report["notes"] == 1


def test_pptx_slide_with_chart_caption_is_exported_by_page(monkeypatch):
    """图表标题是图片的 caption 引用，按页拆分复制不改写引用：这一页退回按页码导出，其余页照常拆分。"""
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE

    from briefly import loaders

    deck = Presentation(BytesIO(pptx_bytes()))
    slide = deck.slides.add_slide(deck.slide_layouts[5])
    slide.shapes.title.text = "季度费用"
    data = CategoryChartData()
    data.categories = ["一季度", "二季度"]
    data.add_series("差旅", (120, 95))
    chart = slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(2), Inches(6), Inches(3), data).chart
    chart.has_title = True
    chart.chart_title.text_frame.text = "虚构差旅费用"
    stream = BytesIO()
    deck.save(stream)
    calls = []
    split = loaders._split_slides
    monkeypatch.setattr(loaders, "_split_slides", lambda doc: calls.append(split(doc)) or calls[-1])
    fast = load_document("费用.pptx", stream.getvalue())
    assert [part is None for part in calls[0]] == [False, False, False, True]
    monkeypatch.setattr(loaders, "_split_slides", lambda doc: None)
    assert fast.text == load_document("费用.pptx", stream.getvalue()).text
    assert "# 第4页 季度费用" in fast.text
