"""单智能体：一份总提示词 + 五项能力说明；意图漏斗（intent.py）先做低成本判断，没把握时由模型自行判断任务。

基于 LangChain create_agent：工具调用循环由框架负责，会话记忆存在 LangGraph checkpointer 中，
以 thread_id 区分会话。本模块只补充项目自己的规则：动态提示词、上下文管理、工具轮数上限、工具报错回传。

上下文管理（详见 files/plans/plan-context.md）：
- 输入预检（chat）：用户一次输入过长时，正文存为材料，消息里只留开头结尾原文和引用。
- 会话压缩（chat 前，compaction.py）：未压缩的历史超过阈值时，把较早的对话整理进会话记忆。
- Context Builder（每次调用模型前，context_builder.py）：系统提示词 + 记忆 + 任务状态 + 最近几轮 + 当前轮，统一预算检查。
- 工具预算（每次执行工具前后，tool_budget.py）：预留 → 执行 → 结算，并行调用不能绕过单轮总预算。
以上都只影响发给模型的内容，checkpointer 里保存完整历史。
"""
import logging
import sqlite3
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from langchain.agents import create_agent
from langchain.agents.middleware import ModelRequest, ToolErrorMiddleware, before_model, dynamic_prompt
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

from . import config
from .compaction import Compactor
from .context import Context
from .context_builder import CONTEXT_OVERFLOW, ContextBuilder, system_reply
from .context_store import ContextStore
from .db import create_db_engine
from .llm import create_chat_model
from .materials import MaterialStore, wrap_material
from .output_check import INTERNAL_PATTERNS, PROGRAM_MATERIAL_REF, InternalTermCheck, reply_char_limit
from .skills import SKILLS, read_prompt
from .sources import conversation_material_ids
from .tokens import TokenCounter
from .tool_budget import ToolBudgetMiddleware, turn_key
from .feishu import configured as feishu_configured
from .intent import DIRECT_REPLIES, IntentDecision, IntentFunnel, TurnState
from .knowledge.assistant import KnowledgeAssistant, knowledge_tools
from .feishu.client import default_client
from .clock import RequestTime, request_time
from .tools import DEFAULT_TOOLS
from .tools.context import context_tools
from .tools.feishu import feishu_tools
from .tools.materials import material_tools
from .tools.business import business_tools

__all__ = [
    "Agent",
    "CONTEXT_OVERFLOW",
    "TOOL_ROUNDS_EXCEEDED",
    "build_system_prompt",
    "new_thread_id",
    "sqlite_checkpointer",
]

log = logging.getLogger(__name__)

TOOL_ROUNDS_EXCEEDED = "工具调用次数过多，已中止，请换一种方式描述需求。"
INPUT_TOO_LONG = (
    "这段输入约 {tokens} tokens，超出单次可直接处理的长度（{limit}），且当前没有配置材料库，无法转存。"
    "请把内容存成文件后用 /add 导入，或分段发送。"
)
INPUT_HEAD_CHARS = 600  # 长输入转存为材料后，消息里保留的开头、结尾原文（用户的要求通常写在这两处）
INPUT_TAIL_CHARS = 400


def build_system_prompt(skill: str | None = None, inferred: str | None = None, knowledge: str | None = None) -> str:
    """skill 为用户显式指定的能力；inferred 为意图漏斗推断的能力，只作提示，用户要求不同时以用户为准。"""
    parts = [read_prompt("base"), "# 能力说明", *(s.prompt for s in SKILLS.values())]
    if skill:
        parts.append(f"# 本次任务\n用户已指定使用「{SKILLS[skill].description}」能力，按该能力的规则和输出格式执行，不改用其他能力的格式；认为别的能力更合适时，只在末尾用一句话建议。")
    elif inferred:
        parts.append(f"# 本次任务\n意图识别判断本次请求是「{SKILLS[inferred].description}」，按该能力的规则和输出格式执行；如果用户的原话明显是别的要求，以用户原话为准。")
    if knowledge:
        parts.append(f"# 知识库\n用户选定了知识库《{knowledge}》。涉及制度、项目资料、产品手册等知识问题时，用 search_knowledge 检索原文后回答，"
                     "只依据检索到的原文并标注〔依据N〕；之前的聊天回答只用于理解问题，不作为事实依据。")
    return "\n\n".join(parts)


