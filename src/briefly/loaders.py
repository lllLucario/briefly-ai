"""把各种办公文件读成文本，标题统一用 Markdown 表示，并附解析报告。"""
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from time import perf_counter

SUPPORTED_TYPES = ("txt", "md", "doc", "docx", "pdf", "xlsx", "xls", "csv", "pptx", "ppt")


@dataclass
class LoadedDocument:
    text: str
    # 标题是否已用 Markdown 的 # 表示：True 只认 #；False 不认 #，按中文编号规则猜；None 按内容自动判断
    markdown: bool | None
    report: dict = field(default_factory=dict)  # 解析报告：结构来源、删除了什么、警告等


def file_type(filename: str) -> str:
    suffix = Path(filename).suffix.lower().lstrip(".")
    if suffix not in SUPPORTED_TYPES:
        raise ValueError(f"不支持的文件类型: .{suffix}，可选: {', '.join(SUPPORTED_TYPES)}")
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
    except ValueError:
        raise
    except Exception as e:  # 底层库的报错（如 BadZipFile）对用户没有意义
        raise ValueError(f"文件无法解析，可能已损坏或与扩展名不符（{type(e).__name__}）") from e


def _load(kind: str, data: bytes) -> LoadedDocument:
    if kind == "doc":
        from . import config
        from .direct_word import DirectWordUnsupported, direct_available, parse_doc
        from .legacy_word import convert_doc
        if config.DOC_PARSER not in {"auto", "direct", "convert"}:
            raise ValueError("BRIEFLY_DOC_PARSER须为auto、direct或convert")
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
    if kind in ("xls", "ppt"):
        return _load_converted(kind, data)
    if kind == "csv":
        from .tables import load_csv

        return load_csv(decode_text(data))
    from .pdf import load_pdf

    return load_pdf(data)


def load_bytes(filename: str, data: bytes) -> str:
    return load_document(filename, data).text


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

    doc = _docling_converter().convert(DocumentStream(name="a.docx", stream=BytesIO(data))).document
    tables = []
    for item, _ in doc.iterate_items():
        if isinstance(item, TextItem) and not isinstance(item, (SectionHeaderItem, TitleItem)):
            item.text = escape_heading_marks(item.text)
        elif isinstance(item, TableItem):
            tables.append(item)
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
    report = {"parser": "docling", "structure": "docx_styles", "footnotes": len(footnotes)}
    if comments:
        report["warnings"] = [f"文档有 {comments} 条批注（审阅意见），未纳入正文"]
    return LoadedDocument(text, markdown=True, report=report)


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
    import re
    from collections import Counter

    from docling.datamodel.base_models import DocumentStream
    from docling_core.types.doc import ContentLayer, PictureItem, SectionHeaderItem, TextItem, TitleItem

    doc = _docling_converter("pptx").convert(DocumentStream(name="a.pptx", stream=BytesIO(data))).document
    for item, _ in doc.iterate_items():
        if isinstance(item, TextItem) and not isinstance(item, (SectionHeaderItem, TitleItem)):
            item.text = escape_heading_marks(item.text)
    # 按每项的页码逐页导出；整篇导出的分页标记在没有占位框的空白版式页上会错位
    slides = [doc.export_to_markdown(page_no=n, escape_html=False, escape_underscores=False, image_placeholder="",
                                     compact_tables=True).strip("\n") for n in sorted(doc.pages)]
    notes: dict[int, list[str]] = {}
    for item in doc.texts:
        if item.content_layer == ContentLayer.NOTES and item.text.strip() and item.prov:
            notes.setdefault(item.prov[0].page_no, []).append(item.text.strip())
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
    pictures = sum(1 for item, _ in doc.iterate_items() if isinstance(item, PictureItem))
    report = {"parser": "docling", "structure": "pptx_slides", "slides": len(slides),
              "notes": sum(len(v) for v in notes.values()), "pictures": pictures}
    if pictures:
        report["warnings"] = [f"幻灯片中有 {pictures} 张图片，未识别图片里的文字，请核对原文件"]
    return LoadedDocument(text, markdown=True, report=report)


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
