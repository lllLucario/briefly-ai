"""待办与周报审核清单、周报的联动（需求第 9 节阶段 5 的回归流程）。用假模型，不调用真实 API、真实飞书。"""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlalchemy.orm import Session

from briefly.api import create_app
from briefly.todos.evidence import TodoEvidence
from briefly.todos.models import DOING, DONE, TodoActivity
from briefly.todos.service import ConfirmationRequired, TodoService
from briefly.weekly.models import CONFIRMED, PENDING, SUPERSEDED
from briefly.weekly.review import ReviewService
from briefly.weekly.service import WeeklyService
from test_tracking_evidence import WEEK
from test_tracking_review import make_model
from tracking_util import env  # noqa: F401

LAST_WEEK = (WEEK[0] - timedelta(days=7), WEEK[1] - timedelta(days=7))


@pytest.fixture
def ctx(env):
    model = make_model()
    env.set_owners(["张三"])
    todos = TodoService(env.svc.engine, env.store)
    return env, env.weekly(lambda: model), todos, model


def run_prepare(env, model, period=WEEK):
    prep, info = env.weekly(lambda: model).prepare(period[0])
    review = env.weekly(lambda: model).review()
    return prep, info, review, review.items(prep.id)


def links(env, report_id):
    return TodoEvidence(env.svc.engine, env.store).links_for_report(report_id)


def backdate(env, todo_id, days=7):
    with Session(env.svc.engine) as s:
        for a in s.query(TodoActivity).filter(TodoActivity.todo_id == todo_id):
            a.created_at -= timedelta(days=days)
        s.commit()


def test_parent_and_children_become_checklist_items(ctx):
    """流程 1：父待办 + 多个子待办（不同截止日期、并行推进）→ 每个有活动的待办一条记录，按父待办分组写进周报。"""
    env, weekly, todos, model = ctx
    parent = todos.create("支付改造")
    a = todos.create("接口联调", parent_id=parent.id)
    b = todos.create("压测", parent_id=parent.id)
    todos.set_status(a.id, DOING)
    todos.add_note(a.id, "完成登录接口联调")
    todos.set_status(a.id, DONE)
    todos.set_status(b.id, DOING)
    todos.add_note(b.id, "压测环境已搭好")
    prep, info, review, items = run_prepare(env, model)
    by_title = {e.title: e for e in items}
    assert set(by_title) == {"支付改造", "接口联调", "压测"} and len(info.todos.created) == 3
    assert info.build.calls == 0  # 待办记录不调用模型
    assert by_title["接口联调"].category == "done" and "完成登录接口联调" in by_title["接口联调"].description
    assert by_title["压测"].category == "in_progress"
    assert by_title["支付改造"].category == "plan"  # 只是新建
    assert todos.get(parent.id).status == "todo"  # 子待办完成不会完成父待办
    link = links(env, prep.id)[by_title["接口联调"].id][0]
    assert link["parent_title"] == "支付改造" and link["snapshot"]["status_in_period"] == DONE

    review.confirm(prep.id, [e.id for e in items])
    report = weekly.generate(prep.id)
    assert report.status == "done", report.error
    groups = {i["point"].split("：")[0]: i["group"] for i in report.items}
    assert groups == {"支付改造": "支付改造", "接口联调": "支付改造", "压测": "支付改造"}  # 写作按顶级待办分组
    assert "所属待办：支付改造" in str(model.calls[-1])
    refs = {r["todo"]["title"]: r["group"] for r in report.inputs["todo_refs"]}
    assert refs["压测"] == "支付改造 › 压测"  # 显示用的完整路径仍保留


def test_rerun_is_idempotent_and_reviewed_items_get_increments(ctx):
    env, weekly, todos, model = ctx
    t = todos.create("调研")
    todos.add_note(t.id, "收集竞品数据")
    prep, _, review, [first] = run_prepare(env, model)
    _, info, _, items = run_prepare(env, model)
    assert info.todos.unchanged == [first.id] and [e.id for e in items] == [first.id]

    todos.add_note(t.id, "整理报价")  # 未审核时有新活动：替换旧记录
    _, info, _, items = run_prepare(env, model)
    [second] = items
    assert review.get(first.id).review_status == SUPERSEDED and "整理报价" in second.description and "收集竞品数据" in second.description

    review.confirm(prep.id, [second.id])
    todos.add_note(t.id, "等待财务回复")  # 审核后的新活动：只生成增量记录
    _, info, _, items = run_prepare(env, model)
    third = next(e for e in items if e.id not in (second.id,))
    assert review.get(second.id).has_update and third.review_status == PENDING
    assert third.description == "进展：等待财务回复。" and "increment" in third.flags


