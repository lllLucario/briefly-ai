"""文件追踪 Step 4：用户审核与周报对接。用假模型，不调用真实 API。"""
import re

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy.orm import sessionmaker

from briefly.tracking.cli_review import review_loop
from briefly.tracking.models import (
    CHECKLIST_CONFIRMED,
    CONFIRMED,
    DRAFT,
    EXCLUDED,
    MERGED,
    PENDING,
    REJECTED,
    REPORTED,
    SPLIT,
    USER_CONFIRMED,
    WorkEvidence,
)
from briefly.tracking.pipeline import generate_report, prepare
from briefly.tracking.review import ReviewService
from briefly.weekly import WeeklyService
from fakes import FunctionModel, structured
from test_tracking_evidence import WEEK
from tracking_util import env  # noqa: F401


def make_model():
    """提取：每行新增内容一个要点；合成：拼接要点；周报：逐条引用全部工作项编号。"""

    def respond(messages):
        system, user = messages[0].content, messages[-1].content
        if "变化记录中，提取" in system:
            body = user.split("【材料开始】\n", 1)[1].split("\n【材料结束】", 1)[0]
            items = []
            for line in body.splitlines():
                quote = line.split("：", 1)[1] if line.startswith(("新增［", "  改为：")) else ""
                if quote:
                    items.append({"category": "other", "point": f"补充：{quote}", "quote": quote})
            return structured("Extraction", items=items)
        if "候选工作记录" in system:
            points = [line[2:] for line in user.splitlines() if line.startswith("- ")]
            return structured("EvidenceDraft", title="更新文档", description="；".join(points), category="other")
        if "撰写周报" in system:
            refs = re.findall(r"#(\d+) (【[^】]*】[^\n]*)", user)
            lines = "\n".join(f"- {text}〔#{n}〕" for n, text in refs)
            return AIMessage(f"【本周完成】\n{lines}")
        return structured("Extraction", items=[])

    return FunctionModel(respond=respond, calls=[])


@pytest.fixture
def setup(env):
    """三个文件各有一处变化 → 三条候选。"""
    for name in ("方案.md", "进度.md", "同事.md"):
        env.write(name, f"# {name}\n原有内容")
    c = env.svc.create_collection("项目", ["张三"])
    env.svc.add_paths(c.id, [str(env.work / n) for n in ("方案.md", "进度.md", "同事.md")])
    env.write("方案.md", "# 方案.md\n原有内容\n补充灰度发布方案")
    env.write("进度.md", "# 进度.md\n原有内容\n压测完成 50%")
    env.write("同事.md", "# 同事.md\n原有内容\n李四整理的会议纪要")
    model = make_model()
    prep, info = prepare(env.svc, c.id, *WEEK, model_factory=lambda: model)
    review = ReviewService(env.svc, lambda: model)
    items = {e.source_refs[0]["path"].rsplit("\\", 1)[-1].rsplit("/", 1)[-1]: e for e in review.items(prep.id)}
    return env, c, prep, review, items, model


def test_confirm_exclude_edit_add_are_persisted_and_logged(setup):
    env, c, prep, review, items, _ = setup
    review.confirm(prep.id, [items["方案.md"].id])
    review.exclude(prep.id, [items["同事.md"].id], note="李四的工作")
    review.edit(prep.id, items["进度.md"].id, description="压测进度推进到 50%")
    added = review.add(prep.id, "参加支付改造评审会", "确认上线时间", "done")

    got = {e.id: e for e in review.items(prep.id)}
    assert got[items["方案.md"].id].review_status == CONFIRMED and got[items["方案.md"].id].attribution == USER_CONFIRMED
    assert got[items["同事.md"].id].review_status == REJECTED and got[items["同事.md"].id].attribution == EXCLUDED
    edited = got[items["进度.md"].id]
    assert edited.description == "压测进度推进到 50%" and edited.user_edited and edited.review_status == PENDING  # 修改不等于确认
    assert got[added.id].review_status == CONFIRMED and got[added.id].origin == "manual"

    log = review.history(prep.id)
    actions = [r.action for r in log]
    assert actions == ["confirm", "exclude", "edit", "add"]
    edit_log = log[2]
    assert "原有内容" not in edit_log.after["description"] and edit_log.before["description"] != edit_log.after["description"]
    assert log[1].note == "李四的工作"


