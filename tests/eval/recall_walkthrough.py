"""压缩摘要不含尾部事实、SQLite重开后，真实模型通过分页回查原话。

准备与压缩使用假模型，避免花钱制造长对话；只对回查阶段调用真实API。
python tests/eval/recall_walkthrough.py -n 2
"""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage  # noqa: E402
from briefly import config  # noqa: E402
from briefly.agent import Agent, sqlite_checkpointer  # noqa: E402
from briefly.context_store import ContextStore  # noqa: E402
from briefly.db import create_db_engine  # noqa: E402
from briefly.llm import create_chat_model  # noqa: E402
import briefly.tools.context as context_module  # noqa: E402
from fakes import FunctionModel, structured  # noqa: E402


def run(root):
    root.mkdir(parents=True)
    engine=create_db_engine(f"sqlite:///{(root/'app.db').as_posix()}")
    saver=sqlite_checkpointer(root/'chat.db')
    old_keep,old_chars,old_rounds=config.COMPACT_KEEP_TURNS,context_module._page_chars,config.MAX_TOOL_ROUNDS
    config.COMPACT_KEEP_TURNS=0
    config.MAX_TOOL_ROUNDS=8
    context_module._page_chars=lambda:700
    try:
        def record(messages):
            if "【当前记忆】" in messages[-1].text:
                return structured("MemoryUpdate",current_goal="归档记录",add=[],remove=[])
            return AIMessage("已记录背景。")
        first=Agent(FunctionModel(respond=record,calls=[]),context_store=ContextStore(engine),checkpointer=saver)
        first.chat("归档背景。"*600+"末尾审计词：松柏；禁止事项：不得代发邮件。")
        thread=first.thread_id
        assert first.compactor.maybe_compact(thread,first.history,force=True)
        saver.conn.close()
        saver=sqlite_checkpointer(root/'chat.db')
        second=Agent(create_chat_model(max_retries=0,timeout=60),context_store=ContextStore(engine),checkpointer=saver)
        started=time.monotonic()
        answer=second.chat("请使用recall_conversation从第1轮page=1开始回查。若未读完，继续用turn=1和下一页直到结尾，再回答末尾审计词及禁止事项。不要只读第一页，不从压缩摘要猜。",thread_id=thread)
        history=second.history_of(thread)
        start=max(i for i,m in enumerate(history) if isinstance(m,HumanMessage) and not m.additional_kwargs.get("internal"))
        calls=[c for m in history[start+1:] if isinstance(m,AIMessage) for c in m.tool_calls if c["name"]=="recall_conversation"]
        pages=[c["args"].get("page",1) for c in calls if c["args"].get("turn")==1]
        results=[m.text for m in history[start+1:] if isinstance(m,ToolMessage)]
        checks={"审计词正确":"松柏" in answer,"禁止事项保留":"代发邮件" in answer and any(w in answer for w in ["禁止","不得","不能","不可"]),
                "确实逐页回查":len(pages)>1 and pages==list(range(1,max(pages)+1)),
                "末页已读完":bool(results) and "已读完" in results[-1]}
        return {"ok":all(checks.values()),"answer":answer,"checks":checks,"pages":pages,
                "seconds":round(time.monotonic()-started,1),"logical_calls":sum(isinstance(m,AIMessage) for m in history[start+1:])}
    finally:
        saver.conn.close()
        engine.dispose()
        config.COMPACT_KEEP_TURNS=old_keep
        config.MAX_TOOL_ROUNDS=old_rounds
        context_module._page_chars=old_chars


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-n',type=int,default=2)
    args=parser.parse_args()
    if args.n<1:parser.error("次数必须为正数")
    out=Path('files/eval/recall')/datetime.now(ZoneInfo('Australia/Sydney')).strftime('%Y%m%d-%H%M%S')
    out.mkdir(parents=True)
    results=[]
    for i in range(1,args.n+1):
        try:
            result=run(out/f'run-{i}')
        except Exception as error:
            result={"ok":False,"error":str(error)}
        results.append(result)
        (out/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
        print(f"第{i}轮 {'通过' if result['ok'] else '失败'}：{result.get('pages',[])}",flush=True)
        if not result['ok']:break
    print(out)
    if len(results)!=args.n or not all(x['ok'] for x in results):raise SystemExit(1)


if __name__=='__main__':main()
