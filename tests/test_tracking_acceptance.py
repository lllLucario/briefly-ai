"""周报验收场景（假模型）：混合追踪 → 基线 → 每日扫描 → 周五整理审核清单 → 审核 → 生成周报。

真实模型的同一场景见 tests/eval/tracking_walkthrough.py（手动运行，结果另行报告）。
"""
import re

from briefly.tracking import scanner as scanner_module
from briefly.tracking.models import ATTRIBUTED, UNKNOWN
from briefly.tracking.scheduler import run_due_daily
from briefly.weekly.cli import review_loop
from briefly.weekly.models import CONFIRMED, CONFIRMED_LIST, DONE, REJECTED
from test_tracking_evidence import WEEK
from test_tracking_review import make_model
from tracking_util import docx_with_revisions, env, xlsx_bytes  # noqa: F401


def test_full_weekly_flow(env, monkeypatch):
    parsed: list[str] = []
    real_load = scanner_module.load_document

    def spy(filename, data):
        parsed.append(filename)
        return real_load(filename, data)

    monkeypatch.setattr(scanner_module, "load_document", spy)

    # 周一：混合添加项目文件夹、Word 和 Excel
    env.write("项目/需求.md", "# 需求\n## 范围\n支付改造。")
    env.write("项目/node_modules/x.md", "# 依赖")
    env.write("方案.docx", docx_with_revisions("原有方案正文", []))
    env.write("进度.xlsx", xlsx_bytes({"进度": [["任务", "负责人", "进度"], ["联调", "张三", "50%"]]}))
    env.set_owners(["张三"])
    added = env.svc.add_paths([str(env.work / "项目"), str(env.work / "方案.docx"), str(env.work / "进度.xlsx")])
    assert added.scan.stats["new_snapshots"] == 3  # 立即保存基线
    assert sorted(parsed) == ["方案.docx", "进度.xlsx", "需求.md"]

    # 周二：改了 Word（张三的修订）和 Excel；需求文档没动 → 每日扫描只存两个新版本
    env.write("方案.docx", docx_with_revisions("原有方案正文", [("张三", "补充异步队列架构设计")]))
    env.write("进度.xlsx", xlsx_bytes({"进度": [["任务", "负责人", "进度"], ["联调", "张三", "100%"]]}))
    env.touch("项目/需求.md")  # 只改了修改时间
    parsed.clear()
    from briefly import config

    monkeypatch.setattr(config, "TRACKING_WORKDAYS_ONLY", False)
    monkeypatch.setattr(config, "TRACKING_DAILY_TIME", "00:00")
    assert "新版本 2" in run_due_daily(env.svc)[0]
    assert sorted(parsed) == ["方案.docx", "进度.xlsx"]  # 未变化的文件不重复解析

    # 周三：李四在 Word 里加了一段（他的工作）
    env.write("方案.docx", docx_with_revisions("原有方案正文", [("张三", "补充异步队列架构设计"), ("李四", "调整预算表")]))

    # 周五：点“现在生成”（或到点自动开始）→ 检查更新 + 整理审核清单
    model = make_model()
    weekly = env.weekly(lambda: model)
    prep, info = weekly.prepare(WEEK[0])
    assert info.scan.stats["new_snapshots"] == 1
    review = weekly.review()
    items = review.items(prep.id)
    by_file = {e.source_refs[0]["path"].replace("\\", "/").rsplit("/", 1)[-1]: e for e in items}
    assert set(by_file) == {"方案.docx", "进度.xlsx"}  # 需求.md 本周没有内容变化，不算新工作
    word = by_file["方案.docx"]
    assert word.attribution == ATTRIBUTED and "others" in word.flags  # 张三和李四都有修订
    assert "原有方案正文" not in word.description  # 已有内容不算本周新增
    assert by_file["进度.xlsx"].attribution == UNKNOWN  # Excel 没有修订记录：等用户确认

    # 审核：拆出李四的部分并排除，修改 Excel 描述，补充会议
    script = iter(
        [
            f"s {word.id}",
            f"sp {word.id}",
            "补充异步队列架构设计",
            "在方案中补充异步队列的架构设计",
            "调整预算表（李四）",
            "李四调整了预算表",
            "",
            "l",
            "q",  # 还有待确认的：先退出，审核结果已保存
        ]
    )
    out: list[str] = []
    review_loop(review, prep.id, ask=lambda _: next(script), out=out.append)
    assert "还有" in "\n".join(out)
    parts = [e for e in review.items(prep.id) if e.origin == "split"]
    mine = next(e for e in parts if "李四" not in e.title)
    theirs = next(e for e in parts if "李四" in e.title)

    script = iter(
        [
            f"y {mine.id}",
            f"n {theirs.id}",
            f"e {by_file['进度.xlsx'].id}",
            "更新联调进度",
            "联调进度从 50% 更新为 100%",
            "done",  # 类别：已完成
            f"y {by_file['进度.xlsx'].id}",
            "a",
            "参加支付改造评审会",
            "确认 10 月 20 日上线",
            "plan",
            "done",
        ]
    )
    review_loop(review, prep.id, ask=lambda _: next(script), out=out.append)
    assert "生成周报" in out[-1]

    # 生成周报：只用确认的记录，出处可追溯
    report = weekly.generate(prep.id)
    assert report.status == DONE, report.error and report.checklist == CONFIRMED_LIST
    assert "李四" not in report.content and "预算" not in report.content
    assert "联调进度从 50% 更新为 100%" in report.content and "评审会" in report.content
    confirmed = {e.id for e in review.items(prep.id) if e.review_status == CONFIRMED}
    assert set(re.findall(r"〔E(\d+)〕", report.content)) == {str(i) for i in confirmed}
    assert {e.review_status for e in review.items(prep.id)} == {CONFIRMED, REJECTED}

    # 审核结果和版本都在：再准备一次不会覆盖
    weekly.prepare(WEEK[0])
    assert {e.id for e in review.items(prep.id) if e.review_status == CONFIRMED} == confirmed
