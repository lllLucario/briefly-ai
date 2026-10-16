"""文件追踪 Step 3：工作归属与候选工作记录。用假模型（FunctionModel），不调用真实 API。"""
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.orm import sessionmaker

from briefly import config
from briefly.tracking.attribution import judge
from briefly.tracking.evidence import EvidenceBuilder
from briefly.tracking.models import (
    ATTRIBUTED,
    CONFIRMED,
    INFERRED,
    MERGED,
    PENDING,
    SUPERSEDED,
    UNKNOWN,
    FileSnapshot,
    WorkEvidence,
)
from briefly.tracking.pipeline import get_prep, prepare
from briefly.tokens import estimate_tokens
from fakes import FunctionModel, structured
from tracking_util import docx_bytes, docx_with_revisions, env  # noqa: F401

TODAY = date.today()
WEEK = (TODAY - timedelta(days=TODAY.weekday()), TODAY - timedelta(days=TODAY.weekday()) + timedelta(days=6))


def fake_model(items: bool = True):
    """提取：每行“新增/改为”的内容各算一个要点；合成：把要点拼成描述。"""

    def respond(messages):
        system, user = messages[0].content, messages[-1].content
        if "变化记录中，提取" in system:
            if not items:
                return structured("Extraction", items=[])
            body = user.split("【材料开始】\n", 1)[1].split("\n【材料结束】", 1)[0]
            found = []
            for line in body.splitlines():
                if line.startswith("删除［"):
                    continue
                if line.startswith(("新增［", "修改［", "新增：", "  改为：")):
                    quote = line.split("：", 1)[1]
                else:
                    quote = line.strip()  # 一段新增内容的后续行
                if line.startswith("修改［") or not quote:
                    continue  # 修改的原文不是新工作，取“改为”那一行
                found.append({"category": "other", "point": f"补充：{quote[:30]}", "quote": quote})
            return structured("Extraction", items=found)
        if "候选工作记录" in system:
            points = [line[2:] for line in user.splitlines() if line.startswith("- ")]
            return structured("EvidenceDraft", title="更新文档内容", description="；".join(points), category="other")
        raise AssertionError(f"未预期的调用：{system[:40]}")

    return FunctionModel(respond=respond, calls=[])


def backdate(env, file_id: int, days: int, versions: list[int] | None = None) -> None:
    """把文件的版本采集时间挪到 days 天前（模拟上周就开始追踪）。"""
    with sessionmaker(env.svc.engine)() as s:
        query = update(FileSnapshot).where(FileSnapshot.file_id == file_id)
        if versions:
            query = query.where(FileSnapshot.version.in_(versions))
        s.execute(query.values(captured_at=datetime.now() - timedelta(days=days)))
        s.commit()


def run_prepare(env, c, model=None, scan=True):
    model = model or fake_model()
    prep, info = prepare(env.svc, c.id, *WEEK, scan=scan, model_factory=lambda: model)
    return prep, info, model


def evidence(env, prep, status=None):
    with sessionmaker(env.svc.engine)() as s:
        rows = s.query(WorkEvidence).filter(WorkEvidence.prep_id == prep.id).order_by(WorkEvidence.id).all()
    return [e for e in rows if status is None or e.review_status == status]


def test_takeover_mid_week_only_counts_changes_after_baseline(env):
    env.write("方案.md", "# 方案\n## 背景\n接手前就有的背景说明。")
    c = env.svc.create_collection("c", ["张三"])
    env.svc.add_paths(c.id, [str(env.work / "方案.md")])  # 周三接手：立即保存基线
    env.write("方案.md", "# 方案\n## 背景\n接手前就有的背景说明。\n## 架构\n新增异步队列设计。")

    prep, info, _ = run_prepare(env, c)  # 周五准备周报

    [e] = evidence(env, prep, PENDING)
    ref = e.source_refs[0]
    baseline = env.svc.snapshots(ref["file_id"])[0]
    assert ref["from_snapshot_id"] == baseline.id and baseline.kind == "baseline"
    assert "异步队列" in e.description and "背景说明" not in e.description  # 接手前的内容不算本周工作
    assert any("开始追踪" in n for n in ref["notes"])
    assert e.attribution == UNKNOWN and e.review_status == PENDING


def test_existing_content_without_changes_is_not_new_work(env):
    env.write("旧文档.md", "# 旧文档\n很早以前写好的内容。")
    c = env.svc.create_collection("c")
    env.svc.add_paths(c.id, [str(env.work / "旧文档.md")])
    backdate(env, env.file(c, "旧文档.md").id, 20)  # 上上周开始追踪，本周没改
    prep, info, model = run_prepare(env, c)
    assert evidence(env, prep) == [] and model.calls == []


