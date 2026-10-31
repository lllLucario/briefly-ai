"""知识库作为助理的一项能力：单问捷径、文档定位（本地）与供智能体多轮使用的检索工具。

会话历史只用来理解问题；事实只来自本轮检索到的知识库原文。外发前检查知识库一次性授权。
单问捷径和 search_knowledge 都用 KnowledgeQA.bundle 产出本轮依据包；助理路径的引用与外发计数
由轮次收尾的 record_knowledge_turn 按本轮实际检索和实际发给模型的内容写进回复元数据。
"""
import logging
import uuid

from langchain.agents.middleware import AgentMiddleware, ModelResponse, after_agent
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from ..context import Context
from ..context_builder import SYSTEM_REPLY
from ..intent import _FOLLOW_UP, knowledge_question
from .qa import EGRESS_POLICY, EGRESS_REQUIRED, MAX_SUB_QUESTIONS, KnowledgeQA, cited_numbers
from .retrieval import both_routes, relevance_floor, relevant, similarity

__all__ = ["KnowledgeAssistant", "KnowledgeSearchGuard", "MAX_SUB_QUESTIONS", "knowledge_tools", "record_knowledge_turn"]

log = logging.getLogger(__name__)


class KnowledgeAssistant:
    def __init__(self, retrieval, model_factory) -> None:
        self.retrieval = retrieval
        self.qa = KnowledgeQA(retrieval, model_factory)

    @property
    def knowledge(self):
        return self.retrieval.imports.knowledge

    def base_name(self, kb_id: str) -> str:
        with self.knowledge._session() as s:
            return self.knowledge._kb(s, kb_id).name

    def relevance(self, kb_id: str, question: str) -> float | None:
        """意图漏斗用：选定知识库中最相关片段的语义相似度。只在本地检索，不外发。"""
        sims = [s for item in self.retrieval.search(kb_id, question)["evidence"] if (s := similarity(item)) is not None]
        return max(sims) if sims else None

    def answer(self, kb_id: str, question: str) -> dict:
        """B1 单点查询捷径：沿用知识问答的质量、授权与引用检查。"""
        return self.qa.answer(kb_id, question)

    def locate(self, kb_id: str, question: str) -> dict:
        """B2 文档定位：只在本地检索并列出文档与章节，不调用模型、不外发。低于相关度下限的条目不列出。"""
        result = self.retrieval.search(kb_id, question)
        evidence = result["evidence"]
        floor = relevance_floor(self.retrieval.imports.encoder_factory().identity) if evidence else None
        if floor is not None:
            # 整体不相关时全部不列；否则去掉低于下限、又没有被两路同时命中的条目
            evidence = [] if not relevant(evidence, floor) else [
                e for e in evidence if (s := similarity(e)) is None or s >= floor or both_routes(e)]
        if not evidence:
            status = "low_relevance" if result["evidence"] else result["status"]
            return dict(status=status, answer="当前知识库没有找到相关文档。", evidence=[])
        documents: dict[str, list[str]] = {}
        for item in evidence:
            sections = documents.setdefault(item["source"]["title"], [])
            # 章节路径首段通常是文档标题，只显示最后一级，便于阅读
            if (section := (item["section"] or "章节未确定").split(" > ")[-1]) not in sections:
                sections.append(section)
        lines = [f"- 《{title}》：{'、'.join(sections)}" for title, sections in documents.items()]
        return dict(status="located", evidence=evidence,
                    answer="以下文档与问题相关（按相关度排列，仅定位位置，请打开原文核对）：\n" + "\n".join(lines))

    def search(self, kb_id: str, questions: list[str], start: int = 1) -> dict:
        """供智能体使用：本轮依据包，允许只依据已就绪部分（附提示）。"""
        return self.qa.bundle(kb_id, questions, allow_partial=True, start=start)


def _turn_start(messages) -> int:
    return max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage) and not m.additional_kwargs.get("internal")), default=-1)


def _searches(messages) -> list[dict]:
    return [m.artifact["knowledge"] for m in messages if isinstance(m, ToolMessage) and m.name == "search_knowledge"
            and isinstance(m.artifact, dict) and m.artifact.get("knowledge")]


