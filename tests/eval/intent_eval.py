"""意图漏斗本地评测：只跑规则层与语义层（本机BGE），不调用生成模型、不外发。

用法：python tests/eval/intent_eval.py [--cases files/eval/intent/cases.jsonl] [--split dev] [--sweep] [--errors]
                                     [--kb-root 本地评测知识库目录]
兜底层（agent）记为“交给助理”，只统计分流是否合理，不评价助理最终回答。
--kb-root 指向用 ImportService 建好的本地知识库目录（含 app.db、index.sqlite、kb_id.txt），
用例选了知识库时按该库做知识库路由；只在本机检索，不外发。
"""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path

from briefly import config
from briefly.intent import LABELS, IntentFunnel, TurnState
from briefly.llm import create_local_embeddings
from briefly.relevance_floor import embedding_fingerprint, kb_floor

# 走直接回复、不经模型的意图：误判到这里用户拿不到任何帮助，代价最高
DIRECT = {"blocked", "clarify", "greeting", "realtime", "out_of_scope"}
# 走知识捷径：不进智能体，误判时用户拿不到想要的结果；其余能力误判只是给助理加一句提示
SHORTCUT = {"knowledge", "locate"}
WRONG = ("wrong_direct", "wrong_shortcut", "wrong_hint", "missed_block")


class CachedEncoder:
    """同一句话只编码一次，阈值扫描时复用。"""

    def __init__(self, encoder) -> None:
        self.encoder, self.cache = encoder, {}
        self.identity = getattr(encoder, "identity", None)  # 知识库下限按模型指纹登记

    def embed_documents(self, texts):
        missing = [t for t in texts if t not in self.cache]
        if missing:
            self.cache.update(zip(missing, self.encoder.embed_documents(missing)))
        return [self.cache[t] for t in texts]


def load_cases(path: Path, split: str = "all") -> list[dict]:
    """split：dev 用于调规则和阈值；holdout 为每类按编号每 3 条封存 1 条，只用于最终报告，调参时不看明细。

    多轮对话文件（每行带 turns）按整段封存：按编号每 3 段封存 1 段，再展开成逐轮用例。
    """
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if cases and "turns" in cases[0]:
        dialogues = sorted(cases, key=lambda d: d["id"])
        held = {d["id"] for i, d in enumerate(dialogues) if i % 3 == 2}
        if split != "all":
            dialogues = [d for d in dialogues if (d["id"] in held) == (split == "holdout")]
        return [turn for d in dialogues for turn in flatten(d)]
    if split == "all":
        return cases
    by = defaultdict(list)
    for case in cases:
        by[case["expected_intent"]].append(case)
    held = {c["id"] for group in by.values() for i, c in enumerate(sorted(group, key=lambda c: c["id"])) if i % 3 == 2}
    return [c for c in cases if (c["id"] in held) == (split == "holdout")]


def flatten(dialogue: dict) -> list[dict]:
    """多轮对话展开成逐轮用例。前面各轮按标注的期望意图模拟系统走过的路径：
    直接回复（问候、拦截、澄清等）不算可调整的回答，与 agent.turn_state 的口径一致。"""
    out, materials, revisable, last = [], False, False, None
    for i, turn in enumerate(dialogue["turns"], 1):
        out.append(dict(id=f"{dialogue['id']}-t{i}", text=turn["text"], expected_intent=turn["expected_intent"],
                        acceptable_intents=turn.get("acceptable_intents", []), note=turn.get("note", ""),
                        difficulty=dialogue.get("difficulty"), kb_mismatch=False,
                        state=dict(kb_selected=dialogue.get("kb") is not None, kb=dialogue.get("kb"),
                                   has_materials=turn.get("has_materials", False), previous_turn=None),
                        turn_state=dict(earlier_materials=materials, has_previous_reply=revisable, last_intent=last)))
        materials = materials or turn.get("has_materials", False)
        revisable = revisable or turn["expected_intent"] not in DIRECT
        last = turn["expected_intent"]
    return out


