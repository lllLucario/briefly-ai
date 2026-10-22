from langchain_core.tools import BaseTool

from .builtin import get_current_date, resolve_date

# 新增工具（飞书文档、多维表格、邮件等）在这里登记
DEFAULT_TOOLS: list[BaseTool] = [get_current_date, resolve_date]

__all__ = ["DEFAULT_TOOLS"]
