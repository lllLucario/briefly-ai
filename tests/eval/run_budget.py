"""按模型调用次数限额运行实测用例：每个用例可指定次数，累计调用达到上限后不再开始新的运行。

用法：python tests/eval/run_budget.py --budget 200 S8:4 I4:4 M6:3 ...
统计每次运行的模型调用次数（含工具轮次与检查重写），结果写到 files/eval/<时间>/，格式同 run_eval.py。
"""
import argparse
import json
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from cases import CASES, REPO  # noqa: E402
from langchain_core.callbacks import BaseCallbackHandler  # noqa: E402
from run_eval import run_once, write_report  # noqa: E402


class Counter(BaseCallbackHandler):
    def __init__(self) -> None:
        self.calls = 0
        self.lock = threading.Lock()

    def on_chat_model_start(self, *args, **kwargs) -> None:
        with self.lock:
            self.calls += 1


class DateLog(logging.Handler):
    """数日期校验的日志：核对、明确错误、重写后的结果（只看数量）。"""

    def __init__(self) -> None:
        super().__init__()
        self.checked = self.errors = self.fixed = self.left = 0

    def emit(self, record) -> None:
        msg = record.getMessage()
        if msg.startswith("日期核对："):
            self.checked += 1
            if "明确错误 0 处" not in msg:
                self.errors += 1
        elif msg.startswith("日期纠正："):
            if "已无明确错误" in msg:
                self.fixed += 1
            else:
                self.left += 1


def main() -> None:
    from briefly.llm import create_chat_model

    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=200)
    ap.add_argument("-w", "--workers", type=int, default=3)
    ap.add_argument("plan", nargs="+", help="用例编号:次数")
    args = ap.parse_args()
    by_id = {c.id: c for c in CASES}
    jobs = [(by_id[cid], i) for item in args.plan for cid, n in [item.split(":")] for i in range(int(n))]
    counter, dates = Counter(), DateLog()
    logging.getLogger("briefly.output_check").addHandler(dates)
    logging.getLogger("briefly.output_check").setLevel(logging.INFO)

    def factory():
        model = create_chat_model()
        model.callbacks = [counter]
        return model

    results: dict = {}
    skipped = []
    lock = threading.Lock()

    def work(job):
        case, i = job
        with lock:
            if counter.calls >= args.budget:
                skipped.append(f"{case.id}#{i + 1}")
                return
        before = counter.calls
        record = run_once(case, factory)
        record["model_calls"] = counter.calls - before  # 并发时为近似值
        with lock:
            results.setdefault(case.id, []).append(record)
        ok = all(c["ok"] for c in record["checks"]) and not record["error"]
        print(f"{case.id} #{i + 1} {'✓' if ok else '✗'} {record['seconds']}s 累计调用 {counter.calls}", flush=True)

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(work, jobs))
    out_dir = REPO / "files" / "eval" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    report = write_report(out_dir, results, max(len(v) for v in results.values()))
    summary = {"model_calls": counter.calls, "runs": sum(len(v) for v in results.values()), "skipped": skipped,
               "date_checks_with_pairs": dates.checked, "answers_with_date_errors": dates.errors,
               "rewrites_fixed": dates.fixed, "rewrites_still_wrong": dates.left, "seconds": round(time.monotonic() - started)}
    (out_dir / "budget.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False), report)


if __name__ == "__main__":
    main()
