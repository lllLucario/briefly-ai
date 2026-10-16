"""待办：增删改查、严格两级、转为独立、归档、状态提示、材料关联与去重、活动与时间线。不调用模型。"""
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError, IntegrityError

from briefly.db import create_db_engine
from briefly.materials import MaterialStore
from briefly.todos.models import DOING, DONE, TODO, Todo
from briefly.todos.service import ConfirmationRequired, TodoNotFound, TodoService
from tracking_util import env  # noqa: F401


@pytest.fixture
def todos():
    engine = create_db_engine("sqlite://")
    store = MaterialStore(engine)
    yield TodoService(engine, store)
    engine.dispose()


def events(svc, todo_id):
    return [a.event_type for a in svc.activities([todo_id])]


def test_create_and_edit(todos):
    t = todos.create("支付改造", "Q4 上线", due_date=date(2026, 11, 30))
    assert (t.status, t.parent_id, t.completed_at) == (TODO, None, None)
    t = todos.update(t.id, title="支付改造二期", due_date=None)
    assert t.title == "支付改造二期" and t.due_date is None
    [created, updated] = todos.activities([t.id])
    assert created.event_type == "created" and updated.old_value == {"title": "支付改造", "due_date": "2026-11-30"}
    with pytest.raises(ValueError):
        todos.create("  ")
    with pytest.raises(TodoNotFound):
        todos.get(999)


def test_children_two_levels_only(todos):
    parent = todos.create("支付改造")
    a = todos.create("接口联调", parent_id=parent.id, due_date=date(2026, 10, 20))
    b = todos.create("压测", parent_id=parent.id, due_date=date(2026, 11, 10))
    assert [k.id for k in todos.children(parent.id)] == [a.id, b.id]
    assert events(todos, parent.id) == ["created", "child_added", "child_added"]
    with pytest.raises(ValueError, match="转为独立待办"):
        todos.create("第三级", parent_id=a.id)
    with pytest.raises(ValueError, match="最多两级"):
        todos.update(parent.id, parent_id=todos.create("别的项目").id)  # 有子待办的不能再变成子待办
    loose = todos.create("零散任务")
    with pytest.raises(ValueError, match="自己"):
        todos.update(loose.id, parent_id=loose.id)
    with pytest.raises(ValueError, match="先转为独立待办"):
        todos.update(b.id, parent_id=loose.id)
    with pytest.raises(ValueError, match="跟随父待办"):
        todos.update(a.id, collection_id=1)


def test_database_rejects_third_level_and_self_parent(todos):
    """绕过服务层直接写库也不能产生第三级或自引用（SQLite 触发器与检查约束）。"""
    parent = todos.create("项目")
    child = todos.create("子任务", parent_id=parent.id)
    with todos.engine.connect() as conn:
        with pytest.raises(DatabaseError):
            conn.execute(text("INSERT INTO todos (title, description, status, parent_id, created_at, updated_at) VALUES ('x', '', 'todo', :p, '2026-10-16', '2026-10-16')"), {"p": child.id})
        conn.rollback()
        with pytest.raises(DatabaseError):
            conn.execute(text("UPDATE todos SET parent_id = :c WHERE id = :p"), {"c": child.id, "p": parent.id})  # 形成循环
        conn.rollback()
        with pytest.raises(IntegrityError):
            conn.execute(text("UPDATE todos SET parent_id = id WHERE id = :p"), {"p": parent.id})
        conn.rollback()


def test_move_top_level_under_parent(todos):
    parent, loose = todos.create("项目"), todos.create("零散任务")
    moved = todos.update(loose.id, parent_id=parent.id)
    assert moved.parent_id == parent.id


def test_statuses_are_independent(todos):
    parent = todos.create("项目")
    a = todos.create("A", parent_id=parent.id)
    b = todos.create("B", parent_id=parent.id)
    _, notices = todos.set_status(a.id, DONE)
    assert notices == [] and todos.get(parent.id).status == TODO
    assert todos.progress(parent.id) == (1, 2)
    _, notices = todos.set_status(b.id, DONE)
    assert "都已完成" in notices[0] and todos.get(parent.id).status == TODO  # 只提醒，不自动完成


def test_completing_parent_with_open_children_needs_confirmation(todos):
    parent = todos.create("项目")
    child = todos.create("子任务", parent_id=parent.id)
    with pytest.raises(ConfirmationRequired, match="子任务"):
        todos.set_status(parent.id, DONE)
    todos.set_status(parent.id, DONE, confirm=True)
    assert todos.get(parent.id).status == DONE and todos.get(child.id).status == TODO


def test_complete_and_reopen_keeps_history(todos):
    t = todos.create("写方案")
    todos.set_status(t.id, DOING)
    done, _ = todos.set_status(t.id, DONE)
    finished = done.completed_at
    assert finished is not None
    reopened, _ = todos.set_status(t.id, DOING)
    assert reopened.completed_at is None
    acts = todos.activities([t.id])
    assert [a.event_type for a in acts] == ["created", "status", "completed", "reopened"]
    assert acts[-1].old_value == {"status": DONE, "completed_at": finished.isoformat(timespec="seconds")}


