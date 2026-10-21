"""彻底删除：树、共享引用、追踪排除和扫描互斥，均使用隔离数据库。"""
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from briefly.todos.service import TodoNotFound, TodoService
from briefly.tracking.models import FileSnapshot, ScanLock, TrackingExclusion
from briefly.weekly.models import WeeklyReport
from tracking_util import env  # noqa: F401


def tracked_task(env):
    env.write("方案.md", "第一版方案")
    env.write("其他.md", "其他独立工作")
    env.svc.add_paths([str(env.work)])
    svc = TodoService(env.store.engine, env.store)
    task = svc.create("主任务")
    file = env.file("方案.md")
    with Session(env.store.engine) as s:
        material = s.scalar(select(FileSnapshot.material_id).where(FileSnapshot.file_id == file.id))
    svc.link(task.id, material)
    return svc, task, file, material


def test_delete_tree_stops_only_orphan_file_and_folder_does_not_readd(env):
    svc, task, file, material = tracked_task(env)
    child = svc.create("子任务", parent_id=task.id)
    result = svc.delete_permanently(task.id)
    assert result["deleted_ids"] == [task.id, child.id]
    assert result["stopped_tracking"] == [file.path_key]
    with pytest.raises(TodoNotFound):
        svc.get(child.id)
    assert env.store.get(material).content == "第一版方案"
    env.write("方案.md", "不再追踪的新内容")
    env.write("其他.md", "其他工作继续更新")
    env.svc.scanner.scan("manual")
    assert env.file("方案.md").status == "untracked"
    with Session(env.store.engine) as s:
        assert len(list(s.scalars(select(FileSnapshot).where(FileSnapshot.file_id == file.id)))) == 1
        assert len(list(s.scalars(select(FileSnapshot).where(FileSnapshot.file_id == env.file("其他.md").id)))) == 2
    assert (env.work / "方案.md").exists()
    assert svc.create("新任务").id > child.id  # 历史编号不能被新任务重用
    # 用户再次明确添加这个文件才恢复，添加文件夹不会取消逐文件排除。
    env.svc.add_paths([str(env.work / "方案.md")])
    assert env.file("方案.md").status == "present"


@pytest.mark.parametrize("reference", ["todo", "archived", "weekly"])
def test_shared_material_keeps_tracking(env, reference):
    svc, task, file, material = tracked_task(env)
    if reference == "weekly":
        with Session(env.store.engine) as s:
            s.add(WeeklyReport(period_start=date(2026, 10, 19), period_end=date(2026, 10, 25), material_ids=[material]))
            s.commit()
    else:
        other = svc.create("其他用途")
        svc.link(other.id, material)
        if reference == "archived":
            svc.archive(other.id)
    assert svc.delete_permanently(task.id)["stopped_tracking"] == []
    assert env.file("方案.md").status == "present"


def test_active_scan_blocks_delete_without_partial_writes(env):
    svc, task, file, material = tracked_task(env)
    with Session(env.store.engine) as s:
        s.add(ScanLock(name="scan", owner="test-scan", expires_at=datetime.now() + timedelta(minutes=5)))
        s.commit()
    with pytest.raises(ValueError, match="稍后重试"):
        svc.delete_permanently(task.id)
    assert svc.get(task.id).title == "主任务"
    with Session(env.store.engine) as s:
        assert s.get(TrackingExclusion, file.path_key) is None
        assert s.get(ScanLock, "scan").owner == "test-scan"


def test_api_requires_explicit_permanent_confirmation():
    from test_todos_api import client

    with client() as c:
        task = c.post("/todos", json={"title": "不能误删"}).json()
        url = f"/todos/{task['id']}/permanent"
        assert c.delete(url).status_code == 409
        assert c.get(f"/todos/{task['id']}").status_code == 200
        assert c.delete(url + "?confirm=true").status_code == 200
        assert c.get(f"/todos/{task['id']}").status_code == 404


def test_delete_failure_rolls_back_relations_and_tracking(env):
    svc, task, file, material = tracked_task(env)

    def fail_delete(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("DELETE FROM todos "):
            raise RuntimeError("模拟事务失败")

    event.listen(env.store.engine, "before_cursor_execute", fail_delete)
    try:
        with pytest.raises(RuntimeError, match="模拟事务失败"):
            svc.delete_permanently(task.id)
    finally:
        event.remove(env.store.engine, "before_cursor_execute", fail_delete)
    assert svc.get(task.id).title == "主任务"
    assert svc.materials(task.id)[0].material_id == material
    assert env.file("方案.md").status == "present"
    with Session(env.store.engine) as s:
        assert s.get(TrackingExclusion, file.path_key) is None
        assert s.get(ScanLock, "scan") is None
