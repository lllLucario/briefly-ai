import os

from dotenv import load_dotenv

# 配置在进程启动时读取；修改 .env 后需重启。模型密钥等可选能力的
# 必填项由使用方在运行时校验，避免阻止材料管理等不需要模型的功能。
load_dotenv()

# OpenAI 兼容接口：换服务商只需改这三项
API_KEY = os.getenv("BRIEFLY_API_KEY") or os.getenv("OPENAI_API_KEY", "")
BASE_URL = os.getenv("BRIEFLY_BASE_URL") or os.getenv("OPENAI_BASE_URL") or None
MODEL = os.getenv("BRIEFLY_MODEL", "")

# 思考模式（DeepSeek 用 extra_body 的 thinking 参数）：off 关闭（默认）、on 开启、
# provider 不传该参数（换成不认识这个参数的服务商时用）
THINKING = {"off": False, "on": True, "provider": None}[os.getenv("BRIEFLY_THINKING", "off").lower()]

# 留空则不传该参数，由服务商决定上限
MAX_TOKENS = int(os.getenv("BRIEFLY_MAX_TOKENS", "0")) or None
# 模型单次请求的超时和重试。默认最长输出按 RESERVED_OUTPUT_TOKENS（8000）算，按保守的 30 token/秒约 267 秒，不会误杀长材料和周报；
# 重试只保留 1 次：限流、5xx 值得再试一次，超时再等一轮也有上限（最长约 10 分钟）。重试的是模型请求，不会重跑工具或外发。
MODEL_TIMEOUT_S = float(os.getenv("BRIEFLY_MODEL_TIMEOUT_S", "300"))
MODEL_MAX_RETRIES = int(os.getenv("BRIEFLY_MODEL_MAX_RETRIES", "1"))
# 模型响应缓存：按白名单缓存通过检查的结果（见 llm_cache.py），off 时和没有缓存完全一样
LLM_CACHE = os.getenv("BRIEFLY_LLM_CACHE", "on").lower() != "off"
LLM_CACHE_TTL_HOURS = float(os.getenv("BRIEFLY_LLM_CACHE_TTL_HOURS", "168"))
LLM_CACHE_MAX_ENTRIES = int(os.getenv("BRIEFLY_LLM_CACHE_MAX_ENTRIES", "5000"))
# 单次提问中，模型最多连续调用几轮工具
MAX_TOOL_ROUNDS = int(os.getenv("BRIEFLY_MAX_TOOL_ROUNDS", "8"))
# 会话记忆（LangGraph checkpointer）所在的 SQLite 文件
DB_PATH = os.getenv("BRIEFLY_DB_PATH", "data/briefly.db")

