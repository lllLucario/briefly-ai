"""对话数字提示：复用周报检查，只提醒，不重试或判定业务事实。"""
import re
from decimal import Decimal, InvalidOperation

from .conflicts import value_in
from .date_check import _pairs, review_dates
from .weekly.engine import unverified_numbers


def number_issues(answer: str, sources: list[str], now=None) -> list[str]:
    evidence = "\n".join(sources)
    if now is not None:
        evidence += f"\n{now:%Y-%m-%d} {now:%Y年%m月%d日} {now:%m-%d} {now:%H:%M} {now:%H:%M:%S}"
    # 数字千分位与单位换写是同一数值；不因 1.2 万改写为 12000 而提醒。
    clean = lambda text: re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", text)
    evidence, answer = clean(evidence), clean(answer)
    # 模型有时把执行顺序写在同一行；这些仍是序号，不是新增业务数字。
    answer = re.sub(r"(?<![\d.])\d+[.、)）]\s+", "", answer)
    values = set()
    known = set(unverified_numbers(evidence, ""))
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
    # 先提取再按完整数值比较，避免把 5 当成 25 或 12.5 中已有的数字。
    for number in unverified_numbers(answer, ""):
        if number in known:
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
