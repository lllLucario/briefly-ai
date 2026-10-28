"""在已准备的隔离索引上核对FTS候选排序和耗时；不加载模型，不读取新真实资料。"""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import socket
import statistics
import time
from zoneinfo import ZoneInfo

from w3_scoped_lexical import bounded_lexical
from w3_retrieval_support import CandidateIndex, query_terms

ROOT = Path(__file__).resolve().parents[2]


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, help="已存在的隔离索引；省略则从已保留压力输入重建")
    parser.add_argument("--size", type=int, choices=(1000,10000,50000), default=50000)
    parser.add_argument("--seed-dir", type=Path, default=ROOT / "files/eval/w3/retrieval/scale")
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 1 or (args.database is not None and not args.database.is_file()):
        parser.error("需要已存在的隔离数据库和正重复次数")
    attempts = []
    def deny(*args, **kwargs):
        attempts.append("blocked")
        raise RuntimeError("词法实验禁止网络连接")
    socket.socket.connect = deny
    socket.socket.connect_ex = deny
    seeds = json.loads((args.seed_dir / "seeds.json").read_text(encoding="utf-8"))
    output = ROOT / ".tmp" / ("w3-scoped-lexical-" + datetime.now(ZoneInfo("Australia/Sydney")).strftime("%Y%m%d-%H%M%S-%f"))
    output.mkdir(parents=True, exist_ok=False)
    build_ms=None
    if args.database is None:
        import numpy as np
        vector_path=args.seed_dir/f"vectors-{args.size}.npy"
        manifest=json.loads((args.seed_dir/"manifest.json").read_text(encoding="utf-8"))
        h=hashlib.sha256()
        with vector_path.open("rb") as stream:
            for block in iter(lambda:stream.read(1048576),b""):
                h.update(block)
        if h.hexdigest()!=manifest["vectors_sha256"][vector_path.name]:
            raise ValueError("压力输入向量指纹变更")
        vectors=np.load(vector_path,mmap_mode="r")
        args.database=output/"scope.db"
        build=CandidateIndex(args.database,seeds["identity"],backend="scalar")
        started=time.perf_counter()
        try:
            for d in range(args.size//100):
                build.replace_document(f"doc-{d:05d}",[(i+1,seeds["texts"][i%len(seeds["texts"])],vectors[i].tolist()) for i in range(d*100,(d+1)*100)])
        finally:
            build.close()
        build_ms=(time.perf_counter()-started)*1000
    index = CandidateIndex(args.database, seeds["identity"], backend="scalar")
    try:
        docs = [r[0] for r in index.db.execute("SELECT DISTINCT doc FROM chunks ORDER BY doc")]
        scopes = dict(all=docs, kb=docs[::10], few=docs[:2], sparse=[docs[9],docs[-10]])
        queries = seeds["questions"] + ["E403 20MB C++ \"引号\"", "完全没有匹配的ZXQ991术语"]
        references = {}
        # 独立参考：完整匹配结果在Python按允许ID筛选后排序；绝不先全库TopK再过滤。
        for question in queries:
            query = " OR ".join('"'+term+'"' for term in query_terms(question))
            matches = index.db.execute("SELECT rowid,bm25(lexical) FROM lexical WHERE lexical MATCH ?", (query,)).fetchall()
            for name,scope in scopes.items():
                marks = ",".join("?" for _ in scope)
                allowed = {r[0] for r in index.db.execute(f"SELECT id FROM chunks WHERE doc IN ({marks})",scope)}
                references[question,name] = sorted([r for r in matches if r[0] in allowed], key=lambda r:(r[1],r[0]))[:20]
        raw = []
        for scope in scopes.values():
            for question in queries:
                index.lexical(question,scope,20)
                bounded_lexical(index,question,scope,20)
        for repeat in range(args.repeats):
            for name,scope in scopes.items():
                for question in queries:
                    methods = ("baseline","bounded") if repeat % 2 == 0 else ("bounded","baseline")
                    for method in methods:
                        started = time.perf_counter()
                        if method == "baseline":
                            hits, plan = index.lexical(question,scope,20), None
                        else:
                            hits, plan = bounded_lexical(index,question,scope,20)
                        ms = (time.perf_counter()-started)*1000
                        raw.append(dict(repeat=repeat+1, scope=name, question=question, method=method,
                                        ms=ms, hits=hits, plan=plan, reference=references[question,name],
                                        exact_match=hits == references[question,name]))
        summary = []
        for name in scopes:
            for method in ("baseline","bounded"):
                rows = [r for r in raw if r["scope"] == name and r["method"] == method]
                times = sorted(r["ms"] for r in rows)
                # 最近秩P95，保留定义，避免与旧numpy线性百分位混用。
                import math
                summary.append(dict(scope=name, method=method, count=len(rows), p50_ms=statistics.median(times),
                                    p95_nearest_rank_ms=times[math.ceil(.95*len(times))-1], max_ms=max(times),
                                    all_exact=all(r["exact_match"] for r in rows)))
        save(output / "raw.json",raw)
        save(output / "summary.json",summary)
        snapshot = output / "source-snapshot"
        snapshot.mkdir()
        paths = [Path(__file__).resolve(), ROOT / "tests/eval/w3_scoped_lexical.py", ROOT / "tests/eval/w3_retrieval_support.py"]
        hashes = {}
        for path in paths:
            target = snapshot / path.name
            target.write_bytes(path.read_bytes())
            hashes[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
        save(output / "manifest.json",dict(source_sha256=hashes, rows=index.db.execute("SELECT count(*) FROM chunks").fetchone()[0],
             repeats=args.repeats, build_ms=build_ms, max_documents=8, max_span_ratio=.25, network_attempts=len(attempts), external_inference_calls=0,
             warmup="计时前两路径逐题逐范围各1次，另有独立参考读取；不是冷缓存",
             scope_documents={name:len(scope) for name,scope in scopes.items()},
             limitations="只比较索引词法路径；暖缓存、单线程，顺序交替；压力循环文本，不证明真实语义质量或服务器SLA。路由阈值仅实验参数。"))
        print(json.dumps(summary, ensure_ascii=False), flush=True)
        print(output, flush=True)
        if attempts or not all(r["exact_match"] for r in raw):
            raise SystemExit("排序/范围或离线检查失败")
    finally:
        index.close()


if __name__ == "__main__":
    main()
