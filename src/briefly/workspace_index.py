"""工作区（材料库）语义索引：默认可见的材料全部自动建索引，检索前对齐最新，向量按内容复用。

与知识库索引分开存放（独立 SQLite 文件），不进知识库的版本、发布与代次体系，每份材料只保留最新内容：
- 切片沿用知识库的 token_slices（v5），向量用同一个本地 Embedding，检索沿用语义＋关键词两路 RRF 与相关度下限；
- 切片向量按（Embedding 指纹, embedding_text 的 SHA256）缓存：材料改动（追踪新版本、重新上传）后只有内容变了的
  切片需要重新向量化；
- 范围＝materials.visible()：用户添加的材料加追踪文件最新版本，不含追踪旧版本和知识库专属材料（隔离规则）；
- 添加或删除材料后由后台线程同步；检索前再比对一次材料 content_hash 与索引记录，不一致的先增量更新。
  关键词索引随切片即时写入；向量没算完的材料先用关键词结果，并在结果里标明。
"""
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import logging
from pathlib import Path
import sqlite3
import threading

from sqlalchemy import select
from sqlalchemy.orm import Session

from .errors import UserInputError
from . import config
from .knowledge.evidence import NOTES, constraints
from .knowledge.index import terms, vector_blob
from .knowledge.retrieval import both_routes, relevance_floor, relevant, similarity
from .knowledge.service import fingerprint
from .knowledge.slicing import token_slices
from .materials import Material, SearchHit, visible

log = logging.getLogger(__name__)

PER_ROUTE = 20
RRF_K = 60
SLICE_TOKENS = 512
SNIPPET_CHARS = 600  # 检索结果每条最多给的原文字数；完整内容用 read_material 读
EMBED_BATCH = 16
SCHEMA = 1


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _markdown(report: dict) -> bool | None:
    # 材料没有保存导入时的 markdown 标志：解析报告认定为 Markdown 结构的按 Markdown 切，其余按内容判断
    return True if (report or {}).get("structure") == "markdown" else None


