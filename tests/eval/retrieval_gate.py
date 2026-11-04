"""检索门禁第二层：用真实 BGE 在虚构资料上记检索基线，并比较两次结果。手动运行，pytest 不收集。

只跑本地检索，不创建生成模型；运行期间禁止非本机网络（尝试即报错并记入结果）。
资料和题目复用端到端评测的 tests/eval/fixtures/w3_e2e/（8 份虚构资料、60 题），没有真实资料。
build 要把 D6～D8 转成 PDF，需要本机 LibreOffice（BRIEFLY_LIBREOFFICE）。

用法（仓库根目录，工作树里要设 PYTHONPATH=src）：
    python tests/eval/retrieval_gate.py build .tmp/gate-kb --model-dir <BGE目录>
    python tests/eval/retrieval_gate.py run .tmp/gate-kb --model-dir <BGE目录> --output <结果.json>
    python tests/eval/retrieval_gate.py compare <基线.json> <结果.json> [--output 报告.json]

compare 的退出码：0 没有变差；1 有题目变差；2 两次结果不可比（模型、资料切片、题集或检索参数不同）。
每题记两种口径：Top5（`search` 默认参数）和依据包（与知识问答相同的参数，含文档开头和预算装填）。
holdout 题的明细也在结果里：这份结果只用来对比退步，不要拿来调参。
"""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))  # 让 tests.eval 可导入

from briefly import config  # noqa: E402
from tests.eval.w3_e2e_scoring import contains, retrieval_hit  # noqa: E402

TOP_K = 5
LOW_OVERLAP = 0.6
PREVIEW_CHARS = 80
EXIT_REGRESSION, EXIT_INCOMPARABLE = 1, 2
LOOPBACK = {"localhost", "127.0.0.1", "::1", "", None}
network_attempts: list[str] = []


def _audit_network(event, args):
    """本进程内的非本机连接一律拒绝并记录，用来证明这次运行没有外发。子进程（LibreOffice）不在此列。"""
    if event == "socket.connect":
        address = args[1]
        host = address[0] if isinstance(address, tuple) else None
    elif event == "socket.getaddrinfo":
        host = args[0]
    else:
        return
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if host not in LOOPBACK:
        network_attempts.append(f"{event}:{host}")
        raise RuntimeError(f"检索基线不允许联网：{host}")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def git(*args) -> str | None:
    try:
        done = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, encoding="utf-8", check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return done.stdout.strip()


def code_identity() -> dict:
    """提交号、src 的树哈希（换分支也能判断检索代码是否相同）和未提交改动。"""
    import briefly
    loaded = Path(briefly.__file__).resolve()
    if ROOT not in loaded.parents:
        # .venv 的可编辑安装指向主检出：不设 PYTHONPATH=src 时测的不是本检出的代码
        raise SystemExit(f"导入的 briefly 不在本检出（{loaded}），请设置 PYTHONPATH=src")
    dirty = git("status", "--porcelain", "--", "src")
    return dict(commit=git("rev-parse", "HEAD"), src_tree=git("rev-parse", "HEAD:src"), src_dirty=bool(dirty),
                main_commit=git("rev-parse", "main"), main_src_tree=git("rev-parse", "main:src"))


def versions() -> dict:
    packages = {}
    for name in ("sqlite-vec", "fastembed", "onnxruntime", "numpy"):
        packages[name] = importlib.metadata.version(name)
    return dict(python=sys.version.split()[0], sqlite=sqlite3.sqlite_version, **packages)


def encoder_for(model_dir: str):
    from briefly.llm import create_local_embeddings
    return create_local_embeddings(model_dir)


def build(root: Path, model_dir: str) -> None:
    from tests.eval.w3_e2e_qa import build as build_library
    config.EMBEDDING_DIR = model_dir
    build_library(root)


def source_keys(imports, kb_id) -> dict[str, str]:
    return {item.id: item.source_key for item in imports.knowledge.documents(kb_id)}


def corpus_fingerprint(imports, kb_id) -> dict:
    """每份资料已发布片段的内容指纹。比较时用它判断两次用的是不是同一批切片。"""
    from briefly.knowledge.models import KnowledgeVersion
    svc, documents, keys = imports.knowledge, {}, source_keys(imports, kb_id)
    for item in svc.scope([kb_id])["versions"]:
        with svc._session() as s:
            numbers = [row["index"] for row in s.get(KnowledgeVersion, item["version_id"]).chunk_manifest]
        chunks = [svc.chunk(kb_id, item["document_id"], item["version_id"], number)["content"] for number in numbers]
        documents[keys[item["document_id"]]] = dict(chunks=len(chunks), sha256=sha("\x00".join(chunks)))
    return dict(documents=dict(sorted(documents.items())), sha256=sha(json.dumps(dict(sorted(documents.items())), sort_keys=True)))