def test_undo_restores_system_attribution(setup):
    env, c, prep, review, items, _ = setup
    e = items["方案.md"]
    review.exclude(prep.id, [e.id])
    review.reset(prep.id, [e.id])
    back = review.get(e.id)
    assert back.review_status == PENDING and back.attribution == e.attribution


def test_merge_and_split_keep_sources_and_history(setup):
    env, c, prep, review, items, _ = setup
    merged = review.merge(prep.id, [items["方案.md"].id, items["进度.md"].id], title="推进支付改造")
    assert merged.review_status == PENDING and len(merged.source_refs) == 2
    assert {review.get(items["方案.md"].id).review_status, review.get(items["进度.md"].id).review_status} == {MERGED}

    parts = review.split(prep.id, merged.id, [("编写灰度方案", "补充灰度发布方案"), ("推进压测", "压测完成 50%")])
    assert [p.review_status for p in parts] == [PENDING, PENDING]
    assert all(len(p.source_refs) == 2 for p in parts)
    assert review.get(merged.id).review_status == SPLIT
    visible = [e.id for e in review.items(prep.id)]
    assert merged.id not in visible and items["方案.md"].id not in visible
    with pytest.raises(ValueError, match="已被合并、拆分"):
        review.confirm(prep.id, [merged.id])


def test_checklist_requires_all_reviewed_and_reopens_on_change(setup):
    env, c, prep, review, items, _ = setup
    with pytest.raises(ValueError, match="还有 3 条记录待确认"):
        review.confirm_checklist(prep.id)
    assert review.prep(prep.id).status == DRAFT  # 没审完：草稿，不会被当成已确认

    review.confirm(prep.id, [items["方案.md"].id, items["进度.md"].id])
    review.exclude(prep.id, [items["同事.md"].id])
    assert review.confirm_checklist(prep.id).status == CHECKLIST_CONFIRMED

    review.edit(prep.id, items["方案.md"].id, title="编写灰度发布方案")
    assert review.prep(prep.id).status == DRAFT  # 确认后又改：需要重新确认


def test_regenerate_protects_user_edits(setup):
    env, c, prep, review, items, model = setup
    e = items["方案.md"]
    review.edit(prep.id, e.id, description="我自己写的描述")
    with pytest.raises(ValueError, match="你修改过"):
        review.regenerate(prep.id, e.id)
    again = review.regenerate(prep.id, e.id, force=True)
    assert "灰度发布方案" in again.description and not again.user_edited
    assert review.history(prep.id)[-1].action == "regenerate"
    with pytest.raises(ValueError, match="没有可用的文件版本"):
        review.regenerate(prep.id, review.add(prep.id, "会议", "").id, force=True)


def test_sources_show_files_versions_and_changes(setup):
    env, c, prep, review, items, _ = setup
    [view] = review.sources(items["方案.md"].id)
    assert view.path.endswith("方案.md") and view.versions == "v1 → v2"
    assert any("灰度发布方案" in h for h in view.hunks) and view.quotes[0]["quote"] == "补充灰度发布方案"


def test_rescan_does_not_override_user_review(setup):
    env, c, prep, review, items, model = setup
    review.confirm(prep.id, [items["方案.md"].id])
    review.edit(prep.id, items["进度.md"].id, description="我的描述")
    prepare(env.svc, c.id, *WEEK, model_factory=lambda: model)  # 没有新变化，重复准备
    assert review.get(items["方案.md"].id).review_status == CONFIRMED
    assert review.get(items["进度.md"].id).description == "我的描述"
    assert len(review.items(prep.id)) == 3


