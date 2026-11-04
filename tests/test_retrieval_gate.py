"""检索门禁第一层（进 CI，不需要模型）：虚构语料＋确定性假编码器，走真实的 LocalIndex、RetrievalService 和 RRF。

W4-10 换数据库时靠这里和 tests/eval/retrieval_gate.py 判断检索有没有退步，所以这里断言的是行为，不是实现：
范围隔离、代次隔离、三种命中路线的 Top 结果、RRF 融合、中文二元组、表格行标识排序、相关度下限。
语料和同义词表在 tests/fixtures/retrieval/corpus.json。
"""
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

# 门禁不能在 CI 里被静默跳过：缺 sqlite_vec 时 CI 必须失败，本地只装 .[dev] 时才跳过
if not os.getenv("CI"):
    pytest.importorskip("sqlite_vec", reason="检索门禁需安装.[rag]")

from briefly import config, relevance_floor as floors  # noqa: E402
from briefly.db import create_db_engine  # noqa: E402
from briefly.knowledge.assistant import KnowledgeAssistant  # noqa: E402
from briefly.knowledge.index import LocalIndex, terms  # noqa: E402
from briefly.knowledge.lifecycle import IndexLifecycle  # noqa: E402
from briefly.knowledge.retrieval import (  # noqa: E402
    PARAMETERS, RetrievalService, both_routes, relevant, row_label_match, similarity,
)
from briefly.knowledge.service import KnowledgeConflict, KnowledgeService, fingerprint  # noqa: E402
from briefly.knowledge.slicing import Slice  # noqa: E402
from briefly.materials import MaterialStore  # noqa: E402

CORPUS = json.loads((Path(__file__).parent / "fixtures" / "retrieval" / "corpus.json").read_text(encoding="utf-8"))
CHUNK_CONFIG = dict(strategy="title-token-offsets", version=6, parameters=dict(tokens=512))


class ConceptEncoder:
    """确定性假编码器：同义词表里的每个概念占一个轴，文本含该概念就在轴上记 1；另有一个小的公共轴保证向量非零。
    所以“酒店”和“宾馆”语义相同、却没有共同的词元，能单独验证向量路。"""
    axes = list(CORPUS["concepts"])
    identity = dict(model="fake/concept", revision="gate-v1", dimension=len(axes) + 1, max_tokens=512,
                    artifact_sha256={"model.onnx": "a" * 64, "tokenizer.json": "b" * 64},
                    pooling="CLS", query_prefix="", normalize=True)

    def _vector(self, text):
        hits = [float(any(word in text for word in CORPUS["concepts"][axis])) for axis in self.axes]
        return [*hits, 0.1]

    def embed_documents(self, texts):
        return [self._vector(text) for text in texts]

    def embed_query(self, text):
        return self._vector(text)

    @staticmethod
    def token_length(text):
        return len(text)


class Library:
    """建好的三个知识库，以及“(文档, 片段号) → 语料里的片段编号”的对照。"""

    def __init__(self, engine, index, store, svc, lifecycle):
        self.engine, self.index, self.store, self.svc, self.lifecycle = engine, index, store, svc, lifecycle
        self.retrieval = RetrievalService(SimpleNamespace(knowledge=svc, lifecycle=lifecycle, encoder_factory=ConceptEncoder))
        self.passages = {}
        self.documents = {}

    def add(self, kb_id, key, title, passages, *, publish=True):
        text = "\n\n".join(p["text"] for p in passages)
        material, _ = self.store.add_text(title, text)
        slices, position = [], 0
        for number, passage in enumerate(passages, 1):
            start = text.index(passage["text"], position)
            position = start + len(passage["text"])
            slices.append(Slice(number, passage["section"], start, position, passage["text"],
                                f"{passage['section']}\n{passage['text']}"))
        doc, version, _ = self.svc.stage_slices(kb_id, material.id, key, CHUNK_CONFIG, slices)
        generation = self.lifecycle.build(kb_id, doc.id, version.id, ConceptEncoder(), expected_revision=1)
        self.documents[key] = doc.id
        for number, passage in enumerate(passages, 1):
            self.passages[(doc.id, number)] = passage["id"]
        return doc, generation

    def search(self, kb_id, question, **kwargs):
        return self.retrieval.search(kb_id, question, **kwargs)

    def passage(self, evidence):
        return self.passages.get((evidence["document_id"], evidence["chunk"]))

    def by_passage(self, result):
        return {self.passage(e): e for e in result["evidence"]}


