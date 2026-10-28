"""验证评测计分边界，防止把错来源、摘要截断或无答案计为成功。"""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture(scope="module")
def baseline():
    path = Path(__file__).parent / "eval" / "w3_rag_baseline.py"
    spec = importlib.util.spec_from_file_location("w3_baseline_checks", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_labels_are_separate_and_valid(baseline):
    corpus, spec = baseline.load_fixtures()
    assert len(corpus["documents"]) == 11
    assert len(spec["cases"]) == 36
    for scenario in ("office", "project", "manual"):
        cases = [c for c in spec["cases"] if c["scenario"] == scenario]
        assert len(cases) == 10
        assert {"direct", "paraphrase", "cross", "no_answer"} <= {c["kind"] for c in cases}


def test_full_chunk_is_not_visible_snippet(baseline):
    case = {"evidence_groups": [["a"], ["b"]]}
    evidence = {"a": {"document": "d1", "quote": "上海650元"}, "b": {"document": "d2", "quote": "订房前批准"}}
    hits = [SimpleNamespace(material_id=1, chunk=1, snippet="上海650元"), SimpleNamespace(material_id=2, chunk=1, snippet="批准")]
    chunks = {(1, 1): {"content": "上海650元"}, (2, 1): {"content": "订房前批准"}}
    assert baseline.coverage(case, evidence, hits, chunks, {1: "d1", 2: "d2"})["recall"] == 1
    result = baseline.coverage(case, evidence, hits, chunks, {1: "d1", 2: "d2"}, snippets=True)
    assert result["recall"] == .5
    assert result["all_required"] is False


def test_wrong_document_cannot_support_identical_quote(baseline):
    case = {"evidence_groups": [["a"]]}
    evidence = {"a": {"document": "current", "quote": "金额650"}}
    hits = [SimpleNamespace(material_id=1, chunk=1, snippet="金额650")]
    result = baseline.coverage(case, evidence, hits, {(1, 1): {"content": "金额650"}}, {1: "obsolete"})
    assert result["recall"] == 0


def test_alternative_evidence_only_counts_once(baseline):
    case = {"evidence_groups": [["a", "b"]]}
    evidence = {"a": {"document": "d1", "quote": "甲"}, "b": {"document": "d2", "quote": "乙"}}
    hits = [SimpleNamespace(material_id=2, chunk=1, snippet="乙")]
    result = baseline.coverage(case, evidence, hits, {(2, 1): {"content": "乙"}}, {2: "d2"})
    assert result["recall"] == 1
    assert result["groups_found"] == [True]


def test_no_answer_is_unscored(baseline):
    assert baseline.coverage({"evidence_groups": []}, {}, [], {}, {}) == {
        "groups_found": [], "recall": None, "all_required": None,
    }
