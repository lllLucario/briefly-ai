"""素材库抽样问答评测：从真实通用办公素材库抽样 Word/PDF → 本地入库 → 外部出题 → 助理完整链路回答 → 规则判分。

与 w3_e2e_qa.py（虚构资料）共用回答与判分流程；差别是资料为真实素材，规模更大、排版更杂，
用来看真实资料下的入库成功率、检索与回答质量。题目由另一个 agent 依据导出的正文独立撰写。

用法（仓库根目录）：
    python tests/eval/w3_library_qa.py sample                         # 分层抽样，写 files/eval/w3/library/sample.json
    python tests/eval/w3_library_qa.py build .tmp/library-kb          # 本地入库并导出出题用正文，不外发
    python tests/eval/w3_library_qa.py check .tmp/library-kb          # 核对题集：gold 原文是否在入库正文中、拒答题是否被库内资料覆盖，不外发
    python tests/eval/w3_library_qa.py run .tmp/library-kb --allow-external-real -n 3 [--split dev] [-k L01-1]

run 会把问题和检索到的真实素材片段发给已配置的生成 API，须先取得用户授权并显式加 --allow-external-real。
抽样清单、导出正文、题集和结果都在 files/eval/w3/library/（不进仓库）。
"""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import random
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))  # 让 tests.eval 可导入
from tests.eval.w3_e2e_qa import answer_cases, library, local_evidence, open_imports, write_report  # noqa: E402
from tests.eval.w3_e2e_scoring import contains  # noqa: E402

from briefly import config  # noqa: E402

LIBRARY = Path("resources/办公Agent测试素材库")
WORK = Path("files/eval/w3/library")
KB_NAME = "素材库抽样评测库"
FORMATS = (".doc", ".docx", ".pdf")
SEED = 20261031
TARGETS_PER_CATEGORY = 3   # 出题资料：每个大类 3 份，尽量覆盖 doc/docx/pdf
TARGET_PDFS = 12           # 出题资料中 PDF 不少于这么多份（从 PDF 多的大类补）
DISTRACTORS = 120          # 干扰资料：不出题，只让检索面对更多同类文档
MIN_BYTES, MAX_BYTES = 15 * 1024, 5 * 1024 * 1024  # 去掉空模板和超大手册


def sample() -> None:
    files = sorted(p for p in LIBRARY.rglob("*") if p.is_file() and p.suffix.lower() in FORMATS
                   and MIN_BYTES <= p.stat().st_size <= MAX_BYTES)
    rng = random.Random(SEED)
    rng.shuffle(files)
    seen_names, seen_hashes, pool = set(), set(), []
    for p in files:  # 同名或同内容只留一份，避免入库标题重复、判分对不上资料
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        if p.stem in seen_names or digest in seen_hashes:
            continue
        seen_names.add(p.stem)
        seen_hashes.add(digest)
        pool.append((p, digest))
    by_category: dict[str, list] = {}
    for item in pool:
        by_category.setdefault(item[0].relative_to(LIBRARY).parts[0], []).append(item)
    chosen, targets = set(), []
    for category, items in sorted(by_category.items()):
        picked = []
        for suffix in (".pdf", ".docx", ".doc"):  # 每类先各取一种格式，不足再随机补
            if len(picked) < TARGETS_PER_CATEGORY and (hit := next((i for i in items if i[0].suffix.lower() == suffix), None)):
                picked.append(hit)
        picked += [i for i in items if i not in picked][:TARGETS_PER_CATEGORY - len(picked)]
        targets += picked
        chosen |= {i[0] for i in picked}
    pdf_pool = [i for i in pool if i[0].suffix.lower() == ".pdf" and i[0] not in chosen]
    while sum(t[0].suffix.lower() == ".pdf" for t in targets) < TARGET_PDFS and pdf_pool:
        extra = pdf_pool.pop(0)
        targets.append(extra)
        chosen.add(extra[0])
    rest = [i for i in pool if i[0] not in chosen]
    distractors = rest[:DISTRACTORS]
    entries = []
    for role, items in (("target", targets), ("distractor", distractors)):
        for n, (p, digest) in enumerate(items, 1):
            entries.append(dict(id=f"{'L' if role == 'target' else 'X'}{n:03d}", role=role,
                                path=p.relative_to(LIBRARY).as_posix(), format=p.suffix.lower().lstrip("."),
                                category=p.relative_to(LIBRARY).parts[0], bytes=p.stat().st_size, sha256=digest))
    WORK.mkdir(parents=True, exist_ok=True)
    (WORK / "sample.json").write_text(json.dumps(dict(seed=SEED, library=LIBRARY.as_posix(), candidates=len(pool),
                                                      entries=entries), ensure_ascii=False, indent=1), encoding="utf-8")
    counts = Counter((e["role"], e["format"]) for e in entries)
    print(f"候选 {len(pool)} 份；抽样", dict(sorted(counts.items())))


