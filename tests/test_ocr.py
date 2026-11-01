"""本地图片文字识别接入解析：样本都由程序现画（带文字的图片、只有图片的扫描页），识别引擎用替身，
按图片尺寸返回预设的识别行，不加载真实模型。真实 RapidOCR 的手动对照见 tests/eval/ocr_probe.py。"""
from io import BytesIO
import json
import sys
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from briefly import config, ocr
from briefly.knowledge.evidence import constraints, quality_snapshot
from briefly.knowledge.slicing import token_slices
from briefly.loaders import load_document
from test_knowledge_slicing import Encoder
from test_pdf import body, make_pdf


class FakeEngine:
    """替身引擎：by_size[(宽, 高)] = [(文字, 置信度)]；没登记的尺寸返回 default；"fail" 表示这张图识别时报错。"""

    def __init__(self, by_size=None, default=()):
        self.by_size, self.default, self.calls = by_size or {}, default, []

    def __call__(self, array):
        height, width = array.shape[:2]
        self.calls.append((width, height))
        lines = self.by_size.get((width, height), self.default)
        if lines == "fail":
            raise RuntimeError("inference failed")
        boxes = [[[10, 10 + 40 * i], [600, 10 + 40 * i], [600, 40 + 40 * i], [10, 40 + 40 * i]] for i in range(len(lines))]
        return SimpleNamespace(txts=tuple(t for t, _ in lines), scores=tuple(s for _, s in lines), boxes=boxes)


@pytest.fixture
def engine(monkeypatch):
    fake = FakeEngine()
    monkeypatch.setattr(config, "OCR", "auto")
    monkeypatch.setattr(ocr, "_engine", fake)
    return fake


def picture(size, text="虚构图片文字", fmt="PNG", mode="RGB") -> bytes:
    image = Image.new(mode, size, "white")
    ImageDraw.Draw(image).text((5, 5), text, fill="black")
    out = BytesIO()
    image.save(out, fmt)
    return out.getvalue()


def scanned_pdf(pages: int) -> bytes:
    """只有图片、没有文字层的 PDF（模拟扫描件）。"""
    images = [Image.new("L", (620, 877), 255) for _ in range(pages)]
    for n, image in enumerate(images, start=1):
        ImageDraw.Draw(image).text((60, 80), f"scan page {n}", fill=0)
    out = BytesIO()
    images[0].save(out, "PDF", save_all=True, append_images=images[1:], resolution=75)
    return out.getvalue()


def block_of(doc, n=0):
    item = doc.report["ocr"]["blocks"][n]
    return item, doc.text[item["start"]:item["end"]]


# ---- PDF ----

def test_scanned_pdf_pages_are_recognized_with_source_and_confidence(engine):
    engine.default = [("第一章 差旅报销", 0.99), ("住宿每晚不超过600元。", 0.97)]
    doc = load_document("扫描件.pdf", scanned_pdf(2))
    assert doc.text.count(f"{ocr.OPEN}｜") == doc.text.count(ocr.CLOSE) == 2 and "住宿每晚不超过600元。" in doc.text
    item, segment = block_of(doc, 1)
    assert item["location"] == "第2页" and item["page"] == 2 and item["image"] is None
    assert segment.startswith(f"{ocr.OPEN}｜第2页｜平均置信度 0.98〕") and segment.endswith(ocr.CLOSE)
    assert doc.report["ocr"]["pages_recognized"] == 2 and doc.report["ocr_unconfirmed_ranges"] == []
    assert constraints(doc.report, 0, len(doc.text)) == {}  # 置信度够：标出来源，但不受限
    assert any("来自图片识别" in w for w in doc.report["warnings"])


def test_scanned_pdf_without_ocr_is_still_rejected_and_says_why(monkeypatch):
    with pytest.raises(ValueError, match="扫描版 PDF（2 页中 2 页.*BRIEFLY_OCR=off"):
        load_document("扫描件.pdf", scanned_pdf(2))


