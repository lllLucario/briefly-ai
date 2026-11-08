"""PostgreSQL 派生索引：接口和语义与 LocalIndex 相同，表和业务库放在同一个数据库。

向量用 pgvector 的精确查询（`<=>`，全量计算，没有近似索引），不带固定维度，不同模型可以共存。
词法复用 terms() 的编码结果，用 simple 配置写进 tsvector，排序用应用层 BM25（见 pg_lexical）。
范围用 ANY 传入，一个代次的写入在同一事务里，要么全部成功，要么不留痕迹。
次要排序用 COLLATE "C"：和 SQLite 的二进制比较一致，不随数据库的 locale 变化。
"""
import hashlib
import math
from collections import defaultdict

from sqlalchemy import Text, bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY

from . import pg_lexical
from .index import terms, unit_vector
from .pg_tables import vector_param
from .service import KnowledgeConflict, fingerprint

_BATCH = 200
_SCOPE = bindparam("scope", type_=ARRAY(Text))


class PostgresIndex:
    def __init__(self, engine):
        self.engine = engine

    @staticmethod
    def signature(generation):
        """带上后端：SQLite 文件里的制品和这里的制品不能互认。"""
        return fingerprint(dict(identity=generation.embedding_identity, storage_schema=1, backend="postgresql",
                                lexical="cjk-bigrams-ascii-v1", metric="cosine-normalized-f32"))

    @staticmethod
    def _delete(connection, generation_id):
        connection.execute(text("DELETE FROM kb_index_chunks WHERE generation=:g"), dict(g=generation_id))
        connection.execute(text("DELETE FROM kb_index_generations WHERE id=:g"), dict(g=generation_id))

    def build(self, generation, rows, vectors):
        """rows为(index, embedding_text)；失败不留下半份代次，不修改别的代次。"""
        if len(rows) != len(vectors) or not rows:
            raise ValueError("向量数量与切片不一致")
        prepared = [dict(g=generation.id, n=number, c=content, e=unit_vector(vector, generation.dimension),
                         t=" ".join(terms(content)))
                    for (number, content), vector in zip(rows, vectors)]
        manifest = fingerprint([(n, hashlib.sha256(t.encode()).hexdigest()) for n, t in rows])
        insert = text("INSERT INTO kb_index_chunks(generation,number,content,embedding,tokens) "
                      "VALUES (:g,:n,:c,:e,to_tsvector('simple',:t))").bindparams(vector_param("e"))
        with self.engine.begin() as connection:
            self._delete(connection, generation.id)
            for start in range(0, len(prepared), _BATCH):
                connection.execute(insert, prepared[start:start + _BATCH])
            connection.execute(text("INSERT INTO kb_index_generations(id,signature,manifest) VALUES (:g,:s,:m)"),
                               dict(g=generation.id, s=self.signature(generation), m=manifest))

    def nearest(self, vector, dimension, generation_ids) -> float | None:
        """范围内与查询向量最接近片段的相似度（1 − cosine 距离）；只读向量，不取片段正文。"""
        if not generation_ids:
            return None
        statement = text("SELECT min(embedding <=> :v) FROM kb_index_chunks WHERE generation = ANY(:scope)").bindparams(
            vector_param("v"), _SCOPE)
        with self.engine.connect() as connection:
            distance = connection.execute(statement, dict(v=unit_vector(vector, dimension), scope=sorted(set(generation_ids)))).scalar()
        return None if distance is None or not math.isfinite(distance) else 1 - distance

    def verify(self, generation, rows):
        self.verify_many([(generation, rows)])

    def verify_many(self, pairs):
        """pairs 为 [(generation, rows)]；一次连接取出全部代次的元数据和切片，逐个按 verify 的规则核对。"""
        if not pairs:
            return
        scope = sorted({generation.id for generation, _ in pairs})
        actual = defaultdict(list)
        with self.engine.connect() as connection:
            meta = {gid: (signature, manifest) for gid, signature, manifest in connection.execute(
                text("SELECT id,signature,manifest FROM kb_index_generations WHERE id = ANY(:scope)").bindparams(_SCOPE), dict(scope=scope))}
            for gid, number, content, size in connection.execute(text(
                    "SELECT generation,number,content,vector_dims(embedding) FROM kb_index_chunks WHERE generation = ANY(:scope) "
                    "ORDER BY generation,number").bindparams(_SCOPE), dict(scope=scope)):
                actual[gid].append((number, content, size))
        for generation, rows in pairs:
            expected = fingerprint([(n, hashlib.sha256(t.encode()).hexdigest()) for n, t in rows])
            found = actual.get(generation.id, [])
            actual_hash = fingerprint([(n, hashlib.sha256(t.encode()).hexdigest()) for n, t, _ in found])
            if meta.get(generation.id) != (self.signature(generation), expected) or actual_hash != expected \
                    or any(size != generation.dimension for _, _, size in found):
                raise KnowledgeConflict("索引制品缺失或与版本契约不匹配，需要重建")

    def delete(self, generation_ids):
        with self.engine.begin() as connection:
            for generation_id in generation_ids:
                self._delete(connection, generation_id)

    def search(self, query_text, vector, dimension, generation_ids, *, limit=20):
        """范围先于截取；两路只返回同一有效代次集合中的候选。"""
        if not generation_ids:
            return [], []
        if not 1 <= limit <= 100:
            raise ValueError("候选数量须为1～100")
        scope = sorted(set(generation_ids))
        dense_query = text("SELECT generation,number,embedding <=> :v AS distance FROM kb_index_chunks WHERE generation = ANY(:scope) "
                           'ORDER BY distance,generation COLLATE "C",number LIMIT :limit').bindparams(vector_param("v"), _SCOPE)
        with self.engine.connect() as connection:
            dense = [tuple(row) for row in connection.execute(dense_query, dict(v=unit_vector(vector, dimension), scope=scope, limit=limit))]
            lexical = pg_lexical.rank(connection, "kb_index_chunks", ("generation", "number"), "generation", scope,
                                      terms(query_text, query=True), limit)
        if any(not math.isfinite(distance) for _, _, distance in dense):
            raise KnowledgeConflict("索引距离无效，需要重建")
        return dense, lexical