def test_linked_tracked_file_change_is_one_item_for_all_todos(ctx):
    """同一份材料的一次变化：只有一条记录，关联到所有直接关联它的待办；文件变化不改待办状态。"""
    env, weekly, todos, model = ctx
    env.write("方案.md", "# 方案\n原有内容")
    env.svc.add_paths([str(env.work / "方案.md")])
    [material] = env.store.list_materials()
    a, b = todos.create("写方案"), todos.create("评审方案")
    todos.set_status(a.id, DONE)
    todos.link(a.id, material.id)
    todos.link(b.id, material.id)
    env.write("方案.md", "# 方案\n原有内容\n补充灰度发布方案")
    prep, _, _, items = run_prepare(env, model)
    files = [e for e in items if e.origin == "generated"]
    assert len(files) == 1
    assert {x["todo_id"] for x in links(env, prep.id)[files[0].id]} == {a.id, b.id}
    assert todos.get(a.id).status == DONE  # 已完成的待办，关联文件又更新了：不自动重开


def test_parent_shared_material_is_not_counted_for_each_child(ctx):
    """流程 4：子待办能看到父待办的共享材料；父材料的变化只关联父待办，不算每个子待办的进展。"""
    env, weekly, todos, model = ctx
    env.write("背景.md", "# 背景\n项目目标")
    env.write("接口.md", "# 接口\n登录接口")
    env.svc.add_paths([str(env.work / "背景.md"), str(env.work / "接口.md")])
    by_name = {m.title.replace("\\", "/").rsplit("/", 1)[-1]: m for m in env.store.list_materials()}
    shared, own = by_name["背景.md"], by_name["接口.md"]
    parent = todos.create("项目")
    a = todos.create("联调", parent_id=parent.id)
    b = todos.create("测试", parent_id=parent.id)
    todos.link(parent.id, shared.id)
    todos.link(a.id, own.id)
    assert [(m.relation, m.from_todo) for m in todos.materials(b.id)] == [("shared", parent.id)]
    backdate(env, parent.id), backdate(env, a.id), backdate(env, b.id)  # 待办本身本周没有活动

    env.write("背景.md", "# 背景\n项目目标\n新增范围说明")
    env.write("接口.md", "# 接口\n登录接口\n支付接口")
    prep, _, _, items = run_prepare(env, model)
    linked = {e.source_refs[0]["path"].replace("\\", "/").rsplit("/", 1)[-1]: links(env, prep.id).get(e.id, []) for e in items}
    assert [(x["todo_id"], x["relation"]) for x in linked["背景.md"]] == [(parent.id, "material")]
    assert linked["背景.md"][0]["snapshot"]["shared_with_children"]
    assert [x["todo_id"] for x in linked["接口.md"]] == [a.id]
    assert all(x["todo_id"] != b.id for xs in linked.values() for x in xs)


def test_two_weeks_record_their_own_activity(ctx):
    """流程 6：长期待办跨两个周期，同一编号；每份周报只有自己周期内的活动。"""
    env, weekly, todos, model = ctx
    t = todos.create("支付改造")
    todos.set_status(t.id, DOING)
    todos.add_note(t.id, "完成方案设计")
    backdate(env, t.id)
    todos.add_note(t.id, "完成接口开发")
    reports = []
    for period in (LAST_WEEK, WEEK):
        prep, _, review, [item] = run_prepare(env, model, period)
        review.confirm(prep.id, [item.id])
        reports.append(weekly.generate(prep.id))
    last, this = (r.items[0]["point"] for r in reports)
    assert "完成方案设计" in last and "完成接口开发" not in last
    assert "完成接口开发" in this and "完成方案设计" not in this
    assert [r.inputs["todo_refs"][0]["todo_id"] for r in reports] == [t.id, t.id]
    assert len(todos.list()) == 1  # 不复制待办


