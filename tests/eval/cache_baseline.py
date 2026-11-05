"""模型响应缓存的前后对比基线：虚构资料、固定问题集、重复场景。手动运行，pytest 不收集。

三个阶段都走完整的 /chat：缓存关闭 → 打开后首次（未命中，写入）→ 再问一遍（命中）。外加一次最终汇总（synthesize）的同样三段。
命中率来自这个重复场景，只说明“同样的问题再问一遍”能省多少，不代表线上真实节省比例。

默认假模型（固定延迟），只验证管线和统计。--real-model 才调用 .env 配置的真实模型，只发送下面的虚构资料，需要用户授权。
所有数据在临时目录，进程内调用，不开端口，不读写 data/。检索和意图判断用本地 BGE（--model-dir）。

用法（工作树里要设 PYTHONPATH=src）：
    python tests/eval/cache_baseline.py --model-dir <BGE目录> --output <结果.json> [--real-model --env <.env路径>]
"""
import argparse
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))

from briefly import config  # noqa: E402

# 全部虚构。公司、人名、数字都是为这个基线编的
DOCUMENTS = {
    "星河科技休假制度": "# 星河科技休假制度\n## 年假\n星河科技正式员工每年享有年假12天，须提前3个工作日在系统提交申请。年假可以结转到次年3月31日。\n"
                 "## 病假\n病假须提交二级以上医院证明，每年累计不超过20天，超过部分按事假处理。\n## 婚假\n婚假为10个自然日，须在领证后一年内休完。",
    "星河科技差旅报销办法": "# 星河科技差旅报销办法\n## 住宿标准\n一线城市住宿上限每晚680元，其他城市每晚450元，超标部分由个人承担。\n"
                  "## 交通\n高铁二等座可直接报销，飞机经济舱须部门负责人事先审批。\n## 报销时限\n差旅结束后15个自然日内提交报销单，逾期须财务总监批准。",
    "星河科技值班规范": "# 星河科技值班规范\n## 响应时间\n值班工程师收到一级告警后10分钟内响应，二级告警后30分钟内响应。\n"
                "## 交接\n交接班在每天上午9点进行，须填写交接记录。\n## 升级\n一级告警30分钟未恢复须升级到值班经理。",
}
QUESTIONS = [
    "星河科技正式员工每年有几天年假？",
    "年假可以结转到什么时候？",
    "病假需要提交什么证明？",
    "婚假有多少天？",
    "一线城市住宿上限是多少钱一晚？",
    "差旅结束后多久内要提交报销单？",
    "一级告警多久内要响应？",
    "交接班在每天几点进行？",
]
SYNTH_SYSTEM = "你负责把用户给的工作说明整理成一段不超过80字的总结，只依据给出的内容，不编造。"
SYNTH_REQUEST = "星河科技值班组本周完成告警规则梳理，修复3个误报，下周计划补充升级流程的演练记录。"
FAKE_LATENCY_S = 1.0


def fake_model_factory():
    import re

    from langchain_core.messages import AIMessage

    from fakes import FunctionModel

    def respond(messages):
        time.sleep(FAKE_LATENCY_S)
        text = str(messages[-1].content)
        quote = re.search(r"【材料开始】\n(.*?)\n【材料结束】", text, re.S)
        return AIMessage("【可答性：可回答】" + (quote[1][:60] if quote else "未提及") + "〔依据1〕", response_metadata=dict(finish_reason="stop"))

    model = FunctionModel(respond=respond, calls=[])
    return lambda: model


def build_knowledge(app):
    from briefly.materials import KNOWLEDGE

    imports, knowledge = app.state.knowledge_imports, app.state.knowledge
    encoder = imports.encoder_factory()
    kb = knowledge.create("星河科技制度（虚构）")
    for title, content in DOCUMENTS.items():
        material, _ = app.state.store.add_text(title, content, source=KNOWLEDGE, kind="md")
        doc, version, _ = knowledge.attach(kb.id, material.id)
        imports.lifecycle.build(kb.id, doc.id, version.id, encoder, expected_revision=1)
    knowledge.set_egress(kb.id, True)
    return kb.id


def timing_of(app, thread_id) -> dict:
    from briefly.context_store import ContextStore

    rows = ContextStore(app.state.store.engine).turns(thread_id=thread_id)
    return json.loads(rows[0].note) if rows else {}


def ask_all(client, app, kb_id, phase: str, *, enabled: bool) -> list[dict]:
    """每题开新会话：对话历史不同的影响排除掉，问的就是“同样的问题再问一遍”。"""
    config.LLM_CACHE = enabled
    rows = []
    for question in QUESTIONS:
        response = client.post("/chat", json=dict(text=question, kb_id=kb_id))
        response.raise_for_status()
        body = response.json()
        thread = body["thread_id"]
        answer = client.get(f"/chat/sessions/{thread}").json()["messages"][-1]
        knowledge = answer.get("knowledge") or {}
        egress = knowledge.get("egress") or {}
        rows.append(dict(phase=phase, question=question, route=(body.get("intent") or {}).get("resolved"),
                         status=knowledge.get("status"), from_cache=bool(knowledge.get("from_cache")),
                         evidence_sent=egress.get("evidence_sent"), references=len(knowledge.get("references") or []),
                         answer_chars=len(answer["text"]), **timing_of(app, thread)))
    return rows


