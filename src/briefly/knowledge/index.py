"""本地SQLite派生索引；按代次原子写入，业务库独立决定发布与可见性。"""
from contextlib import contextmanager
import hashlib
import math
from pathlib import Path
import re
import sqlite3
import struct

from .service import KnowledgeConflict, fingerprint, index_fingerprint


def terms(text, *, query=False):
    result = set()
    for word in re.findall(r"[a-z0-9_]+|[^\W\d_a-z]+", text.lower()):
        pieces = [word] if word.isascii() else ([] if query and len(word) > 1 else list(word)) + [word[i:i+2] for i in range(len(word)-1)]
        result.update("g" + "x".join(format(ord(c), "x") for c in value) for value in pieces)
    return sorted(result)


def vector_blob(values, dimension):
    if len(values) != dimension or not all(math.isfinite(x) for x in values) or not any(values):
        raise ValueError("Embedding向量维度、有限性或范数无效")
    # 归一化后再存float32，避免极小/极大浮点数导致cosine溢出。
    scale = max(abs(x) for x in values)
    scaled = [x / scale for x in values]
    norm = math.sqrt(sum(x*x for x in scaled))
    return struct.pack(f"<{dimension}f", *(x / norm for x in scaled))


class LocalIndex:
    def __init__(self, path):
        self.path = Path(path)

    @staticmethod
    def signature(generation):
        return fingerprint(dict(identity=generation.embedding_identity, storage_schema=1,
            lexical="cjk-bigrams-ascii-v1", metric="cosine-normalized-f32"))

    @contextmanager
    def connect(self):
        import sqlite_vec

        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.enable_load_extension(True)
            try:
                sqlite_vec.load(db)
            finally:
                db.enable_load_extension(False)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS generations (id TEXT PRIMARY KEY, signature TEXT NOT NULL, manifest TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS chunks (id INTEGER PRIMARY KEY, generation TEXT NOT NULL, number INTEGER NOT NULL, content TEXT NOT NULL, embedding BLOB NOT NULL, UNIQUE(generation,number))")
            db.execute("CREATE INDEX IF NOT EXISTS chunks_generation ON chunks(generation)")
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS lexical USING fts5(tokens, tokenize='ascii')")
            db.commit()
            yield db
        finally:
            db.close()

    @staticmethod
    def _delete(db, generation_id):
        db.execute("DELETE FROM lexical WHERE rowid IN (SELECT id FROM chunks WHERE generation=?)", (generation_id,))
        db.execute("DELETE FROM chunks WHERE generation=?", (generation_id,))
        db.execute("DELETE FROM generations WHERE id=?", (generation_id,))

    def build(self, generation, rows, vectors):
        """rows为(index, embedding_text)；失败不留下半份代次，不修改别的代次。"""
        if len(rows) != len(vectors) or not rows:
            raise ValueError("向量数量与切片不一致")
        prepared = [(number, text, vector_blob(vector, generation.dimension))
                    for (number, text), vector in zip(rows, vectors)]
        manifest = fingerprint([(n, hashlib.sha256(t.encode()).hexdigest()) for n, t in rows])
        with self.connect() as db, db:
            self._delete(db, generation.id)
            for number, text, blob in prepared:
                rowid = db.execute("INSERT INTO chunks(generation,number,content,embedding) VALUES (?,?,?,?)",
                                   (generation.id, number, text, blob)).lastrowid
                db.execute("INSERT INTO lexical(rowid,tokens) VALUES (?,?)", (rowid, " ".join(terms(text))))
            db.execute("INSERT INTO generations VALUES (?,?,?)", (generation.id, self.signature(generation), manifest))

    def verify(self, generation, rows):
        with self.connect() as db:
            meta = db.execute("SELECT signature,manifest FROM generations WHERE id=?", (generation.id,)).fetchone()
            expected = fingerprint([(n, hashlib.sha256(t.encode()).hexdigest()) for n, t in rows])
            actual = db.execute("SELECT number,content,length(embedding) FROM chunks WHERE generation=? ORDER BY number", (generation.id,)).fetchall()
            actual_hash = fingerprint([(n, hashlib.sha256(t.encode()).hexdigest()) for n, t, _ in actual])
            if meta != (self.signature(generation), expected) or actual_hash != expected or any(size != generation.dimension * 4 for _, _, size in actual):
                raise KnowledgeConflict("索引制品缺失或与版本契约不匹配，需要重建")

    def delete(self, generation_ids):
        with self.connect() as db, db:
            for generation_id in generation_ids:
                self._delete(db, generation_id)

    def search(self, text, vector, dimension, generation_ids, *, limit=20):
        """范围先于截取；两路只返回同一有效代次集合中的候选。"""
        if not generation_ids:
            return [], []
        if not 1 <= limit <= 100:
            raise ValueError("候选数量须为1～100")
        with self.connect() as db:
            # 临时范围表避免SQLite绑定变量上限；词法先生成范围内候选再计算相关性。
            db.execute("CREATE TEMP TABLE scope (generation TEXT PRIMARY KEY)")
            db.executemany("INSERT INTO scope VALUES (?)", ((x,) for x in set(generation_ids)))
            dense = db.execute("SELECT c.generation,c.number,vec_distance_cosine(c.embedding,?) AS distance FROM chunks c JOIN scope s ON s.generation=c.generation ORDER BY distance,c.generation,c.number LIMIT ?",
                               (vector_blob(vector, dimension), limit)).fetchall()
            query = " OR ".join('"' + term + '"' for term in terms(text, query=True))
            lexical = [] if not query else db.execute(
                "SELECT c.generation,c.number,bm25(lexical) AS score FROM lexical JOIN chunks c ON c.id=lexical.rowid JOIN scope s ON s.generation=c.generation WHERE lexical MATCH ? ORDER BY score,c.generation,c.number LIMIT ?", (query, limit)).fetchall()
            if any(not math.isfinite(score) for _, _, score in dense):
                raise KnowledgeConflict("索引距离无效，需要重建")
            return dense, lexical
