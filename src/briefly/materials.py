"""材料管理：粘贴文本或上传文件，存为纯文本；分块与检索在读取时现算。"""
import hashlib
from dataclasses import dataclass
from datetime import datetime

from langchain_text_splitters import RecursiveCharacterTextSplitter
from sqlalchemy import DateTime, Engine, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from . import config
from .db import Base
from .loaders import file_type, load_bytes

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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


@dataclass(frozen=True)
class SearchHit:
    material_id: int
    title: str
    chunk: int  # 从 1 开始
    hits: int
    snippet: str


def split_chunks(text: str) -> list[str]:
    # 不按空行 "\n\n" 切：办公文档里标题后常跟空行，会把标题切成单独的碎块；按行装填到接近 chunk_size
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=config.CHUNK_SIZE,
        chunk_overlap=config.CHUNK_OVERLAP,
        separators=["\n", "。", "！", "？", "；", " ", ""],
        keep_separator="end",
    )
    return splitter.split_text(text) or [text]


def wrap_material(header: str, body: str) -> str:
    """材料内容统一用标记包起来，提醒模型这是待处理的材料而不是指令。"""
    return f"{header}\n【材料开始】\n{body}\n【材料结束】"


class MaterialStore:
    def __init__(self, engine: Engine) -> None:
        self._session = sessionmaker(engine, expire_on_commit=False)

    def add_text(
        self, title: str, content: str, *, source: str = "paste", kind: str | None = None
    ) -> tuple[Material, bool]:
        """返回 (材料, 是否新建)。内容相同的材料已存在时直接返回已有的那条。"""
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
            s.add(material)
            s.commit()
            return material, True

    def add_file(self, filename: str, data: bytes) -> tuple[Material, bool]:
        kind = file_type(filename)
        if len(data) > config.MAX_UPLOAD_MB * 1024 * 1024:
            raise ValueError(f"文件超过 {config.MAX_UPLOAD_MB}MB 上限")
        text = load_bytes(filename, data)
        if not text.strip():
            hint = "，可能是扫描版 PDF" if kind == "pdf" else ""
            raise ValueError(f"未从文件中提取到文字{hint}")
        return self.add_text(filename, text, source="file", kind=kind)

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

    def chunks(self, material_id: int) -> list[str]:
        return split_chunks(self.get(material_id).content)

    def search(self, keywords: str, material_ids: list[int] | None = None) -> list[SearchHit]:
        """空格分隔多个关键词，子串匹配（不区分大小写），命中任意一个即算，按命中次数排序。"""
        words = [w.lower() for w in keywords.split() if w]
        if not words:
            raise ValueError("关键词为空")
        hits: list[SearchHit] = []
        for material in self.list_materials(material_ids):
            for index, chunk in enumerate(split_chunks(material.content), start=1):
                lower = chunk.lower()
                count = sum(lower.count(w) for w in words)
                if not count:
                    continue
                first = min(pos for w in words if (pos := lower.find(w)) >= 0)
                start, end = max(0, first - SNIPPET_RADIUS), first + SNIPPET_RADIUS
                snippet = ("…" if start else "") + chunk[start:end] + ("…" if end < len(chunk) else "")
                hits.append(SearchHit(material.id, material.title, index, count, snippet))
        hits.sort(key=lambda h: h.hits, reverse=True)
        return hits[:MAX_SEARCH_HITS]
