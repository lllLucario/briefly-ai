"""手动运行：用户指定目录的真实资料，仅本地解析、索引及检索，不调用生成模型。

正文留在隔离输出目录；报告仅保存计数、质量标记、引用及有界检索摘录。
此为探索性测试，文件名推测的期望来源不构成答案金标。
"""
import argparse
import hashlib
import json
from pathlib import Path
from time import perf_counter

from briefly.db import create_db_engine
from briefly.knowledge.index import LocalIndex
from briefly.knowledge.ingestion import ImportService
from briefly.knowledge.qa import KnowledgeQA
from briefly.knowledge.retrieval import RetrievalService
from briefly.llm import create_local_embeddings
from briefly.loaders import LoadedDocument
from briefly.materials import Material
from sqlalchemy import select


def audit_existing(root, encoder, cases_path):
    """复用已建索引；有界人工金标只保存在用户指定的本地JSON。"""
    from w3_production_retrieval import covered
    from briefly.knowledge.slicing import _tables
    from briefly.knowledge.evidence import constraints
    report = json.loads((root / "report.json").read_text(encoding="utf-8"))
    engine = create_db_engine("sqlite:///" + (root / "app.db").as_posix())
    audit = dict(facts=[], tables=[], generation_model_calls=0,
                 note="读过有限摘录后编制的探索题，不是独立留出；只测原文依据覆盖，不评答案准确率")
    try:
        imports = ImportService(engine, root, LocalIndex(root / "index.sqlite"), lambda: encoder)
        retrieval = RetrievalService(imports)
        for item in report["files"]:
            with imports.knowledge._session() as session:
                material = session.scalar(select(Material).where(Material.title == item["filename"]))
            if material is None:
                continue
            tables = _tables(material.content)
            costs = [encoder.token_length(material.title + "\n" + header + row[2])
                     for _, _, _, header, rows in tables for row in rows[2:]]
            audit["tables"].append(dict(filename=item["filename"], characters=material.char_count,
                recognized_markdown_tables=len(tables), table_body_rows=len(costs),
                max_title_header_row_tokens=max(costs, default=0),
                rows_over_256=sum(n > 256 for n in costs), rows_over_512=sum(n > 512 for n in costs),
                constraints=constraints(material.parse_report, 0, material.char_count)))
        for case in json.loads(cases_path.read_text(encoding="utf-8")):
            source = next(e for e in report["files"] if case["source_hint"] in e["filename"])
            event = dict(question=case["question"], source=source["filename"], expected_quote=case["quote"],
                         source_status=source["status"])
            if source["status"] != "ready":
                event["result"] = "source_not_indexed"
                audit["facts"].append(event)
                continue
            material = imports.store.get(source["material_id"])
            if case["quote"] not in material.content:
                raise ValueError("金标摘录不在解析原文，不能计通过")
            result = retrieval.search(report["kb_id"], case["question"], include_document_context=True)
            expected_hits = [e for e in result["evidence"] if e["document_id"] == source["document_id"]]
            event.update(result="covered" if covered(material.content, case["quote"], expected_hits) else "missing_evidence",
                         source_rank=next((i for i,e in enumerate(result["evidence"],1)
                                           if e["document_id"] == source["document_id"]),None),
                         evidence=[dict(title=e["source"]["title"],reference=e["reference"],start=e["start"],end=e["end"])
                                   for e in result["evidence"]])
            audit["facts"].append(event)
    finally:
        engine.dispose()
    (root / "fact-check.json").write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"facts":[dict(question=e["question"], result=e["result"], source_rank=e.get("source_rank"))
                                for e in audit["facts"]],"tables":audit["tables"]},ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--documents", type=Path, required=True)
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tokens", type=int, default=256)
    parser.add_argument("--cached-from", type=Path, help="复用一次已完成实验的解析材料，仍重新切片与建索引")
    parser.add_argument("--audit-existing", action="store_true")
    parser.add_argument("--cases", type=Path)
    args = parser.parse_args()
    files = sorted(p for p in args.documents.resolve().rglob("*") if p.is_file())
    if not 1 <= len(files) <= 16:
        parser.error("显式目录须有1～16份文件，请先筛选")
    root = args.output.resolve()
    encoder = create_local_embeddings(args.model_dir)
    if args.audit_existing:
        if not args.cases:
            parser.error("审查已有实验须指定本地金标--cases")
        audit_existing(root, encoder, args.cases)
        return
    root.mkdir(parents=True, exist_ok=False)
    report = dict(scope=str(args.documents.resolve()), generation_model_calls=0, tokens=args.tokens,
                  model=encoder.identity, files=[], retrieval=[], checks={})
    cached = {}
    if args.cached_from:
        previous = json.loads((args.cached_from / "report.json").read_text(encoding="utf-8"))
        previous_engine = create_db_engine("sqlite:///" + (args.cached_from.resolve() / "app.db").as_posix())
        try:
            previous_imports = ImportService(previous_engine, args.cached_from, LocalIndex(args.cached_from / "index.sqlite"), lambda: encoder)
            for item in previous["files"]:
                with previous_imports.knowledge._session() as session:
                    material = session.scalar(select(Material).where(Material.title == item["filename"]))
                if material:
                    # 当前缓存模式限定Word；loaders的DOC/DOCX出口均明确markdown=True。
                    if Path(item["filename"]).suffix.lower() not in {".doc", ".docx"}:
                        raise ValueError("缓存模式只复用已确认Markdown出口的Word材料")
                    cached[item["filename"]] = (item["sha256"], LoadedDocument(material.content,
                        True, material.parse_report))
            report["cached_from"] = str(args.cached_from.resolve())
        finally:
            previous_engine.dispose()

    def save():
        (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    engine = create_db_engine("sqlite:///" + (root / "app.db").as_posix())
    try:
        imports = ImportService(engine, root, LocalIndex(root / "index.sqlite"), lambda: encoder)
        if args.cached_from:
            def cached_parser(filename, data):
                digest, document = cached[filename]
                if hashlib.sha256(data).hexdigest() != digest:
                    raise ValueError("原件与缓存实验不一致，禁止复用")
                return document
            imports.parser = cached_parser
        kb = imports.knowledge.create("用户手选真实材料本地实验")
        report["kb_id"] = kb.id
        for number, path in enumerate(files, 1):
            started = perf_counter()
            event = dict(filename=path.name, relative_path=path.relative_to(args.documents.resolve()).as_posix(),
                         bytes=path.stat().st_size)
            report["files"].append(event)
            job = None
            try:
                data = path.read_bytes()
                event["sha256"] = hashlib.sha256(data).hexdigest()
                job, _ = imports.enqueue(kb.id, path.name, data, request_key=f"selected:{number}",
                                         source_key=event["relative_path"], tokens=args.tokens)
                event["job_id"] = job.id
                tick = perf_counter()
                preview = imports.parse(kb.id, job.id)
                event["parse_and_slice_seconds"] = round(perf_counter() - tick, 3)
                material = imports.store.get(preview.material_id)
                event.update(material_id=material.id, characters=material.char_count,
                             non_whitespace_characters=sum(not c.isspace() for c in material.content),
                             parse_report=material.parse_report, chunks=len(preview.preview["chunks"]),
                             content_hash=material.content_hash, markdown=preview.preview["markdown"])
                # 验证全部预览块的来源偏移/哈希；只输出计数，不展开全文。
                chunks = preview.preview["chunks"]
                assert all(hashlib.sha256(material.content[c["start"]:c["end"]].encode()).hexdigest()
                           == c["sha256"] for c in chunks)
                event["all_preview_hashes_valid"] = True
                tick = perf_counter()
                ready = imports.confirm(kb.id, job.id, preview.preview["digest"])
                event["index_seconds"] = round(perf_counter() - tick, 3)
                event.update(status=ready.status, version_id=ready.version_id)
                document = next(d for d in imports.knowledge.documents(kb.id) if d.source_key == job.source_key)
                event["document_id"] = document.id
                first = imports.knowledge.chunk(kb.id, document.id, ready.version_id, chunks[0]["index"])
                event["constraints"] = first["constraints"]
                event["confirm_replay_status"] = imports.confirm(kb.id, job.id, preview.preview["digest"]).status
            except Exception as exc:
                event.update(status=imports.get(kb.id, job.id).status if job else "rejected",
                             error_type=type(exc).__name__, error=str(exc)[:1000])
                if job:
                    current = imports.get(kb.id, job.id)
                    event["error_code"] = current.error_code
                    with imports.knowledge._session() as session:
                        material = session.scalar(select(Material).where(Material.title == path.name))
                    if material:
                        event.update(material_id=material.id, characters=material.char_count,
                                     parse_report=material.parse_report)
            event["seconds"] = round(perf_counter() - started, 3)
            save()
            print(json.dumps({k: event[k] for k in ("filename", "status", "characters", "chunks", "seconds", "error")
                              if k in event}, ensure_ascii=False), flush=True)

        retrieval = RetrievalService(imports)

        def forbidden():
            report["generation_model_calls"] += 1
            raise AssertionError("真实材料本轮禁止外发生成")

        qa = KnowledgeQA(retrieval, forbidden)
        cases = [
            ("绩效考核", "绩效考核如何组织实施和评定？"),
            ("内部讲师", "内部讲师如何选拔、培养和考核？"),
            ("年度销售", "年度销售激励有哪些奖励条件？"),
            ("薪资待遇", "销售人员的工资与提成如何计算和发放？"),
            ("信息分类", "信息如何分类和确定密级？"),
            ("恶意软件", "发现恶意软件感染后如何处理？"),
            ("质量手册", "质量管理体系的范围和管理职责是什么？"),
            ("信息处理设备", "信息处理设备如何维护和处置？"),
        ]
        for keyword, question in cases:
            expected = [e for e in report["files"] if keyword in e["filename"] and e.get("status") == "ready"]
            response = retrieval.search(kb.id, question, include_document_context=True)
            hits = response["evidence"]
            event = dict(question=question, expected_filename_hint=keyword, status=response["status"],
                         diagnostics=response["diagnostics"],
                         filename_hint_hit=any(h["document_id"] == e["document_id"] for e in expected for h in hits)
                         if expected else None,
                         hits=[dict(title=h["source"]["title"], reference=h["reference"],
                                    document_id=h["document_id"], start=h["start"], end=h["end"],
                                    constraints=h["constraints"], excerpt=h["content"][:140]) for h in hits])
            if expected:
                doc_id = expected[0]["document_id"]
                scoped = retrieval.search(kb.id, question, document_ids=[doc_id])
                event["explicit_scope_respected"] = all(h["document_id"] == doc_id for h in scoped["evidence"])
                event["scoped_candidates"] = len(scoped["evidence"])
            answer = qa.answer(kb.id, question)
            event["qa_without_external_consent"] = dict(status=answer["status"], model_called=answer["model_called"])
            report["retrieval"].append(event)
            save()
        empty = retrieval.search(kb.id, "销售提成", document_ids=[])
        unknown = retrieval.search(kb.id, "月球基地的氧气采购预算是多少？")
        report["checks"].update(empty_scope_status=empty["status"], empty_scope_candidates=len(empty["evidence"]),
            unanswerable_probe_candidates=len(unknown["evidence"]),
            note="无答案问题也可能有近邻；未运行模型或评定答案正确率",
            scope_before_restart=imports.lifecycle.validate_scope([kb.id], encoder)["status"])
        assert empty["status"] == "empty_scope" and not empty["evidence"]
        assert report["generation_model_calls"] == 0
        save()
    finally:
        engine.dispose()
    engine = create_db_engine("sqlite:///" + (root / "app.db").as_posix())
    try:
        imports = ImportService(engine, root, LocalIndex(root / "index.sqlite"), lambda: encoder)
        report["checks"]["scope_after_restart"] = imports.lifecycle.validate_scope([kb.id], encoder)["status"]
        report["checks"]["jobs_after_restart"] = len(imports.jobs(kb.id))
        save()
    finally:
        engine.dispose()
    print(json.dumps(report["checks"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
