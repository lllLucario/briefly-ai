"""材料库浏览接口 /materials/library：搜索、筛选、排序、分页、来源与更新方式、关联待办。不调用模型和真实飞书。"""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, update
from sqlalchemy.orm import Session

from briefly import config
from briefly.api import create_app
from briefly.db import create_db_engine
from briefly.materials import Material, MaterialStore
from briefly.tracking.blobs import BlobStore
from briefly.tracking.service import TrackingService
from fakes import FakeFeishu, fake
from test_tracking_feishu import API, DOC, URL, Doc
from tracking_util import env  # noqa: F401


@pytest.fixture
def client():
    engine = create_db_engine("sqlite://")
    with TestClient(create_app(MaterialStore(engine), model_factory=lambda: fake())) as c:
        yield c
    engine.dispose()


def add(c, title: str, content: str | None = None) -> int:
    return c.post("/materials", json={"title": title, "content": content or f"{title}的正文"}).json()["material"]["id"]


def page(c, **params) -> dict:
    resp = c.get("/materials/library", params=params)
    assert resp.status_code == 200, resp.text
    return resp.json()


def titles(c, **params) -> list[str]:
    return [x["title"] for x in page(c, **params)["items"]]


def test_title_search_is_literal_case_insensitive_and_trimmed(client):
    for title in ["支付接口说明", "API Guide", "进度100%完成", "a_b 命名", "axb 命名"]:
        add(client, title)
    assert titles(client, q="接口") == ["支付接口说明"]
    assert titles(client, q="  api guide ") == ["API Guide"]
    assert titles(client, q="100%") == ["进度100%完成"]
    assert titles(client, q="a_b") == ["a_b 命名"]  # _ 按普通字符，不匹配 axb
    assert page(client, q="不存在")["total"] == 0
    assert client.get("/materials/library", params={"q": "长" * 201}).status_code == 422


def test_sort_and_pagination_are_stable_without_gaps(client):
    ids = [add(client, f"材料{i:02d}") for i in range(7)]
    with Session(client.app.state.store.engine) as s:  # 同一添加时间：按编号稳定排序
        s.execute(update(Material).values(created_at=datetime(2026, 10, 1)))
        s.commit()
    seen = []
    for offset in range(0, 7, 3):
        got = page(client, limit=3, offset=offset)
        assert got["total"] == 7 and (got["offset"], got["limit"]) == (offset, 3)
        seen += [x["id"] for x in got["items"]]
    assert seen == sorted(ids, reverse=True)
    assert [x["id"] for x in page(client, sort="oldest")["items"]] == ids
    assert page(client, offset=50)["items"] == [] and page(client, offset=50)["total"] == 7
    assert titles(client, sort="title", limit=2) == ["材料00", "材料01"]


def test_items_keep_summary_fields_and_preview_is_plain_text(client):
    long = "第一段  有\n\n多个   空白<b>不是标签</b>" + "很长" * 100
    mid = add(client, "长正文", long)
    item = page(client)["items"][0]
    assert item["id"] == mid and item["source"] == "paste" and item["source_category"] == "paste"
    assert item["tracking_state"] == "fixed" and item["related_todos"] == []
    assert {"file_type", "created_at", "warnings", "feishu", "tracked", "char_count", "chunk_count"} <= item.keys()
    assert item["preview"].startswith("第一段 有 多个 空白<b>不是标签</b>") and item["preview"].endswith("…")
    assert len(item["preview"]) == 121
    upload = client.post("/materials/upload", files={"file": ("进展.md", "# 进展\n完成联调".encode())}).json()["material"]
    assert upload["source_category"] == "file" and upload["preview"] == "进展 完成联调"
    table = add(client, "表格", "## 二、计划\n| 任务 | 负责人 |\n|---|---|\n| 压测 | **小王** |\n- 第一项\n1. 步骤")
    assert next(x for x in page(client)["items"] if x["id"] == table)["preview"] == "二、计划 任务 负责人 压测 小王 第一项 1. 步骤"


