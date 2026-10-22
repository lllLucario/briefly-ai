"""真实模型复测：变更原文含新旧两个日期时，当前取值不能指回旧值。手动运行。"""
import argparse
import json
from datetime import datetime
from pathlib import Path

from langchain_core.callbacks import BaseCallbackHandler

from briefly.conflicts import SUPERSEDED, classify, detect_conflicts
from briefly.llm import create_chat_model
from briefly.summarize import WorkItem


class Counter(BaseCallbackHandler):
    def __init__(self):
        self.calls = 0

    def on_chat_model_start(self, *args, **kwargs):
        self.calls += 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", type=int, default=5)
    args = parser.parse_args()
    counter = Counter()
    model = create_chat_model(max_retries=0, timeout=30)
    model.callbacks = [counter]
    items = [
        WorkItem("plan", "支付改造原定10月20日上线", "支付改造原计划10月20日上线。", "1-1", fact_id="old", subject="支付改造", attribute="date", value="10月20日"),
        WorkItem("plan", "支付改造推迟到10月30日上线", "支付改造上线时间由10月20日推迟到10月30日。", "2-1", fact_id="new", subject="支付改造", attribute="date", value="10月30日"),
    ]
    records = []
    for _ in range(args.n):
        conflicts = detect_conflicts(items)
        classify(model, conflicts, items)
        c = conflicts[0]
        records.append({"safe": c.resolution != SUPERSEDED or c.current == "new", "resolved_correctly": c.resolution == SUPERSEDED and c.current == "new", "conflict": c.to_dict()})
    out = Path(__file__).resolve().parents[2] / "files" / "eval" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-conflict-target")
    out.mkdir(parents=True, exist_ok=True)
    data = {"model_calls": counter.calls, "runs": records}
    (out / "results.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"model_calls": counter.calls, "safe": sum(r["safe"] for r in records), "resolved_correctly": sum(r["resolved_correctly"] for r in records), "runs": len(records), "path": str(out)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
