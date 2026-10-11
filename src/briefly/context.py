from dataclasses import dataclass


@dataclass
class Context:
    """单次调用的运行时参数，不进入会话记忆。"""

    skill: str | None = None
    material_ids: list[int] | None = None  # 用户本次选择的材料，材料工具只能访问这些