@dynamic_prompt
def _system_prompt(request: ModelRequest) -> str:
    context = request.runtime.context
    intent = (context.intent or {}) if context else {}
    inferred = intent.get("intent") if intent.get("intent") in SKILLS else None
    prompt = build_system_prompt(context.skill if context else None, inferred, intent.get("kb_name"))
    if context and context.now:  # 只在每次调用的系统提示里，不进会话历史
        prompt += "\n\n" + RequestTime(context.now).prompt()
    return prompt


@before_model(can_jump_to=["end"])
def _limit_tool_rounds(state, runtime):
    """本轮提问中模型已调用 MAX_TOOL_ROUNDS 次仍未给出答复时中止。"""
    messages = state["messages"]
    last_human = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))
    rounds = sum(isinstance(m, AIMessage) for m in messages[last_human:])
    if rounds >= config.MAX_TOOL_ROUNDS:
        return {"jump_to": "end", "messages": [system_reply(TOOL_ROUNDS_EXCEEDED)]}
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
        feishu_factory: Callable | None = None,
        funnel: IntentFunnel | None = None,
        knowledge: KnowledgeAssistant | None = None,
    ) -> None:
        """context_store 存会话记忆、超大工具结果和观测数据；不传时与材料库同库，都没有时用内存库。

        配置了飞书（或传入 feishu_factory，测试用）且有材料库时，加上读取飞书链接的工具。
        """
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

        self.funnel = funnel or IntentFunnel()  # 默认只有规则层；网页后端注入本地 Embedding 后启用语义层
        self.knowledge = knowledge
        tools = list(DEFAULT_TOOLS if tools is None else tools)
        tools += context_tools(self.context_store)
        if store is not None:
            tools += business_tools(store)
            tools += material_tools(store, model, self.context_store)  # 完整提炼工具与智能体用同一个模型
            if feishu_factory is not None or feishu_configured():
                tools += feishu_tools(store, feishu_factory or default_client)
        if knowledge is not None:
            tools += knowledge_tools(knowledge)
        self._model, self._tools, self._checkpointer = model, tools, checkpointer or InMemorySaver()
        self.graph = self._build_graph()
        self.thread_id = thread_id or new_thread_id()

    def _build_graph(self):
        return create_agent(
            self._model,
            tools=self._tools,
            middleware=[  # 列表中靠前的包在外层：先定提示词，再由 Context Builder 组装上下文并检查预算
                _system_prompt,
                InternalTermCheck(self._check_sources, self._reply_limit),  # 在 Context Builder 外层：重试也经过预算检查
                self.builder,
                self._quality_middleware(),
                _limit_tool_rounds,
                self.tool_budget,  # 包在报错回传外层，报错文字也计入预算
                ToolErrorMiddleware(on_error=_tool_error),
            ],
            context_schema=Context,
            checkpointer=self._checkpointer,
        )

    def enable_knowledge(self, knowledge: KnowledgeAssistant, funnel: IntentFunnel | None = None) -> None:
        """网页后端在知识库服务就绪后接入知识能力和语义层；会话历史在 checkpointer 中，不受影响。"""
        if self.knowledge is None:
            self._tools = [*self._tools, *knowledge_tools(knowledge)]
        self.knowledge = knowledge
        self.funnel = funnel or self.funnel
        self.graph = self._build_graph()

    def _quality_middleware(self):
        @before_model(can_jump_to=["end"])
        def source_quality_guard(state, runtime):
            if self.store is None:
                return None
            notice = self._quality_notice(state["messages"], [])
            if notice:
                reply = system_reply(notice)
                reply.response_metadata["quality_notice"] = notice
                return {"jump_to": "end", "messages": [reply]}
            return None
        return source_quality_guard

    def _quality_notice(self, messages, material_ids):
        from .knowledge.evidence import material_quality_notice
        snapshots = []
        for message in messages:
            notice = message.additional_kwargs.get("quality_notice") or message.response_metadata.get("quality_notice")
            if isinstance(message, ToolMessage) and isinstance(message.artifact, dict):
                notice = notice or message.artifact.get("quality_notice")
            if notice:
                snapshots.append(notice)
        current = material_quality_notice(self.store, [*material_ids, *conversation_material_ids(messages)])
        return current or (snapshots[0] if snapshots else None)

    def reset(self) -> None:
        """开始新会话；旧会话仍保存在 checkpointer 中。"""
        self.thread_id = new_thread_id()

    @property
    def history(self) -> list[AnyMessage]:
        return self.history_of(self.thread_id)

    def history_of(self, thread_id: str) -> list[AnyMessage]:
        state = self.graph.get_state({"configurable": {"thread_id": thread_id}})
        return state.values.get("messages", [])

    def chat(
        self,
        text: str,
        skill: str | None = None,
        material_ids: list[int] | None = None,
        thread_id: str | None = None,
        kb_id: str | None = None,
    ) -> str:
        """thread_id 不传时用当前会话（CLI）；网页后端多个会话共用一个 Agent，每次调用传入会话编号。

        用户的原始输入记在消息的 additional_kwargs["display"] 里，供网页显示历史（发给模型的是附了材料的全文）。
        本轮意图记在 additional_kwargs["intent"]；kb_id 为用户选定的知识库。
        """
        thread_id = thread_id or self.thread_id
        display = text
        if skill and skill not in SKILLS:
            raise ValueError(f"未知技能: {skill}，可选: {', '.join(SKILLS)}")
        run_id = uuid.uuid4().hex[:12]
        started = request_time()  # 本轮内所有模型调用、工具和检查共用这一时间
        # 在附加全文和压缩历史之前挡住受限来源，避免压缩模型成为旁路。
        if self.store is not None:
            notice = self._quality_notice(self.history_of(thread_id), material_ids or [])
            if notice:
                message = HumanMessage(text, id=uuid.uuid4().hex, additional_kwargs=dict(
                    display=display, material_ids=material_ids or [], skill=skill, quality_notice=notice))
                self.graph.update_state({"configurable": {"thread_id": thread_id}},
                    {"messages": [message, system_reply(notice)]}, as_node="model")
                return notice
        history = self.history_of(thread_id)
        decision = self.funnel.classify(display, TurnState(
            has_materials=bool(material_ids), kb_id=kb_id, last_intent=_last_intent(history),
            has_previous_reply=any(isinstance(m, AIMessage) and not m.tool_calls and m.text for m in history)), skill)
        intent = decision.to_dict()
        if kb_id and self.knowledge is not None:
            intent["kb_id"], intent["kb_name"] = kb_id, self.knowledge.base_name(kb_id)
        direct = self._direct(decision, display, kb_id)
        if direct is not None:
            message = HumanMessage(display, id=uuid.uuid4().hex, additional_kwargs=dict(
                display=display, material_ids=material_ids or [], skill=skill, intent=intent, kb_id=kb_id))
            self.graph.update_state({"configurable": {"thread_id": thread_id}}, {"messages": [message, direct]}, as_node="model")
            return direct.text
        try:
            text, stored = self._preflight(text)
        except ValueError as e:
            return str(e)
        if material_ids:
            text = self._attach_materials(text, material_ids)
        scope = [*material_ids, *stored] if material_ids else None
        material_ids = [*(material_ids or []), *stored]  # 转存的长输入只给引用，不再全文附回
        compacted = self._compact(thread_id, run_id)
        meta = {"display": display, "material_ids": material_ids, "skill": skill, "intent": intent, "kb_id": kb_id}
        message = HumanMessage(text, id=uuid.uuid4().hex, additional_kwargs=meta)
        try:
            result = self.graph.invoke(
                {"messages": [message]},
                config={"configurable": {"thread_id": thread_id}},
                context=Context(
                    skill=skill,
                    material_ids=scope,
                    thread_id=thread_id,
                    run_id=run_id,
                    compacted=compacted,
                    reply_limit=reply_char_limit([*self.history_of(thread_id), message]),
                    now=started.now,
                    kb_id=kb_id,
                    intent=intent,
                ),
            )
        except Exception:
            try:
                self._close_failed_turn(thread_id, message.id)
            except Exception:
                log.exception("保存本轮调用失败说明失败")
            raise
        return result["messages"][-1].text

    def _direct(self, decision: IntentDecision, text: str, kb_id: str | None) -> AIMessage | None:
        """处理器登记表：能直接回复的意图不进入智能体循环；返回 None 时交给智能体。"""
        if decision.intent in DIRECT_REPLIES:
            return system_reply(DIRECT_REPLIES[decision.intent])
        if decision.intent not in {"knowledge", "locate"} or self.knowledge is None:
            return None
        if not kb_id:
            return system_reply("这是知识库查询。请先在输入框上方选择要查询的知识库，再提问。")
        if decision.intent == "locate":  # 只在本地检索，不调用模型、不外发
            result = self.knowledge.locate(kb_id, text)
        else:
            result = self.knowledge.answer(kb_id, text)
            if result["status"] in {"workflow_required", "clarification_required", "blocked"}:
                return None  # 知识问答入口不处理的请求，交给智能体结合上下文判断
        references = [dict(label=f"依据{i}", reference=item["reference"], title=item["source"]["title"])
                      for i, item in enumerate(result.get("evidence") or [], 1)]
        reply = AIMessage(result["answer"], id=uuid.uuid4().hex) if result.get("model_called") else system_reply(result["answer"])
        reply.response_metadata["knowledge"] = dict(kb_id=kb_id, status=result["status"], references=references,
                                                    warnings=result.get("warnings") or [],
                                                    egress=result.get("egress") if result.get("model_called") else None)  # 只记录实际发生的外发
        return reply

    def _close_failed_turn(self, thread_id, message_id):
        """闭合已入checkpoint的失败轮次，不删除用户原话，不重发或恢复旧工具任务。"""
        messages = self.history_of(thread_id)
        start = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage) and not m.additional_kwargs.get("internal")), default=-1)
        if start < 0 or messages[start].id != message_id:
            return
        tail = messages[start + 1:]
        if any(isinstance(m, AIMessage) and not m.tool_calls and m.text for m in tail):
            return
        pending = {}
        for m in tail:
            if isinstance(m, AIMessage):
                pending.update({c["id"]: c["name"] for c in m.tool_calls})
            elif isinstance(m, ToolMessage):
                pending.pop(m.tool_call_id, None)
        closed = [ToolMessage("本轮调用失败，工具结果不可用，请重新提问后查询。", tool_call_id=cid, name=name, status="error") for cid, name in pending.items()]
        closed.append(system_reply("本轮调用失败，未生成回答。请修改要求或稍后重试。"))
        self.graph.update_state({"configurable": {"thread_id": thread_id}}, {"messages": closed}, as_node="model")

    def _compact(self, thread_id: str, run_id: str) -> bool:
        """对话前按需压缩。失败不影响本次对话：Context Builder 会降级并提示有未纳入的历史。"""
        try:
            return self.compactor.maybe_compact(thread_id, self.history_of(thread_id), run_id)
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

    def _check_sources(self, request) -> list[str]:
        """输出检查的依据：用户原话、会话涉及材料及本轮工具结果（原文里本来就有的“第 3 块”之类不算泄漏）。"""
        texts = []
        for m in request.messages:
            if isinstance(m, HumanMessage) and not m.additional_kwargs.get("internal"):
                # 消息正文里程序附加的材料编号去掉，只留用户自己输入的原话（display）原样
                texts += [PROGRAM_MATERIAL_REF.sub("", m.text), m.additional_kwargs.get("display") or ""]
        if self.store is not None:
            for mid in conversation_material_ids(request.messages):
                try:
                    texts.append(self.store.get(mid).content)
                except ValueError:  # 已删除的材料不扩大为全库
                    continue
        start = max((i for i, m in enumerate(request.messages) if isinstance(m, HumanMessage) and not m.additional_kwargs.get("internal")), default=-1)
        for m in request.messages[start + 1:]:
            if isinstance(m, ToolMessage):
                body = m.text
                for pattern in INTERNAL_PATTERNS:
                    body = pattern.sub("", body)  # 工具附加的处理编号不能豁免泄漏；原文在上面单独收集
                texts.append(body)
        return texts

    def _reply_limit(self, request) -> int | None:
        # chat 已从完整原始历史计算，早期对话压缩后也保留明确上限。
        context = request.runtime.context
        return context.reply_limit if context else reply_char_limit(request.messages)

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


def _last_intent(messages: list[AnyMessage]) -> str | None:
    for m in reversed(messages):
        if isinstance(m, HumanMessage) and not m.additional_kwargs.get("internal"):
            return (m.additional_kwargs.get("intent") or {}).get("intent")
    return None


def new_thread_id() -> str:
    return uuid.uuid4().hex[:12]