def load_sample() -> list[dict]:
    return json.loads((WORK / "sample.json").read_text(encoding="utf-8"))["entries"]


def build(root: Path) -> None:
    from briefly.llm import create_local_embeddings
    entries = load_sample()
    root.mkdir(parents=True, exist_ok=False)
    encoder = create_local_embeddings(config.EMBEDDING_DIR)
    engine, imports = open_imports(root, encoder)
    manifest = {}
    try:
        kb = imports.knowledge.create(KB_NAME)
        for entry in entries:
            path = LIBRARY / entry["path"]
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise SystemExit(f"{entry['id']} 原件已变化：{entry['path']}")
            record = dict(file=path.name, format=entry["format"], role=entry["role"])
            try:
                job, _ = imports.enqueue(kb.id, path.name, data, request_key=entry["id"], source_key=entry["id"])
                preview = imports.parse(kb.id, job.id)
                ready = imports.confirm(kb.id, job.id, preview.preview["digest"])
                report = imports.store.get(ready.material_id).parse_report
                record.update(status=ready.status, chunks=len(preview.preview["chunks"]),
                              table_structure=report.get("table_structure_status"),
                              partial_ranges=len(report.get("table_partial_ranges") or []),
                              converted=report.get("conversion"))
            except Exception as exc:  # 入库失败本身是评测结果，记录后继续
                record.update(status="failed", error=f"{type(exc).__name__}: {exc}"[:300])
            manifest[entry["id"]] = record
            print(entry["id"], entry["format"], record["status"], record.get("chunks", record.get("error")), flush=True)
        (root / "manifest.json").write_text(json.dumps(dict(kb_id=kb.id, documents=manifest), ensure_ascii=False, indent=1),
                                            encoding="utf-8")
        export(imports, kb.id, entries, manifest)
    finally:
        engine.dispose()
    statuses = Counter((r["format"], r["status"]) for r in manifest.values())
    print("入库结果", dict(sorted(statuses.items())))


def export(imports, kb_id, entries, manifest) -> None:
    """把出题资料的入库正文导出给出题 agent；只导出入库成功的出题资料。"""
    out = WORK / "targets"
    out.mkdir(parents=True, exist_ok=True)
    texts = {}
    for item in imports.knowledge.documents(kb_id):
        texts[item.source_key] = imports.store.get(imports.knowledge.versions(kb_id, item.id)[1][-1].material_id).content
    index = []
    for entry in entries:
        if entry["role"] != "target" or entry["id"] not in texts:
            continue
        name = f"{entry['id']}.md"
        (out / name).write_text(f"<!-- 资料编号 {entry['id']}；原件 {entry['path']} -->\n\n{texts[entry['id']]}", encoding="utf-8")
        index.append(dict(id=entry["id"], title=manifest[entry["id"]]["file"], format=entry["format"],
                          category=entry["category"], chars=len(texts[entry["id"]]), export=f"targets/{name}"))
    (WORK / "targets.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已导出出题资料 {len(index)} 份：{out}")