def numbered(spec):
    """把语料里“编号 + 模板”写法展开成片段列表。"""
    first = spec.get("first_number", 1)
    section = spec.get("section", "{title} > 第{number}条")
    return [dict(id=f"{spec['key']}-{number}", section=section.format(number=number, title=spec["title"]),
                 text=spec["text"].format(number=number)) for number in range(first, first + spec["count"])]


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    root = tmp_path_factory.mktemp("retrieval-gate")
    engine = create_db_engine("sqlite:///" + (root / "app.db").as_posix())
    index = LocalIndex(root / "index.sqlite")
    store, svc = MaterialStore(engine), KnowledgeService(engine)
    lib = Library(engine, index, store, svc, IndexLifecycle(engine, index))
    try:
        lib.main, lib.outside, lib.tiny, lib.versioned = (svc.create(name).id for name in ("主库", "外库", "小库", "版本库"))
        for document in CORPUS["documents"]:
            lib.add(lib.main, document["key"], document["title"], document["passages"])
        filler = CORPUS["filler"]
        lib.add(lib.main, filler["key"], filler["title"], numbered(filler))
        outside = CORPUS["outside"]
        lib.add(lib.outside, outside["key"], outside["title"], numbered(outside))
        tiny = CORPUS["tiny"]
        lib.add(lib.tiny, tiny["key"], tiny["title"], [dict(id="tiny", section=tiny["section"], text=tiny["text"])])
        lib.versions = publish_two_versions(lib)
        yield lib
    finally:
        engine.dispose()


def publish_two_versions(lib):
    spec = CORPUS["versioned"]
    old_doc, old_generation = lib.add(lib.versioned, spec["key"], spec["title"],
                                      [dict(id="versioned-old", section=spec["section"], text=spec["old"])])
    material, _ = lib.store.add_text(spec["title"], spec["new"])
    slices = [Slice(1, spec["section"], 0, len(spec["new"]), spec["new"], f"{spec['section']}\n{spec['new']}")]
    doc, version, _ = lib.svc.stage_slices(lib.versioned, material.id, spec["key"], CHUNK_CONFIG, slices, expected_revision=1)
    new_generation = lib.lifecycle.build(lib.versioned, doc.id, version.id, ConceptEncoder(), expected_revision=2)
    assert doc.id == old_doc.id and new_generation != old_generation
    return SimpleNamespace(doc=doc.id, old=old_generation, new=new_generation)


def routes_of(evidence):
    return {route["route"]: route["rank"] for route in evidence["retrieval"]["routes"]}


def test_parameters_are_the_ones_the_baseline_was_recorded_with():
    assert (PARAMETERS["per_route"], PARAMETERS["rrf_k"], PARAMETERS["top_k"]) == (20, 60, 5)


@pytest.mark.parametrize("case", CORPUS["route_cases"], ids=[c["id"] for c in CORPUS["route_cases"]])
def test_top_result_and_routes(library, case):
    result = library.search(library.main, case["question"])
    target = library.by_passage(result)[case["top"]]
    # 单路第 1 名的 RRF 分都是 1/61，只有词法命中的片段可能和语义路第 1 名同分，所以比分数而不是比位置
    assert target["retrieval"]["rrf_score"] == result["evidence"][0]["retrieval"]["rrf_score"]
    assert sorted(routes_of(target)) == case["routes"]
    assert result["status"] == "candidates" and len(result["evidence"]) <= PARAMETERS["top_k"]
    if case.get("no_lexical_candidates"):
        assert result["diagnostics"]["lexical_candidates"] == 0


