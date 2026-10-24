"""隔离材料检索实验：不改正式库、不调用模型，文件名不进入 pytest 收集。

运行：.venv/Scripts/python.exe tests/eval/material_search_benchmark.py --repeats 20
输出为 files/eval/material-search-<时间>/，每次新建，拒绝覆盖。
原型仅用于选型；未实现正式迁移、自动维护、并发补建或 Postgres 加速。
"""
from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import platform
import random
import sqlite3
import statistics
import sys
import time
import tracemalloc
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import event, func, insert, or_, select, text

from briefly.db import create_db_engine
from briefly.materials import (
    MAX_SEARCH_HITS, SNIPPET_RADIUS, TRACKED, Material, MaterialChunk,
    MaterialStore, SearchHit, visible,
)
from briefly.tracking.models import FileSnapshot, TrackedFile

REPO = Path(__file__).resolve().parents[2]
METHODS = ("baseline", "sql_filter", "fts_bigram")


def words_of(keywords):
    words = [w.lower() for w in keywords.split() if w]
    if not words:
        raise ValueError("关键词为空")
    return words


def token(pair):
    # ASCII 编码避免标点、NUL、组合字符进入 FTS 查询语法或被分词器拆开。
    return "g" + "x".join(format(ord(c), "x") for c in pair)


def grams(content):
    normalized = content.lower()
    return " ".join(sorted({token(normalized[i:i + 2]) for i in range(len(normalized) - 1)}))


def expression(words):
    return " OR ".join("(" + " AND ".join(
        '"' + t + '"' for t in sorted({token(w[i:i + 2]) for i in range(len(w) - 1)})
    ) + ")" for w in words)


def candidate_query(keywords, scope, method):
    words = words_of(keywords)
    query = select(MaterialChunk, Material.title).join(Material)
    query = query.where(Material.id.in_(scope)) if scope is not None else query.where(visible())
    if method == "sql_filter" or (method == "fts_bigram" and any(len(w) < 2 for w in words)):
        # 原生 SQLite lower() 与 Python lower() 不等价；UDF 保留现有规则。
        query = query.where(or_(*(func.instr(func.py_lower(MaterialChunk.content), w) > 0 for w in words)))
    elif method == "fts_bigram":
        candidate_ids = text("SELECT rowid FROM search_grams WHERE search_grams MATCH :terms").bindparams(terms=expression(words))
        query = query.where(MaterialChunk.id.in_(candidate_ids))
    return query.order_by(Material.id, MaterialChunk.index)


def finish(rows, keywords):
    words = words_of(keywords)
    hits = []
    chars = 0
    for chunk, title in rows:
        chars += len(chunk.content)
        lower = chunk.content.lower()
        count = sum(lower.count(w) for w in words)
        if not count:
            continue
        first = min(pos for w in words if (pos := lower.find(w)) >= 0)
        start, end = max(0, first - SNIPPET_RADIUS), first + SNIPPET_RADIUS
        snippet = ("…" if start else "") + chunk.content[start:end] + ("…" if end < len(chunk.content) else "")
        hits.append(SearchHit(chunk.material_id, title, chunk.index, chunk.section, count, snippet))
    hits.sort(key=lambda hit: hit.hits, reverse=True)
    return hits[:MAX_SEARCH_HITS], {"loaded_rows": len(rows), "loaded_characters": chars, "true_hit_rows": len(hits)}


def search(store, keywords, scope, method):
    if method == "baseline":
        return store.search(keywords, scope)
    with store._session() as session:
        rows = session.execute(candidate_query(keywords, scope, method)).all()
    return finish(rows, keywords)[0]


def load_stats(store, keywords, scope, method):
    with store._session() as session:
        rows = session.execute(candidate_query(keywords, scope, method)).all()
    return finish(rows, keywords)[1]


