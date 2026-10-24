import argparse
import sys
from pathlib import Path

from . import config
from .agent import Agent, sqlite_checkpointer
from .db import create_db_engine
from .feishu import FeishuError, looks_like_feishu
from .feishu.client import default_client
from .feishu.service import import_link
from .materials import MaterialStore
from .llm import ModelConfigurationError, create_chat_model
from .skills import SKILLS

REPL_HELP = (
    "命令：/file <路径或飞书链接> [指令]  读取并处理  |  /add <路径或飞书链接>  存入材料库  |  /materials  列出材料"
    "  |  /context  查看上下文用量  |  /reset  开始新会话  |  /exit  退出"
)
NO_INSTRUCTION = "（用户只提供了文件，没有说明要做什么）"


def add_file(agent: Agent, path: str | Path):
    """文件存入材料库（按内容去重，同一文件不重复解析入库）。飞书链接读取当前内容后存入。"""
    if looks_like_feishu(str(path)):
        try:
            with default_client() as client:
                return import_link(client, agent.store, str(path))
        except FeishuError as e:
            raise ValueError(str(e)) from e
    path = Path(path)
    return agent.store.add_file(path.name, path.read_bytes())


def show_context(agent: Agent) -> None:
    """最近一次对话调用的上下文构成、会话记忆版本。"""
    rows = [r for r in agent.context_store.usage(thread_id=agent.thread_id, limit=20) if r.task_type == "chat"]
    if not rows:
        print("（本会话还没有调用记录）")
    else:
        r = rows[0]
        actual = f"，接口实际 {r.input_tokens}" if r.input_tokens else ""
        print(
            f"  上次调用：估算 {r.estimated_tokens} tokens{actual}（上限 {r.context_limit}）\n"
            f"  系统 {r.system_tokens} / 工具定义 {r.tool_schema_tokens} / 记忆 {r.memory_tokens} / "
            f"历史 {r.history_tokens}（{r.history_turns} 轮） / 当前轮 {r.current_tokens}"
            f"（其中材料 {r.material_tokens}、工具结果 {r.tool_result_tokens}）"
        )
        if r.omitted_turns:
            print(f"  有 {r.omitted_turns} 轮历史未放进上下文")
    memory = agent.context_store.latest_memory(agent.thread_id)
    if memory:
        print(f"  会话记忆 v{memory.version}：已压缩 {memory.data.get('compacted_turns', 0)} 轮")


def repl(agent: Agent, skill: str | None) -> None:
    print(f"Briefly 已就绪（会话 {agent.thread_id}，可用 -t 恢复）。{REPL_HELP}")
    while True:
        try:
            line = input("你> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in {"/exit", "/quit"}:
            break
        if line == "/reset":
            agent.reset()
            print(f"（已开始新会话 {agent.thread_id}）")
            continue
        if line == "/context":
            show_context(agent)
            continue
        if line.startswith("/add "):
            try:
                material, created = add_file(agent, line[5:].strip())
            except (OSError, ValueError) as e:
                print(f"导入失败: {e}")
                continue
            state = "已导入" if created else "已存在"
            print(f"（{state}：材料 {material.id}《{material.title}》，{material.char_count} 字）")
            continue
        if line == "/materials":
            for m in agent.store.list_materials():
                print(f"  {m.id}. {m.title}（{m.char_count} 字）")
            continue
        material_ids = None
        if line.startswith("/file "):
            # 文件存入材料库，按材料处理：短文件全文附进消息，长文件只附清单、由材料工具按需读取
            path, _, instruction = line[6:].strip().partition(" ")
            try:
                material, _ = add_file(agent, path)
            except (OSError, ValueError) as e:
                print(f"读取失败: {e}")
                continue
            line, material_ids = instruction.strip() or NO_INSTRUCTION, [material.id]
        print(f"助手> {agent.chat(line, skill, material_ids=material_ids)}\n")


def main() -> None:
    if sys.argv[1:2] == ["track"]:  # 追踪文件；子命令与“briefly 文件路径”的用法分开解析
        from .tracking.cli import main as track_main

        sys.exit(track_main(sys.argv[2:]))
    if sys.argv[1:2] == ["weekly"]:  # 周报：整理审核清单、审核、生成、定时
        from .weekly.cli import main as weekly_main

        sys.exit(weekly_main(sys.argv[2:]))
    ap = argparse.ArgumentParser(prog="briefly", description="办公 AI Agent（追踪文件见 briefly track -h，周报见 briefly weekly -h）")
    ap.add_argument(
        "input", nargs="?", help="文件路径（txt/md/docx/pdf/xlsx/csv）或飞书文档链接，- 表示标准输入；不填则进入对话模式"
    )
    ap.add_argument("-s", "--skill", choices=list(SKILLS), help="指定能力；不指定则由模型判断")
    ap.add_argument("-e", "--extra", default="", help="额外要求，如“控制在200字内”")
    ap.add_argument("-o", "--output", help="结果写入文件，默认打印")
    ap.add_argument("-t", "--thread", help="会话编号，用于恢复之前的对话")
    args = ap.parse_args()

    try:
        model = create_chat_model()
    except ModelConfigurationError as exc:
        sys.exit(str(exc))
    agent = Agent(
        model,
        checkpointer=sqlite_checkpointer(config.DB_PATH),
        thread_id=args.thread,
        store=MaterialStore(create_db_engine()),
    )
    if args.input is None:
        repl(agent, args.skill)
        return

    if args.input == "-":
        text = sys.stdin.read()
        if args.extra:
            text = f"{text}\n\n【额外要求】{args.extra}"
        result = agent.chat(text, args.skill)  # 过长的输入在 chat 里转存为材料
    else:
        try:
            material, _ = add_file(agent, args.input)
        except (OSError, ValueError) as e:
            sys.exit(f"读取失败：{e}")
        result = agent.chat(args.extra or NO_INSTRUCTION, args.skill, material_ids=[material.id])

    if args.output:
        Path(args.output).write_text(result, encoding="utf-8")
    else:
        print(result)


if __name__ == "__main__":
    main()
