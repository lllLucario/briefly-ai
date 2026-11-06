"""工作区索引的存储：SQLite 文件（sqlite-vec＋FTS5）或 PostgreSQL（pgvector＋tsvector）。

WorkspaceIndex 只决定什么时候同步、怎么合并两路结果；具体的表和查询在这里，两边语义相同。
read()、transaction() 产出一个连接（tx），其余方法在同一个 tx 上操作，一次检索看到的是同一份数据。
"""
from contextlib import contextmanager
from pathlib import Path
import sqlite3

from .knowledge.index import terms, unit_vector, vector_blob


class SqliteWorkspaceStore:
    def __init__(self, path) -> None:
        self.path = Path(path)

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
            db.execute("CREATE TABLE IF NOT EXISTS entries (material_id INTEGER PRIMARY KEY, content_hash TEXT NOT NULL, "
                       "signature TEXT NOT NULL, error TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS slices (id INTEGER PRIMARY KEY, material_id INTEGER NOT NULL, "
                       "number INTEGER NOT NULL, section TEXT NOT NULL, start_pos INTEGER NOT NULL, end_pos INTEGER NOT NULL, "
                       "content_sha TEXT NOT NULL, text_sha TEXT NOT NULL, embedding_text TEXT NOT NULL, "
                       "UNIQUE(material_id, number))")
            db.execute("CREATE INDEX IF NOT EXISTS slices_text ON slices(text_sha)")
            db.execute("CREATE TABLE IF NOT EXISTS vectors (signature TEXT NOT NULL, text_sha TEXT NOT NULL, "
                       "embedding BLOB NOT NULL, PRIMARY KEY(signature, text_sha))")
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS lexical USING fts5(tokens, tokenize='ascii')")
            db.commit()
            yield db
        finally:
            db.close()

    read = connect

    @contextmanager
    def transaction(self):
        with self.connect() as db, db:
            yield db

    def slice_counts(self, tx) -> tuple[int, int]:
        return tx.execute("SELECT COUNT(*), COUNT(DISTINCT text_sha) FROM slices").fetchone()

    def vector_count(self, tx) -> int:
        return tx.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]

    def entries(self, tx) -> dict[int, tuple[str, str]]:
        return {row[0]: (row[1], row[2]) for row in tx.execute("SELECT material_id, content_hash, signature FROM entries")}

    def remove(self, tx, material_id: int) -> None:
        tx.execute("DELETE FROM lexical WHERE rowid IN (SELECT id FROM slices WHERE material_id=?)", (material_id,))
        tx.execute("DELETE FROM slices WHERE material_id=?", (material_id,))
        tx.execute("DELETE FROM entries WHERE material_id=?", (material_id,))

    def prune_vectors(self, tx) -> None:
        tx.execute("DELETE FROM vectors WHERE NOT EXISTS (SELECT 1 FROM slices s WHERE s.text_sha = vectors.text_sha)")

    def add_entry(self, tx, material_id: int, digest: str, signature: str, error: str | None = None) -> None:
        tx.execute("INSERT INTO entries VALUES (?,?,?,?)", (material_id, digest, signature, error))

    def add_slices(self, tx, material_id: int, rows: list[tuple]) -> None:
        """rows：(number, section, start, end, content_sha, text_sha, embedding_text)"""
        for row in rows:
            rowid = tx.execute(
                "INSERT INTO slices(material_id,number,section,start_pos,end_pos,content_sha,text_sha,embedding_text) VALUES (?,?,?,?,?,?,?,?)",
                (material_id, *row)).lastrowid
            tx.execute("INSERT INTO lexical(rowid,tokens) VALUES (?,?)", (rowid, " ".join(terms(row[-1]))))

    def missing(self, tx, signature: str) -> list[tuple[str, str]]:
        return tx.execute("SELECT text_sha, MIN(embedding_text) FROM slices s WHERE NOT EXISTS (SELECT 1 FROM vectors v "
                          "WHERE v.signature=? AND v.text_sha=s.text_sha) GROUP BY text_sha ORDER BY MIN(s.id)",
                          (signature,)).fetchall()

    def save_vectors(self, tx, signature: str, rows: list[tuple[str, list[float]]], dimension: int) -> None:
        tx.executemany("INSERT OR IGNORE INTO vectors VALUES (?,?,?)",
                       [(signature, text_sha, vector_blob(vector, dimension)) for text_sha, vector in rows])

    def embedding_texts(self, tx, slice_ids: list[int]) -> dict[int, str]:
        if not slice_ids:
            return {}
        return dict(tx.execute(f"SELECT id, embedding_text FROM slices WHERE id IN ({','.join('?' * len(slice_ids))})", slice_ids))

    def scope(self, tx, material_ids) -> None:
        tx.execute("CREATE TEMP TABLE scope (material_id INTEGER PRIMARY KEY)")
        tx.executemany("INSERT INTO scope VALUES (?)", ((x,) for x in material_ids))

    def routes(self, tx, query: str, vector, dimension: int, signature: str, limit: int) -> tuple[list, list]:
        dense = tx.execute(
            "SELECT s.id, vec_distance_cosine(v.embedding, ?) AS distance FROM slices s JOIN scope USING(material_id) "
            "JOIN vectors v ON v.signature=? AND v.text_sha=s.text_sha ORDER BY distance, s.id LIMIT ?",
            (vector_blob(vector, dimension), signature, limit)).fetchall()
        match = " OR ".join('"' + term + '"' for term in terms(query, query=True))
        lexical = [] if not match else tx.execute(
            "SELECT s.id, bm25(lexical) AS score FROM lexical JOIN slices s ON s.id=lexical.rowid JOIN scope USING(material_id) "
            "WHERE lexical MATCH ? ORDER BY score, s.id LIMIT ?", (match, limit)).fetchall()
        return dense, lexical

    def slice_rows(self, tx, slice_ids) -> dict[int, tuple]:
        slice_ids = list(slice_ids)
        if not slice_ids:
            return {}
        return {r[0]: r for r in tx.execute(
            f"SELECT id, material_id, number, section, start_pos, end_pos FROM slices WHERE id IN ({','.join('?' * len(slice_ids))})",
            slice_ids)}

    def pending(self, tx, signature: str) -> set[int]:
        return {r[0] for r in tx.execute(
            "SELECT DISTINCT s.material_id FROM slices s JOIN scope USING(material_id) WHERE NOT EXISTS "
            "(SELECT 1 FROM vectors v WHERE v.signature=? AND v.text_sha=s.text_sha)", (signature,))}

    def failed(self, tx) -> set[int]:
        return {r[0] for r in tx.execute("SELECT material_id FROM entries JOIN scope USING(material_id) WHERE error IS NOT NULL")}


