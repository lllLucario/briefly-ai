"""意图漏斗：规则层 → 上下文轻量语义层 → 大模型兜底层（助理智能体本身）。

按处理手段的成本分层，哪层有把握就在哪层结束（方案见 files/plans/plan-intent-funnel.md）：
- 规则层：关键词/正则，只收明确、高频、无歧义的请求；
- 语义层：会话状态 + 本地 Embedding 与示例句的相似度，本地运行、不外发；
- 兜底层：交给助理智能体理解与拆解，不另建分类器。
识别结果只决定走捷径、收窄能力或设置护栏；事实仍只来自材料与检索原文。
"""
import logging
import re
from dataclasses import asdict, dataclass

from . import config
from .relevance_floor import kb_floor, missing_floor_message
from .skills import SKILLS

log = logging.getLogger(__name__)

LABELS = {
    "summarize": "文档总结", "minutes": "会议纪要", "weekly": "周报起草", "todo": "待办梳理", "polish": "文本优化",
    "knowledge": "知识问答", "locate": "文档定位", "business_query": "业务查询", "business_action": "业务操作",
    "revise": "调整上一轮", "greeting": "闲聊咨询", "realtime": "实时数据", "out_of_scope": "非办公请求",
    "blocked": "已拦截", "clarify": "需要澄清", "agent": "交给助理判断",
}
LAYERS = {1: "规则层", 2: "语义层", 3: "兜底层"}


@dataclass
class TurnState:
    """语义层使用的会话状态；只用于判断意图，不作为事实依据。

    has_materials：本条消息附了材料；earlier_materials：本会话之前附过或读过材料。
    has_previous_reply：之前有可供调整的回答（问候、拒绝、提示选择知识库等固定回复不算）。
    """

    has_materials: bool = False
    kb_id: str | None = None
    last_intent: str | None = None
    has_previous_reply: bool = False
    earlier_materials: bool = False

    @property
    def has_body(self) -> bool:
        """有可处理的对象：本轮或之前的材料，或可调整的上一轮回答。"""
        return self.has_materials or self.earlier_materials or self.has_previous_reply


@dataclass
class IntentDecision:
    intent: str
    layer: int
    reason: str
    confidence: float | None = None
    matched: str | None = None
    reply: str | None = None  # 直接回复的意图按具体原因给出的答复；为空时用 DIRECT_REPLIES 的通用答复
    # 判断途中算出的分数，只供意图日志调参（intent_log.py），不参与路由：kb_score 为知识库路由相似度，
    # semantic 为语义层第一名 {intent, example, score, gap}；未达门槛、最后交给助理时也记
    kb_score: float | None = None
    semantic: dict | None = None

    @property
    def skill(self) -> str | None:
        return self.intent if self.intent in SKILLS else None

    def to_dict(self) -> dict:
        data = {k: v for k, v in asdict(self).items() if k != "reply"}  # 答复正文已在回复消息里，不重复记录
        return {**data, "label": LABELS[self.intent], "layer_label": LAYERS[self.layer]}


# 规则层：准入严格，只放长期稳定、没有歧义的写法。
# 越权拦截分三类：索要系统内部信息、要求绕过限制、索要他人数据。误拦是直接拒绝，代价高，
# 所以密钥/密码类必须明确指向系统自身（“你的”“.env”“后端”等），办公里正常提到密码、API key 不拦。
_GIVE = r"(?:给我|发给?我?|看看?|贴|展示|显示|输出|泄露|透露|打印|附|复制|读取?|告诉|导出|列出|编码|反写|拿出)"
_SELF_SECRET = r"(?:系统提示词?|提示词原文|system ?prompt|开发者指令|隐藏的?(?:指令|提示词?|消息|设定)|system ?消息|\.env|环境变量)"
_KEY = (r"(?:你的|你用的|你们用的|系统的?|后端的?|服务器的?|运行环境的?|环境变量中?的?|\.env里?的?|配置里的?)"
        r".{0,8}?(?:密钥|api ?key|token|密码)")
_BLOCKED = re.compile(
    rf"{_GIVE}.{{0,20}}?(?:{_SELF_SECRET}|{_KEY})|(?:{_SELF_SECRET}|{_KEY}).{{0,20}}?{_GIVE}"
    r"|(?:忽略|无视|绕过|跳过|关闭|关掉)(?:你的|所有的?|一切|之前的?|以上的?|上面的?|前面的?|系统的?)?"
    r"(?:规则|指令|提示词?|设定|权限检查|保密限制|安全限制|审计)"
    r"|给我(?:你的)?(?:api ?key|密钥)(?!.{0,6}(?:怎么|如何|申请|流程|在哪|规定))"
    r"|ignore (?:all |the )?(?:previous |above )?(?:rules|instructions)"
    r"|其他用户|别的用户|没(?:有)?权限(?:看|访问|查看)", re.I)