def test_only_pages_without_text_layer_are_recognized(engine):
    from pypdf import PdfReader, PdfWriter

    engine.default = [("图片页上的流程说明", 0.95)]
    writer = PdfWriter()
    for source in (make_pdf([[(16, 700, "Title"), *body(680)]]), scanned_pdf(1)):
        for page in PdfReader(BytesIO(source)).pages:
            writer.add_page(page)
    out = BytesIO()
    writer.write(out)
    doc = load_document("混合.pdf", out.getvalue())
    assert len(engine.calls) == 1 and doc.report["ocr"]["blocks"][0]["page"] == 2
    assert doc.text.index("Title") < doc.text.index(ocr.OPEN)
    assert not any("几乎没有文字" in w for w in doc.report["warnings"])


def test_low_confidence_text_restricts_only_its_range_and_slices_apart(engine):
    from pypdf import PdfReader, PdfWriter

    engine.default = [("模糊的手写批注", 0.52), ("看不清", 0.6)]
    writer = PdfWriter()
    for source in (make_pdf([[(10, 700 - 20 * i, f"Native line number {i} of the policy text.") for i in range(10)]]),
                   scanned_pdf(1)):
        for page in PdfReader(BytesIO(source)).pages:
            writer.add_page(page)
    out = BytesIO()
    writer.write(out)
    doc = load_document("混合.pdf", out.getvalue())
    [low] = doc.report["ocr_unconfirmed_ranges"]
    assert "未确认" in doc.text[low["start"]:low["end"]].splitlines()[0]
    assert constraints(doc.report, low["start"], low["end"]) == {"ocr_text": "unverified"}
    assert constraints(doc.report, 0, low["start"]) == {}  # 原生正文不受限
    assert quality_snapshot(doc.report)["ocr_unconfirmed_ranges"] == [low]
    _, chunks = token_slices(doc.text, "混合", doc.markdown, Encoder(), tokens=400, report=doc.report)
    assert any(c.start == low["start"] and c.end == low["end"] for c in chunks)
    assert all(not (c.start < low["start"] < c.end) and not (c.start < low["end"] < c.end) for c in chunks)
    assert any("置信度低" in w for w in doc.report["warnings"])


def test_page_limit_recognizes_part_and_reports_the_rest(engine, monkeypatch):
    monkeypatch.setattr(config, "OCR_MAX_PAGES", 1)
    engine.default = [("只识别了第一页", 0.95)]
    doc = load_document("长扫描件.pdf", scanned_pdf(3))
    assert len(engine.calls) == 1 and doc.report["ocr"]["skipped"] == {"page_limit": 2}
    assert doc.report["ocr"]["partial"] is True
    assert any("超过每份 1 页的识别上限，有 2 页" in w for w in doc.report["warnings"])
    assert any("第 2、3 页几乎没有文字" in w for w in doc.report["warnings"])


def test_exhausted_time_budget_does_not_hang_and_explains(engine, monkeypatch):
    monkeypatch.setattr(config, "OCR_TIME_BUDGET_S", 0)
    engine.default = [("不会被调用", 0.95)]
    with pytest.raises(ValueError, match="超过识别上限或时长预算"):
        load_document("扫描件.pdf", scanned_pdf(2))
    assert engine.calls == []


def test_engine_failure_on_one_page_keeps_the_rest(engine):
    engine.default = "fail"
    with pytest.raises(ValueError, match="没有识别出文字"):
        load_document("扫描件.pdf", scanned_pdf(1))


# ---- Office 文档里的图片 ----

def docx_with(pictures: list[bytes]) -> bytes:
    from docx import Document

    document = Document()
    document.add_heading("采购管理办法", 1)
    document.add_paragraph("正文第一段。")
    for data in pictures:
        document.add_picture(BytesIO(data))
    document.add_paragraph("正文第二段。")
    out = BytesIO()
    document.save(out)
    return out.getvalue()


