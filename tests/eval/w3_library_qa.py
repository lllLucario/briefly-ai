"""素材库抽样问答评测：多格式真实办公资料 → 本地入库 → 独立出题 → 助理回答 → 规则判分。

与 w3_e2e_qa.py（虚构资料）共用回答与判分流程；差别是资料为真实素材，规模更大、排版更杂，
用来看真实资料下的入库成功率、检索与回答质量。题目由另一个 agent 依据导出的正文独立撰写。

用法（仓库根目录）：
    python tests/eval/w3_library_qa.py sample                         # 分层抽样，写 files/eval/w3/library/sample.json
    python tests/eval/w3_library_qa.py build .tmp/library-kb          # 本地入库并导出出题用正文，不外发
    python tests/eval/w3_library_qa.py check .tmp/library-kb          # 核对题集：gold 原文是否在入库正文中、拒答题是否被库内资料覆盖，不外发
    python tests/eval/w3_library_qa.py run .tmp/library-kb --allow-external-real -n 3 [--split dev] [-k R01]
    python tests/eval/w3_library_qa.py rescore files/eval/w3/library/runs/<时间>-assistant   # 不调用模型，按当前规则重判
    python tests/eval/w3_library_qa.py --round 2 sample               # 新格式＋扫描件，种子20261101
    python tests/eval/w3_library_qa.py --round 2 --scan-source catalog sample  # 授权后从原始目录补完整扫描件
    python tests/eval/w3_library_qa.py --round 2 build .tmp/library-round2-kb
    python tests/eval/w3_library_qa.py --round 2 check .tmp/library-round2-kb

run 会把问题和检索到的真实素材片段发给已配置的生成 API，须先取得用户授权并显式加 --allow-external-real。
抽样清单、导出正文、题集和结果都在 files/eval/w3/library/（不进仓库）。
"""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import random
import re
import sqlite3
import sys
from time import perf_counter

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
ROUND = 1
SCAN_SOURCE = "library"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")


def sample_round2():
    """固定种子；筛查元数据只用于挑样，最终 SHA 仍以只读原件重新计算。"""
    first = json.loads((WORK.parent / "sample.json").read_text(encoding="utf-8"))["entries"]
    names = {Path(e["path"]).stem.casefold() for e in first}
    hashes = {e["sha256"] for e in first}
    excluded, pool = Counter(), []
    with sqlite3.connect("files/eval/resource-selection/catalog.sqlite") as db:
        catalog = {row[0]: row[1:] for row in db.execute("SELECT sha, chars, metrics, decision FROM files ORDER BY path")}
    rows = sorted(p for p in LIBRARY.rglob("*") if p.is_file() and p.suffix.lower() in
                  (*FORMATS, ".xlsx", ".xls", ".pptx", ".ppt"))
    rng = random.Random(20261101)
    rng.shuffle(rows)
    for p in rows:
        size, ext = p.stat().st_size, p.suffix.lower()
        if size > 10 * 1024 * 1024 or not p.exists():
            excluded["超10MB或原件不存在"] += 1
            continue
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        chars, metrics, decision = catalog.get(digest, (0, "{}", "unscreened"))
        info = json.loads(metrics or "{}")
        scan = ext == ".pdf" and (decision == "needs_ocr" or info.get("needs_ocr"))
        if not scan and (chars or 0) < 500:
            excluded["正文少于500字，疑似空模板或纯图片"] += 1
            continue
        if p.stem.casefold() in names:
            excluded["第一轮或同名"] += 1
            continue
        if digest in hashes:
            excluded["第一轮或同内容"] += 1
            continue
        names.add(p.stem.casefold())
        hashes.add(digest)
        pool.append(dict(path=p.relative_to(LIBRARY).as_posix(), format=ext[1:],
                         category=p.relative_to(LIBRARY).parts[0], bytes=size, sha256=digest,
                         needs_ocr=bool(scan), pages=info.get("pages"), screened_chars=chars))
    targets = []
    for fmt in ("xlsx", "xls", "pptx", "ppt", "pdf"):
        candidates = [e for e in pool if e["format"] == fmt and (fmt != "pdf" or e["needs_ocr"])]
        targets.extend(candidates[:6])
    # 归类后的素材库未保留筛查目录中的全部 needs_ocr 标记，补做本地页级筛查。
    scans = scan_candidates(first, supplement=SCAN_SOURCE == "catalog")
    targets = [e for e in targets if e["format"] != "pdf"] + scans
    target_hashes = {e["sha256"] for e in targets}
    target_names = {Path(e["path"]).stem.casefold() for e in targets}
    rest = [e for e in pool if e["sha256"] not in target_hashes and Path(e["path"]).stem.casefold() not in target_names]
    categories = {e["category"] for e in targets}
    rest.sort(key=lambda e: e["category"] not in categories)
    distractors = []
    for fmt in ("doc", "docx", "pdf", "xlsx", "xls", "pptx", "ppt"):
        distractors.extend([e for e in rest if e["format"] == fmt][:14])
    distractors.extend([e for e in rest if e not in distractors][:100 - len(distractors)])
    entries = []
    for role, items, prefix in (("target", targets, "N"), ("distractor", distractors, "D")):
        entries.extend(dict(e, role=role, id=f"{prefix}{n:03d}") for n, e in enumerate(items, 1))
    write_json(WORK / "sample.json", dict(seed=20261101, library=LIBRARY.as_posix(), scan_source=SCAN_SOURCE, candidates=len(pool),
                                         excluded=dict(excluded), entries=entries))
    print("第二轮抽样", dict(Counter((e["role"], e["format"]) for e in entries)), flush=True)


