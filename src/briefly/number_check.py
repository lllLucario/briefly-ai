"""对话数字提示：复用周报检查，只提醒，不重试或判定业务事实。"""
import re
from decimal import Decimal, InvalidOperation

from .conflicts import value_in
from .weekly.engine import unverified_numbers


def number_issues(answer: str, sources: list[str], now=None) -> list[str]:
    evidence = "\n".join(sources)
    if now is not None:
        evidence += f"\n{now:%Y-%m-%d} {now:%Y年%m月%d日} {now:%m-%d} {now:%H:%M}"
    # 数字千分位与单位换写是同一数值；不因 1.2 万改写为 12000 而提醒。
    clean = lambda text: re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", text)
    evidence, answer = clean(evidence), clean(answer)
    values = set()
    for m in re.finditer(r"(?<![\d.])(\d+(?:\.\d+)?)\s*([万千亿]?)", evidence):
        values.add(Decimal(m[1]) * {"": 1, "千": 1000, "万": 10000, "亿": 100000000}[m[2]])
    for n, word in enumerate("零一二三四五六七八九十"):
        if word in evidence:
            values.add(Decimal(n))
    issues = []
    for number in unverified_numbers(answer, evidence):
        if value_in(evidence, number):  # 日期换写，保留明确年份边界
            continue
        try:
            if Decimal(number) in values:
                continue
        except InvalidOperation:
            pass
        issues.append(number)
    return issues
