"""把各种办公文件读成文本，标题统一用 Markdown 表示，并附解析报告。"""
from dataclasses import dataclass, field
import hashlib
from io import BytesIO
from pathlib import Path
from time import perf_counter
from .errors import UserInputError

IMAGE_TYPES = ("png", "jpg", "jpeg", "bmp", "tif", "tiff", "webp")  # 只靠本地图片文字识别读取
SUPPORTED_TYPES = ("txt", "md", "doc", "docx", "pdf", "xlsx", "xls", "csv", "pptx", "ppt", *IMAGE_TYPES)


@dataclass
class LoadedDocument:
    text: str
    # 标题是否已用 Markdown 的 # 表示：True 只认 #；False 不认 #，按中文编号规则猜；None 按内容自动判断
    markdown: bool | None
    report: dict = field(default_factory=dict)  # 解析报告：结构来源、删除了什么、警告等


def file_type(filename: str) -> str:
    suffix = Path(filename).suffix.lower().lstrip(".")
    if suffix not in SUPPORTED_TYPES:
        raise UserInputError(f"不支持的文件类型: .{suffix}，可选: {', '.join(SUPPORTED_TYPES)}")
    return suffix


def decode_text(data: bytes) -> str:
    """先按 UTF-8 解码，失败再按 GB18030（兼容 Windows 下保存的中文文件）。"""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("gb18030")


def escape_heading_marks(text: str) -> str:
    """正文里以 # 开头的行（如提示词示例）转义，避免被当成标题。"""
    return "\\" + text if text.lstrip().startswith("#") else text


def load_document(filename: str, data: bytes) -> LoadedDocument:
    kind = file_type(filename)
    started = perf_counter()
    try:
        doc = _load(kind, data)
        doc.report = {**doc.report, "timings": {**doc.report.get("timings", {}),
                      "parse_total_seconds": round(perf_counter() - started, 6)}}
        return doc
    except UserInputError:
        raise
    except Exception as e:  # 底层库的报错（如 BadZipFile，或文字是英文内部信息的 ValueError）对用户没有意义
        raise UserInputError(f"文件无法解析，可能已损坏或与扩展名不符（{type(e).__name__}）") from e


def _load(kind: str, data: bytes) -> LoadedDocument:
    if kind == "doc":
        from . import config
        from .direct_word import DirectWordUnsupported, direct_available, parse_doc
        from .legacy_word import convert_doc
        if config.DOC_PARSER not in {"auto", "direct", "convert"}:
            raise UserInputError("BRIEFLY_DOC_PARSER须为auto、direct或convert")
        reason, timings = None, {}
        tick = perf_counter()
        if config.DOC_PARSER == "direct" or (config.DOC_PARSER == "auto" and direct_available()):
            try:
                text, report = parse_doc(data)
                timings["direct_seconds"] = round(perf_counter() - tick, 6)
                return LoadedDocument(text, markdown=True, report={**report, "timings": timings})
            except DirectWordUnsupported as exc:
                if config.DOC_PARSER == "direct":
                    raise
                reason = str(exc) + "；直接解析暂不支持此结构，已使用本地转换路径；请核对原文件"
        elif config.DOC_PARSER == "auto":
            reason = "本地直接解析依赖未就绪，已使用LibreOffice转换路径"
        timings["direct_attempt_seconds"] = round(perf_counter() - tick, 6)
        tick = perf_counter()
        converted = convert_doc(data)
        timings["conversion_seconds"] = round(perf_counter() - tick, 6)
        tick = perf_counter()
        doc = _load_docx(converted)
        timings["document_parse_seconds"] = round(perf_counter() - tick, 6)
        doc.report = {**doc.report, "original_format": "doc", "conversion": "libreoffice-to-docx",
                      "detected_format": "rtf" if data.lstrip().startswith(b"{\\rtf") else "doc",
                      "timings": timings,
                      "warnings": [*doc.report.get("warnings", []), *([reason] if reason else []), "旧版 .doc 经本地转换后解析，复杂排版及修订内容请核对原文件"]}
        return doc
    if kind == "md":
        return LoadedDocument(decode_text(data), markdown=True)
    if kind == "txt":
        return LoadedDocument(decode_text(data), markdown=False)
    if kind == "docx":
        tick = perf_counter()
        doc = _load_docx(data)
        doc.report = {**doc.report, "timings": {"document_parse_seconds": round(perf_counter() - tick, 6)}}
        return doc
    if kind == "xlsx":
        from .tables import load_xlsx

        return load_xlsx(data)
    if kind == "pptx":
        tick = perf_counter()
        doc = _load_pptx(data)
        doc.report = {**doc.report, "timings": {"document_parse_seconds": round(perf_counter() - tick, 6)}}
        return doc
    if kind == "xls":
        return _load_xls(data)
    if kind == "ppt":
        return _load_ppt(data)
    if kind == "csv":
        from .tables import load_csv

        return load_csv(decode_text(data))
    if kind in IMAGE_TYPES:
        return _load_image(data)
    from .pdf import load_pdf

    return load_pdf(data)