def test_checklist_edits_do_not_touch_the_todo_until_sync_is_confirmed(ctx):
    """流程 7：Checklist 修改本周进度不改待办；本次状态只改本次记录；确认同步后才改待办并记活动。"""
    env, weekly, todos, model = ctx
    t = todos.create("写方案", "原始说明")
    todos.set_status(t.id, DOING)
    prep, _, review, [item] = run_prepare(env, model)
    review.edit(prep.id, item.id, description="方案初稿完成，等待评审")
    bridge = TodoEvidence(env.svc.engine, env.store)
    bridge.set_report_status(prep.id, item.id, t.id, DONE)
    after = review.get(item.id)
    assert after.category == item.category == "in_progress" and after.description == "方案初稿完成，等待评审"  # 记录本身不变
    fresh = todos.get(t.id)
    assert (fresh.status, fresh.description) == (DOING, "原始说明")

    with pytest.raises(ConfirmationRequired, match="进行中改为已完成"):
        bridge.sync_status(prep.id, item.id, t.id)
    assert todos.get(t.id).status == DOING
    bridge.sync_status(prep.id, item.id, t.id, confirm=True)
    assert todos.get(t.id).status == DONE and todos.get(t.id).completed_at is not None
    last = todos.activities([t.id])[-1]
    assert last.event_type == "status_synced" and last.new_value["report_id"] == prep.id
    _, info, _, items = run_prepare(env, model)
    assert info.todos.created == []  # 本次审核里同步的状态不再生成新记录


def test_excluded_item_only_leaves_this_report(ctx):
    env, weekly, todos, model = ctx
    t = todos.create("整理会议纪要")
    material, _ = env.store.add_text("纪要", "会议纪要内容")
    todos.link(t.id, material.id)
    prep, _, review, [item] = run_prepare(env, model)
    review.exclude(prep.id, [item.id])
    review.add(prep.id, "参加评审会")
    report = weekly.generate(prep.id)
    assert "整理会议纪要" not in report.content
    assert todos.get(t.id).archived_at is None and env.store.get(material.id) and todos.materials(t.id)


def test_report_history_keeps_the_snapshot_after_restructuring(ctx):
    """转为独立待办、改标题后，旧周报仍按当时的层级和标题显示；待办能查到关联的周报。"""
    env, weekly, todos, model = ctx
    parent = todos.create("支付改造")
    child = todos.create("接口联调", parent_id=parent.id)
    todos.add_note(child.id, "完成登录接口")
    prep, _, review, items = run_prepare(env, model)
    review.confirm(prep.id, [e.id for e in items])
    report = weekly.generate(prep.id)
    todos.promote(child.id)
    todos.update(child.id, title="接口联调（独立）")
    with TestClient(create_app(env.store, model_factory=lambda: model)) as client:
        refs = client.get(f"/weekly/{report.id}/todos").json()
        mine = next(r for r in refs if r["todo_id"] == child.id)
        assert mine["group"] == "支付改造 › 接口联调" and mine["evidence"]["review_status"] == CONFIRMED
        history = client.get(f"/todos/{child.id}/reports").json()
        assert [h["id"] for h in history] == [report.id]


def test_unlinked_material_changes_still_reach_the_report(ctx):
    """流程 8：没有关联待办的文件变化照原流程进入 Checklist 和周报。"""
    env, weekly, todos, model = ctx
    env.write("进度.md", "# 进度\n原有内容")
    env.svc.add_paths([str(env.work / "进度.md")])
    todos.create("别的事")
    env.write("进度.md", "# 进度\n原有内容\n压测完成 50%")
    prep, _, review, items = run_prepare(env, model)
    file_item = next(e for e in items if e.origin == "generated")
    assert links(env, prep.id).get(file_item.id) is None
    review.confirm(prep.id, [e.id for e in items])
    report = weekly.generate(prep.id)
    assert "压测完成 50%" in report.content


def test_merged_items_keep_their_todos(ctx):
    env, weekly, todos, model = ctx
    a, b = todos.create("A"), todos.create("B")
    prep, _, review, items = run_prepare(env, model)
    merged = review.merge(prep.id, [e.id for e in items], title="A 和 B")
    TodoEvidence(env.svc.engine, env.store).ensure_links(prep)
    assert {x["todo_id"] for x in links(env, prep.id)[merged.id]} == {a.id, b.id}


