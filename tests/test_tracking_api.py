"""追踪与周报的接口（网页用的）：添加追踪、检查更新、文件版本与差异、一周的审核清单、本周材料与说明、生成、历史版本。

用假模型，不调用真实 API。
"""
from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from briefly.api import create_app
from briefly.tracking.models import SCAN_LOCK, ScanLock
from test_tracking_evidence import WEEK
from test_tracking_review import make_model
from tracking_util import env  # noqa: F401


def wait_done(client, report_id: int) -> dict:
    """生成在后台执行；TestClient 里后台任务随请求一起跑完，这里直接取结果。"""
    return client.get(f"/weekly/{report_id}").json()


def test_full_flow_through_api(env):
    env.write("项目/方案.md", "# 方案\n原有内容")
    env.write("进度.md", "# 进度\n原有内容")
    model = make_model()
    with TestClient(create_app(env.store, model_factory=lambda: model)) as client:
        client.put("/weekly-settings", json={"owner_names": ["张三"], "audience": "导师", "author": "王五"})
        added = client.post("/tracking/sources", json={"paths": [str(env.work / "项目"), str(env.work / "进度.md")]}).json()
        assert len(added["added"]) == 2 and added["scan"]["stats"]["new_snapshots"] == 2
        summary = client.get("/tracking/summary").json()
        assert (summary["sources"], summary["files"]) == (2, 2) and summary["last_scan"]

        env.write("项目/方案.md", "# 方案\n原有内容\n补充灰度发布方案")
        env.write("进度.md", "# 进度\n原有内容\n李四整理的会议纪要")
        assert client.post("/tracking/scan").json()["stats"]["modified"] == 2
        assert [f["versions"] for f in client.get("/tracking/files").json()] == [2, 2]

        empty = client.get("/weekly/week", params={"day": WEEK[0].isoformat()}).json()
        assert empty["id"] is None and empty["period_start"] == WEEK[0].isoformat()  # 还没开始：空的一周
        week = client.post("/weekly/prepare", json={"day": WEEK[0].isoformat()}).json()
        assert week["counts"]["PENDING"] == 2 and week["build"]["created"] == 2
        ids = {e["source_refs"][0]["path"].replace("\\", "/").rsplit("/", 1)[-1]: e["id"] for e in week["checklist_items"]}
        rid = week["id"]

        sources = client.get(f"/weekly/items/{ids['方案.md']}/sources").json()
        assert sources[0]["versions"] == "v1 → v2" and any("灰度" in h for h in sources[0]["hunks"])
        assert client.post(f"/weekly/{rid}/generate", json={}).status_code == 400  # 还有待确认的
        client.post(f"/weekly/{rid}/review", json={"action": "confirm", "ids": [ids["方案.md"]]})
        client.post(f"/weekly/{rid}/review", json={"action": "exclude", "ids": [ids["进度.md"]], "note": "李四的工作"})
        client.put(f"/weekly/{rid}/items/{ids['方案.md']}", json={"description": "编写灰度发布方案"})
        added = client.post(f"/weekly/{rid}/items", json={"title": "参加评审会", "description": "确认上线时间", "category": "done"}).json()
        assert added["counts"] == {"PENDING": 0, "CONFIRMED": 2, "REJECTED": 1}
        material = client.post("/materials", json={"title": "周会纪要", "content": "评审会确认上线时间为 10 月 20 日。"}).json()["material"]
        client.put(f"/weekly/{rid}/materials", json={"material_ids": [material["id"]]})
        client.put(f"/weekly/{rid}/note", json={"note": "这周主要做支付改造"})

        started = client.post(f"/weekly/{rid}/generate", json={})
        assert started.status_code == 202
        done = wait_done(client, rid)
        assert done["status"] == "done" and done["checklist"] == "confirmed"
        assert "编写灰度发布方案" in done["content"] and "李四" not in done["content"]
        assert done["inputs"]["audience"] == "导师" and done["inputs"]["name"] == "王五"  # 默认读者、署名
        assert done["material_ids"] == [material["id"]] and done["note"] == "这周主要做支付改造"
        assert [h["action"] for h in client.get(f"/weekly/{rid}/history").json()][-1] == "generate"

        # 手动修改、重新生成：旧正文存为历史版本，可以恢复
        client.put(f"/weekly/{rid}/content", json={"content": "我改过的周报"})
        client.post(f"/weekly/{rid}/generate", json={})
        versions = client.get(f"/weekly/{rid}/versions").json()
        assert [v["reason"] for v in versions] == ["regenerate", "edit"] and versions[0]["content"] == "我改过的周报"
        restored = client.post(f"/weekly/{rid}/versions/{versions[0]['id']}/restore").json()
        assert restored["content"] == "我改过的周报"
        assert client.get(f"/weekly/{rid}/export.md").text == "我改过的周报\n"
        assert [r["id"] for r in client.get("/weekly").json()] == [rid]  # 同一周只有一份


