"""第二轮脚本回归：原创小样本、假编码器和 LangChain 假聊天模型，无真实 API。"""
from argparse import Namespace
import json
from types import SimpleNamespace

import pytest

from tests.eval import w3_library_qa as qa
from tests.eval.w3_e2e_qa import attach_workspace
from test_workspace_index import BagEncoder
from fakes import fake, text_msg, tool_msg


@pytest.mark.parametrize("source,answer,refs,expected", [
    ("workspace", "11日〔依据1〕", [{"label": "依据1", "origin": "knowledge"}], False),
    ("workspace", "11日〔依据1〕", [{"label": "依据1", "origin": "workspace"}], True),
    ("workspace", "11日", [{"label": "依据1", "origin": "workspace"}], False),
    ("both", "两处〔依据1、2〕", [{"label": "依据1", "origin": "knowledge"}, {"label": "依据2", "origin": "workspace"}], True),
    ("conflict", "两边不同，以知识库正式版为准。〔依据1、2〕", [{"label": "依据1", "origin": "knowledge"}, {"label": "依据2", "origin": "workspace"}], True),
    ("conflict", "两边不同。〔依据1、2〕", [{"label": "依据1", "origin": "knowledge"}, {"label": "依据2", "origin": "workspace"}], False),
    ("none", "未提及", [], True),
])
def test_source_requires_actual_citation_and_conflict_priority(source, answer, refs, expected):
    assert qa.score_source(dict(source=source, abstain=source == "none"), answer, refs) is expected


def test_workspace_guard_checks_content_and_complete_set():
    item = dict(material_id=1, title="笔记", content="阅读计划")
    material = SimpleNamespace(id=1, title="笔记", content="阅读计划")
    store = SimpleNamespace(list_materials=lambda: [material])
    qa.validate_workspace(store, [item])
    material.content = "被改过的计划"
    with pytest.raises(SystemExit, match="拒绝外发"):
        qa.validate_workspace(store, [item])
    store.list_materials = lambda: []
    with pytest.raises(SystemExit):
        qa.validate_workspace(store, [item])


def test_format_preserves_spreadsheet_and_mixed_sources(monkeypatch):
    monkeypatch.setattr(qa, "ROUND", 2)
    of = qa.doc_format_of({"N001": "xls", "N002": "pptx"})
    assert of({"gold": [{"doc": "N001"}, {"doc": "N002"}]}) == "pptx+xls"


def test_gold_is_verbatim_in_the_named_document():
    case = dict(id="R01", gold=[dict(doc="N001", text="10 天"), dict(doc="W1", text="12天")])
    assert qa.missing_gold([case], {"N001": "10天", "W1": "12天"}) == [("R01", "N001", "10 天")]
    assert qa.missing_gold([case], {"N001": "10 天", "W1": "12天"}) == []


@pytest.mark.parametrize("mode", ["qa", "assistant", "assistant_material"])
def test_run_and_rescore_round2_with_original_materials(tmp_path, monkeypatch, mode):
    from tests.eval.w3_e2e_qa import open_imports
    monkeypatch.setattr(qa, "WORK", tmp_path / "round2")
    monkeypatch.setattr(qa, "ROUND", 2)
    encoder = BagEncoder()
    root = tmp_path / "kb"
    root.mkdir()
    engine, imports = open_imports(root, encoder)
    try:
        kb = imports.knowledge.create(qa.KB_NAME)
        job, _ = imports.enqueue(kb.id, "原创制度.txt", "# 原创制度\n阅读后须提交学习记录。".encode(), request_key="N001", source_key="N001")
        preview = imports.parse(kb.id, job.id)
        ready = imports.confirm(kb.id, job.id, preview.preview["digest"])
        material, _ = imports.store.add_text("个人阅读计划", "# 个人阅读计划\n个人阅读计划的讨论地点是青石会议室。", markdown=True)
        files = [dict(id="W1", material_id=material.id, title=material.title, content=material.content)]
        qa.write_json(qa.WORK / "workspace.json", dict(files=files))
        qa.write_json(root / "manifest.json", dict(kb_id=kb.id, documents={"N001": dict(format="txt", status=ready.status)}))
        case = dict(id="M01", category="mixed_workspace", split="dev", source="workspace",
                    question="个人阅读计划的讨论地点在哪？", answer="青石会议室", must=[["青石会议室"]],
                    abstain=False, gold=[dict(doc="W1", text="青石会议室")], max_chars=None)
        (qa.WORK / "cases.jsonl").write_text(json.dumps(case, ensure_ascii=False) + "\n", encoding="utf-8")
        attach_workspace(root, engine, imports.store, encoder)
        qa.validate_workspace(imports.store, files)
        imports.knowledge.set_egress(kb.id, True)
        path = "qa" if mode == "qa" else "assistant"
        responses = [tool_msg("search_knowledge", {"questions": [case["question"]]})] if mode == "assistant" else []
        answer = "【可答性：可回答】讨论地点是青石会议室。〔依据2〕"
        if mode == "assistant_material":
            responses = [tool_msg("read_material", dict(material_id=material.id, chunk=1))]
            answer = "根据工作区《个人阅读计划》，讨论地点是青石会议室。"
        model = fake(*responses, text_msg(answer))
        monkeypatch.setattr("briefly.llm.create_chat_model", lambda **kw: model)
        monkeypatch.setattr("briefly.llm.create_local_embeddings", lambda _: encoder)
        args = Namespace(path=path, n=1, split="all", k=[], output=str(tmp_path / "runs"))
        # run 整体包括专用库校验、工作区核验、联合检索、假模型回答、写报告。
        qa.run(root, args)
        result_dir = next((tmp_path / "runs").iterdir())
        data = json.loads((result_dir / "results.json").read_text(encoding="utf-8"))
        assert data["events"][0]["passed"], data["events"]
        assert any(e["origin"] == "workspace" for e in data["events"][0]["evidence"])
        calls = len(model.calls)
        qa.rescore(result_dir)
        assert len(model.calls) == calls
        data = json.loads((result_dir / "results.json").read_text(encoding="utf-8"))
        assert data["source_summary"]["all"]["correct"] == 1
        absent = dict(case, id="M02", source="none", question="个人阅读计划里奖金报销标准是多少？",
                      abstain=True, gold=[], must=[])
        with (qa.WORK / "cases.jsonl").open("a", encoding="utf-8") as out:
            out.write(json.dumps(absent, ensure_ascii=False) + "\n")
        qa.check(root)
        assert "gold 原文不在入库正文中（0 条）" in (qa.WORK / "check.md").read_text(encoding="utf-8")
        assert "工作区《个人阅读计划》" in (qa.WORK / "check.md").read_text(encoding="utf-8")
        assert len(model.calls) == calls
    finally:
        engine.dispose()
