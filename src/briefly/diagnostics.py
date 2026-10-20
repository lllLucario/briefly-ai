"""诊断：对话里“工具做 Map-Reduce → Agent 改写”这条链路，信息在哪一步丢失。只做测量，不改变输出。

对照原始材料中的基准事实，记录以下阶段：
    baseline      原始材料中的基准事实（由评估用例给出：每个事实的关键取值和所在材料）
    extraction    可选，summarize_materials 的逐块提取要点（artifact["items"]）
    map_reduce    summarize_materials 的完整输出（artifact["content_full"]，截断前）
    tool_message  实际交给 Agent 的 ToolMessage（经过 ToolBudgetMiddleware，可能被截断）
    answer        Agent 最终回答
最终回答里缺失的事实，第一次缺失的阶段就是它的损失来源：Map-Reduce / 工具预算截断 / Agent 改写。
中途缺失、但最终回答里又有的（如截断后 Agent 用 read_tool_result 读回），记为 recovered，不算损失。
另外统计：冲突保留率、来源归属错误、无来源断言（回答中的数字在所有材料里都找不到）、Token 与耗时。
判断“写了没有”比较关键取值及用例声明的等义表达；可选subject约束在同一句/表格行，不能证明任意语义正确。
"""
import re
from dataclasses import dataclass, field

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from .conflicts import value_in

STAGES = ("map_reduce", "tool_message", "answer")
LOSS = {"map_reduce": "Map-Reduce", "tool_message": "工具预算截断", "answer": "Agent 改写"}


@dataclass
class Fact:
    """基准事实：terms 都出现才算保留；source 是所在材料的标题（用于检查来源归属）。"""

    id: str
    terms: list[str]
    source: str = ""
    subject: str = ""  # 可选事项锚点；填写时要求取值与事项出现在同一句/同一表格行
    alternatives: dict[str,list[str]] = field(default_factory=dict)  # 评测显式声明的等义表达，不能凭数字替代否定


@dataclass
class ConflictSpec:
    """基准冲突：每个取值都要在该阶段出现，才算冲突被保留。"""

    id: str
    values: list[str]


@dataclass
class Stages:
    map_reduce: str = ""
    tool_message: str = ""
    answer: str = ""
    truncated: bool = False
    tool_called: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    seconds: float = 0.0
    extraction: str | None = None  # 有提取artifact时区分提取损失与合成损失


@dataclass
class Diagnosis:
    retention: dict[str, float] = field(default_factory=dict)  # 各阶段关键事实保留率
    lost_at: dict[str, list[str]] = field(default_factory=dict)  # 损失来源 → 事实 id（只算最终回答里缺失的）
    recovered: list[str] = field(default_factory=list)  # 中途缺失、最终回答里又有的事实
    conflict_retention: dict[str, float] = field(default_factory=dict)
    conflicts_lost_at: dict[str, list[str]] = field(default_factory=dict)
    attribution_errors: list[str] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)


def has_fact(text: str, fact: Fact) -> bool:
    def matches(line):
        return all(any(value_in(line,value) for value in [term,*fact.alternatives.get(term,[])]) for term in fact.terms)
    if fact.subject:
        return any(re.sub(r"\s+","",fact.subject) in re.sub(r"\s+","",line) and matches(line) for line in _lines(text))
    return matches(text)


def stages_from_history(messages) -> Stages:
    """从一次对话的消息历史取出各阶段内容（取最后一轮）。"""
    start = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage) and not m.additional_kwargs.get("internal")), default=-1)
    turn = messages[start + 1 :]
    st = Stages()
    for m in turn:
        if isinstance(m, ToolMessage) and m.name == "summarize_materials":
            st.tool_called = True
            visible = m.text if hasattr(m, "text") else str(m.content)
            st.tool_message = "\n\n".join(x for x in [st.tool_message,visible] if x)
            full = (m.artifact or {}).get("content_full") if isinstance(m.artifact, dict) else None
            st.map_reduce = "\n\n".join(x for x in [st.map_reduce,full or visible] if x)
            items = (m.artifact or {}).get("items") if isinstance(m.artifact,dict) else None
            if items is not None:
                points = "\n".join(i.get("point","") for i in items)
                st.extraction = "\n".join(x for x in [st.extraction,points] if x)
            st.truncated = st.truncated or "工具结果过长" in visible
    answers = [m for m in turn if isinstance(m, AIMessage) and not m.tool_calls]
    st.answer = answers[-1].text if answers else ""
    return st


def _lines(text: str) -> list[str]:
    from .fidelity import clauses
    return clauses(text)


def diagnose(facts: list[Fact], conflicts: list[ConflictSpec], stages: Stages, materials: dict[str, str]) -> Diagnosis:
    """materials：材料标题 → 全文，用于检查回答中的数字是否有来源。"""
    d = Diagnosis()
    texts = {"map_reduce": stages.map_reduce, "tool_message": stages.tool_message, "answer": stages.answer}
    order = STAGES
    loss = LOSS
    if stages.extraction is not None:
        texts["extraction"] = stages.extraction
        order = ("extraction",*STAGES)
        loss = {**LOSS,"extraction":"逐块提取","map_reduce":"合成汇总"}
    for stage in order:
        kept = [f for f in facts if has_fact(texts[stage], f)]
        d.retention[stage] = round(len(kept) / len(facts), 3) if facts else 1.0
        ckept = [c for c in conflicts if all(value_in(texts[stage], v) for v in c.values)]
        d.conflict_retention[stage] = round(len(ckept) / len(conflicts), 3) if conflicts else 1.0
    for f in facts:
        stage = next((s for s in order if not has_fact(texts[s], f)), None)
        if stage and not has_fact(texts["answer"], f):
            d.lost_at.setdefault(loss[stage], []).append(f.id)
        elif stage:
            d.recovered.append(f.id)
    for c in conflicts:
        stage = next((s for s in order if not all(value_in(texts[s], v) for v in c.values)), None)
        if stage and not all(value_in(texts["answer"], v) for v in c.values):
            d.conflicts_lost_at.setdefault(loss[stage], []).append(c.id)
    # 来源归属错误：回答的同一句里写了某个事实，也标了材料名，但标的不是这个事实所在的材料
    titles = list(materials)
    for line in _lines(stages.answer):
        named = [t for t in titles if t and t in line]
        if not named:
            continue
        for f in facts:
            # 标注的材料里确实没有这个事实才算错（同一事实出现在多份材料时不算）
            if f.source and has_fact(line, f) and f.source not in named and not any(has_fact(materials[t], f) for t in named):
                d.attribution_errors.append(f"{f.id}：写在标注《{'》《'.join(named)}》的句子里，实际出自《{f.source}》")
    # 无来源断言：回答中的数字、日期在所有材料里都找不到
    pool = "\n".join(materials.values())
    answer = re.sub(r"(?m)^\s*(?:[-*•]\s*)?\d+[.、)）]\s*", "", stages.answer)  # 列表序号不是断言
    for term in dict.fromkeys(re.findall(r"\d+(?:\.\d+)?\s*(?:%|月\s*\d+\s*日|万|元|条|毫秒|秒|天|个)?", answer)):
        if term.strip() and not value_in(pool, term):
            d.unsupported.append(term.strip())
    return d
