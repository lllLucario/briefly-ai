"""单智能体：一份总提示词 + 五项能力说明，由模型自行判断任务，带会话记忆和工具调用循环。"""
import json

from . import config
from .llm import LLM
from .skills import SKILLS, read_prompt
from .tools import DEFAULT_TOOLS, Tool


class Agent:
    def __init__(self, llm: LLM | None = None, tools: list[Tool] | None = None) -> None:
        self.llm = llm or LLM()
        self.tools = {t.name: t for t in (DEFAULT_TOOLS if tools is None else tools)}
        self.history: list[dict] = []  # 不含 system 消息

    def reset(self) -> None:
        self.history.clear()

    def system_prompt(self, skill: str | None = None) -> str:
        parts = [read_prompt("base"), "# 能力说明", *(s.prompt for s in SKILLS.values())]
        if skill:
            parts.append(f"# 本次任务\n用户已指定使用「{SKILLS[skill].description}」能力，直接按该能力执行。")
        return "\n\n".join(parts)

    def chat(self, text: str, skill: str | None = None) -> str:
        if skill and skill not in SKILLS:
            raise ValueError(f"未知技能: {skill}，可选: {', '.join(SKILLS)}")

        self.history.append({"role": "user", "content": text})
        self._trim_history()
        system = {"role": "system", "content": self.system_prompt(skill)}
        tool_specs = [t.spec() for t in self.tools.values()]

        for _ in range(config.MAX_TOOL_ROUNDS):
            msg = self.llm.create([system, *self.history], tool_specs)
            self.history.append(_assistant_dict(msg))
            if not msg.tool_calls:
                return msg.content or ""
            for call in msg.tool_calls:
                self.history.append(self._run_tool(call))

        reply = "工具调用次数过多，已中止，请换一种方式描述需求。"
        self.history.append({"role": "assistant", "content": reply})
        return reply

    def _run_tool(self, call) -> dict:
        try:
            tool = self.tools.get(call.function.name)
            if tool is None:
                raise ValueError(f"未知工具: {call.function.name}")
            content = tool.func(**json.loads(call.function.arguments or "{}"))
        except Exception as e:
            content = f"工具执行失败: {e}"
        return {"role": "tool", "tool_call_id": call.id, "content": str(content)}

    def _trim_history(self) -> None:
        """超长时丢弃最早的消息，并保证首条是用户消息，避免 tool_calls 与工具结果被拆开。"""
        if len(self.history) <= config.MAX_HISTORY:
            return
        drop = len(self.history) - config.MAX_HISTORY
        while drop < len(self.history) - 1 and self.history[drop]["role"] != "user":
            drop += 1
        del self.history[:drop]


def _assistant_dict(msg) -> dict:
    out: dict = {"role": "assistant", "content": msg.content or ""}
    if msg.tool_calls:
        out["tool_calls"] = [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.function.name, "arguments": c.function.arguments},
            }
            for c in msg.tool_calls
        ]
    return out
