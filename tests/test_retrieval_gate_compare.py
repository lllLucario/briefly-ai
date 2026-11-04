"""检索门禁第二层的比较命令（tests/eval/retrieval_gate.py）：不需要模型和索引，只比较两份结果文件。"""
import copy
import json

import pytest

from tests.eval.retrieval_gate import EXIT_INCOMPARABLE, EXIT_REGRESSION, compare, exit_code, main, render


def entry(rank, name, gold=False):
    return dict(rank=rank, content_sha256=name, gold_hit=gold)


def case(key, names, *, gold_rank=1, hit=True, bundle=True):
    return dict(id=key, question=f"问题{key}", top5=[entry(i, n, i == gold_rank) for i, n in enumerate(names, 1)],
                hit_top5=hit, hit_bundle=bundle, first_gold_rank=gold_rank if hit else None)


def result(cases):
    gold = [c for c in cases if c["hit_top5"] is not None]
    meta = dict(model=dict(fingerprint="m"), corpus=dict(sha256="c"), cases_sha256="q", parameters=dict(top_k=5),
                context_tokens=4000, code=dict(commit="x"), versions=dict(python="3.14"), network_attempts=[])
    return dict(meta=meta, cases=cases, summary=dict(
        hit_top5_rate=sum(c["hit_top5"] for c in gold) / len(gold),
        hit_bundle_rate=sum(c["hit_bundle"] for c in gold) / len(gold)))


BASE = [case("A", list("abcde")), case("B", list("fghij"), gold_rank=2), case("C", list("klmno"), gold_rank=None, hit=None, bundle=None)]


def test_identical_results_pass():
    report = compare(result(BASE), result(copy.deepcopy(BASE)))
    assert exit_code(report) == 0 and report["worsened"] == [] and report["low_overlap"] == []
    assert report["mean_top5_overlap"] == 1 and report["identical_top5"] == 3


def test_reordering_within_top5_keeps_overlap_but_rank_drop_is_worse():
    new = [case("A", list("bacde"), gold_rank=2), *copy.deepcopy(BASE[1:])]
    report = compare(result(BASE), result(new))
    assert report["overlap_by_case"]["A"] == 1
    assert [item["id"] for item in report["worsened"]] == ["A"] and "名次下降 1 → 2" in report["worsened"][0]["reasons"][0]
    assert exit_code(report) == EXIT_REGRESSION
    assert "变差的题：1" in render(report)


def test_lost_evidence_and_low_overlap_are_listed_separately():
    new = [case("A", list("avwxy"), gold_rank=None, hit=False, bundle=True),
           case("B", list("fgvwx"), gold_rank=2),
           copy.deepcopy(BASE[2])]
    report = compare(result(BASE), result(new))
    assert report["worsened"][0]["id"] == "A" and report["worsened"][0]["reasons"] == ["Top5 依据丢失"]
    assert report["overlap_by_case"]["B"] == 0.4
    assert {item["id"] for item in report["low_overlap"]} == {"A", "B"}
    assert report["hit_top5_rate"]["new"] < report["hit_top5_rate"]["base"]


def test_bundle_loss_counts_even_when_top5_hit_remains():
    new = [case("A", list("abcde"), bundle=False), *copy.deepcopy(BASE[1:])]
    assert compare(result(BASE), result(new))["worsened"][0]["reasons"] == ["依据包依据丢失"]


def test_improvement_is_reported_not_treated_as_regression():
    base = [case("A", list("vwxyz"), gold_rank=None, hit=False), *copy.deepcopy(BASE[1:])]
    report = compare(result(base), result(BASE))
    assert report["improved"] == ["A"] and exit_code(report) == 0


@pytest.mark.parametrize("change", [
    lambda m: m["model"].update(fingerprint="other"),
    lambda m: m["corpus"].update(sha256="other"),
    lambda m: m.update(cases_sha256="other"),
    lambda m: m["parameters"].update(top_k=3),
])
def test_different_model_corpus_cases_or_parameters_are_not_comparable(change):
    new = result(copy.deepcopy(BASE))
    change(new["meta"])
    report = compare(result(BASE), new)
    assert report["incomparable"] and exit_code(report) == EXIT_INCOMPARABLE
    assert "不可比" in render(report)


def test_command_prints_report_and_returns_exit_code(tmp_path, capsys):
    base, new, out = tmp_path / "base.json", tmp_path / "new.json", tmp_path / "report.json"
    base.write_text(json.dumps(result(BASE)), encoding="utf-8")
    new.write_text(json.dumps(result([case("A", list("bacde"), gold_rank=2), *copy.deepcopy(BASE[1:])])), encoding="utf-8")
    assert main(["compare", str(base), str(new), "--output", str(out)]) == EXIT_REGRESSION
    assert "变差的题：1" in capsys.readouterr().out
    assert json.loads(out.read_text(encoding="utf-8"))["worsened"][0]["id"] == "A"
    assert main(["compare", str(base), str(base)]) == 0
