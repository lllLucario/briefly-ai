"""知识库作为助理的一项能力：单问捷径、文档定位（本地）与供智能体多轮使用的检索工具。

会话历史只用来理解问题；事实只来自本轮检索到的知识库原文。外发前检查知识库一次性授权。
"""
from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from .. import config
from ..context import Context
from .evidence import restricted_answer
from .payload import evidence_payload
from .qa import EGRESS_REQUIRED, KnowledgeQA, Redactor

MAX_SUB_QUESTIONS = 3


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
        result = self.retrieval.search(kb_id, question)
        sims = [1 - route["raw_score"] for item in result["evidence"]
                for route in item["retrieval"]["routes"] if route["route"] == "semantic"]
        return max(sims) if sims else None

    def answer(self, kb_id: str, question: str) -> dict:
        """B1 单点查询捷径：沿用知识问答的质量、授权与引用检查。"""
        return self.qa.answer(kb_id, question)

    def locate(self, kb_id: str, question: str) -> dict:
        """B2 文档定位：只在本地检索并列出文档与章节，不调用模型、不外发。"""
        result = self.retrieval.search(kb_id, question)
        if not result["evidence"]:
            return dict(status=result["status"], answer="当前知识库没有找到相关文档。", evidence=[])
        documents: dict[str, list[str]] = {}
        for item in result["evidence"]:
            sections = documents.setdefault(item["source"]["title"], [])
            # 章节路径首段通常是文档标题，只显示最后一级，便于阅读
            if (section := (item["section"] or "章节未确定").split(" > ")[-1]) not in sections:
                sections.append(section)
        lines = [f"- 《{title}》：{'、'.join(sections)}" for title, sections in documents.items()]
        return dict(status="located", evidence=result["evidence"],
                    answer="以下文档与问题相关（按相关度排列，仅定位位置，请打开原文核对）：\n" + "\n".join(lines))

    def search(self, kb_id: str, questions: list[str]) -> dict:
        """供智能体使用：一次最多3个子问题，各自检索后合并去重，总额不超过知识问答依据预算。"""
        questions = [q.strip() for q in questions if q and q.strip()][:MAX_SUB_QUESTIONS]
        if not questions:
            raise ValueError("请至少提供一个检索问题")
        if not self.knowledge.egress_allowed(kb_id):
            return dict(status="external_authorization_required", text=EGRESS_REQUIRED, evidence=[])
        share = config.KNOWLEDGE_CONTEXT_TOKENS // len(questions)  # 每个子问题保底同样的额度
        evidence, seen, unavailable = [], set(), []
        for question in questions:
            result = self.retrieval.search(kb_id, question, include_document_context=True,
                                           context_budget_only=True, context_tokens=share)
            unavailable = unavailable or result["unavailable"]
            for item in result["evidence"]:
                if item["reference"] not in seen:
                    seen.add(item["reference"])
                    evidence.append(item)
        if not evidence:
            return dict(status="no_candidates", text="当前知识库没有检索到与问题相关的原文。", evidence=[])
        if restricted := restricted_answer(evidence):
            return dict(status=restricted["status"], text=restricted["answer"], evidence=evidence)
        redactor = Redactor()
        text = redactor.text(evidence_payload("；".join(questions), evidence))
        notes = ["范围内还有未完成索引的资料，本次只依据已就绪部分。"] if unavailable else []
        return dict(status="candidates", text=text, evidence=evidence, notes=notes, redactions=redactor.counts)


def knowledge_tools(assistant: KnowledgeAssistant) -> list:
    @tool(response_format="content_and_artifact")
    def search_knowledge(questions: list[str], runtime: ToolRuntime[Context]) -> tuple[str, dict]:
        """在用户本次选定的知识库里检索制度、项目资料、产品手册等原文。
        questions：1～3 个完整、独立的检索问题。用户追问省略了主语时，结合上文补全后再检索（例如上一轮问年假，
        这一轮“那病假呢？”应写成“病假有几天、如何申请”）；一句话里有多个问题时拆开一次传入。
        回答只能依据本工具返回的原文，关键结论用〔依据N〕标注并附逐字摘录；之前的聊天回答不是依据。
        返回内容不足以回答时如实说明未提及或依据不足。"""
        kb_id = runtime.context.kb_id if runtime.context else None
        if not kb_id:
            return "用户没有选择知识库，请提示用户先在输入框上方选择要查询的知识库。", {}
        result = assistant.search(kb_id, questions)
        artifact = dict(status=result["status"], knowledge=dict(kb_id=kb_id, references=[
            dict(label=f"依据{i}", reference=item["reference"], title=item["source"]["title"])
            for i, item in enumerate(result["evidence"], 1)]))
        if result["status"] == "candidates":
            body = result["text"] + ("\n\n" + "\n".join(result["notes"]) if result["notes"] else "")
            return body, artifact
        return result["text"], artifact

    return [search_knowledge]