class PostgresWorkspaceStore:
    """表在业务库里（knowledge/pg_tables.py），仍然是派生数据：可见性由 materials.visible() 决定。"""

    _LOCK = 0x42524657  # 同步的咨询锁：跨进程只有一个在写，表现和 SQLite 的单写者一样

    def __init__(self, engine) -> None:
        from .knowledge.pg_tables import vector_param

        self.engine = engine
        self._vector = vector_param

    @contextmanager
    def read(self):
        """REPEATABLE READ：一次检索的几条查询看到同一份数据，和 SQLite 同一连接时一样。"""
        with self.engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
            yield _Tx(connection)

    @contextmanager
    def transaction(self):
        from sqlalchemy import text

        with self.engine.begin() as connection:
            connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), dict(key=self._LOCK))
            yield _Tx(connection)

    @staticmethod
    def _exec(tx, sql: str, **params):
        from sqlalchemy import text

        return tx.connection.execute(text(sql), params)

    def slice_counts(self, tx) -> tuple[int, int]:
        return tuple(self._exec(tx, "SELECT COUNT(*), COUNT(DISTINCT text_sha) FROM ws_index_slices").one())

    def vector_count(self, tx) -> int:
        return self._exec(tx, "SELECT COUNT(*) FROM ws_index_vectors").scalar()

    def entries(self, tx) -> dict[int, tuple[str, str]]:
        return {row[0]: (row[1], row[2]) for row in self._exec(tx, "SELECT material_id, content_hash, signature FROM ws_index_entries")}

    def remove(self, tx, material_id: int) -> None:
        self._exec(tx, "DELETE FROM ws_index_slices WHERE material_id=:m", m=material_id)
        self._exec(tx, "DELETE FROM ws_index_entries WHERE material_id=:m", m=material_id)

    def prune_vectors(self, tx) -> None:
        self._exec(tx, "DELETE FROM ws_index_vectors WHERE NOT EXISTS (SELECT 1 FROM ws_index_slices s WHERE s.text_sha = ws_index_vectors.text_sha)")

    def add_entry(self, tx, material_id: int, digest: str, signature: str, error: str | None = None) -> None:
        self._exec(tx, "INSERT INTO ws_index_entries(material_id,content_hash,signature,error) VALUES (:m,:d,:s,:e)",
                   m=material_id, d=digest, s=signature, e=error)

    def add_slices(self, tx, material_id: int, rows: list[tuple]) -> None:
        from sqlalchemy import text

        if rows:
            tx.connection.execute(text(
                "INSERT INTO ws_index_slices(material_id,number,section,start_pos,end_pos,content_sha,text_sha,embedding_text,tokens) "
                "VALUES (:m,:n,:section,:a,:b,:c,:t,:e,to_tsvector('simple',:w))"),
                [dict(m=material_id, n=n, section=section, a=a, b=b, c=c, t=t, e=e, w=" ".join(terms(e)))
                 for n, section, a, b, c, t, e in rows])

    def missing(self, tx, signature: str) -> list[tuple[str, str]]:
        return [tuple(row) for row in self._exec(
            tx, "SELECT text_sha, MIN(embedding_text) FROM ws_index_slices s WHERE NOT EXISTS (SELECT 1 FROM ws_index_vectors v "
                "WHERE v.signature=:s AND v.text_sha=s.text_sha) GROUP BY text_sha ORDER BY MIN(s.id)", s=signature)]

    def save_vectors(self, tx, signature: str, rows: list[tuple[str, list[float]]], dimension: int) -> None:
        from sqlalchemy import text

        tx.connection.execute(
            text("INSERT INTO ws_index_vectors(signature,text_sha,embedding) VALUES (:s,:t,:e) ON CONFLICT DO NOTHING").bindparams(self._vector("e")),
            [dict(s=signature, t=text_sha, e=unit_vector(vector, dimension)) for text_sha, vector in rows])

    def _in(self, tx, sql: str, ids, **params):
        from sqlalchemy import Integer, bindparam, text
        from sqlalchemy.dialects.postgresql import ARRAY

        return tx.connection.execute(text(sql).bindparams(bindparam("ids", type_=ARRAY(Integer))), dict(ids=list(ids), **params))

    def embedding_texts(self, tx, slice_ids: list[int]) -> dict[int, str]:
        return dict(tuple(r) for r in self._in(tx, "SELECT id, embedding_text FROM ws_index_slices WHERE id = ANY(:ids)", slice_ids))

    def scope(self, tx, material_ids) -> None:
        tx.scope = sorted(material_ids)

    def routes(self, tx, query: str, vector, dimension: int, signature: str, limit: int) -> tuple[list, list]:
        from sqlalchemy import Integer, bindparam, text
        from sqlalchemy.dialects.postgresql import ARRAY

        from .knowledge import pg_lexical

        dense = text(
            "SELECT s.id, v.embedding <=> :q AS distance FROM ws_index_slices s "
            "JOIN ws_index_vectors v ON v.signature=:s AND v.text_sha=s.text_sha "
            "WHERE s.material_id = ANY(:ids) ORDER BY distance, s.id LIMIT :limit"
        ).bindparams(self._vector("q"), bindparam("ids", type_=ARRAY(Integer)))
        rows = tx.connection.execute(dense, dict(q=unit_vector(vector, dimension), s=signature, ids=tx.scope, limit=limit)).all()
        lexical = pg_lexical.rank(tx.connection, "ws_index_slices", ("id",), "material_id", tx.scope,
                                  terms(query, query=True), limit, scope_type=Integer)
        return [tuple(r) for r in rows], lexical

    def slice_rows(self, tx, slice_ids) -> dict[int, tuple]:
        return {r[0]: tuple(r) for r in self._in(
            tx, "SELECT id, material_id, number, section, start_pos, end_pos FROM ws_index_slices WHERE id = ANY(:ids)", slice_ids)}

    def pending(self, tx, signature: str) -> set[int]:
        return {r[0] for r in self._in(
            tx, "SELECT DISTINCT s.material_id FROM ws_index_slices s WHERE s.material_id = ANY(:ids) AND NOT EXISTS "
                "(SELECT 1 FROM ws_index_vectors v WHERE v.signature=:s AND v.text_sha=s.text_sha)", tx.scope, s=signature)}

    def failed(self, tx) -> set[int]:
        return {r[0] for r in self._in(tx, "SELECT material_id FROM ws_index_entries WHERE material_id = ANY(:ids) AND error IS NOT NULL", tx.scope)}


class _Tx:
    def __init__(self, connection) -> None:
        self.connection = connection
        self.scope: list[int] = []