def test_added_on_thursday_removed_on_friday_is_still_visible(env):
    env.write("计划.md", "# 计划\n上线时间待定。")
    c = env.svc.create_collection("c")
    env.svc.add_paths(c.id, [str(env.work / "计划.md")])
    f = env.file(c, "计划.md")
    backdate(env, f.id, 20)
    env.write("计划.md", "# 计划\n上线时间待定。\n临时方案：先灰度 10% 流量。")  # 周四
    env.svc.scan(c.id)
    env.write("计划.md", "# 计划\n上线时间待定。")  # 周五删掉
    env.svc.scan(c.id)

    prep, _, _ = run_prepare(env, c, scan=False)

    assert [s.version for s in env.svc.snapshots(f.id)] == [1, 2, 3]  # 历史版本都在
    [e] = evidence(env, prep)
    assert "reverted" in e.flags and "灰度 10%" in e.description


def test_first_seen_file_is_flagged(env):
    env.write("d/a.md", "# A")
    c = env.svc.create_collection("c")
    env.svc.add_paths(c.id, [str(env.work / "d")])
    backdate(env, env.file(c, "a.md").id, 20)
    env.write("d/新文件.md", "# 新文件\n周内新建的说明。")
    prep, _, _ = run_prepare(env, c)
    [e] = evidence(env, prep)
    assert "first_seen" in e.flags and "无法确认创建时间" in e.source_refs[0]["notes"][0]


def test_moved_file_without_content_change_is_not_evidence(env):
    env.write("d/报告.md", "# 报告\n内容")
    c = env.svc.create_collection("c")
    env.svc.add_paths(c.id, [str(env.work / "d")])
    backdate(env, env.file(c, "报告.md").id, 20)
    (env.work / "d" / "报告.md").rename(env.work / "d" / "报告-改名.md")
    prep, _, _ = run_prepare(env, c)
    assert [e for e in evidence(env, prep) if "deleted" not in e.flags] == []


def test_repeated_prepare_is_idempotent(env):
    env.write("a.md", "# A\n一")
    c = env.svc.create_collection("c")
    env.svc.add_paths(c.id, [str(env.work / "a.md")])
    env.write("a.md", "# A\n一\n二")
    prep, info, model = run_prepare(env, c)
    calls = len(model.calls)
    prep2, info2, model2 = run_prepare(env, c, model=model)
    assert prep2.id == prep.id and info2.build.created == [] and info2.build.unchanged == info.build.created
    assert len(model.calls) == calls  # 没有重复调用模型
    assert len(evidence(env, prep)) == 1


def test_pending_evidence_is_superseded_but_reviewed_is_kept(env):
    env.write("a.md", "# A\n一")
    c = env.svc.create_collection("c")
    env.svc.add_paths(c.id, [str(env.work / "a.md")])
    env.write("a.md", "# A\n一\n二")
    prep, _, _ = run_prepare(env, c)
    [first] = evidence(env, prep)

    env.write("a.md", "# A\n一\n二\n三")  # 未审核时又有变化：替换
    run_prepare(env, c)
    statuses = {e.id: e.review_status for e in evidence(env, prep)}
    assert statuses[first.id] == SUPERSEDED
    [second] = evidence(env, prep, PENDING)
    assert "三" in second.description and "二" in second.description

    with sessionmaker(env.svc.engine)() as s:  # 用户确认并修改了描述
        s.execute(update(WorkEvidence).where(WorkEvidence.id == second.id).values(review_status=CONFIRMED, description="我改过的描述", user_edited=True))
        s.commit()
    env.write("a.md", "# A\n一\n二\n三\n四")
    run_prepare(env, c)

    kept = next(e for e in evidence(env, prep) if e.id == second.id)
    assert kept.review_status == CONFIRMED and kept.description == "我改过的描述" and kept.has_update
    [delta] = evidence(env, prep, PENDING)
    assert "四" in delta.description and "三" not in delta.description  # 只包含确认之后的新变化
    assert delta.source_refs[0]["from_snapshot_id"] == kept.source_refs[0]["to_snapshot_id"]
    assert delta.id in kept.related_ids


def test_large_diff_respects_token_budget(env, monkeypatch):
    monkeypatch.setattr(config, "TRACKING_DIFF_BATCH_TOKENS", 150)
    env.write("log.txt", "开头")
    c = env.svc.create_collection("c")
    env.svc.add_paths(c.id, [str(env.work / "log.txt")])
    env.write("log.txt", "开头\n" + "\n".join(f"第 {i} 项：完成接口 {i} 的联调和自测，记录在案。" for i in range(60)))
    prep, info, model = run_prepare(env, c)

    extract_calls = [m for m in model.calls if "变化记录中，提取" in m[0].content]
    assert len(extract_calls) > 3  # 分成多批
    for messages in extract_calls:
        assert estimate_tokens(messages[-1].content) < 150 + 200  # 每批不超过预算（另加固定的说明文字）
    [e] = evidence(env, prep)
    assert len(e.source_refs[0]["quotes"]) <= 20


def test_parse_failure_gives_explicit_status_without_model(env):
    env.write("a.docx", docx_bytes(["正文"]))
    c = env.svc.create_collection("c")
    env.svc.add_paths(c.id, [str(env.work / "a.docx")])
    env.write("a.docx", b"broken file")
    prep, _, model = run_prepare(env, c)
    [e] = evidence(env, prep)
    assert "parse_failed" in e.flags and "解析失败" in e.description and model.calls == []