# 业务数据库（材料、周报设置等），与会话记忆分开；可换成 Postgres 地址
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///data/app.db")
# 材料分块（按字符计）
CHUNK_SIZE = int(os.getenv("BRIEFLY_CHUNK_SIZE", "1500"))
CHUNK_OVERLAP = int(os.getenv("BRIEFLY_CHUNK_OVERLAP", "150"))
# 上传文件大小上限（MB）
MAX_UPLOAD_MB = int(os.getenv("BRIEFLY_MAX_UPLOAD_MB", "10"))
# 知识库单个文件的大小上限（MB）。普通材料上传和粘贴仍按 MAX_UPLOAD_MB；知识库接口另有单批 50MB 的限制，实际不会超过它
MAX_KNOWLEDGE_FILE_MB = int(os.getenv("BRIEFLY_MAX_KNOWLEDGE_FILE_MB", "50"))
# 普通请求体（主要是 JSON）的总上限，只防滥用，不是业务限制。默认 32MB：粘贴上限 10MB 的文字经 JSON 转义最多约 20MB，
# 再留余量。实际上限不低于 MAX_UPLOAD_MB 的 3 倍，调大上传上限时不会挡住粘贴。
MAX_JSON_MB = int(os.getenv("BRIEFLY_MAX_JSON_MB", "32"))
# 知识库单次批量上传的请求总量，在框架写临时文件之前生效。100MB 是保守占位，之后还要再改。
# 目前接口里另有单批 50MB 的限制（knowledge/imports_api.py），所以 50～100MB 的批次会被接口拒绝。
MAX_IMPORT_BATCH_MB = int(os.getenv("BRIEFLY_MAX_IMPORT_BATCH_MB", "100"))
# W3知识入库：覆盖排队、解析及DOC回退后Docling的整文件总预算。
PARSE_TIMEOUT_S = max(1, int(os.getenv("BRIEFLY_PARSE_TIMEOUT_S", "120")))
KNOWLEDGE_DIR = os.getenv("BRIEFLY_KNOWLEDGE_DIR", "data/knowledge")
EMBEDDING_DIR = os.getenv("BRIEFLY_EMBEDDING_DIR", "")
# 工作区（材料库）语义索引文件，不填时放在知识库目录下 workspace.sqlite；检索前对齐时最多当场计算几条新向量，其余交给后台
WORKSPACE_INDEX_PATH = os.getenv("BRIEFLY_WORKSPACE_INDEX_PATH", "")
WORKSPACE_INLINE_EMBED = int(os.getenv("BRIEFLY_WORKSPACE_INLINE_EMBED", "64"))
# 知识问答：发给生成模型的依据包总额（按本地 Embedding 分词器计）及整个回答提示的上限（按生成模型计）
KNOWLEDGE_CONTEXT_TOKENS = int(os.getenv("BRIEFLY_KNOWLEDGE_CONTEXT_TOKENS", "8000"))
# 联合检索（选了知识库时同时检索工作区）：上面的总额里工作区片段最多用多少（知识库用不满时余量也给工作区）
WORKSPACE_CONTEXT_TOKENS = int(os.getenv("BRIEFLY_WORKSPACE_CONTEXT_TOKENS", "2000"))
KNOWLEDGE_PROMPT_TOKENS = int(os.getenv("BRIEFLY_KNOWLEDGE_PROMPT_TOKENS", "12000"))
# 助理路径知识问答这一轮的总输入上限（系统提示＋工具定义＋会话记忆＋历史＋本轮依据与问题，按生成模型计）：
# 超出时先少带历史，不砍本轮依据；本轮依据本身放不下时仍按单次输入上限处理
KNOWLEDGE_TURN_TOKENS = int(os.getenv("BRIEFLY_KNOWLEDGE_TURN_TOKENS", "32000"))
# 意图漏斗语义层：与示例句的最低相似度、与第二名意图的最小差距；达不到交给助理判断
INTENT_THRESHOLD = float(os.getenv("BRIEFLY_INTENT_THRESHOLD", "0.64"))
INTENT_MARGIN = float(os.getenv("BRIEFLY_INTENT_MARGIN", "0.05"))
# 知识库相关度下限：选定知识库中最相关片段的语义相似度（本地检索），达到且句式为提问/找出处才走知识捷径。
# 默认按 Embedding 指纹查登记表（relevance_floor.REGISTERED）；这里填了数字则覆盖登记值，用于临时标定
INTENT_KB_THRESHOLD = float(v) if (v := os.getenv("BRIEFLY_INTENT_KB_THRESHOLD", "").strip()) else None
# 跨库差距：另一个知识库的最高相似度比所选库高出这么多（且达到下限）时，判为可能选错了库，
# 不走所选库的单问捷径、交给助理，并提示可切换的库；与下限一起用 intent_eval.py --sweep 标定
INTENT_KB_RIVAL_MARGIN = float(os.getenv("BRIEFLY_INTENT_KB_RIVAL_MARGIN", "0.08"))
# 意图日志（intent_log.py）：每条消息的判断与纠正记在本机业务库，不外发；原文默认不存，只存长度
INTENT_LOG = os.getenv("BRIEFLY_INTENT_LOG", "on").lower() != "off"
INTENT_LOG_TEXT = os.getenv("BRIEFLY_INTENT_LOG_TEXT", "off").lower() == "on"
# 旧版Word本地转换；不配置时查PATH及常见LibreOffice安装位置。
LIBREOFFICE_PATH = os.getenv("BRIEFLY_LIBREOFFICE", "")
DOC_CONVERSION_TIMEOUT_S = max(1, int(os.getenv("BRIEFLY_DOC_CONVERSION_TIMEOUT_S", "90")))
# 转换时复制预先初始化的加固配置模板（每次仍是独立配置目录、禁用宏）；off 则每次从零初始化。
OFFICE_PROFILE_TEMPLATE = os.getenv("BRIEFLY_OFFICE_PROFILE_TEMPLATE", "on").lower() != "off"
# auto：本地Java/Tika可用时直接读doc；convert保留兼容路线，direct强制直接解析。
DOC_PARSER = os.getenv("BRIEFLY_DOC_PARSER", "auto").lower()
# auto：本地Java/Tika可用时直接读ppt，读不了再转换；convert只走LibreOffice转换。
PPT_PARSER = os.getenv("BRIEFLY_PPT_PARSER", "auto").lower()
JAVA_PATH = os.getenv("BRIEFLY_JAVA", "")
TIKA_APP_JAR = os.getenv("BRIEFLY_TIKA_APP_JAR", "")
# 图片文字识别（本地 RapidOCR，需安装 .[ocr]）：auto 已安装就用于扫描页、文档里的图片和图片文件；off 关闭。
OCR = os.getenv("BRIEFLY_OCR", "auto").lower()
# 每份文件最多识别的整页（扫描页、图片文件的帧）和嵌入图片数；超出的部分跳过并在解析报告里写明。
OCR_MAX_PAGES = max(0, int(os.getenv("BRIEFLY_OCR_MAX_PAGES", "40")))
OCR_MAX_IMAGES = max(0, int(os.getenv("BRIEFLY_OCR_MAX_IMAGES", "60")))
# 每份文件识别的总时长（秒）；知识入库时另不超过解析总预算的四分之三，超时后其余图片不再识别。
OCR_TIME_BUDGET_S = max(1.0, float(os.getenv("BRIEFLY_OCR_TIME_BUDGET_S", "80")))
# 一段识别文字的平均置信度低于此值时记为未确认范围（不作为确定依据外发）。
OCR_MIN_CONFIDENCE = float(os.getenv("BRIEFLY_OCR_MIN_CONFIDENCE", "0.85"))
# 推理线程数；解析最多两份并行，默认不占满所有核。
OCR_THREADS = max(1, int(os.getenv("BRIEFLY_OCR_THREADS", "4")))
# 选中材料合计不超过该 token 数（约 6000 个汉字）时，全文直接放进消息；超过则由模型用工具读取
INLINE_MATERIAL_TOKENS = int(os.getenv("BRIEFLY_INLINE_MATERIAL_TOKENS", "3500"))

