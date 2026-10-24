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