def test_checklist_api_shows_todos_and_requires_sync_confirmation(ctx):
    env, weekly, todos, model = ctx
    t = todos.create("写方案")
    todos.set_status(t.id, DOING)
    with TestClient(create_app(env.store, model_factory=lambda: model)) as client:
        week = client.post("/weekly/prepare", json={"day": WEEK[0].isoformat()}).json()
        assert week["build"]["from_todos"] == 1
        [item] = week["checklist_items"]
        assert item["todos"][0]["title"] == "写方案" and item["todos"][0]["status"] == DOING
        url = f"/weekly/{week['id']}/items/{item['id']}/todos/{t.id}"
        after = client.put(url, json={"status": "done"}).json()
        assert after["checklist_items"][0]["category"] == "in_progress" and not after["checklist_items"][0]["user_edited"]
        assert after["checklist_items"][0]["todos"][0]["report_status"] == "done"
        r = client.post(f"{url}/sync", json={})
        assert r.status_code == 409 and r.json()["confirm"]
        assert client.get(f"/todos/{t.id}").json()["status"] == DOING
        synced = client.post(f"{url}/sync", json={"confirm": True}).json()
        assert synced["checklist_items"][0]["todos"][0]["synced_at"] and client.get(f"/todos/{t.id}").json()["status"] == DONE


def test_plan_without_files_can_prepare_from_todos(ctx):
    env, weekly, todos, model = ctx
    todos.create("只有待办")
    _, _, _, items = run_prepare(env, model)
    assert [e.title for e in items] == ["只有待办"]


def test_suggested_materials_are_untracked_copies_of_active_todos(ctx):
    env, weekly, todos, model = ctx
    t = todos.create("写方案")
    material, _ = env.store.add_text("参考资料", "参考内容")
    todos.link(t.id, material.id)
    prep, _, _, _ = run_prepare(env, model)
    assert TodoEvidence(env.svc.engine, env.store).suggested_materials(prep) == [material.id]


def test_only_materials_changed_todo_has_no_item(ctx):
    env, weekly, todos, model = ctx
    t = todos.create("项目")
    backdate(env, t.id)
    material, _ = env.store.add_text("资料", "内容")
    todos.link(t.id, material.id)  # 本周只有材料关联，不算工作
    _, _, _, items = run_prepare(env, model)
    assert items == []


def test_activity_window_uses_status_at_period_end(ctx):
    env, weekly, todos, model = ctx
    t = todos.create("调研")
    todos.set_status(t.id, DONE)
    backdate(env, t.id)
    todos.set_status(t.id, DOING)  # 本周重新打开
    with Session(env.svc.engine) as s:
        s.execute(update(TodoActivity).where(TodoActivity.todo_id == t.id, TodoActivity.event_type == "reopened").values(note=""))
        s.commit()
    prep, _, _, [this_week] = run_prepare(env, model)
    assert this_week.category == "in_progress" and "重新打开" in this_week.description
    _, _, _, [last_week] = run_prepare(env, model, LAST_WEEK)
    assert last_week.category == "done"


# ---- 飞书文档（模拟的飞书接口）
@pytest.fixture
def feishu(tmp_path, monkeypatch):
    from briefly import config
    from briefly.db import create_db_engine
    from briefly.materials import MaterialStore
    from briefly.tracking.blobs import BlobStore
    from briefly.tracking.service import TrackingService
    from test_tracking_feishu import API, DOC, Doc
    from fakes import FakeFeishu

    monkeypatch.setattr(config, "TRACKING_DIR", str(tmp_path / "store"))
    monkeypatch.setattr(config, "TRACKING_SETTLE_SECONDS", 0)
    doc = Doc()
    fake = FakeFeishu({
        ("GET", f"{API}/docx/v1/documents/{DOC}"): doc.info,
        ("GET", f"{API}/docx/v1/documents/{DOC}/blocks"): doc.blocks,
        ("POST", f"{API}/drive/v1/metas/batch_query"): {"metas": [{"doc_token": DOC, "latest_modify_user": "ou_me", "owner_id": "ou_me"}]},
        ("GET", f"{API}/contact/v3/users/ou_me"): {"user": {"name": "王五"}},
    })  # fmt: skip
    engine = create_db_engine("sqlite://")
    store = MaterialStore(engine)
    svc = TrackingService(engine, store, BlobStore(tmp_path / "store"), feishu_factory=fake.client)
    yield svc, store, doc
    engine.dispose()