def make_engine(path):
    engine = create_db_engine("sqlite:///" + path.as_posix())

    def register(connection, _):
        connection.create_function("py_lower", 1, lambda value: value.lower(), deterministic=True)

    event.listen(engine, "connect", register)
    with engine.connect() as connection:
        register(connection.connection.driver_connection, None)
    return engine


def create_index(engine):
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE VIRTUAL TABLE search_grams USING fts5(tokens, content='', detail='none', columnsize=0, tokenize='ascii')")


def build_index(engine):
    started = time.perf_counter()
    with engine.begin() as connection:
        rows = connection.execute(select(MaterialChunk.id, MaterialChunk.content)).all()
        for start in range(0, len(rows), 100):
            batch = [(row.id, grams(row.content)) for row in rows[start:start + 100]]
            connection.exec_driver_sql("INSERT INTO search_grams(rowid,tokens) VALUES (?,?)", batch)
        connection.exec_driver_sql("INSERT INTO search_grams(search_grams) VALUES ('optimize')")
    return (time.perf_counter() - started) * 1000


def clear_index(connection):
    connection.exec_driver_sql("INSERT INTO search_grams(search_grams) VALUES ('delete-all')")


def add_index_rows(connection, material_id):
    for row in connection.execute(select(MaterialChunk.id, MaterialChunk.content).where(MaterialChunk.material_id == material_id)):
        connection.exec_driver_sql("INSERT INTO search_grams(rowid,tokens) VALUES (?,?)", (row.id, grams(row.content)))


def delete_index_rows(connection, material_id):
    for row in connection.execute(select(MaterialChunk.id, MaterialChunk.content).where(MaterialChunk.material_id == material_id)):
        connection.exec_driver_sql("INSERT INTO search_grams(search_grams,rowid,tokens) VALUES ('delete',?,?)", (row.id, grams(row.content)))


def payload(i, rng):
    prefix = f"虚构记录{i:06d} 本周项目进展。"
    if i % 97 == 0:
        prefix += "预算已确认，预算调整记录。"
    if i % 499 == 0:
        prefix += "支付预算审批完成。"
    if i % 23 == 0:
        prefix += "Release APIv2 接口联调完成。"
    if i % 37 == 0:
        prefix += '字段 rate_50% C++ v1.2 "引号" a\\b 已确认。'
    if i % 61 == 0:
        prefix += "ÄPFEL ΟΣ İSTANBUL 😀 数据核验。"
    if i % 43 == 0:
        prefix += "款项已核对。"
    subjects = ("账号", "页面", "通知", "工单", "记录", "报表", "权限", "缓存", "日志", "附件", "模块", "版本")
    verbs = ("检查", "整理", "复核", "更新", "验证", "同步", "归档", "提交", "讨论", "梳理", "维护", "确认")
    phrases = []
    while len(prefix) + sum(map(len, phrases)) < 1500:
        phrases.append(f"{rng.choice(subjects)}{rng.choice(verbs)}记录{rng.randrange(10000):04d}，结果待复核。")
    return (prefix + "".join(phrases))[:1500]


def seed(engine, count, seed_value):
    rng = random.Random(seed_value)
    materials, chunks = [], []
    for start in range(0, count, 10):
        material_id = start // 10 + 1
        texts = [payload(i, rng) for i in range(start, min(start + 10, count))]
        content = "\n".join(texts)
        materials.append(dict(id=material_id, title=f"虚构办公材料{material_id}",
            source=TRACKED if (material_id - 1) % 20 in (0, 1) else "paste",
            content=content, char_count=len(content), content_hash=hashlib.sha256(content.encode()).hexdigest(), parse_report={}))
        offset = 0
        for j, value in enumerate(texts, 1):
            chunks.append(dict(id=start + j, material_id=material_id, index=j, section=f"章节{j}",
                start=offset, end=offset + len(value), content=value, char_count=len(value)))
            offset += len(value) + 1
    started = time.perf_counter()
    with engine.begin() as connection:
        connection.execute(insert(Material), materials)
        for i in range(0, len(chunks), 100):
            connection.execute(insert(MaterialChunk), chunks[i:i + 100])
        for old in range(1, len(materials), 20):
            path = f"virtual://fixture/{old}"
            file_id = connection.execute(insert(TrackedFile).values(path=path, path_key=path, file_type="txt")).inserted_primary_key[0]
            for version, material_id in ((1, old), (2, old + 1)):
                connection.execute(insert(FileSnapshot).values(file_id=file_id, version=version, kind="baseline",
                    sha256=f"fixture-{material_id}", size=15000, mtime=0, path=path, material_id=material_id))
    return (time.perf_counter() - started) * 1000


