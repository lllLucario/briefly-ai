"""用户手选资料：真实解析分阶段计时、正式缓存及同模型三档切片对照。

仅本地Embedding与检索，不调用生成模型；金标从Git忽略的本地JSON读取。
"""
import argparse
import json
from pathlib import Path
from time import perf_counter

from briefly.db import create_db_engine
from briefly.knowledge.index import LocalIndex
from briefly.knowledge.ingestion import ImportService
from briefly.knowledge.payload import evidence_payload
from briefly.knowledge.retrieval import RetrievalService, PARAMETERS
from briefly.llm import create_local_embeddings
from w3_production_retrieval import covered


def compare_fixed_budget(imports, retrieval, kb_id, trial, cases, encoder):
    selected = [f["document_id"] for f in trial["files"] if f["status"] == "ready"]
    result = []
    for case in cases:
        source = next(f for f in trial["files"] if case["source_hint"] in f["filename"])
        item = dict(question=case["question"],source=source["filename"],result="source_not_indexed")
        if source["status"] == "ready":
            material = imports.store.get(imports.get(kb_id,source["job_id"]).material_id)
            if case["quote"] not in material.content:
                raise ValueError("金标不在原文")
            found = retrieval.search(kb_id,case["question"],document_ids=selected,
                                     include_document_context=True,context_budget_only=True)
            hits = [h for h in found["evidence"] if h["document_id"] == source["document_id"]]
            tokens = encoder.token_length(evidence_payload(case["question"],found["evidence"]))
            assert tokens == found["diagnostics"]["context_tokens"] <= 4000
            item.update(result="covered" if covered(material.content,case["quote"],hits) else "missing_evidence",
                context_tokens=tokens,candidates=len(found["evidence"]),
                parameters=found["parameters"],references=[h["reference"] for h in found["evidence"]])
        result.append(item)
    trial["fixed_budget_cases"] = result
    trial["fixed_budget_covered"] = sum(c["result"]=="covered" for c in result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--documents", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--bge-dir", required=True)
    parser.add_argument("--jina-dir", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--recheck-existing", action="store_true", help="仅补已有索引的可变数量/固定token预算检索，不重新解析或建索引")
    args = parser.parse_args()
    files = sorted(p for p in args.documents.resolve().rglob("*") if p.is_file())
    if not 1 <= len(files) <= 16:
        parser.error("须明确提供1～16份已筛选资料")
    cases = json.loads(args.cases.read_text(encoding="utf-8"))
    root = args.output.resolve()
    if args.recheck_existing:
        report = json.loads((root/"report.json").read_text(encoding="utf-8"))
        engine = create_db_engine("sqlite:///"+(root/"app.db").as_posix())
        try:
            for model,folder in (("bge",args.bge_dir),("jina",args.jina_dir)):
                encoder = create_local_embeddings(folder,**({"max_input_tokens":1024} if model=="jina" else {}))
                imports = ImportService(engine,root,LocalIndex(root/"index.sqlite"),lambda:encoder)
                for trial in report["trials"]:
                    if trial.get("status")=="complete" and trial["name"].startswith(model):
                        compare_fixed_budget(imports,RetrievalService(imports),report["kb_id"],trial,cases,encoder)
                        print(trial["name"],trial["fixed_budget_covered"],flush=True)
                        (root/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        finally:
            engine.dispose()
        return
    root.mkdir(parents=True, exist_ok=False)
    report = dict(documents=str(args.documents.resolve()), generation_model_calls=0,
        context_tokens=PARAMETERS["context_tokens"], trials=[], model_load_seconds={},
        note="同模型比较；已见摘录探索题，非独立留出或答案准确率；大块不得扩大4000token最终依据包")

    def save():
        (root / "report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")

    tick = perf_counter()
    current = create_local_embeddings(args.bge_dir)
    report["model_load_seconds"]["bge"] = round(perf_counter() - tick, 4)
    engine = create_db_engine("sqlite:///" + (root / "app.db").as_posix())
    try:
        imports = ImportService(engine, root, LocalIndex(root / "index.sqlite"), lambda: current)
        kb = imports.knowledge.create("切片预算与解析复用本地对照")
        report["kb_id"] = kb.id
        retrieval = RetrievalService(imports)

        def trial(name, tokens):
            event = dict(name=name, tokens=tokens, model=current.identity, files=[], cases=[])
            report["trials"].append(event)
            if tokens > current.identity["max_tokens"]:
                event.update(status="unsupported", reason="超过该实际Embedding模型的已核验输入上限，未截断或伪造1024结果")
                save()
                return
            for number, path in enumerate(files):
                started = perf_counter()
                item = dict(filename=path.name)
                event["files"].append(item)
                job = None
                try:
                    job, _ = imports.enqueue(kb.id, path.name, path.read_bytes(),
                        request_key=f"{name}:{number}", source_key=f"{name}/{path.name}", tokens=tokens)
                    item["job_id"] = job.id
                    preview = imports.parse(kb.id, job.id)
                    item["chunks"] = len(preview.preview["chunks"])
                    ready = imports.confirm(kb.id, job.id, preview.preview["digest"])
                    document = next(d for d in imports.knowledge.documents(kb.id) if d.source_key == job.source_key)
                    item.update(status=ready.status, document_id=document.id, version_id=ready.version_id,
                        content_hash=preview.preview["content_hash"], chunk_config=preview.preview["config"])
                    # 每个实际编码输入均在预算内，重叠只影响正文范围，不篡改原文。
                    from sqlalchemy import select
                    from briefly.knowledge.models import KnowledgeChunk
                    with imports.knowledge._session() as session:
                        rows = list(session.scalars(select(KnowledgeChunk).where(KnowledgeChunk.version_id == ready.version_id)))
                    material = imports.store.get(preview.material_id)
                    assert all(c.content == material.content[c.start:c.end] for c in rows)
                    lengths = [current.token_length(c.embedding_text) for c in rows]
                    assert max(lengths) <= tokens
                    item.update(max_encoded_tokens=max(lengths), characters=material.char_count)
                except Exception as exc:
                    item.update(status=imports.get(kb.id, job.id).status if job else "rejected",
                                error_type=type(exc).__name__, error=str(exc)[:700])
                if job:
                    item["processing"] = imports.processing(kb.id, job.id)
                item["seconds"] = round(perf_counter() - started, 4)
                save()
                print(json.dumps(dict(trial=name,file=path.name,status=item["status"],
                    cache_hit=item.get("processing",{}).get("cache_hit"),seconds=item["seconds"]),ensure_ascii=False),flush=True)
            selected = [e["document_id"] for e in event["files"] if e["status"] == "ready"]
            for case in cases:
                expected = next(e for e in event["files"] if case["source_hint"] in e["filename"])
                row = dict(question=case["question"], source=expected["filename"], result="source_not_indexed")
                if expected["status"] == "ready":
                    source = imports.store.get(imports.get(kb.id, expected["job_id"]).material_id)
                    if case["quote"] not in source.content:
                        raise ValueError("事实金标不在解析原文，不能计通过")
                    found = retrieval.search(kb.id, case["question"], document_ids=selected, include_document_context=True)
                    hits = [h for h in found["evidence"] if h["document_id"] == expected["document_id"]]
                    payload_tokens = current.token_length(evidence_payload(case["question"], found["evidence"]))
                    assert payload_tokens <= PARAMETERS["context_tokens"]
                    assert found["diagnostics"]["context_tokens"] == payload_tokens
                    row.update(result="covered" if covered(source.content,case["quote"],hits) else "missing_evidence",
                        context_tokens=payload_tokens, candidates=len(found["evidence"]),
                        source_rank=next((i for i,h in enumerate(found["evidence"],1) if h["document_id"]==expected["document_id"]),None),
                        references=[h["reference"] for h in found["evidence"]],
                        constraints=sorted({k for h in found["evidence"] for k in h["constraints"]}))
                event["cases"].append(row)
            event.update(status="complete", ready=len(selected),
                covered=sum(c["result"]=="covered" for c in event["cases"]),
                cache_hits=sum(f.get("processing",{}).get("cache_hit",False) for f in event["files"]))
            save()
            print(json.dumps({k:event[k] for k in ("name","ready","covered","cache_hits")},ensure_ascii=False),flush=True)

        trial("bge-512-measured",512)
        trial("bge-512-reused",512)
        trial("bge-256",256)
        trial("bge-1024",1024)
        tick = perf_counter()
        current = create_local_embeddings(args.jina_dir,max_input_tokens=1024)
        report["model_load_seconds"]["jina"] = round(perf_counter()-tick,4)
        for tokens in (256,512,1024):
            trial(f"jina-{tokens}",tokens)
        trials = [t for t in report["trials"] if t["status"] == "complete"]
        common = set.intersection(*[{f["filename"] for f in t["files"] if f["status"]=="ready"} for t in trials])
        report["common_ready_files"] = sorted(common)
        for t in trials:
            t["common_ready_covered"] = sum(c["result"]=="covered" for c in t["cases"] if c["source"] in common)
        for model,folder in (("bge",args.bge_dir),("jina",args.jina_dir)):
            current = create_local_embeddings(folder,**({"max_input_tokens":1024} if model=="jina" else {}))
            for t in trials:
                if t["name"].startswith(model):
                    compare_fixed_budget(imports,retrieval,kb.id,t,cases,current)
        # 相同预算首次与复用的正文一致；不将跨预算/跨模型差异归因于缓存。
        initial,reused=report["trials"][:2]
        report["cache_same_budget_content_equal"] = all(a.get("content_hash")==b.get("content_hash")
            and a.get("chunk_config")==b.get("chunk_config") for a,b in zip(initial["files"],reused["files"]))
        save()
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