# ---- 上下文预算（tokens）。每次调用模型前由 context_builder 统一检查，全部只影响发给模型的内容，
# 完整历史仍保存在 checkpointer 中。约束：输入估算 + 预留输出 + 安全余量 <= 模型窗口。
# 模型的上下文窗口，按所用模型填（deepseek-flash 为 1000000）；0 表示未知，只按 MAX_INPUT_TOKENS 控制
CONTEXT_LIMIT = int(os.getenv("BRIEFLY_CONTEXT_LIMIT", "0"))
# 自定的单次输入上限：控制成本、速度和效果（上下文过长时模型易漏掉中间内容），不是窗口上限
MAX_INPUT_TOKENS = int(os.getenv("BRIEFLY_MAX_INPUT_TOKENS", "80000"))
# 给模型输出预留的 tokens；设了 MAX_TOKENS 时取它
RESERVED_OUTPUT_TOKENS = int(os.getenv("BRIEFLY_RESERVED_OUTPUT_TOKENS", "0")) or MAX_TOKENS or 8000
# 计数误差的安全余量
SAFETY_MARGIN_TOKENS = int(os.getenv("BRIEFLY_SAFETY_MARGIN_TOKENS", "4000"))
# 计数方式：auto（OpenAI 模型用 tiktoken，其余按实测系数估算）/ heuristic / tiktoken
TOKENIZER = os.getenv("BRIEFLY_TOKENIZER", "auto")
# 用 API 返回的实际用量校准估算时，放大倍数的上限
TOKEN_SCALE_MAX = float(os.getenv("BRIEFLY_TOKEN_SCALE_MAX", "2.0"))

# 历史窗口：之前各轮的原始消息最多占多少（当前轮另算）
HISTORY_TOKENS = int(os.getenv("BRIEFLY_HISTORY_TOKENS", "40000"))
# 会话记忆（压缩后的早期对话）最多占多少
MEMORY_TOKENS = int(os.getenv("BRIEFLY_MEMORY_TOKENS", "3000"))
# 用户一次输入超过该值时，正文存为材料，消息里只留开头、结尾原文和材料引用
INPUT_INLINE_TOKENS = int(os.getenv("BRIEFLY_INPUT_INLINE_TOKENS", "6000"))
# 之前各轮里超过该值的工具结果、材料全文，在发给模型时换成引用（可重新读取）
STALE_RESULT_TOKENS = int(os.getenv("BRIEFLY_STALE_RESULT_TOKENS", "300"))

