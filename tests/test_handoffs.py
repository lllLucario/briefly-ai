"""助理到业务对象的显式交接：假模型、原文核对、版本、去重与事务边界。"""
from datetime import date

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from briefly.agent import Agent
from briefly.api import create_app
from briefly.db import create_db_engine
from briefly.handoffs import Candidate, conversation_input, verified_candidates
from briefly.materials import Material, MaterialStore
from briefly.todos.models import Todo, TodoActivity
from briefly.todos.service import TodoService
from briefly.tools.business import business_tools
from briefly.weekly.models import WeeklyReport
from fakes import FunctionModel, structured

TEXT = "李强负责整理测试记录，截止2026-10-23。"
ITEM = {"title": "整理测试记录", "source_id": "m1", "quote": TEXT, "owner": "李强", "owner_quote": TEXT, "deadline": "2026-10-23"}


@pytest.fixture
def env():
    engine = create_db_engine("sqlite://")
    store = MaterialStore(engine)
    material, _ = store.add_text("会议记录", TEXT)
    data = {"items": [ITEM]}
    def respond(messages):
        if "TodoCandidates" in messages[0].content:
            return structured("TodoCandidates", items=data["items"])
        return AIMessage("【待办清单】整理测试记录，李强负责，截止2026-10-23。")
    model = FunctionModel(respond=respond, calls=[])
    app = create_app(store, model_factory=lambda: model, agent_factory=lambda s: Agent(model, store=s))
    with TestClient(app) as client:
        thread = client.post("/chat", json={"text":"提取待办", "material_ids":[material.id], "skill":"todo"}).json()["thread_id"]
        msg = client.get(f"/chat/sessions/{thread}").json()["messages"][-1]["message_id"]
        yield client, app, data, thread, msg
    engine.dispose()


def preview(env):
    c, _, _, thread, msg = env
    response = c.post(f"/chat/sessions/{thread}/todo-preview", json={"message_id":msg})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("deadline,expected", [
    ("截止2026-10-23", "2026-10-23"),
    ("2026-10-23", "2026-10-23"),
    ("2026-10-23至2026-10-24", None),
    ("下周", None),
])
def test_deadline_preserves_original_and_only_sets_unambiguous_full_date(deadline, expected):
    text = f"李强负责整理测试记录，{deadline}。"
    model = FunctionModel(respond=lambda _: structured("TodoCandidates", items=[{**ITEM, "quote":text, "owner_quote":text, "deadline":deadline}]), calls=[])
    items, warnings = verified_candidates(model, {"answer":"整理测试记录", "sources":[{"id":"m1", "title":"记录", "content":text}]})
    assert items[0]["deadline"] == deadline
    assert items[0]["due_date"] == expected
    assert warnings == []


def chosen(draft, **changes):
    x = draft["items"][0]
    return {"index":0,"title":x["title"],"description":x["description"],"due_date":x["due_date"], **changes}


@pytest.mark.parametrize("field", ["owner", "deadline", "both"])
def test_candidate_fields_from_different_passage_warn_and_preserve_exact_quotes(field):
    text = "需要整理接口文档。\n另一项：李强负责测试，截止2026-10-23。"
    spec = {"title": "整理接口文档", "source_id": "m1", "quote": "需要整理接口文档。"}
    if field in ("owner", "both"):
        spec.update(owner="李强", owner_quote="李强负责测试")
    if field in ("deadline", "both"):
        spec.update(deadline="2026-10-23", deadline_quote="截止2026-10-23")
    model = FunctionModel(respond=lambda _: structured("TodoCandidates", items=[spec]), calls=[])
    items, _ = verified_candidates(model, {"answer": "整理接口文档", "sources": [{"id": "m1", "title": "记录", "content": text}]})
    assert items[0]["review_warnings"] == ["负责人/期限依据与任务原话不在同一处，请核对"]
    for quote in [spec.get("owner_quote"), spec.get("deadline_quote")]:
        if quote:
            assert quote in items[0]["description"]


@pytest.mark.parametrize("deadline_quote", ["", "截止2026-10-24", "原文没有这句话2026-10-23"])
def test_deadline_elsewhere_in_source_without_verified_quote_is_cleared(deadline_quote):
    text = "需要整理接口文档。另一项周报截止2026-10-23。"
    spec = {"title": "整理接口文档", "source_id": "u1", "quote": "需要整理接口文档。", "deadline": "2026-10-23", "deadline_quote": deadline_quote}
    model = FunctionModel(respond=lambda _: structured("TodoCandidates", items=[spec]), calls=[])
    items, warnings = verified_candidates(model, {"answer": "整理接口文档", "sources": [{"id": "u1", "title": "输入", "content": text}]})
    assert items[0]["deadline"] == "待定" and items[0]["due_date"] is None
    assert warnings and not items[0]["review_warnings"]