def ranked_entry(rank, item, keys, gold) -> dict:
    doc = keys[item["document_id"]]
    routes = [dict(route=r["route"], rank=r.get("rank"), raw_score=r.get("raw_score")) for r in item["retrieval"]["routes"]]
    return dict(rank=rank, doc=doc, chunk=item["chunk"], start=item["start"], end=item["end"], section=item["section"],
                content_sha256=sha(item["content"]), preview=item["content"][:PREVIEW_CHARS],
                rrf_score=item["retrieval"].get("rrf_score"), routes=routes,
                gold_hit=any(g["doc"] == doc and contains(item["content"], g["text"]) for g in gold))


def evidence_for_scoring(result) -> list[dict]:
    return [dict(title=e["source"]["title"], content=e["content"]) for e in result["evidence"]]


def run_case(retrieval, kb_id, case, keys, titles) -> dict:
    top = retrieval.search(kb_id, case["question"])
    bundle = retrieval.search(kb_id, case["question"], include_document_context=True, context_budget_only=True,
                              context_tokens=config.KNOWLEDGE_CONTEXT_TOKENS)
    entries = [ranked_entry(rank, item, keys, case["gold"]) for rank, item in enumerate(top["evidence"][:TOP_K], 1)]
    return dict(id=case["id"], category=case["category"], split=case["split"], question=case["question"],
                abstain=case["abstain"], gold=case["gold"], status=top["status"], top5=entries,
                hit_top5=retrieval_hit(case["gold"], evidence_for_scoring(top), titles),
                hit_bundle=retrieval_hit(case["gold"], evidence_for_scoring(bundle), titles),
                first_gold_rank=next((e["rank"] for e in entries if e["gold_hit"]), None),
                bundle_chunks=len(bundle["evidence"]))


def rate(values: list[bool]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def summarize(cases: list[dict]) -> dict:
    gold = [c for c in cases if c["hit_top5"] is not None]
    return dict(cases=len(cases), with_gold=len(gold), abstain=sum(c["abstain"] for c in cases),
                hit_top5=sum(c["hit_top5"] for c in gold), hit_bundle=sum(c["hit_bundle"] for c in gold),
                hit_top5_rate=rate([c["hit_top5"] for c in gold]), hit_bundle_rate=rate([c["hit_bundle"] for c in gold]))


def run(root: Path, model_dir: str, output: Path, allow_dirty: bool) -> None:
    from briefly.knowledge.retrieval import PARAMETERS, RetrievalService
    from briefly.relevance_floor import embedding_fingerprint, kb_floor
    from tests.eval.w3_e2e_qa import KB_NAME, library, load_cases, open_imports

    code = code_identity()
    if code["src_dirty"] and not allow_dirty:
        raise SystemExit("src 有未提交改动，结果对不上提交号；确需运行时加 --allow-dirty")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    encoder = encoder_for(model_dir)
    engine, imports = open_imports(root, encoder)
    try:
        kb_id = manifest["kb_id"]
        if [b.name for b in imports.knowledge.list_bases()] != [KB_NAME]:
            raise SystemExit("不是 build 建的虚构资料库，拒绝运行")
        keys = source_keys(imports, kb_id)
        titles, _ = library(imports, kb_id)
        retrieval = RetrievalService(imports)
        cases = load_cases("all", [])
        results = [run_case(retrieval, kb_id, case, keys, titles) for case in cases]
        corpus = corpus_fingerprint(imports, kb_id)
    finally:
        engine.dispose()
    identity = dict(encoder.identity)
    result = dict(
        meta=dict(created=datetime.now(timezone.utc).isoformat(timespec="seconds"), code=code, versions=versions(),
                  model=dict(fingerprint=embedding_fingerprint(identity), name=identity.get("model"),
                             revision=identity.get("revision"), dimension=identity.get("dimension")),
                  relevance_floor=kb_floor(identity), parameters=PARAMETERS,
                  context_tokens=config.KNOWLEDGE_CONTEXT_TOKENS, corpus=corpus,
                  cases_sha256=sha(json.dumps(cases, ensure_ascii=False, sort_keys=True)),
                  network_attempts=list(network_attempts)),
        summary=summarize(results), cases=results)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(result["summary"], ensure_ascii=False))
    print(f"结果：{output}")


def incomparable(base: dict, new: dict) -> list[str]:
    """这些不同就不是“同一道题换了存储”，比出来的差异没有意义。"""
    checks = [("模型指纹", ("model", "fingerprint")), ("资料切片", ("corpus", "sha256")), ("题集", ("cases_sha256",)),
              ("检索参数", ("parameters",)), ("依据包预算", ("context_tokens",))]
    out = []
    for label, path in checks:
        a, b = base["meta"], new["meta"]
        for key in path:
            a, b = a.get(key) if isinstance(a, dict) else None, b.get(key) if isinstance(b, dict) else None
        if a != b:
            out.append(f"{label}不同：{a} → {b}")
    return out


def overlap(base_case: dict, new_case: dict) -> float:
    """两次 Top5 的重合度：按片段正文比较（代次号每次建库都会变）。两次都为空算完全重合。"""
    a = {e["content_sha256"] for e in base_case["top5"]}
    b = {e["content_sha256"] for e in new_case["top5"]}
    return 1.0 if not a and not b else len(a & b) / max(len(a), len(b))