def load_cases(split: str, keys: list[str]) -> list[dict]:
    cases = [json.loads(line) for line in (WORK / "cases.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    if keys:
        return [c for c in cases if c["id"] in keys]
    return [c for c in cases if split == "all" or c["split"] == split]


def doc_format_of(formats: dict[str, str]):
    def of(case):
        found = {formats[g["doc"]] for g in case["gold"] if g["doc"] in formats}
        return "pdf" if "pdf" in found else "docx" if "docx" in found else "doc" if found else "none"
    return of


def check(root: Path) -> None:
    """不外发：gold 原文须出现在对应资料的入库正文里；拒答题按原问题本地检索，列出最相关片段供人工确认库里确实没有。"""
    from briefly.knowledge.retrieval import RetrievalService, similarity
    from briefly.llm import create_local_embeddings
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    cases = load_cases("all", [])
    engine, imports = open_imports(root, create_local_embeddings(config.EMBEDDING_DIR))
    lines = [f"# 题集核对 {datetime.now():%Y%m%d-%H%M%S}", ""]
    try:
        kb_id = manifest["kb_id"]
        texts = {item.source_key: imports.store.get(imports.knowledge.versions(kb_id, item.id)[1][-1].material_id).content
                 for item in imports.knowledge.documents(kb_id)}
        retrieval = RetrievalService(imports)
        missing = [(c["id"], g["doc"], g["text"]) for c in cases for g in c["gold"]
                   if not contains(texts.get(g["doc"], ""), g["text"])]
        lines += [f"## gold 原文不在入库正文中（{len(missing)} 条）", ""]
        lines += [f"- {cid} {doc}：{text}" for cid, doc, text in missing] or ["无"]
        lines += ["", "## 拒答题的本地最相关片段（人工确认库里确实没有答案）", ""]
        for case in (c for c in cases if c["abstain"]):
            evidence = retrieval.search(kb_id, case["question"])["evidence"][:3]
            lines.append(f"- {case['id']} {case['question']}")
            for e in evidence:
                lines.append(f"  - {similarity(e)} 《{e['source']['title']}》{e['content'][:80].replace(chr(10), ' ')}")
    finally:
        engine.dispose()
    (WORK / "check.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"核对结果：{WORK / 'check.md'}；gold 缺失 {len(missing)} 条")


def run(root: Path, args) -> None:
    from briefly.llm import create_local_embeddings
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    cases = load_cases(args.split, args.k)
    encoder = create_local_embeddings(config.EMBEDDING_DIR)
    engine, imports = open_imports(root, encoder)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        kb_id = manifest["kb_id"]
        if [b.name for b in imports.knowledge.list_bases()] != [KB_NAME]:
            raise SystemExit("不是本脚本建的素材库抽样评测库，拒绝外发")
        imports.knowledge.set_egress(kb_id, True)  # 用户已授权本评测库外发，并在命令行显式确认
        formats = {key: doc["format"] for key, doc in manifest["documents"].items()}
        rows, events = answer_cases(imports, encoder, kb_id, cases, doc_format_of(formats), args, stamp)
    finally:
        engine.dispose()
    write_report(Path(args.output) / f"{stamp}-{args.path}", args, root, manifest["documents"], rows, events, stamp)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sample")
    for name in ("build", "check"):
        sub.add_parser(name).add_argument("root", type=Path)
    r = sub.add_parser("run")
    r.add_argument("root", type=Path)
    r.add_argument("--allow-external-real", action="store_true")
    r.add_argument("--path", choices=["assistant", "qa"], default="assistant")
    r.add_argument("--split", choices=["dev", "holdout", "all"], default="all")
    r.add_argument("-n", type=int, default=3)
    r.add_argument("-k", nargs="*", default=[])
    r.add_argument("--output", default=str(WORK / "runs"))
    args = parser.parse_args()
    if args.command == "sample":
        return sample()
    if not config.EMBEDDING_DIR:
        parser.error("需要本地 Embedding 目录（.env 的 BRIEFLY_EMBEDDING_DIR）")
    if args.command == "build":
        build(args.root)
    elif args.command == "check":
        check(args.root)
    else:
        if not args.allow_external_real:
            parser.error("会把真实素材片段发往已配置的生成 API，须取得授权后显式加 --allow-external-real")
        run(args.root, args)


if __name__ == "__main__":
    main()
