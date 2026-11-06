"""W3 新增元数据表。旧材料及章节/块保持原样，索引是可重建的派生数据。"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Integer, String, Text, UniqueConstraint
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


class KnowledgeBaseRemoval(Base):
    """已删除知识库留下的记录：库本身连同文档、切片一并真删除，这里只留名称和时间，让历史回答里的引用能说明“来源已删除”。
    generation_ids、job_ids 是提交时还没清理的索引代次和上传暂存：独立的索引文件和磁盘文件进不了业务库事务，
    所以和删除同一事务记下，清理成功后清空，失败时下次启动重试。"""
    __tablename__ = "knowledge_base_removals"
    kb_id: Mapped[str] = mapped_column(String(32), primary_key=True)  # 库已删除，不设外键
    name: Mapped[str] = mapped_column(String(200))
    removed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    generation_ids: Mapped[list] = mapped_column(JSON, default=list)
    job_ids: Mapped[list] = mapped_column(JSON, default=list)


class KnowledgeEgressGrant(Base):
    """按知识库一次性授权：允许把问题与检索片段经规则处理后发给外部生成服务。没有记录即未授权。"""
    __tablename__ = "knowledge_egress_grants"
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), primary_key=True)
    allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


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


class KnowledgeChunk(Base):
    """新切片属于新知识版本，不改被业务功能引用的MaterialChunk。"""
    __tablename__ = "knowledge_chunks"
    version_id: Mapped[str] = mapped_column(ForeignKey("knowledge_versions.id"), primary_key=True)
    index: Mapped[int] = mapped_column(Integer, primary_key=True)
    section: Mapped[str] = mapped_column(Text)
    start: Mapped[int] = mapped_column(Integer)
    end: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    embedding_text: Mapped[str] = mapped_column(Text)


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


class KnowledgeImport(Base):
    __tablename__ = "knowledge_imports"
    __table_args__ = (UniqueConstraint("kb_id", "request_key"),
        CheckConstraint("status IN ('queued','parsing','preview','indexing','ready','failed')"))
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    request_key: Mapped[str] = mapped_column(String(200))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(String(200))
    source_key: Mapped[str] = mapped_column(String(200))
    expected_revision: Mapped[int] = mapped_column(Integer)
    business_version: Mapped[str | None] = mapped_column(String(100))
    tokens: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="queued")
    error_code: Mapped[str | None] = mapped_column(String(80))
    error: Mapped[str | None] = mapped_column(Text)
    material_id: Mapped[int | None] = mapped_column(ForeignKey("materials.id", ondelete="SET NULL"))
    version_id: Mapped[str | None] = mapped_column(ForeignKey("knowledge_versions.id"))
    generation_id: Mapped[str | None] = mapped_column(ForeignKey("knowledge_index_generations.id"))
    # 复用旧列名及preview状态存储解析交接数据；不再表示用户审批。
    prepared: Mapped[dict | None] = mapped_column("preview", JSON)
    lease_token: Mapped[str | None] = mapped_column(String(32))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class KnowledgeParseCache(Base):
    __tablename__ = "knowledge_parse_cache"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kb_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    material_id: Mapped[int | None] = mapped_column(ForeignKey("materials.id", ondelete="SET NULL"))
    content_hash: Mapped[str] = mapped_column(String(64))
    markdown: Mapped[bool | None] = mapped_column(Boolean)
    report_snapshot: Mapped[dict] = mapped_column(JSON)


class KnowledgeImportMetrics(Base):
    __tablename__ = "knowledge_import_metrics"
    import_id: Mapped[str] = mapped_column(ForeignKey("knowledge_imports.id", ondelete="CASCADE"), primary_key=True)
    values: Mapped[dict] = mapped_column(JSON)
