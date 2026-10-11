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
MAX_TOOL_ROUNDS = int(os.getenv("BRIEFLY_MAX_TOOL_ROUNDS", "5"))