def load_bytes(filename: str, data: bytes) -> str:
    return load_document(filename, data).text


def _load_image(data: bytes) -> LoadedDocument:
    """图片文件（截图、拍照、扫描件）整张识别；多页 TIFF 每帧算一页。手机照片按 EXIF 方向摆正。"""
    from PIL import Image, ImageOps, ImageSequence

    from . import ocr

    if not ocr.enabled():
        raise UserInputError(f"图片文件需要本地图片文字识别才能读取：{ocr.disabled_note()}")
    tick = perf_counter()
    image = Image.open(BytesIO(data))
    frames = getattr(image, "n_frames", 1)
    session = ocr.OcrSession()
    tokens = []
    for n, frame in enumerate(ImageSequence.Iterator(image), start=1):
        frame = ImageOps.exif_transpose(frame.copy())
        block = session.page(frame, f"第{n}页" if frames > 1 else "图片", n if frames > 1 else None)
        if block:
            tokens.append(session.token(block))
    if not tokens:
        raise UserInputError("图片里没有识别出文字" + ("（已超过识别上限或预算）" if session.skipped else ""))
    text, report = session.finish("\n\n".join(tokens), {"parser": "rapidocr", "structure": None,
                                                         "frames": frames, "warnings": []})
    report["timings"] = {"ocr_seconds": round(perf_counter() - tick, 6)}
    return LoadedDocument(text, markdown=False, report=report)


def _picture_image(item):
    """Docling 图片项里的 PIL 图片；图表（有数据表的）和取不到像素的返回 None。"""
    if item.meta is not None and getattr(item.meta, "tabular_chart", None) is not None:
        return None
    try:
        return item.image.pil_image if item.image is not None else None
    except Exception:
        return None


def _insert_ocr(doc, item, session, location, page=None, number=None):
    """识别一张 Docling 图片，在它后面插入占位文字（导出后由 session.finish 换成识别文字）。
    图片在表格（或另一张图片）里时插到表格之后，不改动表格结构；标题、分组下的图片就地插入。返回是否插入。"""
    from docling_core.types.doc import DocItemLabel, PictureItem, TableItem

    picture = _picture_image(item)
    if picture is None:
        return False
    block = session.picture(None, location, page, number, picture=picture)
    if block is None:
        return False
    anchor = item
    while anchor.parent is not None and isinstance(parent := anchor.parent.resolve(doc), (TableItem, PictureItem)):
        anchor = parent
    doc.insert_text(sibling=anchor, label=DocItemLabel.TEXT, text=session.token(block),
                    prov=item.prov[0] if item.prov else None, content_layer=item.content_layer)
    return True


