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
