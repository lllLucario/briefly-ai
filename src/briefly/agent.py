"""单智能体：一份总提示词 + 五项能力说明，由模型自行判断任务。

基于 LangChain create_agent：工具调用循环由框架负责，会话记忆存在 LangGraph checkpointer 中，
以 thread_id 区分会话。本模块只补充项目自己的规则：动态提示词、上下文管理、工具轮数上限、工具报错回传。

上下文管理（详见 files/plan-context.md）：
- 输入预检（chat）：用户一次输入过长时，正文存为材料，消息里只留开头结尾原文和引用。
- 会话压缩（chat 前，compaction.py）：未压缩的历史超过阈值时，把较早的对话整理进会话记忆。
- Context Builder（每次调用模型前，context_builder.py）：系统提示词 + 记忆 + 任务状态 + 最近几轮 + 当前轮，统一预算检查。
- 工具预算（每次执行工具前后，tool_budget.py）：预留 → 执行 → 结算，并行调用不能绕过单轮总预算。
以上都只影响发给模型的内容，checkpointer 里保存完整历史。
"""
import logging
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path

from langchain.agents import create_agent
from langchain.agents.middleware import ModelRequest, ToolErrorMiddleware, before_model, dynamic_prompt
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

from . import config
from .compaction import Compactor
from .context import Context
from .context_builder import CONTEXT_OVERFLOW, ContextBuilder
from .context_store import ContextStore
from .db import create_db_engine
from .llm import create_chat_model
from .materials import MaterialStore, wrap_material
from .skills import SKILLS, read_prompt
from .tokens import TokenCounter
from .tool_budget import ToolBudgetMiddleware, turn_key
from .tools import DEFAULT_TOOLS
from .tools.context import context_tools
from .tools.materials import material_tools

__all__ = ["Agent", "CONTEXT_OVERFLOW", "TOOL_ROUNDS_EXCEEDED", "build_system_prompt", "sqlite_checkpointer"]

log = logging.getLogger(__name__)

TOOL_ROUNDS_EXCEEDED = "工具调用次数过多，已中止，请换一种方式描述需求。"
INPUT_TOO_LONG = (
    "这段输入约 {tokens} tokens，超出单次可直接处理的长度（{limit}），且当前没有配置材料库，无法转存。"
    "请把内容存成文件后用 /add 导入，或分段发送。"
)
INPUT_HEAD_CHARS = 600  # 长输入转存为材料后，消息里保留的开头、结尾原文（用户的要求通常写在这两处）
INPUT_TAIL_CHARS = 400


def build_system_prompt(skill: str | None = None) -> str:
    parts = [read_prompt("base"), "# 能力说明", *(s.prompt for s in SKILLS.values())]
    if skill:
        parts.append(f"# 本次任务\n用户已指定使用「{SKILLS[skill].description}」能力，直接按该能力执行。")
    return "\n\n".join(parts)