def test_imported_feishu_doc_shows_existing_tracking_without_new_tracking(feishu, tmp_path):
    """流程 3：关联已导入的飞书文档 → 显示已有的追踪状态 → 不新建追踪任务；变化关联到待办。
    流程 9：飞书权限撤销显示为“无法访问”，本机文件被删显示为“不在了”，两者区分。"""
    from briefly.tracking.models import TrackingSource
    from test_tracking_feishu import DOC, URL

    svc, store, doc = feishu
    svc.add_paths([URL])
    imported, _ = store.add_text("周报草稿", "# 周报草稿\n周一：梳理支付回调链路", source="feishu", kind="feishu", report={"feishu": {"kind": "docx", "token": DOC, "url": URL}})
    todos = TodoService(svc.engine, store)
    t = todos.create("支付回调优化")
    todos.link(t.id, imported.id)
    [m] = todos.materials(t.id)
    assert m.track_key == f"feishu://docx/{DOC}" and m.tracking["status"] == "present"
    assert m.current_id != imported.id  # 打开的是追踪中的最新版本
    with Session(svc.engine) as s:
        assert s.query(TrackingSource).count() == 1  # 关联待办不新建追踪

    doc.revision, doc.paragraphs = 2, doc.paragraphs + ["周三：回调耗时降到 200 毫秒"]
    model = make_model()
    prep, _ = WeeklyService(svc.store, lambda: model, tracking=svc).prepare(WEEK[0])
    review = ReviewService(svc, lambda: model)
    file_item = next(e for e in review.items(prep.id) if e.origin == "generated")
    assert [x["todo_id"] for x in TodoEvidence(svc.engine, store).links_for_report(prep.id)[file_item.id]] == [t.id]

    from fakes import feishu_error

    doc.error = feishu_error(1770032, "forBidden", 403)
    svc.scan()
    [m] = todos.materials(t.id)
    assert m.tracking["status"] == "unreachable" and m.tracking["last_error"]
    assert m.current_id is not None  # 无法访问不等于删除：材料仍在

    local = tmp_path / "work" / "方案.md"
    local.parent.mkdir()
    local.write_text("# 方案", encoding="utf-8")
    svc.add_paths([str(local.parent)])
    [version] = [x for x in store.list_materials() if x.title.endswith("方案.md")]
    todos.link(t.id, version.id)
    local.unlink()
    svc.scan()
    statuses = {x.title.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]: x.tracking["status"] for x in todos.materials(t.id)}
    assert statuses["方案.md"] == "missing"


def test_items_of_the_same_parent_are_written_next_to_each_other(ctx):
    """模型基本按给出的顺序写：同一个父待办的记录在交给模型前排在一起，不被别的工作隔开。"""
    env, weekly, todos, model = ctx
    a_parent, b = todos.create("支付改造"), todos.create("数据看板")
    backdate(env, a_parent.id), backdate(env, b.id)
    first = todos.create("接口联调", parent_id=a_parent.id)
    todos.add_note(b.id, "完成订单看板前端")
    second = todos.create("压测", parent_id=a_parent.id)
    todos.add_note(first.id, "完成登录接口")
    todos.add_note(second.id, "压测环境搭好")
    prep, _, review, items = run_prepare(env, model)
    review.confirm(prep.id, [e.id for e in items])
    report = weekly.generate(prep.id)
    order = [i["point"].split("：")[0] for i in report.items]
    assert abs(order.index("接口联调") - order.index("压测")) == 1, order


def test_report_status_is_kept_apart_from_the_item(ctx):
    """本次状态单独保存：不标“你改过”，不改描述和类别；写周报时按它放栏目并写明状态。"""
    env, weekly, todos, model = ctx
    a, b = todos.create("写方案"), todos.create("调研")
    todos.set_status(a.id, DOING)
    todos.set_status(b.id, DOING)
    prep, _, review, items = run_prepare(env, model)
    ids = {e.title: e.id for e in items}
    review.confirm(prep.id, list(ids.values()))
    weekly.generate(prep.id)
    bridge = TodoEvidence(env.svc.engine, env.store)
    bridge.set_report_status(prep.id, ids["写方案"], a.id, DONE)
    bridge.set_report_status(prep.id, ids["调研"], b.id, "todo")
    assert not review.get(ids["写方案"]).user_edited and review.get(ids["写方案"]).category == "in_progress"
    assert weekly.report(prep.id).checklist == "draft"  # 清单确认后有修改：回到待确认
    assert [h.action for h in review.history(prep.id)][-3:] == ["todo_status", "reopen", "todo_status"]
    report = weekly.generate(prep.id)
    by_title = {i["point"].split("：")[0]: i["category"] for i in report.items}
    assert by_title == {"写方案（已完成）": "done", "调研（未开始）": "other"}  # 未开始不写进下周计划
    assert todos.get(a.id).status == DOING  # 待办不变