def knowledge_tools(assistant: KnowledgeAssistant) -> list:
    @tool(response_format="content_and_artifact")
    def search_knowledge(questions: list[str], runtime: ToolRuntime[Context]) -> tuple[str, dict]:
        """在用户本次选定的知识库里检索制度、项目资料、产品手册等原文。
        questions：1～3 个完整、独立的检索问题。用户追问省略了主语时，结合上文补全后再检索（例如上一轮问年假，
        这一轮“那病假呢？”应写成“病假有几天、如何申请”）；一句话里有多个问题时拆开一次传入。
        回答只能依据本工具返回的原文，关键结论用〔依据N〕标注出处；之前的聊天回答不是依据。
        直接回答所问，不评论与问题无关的依据，不用原文数字自行推算新的数字。
        返回内容不足以回答时如实说明未提及或依据不足。之前轮次的检索结果不保留原文，需要时重新检索。"""
        kb_id = runtime.context.kb_id if runtime.context else None
        if not kb_id:
            return "用户没有选择知识库，请提示用户先在输入框上方选择要查询的知识库。", {}
        messages = (runtime.state or {}).get("messages", [])
        # 同一轮多次检索时编号接续，回答里的〔依据N〕在本轮内唯一
        start = 1 + sum(len(s["references"]) for s in _searches(messages[_turn_start(messages) + 1:]) if s.get("status") == "ready")
        result = assistant.search(kb_id, questions, start=start)
        ready = result["status"] == "ready"
        artifact = dict(status=result["status"], knowledge=dict(
            kb_id=kb_id, status=result["status"], questions=questions,
            references=result["references"] if ready else [], warnings=result["warnings"],
            egress=result["egress"] if ready else None))
        if ready:
            return result["payload_text"] + ("\n\n" + "\n".join(result["warnings"]) if result["warnings"] else ""), artifact
        if result["status"] not in {"low_relevance", "external_authorization_required"} and not result["authorized"]:
            return EGRESS_REQUIRED, artifact  # 受限依据的限定摘录也只在授权后发给模型
        return result["answer"], artifact

    return [search_knowledge]


class KnowledgeSearchGuard(AgentMiddleware):
    """已选知识库、用户在问资料，模型却一次工具都没调用就直接作答时，由程序补一次检索。

    提示词要求“先 search_knowledge 再回答”，但模型偶尔凭通用知识直接回答（素材库评测 C09：中文问英文标准条款，
    语义相似度低于知识库路由下限而交给助理，助理未检索就按“通行条款结构”作答）。知识问答的事实只能来自本轮
    检索到的原文，这一点不能只靠提示词：丢弃这份未检索的草稿，换成一次以用户原话为检索问题的 search_knowledge
    调用，模型拿到依据后重新作答，回答检查照常对重新作答的那次进行（不占检查重写次数）。

    不介入：指定了能力或附了材料、意图是业务查询/调整上一轮等非知识类、句式不像提问（闲聊、写作任务），
    或本轮已调用过任何工具（检索过、读过材料或查过业务数据）。每轮最多补一次：补检索后本轮已有工具结果。
    """

    def wrap_model_call(self, request, handler):
        response = handler(request)
        result = response.result if isinstance(response, ModelResponse) else [response]
        answer = next((m for m in reversed(result) if isinstance(m, AIMessage)), None)
        if answer is None or answer.tool_calls or answer.response_metadata.get(SYSTEM_REPLY):
            return response
        questions = self._questions(request)
        if not questions:
            return response
        log.warning("已选知识库的资料提问未检索就作答，丢弃草稿，补检索：%s", questions)
        call = dict(name="search_knowledge", args=dict(questions=questions), id=f"call_guard_{uuid.uuid4().hex[:12]}", type="tool_call")
        forced = AIMessage("", id=answer.id, tool_calls=[call],
                           response_metadata={**answer.response_metadata, "forced_search": True})
        return ModelResponse(result=[forced if m is answer else m for m in result],
                             structured_response=getattr(response, "structured_response", None))

    @staticmethod
    def _questions(request) -> list[str]:
        """需要补检索时返回检索问题（用户原话；简短追问另加上一问拼接的补全句），否则返回空列表。"""
        context = getattr(getattr(request, "runtime", None), "context", None)
        if context is None or not context.kb_id or context.skill or context.material_ids:
            return []
        if (context.intent or {}).get("intent") not in _KNOWLEDGE_INTENTS:
            return []
        if "search_knowledge" not in {getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None)
                                      for t in request.tools or []}:
            return []
        messages = request.messages
        start = _turn_start(messages)
        if start < 0 or any(isinstance(m, ToolMessage) for m in messages[start + 1:]):
            return []
        text = (messages[start].additional_kwargs.get("display") or messages[start].text).strip()
        if not knowledge_question(text):
            return []
        questions = [text[:200]]
        previous = next((m for m in reversed(messages[:start]) if isinstance(m, HumanMessage)
                         and not m.additional_kwargs.get("internal")), None)
        if previous is not None and (len(text) <= 15 or _FOLLOW_UP.match(text)):
            # 只有原话的追问（“那病假呢”）检索不到东西：按上一问补全，与工具说明要求模型做的补全一致
            earlier = (previous.additional_kwargs.get("display") or previous.text).strip()[:100]
            questions.append(f"{earlier} {text}")
        return questions[:MAX_SUB_QUESTIONS]