def test_no_extracted_points_falls_back_to_factual_description(env):
    env.write("a.md", "# A\n## 进度\n接口联调进行中")
    c = env.svc.create_collection("c")
    env.svc.add_paths(c.id, [str(env.work / "a.md")])
    env.write("a.md", "# A\n## 进度\n接口联调进行中，约一半")
    prep, _, _ = run_prepare(env, c, model=fake_model(items=False))
    [e] = evidence(env, prep)
    assert e.description.startswith("《a.md》修改 1 处") and "请查看变化后补充描述" in e.description


def test_near_duplicate_evidence_is_merged_with_both_sources(env):
    env.write("a.md", "# 方案\n一")
    env.write("b.md", "# 方案\n一")  # 同一内容的两份
    c = env.svc.create_collection("c")
    env.svc.add_paths(c.id, [str(env.work / "a.md"), str(env.work / "b.md")])
    env.write("a.md", "# 方案\n一\n新增灰度发布方案")
    env.write("b.md", "# 方案\n一\n新增灰度发布方案")
    prep, _, _ = run_prepare(env, c)
    [kept] = evidence(env, prep, PENDING)
    assert len(kept.source_refs) == 2
    assert len(evidence(env, prep, MERGED)) == 1


def test_done_without_completion_wording_is_downgraded():
    from briefly.tracking.diff import Hunk
    from briefly.tracking.evidence import guard_done

    assert guard_done("done", [Hunk("added", "架构", new="新增异步队列设计")]) == ("other", True)
    assert guard_done("done", [Hunk("modified", "进度", old="联调进行中", new="联调已完成")]) == ("done", False)
    assert guard_done("done", [Hunk("removed", "进度", old="联调已完成")]) == ("other", True)  # 删除的“完成”不算
    assert guard_done("in_progress", []) == ("in_progress", False)


def test_deleted_file_becomes_candidate(env):
    env.write("d/a.md", "# A")
    c = env.svc.create_collection("c")
    env.svc.add_paths(c.id, [str(env.work / "d")])
    (env.work / "d" / "a.md").unlink()
    prep, _, _ = run_prepare(env, c)
    [e] = evidence(env, prep)
    assert "deleted" in e.flags and e.attribution == UNKNOWN


# ---- 归属
def test_attribution_from_word_revisions(env):
    env.write("方案.docx", docx_with_revisions("原有正文", []))
    c = env.svc.create_collection("c", ["张三"])
    env.svc.add_paths(c.id, [str(env.work / "方案.docx")])
    env.write("方案.docx", docx_with_revisions("原有正文", [("张三", "补充架构设计")]))
    prep, _, _ = run_prepare(env, c)
    [e] = evidence(env, prep)
    assert e.attribution == ATTRIBUTED and "作者均为 张三" in e.attribution_basis[0]
    assert e.review_status == PENDING  # 有证据也要用户确认


@pytest.mark.parametrize(
    ("owners", "before", "after", "status", "flags", "basis"),
    [
        (["张三"], {}, {"revisions": [{"type": "insert", "author": "李四", "text": "x"}]}, UNKNOWN, ["others"], "李四"),
        (
            ["张三"],
            {},
            {"revisions": [{"type": "insert", "author": "张三", "text": "a"}, {"type": "insert", "author": "李四", "text": "b"}]},
            ATTRIBUTED,
            ["mixed", "others"],
            "请确认描述只包含你的部分",
        ),
        (["张三"], {}, {"last_modified_by": "张三"}, INFERRED, [], "不能证明全部修改都是你做的"),
        (["张三"], {}, {"last_modified_by": "王五"}, UNKNOWN, ["others"], "不是你的署名"),
        (["张三"], {}, {}, UNKNOWN, [], "修改时间和追踪时间不能说明是谁做的"),
        ([], {}, {"revisions": [{"type": "insert", "author": "张三", "text": "a"}]}, UNKNOWN, ["no_owner", "others"], "未设置你在文档里的名字"),
        (["张三"], {}, {"revisions": [{"type": "insert", "author": "", "text": "a"}]}, UNKNOWN, [], "没有作者信息"),
    ],
)
def test_attribution_rules(owners, before, after, status, flags, basis):
    got, got_basis, got_flags = judge(owners, before, after)
    assert got == status and got_flags == flags and basis in "".join(got_basis)


def test_old_revisions_are_not_counted_again():
    old = {"revisions": [{"type": "insert", "author": "李四", "date": "d1", "text": "旧"}]}
    new = {"revisions": [*old["revisions"], {"type": "insert", "author": "张三", "date": "d2", "text": "新"}]}
    status, basis, flags = judge(["张三"], old, new)
    assert status == ATTRIBUTED and flags == []  # 李四的修订在基线里就有，不算本次


def test_prep_period_validation(env):
    env.svc.create_collection("c")
    with pytest.raises(ValueError, match="不能晚于"):
        get_prep(env.svc, "c", date(2026, 10, 16), date(2026, 10, 12))
    with pytest.raises(ValueError, match="还没有周报准备"):
        get_prep(env.svc, "c", create=False)
    assert EvidenceBuilder  # 模块可导入
