"""W3-02 隔离索引规模实验；衍生向量只证明性能/工程正确性，不证明语义质量。"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import hashlib
from importlib.metadata import version
import json
import math
import os
from pathlib import Path
import platform
from queue import Empty, Queue
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from zoneinfo import ZoneInfo

import numpy as np
import psutil

from w3_retrieval_support import CandidateIndex, rrf

ROOT = Path(__file__).resolve().parents[2]
NETWORK_ATTEMPTS = []


def offline():
    def deny(*args, **kwargs):
        NETWORK_ATTEMPTS.append("blocked")
        raise RuntimeError("本地规模实验禁止网络连接")
    socket.socket.connect = deny
    socket.socket.connect_ex = deny


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def metrics(values):
    return dict(count=len(values), p50_ms=float(np.percentile(values, 50)),
                p95_ms=float(np.percentile(values, 95)), max_ms=max(values))


def documents_for(size):
    return [f"doc-{i:05d}" for i in range(math.ceil(size / 100))]


def query_scope(size, kind):
    docs = documents_for(size)
    if kind == "all":
        return docs
    if kind == "kb":
        return [doc for i, doc in enumerate(docs) if i % 10 == 0]
    if kind == "few":
        return docs[:2]
    raise ValueError(kind)


def oracle(vectors, query, scope, limit=10):
    """独立 NumPy float64 精确余弦；以距离容差处理后端同距排序差异。"""
    allowed = {int(doc.split("-")[1]) for doc in scope}
    ids = np.array([i for i in range(len(vectors)) if i // 100 in allowed])
    selected = vectors[ids].astype(np.float64)
    q = np.asarray(query, dtype=np.float64)
    distance = 1 - (selected @ q) / (np.linalg.norm(selected, axis=1) * np.linalg.norm(q))
    order = np.lexsort((ids, distance))[:limit]
    return [(int(ids[i]) + 1, float(distance[i])) for i in order]


def validate_hits(hits, expected, scope):
    allowed = set(scope)
    return (len(hits) == len(expected)
            and len({ident for ident, _ in hits}) == len(hits)
            and all(f"doc-{(ident - 1) // 100:05d}" in allowed for ident, _ in hits)
            and all(abs(actual[1] - want[1]) <= 2e-6 for actual, want in zip(hits, expected)))


def summarize(folder, sizes, requests):
    rows = []
    complete = True
    for size in sizes:
        for backend in ("scalar", "vec0"):
            path = folder / f"{backend}-{size}.json"
            reopen_path = folder / f"{backend}-{size}-reopen.json"
            if not path.exists() or not reopen_path.exists():
                rows.append(dict(size=size, backend=backend, ok=False, error="缺少执行结果"))
                complete = False
                continue
            result = json.loads(path.read_text(encoding="utf-8"))
            reopen = json.loads(reopen_path.read_text(encoding="utf-8"))
            checks = [r["ok"] for r in result["correctness"] + result["lifecycle"]]
            checks.extend(r["ok"] for c in result["concurrency"] for r in c["raw"])
            ok = (len(result["correctness"]) == 18 and len(result["lifecycle"]) == 7
                  and len(result["concurrency"]) == 6
                  and all(len(c["raw"]) == requests for c in result["concurrency"])
                  and all(checks) and reopen["ok"]
                  and result["network_attempts"] == reopen["network_attempts"] == 0)
            complete = complete and ok
            rows.append(dict(size=size, backend=backend, ok=bool(ok), build_ms=result["build_ms"],
                             database_bytes=result["database_bytes"], peak_working_set_bytes=result["peak_working_set_bytes"],
                             oracle_checks=len(result["correctness"]), lifecycle_checks=len(result["lifecycle"]),
                             reopen=reopen, update_ms=result["update_ms"], delete_ms=result["delete_ms"],
                             concurrency=[{k:v for k,v in c.items() if k != "raw"} for c in result["concurrency"]]))
    summary = dict(ok=bool(complete), configurations=rows)
    save(folder / "summary.json", summary)
    lines = ["# W3-02 本地索引递增规模实验", "", f"工程检查：{'全部通过' if complete else '存在失败/缺失，不能验收'}。", "",
             "使用11份原创短文循环存储，512维本地BGE种子向量加固定随机扰动后归一化。衍生向量与原文不对应，只测索引性能和过滤正确性，不计语义召回/回答准确率。",
             "每100块为一文档；单库为每10文档选1份；少量文档为前2份。每组突发请求，1或4个读取线程。服务耗时包含两路索引查询与RRF，不含问题Embedding、生成和排队；完成耗时包含排队。",
             "每个规模/后端独立进程，另起新进程验证重开。没有清理操作系统缓存，首次查询和重开均不能称为冷启动。没有并发写入或持久化故障注入。", ""]
    for row in rows:
        lines.append(f"## {row['size']}块 / {row['backend']}")
        lines.append("")
        if "error" in row:
            lines.extend([row["error"], ""])
            continue
        peak = row["peak_working_set_bytes"]
        lines.append(f"检查{'通过' if row['ok'] else '失败'}；建库{row['build_ms']/1000:.2f}秒；磁盘{row['database_bytes']/1048576:.2f}MiB；进程峰值工作集{peak/1048576 if peak else 0:.2f}MiB（含衍生向量映射、独立oracle及Python，不是纯索引内存）。")
        lines.append(f"18次独立NumPy余弦/范围核对；7项增改删、事务回滚、空范围、模型/维度身份检查；新进程重开{'通过' if row['reopen']['ok'] else '失败'}。")
        lines.append("")
        for c in row["concurrency"]:
            m = c["summary"]
            lines.append(f"- {c['scope']} / {c['workers']}线程 / {c['requests']}请求：服务P50/P95={m['service_ms']['p50_ms']:.2f}/{m['service_ms']['p95_ms']:.2f}ms；完成P95={m['completion_ms']['p95_ms']:.2f}ms；吞吐={c['throughput_per_second']:.2f}请求/秒。")
        lines.append("")
    lines.extend(["## 证据与适用边界", "", "summary.json为汇总；各后端-规模.json保留实际命中、独立oracle、逐请求分段耗时及生命周期结果；runs.json和日志保留退出码/失败；manifest.json记录本机资源、版本、种子、源文件与向量哈希。", "",
                  "这是本机、固定数据分布与范围形状下的重复工作负载。未限制进程CPU/内存，不是目标服务器容量验收；小文档循环和向量扰动不代表真实长文分布。词法词元为去重集合，不代表标准词频BM25。",
                  "W3-02仍需代表性长文/干扰语料、新文档家族分层题集、模型与切片联合比较。不能据此完成最终Embedding/索引选型；正式语义召回和RAG问答仍未交付。", ""])
    (folder / "report.md").write_text("\n".join(lines), encoding="utf-8")
    return summary


def worker(args):
    """每个规模/后端独立新进程，峰值内存不沿用上一档。"""
    folder = args.output
    seed = np.load(folder / "seeds.npz")
    metadata = json.loads((folder / "seeds.json").read_text(encoding="utf-8"))
    vectors = np.load(folder / f"vectors-{args.size}.npy", mmap_mode="r")
    identity = metadata["identity"]
    path = folder / f"{args.backend}-{args.size}.db"
    result_path = folder / f"{args.backend}-{args.size}.json"
    if args.reopen:
        started = time.perf_counter()
        index = CandidateIndex(path, identity, backend=args.backend)
        try:
            hits = index.dense(seed["queries"][0].tolist(), query_scope(args.size, "few"), 10)
            lexical = index.lexical(metadata["questions"][0], query_scope(args.size, "few"), 10)
            expected = oracle(vectors, seed["queries"][0], query_scope(args.size, "few"))
            save(folder / f"{args.backend}-{args.size}-reopen.json", dict(
                ok=validate_hits(hits, expected, query_scope(args.size, "few")) and bool(lexical),
                open_and_first_query_ms=(time.perf_counter() - started) * 1000,
                network_attempts=len(NETWORK_ATTEMPTS)))
        finally:
            index.close()
        return
    if path.exists():
        raise ValueError("索引已存在，拒绝覆盖实验")
    index = CandidateIndex(path, identity, backend=args.backend)
    started = time.perf_counter()
    try:
        for d, doc in enumerate(documents_for(args.size)):
            index.replace_document(doc, [(i + 1, metadata["texts"][i % len(metadata["texts"])], vectors[i].tolist())
                                         for i in range(d * 100, min(args.size, (d + 1) * 100))])
        build_ms = (time.perf_counter() - started) * 1000
        correctness = []
        first = []
        for kind in ("all", "kb", "few"):
            scope = query_scope(args.size, kind)
            for i, q in enumerate(seed["queries"]):
                started = time.perf_counter()
                hits = index.dense(q.tolist(), scope, 10)
                elapsed = (time.perf_counter() - started) * 1000
                expected = oracle(vectors, q, scope)
                correctness.append(dict(scope=kind, query=i, ok=validate_hits(hits, expected, scope),
                                        actual=hits, oracle=expected))
                if i == 0:
                    first.append(dict(scope=kind, first_query_ms=elapsed, cold_cache=False))
        # 正式原型的增改删、事务内冲突与模型身份变更在大索引上重验。
        row = (args.size + 1, "更新前 E771", seed["queries"][0].tolist())
        index.replace_document("mutation", [row])
        mutation = []
        def check(name, ok):
            mutation.append(dict(name=name, ok=bool(ok)))
        check("add", bool(index.lexical("E771", ["mutation"])))
        update_start = time.perf_counter()
        index.replace_document("mutation", [(args.size + 2, "更新后 E772", row[2])])
        update_ms = (time.perf_counter() - update_start) * 1000
        check("update_both_paths", not index.lexical("E771", ["mutation"])
              and bool(index.lexical("E772", ["mutation"])) and index.contents(args.size + 1) is None)
        try:
            index.replace_document("mutation", [(1, "冲突内容", row[2])])
        except sqlite3.IntegrityError:
            check("transaction_rollback", bool(index.lexical("E772", ["mutation"]))
                  and index.dense(row[2], ["mutation"], 1)[0][0] == args.size + 2)
        else:
            check("transaction_rollback", False)
        delete_start = time.perf_counter()
        index.delete_document("mutation")
        delete_ms = (time.perf_counter() - delete_start) * 1000
        check("delete_both_paths", not index.lexical("E772", ["mutation"])
              and not index.dense(row[2], ["mutation"], 1))
        check("empty_scope", not index.dense(row[2], [], 10) and not index.lexical("导出", [], 10))
        for name, change in (("dimension", {**identity, "dimension":3}),
                             ("model", {**identity, "revision":"changed"})):
            try:
                other = CandidateIndex(path, change, backend=args.backend)
            except ValueError:
                check(name + "_guard", True)
            else:
                other.close()
                check(name + "_guard", False)
    finally:
        index.close()

    # 单独连接按实际并发读取；连接预热不计入稳态查询，保留排队和完成时间。
    concurrency = []
    for workers in (1, 4):
        for kind in ("all", "kb", "few"):
            open_lock = threading.Lock()
            def run(connection, i, submitted):
                begin = time.perf_counter()
                q = seed["queries"][i % len(seed["queries"])].tolist()
                scope = query_scope(args.size, kind)
                dense_start = time.perf_counter()
                dense = connection.dense(q, scope, 20)
                lexical_start = time.perf_counter()
                lexical = connection.lexical(metadata["questions"][i % len(metadata["questions"])], scope, 20)
                fuse_start = time.perf_counter()
                fused = rrf([dense, lexical])[:10]
                end = time.perf_counter()
                scope_ok = all(f"doc-{(ident - 1) // 100:05d}" in scope for ident, _ in dense + lexical + fused)
                return dict(request=i, query=i % len(seed["queries"]), dense_hits=dense,
                            lexical_hits=lexical, fused_hits=fused,
                            dense_ms=(lexical_start - dense_start) * 1000,
                            lexical_ms=(fuse_start - lexical_start) * 1000,
                            fusion_ms=(end - fuse_start) * 1000,
                            service_ms=(end - begin) * 1000,
                            queue_ms=(begin - submitted) * 1000,
                            completion_ms=(end - submitted) * 1000, ok=scope_ok and bool(fused))
            submitted = [0.0]
            barrier = threading.Barrier(workers + 1, action=lambda: submitted.__setitem__(0, time.perf_counter()))
            pending = Queue()
            for i in range(args.requests):
                pending.put(i)
            def consume():
                with open_lock:
                    connection = CandidateIndex(path, identity, backend=args.backend)
                try:
                    connection.db.execute("PRAGMA query_only=ON")
                    connection.dense(seed["queries"][0].tolist(), query_scope(args.size, kind), 20)
                    barrier.wait(timeout=60)
                    rows = []
                    while True:
                        try:
                            i = pending.get_nowait()
                        except Empty:
                            return rows
                        rows.append(run(connection, i, submitted[0]))
                finally:
                    connection.close()
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(consume) for _ in range(workers)]
                # 所有连接就绪后统一确定突发请求的到达时间。
                barrier.wait(timeout=60)
                raw = sorted([row for future in futures for row in future.result(timeout=90)], key=lambda r:r["request"])
                started = submitted[0]
                wall = time.perf_counter() - started
            concurrency.append(dict(workers=workers, scope=kind, requests=args.requests,
                                    throughput_per_second=len(raw) / wall,
                                    summary={key:metrics([r[key] for r in raw]) for key in
                                             ("dense_ms", "lexical_ms", "fusion_ms", "service_ms", "queue_ms", "completion_ms")},
                                    raw=raw))
    mem = psutil.Process().memory_info()
    save(result_path, dict(size=args.size, backend=args.backend, build_ms=build_ms,
                          database_bytes=path.stat().st_size, first_queries=first,
                          rss_bytes=mem.rss, peak_working_set_bytes=getattr(mem, "peak_wset", None),
                          correctness=correctness, lifecycle=mutation, update_ms=update_ms,
                          delete_ms=delete_ms, concurrency=concurrency,
                          network_attempts=len(NETWORK_ATTEMPTS)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--sizes", type=int, nargs="+", default=[1000, 10000, 50000])
    parser.add_argument("--requests", type=int, default=24)
    parser.add_argument("--size", type=int)
    parser.add_argument("--backend", choices=["scalar", "vec0"])
    parser.add_argument("--reopen", action="store_true")
    args = parser.parse_args()
    if args.requests < 4 or any(size < 200 for size in args.sizes) or len(set(args.sizes)) != len(args.sizes):
        parser.error("请求至少4次、规模至少200块")
    offline()
    if args.backend:
        worker(args)
        return
    stamp = datetime.now(ZoneInfo("Australia/Sydney")).strftime("%Y%m%d-%H%M%S-%f")
    output = args.output or ROOT / ".tmp" / ("w3-index-scale-" + stamp)
    output.mkdir(parents=True, exist_ok=False)
    from briefly.llm import create_local_embeddings
    corpus_path = ROOT / "tests/eval/fixtures/w3_rag/documents.json"
    corpus = json.loads(corpus_path.read_text(encoding="utf-8"))["documents"]
    questions = ["上海出差住宿最多报销多少？", "接口联调延期到哪一天？", "E403 如何处理？",
                 "CSV 导出需要什么条件？", "结果未知的超时可以直接重试吗？", "实习生住宿额度是多少？"]
    started = time.perf_counter()
    embedder = create_local_embeddings(str(ROOT / ".tmp/w3-models/bge-small-zh-v1.5"), threads=4)
    load_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    seeds = np.asarray(embedder.embed_documents([d["text"] for d in corpus]), dtype=np.float32)
    queries = np.asarray([embedder.embed_query(q) for q in questions], dtype=np.float32)
    encode_ms = (time.perf_counter() - started) * 1000
    np.savez(output / "seeds.npz", embeddings=seeds, queries=queries)
    save(output / "seeds.json", dict(identity=embedder.identity, texts=[d["text"] for d in corpus],
                                    questions=questions, model_load_ms=load_ms, encode_seed_ms=encode_ms))
    # 不复用完全相同向量凑数；固定种子加扰动。原文重复、向量非实际原文编码，绝不计入语义评测。
    for size in args.sizes:
        rng = np.random.default_rng(20261028)
        derived = seeds[np.arange(size) % len(seeds)] + rng.normal(0, .025, (size, seeds.shape[1])).astype(np.float32)
        derived /= np.linalg.norm(derived, axis=1, keepdims=True)
        np.save(output / f"vectors-{size}.npy", derived)
    del embedder, derived
    env = {**os.environ, "OMP_NUM_THREADS":"1", "OPENBLAS_NUM_THREADS":"1", "MKL_NUM_THREADS":"1"}
    runs = []
    for size in args.sizes:
        for backend in ("scalar", "vec0"):
            command = [sys.executable, str(Path(__file__).resolve()), "--output", str(output),
                       "--size", str(size), "--backend", backend, "--requests", str(args.requests)]
            for mode in ("build", "reopen"):
                log = output / f"{backend}-{size}-{mode}.log"
                with log.open("w", encoding="utf-8") as stream:
                    try:
                        completed = subprocess.run(command + (["--reopen"] if mode == "reopen" else []),
                                                   stdout=stream, stderr=subprocess.STDOUT, env=env, timeout=600)
                        status = dict(size=size, backend=backend, mode=mode, exit_code=completed.returncode)
                    except subprocess.TimeoutExpired:
                        status = dict(size=size, backend=backend, mode=mode, timeout_seconds=600)
                runs.append(status)
                save(output / "runs.json", runs)
                print(json.dumps(status), flush=True)
                if status.get("exit_code") != 0:
                    break
    sources = [Path(__file__).resolve(), ROOT / "tests/eval/w3_retrieval_support.py",
               ROOT / "src/briefly/local_embeddings.py", corpus_path]
    source_hashes = {}
    for path in sources:
        relative = path.relative_to(ROOT)
        target = output / "source-snapshot" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
        source_hashes[str(relative)] = digest(path)
    save(output / "manifest.json", dict(created_at_sydney=stamp, sizes=args.sizes, requests=args.requests,
         source_sha256=source_hashes, vectors_sha256={f"vectors-{s}.npy":digest(output / f"vectors-{s}.npy") for s in args.sizes},
         seed_sha256=digest(output / "seeds.npz"), environment=dict(python=sys.version, platform=platform.platform(),
         cpu=platform.processor(), logical_cpus=os.cpu_count(), memory_bytes=psutil.virtual_memory().total,
         sqlite=sqlite3.sqlite_version, dependencies={name:version(name) for name in ("sqlite-vec", "numpy", "fastembed", "psutil")}),
         workload="闭环突发24请求（可配置）；1/4线程；索引两路查询+RRF，不含在线问题Embedding和生成。无并发写入。",
         data="11原创短文循环作为存储负载；固定种子20261028，原向量加N(0,.025)后归一化。衍生向量不对应存储原文，只用于性能和过滤正确性。",
         scope="每100块一文档；单库为每10文档选1份；少量为前2文档。假设档位，不是用户容量承诺。",
         limitations="未限制进程CPU/内存；1线程BLAS只约束oracle，CPU和磁盘共享宿主。缓存未清空，重开不是冷启动。峰值含向量mmap和oracle，不是纯索引RSS。无新语义质量/服务器容量/答案正确率结论。",
         network_attempts=len(NETWORK_ATTEMPTS), external_inference_calls=0))
    summary = summarize(output, args.sizes, args.requests)
    print(str(output), flush=True)
    if not summary["ok"]:
        raise SystemExit("规模实验存在失败或缺失；见summary.json和逐进程日志")


if __name__ == "__main__":
    main()