# 交给助理的资料类轮次：漏斗没把握（agent），或知识/定位捷径不处理而转给助理的
_KNOWLEDGE_INTENTS = {"agent", "knowledge", "locate"}


@after_agent
def record_knowledge_turn(state, runtime):
    """轮次收尾：助理路径的引用列表和外发计数由程序确定，写进回复的 response_metadata["knowledge"]。

    本轮检索过：引用 = 本轮依据中回答实际引用的；外发 = 本轮各次模型调用实际发出的依据（ContextBuilder 记在
    egress_sent）、历史中残留的依据和材料，分开计数。本轮没检索但回答带〔依据N〕（调整上一轮）：沿用上一轮回答的
    引用列表并提示，让编号仍可核对，而不是留下无法打开的编号。"""
    messages = state["messages"]
    start = _turn_start(messages)
    reply = messages[-1]
    if start < 0 or not isinstance(reply, AIMessage) or reply.tool_calls or reply.response_metadata.get(SYSTEM_REPLY) \
            or "knowledge" in reply.response_metadata:
        return None
    turn = messages[start + 1:]
    searches = _searches(turn)
    cited = set(cited_numbers(reply.text))
    if searches:
        refs = [r for s in searches for r in s["references"]]
        labels = {int(r["label"].removeprefix("依据")) for r in refs}
        warnings = list(dict.fromkeys(w for s in searches for w in s["warnings"]))
        if cited - labels:
            warnings.append("回答含无法定位的依据编号，请核对原文。")
        knowledge = dict(kb_id=searches[-1]["kb_id"], status=searches[-1]["status"], warnings=warnings,
                         references=[r for r in refs if int(r["label"].removeprefix("依据")) in cited],
                         egress=_turn_egress(turn, searches))
    else:
        if not cited:
            return None
        previous = next((m.response_metadata["knowledge"] for m in reversed(messages[:start])
                         if isinstance(m, AIMessage) and not m.tool_calls and (m.response_metadata.get("knowledge") or {}).get("references")), None)
        if previous is None:
            knowledge = dict(kb_id=runtime.context.kb_id if runtime.context else None, status="carried_over", references=[], egress=None,
                             warnings=["回答含〔依据N〕编号，但本轮没有检索，也没有可沿用的上一轮依据，请核对原文。"])
        else:
            refs = [r for r in previous["references"] if r["label"].startswith("依据") and int(r["label"].removeprefix("依据")) in cited]
            warnings = ["本轮没有重新检索，引用沿用上一轮回答的依据，请核对原文。"]
            if len(refs) < len(cited):
                warnings.append("回答含无法定位的依据编号，请核对原文。")
            knowledge = dict(kb_id=previous["kb_id"], status="carried_over", references=refs, warnings=warnings, egress=None)
    reply = reply.model_copy(update={"response_metadata": {**reply.response_metadata, "knowledge": knowledge}})
    return {"messages": [reply]}  # 按消息 ID 替换


def _turn_egress(turn, searches) -> dict:
    """按本轮各次模型调用实际发出的内容计数（ContextBuilder 在每次调用后记在回复的 egress_sent）。"""
    sent, history, materials = set(), 0, 0
    for m in turn:
        record = m.response_metadata.get("egress_sent") if isinstance(m, AIMessage) else None
        if record:
            sent |= set(record["evidence"])
            history = max(history, record["history_evidence"])
            materials = max(materials, record["materials"])
    redactions = {}
    for s in searches:
        for name, count in ((s.get("egress") or {}).get("redactions") or {}).items():
            redactions[name] = redactions.get(name, 0) + count
    return dict(evidence_sent=len(sent), history_sent=True, history_evidence=history, materials=materials,
                redactions=redactions, policy=EGRESS_POLICY + "；对话历史与附带的材料不经规则处理")
