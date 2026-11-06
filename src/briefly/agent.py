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
import json
import logging
import re
import sqlite3
import uuid
from collections.abc import Callable
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path

from langchain.agents import create_agent
from langgraph.prebuilt.tool_node import ToolInvocationError
from langchain.agents.middleware import ModelRequest, ToolErrorMiddleware, after_agent, before_model, dynamic_prompt
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite import SqliteSaver

from .errors import PayloadTooLarge, UserInputError, is_user_facing
from . import config, turn_timing
from .compaction import Compactor
from .context import Context
from .context_builder import CONTEXT_OVERFLOW, SYSTEM_REPLY, ContextBuilder, system_reply
from .context_store import TURN, ContextStore
from .db import create_db_engine, database_url, is_postgres
from .llm import create_chat_model, fresh_model_calls
from .materials import EgressRestricted, MaterialStore, check_pasted_size, wrap_material
from .output_check import INTERNAL_PATTERNS, PROGRAM_MATERIAL_REF, InternalTermCheck, reply_char_limit
from .skills import SKILLS, read_prompt
from .sources import conversation_material_ids
from .tokens import TokenCounter
from .tool_budget import ToolBudgetMiddleware, turn_key
from .turn_record import turn_record
from .feishu import configured as feishu_configured
from .intent import DIRECT_REPLIES, IntentDecision, IntentFunnel, TurnState
from .intent_log import IntentLog
from .knowledge.assistant import KnowledgeAssistant, KnowledgeSearchGuard, knowledge_tools, record_knowledge_turn
from .knowledge.service import KnowledgeNotFound
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
    "open_checkpointer",
    "sqlite_checkpointer",
]

log = logging.getLogger(__name__)

_ASKS_REDO = re.compile(r"重新|重做|重写|重来|再(?:来|做|写|生成|总结|提炼|整理|概括|梳理|归纳|分析)|换(?:一|个)(?:版|种|下)|\b(?:redo|regenerate|again)\b", re.I)
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
                     "只依据检索到的原文并标注〔依据N〕；之前的聊天回答只用于理解问题，不作为事实依据。"
                     "回答知识问题时直接回答所问，先给结论，简单事实一两句答完；原文措辞对结论有决定作用或用户要求原文时才引用原句，且只引必要的一句。"
                     "不评论与问题无关的依据，不补充问题没问的内容（同一条款的其他项、相邻条款、文件本身的修订要求等），不用原文数字自行推算新的数字；"
                     "只有会改变结论的限制才写。依据不足以回答所问时明确写未提及或依据不足。")
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


@after_agent
def _record_turn(state, runtime):
    """轮次收尾：按本轮实际调用的工具和产出的结构写轮次结果记录（turn_record.py），提前结束的轮次也经过这里。"""
    messages = state["messages"]
    start = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage) and not m.additional_kwargs.get("internal")), default=-1)
    reply = messages[-1]
    if start < 0 or not isinstance(reply, AIMessage) or reply.tool_calls or "intent" not in messages[start].additional_kwargs:
        return None
    human, context = messages[start], runtime.context
    tools = [c["name"] for m in messages[start + 1:] if isinstance(m, AIMessage) for c in m.tool_calls]
    intent = human.additional_kwargs["intent"]
    resolved, result = turn_record(reply.text, intent=intent, skill=context.skill if context else None, tools=tools,
                                   user_text=human.additional_kwargs.get("display") or "", message_id=reply.id,
                                   system=bool(reply.response_metadata.get(SYSTEM_REPLY)))
    human = human.model_copy(update={"additional_kwargs": {**human.additional_kwargs, "intent": {**intent, "resolved": resolved}}})
    reply = reply.model_copy(update={"response_metadata": {**reply.response_metadata, "result": result}})
    return {"messages": [human, reply]}  # 按消息 ID 替换，不新增消息