def test_relations_parent_shared_direct_first_and_archived(client):
    parent = client.post("/todos", json={"title": "支付改造"}).json()["id"]
    child = client.post("/todos", json={"title": "接口联调", "parent_id": parent}).json()["id"]
    other = client.post("/todos", json={"title": "旧项目"}).json()["id"]
    shared, own, both, old, free = (add(client, t) for t in ["父材料", "子材料", "双关联", "归档材料", "未关联"])
    for todo, material in [(parent, shared), (child, own), (parent, both), (child, both), (other, old)]:
        client.post(f"/todos/{todo}/materials", json={"material_id": material})
    client.delete(f"/todos/{other}")

    items = {x["title"]: x for x in page(client)["items"]}
    assert [(r["id"], r["relation"], r["from_todo_id"]) for r in items["父材料"]["related_todos"]] == [
        (parent, "direct", parent), (child, "shared", parent)]  # fmt: skip
    child_rel = items["父材料"]["related_todos"][1]
    assert (child_rel["parent_id"], child_rel["parent_title"]) == (parent, "支付改造")
    assert [(r["id"], r["relation"]) for r in items["双关联"]["related_todos"]] == [(parent, "direct"), (child, "direct")]
    assert [r["id"] for r in items["子材料"]["related_todos"]] == [child]  # 子待办的材料不共享给父待办
    assert items["归档材料"]["related_todos"] == []
    archived = {x["title"]: x for x in page(client, include_archived=True)["items"]}["归档材料"]["related_todos"]
    assert archived[0]["archived"] is True

    # 未关联：没有任何直接关联（含已归档待办），不随开关变化
    assert titles(client, relation="unlinked") == ["未关联"]
    assert titles(client, relation="unlinked", include_archived=True) == ["未关联"]
    assert sorted(titles(client, relation="todo", todo_id=child)) == sorted(["父材料", "子材料", "双关联"])
    assert sorted(titles(client, relation="todo", todo_id=parent)) == sorted(["父材料", "双关联"])
    assert titles(client, relation="todo", todo_id=other) == ["归档材料"]
    assert page(client)["total"] == 5  # 关联多个待办也只算一条
    assert client.app.state.store.list_materials()  # 只读：关联没有变化
    assert len(client.get(f"/todos/{child}/materials").json()) == 4


def test_invalid_parameters_fail_explicitly(client):
    add(client, "材料")
    assert client.get("/materials/library", params={"relation": "todo"}).status_code == 400
    assert client.get("/materials/library", params={"todo_id": 1}).status_code == 400
    assert client.get("/materials/library", params={"relation": "todo", "todo_id": 999}).status_code == 404
    for bad in [{"source": "tracked"}, {"sort": "size"}, {"limit": 0}, {"limit": 101}, {"offset": -1}, {"tracking": "x"}]:
        assert client.get("/materials/library", params=bad).status_code == 422, bad
    many = client.get("/materials/library", params={"material_ids": list(range(1, 102))})
    assert many.status_code == 400 and "100" in many.json()["detail"]


def test_material_ids_intersect_with_filters_and_detail_route_still_works(client):
    a, b, c_ = add(client, "甲"), add(client, "乙"), add(client, "丙")
    assert sorted(x["id"] for x in page(client, material_ids=[a, c_])["items"]) == [a, c_]
    assert titles(client, material_ids=[a, c_], q="丙") == ["丙"]
    assert client.get(f"/materials/{b}").json()["title"] == "乙"
    assert client.get("/materials/abc").status_code == 422
    listed = client.get("/materials").json()
    assert isinstance(listed, list) and listed[0]["source_category"] == "paste" and "preview" in listed[0]


