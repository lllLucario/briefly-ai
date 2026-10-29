from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Context:
    """单次调用的运行时参数，不进入会话记忆。"""

    skill: str | None = None
    material_ids: list[int] | None = None  # 用户本次选择的材料，材料工具只能访问这些
    thread_id: str = ""
    run_id: str = ""  # 一次 chat 调用的编号，观测数据按它归组
    compacted: bool = False  # 本次 chat 前是否刚做过会话压缩（观测用）
    reply_limit: int | None = None  # 从原始对话要求计算，本轮所有输出检查使用同一上限
    now: datetime | None = None  # 本次请求开始时的当前时间（带时区）：提示词、日期工具、日期校验共用
    output_check: dict = field(default_factory=dict)  # 仅当前请求：跨工具轮次保留一次重写标记与纠正说明
    kb_id: str | None = None  # 用户本次选定的知识库，知识检索工具只能查这一个
    intent: dict | None = None  # 意图漏斗的判断（见 intent.py），只用于选择能力提示，不是事实依据
