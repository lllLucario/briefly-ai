from datetime import date

from langchain_core.tools import tool

_WEEKDAYS = "一二三四五六日"


@tool
def get_current_date() -> str:
    """获取今天的日期和星期。需要把“明天”“下周五”“本周”等相对时间换算成具体日期时必须调用。"""
    today = date.today()
    return f"{today.isoformat()} 星期{_WEEKDAYS[today.weekday()]}"