def scan_candidates(first, *, supplement=False, count=6):
    import pdfplumber
    hashes = {e["sha256"] for e in first}
    names = {Path(e["path"]).stem.casefold() for e in first}
    if supplement:
        # 只在用户明确允许扩大扫描件来源并指定 --scan-source catalog 后读取这些原件。
        with sqlite3.connect("files/eval/resource-selection/catalog.sqlite") as db:
            rows = db.execute("SELECT path, metrics FROM files WHERE decision='needs_ocr' AND ext='.pdf' AND size<=10485760 ORDER BY path").fetchall()
        paths = [Path(p) for p, metrics in rows if 3 <= (json.loads(metrics or "{}").get("pages") or 0) <= 40
                 and any(w in Path(p).stem for w in ("制度", "规范", "办法", "程序", "管理"))
                 and not any(w in Path(p).stem for w in ("申请", "请示", "明细表", "目录", "标识"))]
    else:
        paths = sorted(p for p in LIBRARY.rglob("*") if p.suffix.lower() == ".pdf")
    random.Random(20261101).shuffle(paths)
    chosen = []
    for p in paths:
        if p.stat().st_size > 10 * 1024 * 1024 or p.stem.casefold() in names:
            continue
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        if digest in hashes:
            continue
        try:
            with pdfplumber.open(p) as doc:
                page = doc.pages[0]
                if len((page.extract_text() or "").strip()) >= 20 or not page.images:
                    continue
                if supplement and any(len(doc.pages[i].chars) >= 20 or not doc.pages[i].images
                                      for i in {len(doc.pages)//2, len(doc.pages)-1}):
                    continue
                relative = Path(os.path.relpath(p, LIBRARY)).as_posix()
                chosen.append(dict(path=relative, format="pdf", source_scope="catalog" if supplement else "library",
                                   category=p.parts[1] if supplement else p.relative_to(LIBRARY).parts[0], bytes=p.stat().st_size,
                                   sha256=digest, needs_ocr=True, pages=len(doc.pages)))
        except Exception:
            continue
        names.add(p.stem.casefold())
        hashes.add(digest)
        if len(chosen) == count:
            break
    if len(chosen) < count:
        raise ValueError(f"扫描 PDF 只找到 {len(chosen)} 份，须检查抽样范围")
    return chosen


def sample() -> None:
    if ROUND == 2:
        return sample_round2()
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


def build(root: Path, resume=False) -> None:
    from briefly.llm import create_local_embeddings
    entries = load_sample()
    if ROUND == 2:
        config.OCR = "auto"
        from briefly import ocr
        if not ocr.enabled():
            raise SystemExit("第二轮需要安装本地 OCR（.[ocr]）")
    root.mkdir(parents=True, exist_ok=resume)
    encoder = create_local_embeddings(config.EMBEDDING_DIR)
    engine, imports = open_imports(root, encoder)
    existing = json.loads((root / "manifest.json").read_text(encoding="utf-8")) if resume else None
    manifest = existing["documents"] if existing else {}
    try:
        if existing:
            bases = imports.knowledge.list_bases()
            if len(bases) != 1 or bases[0].id != existing["kb_id"] or bases[0].name != KB_NAME:
                raise SystemExit("不是本脚本专用评测库，拒绝续建")
            kb = bases[0]
        else:
            kb = imports.knowledge.create(KB_NAME)
        for entry in entries:
            if entry["role"] == "rejected_target":
                if entry["id"] in manifest:
                    manifest[entry["id"]].update(selected=False, exclusion_reason=entry.get("exclusion_reason"))
                continue
            path = LIBRARY / entry["path"]
            data = path.read_bytes()
            if hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise SystemExit(f"{entry['id']} 原件已变化：{entry['path']}")
            if entry["id"] in manifest:
                # 正式幂等入口核验文件名、原件SHA、source_key与切片配置，不凭本地清单跳过。
                previous, _ = imports.enqueue(kb.id, path.name, data, request_key=entry["id"], source_key=entry["id"])
                if previous.status != manifest[entry["id"]]["status"]:
                    raise SystemExit("续建时任务状态与清单不符，请核对上次中断状态")
                manifest[entry["id"]]["sha256"] = entry["sha256"]
                for key in ("parser", "ocr_blocks", "ocr_low_confidence"):
                    manifest[entry["id"]].setdefault(key, None)
                continue
            record = dict(file=path.name, format=entry["format"], role=entry["role"], sha256=entry["sha256"],
                          parser=None, ocr_blocks=None, ocr_low_confidence=None)
            started = perf_counter()
            try:
                job, _ = imports.enqueue(kb.id, path.name, data, request_key=entry["id"], source_key=entry["id"])
                preview = imports.parse(kb.id, job.id)
                ready = imports.confirm(kb.id, job.id, preview.preview["digest"])
                report = imports.store.get(ready.material_id, workspace=False).parse_report
                record.update(status=ready.status, chunks=len(preview.preview["chunks"]),
                              table_structure=report.get("table_structure_status"),
                              partial_ranges=len(report.get("table_partial_ranges") or []),
                              converted=report.get("conversion"))
                record.update(parser=report.get("parser"), parse_report=report,
                              ocr_blocks=len(report.get("ocr", {}).get("blocks", [])),
                              ocr_low_confidence=len(report.get("ocr_unconfirmed_ranges") or []))
            except Exception as exc:  # 入库失败本身是评测结果，记录后继续
                record.update(status="failed", error=f"{type(exc).__name__}: {exc}"[:300])
            record["seconds"] = round(perf_counter() - started, 2)
            manifest[entry["id"]] = record
            write_json(root / "manifest.json", dict(kb_id=kb.id, documents=manifest))
            print(entry["id"], entry["format"], record["status"], record.get("chunks", record.get("error")), flush=True)
        rejected = {e["id"] for e in entries if e["role"] == "rejected_target"}
        for doc in imports.knowledge.documents(kb.id):
            if doc.source_key in rejected:
                imports.knowledge.remove_document(kb.id, doc.id, expected_revision=doc.revision)
        (root / "manifest.json").write_text(json.dumps(dict(kb_id=kb.id, documents=manifest), ensure_ascii=False, indent=1),
                                            encoding="utf-8")
        export(imports, kb.id, entries, manifest)
        if ROUND == 2:
            prepare_workspace(root, engine, imports, encoder)
            write_json(WORK / "build-results.json", dict(kb_root=str(root), kb_id=kb.id,
                       ocr=ocr.identity(), generation_model_calls=0, documents=manifest))
    finally:
        engine.dispose()
    statuses = Counter((r["format"], r["status"]) for r in manifest.values())
    print("入库结果", dict(sorted(statuses.items())))


def export(imports, kb_id, entries, manifest) -> None:
    """把出题资料的入库正文导出给出题 agent；只导出入库成功的出题资料。"""
    out = WORK / "targets"
    out.mkdir(parents=True, exist_ok=True)
    for entry in entries:
        if entry["role"] == "rejected_target" and re.fullmatch(r"N\d{3}", entry["id"]):
            (out / f"{entry['id']}.md").unlink(missing_ok=True)
    texts = {}
    for item in imports.knowledge.documents(kb_id):
        texts[item.source_key] = imports.store.get(imports.knowledge.versions(kb_id, item.id)[1][-1].material_id, workspace=False).content
    index = []
    for entry in entries:
        if entry["role"] != "target" or entry["id"] not in texts:
            continue
        name = f"{entry['id']}.md"
        (out / name).write_text(f"<!-- 资料编号 {entry['id']}；原件 {entry['path']} -->\n\n{texts[entry['id']]}", encoding="utf-8")
        index.append(dict(id=entry["id"], title=manifest[entry["id"]]["file"], format=entry["format"],
                          original=entry["path"], source_scope=entry.get("source_scope", "library"),
                          category=entry["category"], chars=len(texts[entry["id"]]), export=f"targets/{name}",
                          note="只识别前40页（仍受80秒预算限制）" if (entry.get("pages") or 0) > 40 and entry.get("needs_ocr") else "",
                          ocr_blocks=manifest[entry["id"]].get("ocr_blocks", 0),
                          ocr_low_confidence=manifest[entry["id"]].get("ocr_low_confidence", 0),
                          ocr_pages_recognized=manifest[entry["id"]].get("parse_report", {}).get("ocr", {}).get("pages_recognized", 0),
                          ocr_partial=manifest[entry["id"]].get("parse_report", {}).get("ocr", {}).get("partial", False)))
    (WORK / "targets.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已导出出题资料 {len(index)} 份：{out}")


def workspace_files():
    return json.loads((WORK / "workspace.json").read_text(encoding="utf-8"))["files"]


def prepare_workspace(root, engine, imports, encoder):
    """通过材料添加入口建立真实关联；不自动确认疑似关联。"""
    from briefly.links import LinkService
    from tests.eval.w3_e2e_qa import attach_workspace
    titles, _ = library(imports, json.loads((root / "manifest.json").read_text(encoding="utf-8"))["kb_id"])
    docs = {d.source_key: d for d in imports.knowledge.documents(
        json.loads((root / "manifest.json").read_text(encoding="utf-8"))["kb_id"])}
    texts = {key: imports.store.get(imports.knowledge.versions(d.kb_id, d.id)[1][-1].material_id,
                                   workspace=False).content for key, d in docs.items()}
    if not (WORK / "workspace.json").exists():
        targets = [e for e in load_sample() if e["role"] == "target" and e["id"] in texts]
        files, changes = [], []
        for e in targets[:2]:
            files.append(dict(id=f"W{len(files)+1}", kind="identical", base=e["id"],
                              title=titles[e["id"]], content=texts[e["id"]]))
        # 优先取有数字期限/金额和规定段落的制度；保持绝大部分原文以检验疑似改稿。
        candidates = sorted((e["id"] for e in targets),
                            key=lambda k: (not any(w in titles[k] for w in ("制度", "薪酬", "离职", "规定")), k))
        for key in candidates:
            body = texts[key]
            matches = list(re.finditer(r"\d+(?:\.\d+)?\s*(?:工作日|小时|万元|元|个月|天|日|分钟)", body))
            paragraphs = [p for p in body.split("\n") if 20 <= len(p) <= 250 and "|" not in p and not p.startswith(("#", "〔"))]
            paragraphs = [p for p in paragraphs if not any(m.group() in p for m in matches[:2])]
            rules = [p for p in paragraphs if any(word in p for word in ("应", "须", "不得", "负责", "工资", "审批"))]
            if len(matches) < 2 or len(paragraphs) < 2 or not rules or len(body) < 1000:
                continue
            rule = rules[0]
            removed = next((p for p in reversed(paragraphs) if p != rule), None)
            if removed is None:
                continue
            wid = f"W{len(files)+1}"
            edits = []
            for match in matches[:2]:
                old = match.group()
                new = re.sub(r"\d+(?:\.\d+)?", lambda m: str(int(float(m.group())) + 2), old, count=1)
                edits.append(dict(old=old, new=new, location=f"原文字符{match.start()}，上下文：{body[max(0, match.start()-25):match.end()+25]}"))
            edits += [dict(old=rule, new="部门讨论稿：本项须由经办人先登记，再交直属负责人复核。", location=f"原文字符{body.index(rule)}，规定段落"),
                      dict(old=removed, new="", location=f"原文字符{body.index(removed)}，删除段落"),
                      dict(old="", new="## 部门试行补充（尚未发布）\n本部门试行期间，每周三由资料管理员汇总执行反馈，周五提交负责人复核。", location="文末新增")]
            revised = body
            # 使用原文偏移倒序修改数字，避免同值出现多次时改错位置。
            for match, edit in reversed(list(zip(matches[:2], edits[:2]))):
                revised = revised[:match.start()] + edit["new"] + revised[match.end():]
            for edit in edits[2:4]:
                revised = revised.replace(edit["old"], edit["new"], 1)
            revised += "\n\n" + edits[4]["new"]
            files.append(dict(id=wid, kind="revision", base=key, title=Path(titles[key]).stem + "（部门修订稿）", content=revised))
            changes.append(dict(id=wid, base=key, edits=edits))
            if sum(f["kind"] == "revision" for f in files) == 2:
                break
        if len(changes) < 2:
            raise ValueError("没有两份适合改稿的真实制度，请人工挑选后写 workspace.json")
        for n, base in enumerate(targets[2:4], 1):
            title = titles[base["id"]]
            text = (f"# 《{title}》个人学习计划\n参考资料：{title}。\n"
                    f"我计划在11月{10+n}日完成阅读，11月{13+n}日和同组同事讨论。\n"
                    f"个人待办编号：学习-{n}；复盘记录保存到个人笔记，不作为企业制度。\n"
                    "讨论前先核对原资料中的术语和表格；不确定处登记问题，不自行补写标准。")
            files.append(dict(id=f"W{len(files)+1}", kind="workspace_only", base=base["id"],
                              title=f"资料学习计划{n}（个人笔记）", content=text))
        for f in files:
            f["export"] = f"workspace/{f['id']}.md"
            path = WORK / f["export"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f["content"], encoding="utf-8")
        write_json(WORK / "workspace.json", dict(note="本地评测衍生资料，改稿和个人安排均为评测构造，不是正式制度或真实员工事实。", files=files))
        lines = ["# 工作区改稿记录", "", "以下为评测构造，正式依据仍为知识库原件。", ""]
        for change in changes:
            lines += [f"## {change['id']} ← {change['base']}", ""]
            for e in change["edits"]:
                lines += [f"- 位置：{e['location']}\n  原文：{e['old'] or '（无）'}\n  改后：{e['new'] or '（删除）'}"]
        (WORK / "changes.md").write_text("\n".join(lines), encoding="utf-8")
    links = LinkService(engine)
    # add_text 的去重结果须持久化，供外发前按 ID＋标题＋正文逐份核验。
    files = workspace_files()
    for item in files:
        material, _ = imports.store.add_text(item["title"], item["content"], markdown=True)
        item.update(material_id=material.id, title=material.title)
    links.detect()
    for item in files:
        item["links"] = links.material_links(item["material_id"])
        expected = docs.get(item["base"])
        item["expected_link_found"] = any(l["document_id"] == expected.id for l in item["links"]) if expected else False
    write_json(WORK / "workspace.json", dict(files=files, note="评测构造；疑似改稿未自动确认"))
    sync = attach_workspace(root, engine, imports.store, encoder)
    write_json(WORK / "workspace" / "index-result.json", sync)
    print("工作区", [(f["id"], f["kind"], f["expected_link_found"]) for f in files], flush=True)


def load_cases(split: str, keys: list[str]) -> list[dict]:
    cases = [json.loads(line) for line in (WORK / "cases.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    if keys:
        return [c for c in cases if c["id"] in keys]
    return [c for c in cases if split == "all" or c["split"] == split]


def doc_format_of(formats: dict[str, str]):
    def of(case):
        found = {formats[g["doc"]] for g in case["gold"] if g["doc"] in formats}
        if ROUND == 1:
            return "pdf" if "pdf" in found else "docx" if "docx" in found else "doc" if found else "none"
        if any(g["doc"].startswith("W") for g in case["gold"]):
            found.add("workspace")
        return "+".join(sorted(found)) if found else "none"
    return of


def missing_gold(cases, texts):
    return [(c["id"], g["doc"], g["text"]) for c in cases for g in c["gold"]
            if not g["text"] or g["text"] not in texts.get(g["doc"], "")]


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
        texts = {item.source_key: imports.store.get(imports.knowledge.versions(kb_id, item.id)[1][-1].material_id, workspace=False).content
                 for item in imports.knowledge.documents(kb_id)}
        retrieval = RetrievalService(imports)
        qa = None
        if ROUND == 2:
            from briefly.knowledge.qa import KnowledgeQA
            from tests.eval.w3_e2e_qa import attach_workspace
            attach_workspace(root, engine, imports.store, imports.encoder_factory())
            texts.update({f["id"]: f["content"] for f in workspace_files()})
            qa = KnowledgeQA(retrieval, lambda: None, workspace=imports.store)
        missing = missing_gold(cases, texts) if ROUND == 2 else [
            (c["id"], g["doc"], g["text"]) for c in cases for g in c["gold"]
            if not contains(texts.get(g["doc"], ""), g["text"])]
        lines += [f"## gold 原文不在入库正文中（{len(missing)} 条）", ""]
        lines += [f"- {cid} {doc}：{text}" for cid, doc, text in missing] or ["无"]
        lines += ["", "## 拒答题的本地最相关片段（人工确认库里确实没有答案）", ""]
        for case in (c for c in cases if c["abstain"]):
            evidence = retrieval.search(kb_id, case["question"])["evidence"][:3]
            lines.append(f"- {case['id']} {case['question']}")
            for e in evidence:
                lines.append(f"  - {similarity(e)} 《{e['source']['title']}》{e['content'][:80].replace(chr(10), ' ')}")
            if qa:
                for e in qa._workspace([case["question"]])[0] or []:
                    lines.append(f"  - 工作区《{e['source']['title']}》{e['content'][:160]}")
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
        if ROUND == 2:
            from tests.eval.w3_e2e_qa import attach_workspace
            validate_workspace(imports.store, workspace_files())
            attach_workspace(root, engine, imports.store, encoder)
        imports.knowledge.set_egress(kb_id, True)  # 用户已授权本评测库外发，并在命令行显式确认
        formats = {key: doc["format"] for key, doc in manifest["documents"].items()}
        runner = answer_round2 if ROUND == 2 else answer_cases
        rows, events = runner(imports, encoder, kb_id, cases, doc_format_of(formats), args, stamp)
    finally:
        engine.dispose()
    write_report(Path(args.output) / f"{stamp}-{args.path}", args, root, manifest["documents"], rows, events, stamp)
    if ROUND == 2:
        add_source_summary(Path(args.output) / f"{stamp}-{args.path}")


def add_source_summary(results):
    data = json.loads((results / "results.json").read_text(encoding="utf-8"))
    data["round"] = 2
    data["source_summary"] = {split: dict(
        runs=len(items := [e for e in data["events"] if split == "all" or e["split"] == split]),
        correct=sum(e.get("source_correct", False) for e in items),
        incorrect=sum(not e.get("source_correct", False) for e in items)) for split in ("all", "dev", "holdout")}

    def rates(items):
        return {rule: f"{sum(e.get(key, False) for e in items)}/{len(items)}"
                for rule, key in zip(CONFLICT_RULES, ("passed", "passed_if_noted"))}
    data["conflict_rules"] = {name: rates(items) for name, items in (
        ("all", data["events"]), ("mixed_conflict", [e for e in data["events"] if e["category"] == "mixed_conflict"]))}
    write_json(results / "results.json", data)
    with (results / "report.md").open("a", encoding="utf-8") as out:
        out.write("\n来源正确（实际引用来源＋冲突说明，规则判分）：\n\n")
        for split, count in data["source_summary"].items():
            out.write(f"- {split}：{count['correct']}/{count['runs']}；来源错误 {count['incorrect']} 次。\n")
        out.write("\n冲突题口径（主表按场景表态：问制度、问哪版优先须说明以知识库为准，处理自己的稿子只须指出不同）：\n\n")
        for name, count in data["conflict_rules"].items():
            out.write(f"- {name}：按场景表态 {count['priority']}；对照（只要求指出冲突） {count['noted']}。\n")


def validate_workspace(store, files):
    """外发前核对完整集合；同标题的篡改和多余文件均拒绝。"""
    actual = {m.id: (m.title, m.content) for m in store.list_materials()}
    expected = {f["material_id"]: (f["title"], f["content"].strip()) for f in files}
    if not expected or len(expected) != len(files) or actual != expected:
        raise SystemExit("工作区材料与 workspace.json 的 ID、标题、正文不完全一致，拒绝外发")


CONFLICT_NOTED = re.compile(r"不同|不一致|差异|差别|冲突|改为|改成|而工作区|工作区.{0,20}(?:写|为)"
                            r"|(?:没有|不再|未|不)保留|删除|删去|删掉")  # 删除型改稿：“部门稿没有保留这段说明”
_FORMAL = r"(?:知识库|正式版|正式制度|正式课件|正式方案|发布版)"
CONFLICT_PRIORITY = re.compile(rf"以.{{0,35}}{_FORMAL}.{{0,12}}为(?:准|据|依据)|(?:应|须|需要|优先|仍)按.{{0,20}}{_FORMAL}"
                               rf"|按.{{0,6}}{_FORMAL}.{{0,30}}为准"  # “按正式课件，应以‘30日’为准”
                               r"|未发布的?(?:改动|修订|内容).{0,6}不能当作现行")  # 按场景表态里问哪版优先的标准答法
# 冲突题口径（2026-11-02 用户定为按场景表态）：priority＝按题目的 scene 要求——问制度（regulation）、问哪版优先
# （which_first）须指出不同并说明以知识库正式版为准，处理自己的稿子（own_work）只须指出不同；noted＝一律只要求指出不同，
# 作为对照保留
CONFLICT_RULES = ("priority", "noted")


def score_source(case, answer, references, titles=None, conflict="priority"):
    """只认回答实际引用的来源；检索到或发出片段不代表回答引用了它。"""
    from briefly.knowledge.qa import cited_numbers
    cited = {f"依据{n}" for n in cited_numbers(answer)}

    def used(ref):
        if ref.get("label"):
            return ref["label"] in cited
        # 材料工具的正式出处格式是《标题》，而不是知识检索的〔依据N〕。
        return ref.get("origin") == "workspace" and f"《{ref['title']}》" in answer

    gold = case.get("gold", [])
    # 工作区副本与知识库原文在 gold 处逐字相同：联合检索按设计只留知识库片段并注明“工作区版本此处一致”，
    # 引用该知识库片段即覆盖工作区一侧。只看 gold 是否相同，不信依据头注记（改稿处注记可能错）。
    same = {g["doc"]: next(n["doc"] for n in gold if not n["doc"].startswith("W") and n["text"] == g["text"])
            for g in gold if g["doc"].startswith("W")
            and any(not n["doc"].startswith("W") and n["text"] == g["text"] for n in gold)}

    def cites(doc):
        origin = "workspace" if doc.startswith("W") else "knowledge"
        if any(used(r) and r.get("origin", "knowledge") == origin and r.get("title") == titles.get(doc) for r in references):
            return True
        return doc in same and cites(same[doc])

    origins = {r.get("origin", "knowledge") for r in references if used(r)}
    if titles and any(cites(base) for base in same.values()):
        origins.add("workspace")
    required = {"kb": {"knowledge"}, "workspace": {"workspace"},
                "both": {"knowledge", "workspace"}, "conflict": {"knowledge", "workspace"}, "none": set()}
    source = case.get("source", "none" if case["abstain"] else "kb")
    correct = required[source] <= origins
    if titles and source != "none":
        correct = correct and all(cites(g["doc"]) for g in gold)
    if source == "conflict":
        correct = correct and bool(CONFLICT_NOTED.search(answer))
        if conflict == "priority" and case.get("scene") != "own_work":
            correct = correct and bool(CONFLICT_PRIORITY.search(answer))
    return correct


def header_facts(references):
    """依据头随片段发给模型的出处信息（工作区文件的更新时间）；回答复述它不是无出处数字。"""
    return [r["updated_at"].replace("T", " ") for r in references if r.get("updated_at")]


def material_evidence(history):
    """只保留材料工具实际返回的段落，不能把材料全文或可见列表算成已读依据。"""
    evidence, references = [], []
    pattern = re.compile(r"【材料 (\d+)《([^》]+)》[^】]*】\n【材料开始】\n(.*?)\n【材料结束】", re.S)
    for message in history:
        if message.type != "tool" or message.name not in {"read_material", "search_materials"} or message.status == "error":
            continue
        ids = (message.artifact or {}).get("material_ids", [])
        for mid, title, content in pattern.findall(message.content):
            if int(mid) not in ids:
                continue
            evidence.append(dict(title=title, content=content, origin="workspace", withheld=False))
            references.append(dict(title=title, origin="workspace", material_id=int(mid), label=None))
    return evidence, references


def apply_source_score(scored, case, answer, references, titles=None):
    scored = dict(scored, problems=[p for p in scored["problems"] if p != "source_incorrect"])
    others = not any(p != "retrieval_miss" for p in scored["problems"])
    scored["source_correct"] = score_source(case, answer, references, titles)
    if not scored["source_correct"]:
        scored["problems"].append("source_incorrect")
    scored["passed"] = not any(p != "retrieval_miss" for p in scored["problems"])
    # 冲突题另记“只要求指出冲突”口径的结果；口径由产品决定，主通过率仍按出题时的 priority 口径
    scored["passed_if_noted"] = others and score_source(case, answer, references, titles, conflict="noted")
    return scored


def answer_round2(imports, encoder, kb_id, cases, format_of, args, stamp, model_factory=None):
    from briefly.agent import Agent
    from briefly.intent import IntentFunnel
    from briefly.knowledge.assistant import KnowledgeAssistant
    from briefly.knowledge.retrieval import RetrievalService
    from briefly.knowledge.qa import reference_entry
    from briefly.llm import create_chat_model
    from tests.eval.w3_e2e_qa import judge
    model_factory = model_factory or (lambda: create_chat_model(max_retries=1, timeout=90))
    knowledge = KnowledgeAssistant(RetrievalService(imports), model_factory, workspace=imports.store)
    captured = []
    original_bundle = knowledge.qa.bundle

    def capture_bundle(*a, **kw):
        result = original_bundle(*a, **kw)
        if result["status"] == "ready":
            captured.extend(result["evidence"])
        return result

    knowledge.qa.bundle = capture_bundle
    titles, corpus = library(imports, kb_id)
    for f in workspace_files():
        titles[f["id"]] = f["title"]
        corpus.append(f["content"])
    if args.path == "assistant":
        agent = Agent(model_factory(), store=imports.store)
        agent.enable_knowledge(knowledge, IntentFunnel(lambda: encoder, kb_probe=knowledge.relevance_signals))
    rows, events = [], []
    for case in cases:
        for repeat in range(args.n):
            started = perf_counter()
            event = dict(id=case["id"], category=case["category"], split=case["split"], repeat=repeat,
                         source=case.get("source"), references=[], evidence=[])
            try:
                captured.clear()
                if args.path == "assistant":
                    thread = f"library2-{case['id']}-{repeat}-{stamp}"
                    answer = agent.chat(case["question"], thread_id=thread, kb_id=kb_id)
                    history = agent.history_of(thread)
                    meta = history[-1].response_metadata.get("knowledge") or {}
                    status = meta.get("status")
                    references = meta.get("references", [])
                    event.update(queries=meta.get("queries"))
                    actual = list(captured)
                else:
                    result = knowledge.qa.answer(kb_id, case["question"])
                    answer, status, actual = result["answer"], result["status"], result.get("evidence", [])
                    references = [reference_entry(f"依据{i}", e) for i, e in enumerate(actual, 1)]
                evidence = [dict(title=e["source"]["title"], content=e["content"], withheld=False,
                                 origin=e.get("origin", "knowledge")) for e in actual]
                if args.path == "assistant":
                    material_parts, material_refs = material_evidence(history)
                    evidence.extend(material_parts)
                    references = [*references, *material_refs]
                scored = apply_source_score(judge(case, answer, status, evidence, titles, corpus + header_facts(references)),
                                            case, answer, references, titles)
                event.update(status=status, answer=answer, references=references, evidence=evidence)
            except Exception as exc:
                event.update(status="error", error=f"{type(exc).__name__}: {exc}"[:300])
                scored = dict(passed=False, problems=["error"], retrieval_hit=None, length=0, source_correct=False)
            event.update(scored, seconds=round(perf_counter() - started, 2))
            events.append(event)
            rows.append(dict(scored, category=case["category"], split=case["split"], doc_format=format_of(case)))
            print(case["id"], repeat, "PASS" if scored["passed"] else scored["problems"], flush=True)
    return rows, events


def rescore(results: Path) -> None:
    """不调用模型：按当前判分规则、题集和该次运行所用评测库的本地检索结果重新判分，覆盖同目录结果。"""
    from briefly.knowledge.retrieval import RetrievalService
    from briefly.llm import create_local_embeddings
    from tests.eval.w3_e2e_qa import judge, render
    from tests.eval.w3_e2e_scoring import summarize
    data = json.loads((results / "results.json").read_text(encoding="utf-8"))
    root = Path(data["kb_root"])
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    cases = {c["id"]: c for c in load_cases("all", [])}
    of = doc_format_of({key: doc["format"] for key, doc in manifest["documents"].items()})
    engine, imports = open_imports(root, create_local_embeddings(config.EMBEDDING_DIR))
    try:
        titles, corpus = library(imports, manifest["kb_id"])
        if ROUND == 2:
            for f in workspace_files():
                titles[f["id"]] = f["title"]
                corpus.append(f["content"])
            evidence = None  # 第二轮必须复用当次实际依据，不能用重新检索替代。
        else:
            retrieval = RetrievalService(imports)
            evidence = {key: local_evidence(retrieval, manifest["kb_id"], cases[key]["question"])
                        for key in {e["id"] for e in data["events"]}}
    finally:
        engine.dispose()
    rows = []
    for event in data["events"]:
        case = cases[event["id"]]
        if event["status"] != "error":
            scored = judge(case, event["answer"], event["status"], event["evidence"] if ROUND == 2 else evidence[event["id"]], titles,
                           corpus + header_facts(event.get("references", [])) if ROUND == 2 else corpus)
            if ROUND == 2:
                scored = apply_source_score(scored, case, event["answer"], event["references"], titles)
            event.update(scored)
        rows.append(dict({k: event[k] for k in ("passed", "problems", "retrieval_hit", "length")},
                         gold_withheld=event.get("gold_withheld"), category=case["category"], split=case["split"],
                         doc_format=of(case)))
    data["summary"] = {split: summarize([r for r in rows if split == "all" or r["split"] == split])
                       for split in ("all", "dev", "holdout")}
    data["rescored"] = datetime.now().strftime("%Y%m%d-%H%M%S")
    (results / "results.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (results / "report.md").write_text(render(data, data["events"]), encoding="utf-8")
    if ROUND == 2:
        add_source_summary(results)
    print(f"已重新判分：{results}")


def main():
    global WORK, ROUND, SCAN_SOURCE
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--round", type=int, choices=[1, 2], default=1)
    parser.add_argument("--scan-source", choices=["library", "catalog"], default="library",
                        help="catalog 仅在用户允许补抽原始目录完整扫描件后使用")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sample")
    for name in ("build", "check"):
        p = sub.add_parser(name)
        p.add_argument("root", type=Path)
        if name == "build":
            p.add_argument("--resume", action="store_true", help="核验原件身份后续建专用评测库")
    r = sub.add_parser("run")
    r.add_argument("root", type=Path)
    r.add_argument("--allow-external-real", action="store_true")
    r.add_argument("--path", choices=["assistant", "qa"], default="assistant")
    r.add_argument("--split", choices=["dev", "holdout", "all"], default="all")
    r.add_argument("-n", type=int, default=3)
    r.add_argument("-k", nargs="*", default=[])
    r.add_argument("--output")
    sub.add_parser("rescore", help="不调用模型，按当前规则重新判分已有运行").add_argument("results", type=Path)
    args = parser.parse_args()
    ROUND = args.round
    SCAN_SOURCE = args.scan_source
    if ROUND == 2:
        WORK = WORK / "round2"
    if args.command == "run":
        args.output = args.output or str(WORK / "runs")
    if args.command == "sample":
        return sample()
    if not config.EMBEDDING_DIR:
        parser.error("需要本地 Embedding 目录（.env 的 BRIEFLY_EMBEDDING_DIR）")
    if args.command == "build":
        build(args.root, args.resume)
    elif args.command == "check":
        check(args.root)
    elif args.command == "rescore":
        rescore(args.results)
    else:
        if not args.allow_external_real:
            parser.error("会把真实素材片段发往已配置的生成 API，须取得授权后显式加 --allow-external-real")
        run(args.root, args)


if __name__ == "__main__":
    main()
