import os

from dotenv import load_dotenv

load_dotenv()

# OpenAI 兼容接口：换服务商只需改这三项
API_KEY = os.getenv("BRIEFLY_API_KEY") or os.getenv("OPENAI_API_KEY", "")
BASE_URL = os.getenv("BRIEFLY_BASE_URL") or os.getenv("OPENAI_BASE_URL") or None
MODEL = os.getenv("BRIEFLY_MODEL", "")

# 留空则不传该参数，由服务商决定上限
MAX_TOKENS = int(os.getenv("BRIEFLY_MAX_TOKENS", "0")) or None
# 会话历史最多保留的消息条数，超出时从最早的完整轮次开始丢弃
MAX_HISTORY = int(os.getenv("BRIEFLY_MAX_HISTORY", "40"))
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
# 选中材料合计不超过该字数时，全文直接放进消息；超过则由模型用工具读取
INLINE_MATERIAL_CHARS = int(os.getenv("BRIEFLY_INLINE_MATERIAL_CHARS", "6000"))
# 按章节读取时单次最多返回的字数；章节更长时只返回前几块，并提示从哪一块接着读
SECTION_READ_CHARS = int(os.getenv("BRIEFLY_SECTION_READ_CHARS", "3000"))

# 后端服务监听地址；默认只在本机访问
HOST = os.getenv("BRIEFLY_HOST", "127.0.0.1")
PORT = int(os.getenv("BRIEFLY_PORT", "8000"))
