"""真实生成的小批原创语料验证；只允许既有虚构基准，不接受业务原件参数。"""
import argparse
from collections import Counter
import json
from pathlib import Path
from time import perf_counter

from briefly.db import create_db_engine
from briefly.knowledge.index import LocalIndex
from briefly.knowledge.ingestion import ImportService
from briefly.knowledge.qa import KnowledgeQA
from briefly.knowledge.retrieval import RetrievalService
from briefly.llm import create_chat_model, create_local_embeddings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--corpus", required=True, help="w3_production_retrieval输出的独立原创评测数据库")
    parser.add_argument("--output", required=True)
    parser.add_argument("--allow-external-fictional", action="store_true")
    parser.add_argument("-n", type=int, default=3)
    parser.add_argument("-k", nargs="*", default=["O01", "P01", "M01", "O06", "P05", "M05", "G04", "G05"])
    args = parser.parse_args()
    if not args.allow_external_fictional:
        parser.error("需显式允许原创虚构问题及召回片段发往已配置生成API")
    output, root = Path(args.output), Path(args.corpus).resolve()
    output.mkdir(parents=True, exist_ok=False)
    fixtures = Path(__file__).parent / "fixtures" / "w3_rag"
    cases = json.loads((fixtures / "expectations.json").read_text(encoding="utf-8"))["cases"]
    documents = json.loads((fixtures / "documents.json").read_text(encoding="utf-8"))["documents"]
    encoder = create_local_embeddings(args.model_dir)
    engine = create_db_engine("sqlite:///" + (root / "app.db").as_posix())
    events = []
    try:
        imports = ImportService(engine, root, LocalIndex(root / "index.sqlite"), lambda: encoder)
        bases = imports.knowledge.list_bases()
        if len(bases) != 1 or bases[0].name != "生产路径原创基准":
            raise ValueError("不是专用原创评测数据库，拒绝外发")
        kb = bases[0]
        mapping = {doc.source_key: doc.id for doc in imports.knowledge.documents(kb.id)}
        # 核对待外发版本的原文和来源键，防止路径误指业务数据库。
        for item in documents:
            doc_id = mapping[item["id"]]
            version = imports.knowledge.versions(kb.id, doc_id)[1][-1]
            if imports.store.get(version.material_id).content != item["text"].strip():
                raise ValueError("评测资料与原创fixture不一致，拒绝外发")
        qa = KnowledgeQA(RetrievalService(imports), lambda: create_chat_model(max_retries=0, timeout=45))
        for case in [c for c in cases if c["id"] in args.k]:
            for repeat in range(args.n):
                started = perf_counter()
                event = dict(case=case["id"], repeat=repeat, expected=case["answerability"])
                try:
                    result = qa.answer(kb.id, case["question"], document_ids=[mapping.get(key, key) for key in case["scope"]], allow_external=True)
                    event.update(status=result["status"], answer=result["answer"], citations=result.get("citations", []),
                        model_called=result["model_called"], warnings=result.get("warnings", []),
                        evidence=[dict(reference=e["reference"], content=e["content"]) for e in result["evidence"]])
                except Exception as exc:
                    event.update(status="error", error=type(exc).__name__)
                event["seconds"] = round(perf_counter() - started, 3)
                events.append(event)
                (output / "results.json").write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
                print(case["id"], repeat + 1, event["status"], flush=True)
    finally:
        engine.dispose()
    summary = dict(events=len(events), states=dict(Counter(e["status"] for e in events)),
                   qa_requests_with_generation=sum(e.get("model_called", False) for e in events),
                   provider_call_count="本脚本未逐次统计共享检查链的重写调用",
                   note="状态/引用格式不是答案正确率；需人工核对主体、条件、版本和引用支持")
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