def test_weekly_report_uses_only_confirmed_evidence(setup):
    env, c, prep, review, items, model = setup
    weekly = WeeklyService(env.store, lambda: model)
    with pytest.raises(ValueError, match="还没有确认"):
        generate_report(env.svc, weekly, prep.id)

    review.confirm(prep.id, [items["方案.md"].id])
    review.edit(prep.id, items["进度.md"].id, description="压测进度推进到 50%")
    review.confirm(prep.id, [items["进度.md"].id])
    review.exclude(prep.id, [items["同事.md"].id])
    manual = review.add(prep.id, "参加支付改造评审会", "确认上线时间", "done")
    review.confirm_checklist(prep.id)

    report = generate_report(env.svc, weekly, prep.id, brief="")

    assert report.status == "done", report.error
    ids = {items["方案.md"].id, items["进度.md"].id, manual.id}
    assert set(report.options["evidence_ids"]) == ids
    assert {i["source"] for i in report.items} == {f"E{i}" for i in ids}  # 被排除的不在
    assert "压测进度推进到 50%" in report.content  # 用户修改后的描述
    assert "李四" not in report.content
    assert {f"〔E{i}〕" for i in ids} <= set(re.findall(r"〔E\d+〕", report.content))  # 出处可追溯到工作记录
    synth = next(m for m in model.calls if "撰写周报" in m[0].content)
    assert "同事.md" not in synth[-1].content
    assert review.prep(prep.id).status == REPORTED and review.prep(prep.id).weekly_report_id == report.id


def test_weekly_rejects_unconfirmed_evidence(setup):
    env, c, prep, review, items, model = setup
    weekly = WeeklyService(env.store, lambda: model)
    with pytest.raises(ValueError, match="未经确认"):
        weekly.create([], "", evidence_ids=[items["方案.md"].id])


def test_withdrawn_evidence_is_warned_not_used(setup):
    env, c, prep, review, items, model = setup
    weekly = WeeklyService(env.store, lambda: model)
    review.confirm(prep.id, [items["方案.md"].id])
    report = weekly.create([], "", evidence_ids=[items["方案.md"].id])
    review.reset(prep.id, [items["方案.md"].id])  # 生成前撤销了确认
    weekly.generate(report.id)
    done = weekly.get(report.id)
    assert any("撤销确认" in w for w in done.generation["warnings"])


def test_cli_review_loop(setup):
    env, c, prep, review, items, model = setup
    a, b, x = items["方案.md"].id, items["进度.md"].id, items["同事.md"].id
    script = iter(
        [
            f"s E{a}",
            f"y {a} E{b}",
            f"n {x}",
            "a",
            "参加评审会",
            "确认上线时间",
            "",
            "done",
            f"e {b}",
            "",
            "压测推进到 50%",
            "",
            "done",
        ]
    )
    out: list[str] = []
    assert review_loop(review, prep.id, ask=lambda _: next(script), out=out.append) == "confirmed"
    text = "\n".join(out)
    assert "灰度发布方案" in text and "已确认 2 条" in text and "已排除 1 条" in text
    assert review.prep(prep.id).status == CHECKLIST_CONFIRMED
    # 第一次 done 时没有待确认项（补充的直接算确认）→ 已确认后退出；剩余输入没有用到
    assert next(script) == f"e {b}"


def test_cli_review_quit_keeps_draft(setup):
    env, c, prep, review, items, _ = setup
    script = iter([f"y {items['方案.md'].id}", "done", "bad", "q"])
    out: list[str] = []
    assert review_loop(review, prep.id, ask=lambda _: next(script), out=out.append) == "draft"
    text = "\n".join(out)
    assert "还有 2 条记录待确认" in text and "不认识的命令" in text
    assert review.prep(prep.id).status == DRAFT
    assert review.get(items["方案.md"].id).review_status == CONFIRMED  # 退出后已做的审核保留


def test_new_change_after_confirmation_reopens_checklist(setup):
    env, c, prep, review, items, model = setup
    review.confirm(prep.id, [e.id for e in items.values()])
    review.confirm_checklist(prep.id)
    env.write("方案.md", "# 方案.md\n原有内容\n补充灰度发布方案\n补充回滚方案")
    prep2, info = prepare(env.svc, c.id, *WEEK, model_factory=lambda: model)
    assert prep2.status == DRAFT and info.build.created
    assert review.get(items["方案.md"].id).has_update
    [new] = [e for e in review.items(prep.id) if e.review_status == PENDING]
    assert "回滚方案" in new.description and "灰度" not in new.description
    with sessionmaker(env.svc.engine)() as s:
        assert s.get(WorkEvidence, items["方案.md"].id).review_status == CONFIRMED