def test_docx_picture_text_lands_where_the_picture_is_and_icons_are_skipped(engine):
    engine.by_size = {(600, 200): [("审批流程：申请→审批→付款", 0.96)], (40, 40): [("图标", 0.99)]}
    doc = load_document("办法.docx", docx_with([picture((600, 200)), picture((40, 40))]))
    assert (40, 40) not in engine.calls and "图标" not in doc.text
    assert doc.text.index("正文第一段") < doc.text.index("审批流程") < doc.text.index("正文第二段")
    item, segment = block_of(doc)
    assert item["location"] == "图1" and segment.startswith(f"{ocr.OPEN}｜图1｜")
    assert doc.report["ocr"]["skipped"] == {"small": 1}


def test_same_picture_twice_is_recognized_once(engine):
    engine.by_size = {(600, 200): [("重复出现的图", 0.96)]}
    doc = load_document("办法.docx", docx_with([picture((600, 200)), picture((600, 200))]))
    assert engine.calls == [(600, 200)] and doc.text.count("重复出现的图") == 2


def test_image_limit_and_failed_picture_are_reported(engine, monkeypatch):
    monkeypatch.setattr(config, "OCR_MAX_IMAGES", 2)
    engine.by_size = {(600, 200): [("第一张", 0.96)], (600, 210): "fail", (600, 220): [("第三张", 0.96)]}
    doc = load_document("办法.docx", docx_with([picture((600, 200)), picture((600, 210)), picture((600, 220))]))
    assert "第一张" in doc.text and "第三张" not in doc.text and "正文第二段" in doc.text
    info = doc.report["ocr"]
    assert info["failed"] == 1 and info["skipped"] == {"image_limit": 1} and info["partial"]
    assert any("识别失败" in w for w in doc.report["warnings"])


def test_docx_pictures_without_ocr_are_counted_in_a_warning():
    doc = load_document("办法.docx", docx_with([picture((600, 200))]))
    assert ocr.OPEN not in doc.text and "ocr" not in doc.report
    assert any("1 张图片，未识别图片里的文字" in w for w in doc.report["warnings"])


def pptx_with_template(slides: int, extra_on: int) -> bytes:
    from pptx import Presentation
    from pptx.util import Inches

    deck = Presentation()
    for n in range(1, slides + 1):
        slide = deck.slides.add_slide(deck.slide_layouts[5])
        slide.shapes.title.text = f"议题{n}"
        slide.shapes.add_picture(BytesIO(picture((400, 120), "页脚模板")), Inches(0), Inches(6.5))
        if n == extra_on:
            slide.shapes.add_picture(BytesIO(picture((600, 300), "组织图")), Inches(1), Inches(2))
    out = BytesIO()
    deck.save(out)
    return out.getvalue()


def test_pptx_picture_text_goes_to_its_slide_and_repeated_template_is_skipped(engine):
    engine.by_size = {(600, 300): [("市场部  销售部", 0.93)], (400, 120): [("页脚模板", 0.99)]}
    doc = load_document("汇报.pptx", pptx_with_template(4, 2))
    assert "页脚模板" not in doc.text and doc.report["ocr"]["skipped"] == {"repeated": 4}
    assert doc.text.index("# 第2页") < doc.text.index("市场部") < doc.text.index("# 第3页")
    item, _ = block_of(doc)
    assert (item["location"], item["page"], item["image"]) == ("第2页 图1", 2, 1)
    assert not any("未识别图片里的文字" in w for w in doc.report["warnings"])


def test_ppt_direct_pictures_are_rebuilt_in_reading_order(engine, tmp_path):
    from briefly.direct_ppt import slide_documents
    from briefly.loaders import _slides_markdown

    engine.by_size = {(600, 300): [("旧版幻灯片里的图", 0.95)]}
    (tmp_path / "pic-1.png").write_bytes(picture((600, 300)))
    value = {"slides": [{"hidden": False, "pictures": 2, "notes": [], "items": [
        {"t": "title", "x": "封面", "lv": 0, "num": False, "start": 1},
        {"t": "pic", "f": "pic-1.png"},
        {"t": "p", "x": "图后的段落", "lv": 0, "num": False, "start": 1}]}]}
    doc = _slides_markdown(slide_documents(value, tmp_path))
    assert doc.text.index("封面") < doc.text.index("旧版幻灯片里的图") < doc.text.index("图后的段落")
    assert doc.report["pictures"] == 2  # 一张导出了像素，一张（矢量/OLE）只计数


