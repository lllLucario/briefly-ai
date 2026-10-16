"""文件追踪接口（网页“追踪”页使用）。用假模型，不调用真实 API。"""
from fastapi.testclient import TestClient

from briefly.api import create_app
from briefly.tracking.models import ScanLock
from test_tracking_evidence import WEEK
from test_tracking_review import make_model
from tracking_util import env  # noqa: F401


def test_full_flow_through_api(env):
    env.write("项目/方案.md", "# 方案\n原有内容")
    env.write("进度.md", "# 进度\n原有内容")
    model = make_model()
    with TestClient(create_app(env.store, model_factory=lambda: model)) as client:
        assert client.post("/tracking/collections", json={"name": "项目A", "owner_names": ["张三"]}).status_code == 201
        added = client.post("/tracking/collections/项目A/sources", json={"paths": [str(env.work / "项目"), str(env.work / "进度.md")]}).json()
        assert len(added["added"]) == 2 and added["scan"]["stats"]["new_snapshots"] == 2
        [c] = client.get("/tracking/collections").json()
        assert (c["sources"], c["files"], c["schedule"]) == (2, 2, None)

        env.write("项目/方案.md", "# 方案\n原有内容\n补充灰度发布方案")
        env.write("进度.md", "# 进度\n原有内容\n李四整理的会议纪要")
        assert client.post("/tracking/collections/项目A/scan").json()["stats"]["modified"] == 2
        assert [f["versions"] for f in client.get("/tracking/collections/项目A/files").json()] == [2, 2]

        schedule = client.put("/tracking/collections/项目A/schedule", json={"weekday": 4, "time": "16:30"}).json()
        assert schedule["text"] == "每周五 16:30"
        assert client.get("/tracking/collections/项目A/schedule").json()["schedule"]["enabled"]

        start, end = (d.isoformat() for d in WEEK)
        assert client.get("/tracking/collections/项目A/prep", params={"start": start, "end": end}).json() is None
        prep = client.post("/tracking/collections/项目A/prepare", json={"start": start, "end": end}).json()
        assert prep["counts"]["PENDING"] == 2 and prep["build"]["created"] == 2
        ids = {e["source_refs"][0]["path"].replace("\\", "/").rsplit("/", 1)[-1]: e["id"] for e in prep["items"]}

        sources = client.get(f"/tracking/evidence/{ids['方案.md']}/sources").json()
        assert sources[0]["versions"] == "v1 → v2" and any("灰度" in h for h in sources[0]["hunks"])
        pid = prep["id"]
        assert client.post(f"/tracking/preps/{pid}/confirm").status_code == 400  # 还有待确认的
        client.post(f"/tracking/preps/{pid}/review", json={"action": "confirm", "ids": [ids["方案.md"]]})
        client.post(f"/tracking/preps/{pid}/review", json={"action": "exclude", "ids": [ids["进度.md"]], "note": "李四的工作"})
        edited = client.put(f"/tracking/preps/{pid}/evidence/{ids['方案.md']}", json={"description": "编写灰度发布方案"}).json()
        assert edited["user_edited"] and edited["review_status"] == "CONFIRMED"
        manual = client.post(f"/tracking/preps/{pid}/evidence", json={"title": "参加评审会", "description": "确认上线时间", "category": "done"}).json()
        assert manual["review_status"] == "CONFIRMED"
        confirmed = client.post(f"/tracking/preps/{pid}/confirm").json()
        assert confirmed["status"] == "confirmed" and confirmed["counts"] == {"PENDING": 0, "CONFIRMED": 2, "REJECTED": 1}
        assert [h["action"] for h in client.get(f"/tracking/preps/{pid}/history").json()][-1] == "confirm_checklist"

        report = client.post(f"/tracking/preps/{pid}/report", json={}).json()
        assert report["status"] == "done", report
        weekly = client.get(f"/weekly-reports/{report['weekly_report_id']}").json()
        assert "编写灰度发布方案" in weekly["content"] and "李四" not in weekly["content"]


def test_errors_are_readable(env):
    model = make_model()
    with TestClient(create_app(env.store, model_factory=lambda: model)) as client:
        assert client.get("/tracking/collections/不存在/files").status_code == 404
        client.post("/tracking/collections", json={"name": "c"})
        assert client.post("/tracking/collections", json={"name": "c"}).status_code == 400
        assert client.put("/tracking/collections/c/schedule", json={"weekday": 4, "time": "25:00"}).status_code == 400
        assert client.post("/tracking/sources/1/fly").status_code in (400, 404)
        from datetime import datetime, timedelta

        from sqlalchemy.orm import sessionmaker

        with sessionmaker(env.svc.engine)() as s:  # 另一个进程正在扫描
            s.add(ScanLock(collection_id=1, owner="other", expires_at=datetime.now() + timedelta(minutes=5)))
            s.commit()
        assert client.post("/tracking/collections/c/scan").status_code == 409


