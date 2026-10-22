"""用当前 cases.py 的检查，对已保存的对话输出重新判分（不调用模型）。

检查规则修正后，修复前后的结果用同一套检查比较才公平。只重判普通对话用例的逐轮检查；
正式周报、待办预览、分阶段诊断依赖运行时对象，保留原判定。结果写到原目录下的 rescored/。

用法：python tests/eval/rescore.py files/eval/<时间> [...]
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from cases import CASES  # noqa: E402
from run_eval import write_report  # noqa: E402

BY_ID = {c.id: c for c in CASES}


def rescore(runs: list[dict], case) -> list[dict]:
    if case.weekly or case.todo_preview or case.diagnostic_facts:
        return runs
    out = []
    for r in runs:
        r = dict(r)
        if not r["error"] and len(r["outputs"]) >= len(case.turns):
            checks = []
            for n, turn in enumerate(case.turns, start=1):
                previous = r["outputs"][: n - 1]
                checks += [{"turn": n, "name": c.name, "ok": bool(c.fn(r["outputs"][n - 1], previous))} for c in turn.checks]
            r["checks"] = checks
        out.append(r)
    return out


def main() -> None:
    for d in map(Path, sys.argv[1:]):
        results = json.loads((d / "results.json").read_text(encoding="utf-8"))
        rescored = {cid: rescore(runs, BY_ID[cid]) if isinstance(runs, list) and cid in BY_ID else runs for cid, runs in results.items()}
        target = d / "rescored"
        target.mkdir(exist_ok=True)
        (target / "results.json").write_text(json.dumps(rescored, ensure_ascii=False, indent=2), encoding="utf-8")
        repeat = max((len(v) for v in rescored.values() if isinstance(v, list)), default=0)
        print(write_report(target, rescored, repeat))


if __name__ == "__main__":
    main()