@dynamic_prompt
def _system_prompt(request: ModelRequest) -> str:
    context = request.runtime.context
    return build_system_prompt(context.skill if context else None)


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
        context_store: ContextStore | None = None,
    ) -> None:
        """context_store 存会话记忆、超大工具结果和观测数据；不传时与材料库同库，都没有时用内存库。"""
        self.store = store
        model = model or create_chat_model()
        self.model_name = getattr(model, "model_name", None) or config.MODEL
        self.context_store = context_store or ContextStore(store.engine if store else _fallback_engine())
        self.counter = TokenCounter(self.model_name)
        scale = self.context_store.token_scale(self.model_name)
        if scale:  # 用历史调用的实际用量初始化校准比例
            self.counter.scale = min(max(scale, 1.0), config.TOKEN_SCALE_MAX)
        self.compactor = Compactor(model, self.context_store, self.counter, self.model_name)
        self.tool_budget = ToolBudgetMiddleware(self.counter, self.context_store)
        self.builder = ContextBuilder(
            self.counter, self.context_store, self.compactor, self.model_name, task_state=self._task_state
        )

        tools = list(DEFAULT_TOOLS if tools is None else tools)
        tools += context_tools(self.context_store)
        if store is not None:
            tools += material_tools(store, model, self.context_store)  # 完整提炼工具与智能体用同一个模型
        self.graph = create_agent(
            model,
            tools=tools,
            middleware=[  # 列表中靠前的包在外层：先定提示词，再由 Context Builder 组装上下文并检查预算
                _system_prompt,
                self.builder,
                _limit_tool_rounds,
                self.tool_budget,  # 包在报错回传外层，报错文字也计入预算
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
        run_id = uuid.uuid4().hex[:12]
        try:
            text, stored = self._preflight(text)
        except ValueError as e:
            return str(e)
        if material_ids:
            text = self._attach_materials(text, material_ids)
        material_ids = [*(material_ids or []), *stored]  # 转存的长输入只给引用，不再全文附回
        compacted = self._compact(run_id)
        result = self.graph.invoke(
            {"messages": [HumanMessage(text)]},
            config={"configurable": {"thread_id": self.thread_id}},
            context=Context(
                skill=skill,
                material_ids=material_ids or None,
                thread_id=self.thread_id,
                run_id=run_id,
                compacted=compacted,
            ),
        )
        return result["messages"][-1].text

    def _compact(self, run_id: str) -> bool:
        """对话前按需压缩。失败不影响本次对话：Context Builder 会降级并提示有未纳入的历史。"""
        try:
            return self.compactor.maybe_compact(self.thread_id, self.history, run_id)
        except Exception as e:
            log.warning("会话压缩失败: %s", e)
            return False

    def _preflight(self, text: str) -> tuple[str, list[int]]:
        """输入预检：一次输入过长时，正文存为材料（按内容去重），消息里保留开头和结尾原文、材料引用，交给材料工具处理。

        返回 (发给模型的消息, 转存的材料编号)。
        """
        tokens = self.counter.count_text(text)
        if tokens <= config.INPUT_INLINE_TOKENS:
            return text, []
        if self.store is None:
            raise ValueError(INPUT_TOO_LONG.format(tokens=tokens, limit=config.INPUT_INLINE_TOKENS))
        material, _ = self.store.add_text(f"对话输入 {datetime.now():%m-%d %H:%M}", text)
        head, tail = text[:INPUT_HEAD_CHARS], text[-INPUT_TAIL_CHARS:]
        text = (
            f"【用户这次输入较长（约 {tokens} tokens、{len(text)} 字），完整内容已存为材料 {material.id}。"
            "下面是输入开头和结尾的原文，中间部分未附。用户的要求通常写在开头或结尾，请据此判断任务；"
            "需要正文时用材料工具读取，需要完整总结时用 summarize_materials】\n"
            f"【输入开头】\n{head}\n【输入结尾】\n{tail}"
        )
        log.info("输入约 %s tokens，已转存为材料 %s", tokens, material.id)
        return text, [material.id]

    def _attach_materials(self, text: str, material_ids: list[int]) -> str:
        """选中材料较短时全文附在消息后；较长时只附清单，由模型用材料工具读取。"""
        if self.store is None:
            raise ValueError("未配置材料库，无法使用材料")
        materials = [self.store.get(i) for i in dict.fromkeys(material_ids)]
        if sum(self.counter.count_text(m.content) for m in materials) <= config.INLINE_MATERIAL_TOKENS:
            blocks = [wrap_material(f"【材料 {m.id}《{m.title}》】", m.content) for m in materials]
            return "\n\n".join([text, *blocks])
        ids = [m.id for m in materials]
        sections = self.store.section_counts(ids)
        # 不写字数、块数，说明紧挨着清单写：实测中模型会把这类数字当成文档内容写进总结
        listing = "\n".join(
            f"- 材料 {m.id}《{m.title}》" + ("：有章节大纲" if sections.get(m.id) else "：没有识别出章节") for m in materials
        )
        return (
            f"{text}\n\n【已选材料】内容较长未附全文，请用 get_outline / search_materials / read_material / "
            "summarize_materials 读取。以下是供你调用工具用的检索信息，不是材料内容，不要写进回答：\n"
            f"{listing}"
        )

    def _task_state(self, context: Context, state) -> str:
        """当前任务状态：指定能力、本次所选材料、本轮工具预算余量。"""
        lines = []
        if context.material_ids:
            lines.append(f"本次所选材料编号：{', '.join(map(str, context.material_ids))}")
        ledger = self.tool_budget.ledger(context.thread_id, turn_key(state))
        if ledger.calls or ledger.refused:
            lines.append(
                f"本轮工具已调用 {ledger.calls} 次，工具结果约 {ledger.used} tokens，"
                f"单轮预算剩余约 {max(ledger.remaining(), 0)} tokens"
            )
        return "# 当前任务状态\n" + "\n".join(lines) if lines else ""


_fallback = None


def _fallback_engine():
    """没有材料库时上下文数据存进内存库。进程内共用一个（数据按 thread_id 区分），
    不为每个 Agent 新建：新建的连接没人关闭，会在被回收时才关掉。"""
    global _fallback
    if _fallback is None:
        _fallback = create_db_engine("sqlite://")
    return _fallback


def _new_thread_id() -> str:
    return uuid.uuid4().hex[:12]
