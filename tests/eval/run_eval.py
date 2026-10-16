"""批量运行实测用例（真实模型，会产生 API 调用费用），统计每项检查的通过率。

用法（在仓库根目录）：
    python tests/eval/run_eval.py                   # 全部用例各跑 3 次
    python tests/eval/run_eval.py -n 1 -k W1 S2     # 只跑指定用例，各 1 次
结果写到 files/eval/<时间>/：results.json（全部输出与检查结果）、report.md（汇总表与失败详情）。
每次运行都用独立的内存数据库，互不影响；用例之间并发执行。
"""
import argparse
import json
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from cases import CASES, FIXTURES, REPO, Case  # noqa: E402

from briefly.agent import Agent  # noqa: E402
from briefly.db import create_db_engine  # noqa: E402
from briefly.materials import MaterialStore  # noqa: E402
from briefly.weekly import WeeklyService  # noqa: E402


def _load(store: MaterialStore, name: str) -> int:
    path = REPO / name[1:] if name.startswith("@") else FIXTURES / name
    material, _ = store.add_file(path.name, path.read_bytes())
    return material.id


def _evidence(engine, spec: dict) -> tuple[list[int], list[dict]]:
    """周报用例里已确认的工作记录（如来自待办）：直接写进库，并附所属待办（与周报计划生成周报时相同的格式）。"""
    from sqlalchemy.orm import Session

    from briefly.tracking.models import CONFIRMED, ReportPrep, TrackingCollection, WorkEvidence

    rows = spec.get("evidence") or []
    if not rows:
        return [], []
    with Session(engine) as s:
        plan = TrackingCollection(name="实测")
        s.add(plan)
        s.flush()
        prep = ReportPrep(collection_id=plan.id, period_start=date.fromisoformat(spec["period_start"]), period_end=date.fromisoformat(spec["period_end"]))
        s.add(prep)
        s.flush()
        items = [WorkEvidence(prep_id=prep.id, title=x["title"], description=x["description"], category=x["category"], origin="todo", review_status=CONFIRMED) for x in rows]
        s.add_all(items)
        s.commit()
        ids = [e.id for e in items]
    refs = []
    for i, x in zip(ids, rows):
        if x.get("group"):
            *parent, title = x["group"].split(" › ")
            refs.append({"evidence_id": i, "group": x["group"], "todo": {"title": title, "parent_title": parent[0] if parent else None}})
    return ids, refs


def run_once(case: Case, model_factory) -> dict:
    """跑一次用例，返回输出和每项检查的结果。出错也记录下来，不中断其他用例。"""
    engine = create_db_engine("sqlite://")
    store = MaterialStore(engine)
    started = time.monotonic()
    record: dict = {"outputs": [], "checks": [], "error": None}
    try:
        if case.weekly:
            spec = case.weekly
            weekly = WeeklyService(store, model_factory)
            ids = [_load(store, n) for n in spec["materials"]]
            evidence_ids, todo_refs = _evidence(engine, spec)
            report = weekly.create(
                ids, spec["brief"], date.fromisoformat(spec["period_start"]), date.fromisoformat(spec["period_end"]),
                evidence_ids=evidence_ids, todo_refs=todo_refs,
            )
            weekly.generate(report.id)
            report = weekly.get(report.id)
            record["outputs"].append(report.content or report.error)
            record["generation"] = report.generation
            record["checks"] = [{"turn": 1, "name": c.name, "ok": bool(c.fn(report))} for c in case.checks]
        else:
            agent = Agent(model_factory(), store=store)
            for n, turn in enumerate(case.turns, start=1):
                ids = [_load(store, m) for m in turn.materials] or None
                previous = list(record["outputs"])
                output = agent.chat(turn.text, turn.skill, material_ids=ids)
                record["outputs"].append(output)
                record["checks"] += [{"turn": n, "name": c.name, "ok": bool(c.fn(output, previous))} for c in turn.checks]
            record["tools"] = [
                c["name"] for m in agent.history for c in (getattr(m, "tool_calls", None) or [])
            ]
    except Exception as e:
        record["error"] = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"
    finally:
        engine.dispose()
    record["seconds"] = round(time.monotonic() - started, 1)
    return record


def write_report(out_dir: Path, results: dict, repeat: int) -> Path:
    lines = [f"# 实测结果 {datetime.now():%Y-%m-%d %H:%M}", "", f"每个用例运行 {repeat} 次。", ""]
    lines += ["| 用例 | 分组 | 内容 | 检查 | 通过率 |", "|---|---|---|---|---|"]
    failures = []
    for case in CASES:
        runs = results.get(case.id)
        if runs is None:
            continue
        if runs == "skipped":
            lines.append(f"| {case.id} | {case.group} | {case.title} | （缺少本地文件，跳过） | — |")
            continue
        names = list(dict.fromkeys((c["turn"], c["name"]) for r in runs for c in r["checks"]))
        errors = sum(1 for r in runs if r["error"])
        if errors:
            lines.append(f"| {case.id} | {case.group} | {case.title} | 运行出错 | {errors}/{len(runs)} 次出错 |")
        for turn, name in names:
            passed = sum(any(c["turn"] == turn and c["name"] == name and c["ok"] for c in r["checks"]) for r in runs)
            label = f"第{turn}轮 {name}" if len(case.turns) > 1 else name
            lines.append(f"| {case.id} | {case.group} | {case.title} | {label} | {passed}/{len(runs)} |")
            if passed < len(runs):
                failures.append((case, turn, name, runs))
    lines += ["", "## 未通过的检查", ""]
    for case, turn, name, runs in failures:
        lines.append(f"### {case.id} {case.title}：{name}")
        for i, r in enumerate(runs, start=1):
            if not any(c["turn"] == turn and c["name"] == name and c["ok"] for c in r["checks"]):
                output = r["outputs"][turn - 1] if len(r["outputs"]) >= turn else (r["error"] or "")
                lines += [f"第 {i} 次输出：", "```", output.strip()[:1500], "```"]
    path = out_dir / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> None:
    from briefly.llm import create_chat_model

    ap = argparse.ArgumentParser(description="批量运行实测用例（真实模型）")
    ap.add_argument("-n", "--repeat", type=int, default=3, help="每个用例运行几次")
    ap.add_argument("-k", "--cases", nargs="*", help="只运行这些用例编号")
    ap.add_argument("-w", "--workers", type=int, default=4, help="并发运行的用例数")
    ap.add_argument("-o", "--out", default=str(REPO / "files" / "eval"), help="结果目录")
    args = ap.parse_args()

    selected = [c for c in CASES if not args.cases or c.id in args.cases]
    results: dict = {}
    jobs = []
    for case in selected:
        if any(not (REPO / r).exists() for r in case.requires):
            results[case.id] = "skipped"
            continue
        results[case.id] = [None] * args.repeat
        jobs += [(case, i) for i in range(args.repeat)]

    def work(job):
        case, i = job
        results[case.id][i] = run_once(case, create_chat_model)
        ok = all(c["ok"] for c in results[case.id][i]["checks"]) and not results[case.id][i]["error"]
        print(f"{case.id} #{i + 1} {'✓' if ok else '✗'} {results[case.id][i]['seconds']}s", flush=True)

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(work, jobs))

    out_dir = Path(args.out) / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    report = write_report(out_dir, results, args.repeat)
    print(f"共 {len(jobs)} 次运行，用时 {time.monotonic() - started:.0f} 秒；报告：{report}")


if __name__ == "__main__":
    main()
