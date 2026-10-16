"""审核清单与周报生成：确认、排除、修改、补充、合并、拆分，只用已确认的记录生成。用假模型，不调用真实 API。"""
import re

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy.orm import sessionmaker

from briefly.tracking.models import EXCLUDED, USER_CONFIRMED
from briefly.weekly.cli import review_loop
from briefly.weekly.models import CONFIRMED, CONFIRMED_LIST, DONE, DRAFT, MERGED, PENDING, REJECTED, SPLIT, WorkEvidence
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
    env.set_owners(["张三"])
    env.svc.add_paths([str(env.work / n) for n in ("方案.md", "进度.md", "同事.md")])
    env.write("方案.md", "# 方案.md\n原有内容\n补充灰度发布方案")
    env.write("进度.md", "# 进度.md\n原有内容\n压测完成 50%")
    env.write("同事.md", "# 同事.md\n原有内容\n李四整理的会议纪要")
    model = make_model()
    weekly = env.weekly(lambda: model)
    prep, info = weekly.prepare(WEEK[0])
    review = weekly.review()
    items = {e.source_refs[0]["path"].rsplit("\\", 1)[-1].rsplit("/", 1)[-1]: e for e in review.items(prep.id)}
    return env, weekly, prep, review, items, model


def test_confirm_exclude_edit_add_are_persisted_and_logged(setup):
    env, weekly, prep, review, items, _ = setup
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

    log = [r for r in review.history(prep.id) if r.evidence_id]  # 清单级的记录（整理清单）不算
    actions = [r.action for r in log]
    assert actions == ["confirm", "exclude", "edit", "add"]
    edit_log = log[2]
    assert "原有内容" not in edit_log.after["description"] and edit_log.before["description"] != edit_log.after["description"]
    assert log[1].note == "李四的工作"


def test_undo_restores_system_attribution(setup):
    env, weekly, prep, review, items, _ = setup
    e = items["方案.md"]
    review.exclude(prep.id, [e.id])
    review.reset(prep.id, [e.id])
    back = review.get(e.id)
    assert back.review_status == PENDING and back.attribution == e.attribution


def test_merge_and_split_keep_sources_and_history(setup):
    env, weekly, prep, review, items, _ = setup
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


def test_generation_requires_all_reviewed_and_reopens_on_change(setup):
    env, weekly, prep, review, items, _ = setup
    with pytest.raises(ValueError, match="还有 3 条记录待确认"):
        weekly.start_generation(prep.id)
    assert weekly.report(prep.id).checklist == DRAFT  # 没审完：草稿，不会被当成已确认

    review.confirm(prep.id, [items["方案.md"].id, items["进度.md"].id])
    review.exclude(prep.id, [items["同事.md"].id])
    assert weekly.generate(prep.id).checklist == CONFIRMED_LIST

    review.edit(prep.id, items["方案.md"].id, title="编写灰度发布方案")
    assert weekly.report(prep.id).checklist == DRAFT  # 确认后又改：需要重新生成


def test_regenerate_protects_user_edits(setup):
    env, weekly, prep, review, items, model = setup
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
    env, weekly, prep, review, items, _ = setup
    [view] = review.sources(items["方案.md"].id)
    assert view.path.endswith("方案.md") and view.versions == "v1 → v2"
    assert any("灰度发布方案" in h for h in view.hunks) and view.quotes[0]["quote"] == "补充灰度发布方案"


def test_rescan_does_not_override_user_review(setup):
    env, weekly, prep, review, items, model = setup
    review.confirm(prep.id, [items["方案.md"].id])
    review.edit(prep.id, items["进度.md"].id, description="我的描述")
    env.weekly(lambda: model).prepare(WEEK[0])  # 没有新变化，重复准备
    assert review.get(items["方案.md"].id).review_status == CONFIRMED
    assert review.get(items["进度.md"].id).description == "我的描述"
    assert len(review.items(prep.id)) == 3


