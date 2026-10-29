"""意图漏斗：规则层 → 上下文轻量语义层 → 大模型兜底层（助理智能体本身）。

按处理手段的成本分层，哪层有把握就在哪层结束（方案见 files/plans/plan-intent-funnel.md）：
- 规则层：关键词/正则，只收明确、高频、无歧义的请求；
- 语义层：会话状态 + 本地 Embedding 与示例句的相似度，本地运行、不外发；
- 兜底层：交给助理智能体理解与拆解，不另建分类器。
识别结果只决定走捷径、收窄能力或设置护栏；事实仍只来自材料与检索原文。
"""
import re
from dataclasses import asdict, dataclass

from . import config
from .skills import SKILLS

LABELS = {
    "summarize": "文档总结", "minutes": "会议纪要", "weekly": "周报起草", "todo": "待办梳理", "polish": "文本优化",
    "knowledge": "知识问答", "locate": "文档定位", "business_query": "业务查询", "business_action": "业务操作",
    "revise": "调整上一轮", "greeting": "闲聊咨询", "realtime": "实时数据", "out_of_scope": "非办公请求",
    "blocked": "已拦截", "clarify": "需要澄清", "agent": "交给助理判断",
}
LAYERS = {1: "规则层", 2: "语义层", 3: "兜底层"}


@dataclass
class TurnState:
    """语义层使用的会话状态；只用于判断意图，不作为事实依据。"""

    has_materials: bool = False
    kb_id: str | None = None
    last_intent: str | None = None
    has_previous_reply: bool = False


@dataclass
class IntentDecision:
    intent: str
    layer: int
    reason: str
    confidence: float | None = None
    matched: str | None = None

    @property
    def skill(self) -> str | None:
        return self.intent if self.intent in SKILLS else None

    def to_dict(self) -> dict:
        return {**asdict(self), "label": LABELS[self.intent], "layer_label": LAYERS[self.layer]}


# 规则层：准入严格，只放长期稳定、没有歧义的写法。
_BLOCKED = re.compile(
    r"忽略(?:之前|以上|上面|前面|所有)?的?(?:规则|指令|提示|设定)|ignore (?:all |the )?(?:previous|above) instructions"
    r"|(?:告诉|显示|输出|泄露|打印)(?:我)?(?:你的)?(?:系统提示词?|system prompt|密钥|api ?key)", re.I)
_GREETING = re.compile(r"(?:你好|您好|嗨|hi|hello|在吗|早上好|下午好|晚上好)[呀啊！!。.~\s]*", re.I)
_ABILITY = re.compile(r"你(?:能|可以|会)(?:做|干|帮我做)?(?:什么|啥|哪些)|你有(?:哪些|什么)(?:功能|能力)|怎么用你")
_WEEKLY = re.compile(r"(?:生成|写|起草|撰写)(?:一份|一下)?(?:本周|这周|这一周|上周)?的?周报")
_FULL_SUMMARY = re.compile(r"全文总结|(?:总结|概括|梳理)(?:一下)?(?:整份|整个|全部|全文|所有|这份|该份|这篇|本文)")
_BARE_REFERENCE = re.compile(r"(?:它|这个|那个|刚才那个|上面那个)(?:呢|是什么|什么时候|要多久|怎么做|如何处理)[？?。!\s]*")
_REALTIME = re.compile(r"(?:今天|明天|现在|实时|最新|当前).{0,6}?(?:天气|气温|股价|汇率|新闻|油价|航班|路况|比分)")
# 多目标、追问补全交给兜底层，语义层不硬分类。
_COMPLEX = re.compile(r"然后|并且|同时|顺便|再把|另外|以及|对比|区别|那.{1,12}呢")

EXAMPLES: dict[str, list[str]] = {
    "summarize": ["总结一下这份文档", "这份材料主要讲了什么", "帮我提炼这篇文章的要点", "概括这份报告的核心内容"],
    "minutes": ["把这段会议记录整理成纪要", "整理一下今天的会议内容", "会上都定了哪些事，帮我拆一下", "根据录音文字整理会议结论和行动项"],
    "weekly": ["帮我写这周的周报", "根据这些材料起草周报", "本周工作总结怎么写", "把这周做的事整理成周报"],
    "todo": ["这里面有哪些需要跟进的事", "帮我提取待办事项", "列出要做的任务和负责人", "梳理一下后续行动"],
    "polish": ["帮我润色这段话", "把这段文字改得更正式一点", "优化一下这封邮件的措辞", "这段话读起来不通顺，帮我改改"],
    "knowledge": ["试用期多长时间", "出差住宿标准是多少", "报销需要哪些审批", "年假有几天", "加班费怎么计算", "产品支持哪些操作系统",
                  "新员工入职需要办哪些手续"],
    "locate": ["哪份文件讲了报销流程", "关于差旅的规定在哪个文档里", "哪个制度提到了考勤", "找一下讲请假的文件"],
    "business_query": ["我这周还有哪些待办", "有哪些逾期的任务", "这周周报整理到哪一步了", "材料库里有哪些文件"],
    "business_action": ["把这条加到待办里", "新建一个待办提醒我周五交报告", "把这段写进周报", "记一条进展：接口已联调完成"],
    "revise": ["再短一点", "换成表格", "不是这份，是另一份", "重新来一遍", "用英文再写一次", "语气再正式一些"],
    "out_of_scope": ["帮我写一首诗", "写一个Python快速排序", "讲个笑话", "推荐一部电影", "解释一下相对论"],
}
_NEEDS_PREVIOUS = {"revise"}
_STRICTER = {"out_of_scope": 0.04}  # 误拒办公请求代价更大，要求更高的相似度


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


