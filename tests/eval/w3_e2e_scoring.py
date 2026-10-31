"""端到端问答评测的判分规则：只做字符串与数字核对，不调用模型。

每题按四类失败分别判断，一题可以同时出现多种问题：
- retrieval_miss：按原问题检索到的依据里缺少 gold 原文（检索失败）；
- off_target：应答却没答到 must 要点（答非所问；检索已命中时更能说明是回答的问题）；
- hallucination：应拒答却给了答案，或回答里出现资料和问题都没有的数字；
- verbose：concise 类超过 max_chars（内容冗余）。
"""
import re
import unicodedata

from briefly.number_check import number_issues

# 判为“说明资料没有”的写法；知识问答的【可答性：依据不足】和低相关回复也算
ABSTAIN = re.compile(
    r"可答性[：:]\s*依据不足|未提及|没有提及|未找到|没有找到|未涉及|没有涉及|未规定|没有规定|未明确|没有明确|"
    r"未说明|没有说明|无相关|没有相关|未包含|不包含|无法确定|无法回答|依据不足|未覆盖|没有覆盖")
CITATION = re.compile(r"〔依据\d+〕|\[依据\d+\]|【可答性[：:][^】]*】")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"(?<=\d),(?=\d{3})", "", text)
    return re.sub(r"\s+", "", text).lower()


def contains(text: str, needle: str) -> bool:
    return normalize(needle) in normalize(text)


def abstained(answer: str, status: str | None) -> bool:
    return status == "low_relevance" or bool(ABSTAIN.search(answer or ""))


def retrieval_hit(gold: list[dict], evidence: list[dict], titles: dict[str, str]) -> bool | None:
    """gold 每条都要在同一资料的某个依据片段里出现；没有 gold 的题记 None。"""
    if not gold:
        return None
    return all(any(e["title"] == titles[g["doc"]] and contains(e["content"], g["text"]) for e in evidence) for g in gold)


def score(case: dict, answer: str, status: str | None, evidence: list[dict], titles: dict[str, str],
          corpus: list[str]) -> dict:
    """evidence：[{title, content}]；corpus：全部资料原文，用来核对回答里的数字是否有出处。"""
    body = CITATION.sub("", answer or "")
    must = [any(contains(body, alt) for alt in group) for group in case["must"]]
    did_abstain = abstained(answer, status)
    hit = retrieval_hit(case["gold"], evidence, titles)
    numbers = number_issues(body, [*corpus, case["question"]])
    problems = []
    if hit is False:
        problems.append("retrieval_miss")
    if case["abstain"]:
        if not did_abstain:
            problems.append("hallucination")
    elif not all(must):
        problems.append("off_target")
    if numbers and "hallucination" not in problems:
        problems.append("hallucination")
    length = len(body.strip())
    if case.get("max_chars") and length > case["max_chars"]:
        problems.append("verbose")
    # 检索没命中但回答答对了（例如助理改写检索词后找到）不算失败；其余问题都算
    passed = not [p for p in problems if p != "retrieval_miss"]
    return dict(passed=passed, problems=problems, must=must, abstained=did_abstain, retrieval_hit=hit,
                unsupported_numbers=numbers, length=length)


def summarize(rows: list[dict]) -> dict:
    """rows：score 结果加 category、split、doc_format。按类别与总体汇总比例。"""
    def rate(items, key):
        values = [r[key] for r in items if r[key] is not None]
        return round(sum(values) / len(values), 3) if values else None

    def block(items):
        return dict(runs=len(items), pass_rate=rate(items, "passed"), retrieval_hit=rate(items, "retrieval_hit"),
                    problems={p: sum(p in r["problems"] for r in items)
                              for p in ("retrieval_miss", "off_target", "hallucination", "verbose")},
                    median_length=sorted(r["length"] for r in items)[len(items) // 2] if items else None)

    groups = {"all": rows}
    for key in ("category", "doc_format"):
        for value in sorted({r[key] for r in rows}):
            groups[f"{key}={value}"] = [r for r in rows if r[key] == value]
    return {name: block(items) for name, items in groups.items()}
