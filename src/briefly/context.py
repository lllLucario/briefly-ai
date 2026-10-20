from dataclasses import dataclass


@dataclass
class Context:
    """单次调用的运行时参数，不进入会话记忆。"""

    skill: str | None = None
    material_ids: list[int] | None = None  # 用户本次选择的材料，材料工具只能访问这些
    thread_id: str = ""
    run_id: str = ""  # 一次 chat 调用的编号，观测数据按它归组
    compacted: bool = False  # 本次 chat 前是否刚做过会话压缩（观测用）
    reply_limit: int | None = None  # 从原始对话要求计算，本轮所有输出检查使用同一上限
