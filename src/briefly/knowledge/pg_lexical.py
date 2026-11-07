"""PostgreSQL 上的词法排序：在应用层按 SQLite FTS5 的 bm25() 公式算分，名次和 SQLite 一致。

为什么不用 ts_rank：它没有 IDF，罕见词和常见词权重相同，门禁里 Top5 只有约一半的题和 SQLite 相同（数据见
files/eval/w4/postgres-retrieval.md）。这里不装 BM25 扩展，继续用 pgvector 官方镜像。

**公式依赖的前提：** terms() 对每个片段每个词只留一次，tsvector 里也只写一次，所以词频恒为 1，公式里的 f 直接取 1。
如果 terms() 改成保留重复词，FTS5 那边的词频会跟着变，这里却还是 1，两边的名次就会悄悄分叉。
test_postgres_lexical.py 用真实的 FTS5 核对公式，并检查 terms() 不产生重复词，前提变了测试会失败。

统计口径和 FTS5 相同：N、df、平均长度都按全表算，不按范围。IDF ≤ 0 时取 1e-6。分数为负，越小越相关。

分两步查询：先按词取 df（走 GIN），再取范围内的候选，只取命中的词和片段长度，在这里算分。写成一条大 SQL 时，
约 1 万片段的范围中位要 7.5 秒。超过 2046 字节的词 PostgreSQL 不收进 tsvector（FTS5 会收），是已知差异。
"""
import math

from sqlalchemy import Text, bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY

K1 = 1.2
B = 0.75
MAX_LEXEME_BYTES = 2046
_TERMS = bindparam("terms", type_=ARRAY(Text))


def idf(total: int, hits: int) -> float:
    value = math.log((total - hits + 0.5) / (hits + 0.5))
    return value if value > 0 else 1e-6


def score(weights: list[float], length: int, average: float) -> float:
    """一个片段的分数。weights 是它命中的查询词的 IDF；词频恒为 1（见模块说明）。"""
    return -sum(weight * (K1 + 1) / (1 + K1 * (1 - B + B * length / average)) for weight in weights)


def rank(connection, table: str, key: tuple[str, ...], scope_column: str, scope, query_terms, limit: int, *, scope_type=Text):
    """范围内命中任一查询词的片段，按 (分数, *key) 排序取前 limit 条：[(*key, 分数)]。

    table、key、scope_column 是代码里写死的表名和列名，不来自输入。"""
    query_terms = [term for term in query_terms if len(term.encode()) <= MAX_LEXEME_BYTES]
    if not query_terms or not scope:
        return []
    total, average = connection.execute(text(f"SELECT count(*), avg(length(tokens))::float8 FROM {table}")).one()
    if not total or not average:
        return []
    count = text(f"SELECT count(*) FROM {table} WHERE tokens @@ to_tsquery('simple', :term)")
    weights = {}
    for term in query_terms:
        hits = connection.execute(count, dict(term=term)).scalar()
        if hits:
            weights[term] = idf(total, hits)
    if not weights:
        return []
    columns = ",".join(key)
    candidates = text(
        f"SELECT {columns}, length(tokens) AS size, "
        "(SELECT array_agg(lexeme) FROM unnest(tokens) AS word WHERE lexeme = ANY(:terms)) AS matched "
        f"FROM {table} WHERE {scope_column} = ANY(:scope) AND tokens @@ to_tsquery('simple', :query)"
    ).bindparams(_TERMS, bindparam("scope", type_=ARRAY(scope_type())))
    rows = connection.execute(candidates, dict(terms=list(weights), scope=list(scope), query=" | ".join(weights))).all()
    width = len(key)
    scored = [(*row[:width], score([weights[term] for term in row[width + 1]], row[width], average)) for row in rows]
    scored.sort(key=lambda row: (row[width], *row[:width]))
    return scored[:limit]
