"""把一次或几次 run_eval.py 的结果整理成可交付的汇报包（目录 + zip），不调用模型。

用法：python tests/eval/package_report.py files/eval/<时间1> [files/eval/<时间2> ...] -o files/<汇报目录名>
生成：测试结果汇总、用例清单（输入与检查）、逐项检查结果、测试环境、原始数据和测试材料。
失败分析与汇报摘要需要人工阅读输出后另写，放进同一目录后再运行一次以重新打包。
"""
import argparse
import json
import platform
import shutil
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from cases import CASES, FIXTURES, REPO  # noqa: E402

BY_ID = {c.id: c for c in CASES}


def load(dirs: list[Path]) -> dict:
    merged = {}
    for d in dirs:
        merged.update(json.loads((d / "results.json").read_text(encoding="utf-8")))
    return merged


def run_ok(r: dict) -> bool:
    return not r["error"] and all(c["ok"] for c in r["checks"])


def summary(results: dict) -> str:
    groups = defaultdict(lambda: {"cases": 0, "runs": 0, "runs_ok": 0, "checks": 0, "checks_ok": 0, "seconds": []})
    rows = []
    tools = defaultdict(int)
    for case in CASES:
        runs = results.get(case.id)
        if not runs:
            continue
        g = groups[case.group]
        if runs == "skipped":
            rows.append(f"| {case.id} | {case.group} | {case.title} | 跳过（缺少本地文件） | — | — |")
            continue
        g["cases"] += 1
        ok_runs = sum(run_ok(r) for r in runs)
        checks = [c for r in runs for c in r["checks"]]
        g["runs"] += len(runs)
        g["runs_ok"] += ok_runs
        g["checks"] += len(checks)
        g["checks_ok"] += sum(c["ok"] for c in checks)
        g["seconds"] += [r["seconds"] for r in runs]
        for r in runs:
            for t in r.get("tools") or []:
                tools[t] += 1
        errors = sum(1 for r in runs if r["error"])
        turns = len(case.turns) or 1
        rows.append(f"| {case.id} | {case.group} | {case.title} | {turns} | {ok_runs}/{len(runs)}{f'（{errors} 次出错）' if errors else ''} | "
                    f"{sum(c['ok'] for c in checks)}/{len(checks)} |")
    total = {k: sum(g[k] for g in groups.values()) for k in ("cases", "runs", "runs_ok", "checks", "checks_ok")}
    lines = ["# 测试结果汇总", "",
             f"共 {total['cases']} 个用例、{total['runs']} 次运行；整次运行全部检查通过 {total['runs_ok']}/{total['runs']}"
             f"（{total['runs_ok'] / max(total['runs'], 1):.0%}），单项检查通过 {total['checks_ok']}/{total['checks']}"
             f"（{total['checks_ok'] / max(total['checks'], 1):.0%}）。", "",
             "“整次运行通过”指这一次对话里所有轮次的所有检查都通过；任何一项未通过即算失败，口径较严。", "",
             "## 按分组", "", "| 分组 | 用例数 | 运行次数 | 整次通过 | 单项检查通过 | 平均耗时 |", "|---|---|---|---|---|---|"]
    for name, g in groups.items():
        if not g["runs"]:
            continue
        avg = sum(g["seconds"]) / len(g["seconds"])
        lines.append(f"| {name} | {g['cases']} | {g['runs']} | {g['runs_ok']}/{g['runs']}（{g['runs_ok'] / g['runs']:.0%}） | "
                     f"{g['checks_ok']}/{g['checks']}（{g['checks_ok'] / g['checks']:.0%}） | {avg:.1f} 秒 |")
    lines += ["", "## 按用例", "", "| 用例 | 分组 | 内容 | 轮数 | 整次通过 | 单项检查通过 |", "|---|---|---|---|---|---|", *rows]
    if tools:
        lines += ["", "## 工具调用次数（全部运行合计）", "", "| 工具 | 次数 |", "|---|---|"]
        lines += [f"| {t} | {n} |" for t, n in sorted(tools.items(), key=lambda x: -x[1])]
    return "\n".join(lines) + "\n"