# 程序直接写入的轮次（直接回复、受限来源提示、失败收尾）以轮次收尾节点的身份写入，会话停在“已结束”
TURN_END = f"{_record_turn.name}.after_agent"
FAILED_TURN = "本轮调用失败，未生成回答。请修改要求或稍后重试。"


def _tool_error(error: Exception, request) -> str:
    # 工具失败不抛给用户，作为工具结果回传模型，由模型如实说明。给调用方看的说明原样回传，
    # 其他异常（程序 bug、第三方库）的文字可能带路径和内部信息，模型又在外部服务，只进日志
    if is_user_facing(error) or isinstance(error, ToolInvocationError):  # 后者是模型自己传错参数，要看到原因才能改正
        return f"工具执行失败: {error}"
    log.error("工具执行失败", exc_info=error)
    return "工具执行失败: 内部错误，这次操作没有完成。请如实告诉用户，不要编造结果。"


def sqlite_checkpointer(path: str | Path) -> SqliteSaver:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    return SqliteSaver(sqlite3.connect(path, check_same_thread=False))


_CHECKPOINT_SETUP_LOCK = 0x42524643
_CHECKPOINT_SETUP_WAIT_S = 120


def _setup_checkpoints(saver, conninfo: str) -> None:
    """setup() 幂等，但不能并发：多个进程第一次启动时都会建表，撞唯一约束。这里让它们排队。

    只能轮询 pg_try_advisory_lock，不能用阻塞的 pg_advisory_lock：等锁的语句持有快照，而 setup() 里的
    CREATE INDEX CONCURRENTLY 要等所有旧快照结束，两边会互相等死，数据库也检测不出来。持锁的连接不开事务，同样不挡它。"""
    import time

    import psycopg

    deadline = time.monotonic() + _CHECKPOINT_SETUP_WAIT_S
    with psycopg.connect(conninfo, autocommit=True) as lock:
        while not lock.execute("SELECT pg_try_advisory_lock(%s)", (_CHECKPOINT_SETUP_LOCK,)).fetchone()[0]:
            if time.monotonic() > deadline:
                raise TimeoutError("等待其他进程建会话记录表超时，请稍后重试")
            time.sleep(0.1)
        saver.setup()  # 连接关闭时锁自动释放


def postgres_checkpointer(url: str) -> BaseCheckpointSaver:
    """连接池要求和 PostgresSaver.from_conn_string 一致：autocommit、dict_row、不用预备语句。"""
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
    from sqlalchemy.engine import make_url

    conninfo = make_url(database_url(url)).set(drivername="postgresql").render_as_string(hide_password=False)
    pool = ConnectionPool(conninfo, min_size=1, max_size=10, open=True,
                          kwargs=dict(autocommit=True, prepare_threshold=0, row_factory=dict_row))
    saver = PostgresSaver(pool)
    try:
        _setup_checkpoints(saver, conninfo)
    except BaseException:
        pool.close()
        raise
    return saver