def worsened(base_case: dict, new_case: dict) -> list[str]:
    reasons = []
    if base_case["hit_top5"] and not new_case["hit_top5"]:
        reasons.append("Top5 依据丢失")
    if base_case["hit_bundle"] and not new_case["hit_bundle"]:
        reasons.append("依据包依据丢失")
    before, after = base_case["first_gold_rank"], new_case["first_gold_rank"]
    if before is not None and after is not None and after > before:
        reasons.append(f"依据名次下降 {before} → {after}")
    elif before is not None and after is None and not reasons:
        reasons.append(f"依据跌出 Top5（原名次 {before}）")
    return reasons


def compare(base: dict, new: dict, *, low_overlap: float = LOW_OVERLAP) -> dict:
    base_cases, new_cases = {c["id"]: c for c in base["cases"]}, {c["id"]: c for c in new["cases"]}
    problems = incomparable(base, new)
    if set(base_cases) != set(new_cases):
        problems.append(f"题目不同：少了 {sorted(set(base_cases) - set(new_cases))}，多了 {sorted(set(new_cases) - set(base_cases))}")
    shared = [key for key in base_cases if key in new_cases]
    overlaps = {key: round(overlap(base_cases[key], new_cases[key]), 4) for key in shared}
    bad = [dict(id=key, question=base_cases[key]["question"], reasons=reasons, overlap=overlaps[key])
           for key in shared if (reasons := worsened(base_cases[key], new_cases[key]))]
    improved = [key for key in shared if (not base_cases[key]["hit_top5"] and new_cases[key]["hit_top5"])
                or (not base_cases[key]["hit_bundle"] and new_cases[key]["hit_bundle"])]
    a, b = base["summary"], new["summary"]
    return dict(
        incomparable=problems,
        hit_top5_rate=dict(base=a["hit_top5_rate"], new=b["hit_top5_rate"]),
        hit_bundle_rate=dict(base=a["hit_bundle_rate"], new=b["hit_bundle_rate"]),
        mean_top5_overlap=round(sum(overlaps.values()) / len(overlaps), 4) if overlaps else None,
        identical_top5=sum(value == 1 for value in overlaps.values()), cases=len(shared),
        low_overlap=[dict(id=key, overlap=value) for key, value in overlaps.items() if value < low_overlap],
        worsened=bad, improved=improved, overlap_by_case=overlaps,
        code=dict(base=base["meta"]["code"], new=new["meta"]["code"]),
        versions=dict(base=base["meta"]["versions"], new=new["meta"]["versions"]),
        network_attempts=new["meta"].get("network_attempts", []))


def exit_code(report: dict) -> int:
    if report["incomparable"]:
        return EXIT_INCOMPARABLE
    return EXIT_REGRESSION if report["worsened"] else 0


def render(report: dict) -> str:
    lines = []
    if report["incomparable"]:
        lines += ["两次结果不可比，下面的差异不能作为验收依据：", *[f"  - {p}" for p in report["incomparable"]]]
    for label, key in (("Top5 命中率", "hit_top5_rate"), ("依据包命中率", "hit_bundle_rate")):
        lines.append(f"{label}：{report[key]['base']} → {report[key]['new']}")
    lines.append(f"Top5 平均重合度：{report['mean_top5_overlap']}（完全相同 {report['identical_top5']}/{report['cases']} 题）")
    lines.append(f"重合度低于 {LOW_OVERLAP} 的题：{[item['id'] for item in report['low_overlap']] or '无'}")
    lines.append(f"变好的题：{report['improved'] or '无'}")
    lines.append(f"变差的题：{len(report['worsened'])}")
    lines += [f"  - {item['id']}（重合度 {item['overlap']}）{'；'.join(item['reasons'])}：{item['question']}" for item in report["worsened"]]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("build", help="解析虚构资料并本地建库")
    make.add_argument("root", type=Path)
    make.add_argument("--model-dir", required=True)
    go = commands.add_parser("run", help="逐题检索，写结果文件")
    go.add_argument("root", type=Path)
    go.add_argument("--model-dir", required=True)
    go.add_argument("--output", type=Path, required=True)
    go.add_argument("--allow-dirty", action="store_true", help="src 有未提交改动也运行（不能作为基线）")
    diff = commands.add_parser("compare", help="比较基线和新结果")
    diff.add_argument("base", type=Path)
    diff.add_argument("new", type=Path)
    diff.add_argument("--output", type=Path, help="完整报告写入此文件")
    args = parser.parse_args(argv)

    if args.command == "compare":
        report = compare(*(json.loads(path.read_text(encoding="utf-8")) for path in (args.base, args.new)))
        print(render(report))
        if args.output:
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
        return exit_code(report)
    sys.addaudithook(_audit_network)
    if args.command == "build":
        build(args.root.resolve(), args.model_dir)
    else:
        run(args.root.resolve(), args.model_dir, args.output.resolve(), args.allow_dirty)
    return 0


if __name__ == "__main__":
    sys.exit(main())