_GREETING = re.compile(r"(?:你好|您好|嗨|哈喽|hi|hello|在吗|早上好|下午好|晚上好)[呀啊！!。.~\s]*", re.I)
_ABILITY = re.compile(r"你(?:能|可以|会)(?:帮.{0,8}?)?(?:做|干)?(?:些|点)?(?:什么|啥|哪些)|你有(?:哪些|什么)(?:功能|能力)|怎么用你")
_WEEKLY = re.compile(r"(?:生成|写|起草|撰写)(?:一份|一下)?(?:本周|这周|这一周|上周)?的?周报")
_FULL_SUMMARY = re.compile(r"全文总结|(?:总结|概括|梳理)(?:一下)?(?:整份|整个|全部|全文|所有|这份|该份|这篇|本文)")
_BARE_REFERENCE = re.compile(r"(?:它|这个|那个|刚才那个|上面那个)(?:呢|是什么|什么时候|要多久|怎么做|如何处理)[？?。!\s]*")
_REALTIME = re.compile(r"(?:今天|明天|现在|实时|最新|当前).{0,6}?(?:天气|气温|股价|汇率|新闻|油价|航班|路况|比分)")
# 问自己业务数据的进度/数量：明确的进度词，或“我/自己”加业务对象和“哪些/几条”
_PROGRESS = re.compile(
    r"(?:待办|任务|周报|审核|材料).{0,10}?(?:生成了[吗没]|好了[吗没]|提交了[吗没]|到哪一?步|什么状态|进度(?:如何|怎么样))"
    r"|(?:我|自己)(?:的|这周的?|还|现在)?(?:有|剩)?(?:哪些|几条|多少条?).{0,6}?(?:待办|任务|逾期)"
    r"|(?:我|自己)(?:的|这周的?|还|现在)?(?:有|剩)?.{0,6}?(?:待办|任务|逾期).{0,4}?(?:哪些|几条|多少)")
# 没有正文的处理指令、指向材料的提问：会话里没有材料和可调整的回答时需要澄清
_BARE_TASK = re.compile(r"(?:请|帮我|麻烦)?(?:润色|总结|概括|提炼|整理|改写|优化|纪要|梳理)(?:一下|下)?"
                        r"(?:这里面|这份|这篇|这个|它|材料|文件|内容)?(?:的?(?:内容|要点))?[。！？!?\s~]*")
_DEICTIC = re.compile(r"(?:这里面|这份|这篇|这个文件|附件|上面的?材料)(?:都|主要)?(?:讲了?|说了?|写了?)(?:些)?(?:什么|啥)")
# 知识库路由：话题由选定知识库的检索相关度判断（示例句覆盖不了千变万化的话题），任务由句式判断。
_TASK_VERB = re.compile(r"总结|概括|提炼|梳理|捋|摘要|概览|介绍一下|整理|纪要|周报|起草|撰写|写(?:一?[封份篇段个首]|成)"
                        r"|润色|改写|优化|翻译|拟|设计|做[个一]|生成|列成|拆成|新增|添加|追加|记一条|改成|换成|讲解稿"
                        r"|加[到进入]|放[到进]"
                        r"|比较(?:这|两|一下|下)")
_ASK = re.compile(r"[？?]$|多少|几[天种条个次门项级年月周]|哪些|什么|怎么|怎样|如何|是否|能否|吗[？?。]?$|谁|多久|何时|有没有")
_WHERE = re.compile(r"哪[个份]?(?:文件|文档|制度|手册|方案|程序|材料)|哪一?[章节条]|哪个?小节|出处|原文在哪|写在哪|哪里写"
                    r"|条号|章节|定位|(?:表|附件|条款|规定)(?:放)?在哪|附件位置|标出.{0,6}位置"
                    r"|(?:段|部分|区域)在哪|哪一?段|哪(?:张|个)表|什么表|哪个?(?:区域|部分)|什么区域")
# 承接上一轮的追问（“那试用期呢”“那假如……”）需要结合上文补全，不按原句直接检索
_FOLLOW_UP = re.compile(r"(?:那|那么|那如果|那假如|那要是|还有|同样)[，,\s]?")
# 有上一轮回答、本轮只要求换格式或换语言：调整上一轮
_REFORMAT = re.compile(r"(?:请|帮我)?再?(?:改成|换成|变成|转成|整理成|做成|列成)(?:一个|一张|一份)?"
                       r"(?:表格|列表|清单|要点|[两三四五]列|一段话|段落|英文|中文)(?:[，,：:].{0,12})?[。.!！]?")