def open_checkpointer(url: str | None = None, path: str | Path | None = None) -> BaseCheckpointSaver:
    """会话记录跟着业务库走：PostgreSQL 时放在同一个数据库，否则用独立的 SQLite 文件（DB_PATH）。"""
    url = url or config.DATABASE_URL
    return postgres_checkpointer(url) if is_postgres(database_url(url)) else sqlite_checkpointer(path or config.DB_PATH)


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
        self.intent_log = IntentLog(store.engine) if store is not None else None  # 意图判断与纠正记录，只存本机
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
                KnowledgeSearchGuard(),  # 在回答检查内层：未检索的草稿换成补检索，检查只看拿到依据后的回答
                self.builder,
                self._quality_middleware(),
                _limit_tool_rounds,
                self.tool_budget,  # 包在报错回传外层，报错文字也计入预算
                ToolErrorMiddleware(on_error=_tool_error),
                _record_turn,
                record_knowledge_turn,  # 助理路径知识轮次的引用与外发计数（WP2）
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
        skip_funnel: bool = False,
        corrects: str | None = None,
    ) -> str:
        """thread_id 不传时用当前会话（CLI）；网页后端多个会话共用一个 Agent，每次调用传入会话编号。

        用户的原始输入记在消息的 additional_kwargs["display"] 里，供网页显示历史（发给模型的是附了材料的全文）。
        本轮意图记在 additional_kwargs["intent"]（漏斗判断；轮次收尾补上实际能力 resolved，回复记 result，见 turn_record.py）；
        kb_id 为用户选定的知识库。
        skip_funnel：用户认为系统理解错了（网页上的“交给助理处理”），同一句话不经意图漏斗直接交给助理；
        corrects 为被纠正的那条用户消息 ID，记入意图日志（intent_log.py）。
        """
        thread_id = thread_id or self.thread_id
        # “交给助理处理”和“重新……”都是用户在要新的结果；判断宽一点也只是多调用一次模型
        refresh = fresh_model_calls() if skip_funnel or _ASKS_REDO.search(text) else nullcontext()
        with turn_timing.measure() as turn, refresh:
            try:
                return self._chat(text, skill, material_ids, thread_id, kb_id, skip_funnel, corrects)
            finally:
                if turn is not None:
                    self._record_timing(thread_id, turn)

    def _record_timing(self, thread_id: str, turn: turn_timing.TurnTiming) -> None:
        """一轮的耗时拆分记一行观测（task_type=turn，明细 JSON 放 note，旧库不用加列）。只有数字，没有内容。"""
        summary = turn.summary()
        log.info("turn_timing %s", summary)
        self.context_store.record_usage(thread_id=thread_id, run_id=turn.run_id, model=self.model_name, task_type=TURN,
                                        input_tokens=turn.input_tokens, output_tokens=turn.output_tokens,
                                        latency_ms=summary["total_ms"], note=json.dumps(summary))

    def _chat(self, text, skill, material_ids, thread_id, kb_id, skip_funnel, corrects) -> str:
        display = text
        if skill and skill not in SKILLS:
            raise UserInputError(f"未知技能: {skill}，可选: {', '.join(SKILLS)}")
        run_id = uuid.uuid4().hex[:12]
        if (turn := turn_timing.current()) is not None and not turn.run_id:
            turn.run_id = run_id
        started = request_time()  # 本轮内所有模型调用、工具和检查共用这一时间
        # 在附加全文和压缩历史之前挡住受限来源，避免压缩模型成为旁路。
        if self.store is not None:
            notice = self._quality_notice(self.history_of(thread_id), material_ids or [])
            if notice:
                message = HumanMessage(text, id=uuid.uuid4().hex, additional_kwargs=dict(
                    display=display, material_ids=material_ids or [], skill=skill, quality_notice=notice))
                self.graph.update_state({"configurable": {"thread_id": thread_id}},
                    {"messages": [message, system_reply(notice)]}, as_node=TURN_END)
                return notice
        # 所选知识库已删除（页面还没刷新、或恢复的旧选择）：本轮按没选库处理，意图里记一笔，页面据此说明没有查询
        kb_name, kb_removed = None, False
        if kb_id and self.knowledge is not None:
            try:
                kb_name = self.knowledge.base_name(kb_id)
            except KnowledgeNotFound:
                kb_id, kb_removed = None, True
        history = self.history_of(thread_id)
        state = turn_state(history, material_ids, kb_id)
        if skip_funnel:
            decision = IntentDecision("agent", 3, "用户认为判断有误，跳过意图判断交给助理")
        else:
            with turn_timing.span("intent"):
                decision = self.funnel.classify(display, state, skill)
        intent = decision.to_dict()
        if skip_funnel:
            intent["skip_funnel"], intent["corrects"] = True, corrects
        if kb_id and self.knowledge is not None:
            intent["kb_id"], intent["kb_name"] = kb_id, kb_name
        if kb_removed:
            intent["kb_removed"] = True
        try:
            direct = self._direct(decision, display, kb_id)
        except UserInputError:
            raise
        except Exception:  # 单问捷径的模型调用失败或超时：这时还没写入历史，不记下来用户重新打开就是空会话
            message = HumanMessage(display, id=uuid.uuid4().hex, additional_kwargs=dict(
                display=display, material_ids=material_ids or [], skill=skill, intent=intent, kb_id=kb_id))
            try:
                self.graph.update_state({"configurable": {"thread_id": thread_id}},
                                        {"messages": [message, system_reply(FAILED_TURN)]}, as_node=TURN_END)
                self._log_intent(thread_id, message, state, skill, corrects)
            except Exception:
                log.exception("保存本轮调用失败说明失败")
            raise
        if direct is not None:
            # 捷径和直接回复由处理器执行，实际能力就是漏斗的判断
            _, direct.response_metadata["result"] = turn_record(
                direct.text, intent=intent, skill=skill, tools=["search_knowledge"] if "knowledge" in direct.response_metadata else [],
                message_id=direct.id, system=bool(direct.response_metadata.get(SYSTEM_REPLY)) and not direct.response_metadata.get("intent_reply"))
            intent["resolved"] = decision.intent
            intent["direct"] = True  # 网页据此显示系统的理解和纠正入口；交给助理的轮次不显示
            message = HumanMessage(display, id=uuid.uuid4().hex, additional_kwargs=dict(
                display=display, material_ids=material_ids or [], skill=skill, intent=intent, kb_id=kb_id))
            self.graph.update_state({"configurable": {"thread_id": thread_id}}, {"messages": [message, direct]}, as_node=TURN_END)
            self._log_intent(thread_id, message, state, skill, corrects)
            return direct.text
        try:
            text, stored = self._preflight(text)
        except PayloadTooLarge:
            raise  # 超限要让接口返回 413，页面把输入放回去；不能当成一条普通回复
        except UserInputError as e:
            return str(e)
        if material_ids:
            try:
                text = self._attach_materials(text, material_ids)
            except EgressRestricted as e:  # 关联的知识库未允许外发：不调用模型，直接说明
                return str(e)
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
        finally:
            self._log_intent(thread_id, message, state, skill, corrects)
        return result["messages"][-1].text

    def _log_intent(self, thread_id, message, state, skill, corrects):
        """本轮已存入会话后记意图日志（含轮次收尾写回的 resolved）；记录失败不影响对话。"""
        if self.intent_log is None:
            return
        try:
            saved = next((m for m in reversed(self.history_of(thread_id)) if m.id == message.id), message)
            self.intent_log.record(thread_id, message.id, message.additional_kwargs["display"],
                                   saved.additional_kwargs["intent"], state, skill, corrects)
        except Exception:
            log.exception("写入意图日志失败")

    def _direct(self, decision: IntentDecision, text: str, kb_id: str | None) -> AIMessage | None:
        """处理器登记表：能直接回复的意图不进入智能体循环；返回 None 时交给智能体。"""
        if decision.intent in DIRECT_REPLIES:
            return _intent_reply(decision.reply or DIRECT_REPLIES[decision.intent])
        # 知识捷径只由知识库路由给出（已选库且库里有相关原文）；没选库的知识问题由助理提示选择
        if decision.intent not in {"knowledge", "locate"} or self.knowledge is None or not kb_id:
            return None
        if decision.intent == "locate":  # 只在本地检索，不调用模型、不外发；列出的文档都可核对
            result = self.knowledge.locate(kb_id, text)
            references = [dict(label=f"依据{i}", reference=item["reference"], title=item["source"]["title"], section=item["section"])
                          for i, item in enumerate(result.get("evidence") or [], 1)]
            reply = _intent_reply(result["answer"])
        else:
            result = self.knowledge.answer(kb_id, text)
            if result["status"] in {"workflow_required", "clarification_required", "blocked"}:
                return None  # 知识问答入口不处理的请求，交给智能体结合上下文判断
            # 引用由依据包确定：模型回答只列实际引用的依据，编号与回答一致；低相关时列最接近的出处
            references = result.get("references") or []
            if result.get("model_called") or result.get("from_cache"):
                reply = AIMessage(result["answer"], id=uuid.uuid4().hex)
            elif result["status"] == "low_relevance":  # 正常结论（库里没有相关内容），不是失败提示
                reply = _intent_reply(result["answer"])
            else:
                reply = system_reply(result["answer"])
        reply.response_metadata["knowledge"] = dict(kb_id=kb_id, status=result["status"], references=references,
                                                    warnings=result.get("warnings") or [],
                                                    egress=result.get("egress") if result.get("model_called") or result.get("from_cache") else None)  # 只记录实际发生的外发；命中缓存时计数为 0 并标明
        if result.get("from_cache"):
            reply.response_metadata["knowledge"]["from_cache"] = True
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
        closed.append(system_reply(FAILED_TURN))
        self.graph.update_state({"configurable": {"thread_id": thread_id}}, {"messages": closed}, as_node=TURN_END)

    def _compact(self, thread_id: str, run_id: str) -> bool:
        """对话前按需压缩。失败不影响本次对话：Context Builder 会降级并提示有未纳入的历史。"""
        try:
            return self.compactor.maybe_compact(thread_id, self.history_of(thread_id), run_id)
        except Exception as e:
            log.warning("会话压缩失败: %s", type(e).__name__)
            return False

    def _preflight(self, text: str) -> tuple[str, list[int]]:
        """输入预检：一次输入过长时，正文存为材料（按内容去重），消息里保留开头和结尾原文、材料引用，交给材料工具处理。

        返回 (发给模型的消息, 转存的材料编号)。
        """
        tokens = self.counter.count_text(text)
        if tokens <= config.INPUT_INLINE_TOKENS:
            return text, []
        if self.store is None:
            raise UserInputError(INPUT_TOO_LONG.format(tokens=tokens, limit=config.INPUT_INLINE_TOKENS))
        check_pasted_size(text)
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
        self.store.require_egress(material_ids)
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