def test_plan_view_endpoints(env):
    """网页“周报计划”用到的接口：默认设置、本周待确认数、文件类型与最近变化、版本历史与差异、周报带上材料、周报列表标出计划。"""
    env.write("项目/方案.md", "# 方案\n原有内容")
    model = make_model()
    with TestClient(create_app(env.store, model_factory=lambda: model)) as client:
        client.post("/tracking/collections", json={"name": "项目A"})
        client.post("/tracking/collections/项目A/sources", json={"paths": [str(env.work / "项目")]})
        assert client.put("/tracking/collections/项目A/settings", json={"audience": "导师", "author": "王五"}).json() == {"audience": "导师", "author": "王五"}
        [c] = client.get("/tracking/collections").json()
        assert c["settings"] == {"audience": "导师", "author": "王五"} and c["this_week"] is None and c["last_scan"]

        env.write("项目/方案.md", "# 方案\n原有内容\n补充灰度发布方案")
        client.post("/tracking/collections/项目A/scan")
        [f] = client.get("/tracking/collections/项目A/files").json()
        assert f["file_type"] == "md" and f["last_change"] and f["versions"] == 2
        versions = client.get(f"/tracking/files/{f['id']}/versions").json()
        assert [v["version"] for v in versions] == [2, 1] and versions[1]["kind"] == "初始版本"
        diff = client.get(f"/tracking/files/{f['id']}/diff", params={"version": 2}).json()
        assert diff["from_version"] == 1 and any("灰度" in h for h in diff["hunks"])
        assert client.get(f"/tracking/files/{f['id']}/diff", params={"version": 9}).status_code == 404

        start, end = (d.isoformat() for d in WEEK)
        prep = client.post("/tracking/collections/项目A/prepare", json={"start": start, "end": end}).json()
        material = client.post("/materials", json={"title": "周会纪要", "content": "评审会确认上线时间为 10 月 20 日。"}).json()["material"]
        client.post(f"/tracking/preps/{prep['id']}/review", json={"action": "confirm", "ids": [e["id"] for e in prep["items"]]})
        client.post(f"/tracking/preps/{prep['id']}/confirm")
        report = client.post(f"/tracking/preps/{prep['id']}/report", json={"material_ids": [material["id"]], "audience": "导师", "name": "王五"}).json()
        weekly = client.get(f"/weekly-reports/{report['weekly_report_id']}").json()
        assert weekly["material_ids"] == [material["id"]] and weekly["options"]["name"] == "王五"
        listed = {r["id"]: r["plan"] for r in client.get("/weekly-reports").json()}
        assert listed[report["weekly_report_id"]]["name"] == "项目A"

        env.write("项目/方案.md", "# 方案\n原有内容\n补充灰度发布方案\n本周新增压测结论")
        current = client.post("/tracking/collections/项目A/prepare", json={}).json()  # 不传日期：本周
        assert client.get("/tracking/collections").json()[0]["this_week"] == {"id": current["id"], "status": "draft", "pending": current["counts"]["PENDING"]}

        client.delete("/tracking/collections/项目A")  # 删除计划时默认设置一起删除，不报外键错误
        assert client.get("/tracking/collections").json() == []


def test_tracked_files_show_latest_version_in_material_list(env):
    """材料库列出追踪中文件的最新版本并标明属于哪个计划；它随追踪更新，不能在材料库里删除。"""
    env.write("进度.md", "# 进度\n原有内容")
    with TestClient(create_app(env.store, model_factory=make_model)) as client:
        client.post("/tracking/collections", json={"name": "项目A"})
        client.post("/tracking/collections/项目A/sources", json={"paths": [str(env.work / "进度.md")]})
        [m] = client.get("/materials").json()
        assert m["source"] == "tracked" and [t["plan"] for t in m["tracked"]] == ["项目A"]
        r = client.delete(f"/materials/{m['id']}")
        assert r.status_code == 400 and "停止追踪" in r.json()["detail"]

        env.write("进度.md", "# 进度\n原有内容\n新增一行")
        client.post("/tracking/collections/项目A/scan")
        [latest] = client.get("/materials").json()
        assert latest["id"] != m["id"] and "新增一行" in client.get(f"/materials/{latest['id']}").json()["content"]