def _load_docx(data: bytes) -> LoadedDocument:
    """用 docling 把 docx 转成 Markdown：标题样式 → #，表格 → Markdown 表格，列表保留编号。

    文本框随正文导出；页眉页脚不导出（属于干扰）；脚注、尾注 docling 不随正文导出，补在文末；
    批注是审阅意见而不是正文，不纳入，只在报告里注明数量。
    """
    from docling.datamodel.base_models import DocumentStream
    from docling_core.types.doc import (
        ContentLayer,
        DocItemLabel,
        RichTableCell,
        SectionHeaderItem,
        TableItem,
        TextItem,
        TitleItem,
    )

    from docling_core.types.doc import PictureItem

    from . import ocr

    doc = _docling_converter().convert(DocumentStream(name="a.docx", stream=BytesIO(data))).document
    tables, pictures = [], []
    for item, _ in doc.iterate_items():
        if isinstance(item, TextItem) and not isinstance(item, (SectionHeaderItem, TitleItem)):
            item.text = escape_heading_marks(item.text)
        elif isinstance(item, TableItem):
            tables.append(item)
        elif isinstance(item, PictureItem):
            pictures.append(item)
    session = ocr.OcrSession() if pictures and ocr.enabled() else None
    if session is not None:
        for n, item in enumerate(pictures, start=1):
            _insert_ocr(doc, item, session, f"图{n}", number=n)
    for table in tables:
        # 含多段文字的单元格，docling 把内容另存为表格下的子节点；拆格后文字已在单元格里，删掉子节点免得在表格后重复输出
        rich = [c.ref.resolve(doc) for c in table.data.table_cells if isinstance(c, RichTableCell)]
        table.data.table_cells = _unmerge(table.data.table_cells)
        if rich:
            doc.delete_items(node_items=rich)
    # 关闭转义：默认会把 & 改成 &amp;、_ 改成 \_，改动原文
    text = doc.export_to_markdown(escape_html=False, escape_underscores=False, image_placeholder="", compact_tables=True)
    text = text.replace("&#124;", "\\|")

    footnotes = [t.text.strip() for t in doc.texts if t.label == DocItemLabel.FOOTNOTE and t.text.strip()]
    if footnotes:
        text += "\n\n脚注：\n" + "\n".join(f"[{i}] {note}" for i, note in enumerate(footnotes, start=1))
    comments = sum(1 for t in doc.texts if t.content_layer == ContentLayer.NOTES)
    report = {"parser": "docling", "structure": "docx_styles", "footnotes": len(footnotes), "pictures": len(pictures),
              "warnings": []}
    if comments:
        report["warnings"].append(f"文档有 {comments} 条批注（审阅意见），未纳入正文")
    if session is not None:
        text, report = session.finish(text, report)
    elif pictures:
        report["warnings"].append(f"文档中有 {len(pictures)} 张图片，未识别图片里的文字（{ocr.disabled_note()}），请核对原文件")
    return LoadedDocument(text, markdown=True, report=report)


def _load_xls(data: bytes) -> LoadedDocument:
    """旧版 .xls 先用 xlrd 直接读；读不了（加密、早期格式、结构损坏）再走 LibreOffice 转换。"""
    from .legacy_word import OLE_HEADER
    from .tables import XlsUnsupported, load_xls

    if not data.startswith(OLE_HEADER):
        raise UserInputError("文件不是可识别的旧版 Excel .xls 文档；仅更改后缀不能转换文件格式")
    tick = perf_counter()
    try:
        doc = load_xls(data)
    except XlsUnsupported as exc:
        attempt = round(perf_counter() - tick, 6)
        doc = _load_converted("xls", data)
        doc.report = {**doc.report, "timings": {"direct_attempt_seconds": attempt, **doc.report["timings"]},
                      "warnings": [*doc.report["warnings"], f"旧版 .xls {exc}，已使用本地转换路径"]}
        return doc
    doc.report = {**doc.report, "original_format": "xls", "conversion": None,
                  "timings": {"direct_seconds": round(perf_counter() - tick, 6)}}
    return doc


def _load_ppt(data: bytes) -> LoadedDocument:
    """旧版 .ppt：本地 Java 可用时用 POI HSLF 直接读，读不了（加密、早期格式、损坏）再走 LibreOffice 转换。"""
    from . import config
    from .direct_ppt import ADAPTER_VERSION, DirectSlidesUnsupported, parse_ppt
    from .direct_word import direct_available
    from .legacy_word import OLE_HEADER

    if config.PPT_PARSER not in {"auto", "convert"}:
        raise UserInputError("BRIEFLY_PPT_PARSER须为auto或convert")
    if not data.startswith(OLE_HEADER):
        raise UserInputError("文件不是可识别的旧版 PowerPoint .ppt 文档；仅更改后缀不能转换文件格式")
    if config.PPT_PARSER == "convert" or not direct_available():
        return _load_converted("ppt", data)
    tick = perf_counter()
    try:
        slides = parse_ppt(data)
    except DirectSlidesUnsupported as exc:
        attempt = round(perf_counter() - tick, 6)
        doc = _load_converted("ppt", data)
        doc.report = {**doc.report, "timings": {"direct_attempt_seconds": attempt, **doc.report["timings"]},
                      "warnings": [*doc.report["warnings"], f"旧版 .ppt {exc}，已使用本地转换路径"]}
        return doc
    direct = round(perf_counter() - tick, 6)
    tick = perf_counter()
    doc = _slides_markdown(slides)
    doc.report = {**doc.report, "parser": "poi-hslf", "original_format": "ppt", "conversion": None,
                  "adapter_version": ADAPTER_VERSION,
                  "timings": {"direct_seconds": direct, "document_parse_seconds": round(perf_counter() - tick, 6)}}
    return doc


