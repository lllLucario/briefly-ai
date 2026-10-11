from dataclasses import dataclass
from typing import Callable

from .builtin import get_current_date


@dataclass(frozen=True)
class Tool:
    """模型可调用的工具。description 决定模型何时调用，要写清楚。"""

    name: str
    description: str
    parameters: dict  # JSON Schema
    func: Callable[..., str]

    def spec(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


# 新增工具（飞书文档、多维表格、邮件等）在这里登记
DEFAULT_TOOLS: list[Tool] = [
    Tool(
        name="get_current_date",
        description="获取今天的日期和星期。需要把“明天”“下周五”“本周”等相对时间换算成具体日期时必须调用。",
        parameters={"type": "object", "properties": {}},
        func=get_current_date,
    ),
]

__all__ = ["Tool", "DEFAULT_TOOLS"]
