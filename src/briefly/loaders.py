"""把各种办公文件读成纯文本。"""
from io import BytesIO
from pathlib import Path

SUPPORTED_TYPES = ("txt", "md", "docx", "pdf")


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


def load_bytes(filename: str, data: bytes) -> str:
    kind = file_type(filename)
    if kind in {"txt", "md"}:
        return decode_text(data)
    if kind == "docx":
        from docx import Document

        return "\n".join(par.text for par in Document(BytesIO(data)).paragraphs)
    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(data)).pages)


def load_text(path: str | Path) -> str:
    p = Path(path)
    return load_bytes(p.name, p.read_bytes())