def _load_converted(kind: str, data: bytes) -> LoadedDocument:
    """旧版 .xls/.ppt：本地 LibreOffice 转成 .xlsx/.pptx 后按新格式解析。"""
    from .legacy_word import LEGACY, convert_legacy

    timings = {}
    tick = perf_counter()
    converted = convert_legacy(data, kind)
    timings["conversion_seconds"] = round(perf_counter() - tick, 6)
    tick = perf_counter()
    if kind == "xls":
        from .tables import load_xlsx

        doc = load_xlsx(converted)
    else:
        doc = _load_pptx(converted)
    timings["document_parse_seconds"] = round(perf_counter() - tick, 6)
    target = LEGACY[kind][2]
    doc.report = {**doc.report, "original_format": kind, "conversion": f"libreoffice-to-{target}", "timings": timings,
                  "warnings": [*doc.report.get("warnings", []), f"旧版 .{kind} 经本地转换后解析，复杂格式请核对原文件"]}
    return doc


def _load_pptx(data: bytes) -> LoadedDocument:
    """用 docling 读幻灯片：每页一节，标题写成“第N页 标题”，便于引用定位；
    每页都重复的页脚、日期和单独的页码行去掉；演讲者备注并入对应页；图片里的文字不识别。"""
    from docling.datamodel.base_models import DocumentStream

    doc = _docling_converter("pptx").convert(DocumentStream(name="a.pptx", stream=BytesIO(data))).document
    return _slides_markdown(doc)


def _slides_markdown(source) -> LoadedDocument:
    """幻灯片 → 按页 Markdown，pptx 与旧版 .ppt 直读共用。

    source 是整篇 DoclingDocument（pptx，每页一个页码）或每页一份的文档列表（.ppt 直读）。
    Docling 的 export_to_markdown(page_no=n) 每次都遍历全文，逐页导出是页数平方级耗时
    （202 页约 110 秒），所以整篇先按页拆成独立文档再各自整篇导出。
    """
    import re
    from collections import Counter

    from docling_core.types.doc import ContentLayer, PictureItem, SectionHeaderItem, TextItem, TitleItem

    from . import ocr

    options = dict(escape_html=False, escape_underscores=False, image_placeholder="", compact_tables=True)
    by_slide: dict[int, list] = {}  # 页码 → [(文档, 图片项)]
    for n, doc in enumerate(source if isinstance(source, list) else [source], start=1):
        for item, _ in doc.iterate_items():
            if isinstance(item, TextItem) and not isinstance(item, (SectionHeaderItem, TitleItem)):
                item.text = escape_heading_marks(item.text)
            elif isinstance(item, PictureItem):
                page = n if isinstance(source, list) else item.prov[0].page_no if item.prov else None
                if page is not None:
                    by_slide.setdefault(page, []).append((doc, item))
    session = ocr.OcrSession() if by_slide and ocr.enabled() else None
    if session is not None:
        images = {id(item): _picture_image(item) for found in by_slide.values() for _, item in found}
        count = len(source) if isinstance(source, list) else len(source.pages)
        session.ignore_repeated([[hashlib.sha256(images[id(item)].tobytes()).hexdigest()
                                  for _, item in by_slide.get(n, []) if images[id(item)] is not None]
                                 for n in range(1, count + 1)])
        for n in sorted(by_slide):
            for k, (doc, item) in enumerate(by_slide[n], start=1):
                _insert_ocr(doc, item, session, f"第{n}页 图{k}", n, k)
    slides, notes, pictures = [], {}, 0
    if not isinstance(source, list):
        numbers = sorted(source.pages)
        parts = _split_slides(source) or [None] * len(numbers)
        # 拆不出的页按页码导出（原做法，慢但不依赖结构）
        slides = [(part.export_to_markdown(**options) if part is not None
                   else source.export_to_markdown(page_no=n, **options)).strip("\n") for n, part in zip(numbers, parts)]
        pictures = sum(1 for item, _ in source.iterate_items() if isinstance(item, PictureItem))
        for item in source.texts:
            if item.content_layer == ContentLayer.NOTES and item.text.strip() and item.prov:
                notes.setdefault(item.prov[0].page_no, []).append(item.text.strip())
    else:
        for n, doc in enumerate(source, start=1):
            slides.append(doc.export_to_markdown(**options).strip("\n"))
            pictures += sum(1 for item, _ in doc.iterate_items() if isinstance(item, PictureItem))
            for item in doc.texts:
                if item.content_layer == ContentLayer.NOTES and item.text.strip() and item.prov:
                    notes.setdefault(n, []).append(item.text.strip())
    # 至少 3 页、半数以上页面都出现的同一行（页脚、公司名、日期）是版式装饰
    seen = Counter(line.strip() for s in slides for line in {l for l in s.splitlines() if l.strip()}
                   if not line.lstrip().startswith(("#", "|")))
    repeated = {line for line, n in seen.items() if len(slides) >= 3 and n >= max(3, len(slides) / 2)}
    blocks = []
    for n, slide in enumerate(slides, start=1):
        lines = [l for l in slide.splitlines() if l.strip() not in repeated and l.strip() != str(n)]
        title = next((i for i, l in enumerate(lines) if l.startswith("# ")), None)
        if title is None:
            lines.insert(0, f"# 第{n}页")
        else:
            lines[title] = f"# 第{n}页 " + " ".join(lines[title][2:].split())
        if notes.get(n):
            lines += ["", "备注：" + "\n".join(notes[n])]
        blocks.append("\n".join(lines).strip())
    text = re.sub(r"\n{3,}", "\n\n", "\n\n".join(blocks)).replace("&#124;", "\\|")
    report = {"parser": "docling", "structure": "pptx_slides", "slides": len(slides),
              "notes": sum(len(v) for v in notes.values()), "pictures": pictures}
    if session is not None:
        text, report = session.finish(text, report)
    elif pictures:
        report["warnings"] = [f"幻灯片中有 {pictures} 张图片，未识别图片里的文字（{ocr.disabled_note()}），请核对原文件"]
    return LoadedDocument(text, markdown=True, report=report)


