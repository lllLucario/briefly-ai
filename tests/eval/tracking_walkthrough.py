"""追踪文件 → 审核清单 → 周报的真实模型走查（手动运行，会调用 .env 中配置的模型，产生费用）。

    python tests/eval/tracking_walkthrough.py -n 3

场景：周一添加项目文件夹、Word、Excel → 周中修改（Word 有张三的修订，预算表是李四改的）→ 周五整理审核清单 →
确认自己的、排除李四的、补充一条会议 → 生成周报。每轮用临时目录和独立数据库，不碰 data/ 下的数据。
结果写到 files/eval/tracking-<时间>.md。假模型测试见 tests/test_tracking_*.py，两者分开报告。
"""
import argparse
import re
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from briefly import config  # noqa: E402
from briefly.db import create_db_engine  # noqa: E402
from briefly.materials import MaterialStore  # noqa: E402
from briefly.tracking.blobs import BlobStore  # noqa: E402
from briefly.tracking.diff import Differ, format_hunk  # noqa: E402
from briefly.tracking.service import TrackingService  # noqa: E402
from briefly.weekly.engine import unverified_numbers  # noqa: E402
from briefly.weekly.models import CONFIRMED, PENDING  # noqa: E402
from briefly.weekly.service import WeeklyService  # noqa: E402
from tracking_util import docx_with_revisions, xlsx_bytes  # noqa: E402

BASELINE_ONLY = ["现有支付链路为同步调用", "项目背景", "原有需求说明"]


def write(path: Path, content, t: float) -> None:
    import os

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    os.utime(path, (t, t))