def test_local_tracking_states_and_old_versions_stay_hidden(env):  # noqa: F811
    env.write("方案.md", "# 方案\n第一版")
    with TestClient(create_app(env.store, model_factory=lambda: fake())) as c:
        source = c.post("/tracking/sources", json={"paths": [str(env.work / "方案.md")]}).json()["added"][0]["id"]
        first = page(c)["items"]
        todo = c.post("/todos", json={"title": "写方案"}).json()["id"]
        c.post(f"/todos/{todo}/materials", json={"material_id": first[0]["id"]})
        env.write("方案.md", "# 方案\n第二版")
        c.post("/tracking/scan")
        got = page(c)
        assert got["total"] == 1  # 旧版本不回流
        item = got["items"][0]
        assert item["id"] != first[0]["id"] and item["source"] == "tracked"
        assert (item["source_category"], item["tracking_state"]) == ("file", "tracked")
        assert [r["id"] for r in item["related_todos"]] == [todo]  # 关联在旧版本上，也显示在最新版本上
        assert titles(c, tracking="tracked") == ["方案.md"] and titles(c, tracking="fixed") == []
        assert titles(c, source="file") == ["方案.md"] and titles(c, source="feishu") == []

        c.post(f"/tracking/sources/{source}/pause")
        paused = page(c)["items"][0]
        assert paused["tracking_state"] == "paused" and "暂停" in paused["tracking_note"]
        assert titles(c, tracking="tracked") == ["方案.md"]  # 暂停不等于固定副本
        c.post(f"/tracking/sources/{source}/resume")
        (env.work / "方案.md").unlink()
        c.post("/tracking/scan")
        assert page(c)["items"][0]["tracking_state"] == "error"
        assert len(c.get(f"/todos/{todo}/materials").json()) == 1  # 查询不新增关联


@pytest.fixture
def feishu(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TRACKING_DIR", str(tmp_path / "store"))
    doc = Doc()
    feishu = FakeFeishu({
        ("GET", f"{API}/docx/v1/documents/{DOC}"): doc.info,
        ("GET", f"{API}/docx/v1/documents/{DOC}/blocks"): doc.blocks,
        ("POST", f"{API}/drive/v1/metas/batch_query"): {"metas": [{"doc_token": DOC, "latest_modify_user": "ou_me", "owner_id": "ou_me"}]},
        ("GET", f"{API}/contact/v3/users/ou_me"): {"user": {"name": "王五"}},
    })  # fmt: skip
    engine = create_db_engine("sqlite://")
    store = MaterialStore(engine)
    svc = TrackingService(engine, store, BlobStore(tmp_path / "store"), feishu_factory=feishu.client)
    with TestClient(create_app(store, model_factory=lambda: fake(), feishu_factory=feishu.client)) as c:
        yield c, svc, doc, feishu
    engine.dispose()


def test_feishu_copy_and_latest_version_share_todo_but_stay_separate(feishu):
    from briefly.feishu.service import import_link

    c, svc, doc, fake_feishu = feishu
    with fake_feishu.client() as client:
        copy, _ = import_link(client, svc.store, URL)
    todo = c.post("/todos", json={"title": "周报草稿跟进"}).json()["id"]
    c.post(f"/todos/{todo}/materials", json={"material_id": copy.id})
    assert page(c)["items"][0]["tracking_state"] == "fixed"
    source = svc.add_paths([URL]).added[0].id
    doc.revision += 1
    doc.paragraphs += ["新增一段"]
    svc.scan()

    items = {x["id"]: x for x in page(c)["items"]}
    assert len(items) == 2  # 固定副本与最新版本是两份材料
    latest = next(x for x in items.values() if x["source"] == "tracked")
    assert items[copy.id]["source"] == "feishu" and items[copy.id]["tracking_state"] == "tracked"  # 固定副本 · 已开启追踪
    assert latest["source_category"] == items[copy.id]["source_category"] == "feishu"
    assert [r["id"] for r in latest["related_todos"]] == [todo] == [r["id"] for r in items[copy.id]["related_todos"]]
    assert sorted(x["id"] for x in page(c, relation="todo", todo_id=todo)["items"]) == sorted(items)

    svc.remove_source(source)
    left = page(c)["items"]
    assert [x["id"] for x in left] == [copy.id] and left[0]["tracking_state"] == "fixed"  # 移除后不显示为追踪中


def test_query_count_does_not_grow_with_rows(client):
    engine = client.app.state.store.engine
    parent = client.post("/todos", json={"title": "父"}).json()["id"]
    client.post("/todos", json={"title": "子", "parent_id": parent})

    def count(n_total: int) -> int:
        while len(client.get("/materials").json()) < n_total:
            mid = add(client, f"材料{len(client.get('/materials').json())}")
            client.post(f"/todos/{parent}/materials", json={"material_id": mid})
        statements = []
        listener = lambda *args: statements.append(args[2])  # noqa: E731
        event.listen(engine, "before_cursor_execute", listener)
        try:
            page(client, limit=100)
        finally:
            event.remove(engine, "before_cursor_execute", listener)
        return len(statements)

    assert count(5) == count(40)