def test_xlsx_floating_picture_follows_its_sheet(engine):
    from openpyxl import Workbook
    from openpyxl.drawing.image import Image as SheetImage

    engine.by_size = {(600, 200): [("报销单样张", 0.95)]}
    book = Workbook()
    sheet = book.active
    sheet.title = "汇总"
    sheet.append(["项目", "金额"])
    sheet.append(["差旅", 1200])
    sheet.add_image(SheetImage(BytesIO(picture((600, 200)))), "D2")
    only = book.create_sheet("附图")
    only.add_image(SheetImage(BytesIO(picture((600, 200)))), "A1")
    out = BytesIO()
    book.save(out)
    doc = load_document("台账.xlsx", out.getvalue())
    assert doc.text.index("| 差旅 | 1200 |") < doc.text.index("报销单样张") < doc.text.index("# 附图")
    assert [b["location"] for b in doc.report["ocr"]["blocks"]] == ["工作表“汇总” 图1", "工作表“附图” 图1"]


# ---- 图片文件 ----

def test_image_file_is_recognized_and_transparent_background_turns_white(engine):
    engine.by_size = {(500, 200): [("截图里的通知", 0.97)]}
    doc = load_document("通知.png", picture((500, 200), mode="RGBA"))
    assert doc.markdown is False and doc.report["parser"] == "rapidocr"
    assert doc.text.startswith(f"{ocr.OPEN}｜图片｜") and "截图里的通知" in doc.text


def test_multi_page_tiff_frames_are_pages(engine):
    engine.by_size = {(500, 200): [("第一帧", 0.97)], (500, 300): [("第二帧", 0.97)]}
    frames = [Image.new("RGB", (500, 200), "white"), Image.new("RGB", (500, 300), "white")]
    out = BytesIO()
    frames[0].save(out, "TIFF", save_all=True, append_images=frames[1:])
    doc = load_document("扫描.tif", out.getvalue())
    assert [b["location"] for b in doc.report["ocr"]["blocks"]] == ["第1页", "第2页"]


def test_image_file_needs_ocr_and_some_text(engine, monkeypatch):
    with pytest.raises(ValueError, match="没有识别出文字"):
        load_document("空白.jpg", picture((500, 200), fmt="JPEG"))
    monkeypatch.setattr(config, "OCR", "off")
    with pytest.raises(ValueError, match="图片文件需要本地图片文字识别.*BRIEFLY_OCR=off"):
        load_document("空白.jpg", picture((500, 200), fmt="JPEG"))


def test_invalid_switch_is_rejected(monkeypatch):
    monkeypatch.setattr(config, "OCR", "maybe")
    with pytest.raises(ValueError, match="BRIEFLY_OCR须为auto或off"):
        load_document("通知.png", picture((500, 200)))


# ---- 文字整理 ----

def test_wrapped_prose_is_joined_but_short_label_lines_are_not():
    def line(text, y, x1=600):
        return ocr._Line(text, 0.9, 10, y, x1, y + 30)
    prose = [line("员工出差须提前提交申请，经部门负责人审批", 0), line("后方可出行，住宿标准见附表。", 36, 400),
             line("报销须在三十日内提交，逾期不予受理，特殊", 72), line("情况另行审批。", 108, 200)]
    assert ocr._paragraphs(prose) == "员工出差须提前提交申请，经部门负责人审批后方可出行，住宿标准见附表。\n报销须在三十日内提交，逾期不予受理，特殊情况另行审批。"
    labels = [line("流程：申请→审批→付款", 0), line("单笔超过五万元需签字", 36, 400)]
    assert ocr._paragraphs(labels) == "流程：申请→审批→付款\n单笔超过五万元需签字"
    columns = [line("姓名", 0, 100), ocr._Line("张三", 0.9, 300, 2, 400, 30), line("# 不是标题", 50, 200)]
    assert ocr._paragraphs(columns) == "姓名  张三\n\\# 不是标题"


