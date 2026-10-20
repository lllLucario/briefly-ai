"""隔离网页服务及确定性模型；供网页回归和手动真实模型走查共用。"""
import json
import re
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import uvicorn
from langchain_core.messages import AIMessage, HumanMessage

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


def fake_model():
    def respond(messages):
        system, prompt = str(messages[0].content), str(messages[-1].content)
        if "TodoCandidates" in system:
            sources = json.loads(prompt)["原始来源"]
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
        if "模拟失败" in latest:
            raise RuntimeError("模拟服务暂不可用")
        if "异常提醒" in latest:
            return AIMessage("【一句话总结】请查看第 2 块，支付联调完成。")
        if "只保留李强" in latest:
            return AIMessage("【待办清单】\n|任务|负责人|截止时间|\n|---|---|---|\n|整理测试记录|李强|2026-10-23|\n【待确认】无")
        return AIMessage("【会议信息】支付改造会议\n【议题与结论】明确后续分工\n【决议】未提及\n【行动项】\n|任务|负责人|截止时间|\n|---|---|---|\n|整理测试记录|李强|2026-10-23|\n|更新接口文档|赵琳|待定|\n【遗留问题】竞品分析尚未决定是否做。")
    return FunctionModel(respond=respond, calls=[])


@contextmanager
def live_app(root: Path, model_factory=fake_model):
    """只监听随机本地端口；不启用定时、真实飞书或邮件，退出关闭线程和数据库。"""
    root.mkdir(parents=True, exist_ok=True)
    overrides = dict(DB_PATH=str(root/"chat.db"), TRACKING_DIR=str(root/"tracking"),
                     FEISHU_APP_ID="", FEISHU_APP_SECRET="", FEISHU_USER_TOKEN_FILE=str(root/"no-token.json"),
                     SMTP_USER="", SMTP_PASSWORD="")
    original = {k:getattr(config,k) for k in overrides}
    for k,v in overrides.items():
        setattr(config,k,v)
    engine = create_db_engine(f"sqlite:///{(root/'app.db').as_posix()}")
    store = MaterialStore(engine)
    saver = sqlite_checkpointer(root/"chat.db")
    app = create_app(store=store, model_factory=model_factory,
        agent_factory=lambda s: Agent(model_factory(), store=s, checkpointer=saver), tracking_scheduler=False)
    sock = socket.socket()
    sock.bind(("127.0.0.1",0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
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
        sock.close()
        saver.conn.close()
        engine.dispose()
        for k,v in original.items():
            setattr(config,k,v)
        if thread.is_alive():
            raise RuntimeError("隔离网页服务未正常退出")