def canonical(hits):
    return [asdict(hit) for hit in hits]


def correctness(engine, store):
    cases = []
    queries = ["预算", "接口", "支付预算审批", "款", "RELEASE", "release 预算", "预算 预算", "不存在的斑马协议",
        "%", "_", "C++", '"引号"', "a\\b", "ÄPFEL", "ΟΣ", "İSTANBUL", "😀", "APIv2", "2026-10-24", "a a", "\x00"]
    for query in queries:
        for scope in (None, [], [1], [2], [3, 4], [999999]):
            expected = canonical(store.search(query, scope))
            for method in METHODS[1:]:
                actual = canonical(search(store, query, scope, method))
                cases.append(dict(query=query, scope=scope, method=method, passed=actual == expected))
                assert actual == expected, (query, scope, method, actual, expected)
    # 随机 Unicode / 特殊字符，保证包含真实子串以及乱序片段造成的假候选。
    rng = random.Random(8127)
    alphabet = '预算接口AbÄΣİé😀_%"\\0123\x00'
    ids = []
    for i in range(30):
        body = "".join(rng.choice(alphabet) for _ in range(80)) + f"唯一行{i}"
        material, _ = store.add_text(f"边界{i}", body)
        ids.append(material.id)
        with engine.begin() as connection:
            add_index_rows(connection, material.id)
    for _ in range(150):
        if rng.random() < 0.7:
            content = store.get(rng.choice(ids)).content
            start = rng.randrange(len(content) - 5)
            query = content[start:start + rng.randint(1, 5)]
        else:
            query = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 5)))
        if rng.random() < 0.3:
            query += " 预算"
        scope = rng.choice([None, ids, ids[:5], []])
        expected = canonical(store.search(query, scope))
        for method in METHODS[1:]:
            actual = canonical(search(store, query, scope, method))
            cases.append(dict(query=query, scope=scope, method=method, passed=actual == expected))
            assert actual == expected, (query, scope, method, actual, expected)
    for query in ("", " \t\n"):
        for method in METHODS:
            try:
                search(store, query, None, method)
            except ValueError:
                cases.append(dict(query=query, method=method, passed=True))
            else:
                raise AssertionError("空查询未拒绝")
    # 含有所需二元片段，但不含完整词：不能误命中。
    # 最高分在较晚写入的记录中：不能对候选先截断再做精确排序。
    adversarial = []
    for i in range(25):
        content = f"对照{i} 支付，付预，预算，算审，审批。"
        if i == 24:
            content += "支付预算审批" * 30
        material, _ = store.add_text(f"排序对照{i}", content)
        adversarial.append(material.id)
        with engine.begin() as connection:
            add_index_rows(connection, material.id)
    for query in ("支付预算审批", "预算", "预算 预算"):
        expected = canonical(store.search(query, adversarial))
        for method in METHODS[1:]:
            actual = canonical(search(store, query, adversarial, method))
            assert actual == expected
            cases.append(dict(query=query, scope=adversarial, method=method, passed=True))
    return cases


