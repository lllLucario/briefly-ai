"""选型实验的计分与范围校验；假token计数，不加载模型或真实资料。"""
import importlib
from pathlib import Path

import pytest


@pytest.fixture
def pilot(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parent / "eval"))
    return importlib.import_module("w3_chunk_pilot")


@pytest.fixture
def scale(monkeypatch):
    pytest.importorskip("numpy")
    pytest.importorskip("psutil")
    monkeypatch.syspath_prepend(str(Path(__file__).parent / "eval"))
    return importlib.import_module("w3_index_scale")


@pytest.mark.parametrize("overlap", [0, 4])
def test_chunks_keep_source_offsets_and_complete_coverage(pilot, overlap):
    text = "标题\n\n" + "甲乙丙丁戊己庚辛壬癸" * 10
    rows = pilot.chunks(text, "文档", len, budget=20, overlap=overlap)
    assert len(rows) > 1
    assert all(r["text"] == "文档\n" + text[r["start"]:r["end"]] for r in rows)
    assert all(r["tokens"] <= 20 for r in rows)
    assert all(rows[i+1]["start"] > rows[i]["start"] for i in range(len(rows)-1))
    assert pilot.covered(0, len(text), [(r["start"], r["end"]) for r in rows])
    if overlap:
        assert any(rows[i+1]["start"] < rows[i]["end"] for i in range(len(rows)-1))


def test_partial_evidence_and_wrong_document_do_not_count(pilot):
    groups = [[dict(document="a", start=0, end=10)]]
    mapping = {1:dict(document="a", start=0, end=5), 2:dict(document="b", start=5, end=10),
               3:dict(document="a", start=5, end=10)}
    assert pilot.score(groups, [(1,0), (2,0)], mapping)["recall"] == 0
    complete = pilot.score(groups, [(1,0), (3,0)], mapping)
    assert complete["all_required"] is True
    assert complete["single_chunk_recall"] == 0


def test_overlap_does_not_hide_missing_condition(pilot):
    groups = [[dict(document="a", start=0, end=10)], [dict(document="a", start=15, end=20)]]
    mapping = {1:dict(document="a", start=0, end=10), 2:dict(document="a", start=5, end=15)}
    result = pilot.score(groups, [(1,0), (2,0)], mapping)
    assert result["recall"] == .5
    assert result["all_required"] is False
    assert pilot.score([], [(1,0)], mapping)["recall"] is None


def test_scale_oracle_filters_before_ranking(scale):
    import numpy as np
    vectors = np.tile([1., 0.], (201, 1))
    vectors[200] = [0., 1.]
    expected = scale.oracle(vectors, [1., 0.], ["doc-00002"], limit=1)
    assert expected == [(201, 1.)]
    assert scale.validate_hits(expected, expected, ["doc-00002"])
    assert not scale.validate_hits([(1, 1.)], expected, ["doc-00002"])
    assert not scale.validate_hits([(201, 0.)], expected, ["doc-00002"])


def test_scale_missing_result_cannot_be_success(scale, tmp_path):
    result = scale.summarize(tmp_path, [1000], 24)
    assert result["ok"] is False
    assert all(not row["ok"] for row in result["configurations"])


def test_dataset_hash_rejects_stale_parse(pilot, monkeypatch, tmp_path):
    import json
    monkeypatch.setattr(pilot, "ROOT", tmp_path)
    (tmp_path / "source.doc").write_bytes(b"fake original")
    (tmp_path / "parsed.md").write_text("正文", encoding="utf-8")
    doc = dict(id="a", parsed_path="parsed.md", parsed_sha256="stale",
               original_path="source.doc", original_sha256=pilot.sha(tmp_path / "source.doc"))
    path = tmp_path / "dataset.json"
    path.write_text(json.dumps(dict(documents=[doc], cases=[])), encoding="utf-8")
    with pytest.raises(ValueError, match="指纹"):
        pilot.load_dataset(path)


def test_renamed_resource_folder_keeps_original_integrity_check(pilot,monkeypatch,tmp_path):
    import json
    monkeypatch.setattr(pilot,"ROOT",tmp_path)
    folder=tmp_path/"resources"
    folder.mkdir()
    source=folder/"original.doc"
    source.write_bytes(b"original bytes")
    parsed=tmp_path/"parsed.md"
    parsed.write_text("原文",encoding="utf-8")
    document=dict(id="a",original_path="resouce/original.doc",original_sha256=pilot.sha(source),
                  parsed_path="parsed.md",parsed_sha256=pilot.sha(parsed))
    dataset=tmp_path/"dataset.json"
    dataset.write_text(json.dumps(dict(documents=[document],cases=[])),encoding="utf-8")
    assert pilot.load_dataset(dataset)[1]["a"]=="原文"
    source.write_bytes(b"changed")
    with pytest.raises(ValueError,match="指纹"):
        pilot.load_dataset(dataset)
