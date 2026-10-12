"""单智能体：一份总提示词 + 五项能力说明，由模型自行判断任务。

基于 LangChain create_agent：工具调用循环由框架负责，会话记忆存在 LangGraph checkpointer 中，
以 thread_id 区分会话。本模块只补充项目自己的规则：动态提示词、上下文管理（清理旧工具结果、按 token 裁剪、
超长兜底，都只影响发给模型的内容）、工具轮数上限、工具报错回传。
"""
import sqlite3
import uuid
from pathlib import Path

from langchain.agents import create_agent
from langchain.agents.middleware import (
    ClearToolUsesEdit,
    ContextEditingMiddleware,
    ModelRequest,
    ToolErrorMiddleware,
    before_model,
    dynamic_prompt,
    wrap_model_call,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

from . import config
from .context import Context
from .llm import create_chat_model
from .materials import MaterialStore, wrap_material
from .skills import SKILLS, read_prompt
from .tools import DEFAULT_TOOLS
from .tokens import count_message_tokens, estimate_tokens
from .tools.materials import material_tools

TOOL_ROUNDS_EXCEEDED = "工具调用次数过多，已中止，请换一种方式描述需求。"
CONTEXT_OVERFLOW = (
    "这次对话和材料的内容超出了模型一次能处理的长度。请开启新会话（/reset），"
    "或让我用 summarize_materials 先把材料提炼成要点，再继续。"
)


def build_system_prompt(skill: str | None = None) -> str:
    parts = [read_prompt("base"), "# 能力说明", *(s.prompt for s in SKILLS.values())]
    if skill:
        parts.append(f"# 本次任务\n用户已指定使用「{SKILLS[skill].description}」能力，直接按该能力执行。")
    return "\n\n".join(parts)


def trim_history(messages: list[AnyMessage], budget: int) -> list[AnyMessage]:
    """从最新往前保留，总量不超过 budget tokens；首条必须是用户消息，避免 tool_calls 与工具结果被拆开。

    当前这一轮（最后一条用户消息起）即使超出预算也完整保留。
    """
    sizes = [count_message_tokens([m]) for m in messages]
    if sum(sizes) <= budget:
        return messages
    human = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
    if not human:
        return messages
    start = human[-1]
    for i in reversed(human[:-1]):
        if sum(sizes[i:]) > budget:
            break
        start = i
    return messages[start:]


def _is_context_overflow(error: Exception) -> bool:
    text = str(error).lower()
    return "context" in text and any(k in text for k in ("length", "too long", "maximum", "exceed"))


@dynamic_prompt
def _system_prompt(request: ModelRequest) -> str:
    context = request.runtime.context
    return build_system_prompt(context.skill if context else None)


@wrap_model_call
def _trim_history(request: ModelRequest, handler):
    # 只裁剪发给模型的消息，checkpointer 里仍保存完整历史
    return handler(request.override(messages=trim_history(request.messages, config.HISTORY_TOKENS)))


@wrap_model_call
def _context_overflow(request: ModelRequest, handler):
    """兜底：前面的清理和裁剪之后接口仍报上下文超长时，给用户明确提示，不让程序崩溃。"""
    try:
        return handler(request)
    except Exception as e:
        if _is_context_overflow(e):
            return AIMessage(CONTEXT_OVERFLOW)
        raise


def _clear_old_tool_results() -> ContextEditingMiddleware:
    """发给模型的消息过长时，把较早的工具结果（多为材料原文）换成占位文字；保存的历史不受影响。"""
    edit = ClearToolUsesEdit(
        trigger=config.CONTEXT_CLEAR_TOKENS,
        keep=3,
        placeholder="[已清理：较早时工具返回的材料内容。需要时请重新调用工具读取]",
    )
    return ContextEditingMiddleware(edits=[edit], token_counter=count_message_tokens)


@before_model(can_jump_to=["end"])
def _limit_tool_rounds(state, runtime):
    """本轮提问中模型已调用 MAX_TOOL_ROUNDS 次仍未给出答复时中止。"""
    messages = state["messages"]
    last_human = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))
    rounds = sum(isinstance(m, AIMessage) for m in messages[last_human:])
    if rounds >= config.MAX_TOOL_ROUNDS:
        return {"jump_to": "end", "messages": [AIMessage(TOOL_ROUNDS_EXCEEDED)]}
    return None


def _tool_error(error: Exception, request) -> str:
    # 工具失败不抛给用户，作为工具结果回传模型，由模型如实说明
    return f"工具执行失败: {error}"


def sqlite_checkpointer(path: str | Path) -> SqliteSaver:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return SqliteSaver(sqlite3.connect(path, check_same_thread=False))


class Agent:
    def __init__(
        self,
        model: BaseChatModel | None = None,
        tools: list[BaseTool] | None = None,
        checkpointer: BaseCheckpointSaver | None = None,
        thread_id: str | None = None,
        store: MaterialStore | None = None,
    ) -> None:
        self.store = store
        model = model or create_chat_model()
        tools = list(DEFAULT_TOOLS if tools is None else tools)
        if store is not None:
            tools += material_tools(store, model)  # 完整提炼工具与智能体用同一个模型
        self.graph = create_agent(
            model,
            tools=tools,
            middleware=[  # 列表中靠前的包在外层：先定提示词，兜底包住后面所有步骤，再清理工具结果、再裁剪
                _system_prompt,
                _context_overflow,
                _clear_old_tool_results(),
                _trim_history,
                _limit_tool_rounds,
                ToolErrorMiddleware(on_error=_tool_error),
            ],
            context_schema=Context,
            checkpointer=checkpointer or InMemorySaver(),
        )
        self.thread_id = thread_id or _new_thread_id()

    def reset(self) -> None:
        """开始新会话；旧会话仍保存在 checkpointer 中。"""
        self.thread_id = _new_thread_id()

    @property
    def history(self) -> list[AnyMessage]:
        state = self.graph.get_state({"configurable": {"thread_id": self.thread_id}})
        return state.values.get("messages", [])

    def chat(self, text: str, skill: str | None = None, material_ids: list[int] | None = None) -> str:
        if skill and skill not in SKILLS:
            raise ValueError(f"未知技能: {skill}，可选: {', '.join(SKILLS)}")
        if material_ids:
            text = self._attach_materials(text, material_ids)
        result = self.graph.invoke(
            {"messages": [HumanMessage(text)]},
            config={"configurable": {"thread_id": self.thread_id}},
            context=Context(skill=skill, material_ids=material_ids or None),
        )
        return result["messages"][-1].text

    def _attach_materials(self, text: str, material_ids: list[int]) -> str:
        """选中材料较短时全文附在消息后；较长时只附清单，由模型用材料工具读取。"""
        if self.store is None:
            raise ValueError("未配置材料库，无法使用材料")
        materials = [self.store.get(i) for i in material_ids]
        if sum(estimate_tokens(m.content) for m in materials) <= config.INLINE_MATERIAL_TOKENS:
            blocks = [wrap_material(f"【材料 {m.id}《{m.title}》】", m.content) for m in materials]
            return "\n\n".join([text, *blocks])
        chunks, sections = self.store.chunk_counts(material_ids), self.store.section_counts(material_ids)
        listing = "\n".join(
            f"- 材料 {m.id}《{m.title}》：{m.char_count} 字，{chunks[m.id]} 块，{sections.get(m.id, 0)} 个章节"
            for m in materials
        )
        return (
            f"{text}\n\n【已选材料】内容较长未附全文，请用 get_outline / search_materials / read_material 读取：\n{listing}"
        )


def _new_thread_id() -> str:
    return uuid.uuid4().hex[:12]