# ---- 缓存与隔离进程 ----

def test_parse_cache_fingerprint_follows_the_ocr_switch(monkeypatch):
    from briefly.knowledge.parse_cache import cache_key, parser_identity

    monkeypatch.setattr(config, "OCR", "auto")
    on = parser_identity()
    monkeypatch.setattr(config, "OCR", "off")
    off = parser_identity()
    assert on["ocr"]["mode"] == "auto" and off["ocr"] == {"mode": "off", "version": 1}
    assert cache_key(1, "a.pdf", b"x", on) != cache_key(1, "a.pdf", b"x", off)
    monkeypatch.setattr(config, "OCR", "auto")
    monkeypatch.setattr(config, "OCR_MIN_CONFIDENCE", 0.5)
    assert cache_key(1, "a.pdf", b"x", parser_identity()) != cache_key(1, "a.pdf", b"x", on)


def test_isolated_worker_receives_ocr_settings_and_a_share_of_the_budget(monkeypatch):
    from briefly import parse_process

    probe = ("import json,sys,time,pathlib\n"
             "f=pathlib.Path(sys.argv[1])\n"
             "while not (f/'start').exists(): time.sleep(0.01)\n"
             "r=json.loads((f/'request.json').read_text(encoding='utf-8'))\n"
             "(f/'result.json').write_text(json.dumps(dict(text=json.dumps(r['settings']),markdown=False,report={})),encoding='utf-8')\n")
    monkeypatch.setattr(parse_process, "_worker_command", lambda folder: [sys.executable, "-c", probe, str(folder)])
    monkeypatch.setattr(config, "OCR_TIME_BUDGET_S", 80.0)
    settings = json.loads(parse_process.load_isolated("扫描.png", b"x", timeout=40).text)
    assert settings["OCR"] == "off" and settings["OCR_MAX_PAGES"] == config.OCR_MAX_PAGES
    assert settings["OCR_TIME_BUDGET_S"] == 30.0  # 不超过总预算的四分之三


# ---- 知识入库端到端 ----

def test_image_and_scan_flow_through_preview_confirm_and_search(engine, tmp_path):
    pytest.importorskip("sqlite_vec", reason="知识入库回归需安装.[rag]")
    from briefly.db import create_db_engine
    from briefly.knowledge.index import LocalIndex
    from briefly.knowledge.ingestion import ImportService
    from briefly.knowledge.retrieval import RetrievalService

    engine.by_size = {(500, 200): [("会议室预订须提前一天在系统登记。", 0.97)],
                      (500, 260): [("潦草的旁注", 0.41)]}
    database = create_db_engine("sqlite:///" + (tmp_path / "app.db").as_posix())
    try:
        service = ImportService(database, tmp_path, LocalIndex(tmp_path / "index.db"), Encoder, parser=load_document)
        kb = service.knowledge.create("行政制度")
        for name, data in (("预订规定.png", picture((500, 200))), ("旁注.png", picture((500, 260)))):
            job, _ = service.enqueue(kb.id, name, data, request_key=name)
            assert service.parse(kb.id, job.id).status == "preview"
            chunks = service.preview_chunks(kb.id, job.id)
            expected = {"ocr_text": "unverified"} if name == "旁注.png" else {}
            assert all(chunk["constraints"] == expected for chunk in chunks)
            preview = service.get(kb.id, job.id).preview
            assert service.confirm(kb.id, job.id, preview["digest"]).status == "ready"
        hits = RetrievalService(service).search(kb.id, "会议室预订")["evidence"]
        assert any("会议室预订须提前一天" in hit["content"] and ocr.OPEN in hit["content"] for hit in hits)
        restricted = [hit for hit in RetrievalService(service).search(kb.id, "旁注")["evidence"] if "潦草" in hit["content"]]
        assert restricted and all(hit["constraints"] == {"ocr_text": "unverified"} for hit in restricted)
    finally:
        database.dispose()