def test_rrf_prefers_agreement_of_both_routes_over_either_route_alone(library):
    """甲在两路都靠前但语义只排第 2，乙只在语义路第 1，丙只在词法路命中：等权 RRF 下甲第一，乙丙只拿到单路分数。"""
    result = library.search(library.main, "住宿审批编号Q77")
    hits = library.by_passage(result)
    both, semantic_only, lexical_only = hits["travel-both"], hits["travel-synonym"], hits["device-code"]
    assert library.passage(result["evidence"][0]) == "travel-both"
    assert routes_of(both)["semantic"] == 2 and "lexical" in routes_of(both)
    assert routes_of(semantic_only) == dict(semantic=1)
    assert list(routes_of(lexical_only)) == ["lexical"]
    # 分数就是各路 1/(60+名次) 之和，不混入原始相似度或 BM25 分数
    for item in result["evidence"]:
        expected = sum(1 / (PARAMETERS["rrf_k"] + rank) for rank in routes_of(item).values())
        assert item["retrieval"]["rrf_score"] == pytest.approx(expected)
    scores = [item["retrieval"]["rrf_score"] for item in result["evidence"]]
    assert scores == sorted(scores, reverse=True)


def test_chinese_lexical_matches_bigrams_not_single_characters(library):
    """多字查询只看二元组：“巡”“检”分散出现的片段不算词法命中；单字查询才匹配单字。"""
    hits = library.by_passage(library.search(library.main, "巡检"))
    assert "lexical" in routes_of(hits["device-patrol"])
    assert "device-split-chars" not in hits or "lexical" not in routes_of(hits["device-split-chars"])
    hits = library.by_passage(library.search(library.main, "巡"))
    assert "lexical" in routes_of(hits["device-patrol"]) and "lexical" in routes_of(hits["device-split-chars"])
    assert len(terms("巡检", query=True)) == 1 and len(terms("巡检")) == 3


@pytest.mark.parametrize("question", ["住宿审批", "宾馆押金", "编号Q77", "E403", "机房巡检"])
def test_library_scope_filters_both_routes_before_topk(library, question):
    """外库片段更贴合查询、数量也超过每路上限，范围内片段仍要召回，外库内容一条也不能混入。"""
    for kb_id in (library.main, library.tiny):
        result = library.search(kb_id, question)
        assert result["evidence"], "范围内有片段时，向量路总能给出候选；先截取再过滤才会变空"
        assert all(e["kb_id"] == kb_id and "999元" not in e["content"] for e in result["evidence"])
    assert all("外库干扰" in e["content"] for e in library.search(library.outside, question)["evidence"])


def test_document_scope_limits_both_routes_and_empty_scope_stays_empty(library):
    device = library.documents["device"]
    result = library.search(library.main, "住宿审批编号Q77", document_ids=[device])
    assert result["evidence"] and {e["document_id"] for e in result["evidence"]} == {device}
    assert library.search(library.main, "住宿审批", document_ids=[])["status"] == "empty_scope"


def test_only_the_published_generation_is_searched(library):
    versions = library.versions
    result = library.search(library.versioned, "车辆押金标准")
    assert [e["generation_id"] for e in result["evidence"]] == [versions.new]
    assert "650元" in result["evidence"][0]["content"]
    # 旧代次留在索引里也互不混入：两路都只返回传入范围内的代次
    encoder = ConceptEncoder()
    vector, dimension = encoder.embed_query("车辆押金标准"), encoder.identity["dimension"]
    for generation in (versions.old, versions.new):
        dense, lexical = library.index.search("车辆押金标准", vector, dimension, [generation])
        assert dense and lexical and {row[0] for row in dense + lexical} == {generation}


