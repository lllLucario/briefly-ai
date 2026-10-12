import os

from dotenv import load_dotenv

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