def test_weekly_report_uses_only_confirmed_evidence(setup):
    env, weekly, prep, review, items, model = setup
    with pytest.raises(ValueError, match="待确认"):
        weekly.generate(prep.id)

    review.confirm(prep.id, [items["方案.md"].id])
    review.edit(prep.id, items["进度.md"].id, description="压测进度推进到 50%")
    review.confirm(prep.id, [items["进度.md"].id])
    review.exclude(prep.id, [items["同事.md"].id])
    manual = review.add(prep.id, "参加支付改造评审会", "确认上线时间", "done")

    report = weekly.generate(prep.id)

    assert report.status == DONE, report.error
    ids = {items["方案.md"].id, items["进度.md"].id, manual.id}
    assert set(report.inputs["evidence_ids"]) == ids
    assert {i["source"] for i in report.items} == {f"E{i}" for i in ids}  # 被排除的不在
    assert "压测进度推进到 50%" in report.content  # 用户修改后的描述
    assert "李四" not in report.content
    assert {f"〔E{i}〕" for i in ids} <= set(re.findall(r"〔E\d+〕", report.content))  # 出处可追溯到工作记录
    synth = next(m for m in model.calls if "撰写周报" in m[0].content)
    assert "同事.md" not in synth[-1].content
    assert report.id == prep.id and report.checklist == CONFIRMED_LIST  # 同一周就是同一份周报


def test_withdrawn_evidence_is_warned_not_used(setup):
    env, weekly, prep, review, items, model = setup
    review.confirm(prep.id, [e.id for e in items.values()])
    weekly.start_generation(prep.id)
    review.reset(prep.id, [items["方案.md"].id])  # 登记生成之后、真正生成之前撤销了确认
    weekly.run_generation(prep.id)
    done = weekly.report(prep.id)
    assert any("撤销确认" in w for w in done.generation["warnings"])
    assert f"〔E{items['方案.md'].id}〕" not in done.content


def test_cli_review_loop(setup):
    env, weekly, prep, review, items, model = setup
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
    review_loop(review, prep.id, ask=lambda _: next(script), out=out.append)
    text = "\n".join(out)
    assert "灰度发布方案" in text and "已确认 2 条" in text and "已排除 1 条" in text
    assert "生成周报：briefly weekly generate" in text  # 没有待确认项了
    # done 就是退出（审核结果已保存）；剩余输入没有用到
    assert next(script) == f"e {b}"
    assert weekly.generate(prep.id).status == DONE


def test_cli_review_quit_keeps_progress(setup):
    env, weekly, prep, review, items, _ = setup
    script = iter([f"y {items['方案.md'].id}", "bad", "q"])
    out: list[str] = []
    review_loop(review, prep.id, ask=lambda _: next(script), out=out.append)
    text = "\n".join(out)
    assert "还有 2 条待确认" in text and "不认识的命令" in text
    assert weekly.report(prep.id).checklist == DRAFT
    assert review.get(items["方案.md"].id).review_status == CONFIRMED  # 退出后已做的审核保留


def test_new_change_after_confirmation_reopens_checklist(setup):
    env, weekly, prep, review, items, model = setup
    review.confirm(prep.id, [e.id for e in items.values()])
    weekly.generate(prep.id)
    env.write("方案.md", "# 方案.md\n原有内容\n补充灰度发布方案\n补充回滚方案")
    prep2, info = weekly.prepare(WEEK[0])
    assert prep2.id == prep.id and prep2.checklist == DRAFT and info.build.created
    assert review.get(items["方案.md"].id).has_update
    [new] = [e for e in review.items(prep.id) if e.review_status == PENDING]
    assert "回滚方案" in new.description and "灰度" not in new.description
    with sessionmaker(env.svc.engine)() as s:
        assert s.get(WorkEvidence, items["方案.md"].id).review_status == CONFIRMED


def test_confirmed_evidence_left_out_is_rewritten(setup):
    env, weekly, prep, review, items, _ = setup
    ids = [items["方案.md"].id, items["进度.md"].id]
    review.confirm(prep.id, ids)
    review.exclude(prep.id, [items["同事.md"].id])
    calls = []

    def respond(messages):
        if "撰写周报" in messages[0].content:
            calls.append(messages)
            refs = re.findall(r"#(\d+) ", messages[1].content)  # 工作项清单在第一条用户消息里（重写时最后一条是反馈）
            if len(calls) == 1:
                return AIMessage(f"【本周完成】\n- 只写了第一条〔#{refs[0]}〕")
            return AIMessage("【本周完成】\n" + "\n".join(f"- 工作〔#{n}〕" for n in refs))
        return structured("Extraction", items=[])

    report = env.weekly(lambda: FunctionModel(respond=respond, calls=[])).generate(prep.id)
    assert len(calls) == 2 and "是重要内容，没有写进周报" in calls[1][-1].content
    assert set(re.findall(r"〔(E\d+)〕", report.content)) == {f"E{i}" for i in ids}
