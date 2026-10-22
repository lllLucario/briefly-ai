"""回答里“具体日期 + 相对说法”的换算核对（不调用模型）。

模型有时不调用 resolve_date、凭心算把“下周一”写成错误日期，提示词只能降低概率。这里对回答中
成对出现的写法（“10-21（下周三）”“下周三（10-21）”）用同一套日期计算核对：基准取今天，以及用户输入和
材料里出现过的日期，任一基准能对上就算正确。对不上的交给回答检查重写一次，仍不对时提醒用户核对。
只核对能确定计算的说法；“提测后两天”这类相对事件的说法不核对。
"""
import re
from datetime import date, timedelta

from .tools.builtin import relative_date

_DATE = r"(?:(\d{4})[-/年.])?(\d{1,2})[-/月.](\d{1,2})日?"
_RELATIVE = re.compile(
    r"(?:上上|上|本|这个|这|下个|下下|下)?(?:周|星期|礼拜)[一二三四五六日天]"
    r"|大后天|后天|明天|今天|昨天|前天"
    r"|(?:本|这个|下个|下)?月(?:底|末)"
    r"|(?:本月|这个月|下个月|下月)\d{1,2}[号日]"
    r"|(?:\d+|[一二两三四五六七八九十]{1,2})(?:天|周|个星期)(?:后|之后|以后|内|之内|以内)"
)
# 日期后面括号里写相对说法，或相对说法（可带“前”）后面括号里写日期
_DATE_THEN_REL = re.compile(rf"(?<!\d){_DATE}\s*[（(]([^）)\n]{{0,40}})[）)]")
_REL_THEN_DATE = re.compile(rf"({_RELATIVE.pattern})(?:之前|以前|前)?\s*[（(]([^）)\n]{{0,12}}?)(?<!\d){_DATE}")
# 括号里先说明基准（“会议日期10月14日”“按今天10-22”），这个日期不是换算结果
_BASE_WORDS = re.compile(r"会议|按|基准|今天|日期|记录")


def _dates_in(text: str, today: date) -> list[tuple[int, date]]:
    out = []
    for m in re.finditer(rf"(?<!\d){_DATE}(?!\d)", text or ""):
        try:
            out.append((m.start(), date(int(m.group(1) or today.year), int(m.group(2)), int(m.group(3)))))
        except ValueError:
            continue
    return out


def _bases(expression: str, sources: list[str], today: date) -> list[date]:
    """换算基准：原文里出现过这个说法时，取原文的第一个日期（通常是会议或记录日期）和它前面最近的日期
    （分段记录里的当天日期）；那段原文没有日期时取今天。原文里没有这个说法时，今天和原文中的所有日期都算可能的基准。"""
    found: set[date] = set()
    for text in sources:
        if not text or expression not in text:
            continue
        dates = _dates_in(text, today)
        if not dates:
            found.add(today)
            continue
        found.add(dates[0][1])
        for occurrence in re.finditer(re.escape(expression), text):
            before = [d for pos, d in dates if pos < occurrence.start()]
            if before:
                found.add(before[-1])
    if found:
        return sorted(found)
    return sorted({today, *(d for text in sources for _, d in _dates_in(text, today))})


def _matches(year: str | None, month: int, day: int, expression: str, bases: list[date]) -> bool:
    for base in bases:
        expected = [relative_date(expression, base)]
        if re.fullmatch(r"(?:周|星期|礼拜)[一二三四五六日天]", expression) and expected[0]:
            expected.append(expected[0] + timedelta(days=7))  # 只写“周一”：本周或下周都说得通
        for d in expected:
            if d and (d.month, d.day) == (month, day) and (not year or int(year) == d.year):
                return True
    return False


def date_problems(answer: str, sources: list[str], today: date | None = None) -> list[str]:
    """回答中换算不对的“日期 + 相对说法”，如 ['“下周三”写成了 10-22']。"""
    today = today or date.today()
    problems: list[str] = []
    pairs = []
    for m in _DATE_THEN_REL.finditer(answer):
        rel = _RELATIVE.search(m.group(4))
        if rel:
            pairs.append((m.group(1), m.group(2), m.group(3), rel.group(0), m.group(0)))
    for m in _REL_THEN_DATE.finditer(answer):
        if not _BASE_WORDS.search(m.group(2)):
            pairs.append((m.group(3), m.group(4), m.group(5), m.group(1), m.group(0)))
    for year, month, day, expression, written in pairs:
        try:
            ok = _matches(year, int(month), int(day), expression, _bases(expression, sources, today))
        except ValueError:
            continue
        if not ok:
            problem = f"“{expression}”写成了 {written.strip()}"
            if problem not in problems:
                problems.append(problem)
    return problems