class WorkspaceIndex:
    """encoder_factory 返回本地 Embedding（identity、token_length、embed_documents、embed_query）；
    未配置或加载失败时索引不可用，调用方回退到原关键词检索。"""

    def __init__(self, engine, path, encoder_factory, *, inline_embed: int | None = None) -> None:
        self.engine = engine
        self.path = Path(path)
        self.encoder_factory = encoder_factory
        self.inline_embed = config.WORKSPACE_INLINE_EMBED if inline_embed is None else inline_embed
        self._lock = threading.Lock()  # 切片与写索引串行；向量计算在锁外
        self._state = threading.Lock()
        self._dirty = False
        self._worker: threading.Thread | None = None
        self._unavailable_logged = False
        self.hooks: list = []  # 每次后台同步之后调用（无参数），不依赖编码器是否可用，如疑似重复检测

    # —— 编码器与存储 ——

    def encoder(self):
        try:
            return self.encoder_factory()
        except Exception as exc:
            if not self._unavailable_logged:
                self._unavailable_logged = True
                log.info("工作区语义索引未启用（本地 Embedding 不可用：%s），材料检索使用关键词", exc)
            return None

    @staticmethod
    def signature(encoder) -> str:
        """切片与向量的契约：Embedding 身份＋切片策略。变化时全部材料重新切片（向量缓存按指纹天然失效）。"""
        return fingerprint(dict(embedding=fingerprint(encoder.identity), schema=SCHEMA, slicing="title-token-offsets/v5",
                                tokens=SLICE_TOKENS, lexical="cjk-bigrams-ascii-v1", metric="cosine-normalized-f32"))

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

    # —— 同步 ——

    def schedule(self) -> None:
        """材料添加、删除后调用：后台同步（合并连续的多次通知），不阻塞调用方。"""
        with self._state:
            self._dirty = True
            if self._worker is not None:
                return
            self._worker = threading.Thread(target=self._run, name="workspace-index", daemon=True)
            self._worker.start()

    def _run(self) -> None:
        while True:
            with self._state:
                if not self._dirty:
                    self._worker = None
                    return
                self._dirty = False
            try:
                encoder = self.encoder()
                if encoder is not None:
                    self.sync(encoder)
            except Exception:
                log.warning("工作区索引后台同步失败，检索前会再次对齐", exc_info=True)
            for hook in self.hooks:
                try:
                    hook()
                except Exception:
                    log.warning("工作区同步后的处理失败", exc_info=True)

    def reuse_stat(self) -> dict:
        """向量复用的现状（不是累计）：内容相同的切片共用一条向量，hits 是少算的切片数，misses 是实际算过的向量数。"""
        try:
            with self.connect() as db:
                slices = db.execute("SELECT COUNT(*) FROM slices").fetchone()[0]
                vectors = db.execute("SELECT COUNT(DISTINCT text_sha) FROM slices").fetchone()[0]
        except Exception as e:
            log.warning("读取工作区索引统计失败: %s", type(e).__name__)
            slices = vectors = 0
        return dict(name="embedding.workspace", hits=slices - vectors, misses=vectors, snapshot=True)

    def wait(self, timeout: float = 30) -> None:
        """等后台同步结束（关闭服务、测试用）。"""
        with self._state:
            worker = self._worker
        if worker is not None:
            worker.join(timeout)

    def sync(self, encoder, *, embed_limit: int | None = None) -> dict:
        """让索引与当前工作区一致。返回本次的统计：removed / added / failed / embedded / pending。

        embed_limit：本次最多计算几条新向量（None 不限）；检索前对齐时有上限，没算完的留给后台。"""
        signature = self.signature(encoder)
        with self._lock:
            with Session(self.engine) as s:
                eligible = dict(s.execute(select(Material.id, Material.content_hash).where(visible())).all())
            removed, added, failed = 0, 0, 0
            with self.connect() as db, db:
                entries = {row[0]: (row[1], row[2]) for row in db.execute("SELECT material_id, content_hash, signature FROM entries")}
                stale = [m for m, (digest, sig) in entries.items() if eligible.get(m) != digest or sig != signature]
                for material_id in stale:
                    self._remove(db, material_id)
                removed = len(stale)
                for material_id in sorted(set(eligible) - (set(entries) - set(stale))):
                    added += 1
                    if not self._add(db, material_id, signature, encoder):
                        failed += 1
                if removed:
                    # 只保留仍被切片用到的向量；新版本已在上面写入切片，同内容的向量不会被清掉
                    db.execute("DELETE FROM vectors WHERE NOT EXISTS (SELECT 1 FROM slices s WHERE s.text_sha = vectors.text_sha)")
        embedded, pending = self._embed(encoder, signature, embed_limit)
        return dict(removed=removed, added=added, failed=failed, embedded=embedded, pending=pending)

    @staticmethod
    def _remove(db, material_id: int) -> None:
        db.execute("DELETE FROM lexical WHERE rowid IN (SELECT id FROM slices WHERE material_id=?)", (material_id,))
        db.execute("DELETE FROM slices WHERE material_id=?", (material_id,))
        db.execute("DELETE FROM entries WHERE material_id=?", (material_id,))

    def _add(self, db, material_id: int, signature: str, encoder) -> bool:
        with Session(self.engine) as s:
            material = s.get(Material, material_id)
            if material is None:
                return False
            title, content, digest, report = material.title, material.content, material.content_hash, dict(material.parse_report or {})
        try:
            _, slices = token_slices(content, title, _markdown(report), encoder, tokens=SLICE_TOKENS, report=report)
        except ValueError as exc:
            # 切不了的材料（标题过长等）记下原因，不在每次检索时重试；检索时这份材料用关键词结果
            db.execute("INSERT INTO entries VALUES (?,?,?,?)", (material_id, digest, signature, str(exc)[:200]))
            return False
        for piece in slices:
            rowid = db.execute(
                "INSERT INTO slices(material_id,number,section,start_pos,end_pos,content_sha,text_sha,embedding_text) VALUES (?,?,?,?,?,?,?,?)",
                (material_id, piece.index, piece.section, piece.start, piece.end, sha256(piece.content),
                 sha256(piece.embedding_text), piece.embedding_text)).lastrowid
            db.execute("INSERT INTO lexical(rowid,tokens) VALUES (?,?)", (rowid, " ".join(terms(piece.embedding_text))))
        db.execute("INSERT INTO entries VALUES (?,?,?,NULL)", (material_id, digest, signature))
        return True

    def _missing(self, db, signature: str) -> list[tuple[str, str]]:
        return db.execute("SELECT text_sha, MIN(embedding_text) FROM slices s WHERE NOT EXISTS (SELECT 1 FROM vectors v "
                          "WHERE v.signature=? AND v.text_sha=s.text_sha) GROUP BY text_sha ORDER BY MIN(s.id)",
                          (signature,)).fetchall()

    def _embed(self, encoder, signature: str, limit: int | None) -> tuple[int, int]:
        with self.connect() as db:
            missing = self._missing(db, signature)
        todo = missing if limit is None else missing[:limit]
        dimension = encoder.identity["dimension"]
        for start in range(0, len(todo), EMBED_BATCH):
            batch = todo[start:start + EMBED_BATCH]
            vectors = encoder.embed_documents([text for _, text in batch])
            rows = [(signature, text_sha, vector_blob(vector, dimension)) for (text_sha, _), vector in zip(batch, vectors)]
            with self.connect() as db, db:
                db.executemany("INSERT OR IGNORE INTO vectors VALUES (?,?,?)", rows)
        return len(todo), len(missing) - len(todo)

    # —— 检索 ——

    def search(self, query: str, material_ids: list[int] | None = None, *, limit: int = 10) -> list[SearchHit] | None:
        """混合检索（语义＋关键词，RRF 合并，相关度下限过滤）。返回 None 表示索引不可用或范围里有不在索引中的
        材料（如显式指定的追踪旧版本），由调用方用原关键词检索。material_ids 的知识库专属检查由调用方先做。"""
        query = query.strip()
        if not query:
            raise UserInputError("关键词为空")
        found = self._rank(query, material_ids)
        if found is None:
            return None
        ranked, rows, pending, failed = found
        hits = self._hits(ranked[:limit], rows, pending)
        if failed:
            from .materials import MaterialStore
            hits += [replace(h, keyword_only=True) for h in MaterialStore(self.engine).search(query, sorted(failed))]
        return hits[:limit]

    def evidence(self, query: str, *, limit: int = 5) -> list[dict] | None:
        """联合检索（三期）用：工作区里与问题相关的原文片段，形状与知识库依据一致（标题、章节、原文、续表表头、
        质量限制），另带 origin="workspace"、材料编号、更新时间与稳定引用 wsref。索引不可用时返回 None。
        切片失败的材料不参与（联合检索只用有位置的片段）。"""
        query = query.strip()
        found = self._rank(query, None) if query else None
        if found is None:
            return None
        ranked, rows, pending, _ = found
        ranked = ranked[:limit]
        ids = {rows[e["slice"]][1] for e in ranked}
        with Session(self.engine) as s:
            materials = {m.id: m for m in s.scalars(select(Material).where(Material.id.in_(ids)))}
        with self.connect() as db:
            texts = dict(db.execute(f"SELECT id, embedding_text FROM slices WHERE id IN ({','.join('?' * len(ranked))})",
                                    [e["slice"] for e in ranked])) if ranked else {}
        from types import SimpleNamespace
        from .knowledge.service import _table_header
        out = []
        for e in ranked:
            _, material_id, number, section, start, end = rows[e["slice"]]
            material = materials.get(material_id)
            if material is None:
                continue
            content = material.content[start:end]
            out.append(dict(origin="workspace", reference=f"wsref:{material_id}:{start}:{end}:{sha256(content)[:16]}",
                            material_id=material_id, document_id=f"ws:{material_id}", version_id=material.content_hash,
                            number=None, business_version=None, source=dict(title=material.title), section=section or None,
                            start=start, end=end, content=content, updated_at=material.created_at,
                            table_header=_table_header(SimpleNamespace(content=content, embedding_text=texts.get(e["slice"]))),
                            constraints=constraints(material.parse_report or {}, start, end),
                            keyword_only=material_id in pending and similarity(e) is None,
                            similarity=similarity(e), retrieval=e["retrieval"]))
        return out

    def _rank(self, query: str, material_ids: list[int] | None):
        """(排序后的候选, 切片行, 向量未就绪的材料, 切片失败的材料)；索引不可用或范围不在索引里时返回 None。"""
        encoder = self.encoder()
        if encoder is None:
            return None
        self.sync(encoder, embed_limit=self.inline_embed)
        signature = self.signature(encoder)
        with Session(self.engine) as s:
            eligible = set(s.scalars(select(Material.id).where(visible())))
        if material_ids is not None:
            if not set(material_ids) <= eligible:
                return None
            eligible = set(material_ids)
        if not eligible:
            return [], {}, set(), set()
        vector = vector_blob(encoder.embed_query(query), encoder.identity["dimension"])
        with self.connect() as db:
            db.execute("CREATE TEMP TABLE scope (material_id INTEGER PRIMARY KEY)")
            db.executemany("INSERT INTO scope VALUES (?)", ((x,) for x in eligible))
            dense = db.execute(
                "SELECT s.id, vec_distance_cosine(v.embedding, ?) AS distance FROM slices s JOIN scope USING(material_id) "
                "JOIN vectors v ON v.signature=? AND v.text_sha=s.text_sha ORDER BY distance, s.id LIMIT ?",
                (vector, signature, PER_ROUTE)).fetchall()
            match = " OR ".join('"' + term + '"' for term in terms(query, query=True))
            lexical = [] if not match else db.execute(
                "SELECT s.id, bm25(lexical) AS score FROM lexical JOIN slices s ON s.id=lexical.rowid JOIN scope USING(material_id) "
                "WHERE lexical MATCH ? ORDER BY score, s.id LIMIT ?", (match, PER_ROUTE)).fetchall()
            scores, routes = {}, {}
            for route, ranking in (("semantic", dense), ("lexical", lexical)):
                for rank, (slice_id, raw) in enumerate(ranking, 1):
                    scores[slice_id] = scores.get(slice_id, 0) + 1 / (RRF_K + rank)
                    routes.setdefault(slice_id, []).append(dict(route=route, rank=rank, raw_score=raw))
            rows = {} if not scores else {r[0]: r for r in db.execute(
                f"SELECT id, material_id, number, section, start_pos, end_pos FROM slices WHERE id IN ({','.join('?' * len(scores))})",
                list(scores))}
            pending = {r[0] for r in db.execute(
                "SELECT DISTINCT s.material_id FROM slices s JOIN scope USING(material_id) WHERE NOT EXISTS "
                "(SELECT 1 FROM vectors v WHERE v.signature=? AND v.text_sha=s.text_sha)", (signature,))}
            failed = {r[0] for r in db.execute("SELECT material_id FROM entries JOIN scope USING(material_id) WHERE error IS NOT NULL")}
        ranked = [dict(slice=slice_id, retrieval=dict(rrf_score=score, routes=routes[slice_id]))
                  for slice_id, score in sorted(scores.items(), key=lambda row: (-row[1], row[0]))]
        floor = relevance_floor(encoder.identity) if ranked else None
        if floor is not None:
            # 与知识库文档定位同一口径：整体不相关时只留关键词命中；否则去掉低于下限、又没有被两路同时命中的条目
            if not relevant(ranked, floor):
                ranked = [e for e in ranked if any(r["route"] == "lexical" for r in e["retrieval"]["routes"])]
            else:
                ranked = [e for e in ranked if (x := similarity(e)) is None or x >= floor or both_routes(e)]
        return ranked, rows, pending, failed

    def _hits(self, ranked, rows, pending) -> list[SearchHit]:
        ids = {rows[e["slice"]][1] for e in ranked}
        with Session(self.engine) as s:
            materials = {m.id: m for m in s.scalars(select(Material).where(Material.id.in_(ids)))}
            from .materials import MaterialChunk
            bounds = {}
            for e in ranked:
                _, material_id, _, _, start, end = rows[e["slice"]]
                bounds[e["slice"]] = s.scalar(select(MaterialChunk.index).where(
                    MaterialChunk.material_id == material_id, MaterialChunk.start < end, MaterialChunk.end > start
                ).order_by(MaterialChunk.index).limit(1)) or 1
        hits = []
        for e in ranked:
            _, material_id, _, section, start, end = rows[e["slice"]]
            material = materials.get(material_id)
            if material is None:
                continue
            text = material.content[start:end].strip()
            snippet = text if len(text) <= SNIPPET_CHARS else text[:SNIPPET_CHARS] + "…"
            lexical = [r for r in e["retrieval"]["routes"] if r["route"] == "lexical"]
            semantic = similarity(e)
            limits = constraints(material.parse_report or {}, start, end)
            hits.append(SearchHit(material_id, material.title, bounds[e["slice"]], section, len(lexical), snippet,
                                  start=start, end=end, similarity=semantic,
                                  keyword_only=semantic is None and material_id in pending,
                                  notes=tuple(NOTES[k] for k in sorted(limits))))
        return hits


def attach(store, encoder_factory=None) -> WorkspaceIndex:
    """给材料库接上工作区索引：材料增删后后台同步，search_materials 用混合检索。
    encoder_factory 不传时按配置加载本地 Embedding（命令行用；网页后端传入与知识库共用的那一个）。"""
    if encoder_factory is None:
        encoder_factory = _local_encoder()
    path = config.WORKSPACE_INDEX_PATH or str(Path(config.KNOWLEDGE_DIR) / "workspace.sqlite")
    index = WorkspaceIndex(store.engine, path, encoder_factory)
    store.workspace_index = index
    store.listeners.append(index.schedule)
    return index


def _local_encoder():
    encoder, lock = None, threading.Lock()

    def factory():
        nonlocal encoder
        with lock:
            if encoder is None:
                if not config.EMBEDDING_DIR:
                    raise UserInputError("尚未配置本地Embedding目录BRIEFLY_EMBEDDING_DIR")
                from .llm import create_local_embeddings
                encoder = create_local_embeddings(config.EMBEDDING_DIR)
            return encoder
    return factory
