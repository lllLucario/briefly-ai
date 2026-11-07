"""PostgreSQL 上的派生索引表。只在 PostgreSQL 用，所以不进 Base.metadata（SQLite 的 create_all 建不了 vector 类型）。

知识索引和工作区索引的数据都能从业务库重建，业务库决定发布和可见性。没有近似向量索引：它会改变召回。"""
from pgvector.sqlalchemy import Vector
from sqlalchemy import BigInteger, Column, Identity, Index, Integer, MetaData, String, Table, Text, UniqueConstraint, bindparam
from sqlalchemy.dialects.postgresql import TSVECTOR

metadata = MetaData()

kb_generations = Table(
    "kb_index_generations", metadata,
    Column("id", Text, primary_key=True),
    Column("signature", Text, nullable=False),
    Column("manifest", Text, nullable=False),
)

kb_chunks = Table(
    "kb_index_chunks", metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("generation", Text, nullable=False),
    Column("number", Integer, nullable=False),
    Column("content", Text, nullable=False),
    Column("embedding", Vector(), nullable=False),
    Column("tokens", TSVECTOR, nullable=False),
    UniqueConstraint("generation", "number", name="uq_kb_index_chunks_generation_number"),
    Index("ix_kb_index_chunks_tokens", "tokens", postgresql_using="gin"),
)

ws_entries = Table(
    "ws_index_entries", metadata,
    Column("material_id", Integer, primary_key=True, autoincrement=False),
    Column("content_hash", Text, nullable=False),
    Column("signature", Text, nullable=False),
    Column("error", Text),
)

ws_slices = Table(
    "ws_index_slices", metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("material_id", Integer, nullable=False),
    Column("number", Integer, nullable=False),
    Column("section", Text, nullable=False),
    Column("start_pos", Integer, nullable=False),
    Column("end_pos", Integer, nullable=False),
    Column("content_sha", String(64), nullable=False),
    Column("text_sha", String(64), nullable=False),
    Column("embedding_text", Text, nullable=False),
    Column("tokens", TSVECTOR, nullable=False),
    UniqueConstraint("material_id", "number", name="uq_ws_index_slices_material_number"),
    Index("ix_ws_index_slices_text_sha", "text_sha"),
    Index("ix_ws_index_slices_tokens", "tokens", postgresql_using="gin"),
)

ws_vectors = Table(
    "ws_index_vectors", metadata,
    Column("signature", Text, primary_key=True),
    Column("text_sha", String(64), primary_key=True),
    Column("embedding", Vector(), nullable=False),
)


def vector_param(name: str):
    """text() 语句里的向量参数：按 pgvector 的类型转成它的文本格式。"""
    return bindparam(name, type_=Vector())
