"""隔离索引候选原型；未接入正式材料、迁移或问答流程。"""
from collections import defaultdict
import json
import math
from pathlib import Path
import re
import sqlite3
import struct


def grams(text):
    tokens = set()
    for word in re.findall(r"[a-z0-9_]+|[^\W\d_a-z]+", text.lower()):
        pieces = [word] if word.isascii() else list(word) + [word[i:i+2] for i in range(len(word)-1)]
        tokens.update("g" + "x".join(format(ord(c), "x") for c in p) for p in pieces)
    return sorted(tokens)


def query_terms(text):
    terms = set()
    for word in re.findall(r"[a-z0-9_]+|[^\W\d_a-z]+", text.lower()):
        pieces = [word] if word.isascii() or len(word) == 1 else [word[i:i+2] for i in range(len(word)-1)]
        terms.update("g" + "x".join(format(ord(c), "x") for c in p) for p in pieces)
    return sorted(terms)


def rrf(rankings, k=60):
    scores = defaultdict(float)
    for ranking in rankings:
        seen = set()
        for rank, (ident, _) in enumerate(ranking, 1):
            if ident not in seen:
                scores[ident] += 1 / (k + rank)
                seen.add(ident)
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))


class CandidateIndex:
    def __init__(self, path, identity, *, backend="vec0"):
        import sqlite_vec

        if backend not in {"scalar", "vec0"}:
            raise ValueError("未知索引后端")
        self.path = Path(path)
        self.dimension = identity["dimension"]
        if not isinstance(self.dimension, int) or not 1 <= self.dimension <= 4096:
            raise ValueError("维度无效")
        self.backend = backend
        self.db = sqlite3.connect(self.path)
        try:
            self.db.enable_load_extension(True)
            try:
                sqlite_vec.load(self.db)
            finally:
                self.db.enable_load_extension(False)
            self.db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            signature = json.dumps(dict(identity=identity, backend=backend, schema=1), sort_keys=True)
            existing = self.db.execute("SELECT value FROM metadata WHERE key='signature'").fetchone()
            if existing and existing[0] != signature:
                raise ValueError("模型、维度或参数改变：必须新建索引代次并重新向量化，禁止混用")
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('signature', ?)", (signature,))
                self.db.execute("CREATE TABLE IF NOT EXISTS chunks (id INTEGER PRIMARY KEY, doc TEXT NOT NULL, content TEXT NOT NULL, embedding BLOB NOT NULL)")
                self.db.execute("CREATE INDEX IF NOT EXISTS chunks_doc ON chunks(doc)")
                self.db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS lexical USING fts5(tokens, tokenize='ascii')")
                if backend == "vec0":
                    self.db.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS vectors USING vec0(embedding float[{self.dimension}] distance_metric=cosine)")
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def vector(self, values):
        if len(values) != self.dimension or not all(math.isfinite(v) for v in values) or not any(values):
            raise ValueError("向量维度、有限性或范数无效")
        return struct.pack(f"<{self.dimension}f", *values)

    def replace_document(self, document, rows):
        # rows = [(稳定实验块ID, 原文, 向量)]。校验失败和事务失败不能破坏旧内容。
        prepared = [(ident, text, self.vector(vec)) for ident, text, vec in rows]
        with self.db:
            self._delete(document)
            for ident, text, vector in prepared:
                self.db.execute("INSERT INTO chunks VALUES (?,?,?,?)", (ident, document, text, vector))
                self.db.execute("INSERT INTO lexical(rowid,tokens) VALUES (?,?)", (ident, " ".join(grams(text))))
                if self.backend == "vec0":
                    self.db.execute("INSERT INTO vectors(rowid,embedding) VALUES (?,?)", (ident, vector))

    def _delete(self, document):
        rows = self.db.execute("SELECT id FROM chunks WHERE doc=?", (document,)).fetchall()
        for (ident,) in rows:
            self.db.execute("DELETE FROM lexical WHERE rowid=?", (ident,))
            if self.backend == "vec0":
                self.db.execute("DELETE FROM vectors WHERE rowid=?", (ident,))
        self.db.execute("DELETE FROM chunks WHERE doc=?", (document,))

    def delete_document(self, document):
        with self.db:
            self._delete(document)

    def dense(self, vector, scope, limit=10):
        blob = self.vector(vector)
        if not scope:
            return []
        if limit < 1:
            raise ValueError("候选数必须大于0")
        marks = ",".join("?" for _ in scope)
        if self.backend == "vec0":
            sql = f"SELECT rowid,distance FROM vectors WHERE embedding MATCH ? AND k=? AND rowid IN (SELECT id FROM chunks WHERE doc IN ({marks})) ORDER BY distance"
            return self.db.execute(sql, (blob, limit, *scope)).fetchall()
        sql = f"SELECT id,vec_distance_cosine(embedding,?) AS distance FROM chunks WHERE doc IN ({marks}) ORDER BY distance,id LIMIT ?"
        return self.db.execute(sql, (blob, *scope, limit)).fetchall()

    def lexical(self, text, scope, limit=10):
        if not scope:
            return []
        terms = query_terms(text)
        if not terms:
            return []
        marks = ",".join("?" for _ in scope)
        query = " OR ".join('"' + term + '"' for term in terms)
        return self.db.execute(f"SELECT lexical.rowid,bm25(lexical) AS score FROM lexical JOIN chunks ON chunks.id=lexical.rowid WHERE lexical MATCH ? AND chunks.doc IN ({marks}) ORDER BY score,lexical.rowid LIMIT ?", (query, *scope, limit)).fetchall()

    def contents(self, ident):
        return self.db.execute("SELECT doc,content FROM chunks WHERE id=?", (ident,)).fetchone()
