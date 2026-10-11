"""把各种办公文件读成纯文本。"""
from pathlib import Path


def load_text(path: str | Path) -> str:
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix in {".txt", ".md"}:
        return p.read_text(encoding="utf-8")
    if suffix == ".docx":
        from docx import Document

        return "\n".join(par.text for par in Document(p).paragraphs)
    if suffix == ".pdf":
        from pypdf import PdfReader

        return "\n".join(page.extract_text() or "" for page in PdfReader(p).pages)
    raise ValueError(f"不支持的文件类型: {suffix}")
