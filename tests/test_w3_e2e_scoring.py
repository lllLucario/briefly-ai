"""端到端问答评测判分规则（tests/eval/w3_e2e_scoring.py）；不调用模型。"""
from tests.eval.w3_e2e_scoring import abstained, normalize, retrieval_hit, score, summarize

TITLES = {"D1": "差旅与费用报销管理办法.docx", "D6": "云笺协同办公平台用户手册.pdf"}
CORPUS = ["一类城市经理及以上每晚750元，普通员工550元。", "专业版单文件上限 2GB，回收站保留30 天。"]
EVIDENCE = [dict(title=TITLES["D1"], content="| 一类 | 北京、上海 | 550 | 750 |")]


def case(**kw):
    base = dict(id="R01", category="relevance", question="上海经理住宿每晚多少？", must=[["750"]], abstain=False,
                gold=[{"doc": "D1", "text": "750"}], max_chars=None)
    return {**base, **kw}


def test_normalize_ignores_width_spaces_and_thousands():
    assert normalize("３０ 天") == normalize("30天")
    assert normalize("1,200 元") == "1200元"


def test_correct_answer_passes_and_citations_do_not_count_as_numbers():
    result = score(case(), "【可答性：可回答】上海属一类城市，经理每晚750元〔依据1〕。", "answered", EVIDENCE, TITLES, CORPUS)
    assert result["passed"] and result["problems"] == [] and result["retrieval_hit"] is True


def test_missing_point_is_off_target_and_invented_number_is_hallucination():
    result = score(case(), "经理每晚650元。", "answered", EVIDENCE, TITLES, CORPUS)
    assert not result["passed"]
    assert result["problems"] == ["off_target", "hallucination"] and result["unsupported_numbers"] == ["650"]


def test_abstain_case_requires_saying_not_found():
    no = case(id="H01", category="hallucination", must=[], gold=[], abstain=True, question="境外住宿标准？")
    assert score(no, "【可答性：依据不足】资料未提及境外住宿标准。", "insufficient_evidence", [], TITLES, CORPUS)["passed"]
    assert score(no, "知识库里没有找到相关内容。", "low_relevance", [], TITLES, CORPUS)["passed"]
    made_up = score(no, "境外住宿每晚800元。", "answered", [], TITLES, CORPUS)
    assert made_up["problems"] == ["hallucination"] and not made_up["passed"]


def test_concise_case_flags_long_answers():
    short = case(id="C01", category="concise", max_chars=20)
    assert score(short, "每晚750元。", "answered", EVIDENCE, TITLES, CORPUS)["passed"]
    long = score(short, "根据差旅办法第七条，一类城市经理及以上人员住宿每晚750元，超标部分自理。", "answered", EVIDENCE, TITLES, CORPUS)
    assert long["problems"] == ["verbose"]


def test_retrieval_miss_alone_does_not_fail_a_correct_answer():
    other = [dict(title=TITLES["D6"], content="专业版 2GB")]
    result = score(case(), "经理每晚750元。", "answered", other, TITLES, CORPUS)
    assert result["passed"] and result["problems"] == ["retrieval_miss"]
    assert retrieval_hit([], other, TITLES) is None


def test_abstain_detection_and_summary():
    assert abstained("资料中没有明确规定。", "answered") and not abstained("每晚750元。", "answered")
    rows = [dict(passed=True, problems=[], retrieval_hit=True, length=10, category="relevance", doc_format="docx"),
            dict(passed=False, problems=["verbose"], retrieval_hit=None, length=30, category="concise", doc_format="pdf")]
    summary = summarize(rows)
    assert summary["all"]["pass_rate"] == 0.5 and summary["all"]["retrieval_hit"] == 1.0
    assert summary["category=concise"]["problems"]["verbose"] == 1