# 工具预算：单次工具结果上限、一轮提问中工具结果合计上限（旧名 BRIEFLY_READ_BUDGET_TOKENS）、
# 同时执行的工具数、一轮提问中工具的总耗时上限（秒）
TOOL_RESULT_TOKENS = int(os.getenv("BRIEFLY_TOOL_RESULT_TOKENS", "8000"))
TURN_TOOL_TOKENS = int(os.getenv("BRIEFLY_TURN_TOOL_TOKENS") or os.getenv("BRIEFLY_READ_BUDGET_TOKENS") or "30000")
TOOL_CONCURRENCY = int(os.getenv("BRIEFLY_TOOL_CONCURRENCY", "4"))
TURN_TIME_BUDGET_S = float(os.getenv("BRIEFLY_TURN_TIME_BUDGET_S", "600"))
# 按章节读取时单次最多返回的字数；章节更长时只返回前几块，并提示从哪一块接着读
SECTION_READ_CHARS = int(os.getenv("BRIEFLY_SECTION_READ_CHARS", "3000"))

# 会话压缩：未压缩的历史超过该值时，把最近 COMPACT_KEEP_TURNS 轮之前的对话压进会话记忆；
# 每次交给压缩模型的原文不超过 COMPACT_INPUT_TOKENS，更多时分批
COMPACT_TRIGGER_TOKENS = int(os.getenv("BRIEFLY_COMPACT_TRIGGER_TOKENS", "30000"))
COMPACT_KEEP_TURNS = int(os.getenv("BRIEFLY_COMPACT_KEEP_TURNS", "4"))
COMPACT_INPUT_TOKENS = int(os.getenv("BRIEFLY_COMPACT_INPUT_TOKENS", "12000"))
# 接口仍报上下文超长时，缩小预算重建上下文的重试次数
CONTEXT_RETRIES = int(os.getenv("BRIEFLY_CONTEXT_RETRIES", "1"))

# 后端服务监听地址；默认只在本机访问
HOST = os.getenv("BRIEFLY_HOST", "127.0.0.1")
PORT = int(os.getenv("BRIEFLY_PORT", "8000"))
# 访问令牌：设了就强制登录（网页登录或 Authorization: Bearer）；没设时只能绑定本机地址，不要求登录
ACCESS_TOKEN = os.getenv("BRIEFLY_ACCESS_TOKEN", "").strip()
# 网页登录的有效期（天）
SESSION_DAYS = float(os.getenv("BRIEFLY_SESSION_DAYS", "7"))
# 允许的访问主机名（逗号分隔，不含端口），其他 Host 一律拒绝，防 DNS 重绑定。不管有没有令牌都生效
DEFAULT_ALLOWED_HOSTS = "localhost,127.0.0.1,[::1]"
ALLOWED_HOSTS = [h.strip().lower() for h in (os.getenv("BRIEFLY_ALLOWED_HOSTS") or DEFAULT_ALLOWED_HOSTS).split(",") if h.strip()]
# 日志：每行一条 JSON。级别留空时服务用 INFO、命令行用 WARNING（命令行的输出是给人看的）；
# 只对本项目的日志生效，第三方库最低 WARNING。文件留空时只输出到标准输出（命令行是标准错误）
LOG_LEVEL = os.getenv("BRIEFLY_LOG_LEVEL", "")
LOG_FILE = os.getenv("BRIEFLY_LOG_FILE", "")

# Map-Reduce 提炼：逐块提取的并发数、单块失败后的重试次数、最终合成时工作项的 token 预算（超出则先分批合并）
MAP_CONCURRENCY = int(os.getenv("BRIEFLY_MAP_CONCURRENCY", "4"))
MAP_RETRIES = int(os.getenv("BRIEFLY_MAP_RETRIES", "1"))
REDUCE_BUDGET_TOKENS = int(os.getenv("BRIEFLY_REDUCE_BUDGET_TOKENS", "30000"))