def test_preview_does_not_create_and_confirm_preserves_source_and_owner(env):
    c, app, _, thread, _ = env
    draft = preview(env)
    assert c.get("/todos").json() == []
    x = draft["items"][0]
    assert x["owner"] == "李强" and x["due_date"] == "2026-10-23"
    url = f"/chat/sessions/{thread}/todo-import/{draft['draft_id']}"
    body = {"items":[chosen(draft)]}
    response = c.post(url, json=body)
    assert response.status_code == 200, response.text
    assert c.post(url,json=body).json() == response.json()
    [todo] = c.get("/todos").json()
    detail = c.get(f"/todos/{todo['id']}").json()
    assert detail["status"] == "todo" and "李强" in detail["description"] and TEXT in detail["description"]
    assert detail["material_list"][0]["material_id"] == 1
    history = c.get(f"/chat/sessions/{thread}").json()["messages"]
    assert history[-1]["saved_todo_ids"] == [todo["id"]]
    assert c.post(url,json={"items":[chosen(draft,title="另一项任务")]}).status_code == 409
    # 即使之后有了新回答，重试同一次成功请求仍能找回回执，不重复创建。
    c.post("/chat",json={"text":"再短一点","thread_id":thread})
    assert c.post(url,json=body).json() == response.json()
    assert len(c.get("/todos").json()) == 1


@pytest.mark.parametrize("change", ["new_answer", "changed_material", "deleted_material"])
def test_stale_preview_is_rejected_without_writes(env, change):
    c, app, _, thread, _ = env
    draft = preview(env)
    if change == "new_answer":
        c.post("/chat", json={"thread_id":thread,"text":"删掉这条，不要保存"})
    else:
        with Session(app.state.store.engine) as s:
            if change == "changed_material":
                s.execute(update(Material).where(Material.id == 1).values(content="新的内容"))
            else:
                s.delete(s.get(Material,1))
            s.commit()
    r = c.post(f"/chat/sessions/{thread}/todo-import/{draft['draft_id']}",json={"items":[chosen(draft)]})
    assert r.status_code in {404,409},r.text
    assert c.get("/todos").json() == []


def test_batch_import_rolls_back_if_later_parent_invalid(env):
    c, app, data, thread, _ = env
    data["items"] = [ITEM,{**ITEM,"title":"复核测试记录"}]
    draft=preview(env)
    rows=[chosen(draft),{**chosen(draft),"index":1,"parent_id":999}]
    r=c.post(f"/chat/sessions/{thread}/todo-import/{draft['draft_id']}",json={"items":rows})
    assert r.status_code == 404,r.text
    assert c.get("/todos").json() == []
    with Session(app.state.store.engine) as s:
        assert list(s.scalars(select(TodoActivity))) == []


def test_unverified_owner_deadline_and_quote_not_saved_as_facts(env):
    _, _, data, _, _=env
    data["items"]=[{**ITEM,"owner":"王五","owner_quote":TEXT,"deadline":"2026-10-30"},{**ITEM,"quote":"原文不存在"}]
    draft=preview(env)
    assert len(draft["items"])==1
    item=draft["items"][0]
    assert item["owner"]==item["deadline"]=="待定" and item["due_date"] is None
    assert len(draft["warnings"])==2


def test_relative_deadline_is_preserved_without_guessing(env):
    _, app, data, _, _=env
    with Session(app.state.store.engine) as s:
        s.get(Material,1).content="李强负责整理测试记录，下周五交。";s.commit()
    data["items"]=[{**ITEM,"quote":"李强负责整理测试记录，下周五交。","owner_quote":"李强负责整理测试记录，下周五交。","deadline":"下周五"}]
    x=preview(env)["items"][0]
    assert x["deadline"]=="下周五" and x["due_date"] is None


def test_drafts_are_bound_to_session(env):
    c, _, _, thread, _=env
    draft=preview(env)
    other=c.post("/chat",json={"text":"新会话"}).json()["thread_id"]
    r=c.post(f"/chat/sessions/{other}/todo-import/{draft['draft_id']}",json={"items":[chosen(draft)]})
    assert r.status_code==404


def weekly_preview(env, **extra):
    c, _, _, thread, msg=env
    origin={"thread_id":thread,"message_id":msg,"day":"2026-10-20", **extra}
    r=c.post("/weekly/input-preview",json=origin)
    assert r.status_code==200,r.text
    p=r.json()
    return {**origin,"revision":p["revision"],"target_revision":p["target_revision"],"request_id":"request-one"},p


