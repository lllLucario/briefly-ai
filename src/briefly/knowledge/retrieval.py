"""正式有范围的混合召回；命中仅是候选依据，不是可答性或答案准确率。"""
from collections import defaultdict
from time import perf_counter
from uuid import uuid4

from .models import IndexGeneration, KnowledgeVersion
from .payload import evidence_payload
from .service import KnowledgeConflict, KnowledgeNotFound

PARAMETERS = dict(version=1, per_route=20, rrf_k=60, top_k=5, context_tokens=4000,
                  threshold=None, adjacent_expansion=False)


class RetrievalService:
    def __init__(self, imports):
        self.imports = imports

    def search(self, kb_id, question, *, document_ids=None, include_document_context=False,
               context_budget_only=False):
        if not question.strip() or len(question) > 4000:
            raise ValueError("问题须为1～4000字")
        started = perf_counter()
        svc = self.imports.knowledge
        # 范围必须明确；空文档选择不回退全库，也不需要加载模型。
        metadata = svc.scope([kb_id])
        if document_ids is not None:
            with svc._session() as s:
                for doc_id in set(document_ids):
                    svc._document(s, kb_id, doc_id)
            selected = set(document_ids)
            metadata["versions"] = [v for v in metadata["versions"] if v["document_id"] in selected]
            metadata["unavailable"] = [v for v in metadata["unavailable"] if v["document_id"] in selected]
        result = dict(request_id=uuid4().hex, parameters=PARAMETERS.copy(), evidence=[],
                      unavailable=metadata["unavailable"], diagnostics={})
        if include_document_context:
            result["parameters"].update(version=2, document_context="最多2份已命中文档的首片段")
        if context_budget_only:
            result["parameters"].update(top_k=None, selection="top5-and-intros-then-fill-complete-chunks-within-budget")
        if not metadata["versions"]:
            result["status"] = "not_ready" if metadata["unavailable"] else "empty_scope" if document_ids == [] else "empty_library"
            return result
        encoder = self.imports.encoder_factory()
        from .service import fingerprint
        expected = fingerprint(encoder.identity)
        versions = metadata["versions"]
        with svc._session() as s:
            for item in versions:
                if item["embedding_fingerprint"] != expected:
                    raise KnowledgeConflict("范围内存在不兼容的Embedding代次，请重建索引")
                version = s.get(KnowledgeVersion, item["version_id"])
                generation = s.get(IndexGeneration, item["generation_id"])
                self.imports.lifecycle.index.verify(generation, self.imports.lifecycle.rows(s, version))
        vector = encoder.embed_query(question)
        dense, lexical = self.imports.lifecycle.index.search(question, vector, encoder.identity["dimension"],
            [v["generation_id"] for v in versions], limit=PARAMETERS["per_route"])
        scores, routes = defaultdict(float), defaultdict(list)
        for route, ranking in (("semantic", dense), ("lexical", lexical)):
            for rank, (generation, number, raw_score) in enumerate(ranking, 1):
                key = (generation, number)
                scores[key] += 1 / (PARAMETERS["rrf_k"] + rank)
                routes[key].append(dict(route=route, rank=rank, raw_score=raw_score))
        by_generation = {v["generation_id"]: v for v in versions}
        budget = encoder.token_length(evidence_payload(question, []))
        omitted = 0
        for (generation, number), score in sorted(scores.items(), key=lambda row: (-row[1], row[0])):
            item = by_generation[generation]
            source = svc.chunk(kb_id, item["document_id"], item["version_id"], number)
            if source["status"] != "current" or source["content"] is None:
                raise KnowledgeConflict("来源在检索期间已变化，请重新检索")
            tokens = encoder.token_length(evidence_payload(question, [*result["evidence"], source]))
            if tokens > PARAMETERS["context_tokens"]:
                omitted += 1
                continue
            budget = tokens
            result["evidence"].append(dict(**source, generation_id=generation,
                retrieval=dict(rrf_score=score, routes=routes[(generation, number)])))
            if len(result["evidence"]) >= PARAMETERS["top_k"]:
                break
        context_added = 0
        if include_document_context:
            selected = {(e["generation_id"], e["chunk"]) for e in result["evidence"]}
            docs = list(dict.fromkeys(e["document_id"] for e in result["evidence"]))[:2]
            for doc_id in docs:
                item = next(v for v in versions if v["document_id"] == doc_id)
                with svc._session() as s:
                    version = s.get(KnowledgeVersion, item["version_id"])
                    first = version.chunk_manifest[0]["index"]
                if (item["generation_id"], first) in selected:
                    continue
                source = svc.chunk(kb_id, doc_id, item["version_id"], first)
                if source["status"] != "current" or source["content"] is None:
                    raise KnowledgeConflict("文档上下文在检索期间已变化，请重新检索")
                tokens = encoder.token_length(evidence_payload(question, [*result["evidence"], source]))
                if tokens > PARAMETERS["context_tokens"]:
                    omitted += 1
                    continue
                budget = tokens
                context_added += 1
                result["evidence"].append(dict(**source, generation_id=item["generation_id"],
                    retrieval=dict(rrf_score=None, routes=[dict(route="document_context", rule="first_source_chunk")])) )
        if context_budget_only:
            # 先保留同样的Top5与首片段规则，再给小块补足剩余额度；不能挤掉适用范围。
            selected = {(e["generation_id"], e["chunk"]) for e in result["evidence"]}
            for (generation, number), score in sorted(scores.items(), key=lambda row: (-row[1], row[0])):
                if (generation, number) in selected:
                    continue
                item = by_generation[generation]
                source = svc.chunk(kb_id, item["document_id"], item["version_id"], number)
                if source["status"] != "current" or source["content"] is None:
                    raise KnowledgeConflict("来源在预算装填期间已变化，请重新检索")
                tokens = encoder.token_length(evidence_payload(question, [*result["evidence"], source]))
                if tokens > PARAMETERS["context_tokens"]:
                    omitted += 1
                    continue
                budget = tokens
                selected.add((generation, number))
                result["evidence"].append(dict(**source, generation_id=generation,
                    retrieval=dict(rrf_score=score, routes=routes[(generation, number)])))
        # 发布、删除或来源变化不能悄悄混进同一请求的索引快照。
        latest = svc.scope([kb_id])
        current = {(v["document_id"], v["generation_id"]) for v in latest["versions"]}
        if any((v["document_id"], v["generation_id"]) not in current for v in versions):
            raise KnowledgeConflict("资料在检索期间已更新或移除，请重新检索")
        result["status"] = "partial" if result["unavailable"] else "candidates" if result["evidence"] else "no_candidates"
        result["diagnostics"] = dict(semantic_candidates=len(dense), lexical_candidates=len(lexical),
            deduplicated_candidates=len(scores), selected=len(result["evidence"]),
            context_tokens=budget, omitted_for_budget=omitted, seconds=round(perf_counter() - started, 4),
            document_context_added=context_added,
            answerability="not_assessed", context_accounting="question-and-serialized-evidence/v1")
        return result