def controls(engine):
    with engine.begin() as connection:
        native = connection.exec_driver_sql("SELECT lower('ÄPFEL'), py_lower('ÄPFEL')").one()
        connection.exec_driver_sql("CREATE VIRTUAL TABLE tri_control USING fts5(content, tokenize='trigram')")
        connection.exec_driver_sql("INSERT INTO tri_control(content) VALUES ('支付预算审批完成')")
        two = connection.exec_driver_sql("SELECT count(*) FROM tri_control WHERE tri_control MATCH ?", ("预算",)).scalar_one()
        three = connection.exec_driver_sql("SELECT count(*) FROM tri_control WHERE tri_control MATCH ?", ("预算审",)).scalar_one()
        connection.exec_driver_sql("DROP TABLE tri_control")
        return dict(native_lower=native[0], python_lower=native[1], trigram_two_char_hits=two, trigram_three_char_hits=three)


def lifecycle(engine, store):
    checks = []
    mid = store.add_text("原型生命周期", "索引生命周期核验，预算预算预算。" * 8)[0].id
    with engine.begin() as connection:
        add_index_rows(connection, mid)
    assert canonical(search(store, "生命周期", [mid], "fts_bigram")) == canonical(store.search("生命周期", [mid]))
    checks.append("新增后同步可查")
    # 同一个事务中清索引并删除来源，主动回滚，两边都应保留。
    with engine.connect() as connection:
        transaction = connection.begin()
        delete_index_rows(connection, mid)
        connection.execute(MaterialChunk.__table__.delete().where(MaterialChunk.material_id == mid))
        transaction.rollback()
    assert search(store, "生命周期", [mid], "fts_bigram") == store.search("生命周期", [mid])
    checks.append("来源与索引删除事务回滚")
    with engine.begin() as connection:
        delete_index_rows(connection, mid)
        connection.execute(MaterialChunk.__table__.delete().where(MaterialChunk.material_id == mid))
        connection.execute(Material.__table__.delete().where(Material.id == mid))
    assert search(store, "生命周期", [mid], "fts_bigram") == []
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT count(*) FROM search_grams WHERE search_grams MATCH ?", (expression(["生命周期"]),)).scalar_one() == 0
    checks.append("提交删除后无残留候选")
    expected = canonical(store.search("预算"))
    for _ in range(2):
        with engine.begin() as connection:
            clear_index(connection)
        build_index(engine)
        assert canonical(search(store, "预算", None, "fts_bigram")) == expected
    checks.append("两次清空重建结果一致")
    # 中断发生在一个事务内：验证回滚能恢复，而非声称已实现跨事务续建。
    with engine.connect() as connection:
        transaction = connection.begin()
        clear_index(connection)
        transaction.rollback()
    assert canonical(search(store, "预算", None, "fts_bigram")) == expected
    checks.append("重建事务中断回滚保留旧索引")
    return checks


def measure(call, repeats):
    first_start = time.perf_counter()
    call()
    first = (time.perf_counter() - first_start) * 1000
    samples = []
    for _ in range(repeats):
        start = time.perf_counter()
        call()
        samples.append((time.perf_counter() - start) * 1000)
    gc.collect()
    tracemalloc.start()
    call()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return dict(first_observed_ms=first, median_ms=statistics.median(samples),
        p95_ms=sorted(samples)[math.ceil(len(samples) * .95) - 1], samples_ms=samples,
        python_peak_bytes=peak)


def benchmark_search(store, repeats, count):
    workloads = [("rare_zh", "预算", None), ("long_zh", "支付预算审批", None),
        ("common_zh", "本周", None), ("single_zh_fallback", "款", None),
        ("english", "RELEASE", None), ("multi_or", "release 预算", None),
        ("absent", "不存在的斑马协议", None), ("selected_10_chunks", "预算", [1]),
        ("empty_scope", "预算", [])]
    results = []
    for position, (name, query, scope) in enumerate(workloads):
        expected = canonical(store.search(query, scope))
        # 每种方法交错执行，避免某一种总是承担首次访问的成本。
        methods = list(METHODS)
        methods = methods[position % 3:] + methods[:position % 3]
        for method in methods:
            call = lambda: search(store, query, scope, method)
            assert canonical(call()) == expected
            metrics = measure(call, repeats)
            metrics.update(load_stats(store, query, scope, method))
            results.append(dict(chunks=count, workload=name, query=query, scope=scope,
                method=method, **metrics))
    return results