def run_once(workdir: Path) -> dict:
    config.TRACKING_SETTLE_SECONDS = 0
    engine = create_db_engine(f"sqlite:///{workdir / 'app.db'}")
    store = MaterialStore(engine)
    svc = TrackingService(engine, store, BlobStore(workdir / "store"))
    work = workdir / "work"
    t = time.time() - 3600

    # 周一：基线
    write(work / "项目" / "需求.md", "# 支付改造需求\n## 背景\n项目背景：现有支付链路为同步调用，高峰期回调超时。\n## 范围\n原有需求说明：改造回调链路。", t)
    write(work / "方案.docx", docx_with_revisions("现有支付链路为同步调用，回调平均耗时 1.8 秒。", []), t)
    write(work / "进度.xlsx", xlsx_bytes({"进度": [["任务", "负责人", "进度"], ["接口联调", "张三", "50%"], ["压测", "赵琳", "0%"]]}), t)
    write(work / "预算.xlsx", xlsx_bytes({"预算": [["项目", "金额"], ["服务器", 20000]]}), t)
    weekly = WeeklyService(store, tracking=svc)
    weekly.update_settings(owner_names=["张三"])
    svc.add_paths([str(work / "项目"), str(work / "方案.docx"), str(work / "进度.xlsx"), str(work / "预算.xlsx")])

    # 周中：张三在 Word 里补充架构（修订记录），更新进度表和需求文档；李四改了预算表
    t += 600
    write(
        work / "方案.docx",
        docx_with_revisions(
            "现有支付链路为同步调用，回调平均耗时 1.8 秒。",
            [("张三", "新增异步队列：回调先入队再处理，预发环境实测平均耗时降到 200 毫秒。"), ("张三", "风控调用改为异步，失败时重试 3 次。")],
        ),
        t,
    )
    write(work / "进度.xlsx", xlsx_bytes({"进度": [["任务", "负责人", "进度"], ["接口联调", "张三", "100%"], ["压测", "赵琳", "0%"], ["回调超时排查", "张三", "60%"]]}), t)
    write(
        work / "项目" / "需求.md",
        "# 支付改造需求\n## 背景\n项目背景：现有支付链路为同步调用，高峰期回调超时。\n## 范围\n原有需求说明：改造回调链路。\n## 验收标准\n回调 P99 耗时低于 500 毫秒；10 月 18 日前提交压测报告。",
        t,
    )
    write(work / "预算.xlsx", xlsx_bytes({"预算": [["项目", "金额"], ["服务器", 20000], ["压测环境", 8000]]}), t)
    svc.scan()

    # 周五：准备周报
    start = date.today() - timedelta(days=date.today().weekday())
    started = time.monotonic()
    prep, info = weekly.prepare(start)
    review = weekly.review()
    items = review.items(prep.id)
    checks: dict[str, bool] = {}
    problems: list[str] = []

    by_file: dict[str, list] = {}
    for e in items:
        for ref in e.source_refs:
            by_file.setdefault(Path(ref["path"]).name, []).append(e)
    checks["四个改动的文件都有候选"] = set(by_file) >= {"方案.docx", "进度.xlsx", "需求.md", "预算.xlsx"}
    leaked = [e for e in items if any(p in e.description for p in BASELINE_ONLY)]
    checks["已有内容没有被当成本周工作"] = not leaked
    problems += [f"E{e.id} 描述包含基线内容：{e.description}" for e in leaked]
    word = by_file.get("方案.docx", [])
    checks["Word 修订归属为 ATTRIBUTED"] = bool(word) and all(e.attribution == "ATTRIBUTED" for e in word)
    checks["无修订记录的文件归属为 UNKNOWN"] = all(e.attribution == "UNKNOWN" for e in by_file.get("预算.xlsx", []))
    done_words = [e for e in word if e.category == "done" or "完成" in e.description]
    checks["文档编辑没有被说成“完成”"] = not done_words
    problems += [f"E{e.id} 把编辑说成完成：{e.category} {e.description}" for e in done_words]
    differ = Differ(svc)
    fabricated = []
    for e in items:
        diff_text = ""
        for ref in e.source_refs:
            if ref.get("to_snapshot_id"):
                base = svc.snapshot(ref["from_snapshot_id"]) if ref.get("from_snapshot_id") else None
                diff_text += "\n".join(format_hunk(h) for h in differ.diff(base, svc.snapshot(ref["to_snapshot_id"])).hunks)
        numbers = unverified_numbers(e.description, diff_text)
        if numbers:
            fabricated.append(f"E{e.id} 描述中的数字在变化里找不到：{numbers}（{e.description}）")
    checks["描述中的数字都有变化依据"] = not fabricated
    problems += fabricated

    # 审核：预算表是李四的 → 排除；其余确认；补充一条会议
    for e in items:
        if any(Path(r["path"]).name == "预算.xlsx" for r in e.source_refs):
            review.exclude(prep.id, [e.id], note="李四的工作")
        elif e.review_status == PENDING:
            review.confirm(prep.id, [e.id])
    review.add(prep.id, "参加支付改造评审会", "评审会确认 10 月 20 日上线", "done")
    report = weekly.generate(prep.id)
    seconds = round(time.monotonic() - started, 1)

    confirmed = {e.id for e in review.items(prep.id) if e.review_status == CONFIRMED}
    refs = {int(x) for x in re.findall(r"〔E(\d+)", report.content)}
    checks["周报生成成功"] = report.status == "done"
    checks["周报不含被排除的预算内容"] = "8000" not in report.content and "压测环境" not in report.content
    checks["周报出处都指向已确认记录"] = bool(refs) and refs <= confirmed
    checks["周报数字核对无未核实数字"] = not report.generation.get("unverified_numbers")
    if report.generation.get("unverified_numbers"):
        problems.append(f"周报未核实数字：{report.generation['unverified_numbers']}")
    engine.dispose()
    return {
        "checks": checks,
        "problems": problems,
        "items": [(e.id, e.title, e.description, e.attribution, e.category, e.flags) for e in items],
        "report": report.content,
        "reminders": report.generation.get("reminders", []),
        "calls": info.build.calls + report.generation.get("calls", 0),
        "seconds": seconds,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=3, help="运行次数")
    args = ap.parse_args()
    runs = []
    for i in range(args.n):
        with tempfile.TemporaryDirectory() as tmp:
            print(f"第 {i + 1} 轮……", flush=True)
            runs.append(run_once(Path(tmp)))
    names = list(runs[0]["checks"])
    lines = [f"# 文件追踪真实模型走查（{datetime.now():%Y-%m-%d %H:%M}，{config.MODEL}，{args.n} 轮）", "", "| 检查 | 通过 |", "|---|---|"]
    for name in names:
        lines.append(f"| {name} | {sum(r['checks'][name] for r in runs)}/{len(runs)} |")
    total = sum(sum(r["checks"].values()) for r in runs)
    lines += ["", f"合计 {total}/{len(names) * len(runs)}；每轮模型调用 {[r['calls'] for r in runs]} 次，耗时 {[r['seconds'] for r in runs]} 秒", ""]
    for i, r in enumerate(runs, start=1):
        lines += [f"## 第 {i} 轮", ""]
        lines += [f"- 问题：{p}" for p in r["problems"]] or ["- 无问题"]
        lines += ["", "候选记录："]
        lines += [f"- E{i_}「{t}」{d}（{a}，{cat}，{', '.join(f)}）" for i_, t, d, a, cat, f in r["items"]]
        lines += ["", "周报：", "", r["report"], ""]
        lines += [f"- 提醒：{x}" for x in r["reminders"]]
        lines.append("")
    out = ROOT / "files" / "eval" / f"tracking-{datetime.now():%Y%m%d-%H%M%S}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines[: len(names) + 6]))
    print(f"详细结果：{out}")


if __name__ == "__main__":
    main()
