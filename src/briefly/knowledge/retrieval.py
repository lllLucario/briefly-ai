"""正式有范围的混合召回；命中仅是候选依据，不是可答性或答案准确率。"""
import logging
import re
from collections import defaultdict
from time import perf_counter
from uuid import uuid4

from ..relevance_floor import describe, embedding_fingerprint, kb_floor
from .models import IndexGeneration, KnowledgeVersion
from .payload import evidence_payload
from .service import KnowledgeConflict, KnowledgeNotFound

log = logging.getLogger(__name__)

PARAMETERS = dict(version=1, per_route=20, rrf_k=60, top_k=5, context_tokens=4000,
                  threshold=None, adjacent_expansion=False, row_label_route="table-row-label/v1")
_warned: set[str] = set()
_ROW_LABEL = re.compile(r"第\d+行（([^）]*)）")
_IDENTIFIER = re.compile(r"[0-9A-Za-z]+")


def question_identifiers(question: str) -> set[str]:
    """问题里的数字/字母编号（“24级A档”→ {"24", "A"}），用于匹配表格行标识。"""
    return set(_IDENTIFIER.findall(question))


def row_label_match(identifiers: set[str], section: str | None) -> bool:
    """表格行片段（章节里“第N行（行标识）”）的行标识是否包含问题里的全部编号。只看括号里的行标识，不看“第N行”本身；
    按整段编号匹配，“2”不会命中“24”。"""
    label = _ROW_LABEL.search(section or "")
    if not identifiers or not label:
        return False
    found = set(_IDENTIFIER.findall(label.group(1)))
    return identifiers <= found


def relevance_floor(identity) -> float | None:
    """检索相关度下限：与意图漏斗的知识库路由同一口径，按 Embedding 指纹查登记值（relevance_floor.kb_floor），
    配置了 BRIEFLY_INTENT_KB_THRESHOLD 时用覆盖值。没有登记值时返回 None，不判断相关度，每个模型记一次日志。"""
    floor = kb_floor(identity)
    if floor is None and (key := embedding_fingerprint(identity)) not in _warned:
        _warned.add(key)
        log.warning("Embedding %s 没有登记相关度下限，知识检索不做相关度判断（库外问题也会检索并按授权外发）；"
                    "请标定后登记到 briefly.relevance_floor.REGISTERED", describe(identity))
    return floor


def similarity(item) -> float | None:
    """语义路的相似度（1 − cosine 距离）；只有词法路命中、或文档开头补充片段时为 None。"""
    sims = [1 - route["raw_score"] for route in item["retrieval"]["routes"] if route["route"] == "semantic"]
    return max(sims) if sims else None


def both_routes(item) -> bool:
    return {route["route"] for route in item["retrieval"]["routes"]} >= {"semantic", "lexical"}


def relevant(evidence, floor) -> bool:
    """一次检索的结果是否与问题相关。语义相似度单独分不开（BGE-small 在 W3-selected 上库内题最低 0.49、
    库外题最高 0.52），所以同时看语义、词法两路是否一致，只看排名前 5 的召回（不含文档开头补充片段）：
    - 最高相似度达到下限，但前 5 条没有一条被两路同时命中：只是语义上沾边（只碰上“公司”这类字眼的词法命中不算），
      按不相关处理；
    - 低于下限，但前 5 条全部被两路同时命中：两路一致，按相关处理。
    floor 为 None 或没有语义分数时不判断，按相关处理。规则是在 W3-selected 9 份 Word＋18 道题上看出来的，不是独立验证。"""
    ranked = [e for e in evidence if e["retrieval"].get("rrf_score") is not None][:PARAMETERS["top_k"]]
    sims = [s for e in ranked if (s := similarity(e)) is not None]
    if floor is None or not sims:
        return True
    if max(sims) >= floor:
        return any(both_routes(e) for e in ranked)
    return all(both_routes(e) for e in ranked)


class RetrievalService:
    def __init__(self, imports):
        self.imports = imports

    def search(self, kb_id, question, *, document_ids=None, include_document_context=False,
               context_budget_only=False, context_tokens=None):
        """context_tokens：依据包总额，默认沿用对照实验口径；正式回答传入 KNOWLEDGE_CONTEXT_TOKENS。"""
        limit = context_tokens or PARAMETERS["context_tokens"]
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
        result["parameters"]["context_tokens"] = limit
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
        fetched = {}

        def chunk(generation, number):
            if (generation, number) not in fetched:
                item = by_generation[generation]
                fetched[(generation, number)] = svc.chunk(kb_id, item["document_id"], item["version_id"], number)
            return fetched[(generation, number)]

        # 第三路“表格行标识”：各行几乎同质的数值表（薪酬等级表）语义上分不出“24级”和“3级”，只有词法能分，
        # 而 RRF 里两路都沾边的相邻行会压过只有词法命中的正确行。问题带数字/字母编号时，已召回的候选中
        # 行标识包含全部编号的表格行片段，按词法名次（其次语义名次）排成一路，同样按 1/(k+名次) 计入。
        # 只对已有候选重排，不新增候选，也不影响相关度判断（relevant 只看语义、词法两路）。
        identifiers = question_identifiers(question)
        if identifiers:
            def order(key):
                ranks = {r["route"]: r["rank"] for r in routes[key]}
                return ranks.get("lexical", PARAMETERS["per_route"] + 1), ranks.get("semantic", PARAMETERS["per_route"] + 1), key
            matched = sorted((key for key in scores if row_label_match(identifiers, chunk(*key)["section"])), key=order)
            for rank, key in enumerate(matched, 1):
                scores[key] += 1 / (PARAMETERS["rrf_k"] + rank)
                routes[key].append(dict(route="row_label", rank=rank, raw_score=None))
        budget = encoder.token_length(evidence_payload(question, []))
        omitted = 0
        for (generation, number), score in sorted(scores.items(), key=lambda row: (-row[1], row[0])):
            source = chunk(generation, number)
            if source["status"] != "current" or source["content"] is None:
                raise KnowledgeConflict("来源在检索期间已变化，请重新检索")
            tokens = encoder.token_length(evidence_payload(question, [*result["evidence"], source]))
            if tokens > limit:
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
                if tokens > limit:
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
                source = chunk(generation, number)
                if source["status"] != "current" or source["content"] is None:
                    raise KnowledgeConflict("来源在预算装填期间已变化，请重新检索")
                tokens = encoder.token_length(evidence_payload(question, [*result["evidence"], source]))
                if tokens > limit:
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
