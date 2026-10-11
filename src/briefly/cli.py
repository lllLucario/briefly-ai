import argparse
import sys
from pathlib import Path

from .agent import Agent
from .loaders import load_text
from .skills import SKILLS

REPL_HELP = "命令：/file <路径> [指令]  读取文件  |  /reset  清空对话  |  /exit  退出"


def repl(agent: Agent, skill: str | None) -> None:
    print(f"Briefly 已就绪。{REPL_HELP}")
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
            print("（对话已清空）")
            continue
        if line.startswith("/file "):
            path, _, instruction = line[6:].strip().partition(" ")
            try:
                line = f"{instruction}\n\n{load_text(path)}".strip()
            except (OSError, ValueError) as e:
                print(f"读取失败: {e}")
                continue
        print(f"助手> {agent.chat(line, skill)}\n")


def main() -> None:
    ap = argparse.ArgumentParser(prog="briefly", description="办公 AI Agent")
    ap.add_argument("input", nargs="?", help="文件路径（txt/md/docx/pdf），- 表示标准输入；不填则进入对话模式")
    ap.add_argument("-s", "--skill", choices=list(SKILLS), help="指定能力；不指定则由模型判断")
    ap.add_argument("-e", "--extra", default="", help="额外要求，如“控制在200字内”")
    ap.add_argument("-o", "--output", help="结果写入文件，默认打印")
    args = ap.parse_args()

    agent = Agent()
    if args.input is None:
        repl(agent, args.skill)
        return

    text = sys.stdin.read() if args.input == "-" else load_text(args.input)
    if args.extra:
        text = f"{text}\n\n【额外要求】{args.extra}"
    result = agent.chat(text, args.skill)

    if args.output:
        Path(args.output).write_text(result, encoding="utf-8")
    else:
        print(result)


if __name__ == "__main__":
    main()