def test_other_embedding_identity_is_rejected_not_mixed(library):
    class OtherModel(ConceptEncoder):
        identity = {**ConceptEncoder.identity, "revision": "gate-v2"}

    service = RetrievalService(SimpleNamespace(knowledge=library.svc, lifecycle=library.lifecycle, encoder_factory=OtherModel))
    with pytest.raises(KnowledgeConflict, match="不兼容"):
        service.search(library.main, "住宿审批")


def test_table_row_label_decides_ranking_among_similar_rows(library, monkeypatch):
    """各行向量相同：没有行标识路时，语义靠前的相邻行（24 级 B 档）会压过正确行（24 级 A 档）。"""
    question = "24级A档工资"
    with monkeypatch.context() as patched:
        patched.setattr("briefly.knowledge.retrieval.row_label_match", lambda identifiers, section: False)
        control = library.search(library.main, question)
    assert library.passage(control["evidence"][0]) == "row-24b"

    result = library.search(library.main, question)
    top = result["evidence"][0]
    assert library.passage(top) == "row-24a"
    assert routes_of(top)["row_label"] == 1
    # 只有包含全部编号的行才进这一路；“24 级 B 档”只含 24，不算
    assert all("row_label" not in routes_of(e) for e in result["evidence"][1:])
    assert row_label_match({"24", "A"}, top["section"])


def test_row_label_only_reorders_existing_candidates_and_leaves_plain_questions_alone(library):
    plain = library.search(library.main, "工资")
    assert plain["evidence"] and all("row_label" not in routes_of(e) for e in plain["evidence"])
    assert plain["diagnostics"]["selected"] == len(plain["evidence"]) <= PARAMETERS["top_k"]


def top_similarity(result):
    return max(s for e in result["evidence"] if (s := similarity(e)) is not None)


def test_relevance_floor_combines_similarity_with_route_agreement(library):
    both = library.search(library.main, "宾馆押金")
    assert any(both_routes(e) for e in both["evidence"])
    assert relevant(both["evidence"], top_similarity(both) - 0.01)
    assert relevant(both["evidence"], None), "没有登记下限时不判断"

    # 相似度达到下限，但没有一条被两路同时命中：只是语义上沾边
    synonym = library.search(library.main, "酒店开销")
    assert not any(both_routes(e) for e in synonym["evidence"])
    assert not relevant(synonym["evidence"], top_similarity(synonym) - 0.01)

    # 低于下限：前 5 条全部两路一致才算相关
    salary = library.search(library.main, "工资", document_ids=[library.documents["salary"]])
    assert all(both_routes(e) for e in salary["evidence"])
    assert relevant(salary["evidence"], top_similarity(salary) + 0.01)
    weak = library.search(library.main, "巡检")
    assert not all(both_routes(e) for e in weak["evidence"])
    assert not relevant(weak["evidence"], top_similarity(weak) + 0.01)


def test_registered_floor_blocks_and_unregistered_model_warns_once(library, monkeypatch, caplog):
    """本地定位这条链路真正用到下限，不调用模型：登记了下限，语义沾边的问题不列文档；没登记时不判断、记一次日志。"""
    assistant = KnowledgeAssistant(library.retrieval, None)
    monkeypatch.setattr(config, "INTENT_KB_THRESHOLD", None)
    monkeypatch.setattr(floors, "REGISTERED", {fingerprint(ConceptEncoder.identity): 0.5})
    assert assistant.locate(library.main, "酒店开销")["status"] == "low_relevance"
    assert assistant.locate(library.main, "宾馆押金")["status"] == "located"

    monkeypatch.setattr(floors, "REGISTERED", {})
    monkeypatch.setattr("briefly.knowledge.retrieval._warned", set())
    with caplog.at_level(logging.WARNING, logger="briefly.knowledge.retrieval"):
        assert assistant.locate(library.main, "酒店开销")["status"] == "located"
        assert assistant.locate(library.main, "酒店开销")["status"] == "located"
    assert sum("没有登记相关度下限" in record.getMessage() for record in caplog.records) == 1
