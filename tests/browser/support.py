"""隔离网页服务及确定性模型；供网页回归和手动真实模型走查共用。"""
import json
import asyncio
import os
import re
import random
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import uvicorn
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from briefly import config
from briefly.agent import Agent, sqlite_checkpointer
from briefly.api import create_app
from briefly.db import create_db_engine
from briefly.materials import MaterialStore
from fakes import FunctionModel, structured

MEETING = """支付改造会议文字稿（虚构）
李强负责整理测试记录，截止2026-10-23。
赵琳负责更新接口文档，截止时间待定。
可以考虑补一份竞品分析，尚未决定是否做。
"""
TASK = "李强负责整理测试记录，截止2026-10-23。"
PROGRESS = "已完成测试记录初稿，正在复核异常分支。"


def server_event_loop():
    # 隔离服务线程仅处理TCP，不创建asyncio子进程。Windows Proactor在浏览器
    # 断开并行请求后可能留下closing transport；用Selector让关闭回调正常完成。
    # 仅影响测试服务，Playwright自身仍使用支持子进程的默认事件循环。
    return asyncio.SelectorEventLoop() if os.name == "nt" else asyncio.new_event_loop()


def fake_model():
    def respond(messages):
        system, prompt = str(messages[0].content), str(messages[-1].content)
        if "TodoCandidates" in system:
            sources = json.loads(prompt)["原始来源"]
            separate = next((s for s in sources if "负责人另处依据" in s["content"]), None)
            if separate:
                return structured("TodoCandidates", items=[dict(title="整理测试记录", source_id=separate["id"],
                    quote="整理测试记录。", owner="李强", owner_quote="李强负责测试。",
                    deadline="2026-10-23", deadline_quote="截止2026-10-23。")])
            source = next(s for s in sources if TASK in s["content"])
            return structured("TodoCandidates", items=[dict(title="整理测试记录", source_id=source["id"],
                quote=TASK, owner="李强", owner_quote=TASK, deadline="2026-10-23")])
        if "你负责从一段材料中提取要点" in system:
            rows = []
            for quote, category in [(TASK, "plan"), ("赵琳负责更新接口文档，截止时间待定。", "plan"), (PROGRESS, "in_progress")]:
                if quote in prompt:
                    rows.append(dict(category=category, point=quote, quote=quote, importance="high"))
            return structured("Extraction", items=rows)
        if "判断几处" in system:
            return structured("_Judgments", judgments=[])
        if "撰写周报" in system:
            rows = re.findall(r"(?m)^- #(\d+) 【([^】]+)】([^\n]+)", prompt)
            sections = {"本周完成": [], "进行中": [], "问题与风险": [], "下周计划": []}
            for number, kind, point in rows:
                section = {"已完成":"本周完成", "进行中":"进行中", "问题风险":"问题与风险"}.get(kind,"下周计划")
                sections[section].append(f"- {point}〔#{number}〕")
            return AIMessage("【周报标题】本周周报\n" + "\n".join(f"【{k}】\n" + ("\n".join(v) or "无") for k,v in sections.items()))
        latest = next(str(m.content) for m in reversed(messages) if isinstance(m, HumanMessage) and not m.additional_kwargs.get("internal"))
        if "首行必须选择一个状态" in latest:
            quote = re.search(r"【材料开始】\n(.*?)\n【材料结束】", latest, re.S)
            return AIMessage("【可答性：可回答】" + (quote[1][:120] if quote else "资料未提及") + "〔依据1〕")
        if "证据缺口" in latest:
            if isinstance(messages[-1], ToolMessage):
                return AIMessage("【一句话总结】仅依据当前输入整理。")
            return AIMessage("", tool_calls=[{"name": "read_material", "args": {"material_id": 99999}, "id": "missing-source"}])
        if "虚报成功" in latest:
            return AIMessage("已为你保存待办任务。")
        if "模拟失败" in latest:
            raise RuntimeError("模拟服务暂不可用")
        if "异常提醒" in latest:
            return AIMessage("【一句话总结】请查看第 2 块，支付联调完成。")
        if "加到我的待办" in latest:  # 业务操作类回复：给了待办表，但没写【待办清单】栏目
            return AIMessage("可以加入待办的事项：\n|任务|负责人|截止时间|\n|---|---|---|\n|整理测试记录|李强|2026-10-23|\n请用“预览并保存待办”确认后保存。")
        if "只保留李强" in latest:
            return AIMessage("【待办清单】\n|任务|负责人|截止时间|\n|---|---|---|\n|整理测试记录|李强|2026-10-23|\n【待确认】无")
        return AIMessage("【会议信息】支付改造会议\n【议题与结论】明确后续分工\n【决议】未提及\n【行动项】\n|任务|负责人|截止时间|\n|---|---|---|\n|整理测试记录|李强|2026-10-23|\n|更新接口文档|赵琳|待定|\n【遗留问题】竞品分析尚未决定是否做。")
    return FunctionModel(respond=respond, calls=[])


