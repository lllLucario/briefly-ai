"""待办接口：父子结构、状态确认（409）、归档、上传与从材料库选择、删除被关联的材料。不调用模型。"""
from fastapi.testclient import TestClient

from briefly.api import create_app
from briefly.db import create_db_engine
from briefly.materials import MaterialStore
from fakes import fake


def client():
    return TestClient(create_app(MaterialStore(create_db_engine("sqlite://")), model_factory=lambda: fake()))


def test_parent_children_and_status_flow():
    with client() as c:
        parent = c.post("/todos", json={"title": "支付改造", "due_date": "2026-11-30"}).json()
        a = c.post("/todos", json={"title": "接口联调", "parent_id": parent["id"], "due_date": "2026-10-20"}).json()
        b = c.post("/todos", json={"title": "压测", "parent_id": parent["id"], "due_date": "2026-11-10"}).json()
        third = c.post("/todos", json={"title": "第三级", "parent_id": a["id"]})
        assert third.status_code == 400 and "转为独立待办" in third.json()["detail"]

        got = c.get(f"/todos/{parent['id']}").json()
        assert [k["title"] for k in got["children"]] == ["接口联调", "压测"] and got["children_total"] == 2

        c.post(f"/todos/{a['id']}/status", json={"status": "done"})
        listed = {t["id"]: t for t in c.get("/todos").json()}
        assert (listed[parent["id"]]["children_done"], listed[parent["id"]]["children_total"]) == (1, 2)

        r = c.post(f"/todos/{parent['id']}/status", json={"status": "done"})
        assert r.status_code == 409 and r.json()["confirm"] and "压测" in r.json()["detail"]
        assert c.get(f"/todos/{b['id']}").json()["status"] == "todo"
        done = c.post(f"/todos/{parent['id']}/status", json={"status": "done", "confirm": True}).json()
        assert done["status"] == "done" and c.get(f"/todos/{b['id']}").json()["status"] == "todo"

        notices = c.post(f"/todos/{b['id']}/status", json={"status": "done"}).json()["notices"]
        assert notices == []  # 父待办已经完成，不再提示

        edited = c.put(f"/todos/{b['id']}", json={"title": "压测与报告"}).json()
        assert edited["title"] == "压测与报告" and edited["due_date"] == "2026-11-10"  # 没传的字段不变


def test_promote_and_archive():
    with client() as c:
        parent = c.post("/todos", json={"title": "项目"}).json()
        child = c.post("/todos", json={"title": "子任务", "parent_id": parent["id"]}).json()
        r = c.delete(f"/todos/{parent['id']}")
        assert r.status_code == 409 and "子待办" in r.json()["detail"]
        promoted = c.post(f"/todos/{child['id']}/promote").json()
        assert promoted["id"] == child["id"] and promoted["parent"] is None
        assert c.delete(f"/todos/{parent['id']}").json()["archived_at"]
        assert [t["id"] for t in c.get("/todos").json()] == [child["id"]]
        assert len(c.get("/todos", params={"archived": True}).json()) == 2
        assert c.post(f"/todos/{parent['id']}/restore").json()["archived_at"] is None


def test_upload_in_todo_then_select_from_library():
    """流程 2：在待办里上传 → 材料库自动出现 → 另一个待办从材料库选择同一份材料，只存一份。"""
    with client() as c:
        a = c.post("/todos", json={"title": "写方案"}).json()
        b = c.post("/todos", json={"title": "评审"}).json()
        up = c.post(f"/todos/{a['id']}/materials/upload", files={"file": ("方案.md", "# 方案\n灰度发布".encode(), "text/markdown")}).json()
        assert up["new_material"] and [m["relation"] for m in up["materials"]] == ["direct"]
        library = c.get("/materials").json()
        assert [m["id"] for m in library] == [up["material_id"]]

        linked = c.post(f"/todos/{b['id']}/materials", json={"material_id": up["material_id"]}).json()
        assert linked["created"] and linked["materials"][0]["material_id"] == up["material_id"]
        assert not c.post(f"/todos/{b['id']}/materials", json={"material_id": up["material_id"]}).json()["created"]
        again = c.post(f"/todos/{b['id']}/materials/upload", files={"file": ("副本.md", "# 方案\n灰度发布".encode(), "text/markdown")}).json()
        assert not again["new_material"] and len(c.get("/materials").json()) == 1
        assert {t["id"] for t in c.get(f"/materials/{up['material_id']}/todos").json()} == {a["id"], b["id"]}

        assert c.delete(f"/todos/{a['id']}/materials/{up['material_id']}").json() == []
        assert len(c.get("/materials").json()) == 1  # 解除关联不删材料


def test_shared_materials_from_parent():
    with client() as c:
        doc = c.post("/materials", json={"title": "项目背景", "content": "背景内容"}).json()["material"]
        own = c.post("/materials", json={"title": "接口文档", "content": "接口内容"}).json()["material"]
        parent = c.post("/todos", json={"title": "项目"}).json()
        child = c.post("/todos", json={"title": "联调", "parent_id": parent["id"]}).json()
        c.post(f"/todos/{parent['id']}/materials", json={"material_id": doc["id"]})
        c.post(f"/todos/{child['id']}/materials", json={"material_id": own["id"]})
        got = c.get(f"/todos/{child['id']}").json()["material_list"]
        assert [(m["title"], m["relation"], m["from_todo"]) for m in got] == [("接口文档", "direct", child["id"]), ("项目背景", "shared", parent["id"])]


def test_deleting_linked_material_needs_confirmation():
    with client() as c:
        m = c.post("/materials", json={"title": "方案", "content": "方案内容"}).json()["material"]
        t = c.post("/todos", json={"title": "写方案"}).json()
        c.post(f"/todos/{t['id']}/materials", json={"material_id": m["id"]})
        r = c.delete(f"/materials/{m['id']}")
        assert r.status_code == 409 and r.json()["confirm"] and "1 个待办" in r.json()["detail"]
        assert c.delete(f"/materials/{m['id']}", params={"force": True}).status_code == 204
        assert c.get(f"/todos/{t['id']}").json()["material_list"] == []
        events = [a["event_type"] for a in c.get(f"/todos/{t['id']}/activities").json()]
        assert events[-1] == "material_deleted"


def test_notes_and_timeline_by_window():
    with client() as c:
        t = c.post("/todos", json={"title": "调研"}).json()
        c.post(f"/todos/{t['id']}/notes", json={"text": "完成竞品数据收集，等待财务提供报价"})
        today = c.get(f"/todos/{t['id']}/timeline", params={"start": "2026-01-01"}).json()
        assert [x["event_type"] for x in today] == ["created", "note"]
        assert c.get(f"/todos/{t['id']}/activities", params={"end": "2020-01-01"}).json() == []
        assert c.post(f"/todos/{t['id']}/notes", json={"text": " "}).status_code == 400