def state_of(case: dict, kb_ids: dict | str = "kb-eval") -> TurnState:
    """kb_ids：知识库键（hr/fin…）到本地库 ID 的映射；键 None 为未标库键时的默认库。"""
    s = case["state"]
    if isinstance(kb_ids, str):
        kb_ids = {None: kb_ids}
    kb_id = (kb_ids.get(s.get("kb")) or kb_ids.get(None) or "kb-eval") if s["kb_selected"] else None
    if extra := case.get("turn_state"):
        return TurnState(has_materials=s["has_materials"], kb_id=kb_id, **extra)
    return TurnState(has_materials=s["has_materials"], kb_id=kb_id,
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


def run(cases, funnel, kb_ids: dict | str = "kb-eval") -> list[dict]:
    rows = []
    for case in cases:
        d = funnel.classify(case["text"], state_of(case, kb_ids))
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
    if mismatch := [r for r in rows if r["case"].get("kb_mismatch")]:
        routed = sum(r["intent"] in SHORTCUT for r in mismatch)
        print(f"\n选错库或库里没有的问题 {len(mismatch)} 条：走了知识捷径 {routed} 条")
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


def kb_probe(root: Path, encoder_factory):
    """返回 (知识库映射, 带缓存的相关度函数, 关闭函数)。

    目录里有 libs.json（多库：{键: {kb_id, …}}）时按用例的 state.kb 选库，否则用 kb_id.txt 的单库。"""
    from briefly.db import create_db_engine
    from briefly.knowledge.assistant import KnowledgeAssistant
    from briefly.knowledge.index import LocalIndex
    from briefly.knowledge.ingestion import ImportService
    from briefly.knowledge.retrieval import RetrievalService

    engine = create_db_engine("sqlite:///" + (root / "app.db").as_posix())
    imports = ImportService(engine, root, LocalIndex(root / "index.sqlite"), encoder_factory)
    assistant, cache = KnowledgeAssistant(RetrievalService(imports), None), {}

    def probe(kb_id, text):
        if (kb_id, text) not in cache:
            cache[kb_id, text] = assistant.relevance(kb_id, text)
        return cache[kb_id, text]
    libs = root / "libs.json"
    if libs.is_file():
        kb_ids = {key: lib["kb_id"] for key, lib in json.loads(libs.read_text(encoding="utf-8")).items()}
    else:
        kb_ids = {None: (root / "kb_id.txt").read_text(encoding="utf-8").strip()}
    return kb_ids, probe, engine.dispose


def kb_sweep(cases, encoder, kb_ids, probe) -> None:
    print("\n知识库路由相似度下限扫描（下限 → 可接受率、前两层处理比例、前两层正确率、误判直接回复、误走捷径、误判提示）：")
    for floor in (0.45, 0.50, 0.55, 0.60):
        funnel = IntentFunnel(lambda: encoder, kb_probe=probe)
        funnel.kb_threshold = floor
        s = summarize(run(cases, funnel, kb_ids))
        print(f"  {floor:.2f}  {s['acceptable']:.1%}  {1 - s['layers'].get(3, 0):.1%}  {s['early_precision'] or 0:.1%}  "
              f"{s['outcomes'].get('wrong_direct', 0):>3}  {s['outcomes'].get('wrong_shortcut', 0):>3}  "
              f"{s['outcomes'].get('wrong_hint', 0):>3}")


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
    parser.add_argument("--split", choices=["all", "dev", "holdout"], default="all")
    parser.add_argument("--kb-root", type=Path, help="本地评测知识库目录，启用知识库路由")
    args = parser.parse_args()
    if not args.model_dir:
        parser.error("需要本地Embedding目录（.env 的 BRIEFLY_EMBEDDING_DIR 或 --model-dir）")
    cases = load_cases(Path(args.cases), args.split)
    encoder = CachedEncoder(create_local_embeddings(args.model_dir))
    print("== 只用规则层 ==")
    report(run(cases, IntentFunnel()), False)
    print("\n== 规则层＋语义层（当前阈值） ==")
    report(run(cases, IntentFunnel(lambda: encoder)), args.errors and not args.kb_root)
    if args.kb_root:
        kb_ids, probe, close = kb_probe(args.kb_root, lambda: encoder.encoder)
        try:
            floor = kb_floor(encoder.identity)
            print(f"\n== 规则层＋知识库路由＋语义层 ==\n模型指纹 {embedding_fingerprint(encoder.identity)}，"
                  f"当前下限 {'未登记' if floor is None else f'{floor:.2f}'}（标定后登记到 relevance_floor.REGISTERED）")
            report(run(cases, IntentFunnel(lambda: encoder, kb_probe=probe), kb_ids), args.errors)
            if args.sweep:
                kb_sweep(cases, encoder, kb_ids, probe)
        finally:
            close()
    if args.sweep:
        sweep(cases, encoder)


if __name__ == "__main__":
    main()
