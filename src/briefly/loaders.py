"""把各种办公文件读成文本，标题统一用 Markdown 表示，并附解析报告。"""
import re
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

SUPPORTED_TYPES = ("txt", "md", "docx", "pdf")


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
    if kind == "md":
        return LoadedDocument(decode_text(data), markdown=True)
    if kind == "txt":
        return LoadedDocument(decode_text(data), markdown=False)
    if kind == "docx":
        from docx import Document

        text = "\n".join(_docx_line(p) for p in Document(BytesIO(data)).paragraphs)
        return LoadedDocument(text, markdown=True, report={"parser": "docx", "structure": "docx_styles"})
    from .pdf import load_pdf

    return load_pdf(data)


def load_bytes(filename: str, data: bytes) -> str:
    return load_document(filename, data).text


_DOCX_HEADING = re.compile(r"^(?:Heading|标题)\s*([1-6])$")


def _docx_line(paragraph) -> str:
    """标题样式转成 Markdown 标题，供章节识别使用。"""
    style = paragraph.style.name if paragraph.style is not None else ""
    if style == "Title" and paragraph.text.strip():
        return f"# {paragraph.text}"
    if (m := _DOCX_HEADING.match(style)) and paragraph.text.strip():
        return f"{'#' * int(m.group(1))} {paragraph.text}"
    return escape_heading_marks(paragraph.text)


def load_text(path: str | Path) -> str:
    p = Path(path)
    return load_bytes(p.name, p.read_bytes())
