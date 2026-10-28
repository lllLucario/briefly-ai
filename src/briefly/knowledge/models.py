"""W3 新增元数据表。旧材料及章节/块保持原样，索引是可重建的派生数据。"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import JSON, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..db import Base


def new_id():
    return uuid4().hex


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"
    __table_args__ = (UniqueConstraint("kb_id", "source_key"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    source_key: Mapped[str] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(String(200))
    revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)


class KnowledgeVersion(Base):
    __tablename__ = "knowledge_versions"
    __table_args__ = (
        UniqueConstraint("document_id", "number"),
        UniqueConstraint("document_id", "id"),
        UniqueConstraint("document_id", "content_hash", "chunk_fingerprint", "material_id"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("knowledge_documents.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)  # 技术版本序号，不冒充原文的业务版本
    business_version: Mapped[str | None] = mapped_column(String(100))
    material_id: Mapped[int | None] = mapped_column(ForeignKey("materials.id", ondelete="SET NULL"), index=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    source_snapshot: Mapped[dict] = mapped_column(JSON)
    chunk_config: Mapped[dict] = mapped_column(JSON)
    chunk_manifest: Mapped[list] = mapped_column(JSON)
    chunk_fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class IndexGeneration(Base):
    __tablename__ = "knowledge_index_generations"
    __table_args__ = (
        UniqueConstraint("version_id", "id"),
        CheckConstraint("dimension > 0 AND dimension <= 4096"),
        CheckConstraint("status IN ('planned','building','ready','failed')"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    version_id: Mapped[str] = mapped_column(ForeignKey("knowledge_versions.id"), index=True)
    embedding_identity: Mapped[dict] = mapped_column(JSON)
    dimension: Mapped[int] = mapped_column(Integer)
    chunk_fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="planned")
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class KnowledgePublication(Base):
    """每个文档最多一个有效索引代次；复合外键禁止跨文档/版本拼接。"""
    __tablename__ = "knowledge_publications"
    __table_args__ = (
        ForeignKeyConstraint(["document_id", "version_id"], ["knowledge_versions.document_id", "knowledge_versions.id"]),
        ForeignKeyConstraint(["version_id", "generation_id"], ["knowledge_index_generations.version_id", "knowledge_index_generations.id"]),
    )
    document_id: Mapped[str] = mapped_column(ForeignKey("knowledge_documents.id"), primary_key=True)
    version_id: Mapped[str] = mapped_column(String(32))
    generation_id: Mapped[str] = mapped_column(String(32))