# 多目标、追问补全交给兜底层，语义层不硬分类。
_COMPLEX = re.compile(r"然后|并且|同时|顺便|再把|另外|以及|对比|区别|那.{1,12}呢|先.{1,30}再")

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
    # 从零写作、办公软件操作：不属于五项能力，由助理按“通用办公帮助”回答；没有这组示例时“写个请假条”最接近润色
    "office": ["帮我写个请假条", "写一份放假通知", "起草一封给客户的邮件", "Excel 怎么冻结首行", "Word 里怎么加页码",
               "PPT 怎么统一换字体"],
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
                 threshold: float | None = None, margin: float | None = None, kb_probe=None) -> None:
        """kb_probe(kb_id, text) 返回选定知识库中最相关片段的相似度（本地检索，不外发）。"""
        self.encoder_factory = encoder_factory
        self.kb_probe = kb_probe
        self.kb_threshold: float | None = None  # 不指定时按当前 Embedding 指纹取登记值（标定扫描时直接赋值）
        self._kb_floor_checked = False
        self.examples = examples or EXAMPLES
        self.threshold = config.INTENT_THRESHOLD if threshold is None else threshold
        self.margin = config.INTENT_MARGIN if margin is None else margin
        self._vectors: list[tuple[str, str, list[float]]] | None = None

    def classify(self, text: str, state: TurnState | None = None, skill: str | None = None) -> IntentDecision:
        state = state or TurnState()
        if skill:  # 命令行显式指定的能力是规则层输入
            return IntentDecision(skill, 1, "用户显式指定能力", 1.0, skill)
        text = text.strip()
        signals: dict = {}
        decision = self._rules(text, state) or self._knowledge(text, state, signals) or \
            self._semantic(text, state, signals) or IntentDecision("agent", 3, "规则与语义层都没有把握，交给助理理解")
        decision.kb_score, decision.semantic = signals.get("kb_score"), signals.get("semantic")
        return decision

    def _rules(self, text: str, state: TurnState) -> IntentDecision | None:
        if not text:
            if state.has_materials:  # 只附材料、没说要做什么：助理按内容判断（会议记录→纪要等），不追问
                return IntentDecision("agent", 1, "只附了材料、没有文字要求，交给助理按内容判断", 1.0)
            return IntentDecision("clarify", 1, "空请求", 1.0)
        if m := _BLOCKED.search(text):
            return IntentDecision("blocked", 1, "要求越过规则或索取内部信息", 1.0, m.group())
        if _GREETING.fullmatch(text) or _ABILITY.search(text):
            return IntentDecision("greeting", 1, "打招呼或询问能力", 1.0, text[:20])
        if m := _REALTIME.search(text):
            return IntentDecision("realtime", 1, "需要实时外部数据", 1.0, m.group())
        if _BARE_REFERENCE.fullmatch(text) and not state.has_previous_reply:
            return IntentDecision("clarify", 1, "只有指代、没有可补全的上文", 1.0, text, CLARIFY_REFERENCE)
        # 选了知识库时可能指库里的资料，交给后续判断
        if not state.has_body and not state.kb_id and (_BARE_TASK.fullmatch(text) or _DEICTIC.search(text)):
            return IntentDecision("clarify", 1, "要处理的对象不明：没有附材料，也没有可调整的回答", 1.0, text[:20],
                                  CLARIFY_NO_BODY)
        if (m := _PROGRESS.search(text)) and not _COMPLEX.search(text):
            return IntentDecision("business_query", 1, "查询自己的业务数据或进度", 1.0, m.group())
        if state.has_previous_reply and not state.has_materials and _REFORMAT.fullmatch(text):
            return IntentDecision("revise", 1, "只要求调整上一轮回答的格式或语言", 1.0, text[:20])
        if m := _WEEKLY.search(text):
            return IntentDecision("weekly", 1, "明确要求起草周报", 1.0, m.group())
        if (m := _FULL_SUMMARY.search(text)) and not _COMPLEX.search(text):
            return IntentDecision("summarize", 1, "明确要求整份总结", 1.0, m.group())
        return None

    def _knowledge(self, text: str, state: TurnState, signals: dict) -> IntentDecision | None:
        """选了知识库、句式是提问或找出处、且库里有相关原文时，走知识问答或文档定位。"""
        if not state.kb_id or self.kb_probe is None:
            return None
        if _COMPLEX.search(text) or _TASK_VERB.search(text) or len(text) > 80 or "\n" in text \
                or re.match(r".{4,30}?[：:]", text) or (state.has_previous_reply and _FOLLOW_UP.match(text)):
            return None
        where, ask = _WHERE.search(text), _ASK.search(text)
        if not (where or ask) or (floor := self._kb_floor()) is None:
            return None
        try:
            score = self.kb_probe(state.kb_id, text)
        except Exception:  # 索引未就绪或检索失败时不影响对话
            return None
        signals["kb_score"] = None if score is None else round(score, 4)
        if score is None or score < floor:
            return None
        intent, cue = ("locate", where.group()) if where else ("knowledge", ask.group())
        return IntentDecision(intent, 2, f"选定知识库中有相关原文（相似度{score:.2f}），句式为{'找出处' if where else '提问'}",
                              round(score, 4), cue)

    def _kb_floor(self) -> float | None:
        """当前模型没有登记下限时跳过知识库路由、交给助理：只看句式会把库里无关的问题也送进知识捷径并外发依据，
        而助理仍可按需检索知识库，代价只是多一次模型判断。"""
        if self.kb_threshold is not None or self._kb_floor_checked:
            return self.kb_threshold
        try:
            identity = getattr(self.encoder_factory(), "identity", None) if self.encoder_factory else None
        except Exception:  # 本地 Embedding 未就绪时下次再取
            return config.INTENT_KB_THRESHOLD
        self._kb_floor_checked = True
        self.kb_threshold = kb_floor(identity)
        if self.kb_threshold is None:
            log.warning(missing_floor_message(identity) if identity else
                        "本地 Embedding 没有可核对的模型身份，无法确定知识库相关度下限，知识库路由已跳过")
        return self.kb_threshold

    def _semantic(self, text: str, state: TurnState, signals: dict) -> IntentDecision | None:
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
            score = _cosine(query, vector)
            if score > best.get(intent, (-1.0, ""))[0]:
                best[intent] = (score, example)
        ranked = sorted(best.items(), key=lambda kv: -kv[1][0])
        if not ranked:
            return None
        intent, (score, example) = ranked[0]
        second = ranked[1][1][0] if len(ranked) > 1 else 0.0
        signals["semantic"] = dict(intent=intent, example=example, score=round(score, 4), gap=round(score - second, 4))
        if score < self.threshold + _STRICTER.get(intent, 0) or score - second < self.margin:
            return None
        reason = f"与示例“{example}”最接近"
        if intent in {"knowledge", "locate", "office"}:
            # 知识问答只由知识库路由判断：没选库或库里没有相关原文时交给助理，不凭示例句走捷径；
            # 通用办公请求没有对应能力，交给助理，不套五项能力的栏目
            return None
        if intent in _NEEDS_PREVIOUS and not state.has_previous_reply:
            # “再短一点”“换成表格”却没有可调整的回答：问清楚，而不是让助理猜；带了正文或材料时交给助理
            if head or state.has_materials or state.earlier_materials:
                return None
            return IntentDecision("clarify", 2, reason + "，但没有可调整的回答", round(score, 4), example,
                                  CLARIFY_NO_PREVIOUS)
        if intent == "polish" and state.has_previous_reply and not state.has_materials and not head and len(text) <= 30:
            # 有上一轮回答、本轮没给新正文的改写要求，是在改上一轮
            return IntentDecision("revise", 2, reason + "，本轮没有新正文，按调整上一轮处理", round(score, 4), example)
        return IntentDecision(intent, 2, reason, round(score, 4), example)

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
CLARIFY_REFERENCE = "没能确定你指的是什么。请说明具体对象，例如“差旅制度里的住宿标准”。"
CLARIFY_NO_BODY = ("还没有可以处理的内容。请把要处理的文字贴在消息里，或用“添加文件 / 链接”“选择材料”附上文件；"
                   "如果想查公司制度，请先在输入框上方选择知识库。")
CLARIFY_NO_PREVIOUS = "目前还没有可以调整的回答。请先告诉我要处理什么，或把文字贴给我。"
DIRECT_REPLIES = {
    # 与助理提示词（base.md“安全与保密”）的约定同一句，模型兜底拦下时说法一致
    "blocked": "这部分内容我无法提供。可以直接说明你要处理的办公任务。",
    "clarify": "请补充你想了解的对象和具体问题，例如“差旅制度里住宿标准是多少”。",
    "greeting": ("你好，我是 Briefly 办公助理，可以帮你：总结文档、整理会议纪要、起草周报、梳理待办、优化文字，"
                 "以及在选定的知识库里查找制度和资料并给出原文依据。直接用一句话告诉我要做什么即可。"),
    "realtime": "目前还不能查询天气、股价、新闻等实时数据。可以处理你提供的材料，或在知识库里查找已入库的资料。",
    "out_of_scope": ("这个请求不在办公助理的范围内。我可以帮你处理文档总结、会议纪要、周报、待办、文字优化和知识库查询，"
                     "也能解答办公软件操作、帮你写邮件和通知。如果和工作有关，可以直接告诉我。"),
}
