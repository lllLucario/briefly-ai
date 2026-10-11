from datetime import date

_WEEKDAYS = "一二三四五六日"


def get_current_date() -> str:
    today = date.today()
    return f"{today.isoformat()} 星期{_WEEKDAYS[today.weekday()]}"