def _split_slides(doc):
    """Docling pptx 文档按页拆成独立文档：正文的每个直接子节点是一页的分组，且组内每一项都只标着这一页。

    返回与 sorted(doc.pages) 对应的列表；某页含跨项引用（图表标题等 caption、脚注、富文本表格单元格）时
    该页为 None——extract_items_range 不改写这类引用，由调用方对这一页按页码导出。整体结构不符
    （分组数与页数不符、有项没有页码或跨页）时返回 None，全部按页码逐页导出。两种退回输出都不变。
    """
    from docling_core.types.doc import ContentLayer, DocItem, GroupItem, RichTableCell, TableItem

    groups = [ref.resolve(doc) for ref in doc.body.children]
    numbers = sorted(doc.pages)
    if len(groups) != len(numbers) or not all(isinstance(g, GroupItem) for g in groups):
        return None
    layers, pages = set(ContentLayer), []
    for group, number in zip(groups, numbers):
        linked = False
        for item, _ in doc.iterate_items(root=group, included_content_layers=layers):
            if isinstance(item, DocItem) and (not item.prov or any(p.page_no != number for p in item.prov)):
                return None
            linked = linked or any(getattr(item, name, None) for name in ("captions", "references", "footnotes")) or (
                isinstance(item, TableItem) and any(isinstance(c, RichTableCell) for c in item.data.table_cells))
        pages.append(None if linked else doc.extract_items_range(start=group, end=group))
    return pages


def _unmerge(cells):
    """把合并单元格拆成单格，按 tables.merged_value 的规则决定每格显示什么。

    docling 默认把横向合并的值复制到每一列、纵向合并只留在第一行，与我们要的正好相反。
    """
    from docling_core.types.doc import TableCell

    from .tables import merged_value

    out = []
    for cell in cells:
        text = " ".join(cell.text.split())
        for r in range(cell.start_row_offset_idx, cell.end_row_offset_idx):
            for c in range(cell.start_col_offset_idx, cell.end_col_offset_idx):
                header = cell.column_header or r == 0
                value = merged_value(text, r, c, cell.start_row_offset_idx, cell.start_col_offset_idx, header)
                out.append(
                    TableCell(
                        text=value or "",
                        row_span=1,
                        col_span=1,
                        start_row_offset_idx=r,
                        end_row_offset_idx=r + 1,
                        start_col_offset_idx=c,
                        end_col_offset_idx=c + 1,
                        column_header=cell.column_header,
                        row_header=cell.row_header,
                    )
                )
    return out


_converters = {}


def _docling_converter(kind="docx"):
    """docling 初始化较慢，每种格式进程内只建一次。"""
    if kind not in _converters:
        from docling.datamodel.base_models import InputFormat
        from docling.document_converter import DocumentConverter

        _converters[kind] = DocumentConverter(allowed_formats=[InputFormat(kind)])
    return _converters[kind]


def load_text(path: str | Path) -> str:
    p = Path(path)
    return load_bytes(p.name, p.read_bytes())
