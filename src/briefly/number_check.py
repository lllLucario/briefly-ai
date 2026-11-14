"""对话数字提示：复用周报检查，只提醒，不重试或判定业务事实。"""
import re
from decimal import Decimal, InvalidOperation

from .conflicts import value_in
from .date_check import _pairs, review_dates
from .weekly.engine import unverified_numbers

_CITED = re.compile(r"依据\s*\d+(?:\s*[-–—~～至到、]\s*\d+)*")
# 中文日期整体：2026年11月14日、11月14日（号也算）
_NOW_CHINESE_DATE = re.compile(r"(?<![\d.])(?:(\d{4})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")


def _now_forms(now) -> set[str]:
    """当前时间的完整写法，两位数补零和不补零都算。"""
    forms = set()
    for month in {f"{now.month:02}", str(now.month)}:
        for day in {f"{now.day:02}", str(now.day)}:
            for sep in "-/":
                forms.add(f"{month}{sep}{day}")
                forms.add(f"{now.year}{sep}{month}{sep}{day}")
    for hour in {f"{now.hour:02}", str(now.hour)}:
        forms.add(f"{hour}:{now.minute:02}")
        forms.add(f"{hour}:{now.minute:02}:{now.second:02}")
    return forms


def _strip_current_month(m, now, now_forms: set[str]) -> str:
    """回答里的中文日期：就是今天就整个放行；年、月是当前年月时只留下日去核对，月份不单独再报。"""
    if (f"{m[1]}-" if m[1] else "") + f"{m[2]}-{m[3]}" in now_forms:
        return " "
    if (m[1] is None or int(m[1]) == now.year) and int(m[2]) == now.month:
        return f" {m[3]}日"
    return m[0]


def number_issues(answer: str, sources: list[str], now=None) -> list[str]:
    evidence = "\n".join(sources)
    # 数字千分位与单位换写是同一数值；不因 1.2 万改写为 12000 而提醒。
    clean = lambda text: re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", text)
    evidence, answer = clean(evidence), clean(answer)
    # 当前时间只放行完整的时间和日期写法（19:38、2026-11-14、11月14日），不把拆开的 38、14、11 当依据：
    # 否则 19:38 时编造的“年假 38 天”、14 号时的“14 天”都不会提醒（问题登记表第 108 条）
    now_forms = _now_forms(now) if now is not None else set()
    # 模型有时把执行顺序写在同一行；这些仍是序号，不是新增业务数字。
    answer = re.sub(r"(?<![\d.])\d+[.、)）]\s+", "", answer)
    # 依据编号（〔依据3〕、依据1–37、依据1-3、5）是出处标注，不是业务数字；正文里的数字照常核对
    answer = _CITED.sub("依据", answer)
    values = set()
    known = set(unverified_numbers(evidence, ""))
    # 制度、标准常把多级条款编号写在行首（“7.5.3 文件控制”）；周报规则把行首“7.”当列表序号剥掉，这里按原样补回
    known.update(re.findall(r"(?m)^[ \t]*(\d+(?:\.\d+)+)(?![\d.])", evidence))
    verified_dates = []
    if now is not None:
        for year, month, day, _, written, is_label in _pairs(answer):
            if is_label:
                continue  # 星期标注正确只能证明星期，不能证明用户所问的日期
            checked = review_dates(written, sources, now.date())
            if checked.checked and not checked.problems and not checked.uncertain:
                verified_dates.append((f"{year}-" if year else "") + f"{int(month):02}-{int(day):02}")
                values.update(Decimal(v) for v in (year, month, day) if v is not None)
    for m in re.finditer(r"(?<![\d.])(\d+(?:\.\d+)?)\s*([万千亿]?)", evidence):
        values.add(Decimal(m[1]) * {"": 1, "千": 1000, "万": 10000, "亿": 100000000}[m[2]])
    digits = {word: n for n, word in enumerate("零一二三四五六七八九")}
    digits.update({"两": 2, "〇": 0})
    for m in re.finditer(r"([零〇一二两三四五六七八九十]{1,3})\s*([万千亿]?)", evidence):
        word = m[1]
        if "十" in word:
            a, b = word.split("十", 1)
            if len(a) > 1 or len(b) > 1 or "十" in b:
                continue
            n = digits.get(a or "一", 0) * 10 + digits.get(b or "零", 0)
        else:
            n = int("".join(str(digits[ch]) for ch in word))
        values.add(Decimal(n) * {"": 1, "千": 1000, "万": 10000, "亿": 100000000}[m[2]])
    issues = []
    if now is not None:
        answer = _NOW_CHINESE_DATE.sub(lambda m: _strip_current_month(m, now, now_forms), answer)
    compact = re.sub(r"\s+", "", evidence)
    # 先提取再按完整数值比较，避免把 5 当成 25 或 12.5 中已有的数字。
    for number in unverified_numbers(answer, ""):
        if number in known or number in now_forms:
            continue
        # 区间的一端（原文 9:30-10:00，回答换了连接号拆成 9:30 和 10:00）仍是原文数字
        if not re.fullmatch(r"\d+(?:\.\d+)?", number) and re.search(r"(?<![\d.:])" + re.escape(number) + r"(?![\d.:])", compact):
            continue
        # 斜杠并列（原文 20元、50元、80元，回答写 20/50/80）：每一项都在原文数值里
        if re.fullmatch(r"\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?){2,}", number) and all(Decimal(part) in values for part in number.split("/")):
            continue
        date_sources = [*sources, *verified_dates, now.strftime("%Y-%m-%d") if now is not None else ""]
        if re.fullmatch(r"(?:\d{4}[-/])?\d{1,2}[-/]\d{1,2}", number) and any(value_in(text, number) for text in date_sources):
            continue
        try:
            occurrences = list(re.finditer(r"(?<![\d.])" + re.escape(number) + r"(?![\d.])\s*([万千亿]?)", answer))
            if occurrences and all(Decimal(number) * {"": 1, "千": 1000, "万": 10000, "亿": 100000000}[m[1]] in values for m in occurrences):
                continue
        except InvalidOperation:
            pass
        issues.append(number)
    return issues