def benchmark_read(store, repeats):
    def current():
        material = store.get(1)
        chunks = store.chunks(1)
        return material.title, chunks[5].content, len(chunks)

    def direct():
        with store._session() as session:
            title = session.scalar(select(Material.title).where(Material.id == 1))
            count = session.scalar(select(func.count()).select_from(MaterialChunk).where(MaterialChunk.material_id == 1))
            content = session.scalar(select(MaterialChunk.content).where(MaterialChunk.material_id == 1, MaterialChunk.index == 6))
        return title, content, count

    assert current() == direct()
    result = {"current": measure(current, repeats), "direct_before_index": measure(direct, repeats)}
    with store.engine.begin() as connection:
        result["plan_before"] = [list(row) for row in connection.exec_driver_sql('EXPLAIN QUERY PLAN SELECT content FROM material_chunks WHERE material_id=1 AND "index"=6')]
        connection.exec_driver_sql('CREATE INDEX experiment_material_chunk ON material_chunks(material_id,"index")')
        result["plan_after"] = [list(row) for row in connection.exec_driver_sql('EXPLAIN QUERY PLAN SELECT content FROM material_chunks WHERE material_id=1 AND "index"=6')]
    result["direct_after_index"] = measure(direct, repeats)
    result["current_loaded_characters"] = 2 * len(store.get(1).content) + sum(len(c.content) for c in store.chunks(1))
    result["direct_loaded_characters"] = 1500
    result["material_chunks"] = store.chunk_counts([1])[1]
    return result


def run_long_read(root, source_path, repeats):
    source = make_engine(source_path)
    try:
        with source.connect() as connection:
            texts = list(connection.scalars(select(MaterialChunk.content).order_by(MaterialChunk.id)))
    finally:
        source.dispose()
    engine = make_engine(root / "long-material-read.db")
    try:
        content = "\n".join(texts)
        with engine.begin() as connection:
            connection.execute(insert(Material).values(id=1, title="虚构长材料", source="paste", content=content,
                char_count=len(content), content_hash=hashlib.sha256(content.encode()).hexdigest(), parse_report={}))
            for start in range(0, len(texts), 100):
                connection.execute(insert(MaterialChunk), [dict(material_id=1, index=i + 1, section="",
                    start=i * 1501, end=i * 1501 + len(texts[i]), content=texts[i], char_count=len(texts[i]))
                    for i in range(start, min(start + 100, len(texts)))])
        del content, texts
        gc.collect()
        return benchmark_read(MaterialStore(engine), repeats)
    finally:
        engine.dispose()


def maintenance(engine, indexed, count=30):
    rng = random.Random(711)
    insert_samples, delete_samples = [], []
    for i in range(count):
        content = payload(100000 + i, rng)
        start = time.perf_counter()
        with engine.begin() as connection:
            mid = connection.execute(insert(Material).values(title="写入计时", source="paste", content=content,
                char_count=len(content), content_hash=hashlib.sha256(content.encode()).hexdigest(), parse_report={})).inserted_primary_key[0]
            chunk_id = connection.execute(insert(MaterialChunk).values(material_id=mid, index=1, section="", start=0,
                end=len(content), content=content, char_count=len(content))).inserted_primary_key[0]
            if indexed:
                connection.exec_driver_sql("INSERT INTO search_grams(rowid,tokens) VALUES (?,?)", (chunk_id, grams(content)))
        insert_samples.append((time.perf_counter() - start) * 1000)
        start = time.perf_counter()
        with engine.begin() as connection:
            if indexed:
                delete_index_rows(connection, mid)
            connection.execute(MaterialChunk.__table__.delete().where(MaterialChunk.material_id == mid))
            connection.execute(Material.__table__.delete().where(Material.id == mid))
        delete_samples.append((time.perf_counter() - start) * 1000)
    return dict(insert_median_ms=statistics.median(insert_samples), delete_median_ms=statistics.median(delete_samples),
        insert_samples_ms=insert_samples, delete_samples_ms=delete_samples)


