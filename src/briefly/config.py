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

# 上下文管理（token 按 tokens.py 的中文实测系数估算）。deepseek-flash 窗口有 100 万 tokens，
# 这里是自定的工作预算，用来控制成本、速度和效果（上下文过长时模型易漏掉中间内容），不是窗口上限。
# 发给模型的消息超过该值时，清理较早的工具结果（材料原文等），只留最近 3 次；保存的历史不受影响
CONTEXT_CLEAR_TOKENS = int(os.getenv("BRIEFLY_CONTEXT_CLEAR_TOKENS", "40000"))
# 清理后仍超过该值时，从最早的对话开始裁剪（同样只影响发给模型的内容）
HISTORY_TOKENS = int(os.getenv("BRIEFLY_HISTORY_TOKENS", "60000"))
# 一轮提问中，材料工具已返回超过该值的原文后，再读原文改为提示改用 summarize_materials 或检索
READ_BUDGET_TOKENS = int(os.getenv("BRIEFLY_READ_BUDGET_TOKENS", "30000"))
# 按章节读取时单次最多返回的字数；章节更长时只返回前几块，并提示从哪一块接着读
SECTION_READ_CHARS = int(os.getenv("BRIEFLY_SECTION_READ_CHARS", "3000"))

# 后端服务监听地址；默认只在本机访问
HOST = os.getenv("BRIEFLY_HOST", "127.0.0.1")
PORT = int(os.getenv("BRIEFLY_PORT", "8000"))

# Map-Reduce 提炼：逐块提取的并发数、单块失败后的重试次数、最终合成时工作项的 token 预算（超出则先分批合并）
MAP_CONCURRENCY = int(os.getenv("BRIEFLY_MAP_CONCURRENCY", "4"))
MAP_RETRIES = int(os.getenv("BRIEFLY_MAP_RETRIES", "1"))
REDUCE_BUDGET_TOKENS = int(os.getenv("BRIEFLY_REDUCE_BUDGET_TOKENS", "30000"))