def test_weekly_transfer_merges_inputs_and_is_idempotent(env):
    c,app,_,_,_=env
    weekly=app.state.weekly
    report=weekly.week(date(2026,10,20))
    weekly.set_note(report.id,"已有说明")
    with Session(app.state.store.engine) as s:
        r=s.get(WeeklyReport,report.id);r.status="done";r.content="原来已经写好的正文";s.commit()
    body,p=weekly_preview(env)
    assert p["existing_note"]=="已有说明" and p["available_materials"]==[{"id":1,"title":"会议记录"}]
    body.update(material_ids=[1],note="用户确认追加的原始工作说明")
    r=c.post("/weekly/input-transfer",json=body)
    assert r.status_code==200,r.text
    assert c.post("/weekly/input-transfer",json=body).json()==r.json()
    saved=weekly.report(report.id)
    assert saved.note=="已有说明\n\n用户确认追加的原始工作说明"
    assert saved.material_ids==[1] and saved.content=="原来已经写好的正文"
    assert saved.checklist=="draft"
    assert "【待办清单】" not in saved.note


def test_weekly_transfer_rejects_concurrent_edits_and_extra_material(env):
    c, app, _, _, _=env
    body,_=weekly_preview(env)
    report=app.state.weekly.week(date(2026,10,20))
    app.state.weekly.set_note(report.id,"另一个页面刚保存的说明")
    r=c.post("/weekly/input-transfer",json={**body,"note":"追加"})
    assert r.status_code==409,r.text
    assert app.state.weekly.report(report.id).note=="另一个页面刚保存的说明"
    m,_=app.state.store.add_text("别的材料","无关内容")
    r=c.post("/weekly/input-preview",json={k:v for k,v in {**body,"material_ids":[m.id]}.items() if k in {"thread_id","message_id","day","material_ids"}})
    assert r.status_code==400


def test_material_shortcut_preview_has_no_write_and_can_transfer(env):
    c,app,_,_,_=env
    origin={"day":"2026-10-20","material_ids":[1]}
    p=c.post("/weekly/input-preview",json=origin).json()
    assert app.state.weekly.list_reports()==[]
    r=c.post("/weekly/input-transfer",json={**origin,"revision":p["revision"],"target_revision":p["target_revision"],"request_id":"direct","note":""})
    assert r.status_code==200,r.text
    saved=app.state.weekly.report(r.json()["report_id"])
    assert saved.material_ids==[1] and not saved.content


def test_weekly_transfer_checks_target_again_at_the_actual_write(env):
    from sqlalchemy import event
    c, app, _, _, _ = env
    report = app.state.weekly.week(date(2026,10,20))
    body, _ = weekly_preview(env)
    fired = []

    def edit_before_write(conn, cursor, statement, parameters, context, executemany):
        if not fired and statement.startswith("UPDATE weekly_reports"):
            fired.append(True)
            app.state.weekly.set_note(report.id, "刚刚在另一个页面补充的内容")

    event.listen(app.state.store.engine, "before_cursor_execute", edit_before_write)
    try:
        response = c.post("/weekly/input-transfer", json={**body,"note":"这次准备追加的内容"})
        assert response.status_code == 409, response.text
        assert app.state.weekly.report(report.id).note == "刚刚在另一个页面补充的内容"
    finally:
        event.remove(app.state.store.engine, "before_cursor_execute", edit_before_write)


def test_progress_and_optional_status_are_atomic_and_require_parent_confirmation(env):
    c, app, _, _, _=env
    svc=TodoService(app.state.store.engine,app.state.store)
    parent=svc.create("发布准备");svc.create("回归",parent_id=parent.id)
    url=f"/todos/{parent.id}/notes"
    assert c.post(url,json={"text":"已准备发布说明","status":"done"}).status_code==409
    assert svc.get(parent.id).status=="todo" and len(svc.activities([parent.id]))==2
    assert c.post(url,json={"text":"已准备发布说明","status":"done","confirm":True}).status_code==201
    assert svc.get(parent.id).status=="done"
    assert c.post(url,json={"text":"x"*501,"status":"todo"}).status_code==400
    assert svc.get(parent.id).status=="done"


def test_business_tools_are_read_only_and_page_real_data(env):
    _,app,_,_,_=env
    svc=TodoService(app.state.store.engine,app.state.store)
    t=svc.create("真实保存的任务", "甲"*3100)
    tools={t.name:t for t in business_tools(app.state.store)}
    assert set(tools)=={"read_todos","read_weekly_reports"}
    assert "真实保存的任务" in tools["read_todos"].invoke({})
    assert "第2/2页" in tools["read_todos"].invoke({"todo_id":t.id,"page":2})
    assert "尚未保存正式周报" in tools["read_weekly_reports"].invoke({})
    assert app.state.weekly.list_reports()==[] and len(svc.list())==1


def test_feishu_tool_artifact_is_an_original_source_without_explicit_selection(env):
    from types import SimpleNamespace
    _, app, _, _, _ = env
    messages = [
        HumanMessage("读取这个会议链接"),
        ToolMessage("已读取飞书文档", tool_call_id="call-1", artifact={"material_ids":[1]}),
        AIMessage("整理测试记录", id="answer-1"),
    ]
    snap = conversation_input(SimpleNamespace(history_of=lambda _: messages), app.state.store, "thread", "answer-1")
    assert snap["sources"][-1] == {"id":"m1", "title":"会议记录", "content":TEXT, "material_id":1}
