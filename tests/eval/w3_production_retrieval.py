"""生产来源/索引/召回服务的离线基准；仅原创语料，不调用生成模型。"""
import argparse
import json
from pathlib import Path
from time import perf_counter

from briefly.db import create_db_engine
from briefly.knowledge.index import LocalIndex
from briefly.knowledge.ingestion import ImportService
from briefly.knowledge.retrieval import RetrievalService, PARAMETERS
from briefly.knowledge.slicing import token_slices
from briefly.llm import create_local_embeddings


def covered(document, quote, hits):
    start = document.find(quote)
    if start < 0:
        raise ValueError("评测依据不在原文")
    target = start + len(quote)
    for hit in sorted(hits, key=lambda h: h["start"]):
        if hit["start"] <= start < hit["end"]:
            start = max(start, hit["end"])
    return start >= target


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("-n", type=int, default=3)
    parser.add_argument("--document-context", action="store_true")
    args = parser.parse_args()
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    fixtures = Path(__file__).parent / "fixtures" / "w3_rag"
    documents = json.loads((fixtures / "documents.json").read_text(encoding="utf-8"))
    expectations = json.loads((fixtures / "expectations.json").read_text(encoding="utf-8"))
    encoder = create_local_embeddings(args.model_dir)
    engine = create_db_engine("sqlite:///" + (root / "app.db").as_posix())
    events = []
    try:
        imports = ImportService(engine, root, LocalIndex(root / "index.sqlite"), lambda: encoder)
        kb = imports.knowledge.create("生产路径原创基准")
        mapping, originals = {}, {}
        for item in documents["documents"]:
            material, _ = imports.store.add_text(item["title"], item["text"], markdown=True)
            config, slices = token_slices(material.content, item["title"], True, encoder)
            doc, version, _ = imports.knowledge.stage_slices(kb.id, material.id, item["id"], config, slices,
                business_version=item["version"])
            imports.lifecycle.build(kb.id, doc.id, version.id, encoder, expected_revision=1)
            mapping[item["id"]], originals[item["id"]] = doc.id, material.content
        retrieval = RetrievalService(imports)
        for repeat in range(args.n):
            for case in expectations["cases"]:
                started = perf_counter()
                event = dict(case=case["id"], repeat=repeat, answerability=case["answerability"],
                             groups=len(case["evidence_groups"]))
                try:
                    response = retrieval.search(kb.id, case["question"], document_ids=[mapping.get(key, key) for key in case["scope"]], include_document_context=args.document_context)
                    hits = response["evidence"]
                    found = 0
                    for group in case["evidence_groups"]:
                        matched = False
                        for label in group:
                            expected = expectations["evidence"][label]
                            doc_id = expected["document"]
                            if covered(originals[doc_id], expected["quote"], [h for h in hits if h["document_id"] == mapping[doc_id]]):
                                matched = True
                        found += matched
                    event.update(status=response["status"], found=found, candidates=len(hits),
                                 selected=[dict(document=next(k for k, v in mapping.items() if v == h["document_id"]),
                                                start=h["start"], end=h["end"]) for h in hits])
                    assert all(h["document_id"] in {mapping[k] for k in case["scope"]} for h in hits)
                except ValueError as exc:
                    event.update(status="error", error=type(exc).__name__)
                event["seconds"] = round(perf_counter() - started, 4)
                events.append(event)
        answerable = [e for e in events if e["answerability"] == "answerable"]
        negatives = [e for e in events if e["answerability"] == "insufficient_evidence"]
        summary = dict(events=len(events), answerable=len(answerable),
            fully_covered=sum(e.get("found") == e["groups"] for e in answerable),
            unanswerable=len(negatives), negatives_with_candidates=sum(e.get("candidates", 0) > 0 for e in negatives),
            external_generation_calls=0, parameters={**PARAMETERS, "version":2 if args.document_context else 1, "document_context": args.document_context}, model=encoder.identity,
            scope_note="单个隔离评测库按文档显式范围，不模拟业务版本替换；不等于答案准确率")
    finally:
        engine.dispose()
    (root / "report.json").write_text(json.dumps(dict(summary=summary, events=events), ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "model"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
