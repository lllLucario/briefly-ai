"""把各种办公文件读成文本，标题统一用 Markdown 表示，并附解析报告。"""
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

SUPPORTED_TYPES = ("txt", "md", "docx", "pdf", "xlsx", "csv")


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
    try:
        return _load(kind, data)
    except ValueError:
        raise
    except Exception as e:  # 底层库的报错（如 BadZipFile）对用户没有意义
        raise ValueError(f"文件无法解析，可能已损坏或与扩展名不符（{type(e).__name__}）") from e


def _load(kind: str, data: bytes) -> LoadedDocument:
    if kind == "md":
        return LoadedDocument(decode_text(data), markdown=True)
    if kind == "txt":
        return LoadedDocument(decode_text(data), markdown=False)
    if kind == "docx":
        report = {"parser": "docling", "structure": "docx_styles"}
        return LoadedDocument(_docx_to_markdown(data), markdown=True, report=report)
    if kind == "xlsx":
        from .tables import load_xlsx

        return load_xlsx(data)
    if kind == "csv":
        from .tables import load_csv

        return load_csv(decode_text(data))
    from .pdf import load_pdf

    return load_pdf(data)


def load_bytes(filename: str, data: bytes) -> str:
    return load_document(filename, data).text


def _docx_to_markdown(data: bytes) -> str:
    """用 docling 把 docx 转成 Markdown：标题样式 → #，表格 → Markdown 表格，列表保留编号。"""
    from docling.datamodel.base_models import DocumentStream
    from docling_core.types.doc import SectionHeaderItem, TextItem, TitleItem

    doc = _docling_converter().convert(DocumentStream(name="a.docx", stream=BytesIO(data))).document
    for item, _ in doc.iterate_items():
        if isinstance(item, TextItem) and not isinstance(item, (SectionHeaderItem, TitleItem)):
            item.text = escape_heading_marks(item.text)
    # 关闭转义：默认会把 & 改成 &amp;、_ 改成 \_，改动原文
    text = doc.export_to_markdown(escape_html=False, escape_underscores=False, image_placeholder="", compact_tables=True)
    return text.replace("&#124;", "\\|")


_converter = None


def _docling_converter():
    """docling 初始化较慢，进程内只建一次。"""
    global _converter
    if _converter is None:
        from docling.datamodel.base_models import InputFormat
        from docling.document_converter import DocumentConverter

        _converter = DocumentConverter(allowed_formats=[InputFormat.DOCX])
    return _converter


def load_text(path: str | Path) -> str:
    p = Path(path)
    return load_bytes(p.name, p.read_bytes())
