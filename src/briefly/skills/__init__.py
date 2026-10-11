from .base import Skill, read_prompt

# 注册表：新增能力 = 加一个 prompt 文件 + 在这里登记一行
SKILLS: dict[str, Skill] = {
    s.name: s
    for s in [
        Skill("summarize", "文档总结"),
        Skill("minutes", "会议纪要拆解"),
        Skill("weekly", "周报生成"),
        Skill("todo", "待办事项梳理"),
        Skill("polish", "文本优化"),
    ]
}

__all__ = ["Skill", "SKILLS", "read_prompt"]
