"""PostgreSQL 词法排序的公式和前提。这些用例用真实的 FTS5 核对，不需要 PostgreSQL。"""
import random
import sqlite3

import pytest

from briefly.knowledge import pg_lexical
from briefly.knowledge.index import terms

CORPUS = [
    "出差结束后30天内办理借款冲账，逾期由财务提醒",
    "公司差旅标准：公司员工出差住宿按城市分级报销",
    "合同审批流程：合同金额超过十万元需要总经理审批",
    "Q3 revenue grew 12% and the API latency dropped",
    "会议纪要：讨论了产品路线图和下季度的招聘计划，公司公司公司",
    "报销 报销 报销 报销 报销 同一个词重复很多遍",
    "短",
    "!!! ???",  # 没有词的片段也算进总行数
]


def fts5(corpus, query_terms, scope=None):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE VIRTUAL TABLE lexical USING fts5(tokens, tokenize='ascii')")
    for rowid, content in enumerate(corpus):
        db.execute("INSERT INTO lexical(rowid,tokens) VALUES (?,?)", (rowid, " ".join(terms(content))))
    match = " OR ".join(f'"{term}"' for term in query_terms)
    rows = db.execute("SELECT rowid, bm25(lexical) FROM lexical WHERE lexical MATCH ? ORDER BY 2, rowid", (match,)).fetchall()
    db.close()
    return [row for row in rows if scope is None or row[0] in scope]


def ours(corpus, query_terms, scope=None):
    """pg_lexical.rank 里的算分步骤，数据换成内存里的集合。"""
    documents = [set(terms(content)) for content in corpus]
    average = sum(map(len, documents)) / len(documents)
    weights = {}
    for term in query_terms:
        hits = sum(term in document for document in documents)
        if hits:
            weights[term] = pg_lexical.idf(len(documents), hits)
    rows = []
    for rowid, document in enumerate(documents):
        matched = [weights[term] for term in weights if term in document]
        if matched and (scope is None or rowid in scope):
            rows.append((rowid, pg_lexical.score(matched, len(document), average)))
    return sorted(rows, key=lambda row: (row[1], row[0]))


def test_terms_keep_each_word_once():
    """BM25 公式把词频当成 1，靠的就是这一点。改了 terms() 让这条失败时，要同步改 pg_lexical 的公式和 tsvector 的写入。"""
    for text in [*CORPUS, "公司公司公司 公司", "a a a b", "同一句话。同一句话。同一句话。"]:
        for query in (False, True):
            found = terms(text, query=query)
            assert len(found) == len(set(found))
            assert found == sorted(found)


def test_terms_stay_plain_words_for_tsvector():
    """to_tsvector('simple') 要把每个词原样收下，不能被解析器拆开。"""
    for term in terms("Hello_World 123 中文，混合 C++ 😀 ÄPFEL"):
        assert term.isalnum() and term.isascii() and term.startswith("g")


@pytest.mark.parametrize("query", ["公司报销", "合同审批", "API latency", "出差借款冲账", "公司", "短"])
def test_formula_matches_fts5(query):
    query_terms = terms(query, query=True)
    expected, actual = fts5(CORPUS, query_terms), ours(CORPUS, query_terms)
    assert [row[0] for row in actual] == [row[0] for row in expected]
    assert [row[1] for row in actual] == pytest.approx([row[1] for row in expected], rel=1e-9)


def test_formula_matches_fts5_on_a_larger_random_corpus():
    """常见词（IDF ≤ 0 取 1e-6）和长度差异大的片段都覆盖到。统计按全表，范围只筛结果。"""
    rng = random.Random(10)
    words = ["公司", "报销", "合同", "审批", "财务", "budget", "report", "2026", "会议", "路线图", "招聘", "差旅"]
    corpus = ["公司 " + " ".join(rng.choices(words, k=rng.randint(1, 40))) for _ in range(300)]
    scope = set(rng.sample(range(300), 120))
    for query in ("公司报销", "budget report", "路线图 招聘 差旅", "公司"):
        query_terms = terms(query, query=True)
        expected, actual = fts5(corpus, query_terms, scope), ours(corpus, query_terms, scope)
        assert [row[0] for row in actual] == [row[0] for row in expected]
        assert [row[1] for row in actual] == pytest.approx([row[1] for row in expected], rel=1e-9)


def test_common_word_idf_floor():
    assert pg_lexical.idf(10, 9) == 1e-6
    assert pg_lexical.idf(10, 1) > 1