def _intent_reply(text: str) -> AIMessage:
    """意图漏斗直接给出的正常答复（问候、拦截说明、文档定位等）：不是模型输出，也不是失败提示。"""
    reply = system_reply(text)
    reply.response_metadata["intent_reply"] = True
    return reply


def turn_state(history: list[AnyMessage], material_ids: list[int] | None, kb_id: str | None) -> TurnState:
    """从完整会话历史（checkpointer 不因压缩删除消息）推出意图判断用的状态。"""
    return TurnState(has_materials=bool(material_ids), kb_id=kb_id, last_intent=_last_intent(history),
                     has_previous_reply=any(_revisable(m) for m in history),
                     earlier_materials=bool(conversation_material_ids(history)))


def _revisable(m: AnyMessage) -> bool:
    """可供“调整上一轮”的回答：模型回答和文档定位结果；问候、拒绝、提示类固定回复不算。"""
    if not isinstance(m, AIMessage) or m.tool_calls or not m.text:
        return False
    return not m.response_metadata.get(SYSTEM_REPLY) or bool(m.response_metadata.get("knowledge"))


def _last_intent(messages: list[AnyMessage]) -> str | None:
    """上一轮实际能力（intent.resolved）；2026-10-30 之前的消息没有这个字段，按未知处理，不回退到漏斗当时的判断。"""
    for m in reversed(messages):
        if isinstance(m, HumanMessage) and not m.additional_kwargs.get("internal"):
            return (m.additional_kwargs.get("intent") or {}).get("resolved")
    return None


def new_thread_id() -> str:
    return uuid.uuid4().hex[:12]
