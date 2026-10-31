"""材料库（个人工作区）与知识库（受管控参考资料）隔离：知识库专属材料不经材料入口列出、检索或读取。

知识库内容只经知识库检索读取（外发授权、质量限制、引用才生效）；用户另行添加的同样内容按工作区材料正常可见。
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from briefly.agent import Agent
from briefly.api import create_app
from briefly.context import Context
from briefly.db import create_db_engine
from briefly.knowledge.models import KnowledgeBase, KnowledgeImport
from briefly.materials import (
    KNOWLEDGE, WORKSPACE_FLAG, KnowledgeOnlyMaterial, Material, MaterialStore, mark_knowledge_only,
)
from briefly.material_library import browse
from briefly.todos.service import TodoService
from briefly.tools.materials import material_tools
from briefly.weekly.service import WeeklyService
from fakes import fake, text_msg

KB_TEXT = "# 差旅制度\n上海住宿上限650元，预订前须审批。"
HINT = "属于知识库"


@pytest.fixture
def store():
    engine = create_db_engine("sqlite://")
    try:
        yield MaterialStore(engine)
    finally:
        engine.dispose()


def invoke(store, name, scope=None, **kwargs):
    tool = next(tool for tool in material_tools(store) if tool.name == name)
    result = tool.func(runtime=SimpleNamespace(context=Context(material_ids=scope)), **kwargs)
    return result[0] if tool.response_format == "content_and_artifact" else result


def kb_only(store, text=KB_TEXT):
    material, _ = store.add_text("差旅制度.md", text, source=KNOWLEDGE, kind="md", markdown=True)
    return material


def test_material_tools_hide_and_reject_knowledge_only_material(store):
    kb = kb_only(store)
    mine, _ = store.add_text("我的笔记", "上海出差住宿已订好，住宿发票待报销。")
    listed = invoke(store, "list_materials")
    assert "我的笔记" in listed and "差旅制度" not in listed
    found = invoke(store, "search_materials", keywords="住宿")
    assert "我的笔记" in found and "650" not in found and "差旅制度" not in found
    assert [m.id for m in store.list_materials()] == [mine.id]
    # 显式指定（模型自己填的编号，或用户本次选择）都要拒绝并说明去知识库提问，不静默放行
    calls = [
        ("read_material", None, dict(material_id=kb.id)),
        ("read_material", None, dict(material_id=kb.id, section=1)),
        ("get_outline", None, dict(material_id=kb.id)),
        ("search_materials", None, dict(keywords="住宿", material_ids=[kb.id])),
        ("summarize_materials", None, dict(material_ids=[kb.id])),
        ("list_materials", [kb.id], {}),
        ("read_material", [kb.id], dict(material_id=kb.id)),
    ]
    for name, scope, kwargs in calls:
        with pytest.raises(KnowledgeOnlyMaterial, match=HINT):
            invoke(store, name, scope, **kwargs)
    # 普通材料不受影响
    assert "住宿发票待报销" in invoke(store, "read_material", material_id=mine.id)
    for read in (lambda: store.get(kb.id), lambda: store.chunks(kb.id), lambda: store.sections(kb.id),
                 lambda: store.read_chunk(kb.id, 1), lambda: store.search("住宿", [kb.id])):
        with pytest.raises(KnowledgeOnlyMaterial, match=HINT):
            read()
    assert store.get(kb.id, workspace=False).content == KB_TEXT  # 知识库自身仍可读来源正文


def test_explicit_selection_is_rejected_in_every_workspace_entry(store):
    kb = kb_only(store)
    model = fake(text_msg("不应调用"))
    with pytest.raises(KnowledgeOnlyMaterial, match=HINT):  # 对话附带材料（上下文材料选择）
        Agent(model, tools=[], store=store).chat("总结", material_ids=[kb.id])
    assert model.calls == []
    todos = TodoService(store.engine, store)
    todo = todos.create("订酒店")
    with pytest.raises(KnowledgeOnlyMaterial, match=HINT):
        todos.link(todo.id, kb.id)
    weekly = WeeklyService(store, model_factory=lambda: model)
    with pytest.raises(KnowledgeOnlyMaterial, match=HINT):
        weekly.set_materials(weekly.week().id, [kb.id])
    with pytest.raises(KnowledgeOnlyMaterial, match=HINT):
        store.delete(kb.id)  # 不能从材料库删掉知识库的来源正文
    assert store.get(kb.id, workspace=False)
    assert browse(store.engine)[0] == [] and browse(store.engine, material_ids=[kb.id])[0] == []


def test_web_material_pages_hide_knowledge_only_material(store):
    kb = kb_only(store)
    mine, _ = store.add_text("我的笔记", "本周完成报销。")
    with TestClient(create_app(store, agent_factory=lambda s: Agent(fake(), tools=[], store=s))) as client:
        assert [m["id"] for m in client.get("/materials").json()] == [mine.id]
        assert [m["id"] for m in client.get("/materials/library").json()["items"]] == [mine.id]
        for response in (client.get(f"/materials/{kb.id}"), client.delete(f"/materials/{kb.id}"),
                         client.post("/chat", json={"text": "总结", "material_ids": [kb.id]})):
            assert response.status_code == 404 and HINT in response.json()["detail"]
        assert client.get("/chat/sessions").json() == []
        assert client.get(f"/materials/{mine.id}").json()["content"] == "本周完成报销。"


def test_same_content_added_by_user_becomes_workspace_material(store):
    kb = kb_only(store)
    again, created = store.add_text("我上传的制度", KB_TEXT, source="file", kind="md")
    assert created and again.id == kb.id  # 同一条记录：知识版本仍引用它，工作区按用户材料可见
    assert again.source == "file" and again.parse_report[WORKSPACE_FLAG] is True
    assert "我上传的制度" in invoke(store, "list_materials")
    assert "650" in invoke(store, "read_material", material_id=kb.id)
    # 反过来：用户先上传，知识库入库复用同一条，仍是用户的工作区材料
    mine, _ = store.add_text("先上传的", "工作区先有的内容")
    reused, created = store.add_text("入库", "工作区先有的内容", source=KNOWLEDGE)
    assert not created and reused.id == mine.id and store.get(mine.id).source == "paste"
    # 追踪的文件内容与知识库专属材料相同：转为追踪材料，按追踪规则可见
    other = kb_only(store, "另一份制度原文")
    tracked, created = store.add_text("追踪版本", "另一份制度原文", source="tracked")
    assert created and tracked.id == other.id and store.get(other.id).source == "tracked"


def test_deleting_a_shared_record_in_the_workspace_keeps_the_knowledge_source(store):
    from briefly.knowledge.service import KnowledgeService
    mine, _ = store.add_text("我的制度稿", KB_TEXT, source="file", kind="md")
    svc = KnowledgeService(store.engine)
    kb = svc.create("库")
    doc, version, _ = svc.attach(kb.id, mine.id)
    ref = svc.chunk(kb.id, doc.id, version.id, 1)["reference"]
    todos = TodoService(store.engine, store)
    todo = todos.create("订酒店")
    todos.link(todo.id, mine.id)
    store.delete(mine.id, force=True)  # 在工作区删除：只是移出工作区
    assert store.list_materials() == [] and "我的制度稿" not in invoke(store, "list_materials")
    with pytest.raises(KnowledgeOnlyMaterial, match=HINT):
        store.get(mine.id)
    assert todos.materials(todo.id) == []  # 工作区的关联照常清掉
    source = svc.resolve_reference(kb.id, ref)  # 知识库的来源正文与引用不受影响
    assert source["status"] != "source_deleted" and "650" in source["content"]
    assert store.get(mine.id, workspace=False).source == KNOWLEDGE
    # 再上传同样内容又回到工作区
    again, _ = store.add_text("重新上传", KB_TEXT, source="file", kind="md")
    assert again.id == mine.id and [m.id for m in store.list_materials()] == [mine.id]


def _old_import(s, kb_id, material_id, created_at, n):
    s.add(KnowledgeImport(kb_id=kb_id, request_key=f"r{n}", request_fingerprint="f", filename="a.md",
                          source_key=f"a{n}", expected_revision=0, tokens=512, status="ready",
                          material_id=material_id, created_at=created_at))


def test_old_database_migration_marks_only_import_created_materials(tmp_path):
    from sqlalchemy.orm import Session

    url = "sqlite:///" + (tmp_path / "app.db").as_posix()
    engine = create_db_engine(url)
    store = MaterialStore(engine)
    now = datetime.now()
    by_import = store.add_text("入库.md", "知识库入库产生的正文", source="file")[0]
    uploaded_first = store.add_text("先上传.md", "用户先上传的正文", source="file")[0]
    linked = store.add_text("待办里用过.md", "待办关联过的正文", source="file")[0]
    flagged = store.add_text("另行添加.md", "用户另行添加过的正文", source="file")[0]
    todos = TodoService(engine, store)
    todos.link(todos.create("用到").id, linked.id)
    with Session(engine) as s:
        kb = KnowledgeBase(name="库")
        s.add(kb)
        s.flush()
        s.get(Material, flagged.id).parse_report = {WORKSPACE_FLAG: True}
        s.get(Material, uploaded_first.id).created_at = now - timedelta(days=2)
        for n, material in enumerate((by_import, uploaded_first, linked, flagged)):
            _old_import(s, kb.id, material.id, now - timedelta(days=1), n)
        s.commit()
    engine.dispose()
    engine = create_db_engine(url)  # 启动时迁移
    store = MaterialStore(engine)
    try:
        sources = {m.id: m.source for m in store.list_materials()}
        assert by_import.id not in sources
        assert store.get(by_import.id, workspace=False).source == KNOWLEDGE
        assert {uploaded_first.id, linked.id, flagged.id} <= set(sources)
        assert mark_knowledge_only(engine) == 0  # 幂等
    finally:
        engine.dispose()


def test_knowledge_import_is_knowledge_only_and_knowledge_qa_still_answers(tmp_path):
    pytest.importorskip("sqlite_vec", reason="知识入库回归需安装.[rag]")
    from briefly.knowledge.index import LocalIndex
    from briefly.knowledge.ingestion import ImportService
    from briefly.knowledge.qa import KnowledgeQA
    from briefly.knowledge.retrieval import RetrievalService
    from briefly.loaders import LoadedDocument
    from test_knowledge_slicing import Encoder

    engine = create_db_engine("sqlite:///" + (tmp_path / "app.db").as_posix())
    try:
        imports = ImportService(engine, tmp_path, LocalIndex(tmp_path / "index.db"), Encoder,
                                parser=lambda filename, data: LoadedDocument(data.decode(), markdown=True))
        kb = imports.knowledge.create("制度库")
        job, _ = imports.enqueue(kb.id, "travel.md", KB_TEXT.encode(), request_key="one")
        preview = imports.parse(kb.id, job.id)
        assert "650" in imports.preview_chunks(kb.id, job.id)[0]["content"]  # 入库预览不受隔离影响
        assert imports.confirm(kb.id, job.id, preview.preview["digest"]).status == "ready"
        store = imports.store
        material = store.get(preview.material_id, workspace=False)
        assert material.source == KNOWLEDGE
        assert "当前没有可用的材料" in invoke(store, "list_materials")
        assert "没有找到" in invoke(store, "search_materials", keywords="住宿 650")
        with pytest.raises(KnowledgeOnlyMaterial, match=HINT):
            invoke(store, "read_material", material_id=material.id)
        model = fake(text_msg("【可答性：可回答】上海住宿上限650元。〔依据1〕"))
        qa = KnowledgeQA(RetrievalService(imports), lambda: model)
        assert qa.answer(kb.id, "上海住宿上限是多少？")["status"] == "external_authorization_required"
        imports.knowledge.set_egress(kb.id, True)
        result = qa.answer(kb.id, "上海住宿上限是多少？")
        assert result["status"] == "answered" and len(model.calls) == 1 and result["citations"]
    finally:
        engine.dispose()
