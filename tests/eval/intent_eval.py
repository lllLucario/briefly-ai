"""意图漏斗本地评测：只跑规则层与语义层（本机BGE），不调用生成模型、不外发。

用法：python tests/eval/intent_eval.py [--cases files/eval/intent/cases.jsonl] [--sweep] [--errors]
兜底层（agent）记为“交给助理”，只统计分流是否合理，不评价助理最终回答。
"""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path

from briefly import config
from briefly.intent import LABELS, IntentFunnel, TurnState
from briefly.llm import create_local_embeddings

# 走直接回复、不经模型的意图：误判到这里用户拿不到任何帮助，代价最高
DIRECT = {"blocked", "clarify", "greeting", "realtime", "out_of_scope"}
# 走知识捷径：不进智能体，误判时用户拿不到想要的结果；其余能力误判只是给助理加一句提示
SHORTCUT = {"knowledge", "locate"}
WRONG = ("wrong_direct", "wrong_shortcut", "wrong_hint", "missed_block")


class CachedEncoder:
    """同一句话只编码一次，阈值扫描时复用。"""

    def __init__(self, encoder) -> None:
        self.encoder, self.cache = encoder, {}

    def embed_documents(self, texts):
        missing = [t for t in texts if t not in self.cache]
        if missing:
            self.cache.update(zip(missing, self.encoder.embed_documents(missing)))
        return [self.cache[t] for t in texts]


def load_cases(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def state_of(case: dict) -> TurnState:
    s = case["state"]
    return TurnState(has_materials=s["has_materials"], kb_id="kb-eval" if s["kb_selected"] else None,
                     has_previous_reply=s["previous_turn"] is not None)


def outcome(case: dict, predicted: str) -> str:
    expected, acceptable = case["expected_intent"], set(case["acceptable_intents"])
    if predicted == expected:
        return "exact"
    if predicted in acceptable:
        return "fallback" if predicted == "agent" else "acceptable"
    if expected == "blocked":
        return "missed_block"  # 越权请求没拦下
    if predicted in DIRECT:
        return "wrong_direct"  # 误拒、误澄清等，直接回复且不经模型
    if predicted == "agent":
        return "fallback"  # 交给助理不是错误，只是更慢更贵
    if predicted in SHORTCUT:
        return "wrong_shortcut"  # 误走知识问答/文档定位捷径
    return "wrong_hint"  # 误判能力，只作为提示交给助理，用户原话优先


def run(cases, funnel) -> list[dict]:
    rows = []
    for case in cases:
        d = funnel.classify(case["text"], state_of(case))
        rows.append(dict(case=case, intent=d.intent, layer=d.layer, confidence=d.confidence,
                         outcome=outcome(case, d.intent)))
    return rows


def summarize(rows) -> dict:
    n = len(rows)
    out = Counter(r["outcome"] for r in rows)
    layers = Counter(r["layer"] for r in rows)
    ok = out["exact"] + out["acceptable"] + sum(1 for r in rows if r["outcome"] == "fallback"
                                                and "agent" in r["case"]["acceptable_intents"] + [r["case"]["expected_intent"]])
    return dict(n=n, exact=out["exact"] / n, acceptable=ok / n, outcomes=dict(out),
                layers={k: layers[k] / n for k in sorted(layers)},
                # 前两层自己处理的请求里有多少是对的：这才是漏斗“拦截”的准确度
                early_precision=_early_precision(rows))


def _early_precision(rows) -> float | None:
    early = [r for r in rows if r["layer"] < 3]
    good = [r for r in early if r["outcome"] in ("exact", "acceptable")]
    return len(good) / len(early) if early else None


def report(rows, show_errors: bool) -> None:
    s = summarize(rows)
    print(f"用例 {s['n']}：完全命中 {s['exact']:.1%}，可接受 {s['acceptable']:.1%}；"
          f"前两层处理 {1 - s['layers'].get(3, 0):.1%}，其中正确 {s['early_precision'] or 0:.1%}")
    print("分层：", {f"L{k}": f"{v:.1%}" for k, v in s["layers"].items()})
    print("结果：", s["outcomes"])
    print("\n按期望意图（命中/可接受/交给助理/误判直接回复/误走捷径/误判提示/漏拦）：")
    by = defaultdict(Counter)
    for r in rows:
        by[r["case"]["expected_intent"]][r["outcome"]] += 1
    for intent in LABELS:
        if c := by.get(intent):
            total = sum(c.values())
            print(f"  {LABELS[intent]:<6} {total:>3}  {c['exact']:>3} {c['acceptable']:>3} {c['fallback']:>3} "
                  f"{c['wrong_direct']:>3} {c['wrong_shortcut']:>3} {c['wrong_hint']:>3} {c['missed_block']:>3}")
    confusion = Counter((r["case"]["expected_intent"], r["intent"]) for r in rows
                        if r["outcome"] in WRONG)
    if confusion:
        print("\n误判方向（期望 → 实际）：")
        for (e, p), k in confusion.most_common():
            print(f"  {LABELS[e]} → {LABELS[p]}：{k}")
    if show_errors:
        print("\n误判明细：")
        for r in rows:
            if r["outcome"] in WRONG:
                c = r["case"]
                conf = f" {r['confidence']:.2f}" if r["confidence"] is not None else ""
                print(f"  {c['id']} [{r['outcome']}] L{r['layer']}{conf} {LABELS[c['expected_intent']]}→"
                      f"{LABELS[r['intent']]}：{c['text'][:60]}")


def sweep(cases, encoder) -> None:
    print("\n阈值扫描（阈值/差距 → 可接受率、前两层处理比例、前两层正确率、误判直接回复、误走捷径、误判提示）：")
    for threshold in (0.60, 0.62, 0.64, 0.66, 0.68, 0.70):
        for margin in (0.03, 0.05, 0.07):
            rows = run(cases, IntentFunnel(lambda: encoder, threshold=threshold, margin=margin))
            s = summarize(rows)
            print(f"  {threshold:.2f}/{margin:.2f}  {s['acceptable']:.1%}  {1 - s['layers'].get(3, 0):.1%}  "
                  f"{s['early_precision'] or 0:.1%}  {s['outcomes'].get('wrong_direct', 0):>3}  "
                  f"{s['outcomes'].get('wrong_shortcut', 0):>3}  {s['outcomes'].get('wrong_hint', 0):>3}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", default="files/eval/intent/cases.jsonl")
    parser.add_argument("--model-dir", default=config.EMBEDDING_DIR)
    parser.add_argument("--sweep", action="store_true", help="扫描语义层阈值与差距")
    parser.add_argument("--errors", action="store_true", help="列出每条误判")
    args = parser.parse_args()
    if not args.model_dir:
        parser.error("需要本地Embedding目录（.env 的 BRIEFLY_EMBEDDING_DIR 或 --model-dir）")
    cases = load_cases(Path(args.cases))
    encoder = CachedEncoder(create_local_embeddings(args.model_dir))
    print("== 只用规则层 ==")
    report(run(cases, IntentFunnel()), False)
    print("\n== 规则层＋语义层（当前阈值） ==")
    report(run(cases, IntentFunnel(lambda: encoder)), args.errors)
    if args.sweep:
        sweep(cases, encoder)


if __name__ == "__main__":
    main()
