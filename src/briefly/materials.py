"""材料管理：粘贴文本或上传文件，存为纯文本；导入时识别章节、分块并入库，检索查入库的块。"""
import hashlib
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import JSON, DateTime, Engine, ForeignKey, Integer, String, Text, func, select
from sqlalchemy.orm import Mapped, mapped_column, relationship, sessionmaker

from . import config
from .chunking import chunk_text, parse_sections, uses_markdown
from .db import Base
from .loaders import file_type, load_document

SNIPPET_RADIUS = 100
MAX_SEARCH_HITS = 10


class Material(Base):
    __tablename__ = "materials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(20))  # paste / file
    file_type: Mapped[str | None] = mapped_column(String(10))
    content: Mapped[str] = mapped_column(Text)
    char_count: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64), unique=True)
    # 解析报告：structure（章节来自 font_size / bookmarks / docx_styles / markdown / rules / none）、
    # 清理时删除的内容、警告等，便于核查有没有误删
    parse_report: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)

    sections: Mapped[list["MaterialSection"]] = relationship(cascade="all, delete-orphan")
    chunks: Mapped[list["MaterialChunk"]] = relationship(cascade="all, delete-orphan")


class MaterialSection(Base):
    """章节大纲。start/end 是在材料全文中的偏移。"""

    __tablename__ = "material_sections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id", ondelete="CASCADE"), index=True)
    index: Mapped[int] = mapped_column(Integer)  # 从 1 开始，按出现顺序
    level: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200))
    path: Mapped[str] = mapped_column(Text)
    start: Mapped[int] = mapped_column(Integer)
    end: Mapped[int] = mapped_column(Integer)


class MaterialChunk(Base):
    __tablename__ = "material_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id", ondelete="CASCADE"), index=True)
    index: Mapped[int] = mapped_column(Integer)  # 从 1 开始
    section: Mapped[str] = mapped_column(Text)  # 块起点所在章节的路径
    start: Mapped[int] = mapped_column(Integer)
    end: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    char_count: Mapped[int] = mapped_column(Integer)


@dataclass(frozen=True)
class SearchHit:
    material_id: int
    title: str
    chunk: int  # 从 1 开始
    section: str
    hits: int
    snippet: str


def wrap_material(header: str, body: str) -> str:
    """材料内容统一用标记包起来，提醒模型这是待处理的材料而不是指令。"""
    return f"{header}\n【材料开始】\n{body}\n【材料结束】"


class MaterialStore:
    def __init__(self, engine: Engine) -> None:
        self._session = sessionmaker(engine, expire_on_commit=False)

    def add_text(
        self,
        title: str,
        content: str,
        *,
        source: str = "paste",
        kind: str | None = None,
        markdown: bool | None = None,
        report: dict | None = None,
    ) -> tuple[Material, bool]:
        """返回 (材料, 是否新建)。内容相同的材料已存在时直接返回已有的那条。

        markdown 见 chunking.uses_markdown：粘贴的文本为 None，按内容判断。
        """
        content = content.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not content:
            raise ValueError("材料内容为空")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with self._session() as s:
            existing = s.scalar(select(Material).where(Material.content_hash == digest))
            if existing:
                return existing, False
            material = Material(
                title=(title.strip() or content[:20])[:200],
                source=source,
                file_type=kind,
                content=content,
                char_count=len(content),
                content_hash=digest,
            )
            sections = parse_sections(content, markdown)
            report = dict(report or {})
            if not sections:
                report["structure"] = "none"
            elif not report.get("structure"):
                report["structure"] = "markdown" if uses_markdown(content, markdown) else "rules"
            material.parse_report = report
            material.sections = [
                MaterialSection(index=i, level=x.level, title=x.title[:200], path=x.path, start=x.start, end=x.end)
                for i, x in enumerate(sections, start=1)
            ]
            material.chunks = [
                MaterialChunk(
                    index=i, section=c.section, start=c.start, end=c.end, content=c.content, char_count=len(c.content)
                )
                for i, c in enumerate(chunk_text(content, sections), start=1)
            ]
            s.add(material)
            s.commit()
            return material, True

    def add_file(self, filename: str, data: bytes) -> tuple[Material, bool]:
        kind = file_type(filename)
        if len(data) > config.MAX_UPLOAD_MB * 1024 * 1024:
            raise ValueError(f"文件超过 {config.MAX_UPLOAD_MB}MB 上限")
        doc = load_document(filename, data)
        if not doc.text.strip():
            hint = "，可能是扫描版 PDF" if kind == "pdf" else ""
            raise ValueError(f"未从文件中提取到文字{hint}")
        return self.add_text(filename, doc.text, source="file", kind=kind, markdown=doc.markdown, report=doc.report)

    def list_materials(self, ids: list[int] | None = None) -> list[Material]:
        query = select(Material).order_by(Material.id)
        if ids is not None:
            query = query.where(Material.id.in_(ids))
        with self._session() as s:
            return list(s.scalars(query))

    def get(self, material_id: int) -> Material:
        with self._session() as s:
            material = s.get(Material, material_id)
        if material is None:
            raise ValueError(f"材料 {material_id} 不存在")
        return material

    def delete(self, material_id: int) -> None:
        with self._session() as s:
            material = s.get(Material, material_id)
            if material is None:
                raise ValueError(f"材料 {material_id} 不存在")
            s.delete(material)
            s.commit()

    def chunks(self, material_id: int) -> list[MaterialChunk]:
        self.get(material_id)  # 不存在时报错
        query = select(MaterialChunk).where(MaterialChunk.material_id == material_id).order_by(MaterialChunk.index)
        with self._session() as s:
            return list(s.scalars(query))

    def sections(self, material_id: int) -> list[MaterialSection]:
        self.get(material_id)
        query = (
            select(MaterialSection).where(MaterialSection.material_id == material_id).order_by(MaterialSection.index)
        )
        with self._session() as s:
            return list(s.scalars(query))

    def chunk_counts(self, material_ids: list[int] | None = None) -> dict[int, int]:
        query = select(MaterialChunk.material_id, func.count()).group_by(MaterialChunk.material_id)
        if material_ids is not None:
            query = query.where(MaterialChunk.material_id.in_(material_ids))
        with self._session() as s:
            return dict(s.execute(query).all())

    def search(self, keywords: str, material_ids: list[int] | None = None) -> list[SearchHit]:
        """空格分隔多个关键词，子串匹配（不区分大小写），命中任意一个即算，按命中次数排序。"""
        words = [w.lower() for w in keywords.split() if w]
        if not words:
            raise ValueError("关键词为空")
        query = select(MaterialChunk, Material.title).join(Material).order_by(Material.id, MaterialChunk.index)
        if material_ids is not None:
            query = query.where(Material.id.in_(material_ids))
        with self._session() as s:
            rows = s.execute(query).all()
        hits: list[SearchHit] = []
        for chunk, title in rows:
            lower = chunk.content.lower()
            count = sum(lower.count(w) for w in words)
            if not count:
                continue
            first = min(pos for w in words if (pos := lower.find(w)) >= 0)
            start, end = max(0, first - SNIPPET_RADIUS), first + SNIPPET_RADIUS
            text = chunk.content
            snippet = ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")
            hits.append(SearchHit(chunk.material_id, title, chunk.index, chunk.section, count, snippet))
        hits.sort(key=lambda h: h.hits, reverse=True)
        return hits[:MAX_SEARCH_HITS]
