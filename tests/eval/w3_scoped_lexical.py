"""隔离的FTS区间查询候选；保持原全局BM25与范围过滤，未接正式召回。"""
from w3_retrieval_support import query_terms


def merge_ranges(ranges):
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def bounded_lexical(index, text, scope, limit=10, *, max_documents=8, max_span_ratio=.25):
    """每个区间取K后按同一全局分数合并，仍保留实际doc过滤，不以范围包络代替授权。"""
    if limit < 1 or max_documents < 1 or not 0 < max_span_ratio <= 1:
        raise ValueError("检索预算无效")
    docs = tuple(dict.fromkeys(scope))
    if not docs or not query_terms(text):
        return [], dict(route="empty", ranges=[])
    # 元数据读取与所有区间检索共用一个读快照；不能在获取范围后漏掉同期更新的高编号块。
    index.db.execute("SAVEPOINT bounded_lexical")
    try:
        if len(docs) > max_documents:
            result = index.lexical(text, docs, limit)
            plan = dict(route="baseline_many_documents", ranges=[])
        else:
            ranges = []
            for doc in docs:
                lower, upper = index.db.execute("SELECT min(id),max(id) FROM chunks WHERE doc=?", (doc,)).fetchone()
                if lower is not None:
                    ranges.append((lower, upper))
            ranges = merge_ranges(ranges)
            if not ranges:
                result, plan = [], dict(route="empty", ranges=[])
            else:
                lower = index.db.execute("SELECT min(id) FROM chunks").fetchone()[0]
                upper = index.db.execute("SELECT max(id) FROM chunks").fetchone()[0]
                span_ratio = sum(b-a+1 for a,b in ranges) / (upper-lower+1)
                if span_ratio > max_span_ratio:
                    result = index.lexical(text, docs, limit)
                    plan = dict(route="baseline_wide_span", ranges=ranges, span_ratio=span_ratio)
                else:
                    query = " OR ".join('"'+term+'"' for term in query_terms(text))
                    marks = ",".join("?" for _ in docs)
                    sql = ("SELECT lexical.rowid,bm25(lexical) AS score FROM lexical "
                           "JOIN chunks ON chunks.id=lexical.rowid WHERE lexical MATCH ? "
                           f"AND lexical.rowid>=? AND lexical.rowid<=? AND chunks.doc IN ({marks}) "
                           "ORDER BY score,lexical.rowid LIMIT ?")
                    candidates = []
                    for a,b in ranges:
                        candidates.extend(index.db.execute(sql, (query, a, b, *docs, limit)).fetchall())
                    result = sorted(candidates, key=lambda row:(row[1],row[0]))[:limit]
                    plan = dict(route="bounded", ranges=ranges, span_ratio=span_ratio,
                                query_plan=index.db.execute("EXPLAIN QUERY PLAN "+sql, (query, *ranges[0], *docs, limit)).fetchall())
        index.db.execute("RELEASE bounded_lexical")
        return result, plan
    except BaseException:
        index.db.execute("ROLLBACK TO bounded_lexical")
        index.db.execute("RELEASE bounded_lexical")
        raise