@contextmanager
def live_app(root: Path, model_factory=fake_model, embeddings_factory=None):
    """只监听随机本地端口；不启用定时、真实飞书或邮件，退出关闭线程和数据库。"""
    root.mkdir(parents=True, exist_ok=True)
    overrides = dict(DB_PATH=str(root/"chat.db"), TRACKING_DIR=str(root/"tracking"),
                     FEISHU_APP_ID="", FEISHU_APP_SECRET="", FEISHU_USER_TOKEN_FILE=str(root/"no-token.json"),
                     SMTP_USER="", SMTP_PASSWORD="", KNOWLEDGE_DIR=str(root/"knowledge"), EMBEDDING_DIR="")
    original = {k:getattr(config,k) for k in overrides}
    for k,v in overrides.items():
        setattr(config,k,v)
    engine = create_db_engine(f"sqlite:///{(root/'app.db').as_posix()}")
    store = MaterialStore(engine)
    saver = sqlite_checkpointer(root/"chat.db")
    app = create_app(store=store, model_factory=model_factory,
        agent_factory=lambda s: Agent(model_factory(), store=s, checkpointer=saver), tracking_scheduler=False)
    class BrowserEncoder:
        identity = dict(model="browser-fixture", revision="v1", dimension=2, max_tokens=512,
            artifact_sha256={"tokenizer.json":"a"*64, "model.onnx":"b"*64}, pooling="CLS", normalize=True, query_prefix="")
        def token_length(self, text):
            return len(text)
        def embed_documents(self, texts):
            return [[len(text), 1] for text in texts]
        def embed_query(self, text):
            return [len(text), 1]
    app.state.knowledge_imports.encoder_factory = embeddings_factory or BrowserEncoder
    sock = socket.socket()
    # Windows 的动态端口范围可能含 Chromium 禁用的低端口（曾随机到4045）。
    # 直接原子绑定高位端口，冲突时重选；保留已绑定socket交给uvicorn，避免探测/启动竞态。
    for _ in range(100):
        try:
            sock.bind(("127.0.0.1", random.randrange(49152, 65536)))
            break
        except OSError:
            continue
    else:
        sock.close()
        raise RuntimeError("无法为隔离网页服务分配高位本地端口")
    port = sock.getsockname()[1]
    # Windows 上连接异常关闭（_call_connection_lost 报错）时 asyncio 的 server.wait_closed() 可能永不返回；
    # 设置优雅关闭超时，让 uvicorn 到时继续退出，而不是卡满 15 秒
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", timeout_graceful_shutdown=5,
                                         loop=f"{__name__}:server_event_loop"))
    thread = threading.Thread(target=server.run, kwargs={"sockets":[sock]}, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=1) as client:
            for _ in range(100):
                if not thread.is_alive():
                    raise RuntimeError("隔离网页服务未能启动")
                try:
                    if client.get("/skills").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(.05)
            else:
                raise RuntimeError("隔离网页服务启动超时")
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        # 退出超时时先记下服务在等什么（未关闭的连接、后台任务、各线程调用栈），再清理
        stuck = shutdown_report(server, thread) if thread.is_alive() else ""
        sock.close()
        saver.conn.close()
        engine.dispose()
        for k,v in original.items():
            setattr(config,k,v)
        if thread.is_alive():
            (root/"shutdown-timeout.txt").write_text(stuck, encoding="utf-8")
            raise RuntimeError(f"隔离网页服务未正常退出：\n{stuck}")


def shutdown_report(server, thread) -> str:
    import sys
    import traceback
    state = server.server_state
    lines = [f"未关闭连接 {len(state.connections)} 个，后台任务 {len(state.tasks)} 个"]
    for ident, frame in sys._current_frames().items():
        if ident == threading.get_ident():
            continue
        name = next((t.name for t in threading.enumerate() if t.ident == ident), str(ident))
        lines.append(f"--- 线程 {name}{'（服务主线程）' if ident == thread.ident else ''}")
        lines += [x.rstrip() for x in traceback.format_stack(frame)[-8:]]
    return "\n".join(lines)
