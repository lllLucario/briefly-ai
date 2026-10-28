"""W3-01 离线基准；不导入 Agent、不创建模型、不访问业务库。

运行：.venv/Scripts/python.exe tests/eval/w3_rag_baseline.py --split all -n 3
逐次结果默认写 .tmp/eval-runs/w3-baseline-*，有效结论再归并至 files/eval/w3；公共虚构语料与标签分别保存在 fixtures/w3_rag/。
这是检索层基准，不是问答准确率评测。独立于真实生成 run_eval.py，避免默认外发。
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
import hashlib
from importlib.metadata import version
import json
import math
from pathlib import Path
import platform
import statistics
import subprocess
from tempfile import TemporaryDirectory
import time
from zoneinfo import ZoneInfo

from sqlalchemy import select

from briefly import config
from briefly.db import create_db_engine
from briefly.materials import MAX_SEARCH_HITS, MaterialChunk, MaterialStore

REPO = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures" / "w3_rag"


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_fixtures():
    corpus = json.loads((FIXTURES / "documents.json").read_text(encoding="utf-8"))
    spec = json.loads((FIXTURES / "expectations.json").read_text(encoding="utf-8"))
    docs = {d["id"]: d for d in corpus["documents"]}
    assert len(docs) == len(corpus["documents"]), "重复文档标识"
    assert len({c["id"] for c in spec["cases"]}) == len(spec["cases"]), "重复题号"
    for key, item in spec["evidence"].items():
        assert docs[item["document"]]["text"].count(item["quote"]) == 1, f"依据不唯一或缺失：{key}"
    for case in spec["cases"]:
        for group in case["evidence_groups"]:
            assert group, f"空依据组：{case['id']}"
            for ref in group:
                assert spec["evidence"][ref]["document"] in case["scope"], f"标签越范围：{case['id']}"
        assert all(s in docs or s == "missing-document" for s in case["scope"])
    return corpus, spec


def coverage(case, evidence, hits, chunks, id_to_doc, snippets=False):
    """每组代表一项必要依据，组内多个标识为可替代依据；依据需完整出现。"""
    found = []
    for group in case["evidence_groups"]:
        supported = any(
            id_to_doc[h.material_id] == evidence[ref]["document"]
            and evidence[ref]["quote"] in (h.snippet if snippets else chunks[(h.material_id, h.chunk)]["content"])
            for ref in group for h in hits
        )
        found.append(supported)
    return {"groups_found": found, "recall": sum(found) / len(found) if found else None,
            "all_required": all(found) if found else None}


def run_query(case, mode, store, ids, chunks, evidence):
    query = case["question"] if mode == "raw_question" else case["keywords"]
    # 不存在范围用已知不存在的 ID；空列表原样传递，不用 None 回落全库。
    scope = [ids.get(s, 2_000_000_000) for s in case["scope"]]
    started = time.perf_counter()
    hits, error = [], None
    try:
        hits = store.search(query, scope)
    except ValueError as exc:
        error = str(exc)
    elapsed = (time.perf_counter() - started) * 1000
    reverse = {value: key for key, value in ids.items()}
    return dict(case_id=case["id"], scenario=case["scenario"], kind=case["kind"], split=case["split"],
                mode=mode, query=query, scope=case["scope"], latency_ms=elapsed, error=error,
                error_as_expected=error == case["expected_search_error"],
                out_of_scope=[reverse[h.material_id] for h in hits if reverse[h.material_id] not in case["scope"]],
                hits=[dict(**asdict(h), document=reverse[h.material_id]) for h in hits],
                chunk_coverage=coverage(case, evidence, hits, chunks, reverse),
                snippet_coverage=coverage(case, evidence, hits, chunks, reverse, snippets=True),
                answer_evaluation="未运行生成，无法据检索判定拒答、事实归属或引用支持")


def summarize(records):
    output = []
    for mode in ("raw_question", "manual_keywords"):
        for split in sorted({r["split"] for r in records}):
            for scenario in ("office", "project", "manual", "global"):
                rows = [r for r in records if (r["mode"], r["scenario"], r["split"]) == (mode, scenario, split)]
                if not rows:
                    continue
                scored = [r for r in rows if r["chunk_coverage"]["recall"] is not None]
                no_evidence = [r for r in rows if r["chunk_coverage"]["recall"] is None and r["kind"] != "invalid_query"]
                latencies = sorted(r["latency_ms"] for r in rows)
                output.append(dict(mode=mode, split=split, scenario=scenario, runs=len(rows),
                    scored_runs=len(scored), macro_evidence_recall_at_10=statistics.mean(r["chunk_coverage"]["recall"] for r in scored) if scored else None,
                    all_required_runs=sum(r["chunk_coverage"]["all_required"] for r in scored),
                    snippet_macro_evidence_recall=statistics.mean(r["snippet_coverage"]["recall"] for r in scored) if scored else None,
                    no_evidence_runs=len(no_evidence), no_evidence_with_hits=sum(bool(r["hits"]) for r in no_evidence),
                    scope_leaks=sum(bool(r["out_of_scope"]) for r in rows),
                    unexpected_errors=sum(not r["error_as_expected"] for r in rows),
                    p50_ms=statistics.median(latencies), p95_nearest_rank_ms=latencies[math.ceil(.95 * len(latencies)) - 1]))
    return output


def lifecycle(store):
    """材料存储层的真实行为；不宣称为尚未实现的知识库版本/索引生命周期。"""
    results = []
    for filename, payload in (("empty.txt", b""), ("broken.docx", b"not-a-zip"), ("legacy.doc", b"legacy")):
        try:
            store.add_file(filename, payload)
        except ValueError as exc:
            results.append(dict(id=filename, ok=True, error=str(exc)))
        else:
            results.append(dict(id=filename, ok=False, error=None))
    first, created = store.add_text("生命周期旧版", "桐叶唯一标记：审批额度 111 元。")
    repeated, second_created = store.add_text("生命周期重复", "桐叶唯一标记：审批额度 111 元。")
    results.append(dict(id="duplicate", ok=created and not second_created and first.id == repeated.id))
    newer, _ = store.add_text("生命周期新版", "桐叶唯一标记：审批额度 222 元。")
    both = store.search("桐叶唯一标记", [first.id, newer.id])
    results.append(dict(id="ordinary_material_versions_coexist", ok={h.material_id for h in both} == {first.id, newer.id},
                        limitation="普通材料新增不会自动让旧版失效；知识库生命周期尚未实现"))
    store.delete(first.id)
    results.append(dict(id="delete_old", ok=not store.search("桐叶唯一标记", [first.id])))
    store.delete(newer.id)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-n", "--repeats", type=int, default=3)
    parser.add_argument("--split", choices=("dev", "holdout", "all"), default="dev")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("重复次数须大于0")
    corpus, spec = load_fixtures()
    # 固定实验切片参数，避免开发机 .env 差异；不修改环境文件或正式服务。
    config.CHUNK_SIZE, config.CHUNK_OVERLAP = 1500, 150
    stamp = datetime.now(ZoneInfo("Australia/Sydney")).strftime("%Y%m%d-%H%M%S-%f")
    output = REPO / ".tmp" / "eval-runs" / ("w3-baseline-" + stamp)
    output.mkdir(parents=True, exist_ok=False)
    records, ids, chunks = [], {}, {}
    with TemporaryDirectory(prefix="briefly-w3-") as temporary:
        url = "sqlite:///" + (Path(temporary) / "baseline.db").as_posix()
        engine = create_db_engine(url)
        try:
            store = MaterialStore(engine)
            for doc in corpus["documents"]:
                material, _ = store.add_text(doc["title"], doc["text"], markdown=True)
                ids[doc["id"]] = material.id
            with store._session() as session:
                for chunk in session.scalars(select(MaterialChunk).order_by(MaterialChunk.material_id, MaterialChunk.index)):
                    chunks[(chunk.material_id, chunk.index)] = dict(material_id=chunk.material_id,
                        chunk=chunk.index, section=chunk.section, start=chunk.start, end=chunk.end, content=chunk.content)
            for case in spec["cases"]:
                # 标签确实映射到当前切片；跨片段题不得被同一块满足全部依据。
                for group in case["evidence_groups"]:
                    assert any(any(c["material_id"] == ids[spec["evidence"][ref]["document"]] and spec["evidence"][ref]["quote"] in c["content"] for c in chunks.values()) for ref in group), case["id"]
                if case["kind"] == "cross":
                    assert len({spec["evidence"][g[0]]["document"] for g in case["evidence_groups"]}) >= 2
            selected = [c for c in spec["cases"] if args.split == "all" or c["split"] == args.split]
            for repeat in range(args.repeats):
                for case in selected:
                    for mode in ("raw_question", "manual_keywords"):
                        records.append(dict(repeat=repeat + 1, **run_query(case, mode, store, ids, chunks, spec["evidence"])))
            checks = lifecycle(store)
        finally:
            engine.dispose()
        # 同一持久化数据库重新开引擎，核对已导入材料与原始检索结果可重读。
        engine = create_db_engine(url)
        try:
            restarted = MaterialStore(engine)
            expected = next(r for r in records if r["case_id"] == selected[0]["id"] and r["mode"] == "manual_keywords")
            actual = run_query(selected[0], "manual_keywords", restarted, ids, chunks, spec["evidence"])
            checks.append(dict(id="restart", ok=actual["hits"] == expected["hits"]))
        finally:
            engine.dispose()
    save(output / "results.json", records)
    save(output / "chunks.json", list(chunks.values()))
    save(output / "lifecycle.json", checks)
    summary = summarize(records)
    save(output / "summary.json", summary)
    inputs = [FIXTURES / "documents.json", FIXTURES / "expectations.json", Path(__file__),
              REPO / "src/briefly/materials.py", REPO / "src/briefly/chunking.py"]
    save(output / "manifest.json", dict(timestamp_sydney=stamp, corpus_version=corpus["version"],
         question_version=spec["version"], command_args=vars(args), git_head=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip(),
         working_tree=subprocess.check_output(["git", "status", "--short"], cwd=REPO, text=True),
         input_sha256={p.relative_to(REPO).as_posix(): digest(p) for p in inputs},
         python=platform.python_version(), platform=platform.platform(),
         packages={p: version(p) for p in ("SQLAlchemy", "langchain-text-splitters", "langchain-core")},
         documents=len(ids), chunks=len(chunks), material_ids=ids, cases=len(selected), repeats=args.repeats,
         chunk_size=1500, chunk_overlap=150, top_k=MAX_SEARCH_HITS, external_calls=0,
         generation="未执行；Agent无专属RAG工作流。仅测检索函数，未测模型意图识别、关键词选择、拒答及引用支持。",
         timing="串行小样本；同一进程SQLite温热查询，含首次查询，不代表冷启动或端到端延迟。"))
    print(output.relative_to(REPO).as_posix())
    print(json.dumps(dict(cases=len(selected), runs=len(records), lifecycle_checks=len(checks),
                          lifecycle_failed=sum(not c["ok"] for c in checks),
                          scope_leaks=sum(bool(r["out_of_scope"]) for r in records)), ensure_ascii=False))
    if any(not c["ok"] for c in checks) or any(r["out_of_scope"] or not r["error_as_expected"] for r in records):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