class IntentFunnel:
    """encoder_factory 返回本地 Embedding（需有 embed_documents）；不传或加载失败时只用规则层。"""

    def __init__(self, encoder_factory=None, examples: dict[str, list[str]] | None = None,
                 threshold: float | None = None, margin: float | None = None) -> None:
        self.encoder_factory = encoder_factory
        self.examples = examples or EXAMPLES
        self.threshold = config.INTENT_THRESHOLD if threshold is None else threshold
        self.margin = config.INTENT_MARGIN if margin is None else margin
        self._vectors: list[tuple[str, str, list[float]]] | None = None

    def classify(self, text: str, state: TurnState | None = None, skill: str | None = None) -> IntentDecision:
        state = state or TurnState()
        if skill:  # 命令行显式指定的能力是规则层输入
            return IntentDecision(skill, 1, "用户显式指定能力", 1.0, skill)
        return self._rules(text.strip(), state) or self._semantic(text.strip(), state) or \
            IntentDecision("agent", 3, "规则与语义层都没有把握，交给助理理解")

    def _rules(self, text: str, state: TurnState) -> IntentDecision | None:
        if not text:
            return IntentDecision("clarify", 1, "空请求", 1.0)
        if m := _BLOCKED.search(text):
            return IntentDecision("blocked", 1, "要求越过规则或索取内部信息", 1.0, m.group())
        if _GREETING.fullmatch(text) or _ABILITY.search(text):
            return IntentDecision("greeting", 1, "打招呼或询问能力", 1.0, text[:20])
        if m := _REALTIME.search(text):
            return IntentDecision("realtime", 1, "需要实时外部数据", 1.0, m.group())
        if _BARE_REFERENCE.fullmatch(text) and not state.has_previous_reply:
            return IntentDecision("clarify", 1, "只有指代、没有可补全的上文", 1.0, text)
        if m := _WEEKLY.search(text):
            return IntentDecision("weekly", 1, "明确要求起草周报", 1.0, m.group())
        if (m := _FULL_SUMMARY.search(text)) and not _COMPLEX.search(text):
            return IntentDecision("summarize", 1, "明确要求整份总结", 1.0, m.group())
        return None

    def _semantic(self, text: str, state: TurnState) -> IntentDecision | None:
        if _COMPLEX.search(text) or len(text) > 300:  # 多目标、追问与长输入交给兜底层
            return None
        vectors = self._load()
        if not vectors:
            return None
        # “要求：正文”的写法只比较冒号前的要求，避免粘贴的正文稀释相似度
        head = re.match(r"(.{4,30}?)[：:]", text)
        try:
            query = self.encoder_factory().embed_documents([head.group(1) if head else text])[0]
        except Exception:  # 输入超长、模型异常时不影响对话
            return None
        best: dict[str, tuple[float, str]] = {}
        for intent, example, vector in vectors:
            if intent in _NEEDS_PREVIOUS and not state.has_previous_reply:
                continue
            score = _cosine(query, vector)
            if score > best.get(intent, (-1.0, ""))[0]:
                best[intent] = (score, example)
        ranked = sorted(best.items(), key=lambda kv: -kv[1][0])
        if not ranked:
            return None
        intent, (score, example) = ranked[0]
        second = ranked[1][1][0] if len(ranked) > 1 else 0.0
        if score < self.threshold + _STRICTER.get(intent, 0) or score - second < self.margin:
            return None
        return IntentDecision(intent, 2, f"与示例“{example}”最接近", round(score, 4), example)

    def _load(self):
        if self._vectors is not None or self.encoder_factory is None:
            return self._vectors
        try:
            encoder = self.encoder_factory()
            pairs = [(intent, text) for intent, texts in self.examples.items() for text in texts]
            vectors = encoder.embed_documents([text for _, text in pairs])
        except Exception:  # 未配置本地 Embedding 时只用规则层
            return None
        self._vectors = [(intent, text, vector) for (intent, text), vector in zip(pairs, vectors)]
        return self._vectors


# 处理器登记表中“直接回复”的意图：不调用模型。其余意图由助理按能力或知识检索处理。
DIRECT_REPLIES = {
    "blocked": "这个请求涉及越过使用规则或索取内部信息，无法执行。可以直接说明你要处理的办公任务。",
    "clarify": "请补充你想了解的对象和具体问题，例如“差旅制度里住宿标准是多少”。",
    "greeting": ("你好，我是 Briefly 办公助理，可以帮你：总结文档、整理会议纪要、起草周报、梳理待办、优化文字，"
                 "以及在选定的知识库里查找制度和资料并给出原文依据。直接用一句话告诉我要做什么即可。"),
    "realtime": "目前还不能查询天气、股价、新闻等实时数据。可以处理你提供的材料，或在知识库里查找已入库的资料。",
    "out_of_scope": ("这个请求不在办公助理的范围内，我主要帮你处理文档总结、会议纪要、周报、待办、文字优化和知识库查询。"
                     "如果和工作材料有关，可以把材料发给我。"),
}
