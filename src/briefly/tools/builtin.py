import calendar
import re
from datetime import date, timedelta

from langchain_core.tools import tool

_WEEKDAYS = "一二三四五六日"
_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_DAY_WORDS = {"今天": 0, "今日": 0, "明天": 1, "明日": 1, "后天": 2, "大后天": 3, "昨天": -1, "前天": -2}
_WEEK_PREFIX = {"上上": -2, "上": -1, "本": 0, "这": 0, "这个": 0, "下": 1, "下个": 1, "下下": 2}


def _weekday_name(d: date) -> str:
    return f"星期{_WEEKDAYS[d.weekday()]}"


def _number(text: str) -> int | None:
    if text.isdigit():
        return int(text)
    if text in _CN_NUM:
        return _CN_NUM[text]
    if len(text) == 2 and text[0] == "十":
        return 10 + _CN_NUM.get(text[1], 0)
    return None


def parse_base_date(text: str, today: date) -> date | None:
    """基准日期：2026-10-14、2026/10/14、2026年10月14日、10月14日、10-14（没写年份按今天所在年份）。"""
    text = re.sub(r"\s+", "", text)
    m = re.fullmatch(r"(?:(\d{4})[-/年.])?(\d{1,2})[-/月.](\d{1,2})日?(?:号)?", text)
    if not m:
        return None
    try:
        return date(int(m.group(1) or today.year), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def relative_date(expression: str, base: date) -> date | None:
    """常见中文相对时间 → 日期；认不出时返回 None（由调用方保留原话，不猜）。一周从周一开始。"""
    raw = re.sub(r"\s+", "", expression)
    m = re.fullmatch(r"(\d+|[一二两三四五六七八九十]{1,2})(天|日|周|个星期|星期)(后|之后|以后|前|之前|以前|内|之内|以内)", raw)
    if m and (n := _number(m.group(1))) is not None:  # “3天后”“两周内”（期限按最后一天）、“3天前”
        days = n * (7 if m.group(2) in ("周", "个星期", "星期") else 1)
        return base + timedelta(days=-days if m.group(3) in ("前", "之前", "以前") else days)
    text = re.sub(r"(之前|以前|前|之内|以内|内)$", "", raw)  # “下周五前”“月底前”：截止说法按那一天
    if text in _DAY_WORDS:
        return base + timedelta(days=_DAY_WORDS[text])
    m = re.fullmatch(r"(上上|上|本|这个|这|下个|下下|下)?(?:周|星期|礼拜)([一二三四五六日天])", text)
    if m:
        weeks = _WEEK_PREFIX.get(m.group(1) or "本", 0)
        index = 6 if m.group(2) in "日天" else _WEEKDAYS.index(m.group(2))
        return base - timedelta(days=base.weekday()) + timedelta(weeks=weeks, days=index)
    m = re.fullmatch(r"(本|这个|下个|下)?月(底|末)", text)
    if m:
        year, month = base.year, base.month + (1 if m.group(1) in ("下个", "下") else 0)
        if month > 12:
            year, month = year + 1, 1
        return date(year, month, calendar.monthrange(year, month)[1])
    m = re.fullmatch(r"(本月|这个月|下个月|下月)?(\d{1,2})[号日]", text)
    if m:
        year, month = base.year, base.month + (1 if m.group(1) in ("下个月", "下月") else 0)
        if month > 12:
            year, month = year + 1, 1
        try:
            return date(year, month, int(m.group(2)))
        except ValueError:
            return None
    return None


@tool
def get_current_date() -> str:
    """获取今天的日期和星期。要把“下周五”等相对时间换算成具体日期，请用 resolve_date，由程序计算。"""
    today = date.today()
    return f"{today.isoformat()} {_weekday_name(today)}"


@tool
def resolve_date(expression: str, base_date: str = "") -> str:
    """把相对时间（明天、后天、下周一、本周五、上周三、3天后、两周后、月底、下个月5号等）换算成具体日期。由程序计算，不要心算星期。

    base_date：换算的基准日期，照抄材料原文。材料里的相对时间以材料中的日期为基准（如会议日期“10月14日”）；
    原文没写年份就不要补年份（程序按今年计算），不要自己推测年份。材料没有日期、或用户说的是相对今天的时间时留空，按今天计算。
    一周从周一开始。
    """
    today = date.today()
    base = parse_base_date(base_date, today) if base_date.strip() else today
    if base is None:
        return f"无法识别基准日期“{base_date}”，请写成 2026-10-14 或 10月14日 的形式；无法确定时在回答中保留原话。"
    result = relative_date(expression, base)
    label = f"{base.isoformat()} {_weekday_name(base)}" + ("（今天）" if not base_date.strip() else "")
    if result is None:
        return f"无法换算“{expression}”（基准日期 {label}）：请在回答中保留原话，不要自行推算。"
    note = ""
    if base_date.strip() and base.year != today.year:
        note += f"；基准日期的年份 {base.year} 不是今年，请确认材料原文确实写了这个年份，原文没写年份时只传月和日"
    if re.fullmatch(r"(?:周|星期|礼拜)[一二三四五六日天]", re.sub(r"\s+", "", expression)) and result < base:
        note += "；注意：按“本周”理解这一天早于基准日期，原意可能是下周，请保留原话或向用户确认"
    return f"“{expression}” = {result.isoformat()} {_weekday_name(result)}（基准日期 {label}{note}）"