def case_list(results: dict) -> str:
    lines = ["# 用例清单：输入与检查", "",
             "每个用例的输入（各轮用户原话、所选材料、指定能力）和自动检查项。材料原文见“测试材料”目录。", ""]
    group = None
    for case in CASES:
        if case.id not in results:
            continue
        if case.group != group:
            group = case.group
            lines += [f"## {group}", ""]
        lines += [f"### {case.id} {case.title}", ""]
        for i, t in enumerate(case.turns, start=1):
            text = t.text if len(t.text) <= 300 else t.text[:300] + f"……（共 {len(t.text)} 字）"
            extra = [f"材料：{'、'.join(m.lstrip('@') for m in t.materials)}" if t.materials else "", f"指定能力：{t.skill}" if t.skill else ""]
            lines.append(f"- 第 {i} 轮：{text.strip() or '（只选材料，不输入文字）'}" + "".join(f"；{x}" for x in extra if x))
            lines += [f"  - 检查：{c.name}" for c in t.checks]
        if case.weekly:
            w = case.weekly
            lines.append(f"- 正式周报：材料 {', '.join(w.get('materials') or []) or '无'}；简述“{w.get('brief', '')}”；"
                         f"已确认工作记录 {len(w.get('evidence') or [])} 条；周期 {w.get('period_start')}～{w.get('period_end')}")
            lines += [f"  - 检查：{c.name}" for c in case.checks]
        if case.todo_preview:
            lines.append("- 对话后提取待办候选（结构化预览）")
            lines += [f"  - 检查：{c.name}" for c in case.checks]
        lines.append("")
    return "\n".join(lines)


def environment(dirs: list[Path], results: dict) -> str:
    from briefly import config

    commit = subprocess.run(["git", "-C", str(REPO), "log", "-1", "--format=%h %s"], capture_output=True, text=True, encoding="utf-8").stdout.strip()
    runs = {len(v) for v in results.values() if isinstance(v, list)}
    return "\n".join([
        "# 测试方法与环境", "",
        f"- 整理时间：{datetime.now():%Y-%m-%d %H:%M}",
        f"- 模型：{config.MODEL}（OpenAI 兼容接口，服务商由本地配置决定；密钥不在本包内）",
        f"- 代码版本：{commit}",
        f"- Python：{platform.python_version()}；系统：{platform.system()} {platform.release()}",
        f"- 每个用例重复运行：{'、'.join(map(str, sorted(runs)))} 次；原始结果目录：{'、'.join(d.name for d in dirs)}",
        "",
        "## 方法",
        "",
        "- 每次运行都新建独立的内存数据库和会话，导入本轮所选材料后，按用例逐轮对智能体发消息，与网页使用同一套智能体、提示词、工具和回答检查。",
        "- 每轮回答用代码做自动检查：格式栏目是否齐全、必须出现的事实（人名、日期、数字）、不应出现的内容、有没有材料以外的数字、字数上限、是否有表格等。",
        "- 同一用例重复多次，看稳定性；统计按“整次运行全部通过”和“单项检查通过”两种口径给出。",
        "- 自动检查只能发现问题，不能证明回答完全正确；结论需结合“逐项检查结果”里的未通过输出与人工阅读。",
        "- 测试材料均为虚构业务内容；长材料用例使用本地的一份内部培训文档，未放入本包。",
        "",
    ])


def main() -> None:
    ap = argparse.ArgumentParser(description="整理实测结果为汇报包")
    ap.add_argument("dirs", nargs="+", type=Path)
    ap.add_argument("-o", "--out", type=Path, required=True)
    ap.add_argument("--retest", nargs="*", type=Path, default=[], help="修复后复测的结果目录：单独列出，不计入主统计")
    args = ap.parse_args()
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    results = load(args.dirs)
    (out / "01-测试结果汇总.md").write_text(summary(results), encoding="utf-8")
    (out / "02-用例清单.md").write_text(case_list(results), encoding="utf-8")
    reports = [f"<!-- 来自 {d.name} -->\n" + (d / "report.md").read_text(encoding="utf-8") for d in args.dirs]
    (out / "03-逐项检查结果.md").write_text("\n\n".join(reports), encoding="utf-8")
    (out / "05-测试方法与环境.md").write_text(environment(args.dirs, results), encoding="utf-8")
    if args.retest:
        retest = load(args.retest)
        reports = "\n\n".join((d / "report.md").read_text(encoding="utf-8") for d in args.retest)
        (out / "06-修复后复测.md").write_text(summary(retest).replace("# 测试结果汇总", "# 修复后复测", 1) + "\n\n" + reports, encoding="utf-8")
    raw = out / "原始数据"
    raw.mkdir(exist_ok=True)
    for d in [*args.dirs, *args.retest]:
        shutil.copy(d / "results.json", raw / f"results-{d.name}.json")
    shutil.copytree(FIXTURES, out / "测试材料", dirs_exist_ok=True)
    archive = shutil.make_archive(str(out), "zip", root_dir=out.parent, base_dir=out.name)
    print(f"汇报目录：{out}\n压缩包：{archive}")


if __name__ == "__main__":
    main()
