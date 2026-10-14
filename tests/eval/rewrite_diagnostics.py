"""评估：对话里的长材料总结（工具做 Map-Reduce → Agent 改写），信息在哪一步丢失。手动运行，调用真实模型。

    python tests/eval/rewrite_diagnostics.py -n 2

两种设置：正常工具预算；把 summarize_materials 的预算调小、强制截断（测出截断损失）。
每次用临时数据库，不碰 data/。结果写到 files/eval/rewrite-<时间>.md。只测量，不改变输出链路。
"""
import argparse
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src")]

from briefly import tool_budget  # noqa: E402
from briefly.agent import Agent  # noqa: E402
from briefly.db import create_db_engine  # noqa: E402
from briefly.diagnostics import ConflictSpec, Fact, diagnose, stages_from_history  # noqa: E402
from briefly.materials import MaterialStore  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
MATERIALS = {"周志": "week_log.md", "项目A排期": "proj_a.md", "项目A会议": "proj_b.md"}
FACTS = [
    Fact("登录联调 32 条", ["32 条"], "周志"),
    Fact("回调耗时 1.8→200", ["1.8", "200"], "周志"),
    Fact("游标分页", ["游标分页"], "周志"),
    Fact("李经理确认上线", ["李经理"], "周志"),
    Fact("压测报告", ["压测报告"], "周志"),
    Fact("订单导出需求", ["订单导出"], "周志"),
    Fact("接口联调（项目A）", ["接口联调"], "项目A排期"),
    Fact("验收标准（项目A）", ["验收标准"], "项目A会议"),
]
CONFLICTS = [ConflictSpec("项目A 交付日期", ["10 月 20 日", "10 月 25 日"])]
QUESTION = "总结这三份材料的主要内容"
SETTINGS = {"正常预算": None, "强制截断": 500}


def run_once(limit: int | None) -> dict:
    original = tool_budget.TOOL_LIMITS["summarize_materials"]
    if limit:
        tool_budget.TOOL_LIMITS["summarize_materials"] = limit
    try:
        with tempfile.TemporaryDirectory() as tmp:
            engine = create_db_engine(f"sqlite:///{Path(tmp, 'app.db').as_posix()}")
            store = MaterialStore(engine)
            texts, ids = {}, []
            for title, name in MATERIALS.items():
                texts[title] = (FIXTURES / name).read_text(encoding="utf-8")
                ids.append(store.add_text(title, texts[title])[0].id)
            agent = Agent(store=store)
            started = time.monotonic()
            agent.chat(QUESTION, material_ids=ids)
            seconds = round(time.monotonic() - started, 1)
            st = stages_from_history(agent.history)
            usage = [r for r in agent.context_store.usage(thread_id=agent.thread_id, limit=200)]
            st.input_tokens = sum(r.input_tokens or 0 for r in usage)
            st.output_tokens = sum(r.output_tokens or 0 for r in usage)
            st.calls, st.seconds = len(usage), seconds
            d = diagnose(FACTS, CONFLICTS, st, texts)
            engine.dispose()
            return {"stages": st, "diagnosis": d}
    finally:
        tool_budget.TOOL_LIMITS["summarize_materials"] = original


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=2)
    args = ap.parse_args()
    lines = [f"# Agent 二次改写诊断（{datetime.now():%Y-%m-%d %H:%M}，每种设置 {args.n} 次）", "", f"问题：{QUESTION}；基准事实 {len(FACTS)} 条，基准冲突 {len(CONFLICTS)} 处。", ""]
    lines += ["| 设置 | 轮 | 调用工具 | 截断 | 事实保留 MR / 工具消息 / 回答 | 冲突保留 MR / 工具消息 / 回答 | 损失来源（最终回答缺失） | 中途缺失后读回 | 来源归属错误 | 无来源断言 | 输入/输出 tokens | 调用 | 秒 |", "|" + "---|" * 13]
    details = []
    for name, limit in SETTINGS.items():
        for k in range(1, args.n + 1):
            print(f"{name} 第 {k} 轮……", flush=True)
            r = run_once(limit)
            st, d = r["stages"], r["diagnosis"]
            ret = " / ".join(f"{d.retention[s]:.0%}" for s in ("map_reduce", "tool_message", "answer"))
            cret = " / ".join(f"{d.conflict_retention[s]:.0%}" for s in ("map_reduce", "tool_message", "answer"))
            lost = "；".join(f"{k2}：{'、'.join(v)}" for k2, v in d.lost_at.items()) or "无"
            if d.conflicts_lost_at:
                lost += "；冲突丢在 " + "、".join(d.conflicts_lost_at)
            lines.append(
                f"| {name} | {k} | {'是' if st.tool_called else '否'} | {'是' if st.truncated else '否'} | {ret} | {cret} | {lost} | {'、'.join(d.recovered) or '无'} | "
                f"{len(d.attribution_errors)} | {len(d.unsupported)} | {st.input_tokens}/{st.output_tokens} | {st.calls} | {st.seconds} |"
            )
            details += [f"## {name} 第 {k} 轮", "", "来源归属错误：" + ("；".join(d.attribution_errors) or "无"), "",
                        "无来源断言：" + ("、".join(d.unsupported) or "无"), "", "最终回答：", "", st.answer, ""]
    out = ROOT / "files" / "eval" / f"rewrite-{datetime.now():%Y%m%d-%H%M%S}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines + [""] + details), encoding="utf-8")
    print("\n".join(lines))
    print(f"详细结果：{out}")


if __name__ == "__main__":
    main()