def summarize_phase(rows: list[dict]) -> dict:
    def values(name):
        return [r.get(name) or 0 for r in rows]

    return dict(questions=len(rows), shortcut=sum(r["route"] == "knowledge" for r in rows),
                from_cache=sum(r["from_cache"] for r in rows), evidence_sent=sum(values("evidence_sent")),
                model_calls=sum(values("model_calls")), input_tokens=sum(values("input_tokens")),
                output_tokens=sum(values("output_tokens")), provider_cache_hit_tokens=sum(values("provider_cache_hit_tokens")),
                provider_cache_miss_tokens=sum(values("provider_cache_miss_tokens")),
                total_ms_median=statistics.median(values("total_ms")), total_ms_mean=round(statistics.mean(values("total_ms"))),
                total_ms_max=max(values("total_ms")),
                **{f"{k}_ms_mean": round(statistics.mean(values(f"{k}_ms"))) for k in ("intent", "retrieval", "model", "check")})


def synth_scene(app, model_factory) -> list[dict]:
    from briefly.llm_cache import bind_scope, release_scope
    from briefly.summarize import synthesize

    rows = []
    token = bind_scope(app.state.response_cache, "local")
    try:
        for phase, enabled in (("off", False), ("cold", True), ("repeat", True)):
            config.LLM_CACHE = enabled
            started = time.perf_counter()
            result = synthesize(model_factory(), SYNTH_SYSTEM, SYNTH_REQUEST, [])
            rows.append(dict(phase=phase, ms=int((time.perf_counter() - started) * 1000), model_calls=result.usage.calls,
                             input_tokens=result.usage.input_tokens, output_tokens=result.usage.output_tokens))
    finally:
        release_scope(token)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model-dir", required=True, help="本地 BGE 目录")
    parser.add_argument("--output", required=True)
    parser.add_argument("--real-model", action="store_true", help="调用真实模型（需要用户授权，只发送虚构资料）")
    parser.add_argument("--env", help="--real-model 时读取模型配置的 .env；不打印其中的值")
    args = parser.parse_args()

    from fastapi.testclient import TestClient

    from briefly.agent import Agent, sqlite_checkpointer
    from briefly.api import create_app
    from briefly.db import create_db_engine
    from briefly.llm import create_chat_model
    from briefly.llm_cache import cache_stats
    from briefly.materials import MaterialStore

    if args.real_model:
        from dotenv import dotenv_values
        values = dotenv_values(args.env) if args.env else {}
        config.API_KEY = values.get("BRIEFLY_API_KEY") or config.API_KEY
        config.BASE_URL = values.get("BRIEFLY_BASE_URL") or config.BASE_URL
        config.MODEL = values.get("BRIEFLY_MODEL") or config.MODEL
        model_factory = create_chat_model
    else:
        model_factory = fake_model_factory()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        for name, value in dict(ACCESS_TOKEN="", ALLOWED_HOSTS=[*config.DEFAULT_ALLOWED_HOSTS.split(","), "testserver"],
                                DB_PATH=str(root / "chat.db"), TRACKING_DIR=str(root / "tracking"), FEISHU_APP_ID="", FEISHU_APP_SECRET="",
                                FEISHU_USER_TOKEN_FILE=str(root / "none.json"), SMTP_USER="", SMTP_PASSWORD="",
                                KNOWLEDGE_DIR=str(root / "knowledge"), WORKSPACE_INDEX_PATH=str(root / "workspace.sqlite"),
                                EMBEDDING_DIR=args.model_dir).items():
            setattr(config, name, value)
        engine = create_db_engine(f"sqlite:///{(root / 'app.db').as_posix()}")
        saver = sqlite_checkpointer(root / "chat.db")
        app = create_app(MaterialStore(engine), model_factory=model_factory,
                         agent_factory=lambda s: Agent(model_factory(), store=s, checkpointer=saver))
        with TestClient(app) as client:
            kb_id = build_knowledge(app)
            ask_all(client, app, kb_id, "warmup", enabled=False) if not args.real_model else None  # 假模型下预热，不计入
            rows = []
            for phase, enabled in (("off", False), ("cold", True), ("repeat", True)):
                rows += ask_all(client, app, kb_id, phase, enabled=enabled)
            synth = synth_scene(app, model_factory)
            config.LLM_CACHE = True
            stats = cache_stats(engine, [])
        saver.conn.close()
        engine.dispose()

    result = dict(mode="real" if args.real_model else "fake", model=config.MODEL if args.real_model else f"假模型（固定 {FAKE_LATENCY_S}s）",
                  questions=len(QUESTIONS), scenario="虚构资料，同一组问题先关缓存问一遍，开缓存问两遍；每题新会话",
                  phases={p: summarize_phase([r for r in rows if r["phase"] == p]) for p in ("off", "cold", "repeat")},
                  rows=rows, synthesize=synth, stats=stats)
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result["phases"], ensure_ascii=False, indent=2))
    print(json.dumps(synth, ensure_ascii=False))


if __name__ == "__main__":
    main()
