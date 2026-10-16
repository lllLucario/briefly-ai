"""删除材料：被待办关联时需确认；确认后在一个事务里清理关联、活动引用、周报引用；外键约束在数据库层生效。"""
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from briefly.db import create_db_engine, foreign_key_problems
from briefly.materials import MaterialNotFound, MaterialStore
from briefly.todos.service import ConfirmationRequired, TodoService
from briefly.weekly.service import WeeklyService
from fakes import fake
from weekly_util import draft


@pytest.fixture
def store():
    engine = create_db_engine("sqlite://")
    yield MaterialStore(engine)
    engine.dispose()


def test_unlinked_material_is_deleted_directly(store):
    m, _ = store.add_text("笔记", "笔记内容\n\n## 第二节\n内容")
    store.delete(m.id)
    with pytest.raises(MaterialNotFound):
        store.get(m.id)
    with store.engine.connect() as conn:  # 章节、分块随材料删除
        assert conn.execute(text("select count(*) from material_chunks")).scalar() == 0
        assert conn.execute(text("select count(*) from material_sections")).scalar() == 0


def test_linked_material_needs_force_and_is_cleaned_up(store):
    todos = TodoService(store.engine, store)
    m, _ = store.add_text("方案", "方案内容")
    a, b = todos.create("写方案"), todos.create("评审")
    todos.link(a.id, m.id)
    todos.link(b.id, m.id)
    weekly = WeeklyService(store, lambda: fake())
    report = draft(weekly, [m.id], "简述")
    with pytest.raises(ConfirmationRequired, match="2 个待办"):
        store.delete(m.id)
    assert store.get(m.id) and len(todos.direct_links()) == 2  # 未确认：什么都没动

    store.delete(m.id, force=True)
    assert todos.direct_links() == []
    for t in (a, b):
        acts = todos.activities([t.id])
        assert acts[-1].event_type == "material_deleted" and acts[-1].old_value == {"title": "方案"}
        assert all(x.material_id is None for x in acts)  # 不留指向已删材料的编号；标题仍在活动内容里
        assert acts[1].event_type == "material_linked" and acts[1].new_value == {"title": "方案"}
    kept = weekly.report(report.id)
    assert kept.material_ids == [] and kept.note == "简述"
    assert kept.removed_materials[0]["id"] == m.id and kept.removed_materials[0]["title"] == "方案"
    assert foreign_key_problems(store.engine) == {}


def test_feishu_material_deletion_is_local_only(store):
    """飞书导入的材料：只删本地记录（删除流程不创建飞书客户端，也就不会调用飞书）。"""
    m, _ = store.add_text("周报草稿", "飞书内容", source="feishu", kind="feishu", report={"feishu": {"kind": "docx", "token": "doxcn1", "url": "https://x.feishu.cn/docx/doxcn1"}})
    store.delete(m.id)
    with pytest.raises(MaterialNotFound):
        store.get(m.id)


def test_database_rejects_dangling_references(store):
    """外键约束在数据库层生效：绕过服务直接写也不能关联不存在的材料或待办。"""
    with store.engine.connect() as conn:
        with pytest.raises(IntegrityError):
            conn.execute(text("INSERT INTO todo_materials (todo_id, material_id, relation, created_at) VALUES (999, 999, 'direct', '2026-10-17')"))
        conn.rollback()
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