def run_scale(root, count, repeats):
    path = root / f"chunks-{count}.db"
    engine = make_engine(path)
    try:
        seed_ms = seed(engine, count, 10424)
        before = maintenance(engine, False)
        base_bytes = path.stat().st_size
        create_index(engine)
        index_ms = build_index(engine)
        indexed_bytes = path.stat().st_size
        after = maintenance(engine, True)
        store = MaterialStore(engine)
        search_results = benchmark_search(store, repeats, count)
        read_results = benchmark_read(store, repeats)
        with engine.connect() as connection:
            query = candidate_query("预算", None, "fts_bigram")
            compiled = str(query.compile(engine, compile_kwargs={"literal_binds": True}))
            plan = [list(row) for row in connection.exec_driver_sql("EXPLAIN QUERY PLAN " + compiled)]
        print(f"{count} chunks: build={index_ms:.0f}ms, base={base_bytes}, indexed={indexed_bytes}", flush=True)
        return dict(chunks=count, seed_ms=seed_ms, base_bytes=base_bytes, indexed_bytes=indexed_bytes,
            index_build_ms=index_ms, maintenance_before=before, maintenance_after=after,
            search=search_results, read=read_results, fts_query_plan=plan)
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=[100, 1000, 10000])
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.repeats < 5 or any(n < 20 or n % 10 for n in args.sizes):
        parser.error("重复次数至少 5；块数须为不小于 20 的 10 的倍数")
    stamp = datetime.now(ZoneInfo("Australia/Sydney")).strftime("%Y%m%d-%H%M%S")
    root = args.output or REPO / "files" / "eval" / f"material-search-{stamp}"
    root = root.resolve()
    if not root.is_relative_to(REPO / "files"):
        parser.error("实验输出必须在仓库 files/ 内，避免误用正式数据库")
    root.mkdir(parents=True, exist_ok=False)
    (root / "benchmark-source.py").write_bytes(Path(__file__).read_bytes())
    result = dict(started_at=datetime.now(ZoneInfo("Australia/Sydney")).isoformat(),
        python=sys.version, sqlite=sqlite3.sqlite_version, platform=platform.platform(),
        repeats=args.repeats, seed=10424, script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        limitations=["合成办公语料，每块1500字符，每份材料10块；未测真实用户数据或真实模型。",
            "first_observed不是操作系统冷缓存测试；正确性检查已经访问数据库。",
            "tracemalloc只测Python堆，不包含SQLite原生内存和操作系统页缓存。",
            "SQL过滤使用Python lower UDF保持Unicode语义，并非纯原生SQL性能。",
            "FTS单字或含单字的多词查询回退SQL过滤；高频候选仍需精确核验与排序。",
            "维护成本含单块事务提交；未测并发、长事务、生产迁移或Postgres。"])
    correctness_engine = make_engine(root / "correctness.db")
    try:
        seed(correctness_engine, 100, 10424)
        create_index(correctness_engine)
        build_index(correctness_engine)
        store = MaterialStore(correctness_engine)
        result["controls"] = controls(correctness_engine)
        result["correctness"] = correctness(correctness_engine, store)
        result["lifecycle"] = lifecycle(correctness_engine, store)
        print(f"Correctness: {len(result['correctness'])} comparisons passed; lifecycle: {len(result['lifecycle'])}", flush=True)
    finally:
        correctness_engine.dispose()
    (root / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    result["scales"] = []
    for size in args.sizes:
        result["scales"].append(run_scale(root, size, args.repeats))
        (root / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    result["long_material_read"] = run_long_read(root, root / f"chunks-{max(args.sizes)}.db", args.repeats)
    result["completed_at"] = datetime.now(ZoneInfo("Australia/Sydney")).isoformat()
    (root / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(root, flush=True)


if __name__ == "__main__":
    main()
