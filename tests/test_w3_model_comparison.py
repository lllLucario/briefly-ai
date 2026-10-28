"""家族分组与验证冻结清单，不调用真实模型。"""
import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def runner(monkeypatch):
    pytest.importorskip("psutil")
    monkeypatch.syspath_prepend(str(Path(__file__).parent/"eval"))
    return importlib.import_module("w3_model_comparison")


def write(path,data):
    path.write_text(json.dumps(data),encoding="utf-8")


def test_dev_does_not_open_validation_questions(runner,tmp_path):
    write(tmp_path/"documents.json",dict(documents=[dict(id="a")]))
    write(tmp_path/"family-split.json",dict(dev=["a"],validation=["b"]))
    write(tmp_path/"cases-dev.json",dict(cases=[dict(id="q",split="dev",family="a")]))
    # 验证文件故意不存在；开发运行不依赖或打开其问题标签。
    assert runner.corpus_dataset(tmp_path,"dev")["cases"][0]["id"]=="q"


def test_family_overlap_and_case_misassignment_rejected(runner,tmp_path):
    write(tmp_path/"documents.json",dict(documents=[]))
    write(tmp_path/"family-split.json",dict(dev=["a"],validation=["a"]))
    with pytest.raises(ValueError,match="家族"):
        runner.corpus_dataset(tmp_path,"dev")
    write(tmp_path/"family-split.json",dict(dev=["a"],validation=["b"]))
    write(tmp_path/"cases-dev.json",dict(cases=[dict(id="q",split="dev",family="b")]))
    with pytest.raises(ValueError,match="题目分组"):
        runner.corpus_dataset(tmp_path,"dev")


def test_missing_or_changed_freeze_cannot_validate(runner,monkeypatch,tmp_path):
    monkeypatch.setattr(runner,"ROOT",tmp_path)
    corpus=tmp_path/"corpus"
    corpus.mkdir()
    for name in runner.INPUT_NAMES:
        (corpus/name).write_text(name,encoding="utf-8")
    for relative in runner.SOURCE_PATHS:
        path=tmp_path/relative
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(relative,encoding="utf-8")
    decision=dict(inputs_sha256={name:runner.digest(corpus/name) for name in runner.INPUT_NAMES},
                  source_sha256={relative:runner.digest(tmp_path/relative) for relative in runner.SOURCE_PATHS},
                  selected_profile="bge-common256",split_used_for_selection="dev")
    prepared=tmp_path/".tmp/w3-models/bge-small-zh-v1.5/prepared.json"
    prepared.parent.mkdir(parents=True)
    prepared.write_text("fake prepared identity",encoding="utf-8")
    decision.update(model_prepared_sha256=runner.digest(prepared),context_char_budget=2000,
                    analysis_sha256=decision["source_sha256"]["tests/eval/w3_model_results.py"])
    assert runner.validate_decision(corpus,decision)[0]["name"]=="bge-common256"
    (corpus/"cases-validation.json").write_text("changed",encoding="utf-8")
    with pytest.raises(ValueError,match="冻结选择"):
        runner.validate_decision(corpus,decision)
    decision["inputs_sha256"]={}
    with pytest.raises(ValueError,match="缺少必要"):
        runner.validate_decision(corpus,decision)
