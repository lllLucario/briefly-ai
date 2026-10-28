import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from sqlalchemy.orm import Session

from briefly.agent import Agent
from briefly.context_builder import SYSTEM_REPLY
from briefly.db import create_db_engine
from briefly.materials import Material, MaterialStore
from fakes import fake, text_msg, tool_msg


@pytest.fixture
def store():
    engine = create_db_engine("sqlite://")
    try:
        yield MaterialStore(engine)
    finally:
        engine.dispose()


def restricted(store):
    return store.add_text("需复核资料", "某单元格写着金额650元。", report={"table_structure_status": "partial"})[0]


def test_selected_restricted_material_blocks_before_model_and_keeps_history(store):
    material = restricted(store)
    model = fake()
    agent = Agent(model, tools=[], store=store)
    answer = agent.chat("金额属于谁？", material_ids=[material.id])
    assert "需核对原件" in answer and model.calls == []
    assert isinstance(agent.history[0], HumanMessage)
    assert agent.history[-1].response_metadata[SYSTEM_REPLY] is True
    assert "650" not in agent.history[0].content  # 未先附加原文


@pytest.mark.parametrize("tool_name,args", [
    ("read_material", {"material_id": 1}),
    ("search_materials", {"keywords": "650"}),
    ("get_outline", {"material_id": 1}),
    ("summarize_materials", {"material_ids": [1]}),
])
def test_unselected_restricted_tool_result_cannot_reach_generation(store, tool_name, args):
    material = restricted(store)
    assert material.id == 1
    model = fake(tool_msg(tool_name, args))
    agent = Agent(model, tools=[], store=store)
    answer = agent.chat("查资料")
    assert "需核对原件" in answer
    assert len(model.calls) == 1  # 只允许先选工具，受限原文没有送入后续生成
    assert all("某单元格" not in str(m.content) for m in model.calls[0])


def test_unrelated_restricted_material_does_not_block_normal_answer(store):
    restricted(store)
    normal, _ = store.add_text("已确认资料", "预订前申请审批。")
    model = fake(text_msg("应在预订前申请审批。"))
    agent = Agent(model, tools=[], store=store)
    assert "预订前" in agent.chat("什么时间申请？", material_ids=[normal.id])
    assert len(model.calls) == 1


def test_new_quality_restriction_blocks_historical_compaction_path(store, monkeypatch):
    material, _ = store.add_text("资料", "原文未限制。")
    model = fake(text_msg("已阅读。"))
    agent = Agent(model, tools=[], store=store)
    agent.chat("阅读", material_ids=[material.id])
    with Session(store.engine) as s, s.begin():
        s.get(Material, material.id).parse_report = {"table_structure_status": "partial"}
    monkeypatch.setattr(agent.compactor, "maybe_compact", lambda *args: (_ for _ in ()).throw(AssertionError("不得压缩受限历史")))
    assert "需核对原件" in agent.chat("再解释一下")
    assert len(model.calls) == 1


def test_clearing_current_report_does_not_wash_conversation_quality_snapshot(store):
    material = restricted(store)
    model = fake()
    agent = Agent(model, tools=[], store=store)
    agent.chat("解释表格", material_ids=[material.id])
    with Session(store.engine) as s, s.begin():
        s.get(Material, material.id).parse_report = {}
    assert "需核对原件" in agent.chat("现在继续回答")
    assert model.calls == []


def test_legacy_chat_inherits_existing_knowledge_snapshot_before_first_read(store):
    from briefly.knowledge.service import KnowledgeService
    material = restricted(store)
    svc = KnowledgeService(store.engine)
    kb = svc.create("资料库")
    svc.attach(kb.id, material.id)
    with Session(store.engine) as s, s.begin():
        s.get(Material, material.id).parse_report = {}
    model = fake()
    assert "需核对原件" in Agent(model, tools=[], store=store).chat("解释归属", material_ids=[material.id])
    assert model.calls == []


def test_legacy_read_artifact_keeps_only_associated_stable_knowledge_refs(store):
    from briefly.knowledge.service import KnowledgeService
    material, _ = store.add_text("已确认材料", "预订前申请审批。")
    other, _ = store.add_text("无关材料", "别的事项。")
    svc = KnowledgeService(store.engine)
    kb = svc.create("库")
    _, version, _ = svc.attach(kb.id, material.id)
    svc.attach(kb.id, other.id)
    model = fake(tool_msg("read_material", {"material_id": material.id}), text_msg("已阅读原文。"))
    agent = Agent(model, tools=[], store=store)
    agent.chat("读取材料")
    message = next(m for m in agent.history if isinstance(m, ToolMessage) and m.name == "read_material")
    references = message.artifact["knowledge_references"]
    assert references and {r["version_id"] for r in references} == {version.id}
    assert svc.resolve_reference(kb.id, references[0]["reference"])["content"] == material.content