# ---- 文件追踪（File Tracking）
# 原始文件版本的存放目录（按 SHA-256 内容寻址）
TRACKING_DIR = os.getenv("BRIEFLY_TRACKING_DIR", "data/tracking")
# 单个文件超过该大小不采集快照（MB）；版本库总量超过上限时不再保存新版本（MB）
TRACKING_MAX_FILE_MB = float(os.getenv("BRIEFLY_TRACKING_MAX_FILE_MB", "50"))
TRACKING_MAX_STORE_MB = float(os.getenv("BRIEFLY_TRACKING_MAX_STORE_MB", "2048"))
# mtime 距现在不足该秒数时视为可能正在写入，本次跳过
TRACKING_SETTLE_SECONDS = float(os.getenv("BRIEFLY_TRACKING_SETTLE_SECONDS", "2"))
# 扫描锁的租约时长（秒）：持有者崩溃后超过该时长，其他进程可接手
TRACKING_LOCK_SECONDS = int(os.getenv("BRIEFLY_TRACKING_LOCK_SECONDS", "900"))
# 每日扫描：时间（HH:MM）、时区（留空用系统时区）、只在工作日（周一至周五）
TRACKING_DAILY_TIME = os.getenv("BRIEFLY_TRACKING_DAILY_TIME", "18:00")
TRACKING_TIMEZONE = os.getenv("BRIEFLY_TRACKING_TIMEZONE", "")
TRACKING_WORKDAYS_ONLY = os.getenv("BRIEFLY_TRACKING_WORKDAYS_ONLY", "1") != "0"
# 每日扫描失败后的重试次数、重试间隔（分钟）
TRACKING_SCAN_RETRIES = int(os.getenv("BRIEFLY_TRACKING_SCAN_RETRIES", "2"))
TRACKING_RETRY_MINUTES = int(os.getenv("BRIEFLY_TRACKING_RETRY_MINUTES", "10"))
# API 进程内是否启动每日扫描线程
TRACKING_SCHEDULER = os.getenv("BRIEFLY_TRACKING_SCHEDULER", "1") != "0"
# 生成候选证据时，每批交给模型的 Diff 上限（tokens）
TRACKING_DIFF_BATCH_TOKENS = int(os.getenv("BRIEFLY_TRACKING_DIFF_BATCH_TOKENS", "3000"))
# 单个文件一次 Diff 最多保留的变化段数，超出部分只计数（避免超大 Diff 撑爆存储和上下文）
TRACKING_MAX_HUNKS = int(os.getenv("BRIEFLY_TRACKING_MAX_HUNKS", "400"))

# ---- 飞书（企业自建应用，以应用身份访问：文档需要把应用添加为协作者）
FEISHU_APP_ID = os.getenv("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.getenv("FEISHU_APP_SECRET", "")
# 国际版 Lark 用 https://open.larksuite.com
FEISHU_BASE_URL = os.getenv("FEISHU_BASE_URL", "https://open.feishu.cn")
# 打开文档时用的域名（只影响生成的链接），如 https://xxx.feishu.cn；留空用 https://feishu.cn
FEISHU_DOC_DOMAIN = os.getenv("FEISHU_DOC_DOMAIN", "")
# 写入飞书文档时放在哪个文件夹（应用需有该文件夹的编辑权限）；留空放在应用自己的空间
FEISHU_FOLDER_TOKEN = os.getenv("FEISHU_FOLDER_TOKEN", "")
# 你自己的飞书账号：open_id（ou_ 开头）或飞书邮箱，普通邮箱（QQ、Gmail）飞书找不到。写入的文档自动加你为协作者、机器人提醒发给你
FEISHU_NOTIFY_USER = os.getenv("FEISHU_NOTIFY_USER", "")
# 用户授权（OAuth）：回调地址要与开发者后台“安全设置 → 重定向 URL”完全一致；留空用 http://HOST:PORT/feishu/oauth/callback
FEISHU_REDIRECT_URI = os.getenv("FEISHU_REDIRECT_URI", "")
# 申请的用户权限（空格分隔）；每一项都要先在后台“权限管理”里开通对应的用户身份权限
FEISHU_OAUTH_SCOPES = os.getenv(
    "FEISHU_OAUTH_SCOPES",
    "offline_access drive:drive:readonly docx:document docx:document:readonly wiki:wiki:readonly "
    "bitable:app:readonly search:docs:read minutes:minutes.basic:read minutes:minutes.transcript:export",
)
# 用户令牌存放位置（本机文件，不进 Git）
FEISHU_USER_TOKEN_FILE = os.getenv("FEISHU_USER_TOKEN_FILE", "data/feishu_user_token.json")

# ---- 邮件（发送周报，SMTP）：465 用 SSL，587 等用 STARTTLS；QQ、163 等邮箱的密码填“授权码”
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
# 发件人显示名、网页上默认填的收件人（逗号分隔，如导师邮箱）
MAIL_FROM_NAME = os.getenv("MAIL_FROM_NAME", "")
MAIL_DEFAULT_TO = os.getenv("MAIL_DEFAULT_TO", "")
