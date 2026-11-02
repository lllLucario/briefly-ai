"""手动运行：真实资料按正式入库流程（隔离解析 → 预览 → 确认 → 真实索引）验证图片文字识别。

只在本机运行：真实 RapidOCR、本地 BGE Embedding、SQLite 索引；不调用生成模型、不外发。原件只读，
通过 --samples（JSON 列表，每项含 path、kind）显式指定。输出只保留计数、耗时、置信度和哈希，不写业务正文；
--audit 另把少量识别段落和对应页面渲染图存到指定目录（应在临时目录，供人工核对，不进仓库）。

    python tests/eval/ocr_real_validation.py --samples samples.json --model-dir C:/models/bge-small-zh-v1.5 \
        --output .tmp/ocr-real --audit .tmp/ocr-audit
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from briefly import config, ocr  # noqa: E402
from briefly.db import create_db_engine  # noqa: E402
from briefly.knowledge.index import LocalIndex  # noqa: E402
from briefly.knowledge.ingestion import ImportService  # noqa: E402
from briefly.knowledge.retrieval import RetrievalService  # noqa: E402
from briefly.llm import create_local_embeddings  # noqa: E402
from briefly.parse_process import ParseFailure, load_isolated  # noqa: E402


def baseline(name, data):
    """关闭 OCR 时的结果（改动前的行为）：被拒绝的原因或正文字数。"""
    previous = config.OCR
    config.OCR = "off"
    try:
        doc = load_isolated(name, data)
        return dict(status="ok", chars=len(doc.text))
    except ParseFailure as exc:
        return dict(status="rejected", reason=str(exc)[:80])
    finally:
        config.OCR = previous


def probe_phrase(text, block):
    """从一段可信的识别文字里取检索探针：最长的一行取前 16 个字。"""
    segment = text[block["start"]:block["end"]].splitlines()[1:-1]
    line = max(segment, key=len, default="").strip()
    return line[:16] if len(line) >= 6 else None


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--samples", required=True)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--audit")
    parser.add_argument("--resources", default="W:/briefly-ai")
    parser.add_argument("--skip-baseline", action="store_true")
    args = parser.parse_args()
    config.OCR = "auto"
    samples = json.loads(Path(args.samples).read_text(encoding="utf-8"))
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    audit = Path(args.audit).resolve() if args.audit else None
    if audit:
        audit.mkdir(parents=True, exist_ok=True)
    encoder = create_local_embeddings(args.model_dir)
    engine = create_db_engine("sqlite:///" + (root / "app.db").as_posix())
    report = dict(generation_model_calls=0, external_calls=0, ocr=ocr.identity(), parse_timeout=config.PARSE_TIMEOUT_S,
                  files=[])
    try:
        svc = ImportService(engine, root, LocalIndex(root / "index.sqlite"), lambda: encoder)
        retrieval = RetrievalService(svc)
        kb = svc.knowledge.create("OCR真实资料验证")
        for number, sample in enumerate(samples):
            path = Path(args.resources) / sample["path"]
            data = path.read_bytes()
            event = dict(kind=sample["kind"], name_sha256=hashlib.sha256(sample["path"].encode()).hexdigest()[:12],
                         size=len(data), file_sha256=hashlib.sha256(data).hexdigest()[:16])
            if not args.skip_baseline:
                event["baseline"] = baseline(path.name, data)
            started = perf_counter()
            try:
                job, _ = svc.enqueue(kb.id, path.name, data, request_key=f"ocr:{number}")
                preview = svc.parse(kb.id, job.id)
                event["parse_preview_seconds"] = round(perf_counter() - started, 2)
                material = svc.store.get(preview.material_id, workspace=False)
                text, parsed = material.content, material.parse_report
                info = parsed.get("ocr", {})
                blocks = info.get("blocks", [])
                material = svc.store.get(preview.material_id, workspace=False)
                headings = svc.outline(kb.id, job.id)["items"]
                chunks = [{**chunk, "content": material.content[chunk["start"]:chunk["end"]], "constraints": headings[i]["constraints"]}
                          for i, chunk in enumerate(preview.prepared["chunks"])]
                event.update(
                    chars=len(text), ocr_chars=sum(b["end"] - b["start"] for b in blocks),
                    blocks=len(blocks), unconfirmed=sum(b["unconfirmed"] for b in blocks),
                    mean_confidence=round(sum(b["confidence"] * b["chars"] for b in blocks) / max(1, sum(b["chars"] for b in blocks)), 4),
                    pages_recognized=info.get("pages_recognized", 0), images_recognized=info.get("images_recognized", 0),
                    skipped=info.get("skipped", {}), failed=info.get("failed", 0), partial=info.get("partial", False),
                    ocr_seconds=info.get("seconds"), pages=parsed.get("pages") or parsed.get("slides"),
                    chunk_count=len(chunks), restricted_chunks=sum(bool(c["constraints"]) for c in chunks),
                    restricted_kinds=sorted({k for c in chunks for k in c["constraints"]}),
                    warnings=len(parsed.get("warnings", [])))
                tick = perf_counter()
                ready = svc.build_index(kb.id, job.id)
                event.update(status=ready.status, confirm_index_seconds=round(perf_counter() - tick, 2))
                confident = [b for b in blocks if not b["unconfirmed"]]
                phrase = next((p for b in sorted(confident, key=lambda b: -b["chars"]) if (p := probe_phrase(text, b))), None)
                if phrase:
                    document = next(d.id for d in svc.knowledge.documents(kb.id) if d.source_key == job.source_key)
                    hits = retrieval.search(kb.id, phrase, document_ids=[document])["evidence"]
                    ranks = [i for i, hit in enumerate(hits, 1) if phrase in hit["content"]]
                    event["retrieval_probe"] = dict(sha=hashlib.sha256(phrase.encode()).hexdigest()[:12],
                                                    rank=ranks[0] if ranks else None, hits=len(hits))
                if audit and blocks:
                    _audit(audit, number, path, data, text, blocks)
            except (ValueError, ParseFailure) as exc:
                event.update(status="failed", error=str(exc)[:120])
            event["total_seconds"] = round(perf_counter() - started, 2)
            report["files"].append(event)
            print(json.dumps({k: event.get(k) for k in ("kind", "status", "blocks", "unconfirmed", "mean_confidence",
                                                         "pages_recognized", "images_recognized", "skipped", "total_seconds",
                                                         "retrieval_probe", "error")}, ensure_ascii=False), flush=True)
    finally:
        engine.dispose()
    (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")


def _audit(folder, number, path, data, text, blocks):
    """人工核对材料：PDF 识别页的渲染图＋识别文字；其他格式只存识别文字。"""
    target = folder / f"{number:02d}"
    target.mkdir(exist_ok=True)
    picked = blocks[:2] + [b for b in blocks if b["unconfirmed"]][:1]
    for k, block in enumerate(picked):
        (target / f"block{k}.txt").write_text(f"{path.name}\n{block['location']} {block['confidence']}\n\n"
                                              + text[block["start"]:block["end"]], encoding="utf-8")
        if path.suffix.lower() == ".pdf" and block["page"]:
            import pypdfium2
            document = pypdfium2.PdfDocument(data)
            try:
                ocr.render_page(document[block["page"] - 1]).save(target / f"block{k}.png")
            finally:
                document.close()


if __name__ == "__main__":
    main()