def test_errors_are_readable(env):
    model = make_model()
    with TestClient(create_app(env.store, model_factory=lambda: model)) as client:
        assert client.get("/weekly/999").status_code == 404
        assert client.get("/tracking/files/999/versions").status_code == 404
        assert client.post("/tracking/sources/999/pause").status_code == 404
        week = client.post("/weekly/week", json={}).json()
        r = client.post(f"/weekly/{week['id']}/generate", json={})
        assert r.status_code == 400 and "无法生成" in r.json()["detail"]
        with sessionmaker(env.svc.engine)() as s:
            s.add(ScanLock(name=SCAN_LOCK, owner="other", expires_at=datetime.now() + timedelta(minutes=5)))
            s.commit()
        assert client.post("/tracking/scan").status_code == 409


def test_file_versions_and_diff(env):
    env.write("项目/方案.md", "# 方案\n原有内容")
    with TestClient(create_app(env.store, model_factory=make_model)) as client:
        client.post("/tracking/sources", json={"paths": [str(env.work / "项目")]})
        env.write("项目/方案.md", "# 方案\n原有内容\n补充灰度发布方案")
        client.post("/tracking/scan")
        [f] = client.get("/tracking/files").json()
        assert f["file_type"] == "md" and f["last_change"] and f["versions"] == 2
        versions = client.get(f"/tracking/files/{f['id']}/versions").json()
        assert [v["version"] for v in versions] == [2, 1] and versions[1]["kind"] == "初始版本"
        diff = client.get(f"/tracking/files/{f['id']}/diff", params={"version": 2}).json()
        assert diff["from_version"] == 1 and any("灰度" in h for h in diff["hunks"])
        assert client.get(f"/tracking/files/{f['id']}/diff", params={"version": 9}).status_code == 404


def test_tracked_files_show_latest_version_in_material_list(env):
    """材料库列出追踪中文件的最新版本；它随追踪更新，不能在材料库里删除。"""
    env.write("进度.md", "# 进度\n原有内容")
    with TestClient(create_app(env.store, model_factory=make_model)) as client:
        client.post("/tracking/sources", json={"paths": [str(env.work / "进度.md")]})
        [m] = client.get("/materials").json()
        assert m["source"] == "tracked" and m["tracked"]["file_id"]
        r = client.delete(f"/materials/{m['id']}")
        assert r.status_code == 400 and "停止追踪" in r.json()["detail"]

        env.write("进度.md", "# 进度\n原有内容\n新增一行")
        client.post("/tracking/scan")
        [latest] = client.get("/materials").json()
        assert latest["id"] != m["id"] and "新增一行" in client.get(f"/materials/{latest['id']}").json()["content"]


def test_only_imported_feishu_docs_can_be_tracked_from_materials(env):
    with TestClient(create_app(env.store, model_factory=make_model)) as client:
        m = client.post("/materials", json={"title": "笔记", "content": "粘贴的内容"}).json()["material"]
        r = client.post(f"/materials/{m['id']}/track")
        assert r.status_code == 400 and "飞书文档" in r.json()["detail"]