def test_promote_keeps_id_materials_and_history(todos):
    store = todos.store
    shared, _ = store.add_text("项目背景", "项目背景内容")
    own, _ = store.add_text("接口文档", "接口文档内容")
    parent = todos.create("项目")
    child = todos.create("接口联调", parent_id=parent.id, status=DOING)
    todos.link(parent.id, shared.id)
    todos.link(child.id, own.id)
    todos.add_note(child.id, "完成登录接口")
    before = [m.relation for m in todos.materials(child.id)]
    assert before == ["direct", "shared"]

    promoted = todos.promote(child.id)
    assert promoted.id == child.id and promoted.parent_id is None and promoted.status == DOING
    assert [(m.material_id, m.relation) for m in todos.materials(child.id)] == [(own.id, "direct")]  # 共享材料不复制
    assert events(todos, child.id) == ["created", "material_linked", "note", "promoted"]
    assert events(todos, parent.id)[-1] == "child_removed"
    todos.create("新的子任务", parent_id=child.id)  # 转为独立后可以有子待办


def test_promote_keeps_parent_plan(env):
    c = env.svc.create_collection("项目A")
    todos = TodoService(env.svc.engine, env.store)
    parent = todos.create("项目", collection_id=c.id)
    child = todos.create("子任务", parent_id=parent.id)
    assert todos.plan_of(child) == c.id
    assert todos.promote(child.id).collection_id == c.id


def test_archive_never_cascades_silently(todos):
    material, _ = todos.store.add_text("方案", "方案内容")
    parent = todos.create("项目")
    a = todos.create("A", parent_id=parent.id)
    b = todos.create("B", parent_id=parent.id)
    todos.link(parent.id, material.id)
    with pytest.raises(ConfirmationRequired, match="2 个子待办"):
        todos.archive(parent.id)
    todos.archive(parent.id, children="promote")
    assert todos.get(parent.id).archived_at is not None
    assert todos.get(a.id).parent_id is None and todos.get(a.id).archived_at is None
    assert todos.store.get(material.id)  # 材料不受影响
    assert [m.material_id for m in todos.materials(parent.id)] == [material.id]  # 关联也保留
    assert parent.id not in [t.id for t in todos.list()] and parent.id in [t.id for t in todos.list(include_archived=True)]
    with pytest.raises(ValueError, match="已归档"):
        todos.add_note(parent.id, "x")
    todos.restore(parent.id)
    assert todos.get(parent.id).archived_at is None

    other = todos.create("项目2")
    c = todos.create("C", parent_id=other.id)
    todos.archive(other.id, children="archive")
    assert todos.get(c.id).archived_at is not None
    with pytest.raises(ValueError, match="先恢复父待办"):
        todos.restore(c.id)
    assert b.id in [t.id for t in todos.list()]


def test_link_material_is_idempotent_and_shared_across_todos(todos):
    a, b = todos.create("A"), todos.create("B")
    material, created, _ = todos.upload(a.id, "方案.md", "# 方案\n内容".encode())
    assert created
    again, created, _ = todos.upload(b.id, "方案.md", "# 方案\n内容".encode())  # 同一个文件：不再存一份
    assert again.id == material.id and not created
    assert len(todos.store.list_materials()) == 1
    _, new = todos.link(a.id, material.id)
    assert not new and len(todos.direct_links([a.id])) == 1
    assert {t.id for t in todos.todos_of_material(material.id)} == {a.id, b.id}
    todos.unlink(a.id, material.id)
    assert todos.store.get(material.id) and todos.materials(a.id) == []
    assert events(todos, a.id) == ["created", "material_linked", "material_unlinked"]
    with pytest.raises(TodoNotFound):
        todos.unlink(a.id, material.id)


def test_notes_and_time_window(todos):
    t = todos.create("调研")
    todos.add_note(t.id, "完成竞品数据收集，等待财务提供报价")
    with pytest.raises(ValueError):
        todos.add_note(t.id, "  ")
    now = datetime.now()
    assert [a.note for a in todos.activities([t.id], now - timedelta(hours=1), now + timedelta(hours=1)) if a.note] == ["完成竞品数据收集，等待财务提供报价"]
    assert todos.activities([t.id], now + timedelta(hours=1)) == []


def test_tracked_file_link_follows_latest_version(env):
    """关联追踪中文件的某个版本：显示时换成最新版本，追踪状态可见；时间线引用文件变化（不复制内容）。"""
    env.write("方案.md", "# 方案\n第一版")
    c = env.svc.create_collection("项目A")
    env.svc.add_paths(c.id, [str(env.work / "方案.md")])
    todos = TodoService(env.svc.engine, env.store)
    t = todos.create("写方案")
    [v1] = env.store.list_materials()
    todos.link(t.id, v1.id)
    env.write("方案.md", "# 方案\n第一版\n第二版补充")
    env.svc.scan(c.id)
    [v2] = env.store.list_materials()
    assert v2.id != v1.id
    [m] = todos.materials(t.id)
    assert (m.material_id, m.current_id) == (v1.id, v2.id)
    assert m.tracking[0]["plan"] == "项目A" and m.tracking[0]["status"] == "present"
    _, new = todos.link(t.id, v2.id)  # 同一个文件的新版本：不重复关联
    assert not new and len(todos.direct_links([t.id])) == 1
    changes = [x for x in todos.timeline(t.id) if x["kind"] == "file_change"]
    assert [(x["change_type"], x["from_version"], x["to_version"], x["relation"]) for x in changes] == [("MODIFIED", 1, 2, "direct")]
    assert "第二版补充" not in str(changes)
