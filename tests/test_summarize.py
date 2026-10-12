import pytest
from langchain_core.messages import AIMessage

from briefly import config
from briefly.summarize import Source, WorkItem, extract_items, format_items, synthesize
from briefly.tokens import estimate_tokens
from fakes import FunctionModel, structured


def src(chunk: int, content: str, material_id: int = 1) -> Source:
    return Source(material_id, chunk, "周报素材", content)


def item(category: str, point: str, quote: str) -> dict:
    return {"category": category, "point": point, "quote": quote}


def by_chunk(answers: dict[int, object]):
    """按“第 N 块”决定回复；值为 dict 列表表示工作项，为异常则抛出，为可调用对象则调用。"""

    def respond(messages):
        text = messages[-1].content
        n = next(k for k in answers if f"第 {k} 块" in text)
        answer = answers[n]
        if callable(answer):
            answer = answer()
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, AIMessage):
            return answer
        return structured("Extraction", items=answer)

    return respond


SOURCES = [
    src(1, "周一：完成登录联调，用例 32 条全部通过。"),
    src(2, "周三：定位支付回调超时，原因是同步调用风控接口。"),
    src(3, "整理了需求池，参加站会。"),
]


def test_items_are_extracted_in_source_order_with_tags():
    model = FunctionModel(
        respond=by_chunk(
            {
                2: [item("done", "定位支付回调超时原因", "定位支付回调超时，原因是同步调用风控接口。")],
                1: [item("done", "登录联调完成，32 条用例通过", "完成登录联调，用例 32 条全部通过。")],
                3: [],
            }
        )
    )
    result = extract_items(model, SOURCES, focus="支付和登录")
    assert [(i.source, i.point) for i in result.items] == [
        ("1-1", "登录联调完成，32 条用例通过"),
        ("1-2", "定位支付回调超时原因"),
    ]
    assert result.processed == result.total == 3 and not result.failed
    assert result.usage.calls == 3


def test_fabricated_quotes_are_dropped():
    model = FunctionModel(
        respond=by_chunk({1: [item("done", "登录联调完成", "完成登录联调，用例 50 条全部通过。")], 2: [], 3: []})
    )
    result = extract_items(model, SOURCES)
    assert result.items == []
    assert result.dropped == [
        {"source": "1-1", "point": "登录联调完成", "quote": "完成登录联调，用例 50 条全部通过。", "reason": "引用在原文中找不到"}
    ]


def test_quote_check_ignores_whitespace_and_markdown_escapes():
    sources = [src(1, "示例：\n\\# Role\n你是助理")]
    model = FunctionModel(respond=by_chunk({1: [item("other", "示例提示词", "# Role 你是助理")]}))
    assert len(extract_items(model, sources).items) == 1


def test_failed_chunk_is_retried():
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        return ConnectionError("timeout") if attempts["n"] == 1 else [item("done", "登录联调", "完成登录联调")]

    model = FunctionModel(respond=by_chunk({1: flaky, 2: [], 3: []}))
    result = extract_items(model, SOURCES)
    assert not result.failed and len(result.items) == 1
    assert attempts["n"] == 2


def test_chunk_that_keeps_failing_is_reported_not_dropped_silently(monkeypatch):
    monkeypatch.setattr(config, "MAP_RETRIES", 1)
    model = FunctionModel(respond=by_chunk({1: [], 2: ConnectionError("boom"), 3: []}))
    result = extract_items(model, SOURCES)
    assert result.processed == 2 and result.total == 3
    assert result.failed[0]["source"] == "1-2" and "boom" in result.failed[0]["error"]
    assert sum("第 2 块" in c[-1].content for c in model.calls) == 2  # 首次 + 重试 1 次


def test_unparseable_output_is_retried_then_reported():
    model = FunctionModel(respond=by_chunk({1: AIMessage("我不想用工具"), 2: [], 3: []}))
    result = extract_items(model, SOURCES)
    assert result.failed[0]["source"] == "1-1" and "输出格式不对" in result.failed[0]["error"]


def test_duplicate_quotes_from_overlapping_chunks_are_kept_once():
    sources = [src(1, "甲事项。重叠的一句话。"), src(2, "重叠的一句话。乙事项。")]
    same = [item("done", "重叠事项", "重叠的一句话。")]
    result = extract_items(FunctionModel(respond=by_chunk({1: same, 2: same})), sources)
    assert [i.source for i in result.items] == ["1-1"]


def test_extract_prompt_carries_focus_period_and_wrapped_material():
    model = FunctionModel(respond=by_chunk({1: []}))
    extract_items(model, [SOURCES[0]], focus="支付改造", period="2026-10-12 至 2026-10-18")
    system, user = model.calls[0]
    assert "逐字照抄" in system.content
    assert "简述：支付改造" in user.content and "周期：2026-10-12 至 2026-10-18" in user.content
    assert "【材料开始】\n周一：完成登录联调" in user.content
    model.calls.clear()
    extract_items(model, [SOURCES[0]])
    assert "提取全部工作内容" in model.calls[0][-1].content


ITEMS = [
    WorkItem("done", "登录联调完成", "完成登录联调", "1-1"),
    WorkItem("issue", "支付回调超时", "支付回调超时", "1-2"),
]


def test_format_items_shows_category_source_and_quote():
    assert format_items(ITEMS[:1]) == "- 【已完成】登录联调完成〔1-1〕\n  原文：完成登录联调"


def test_synthesize_in_one_call_when_within_budget():
    model = FunctionModel(respond=lambda messages: AIMessage("【本周完成】\n- 登录联调完成〔1-1〕"))
    result = synthesize(model, "按周报格式输出", "写本周周报", ITEMS)
    assert result.text.startswith("【本周完成】") and result.merge_levels == 0 and result.usage.calls == 1
    system, user = model.calls[0]
    assert system.content == "按周报格式输出"
    assert user.content.startswith("写本周周报") and "〔1-2〕" in user.content


def test_synthesize_merges_in_batches_when_over_budget():
    many = [WorkItem("done", f"第{i}项工作内容比较长一些", f"原文{i}", f"1-{i}") for i in range(40)]

    def respond(messages):
        if "合并" in messages[0].content:  # merge.md
            return AIMessage("- 【已完成】合并后的一条〔1-0〕")
        return AIMessage("最终周报")

    model = FunctionModel(respond=respond)
    result = synthesize(model, "最终格式", "写周报", many, budget=200)
    assert result.text == "最终周报" and result.merge_levels >= 1
    merge_calls = [c for c in model.calls if "合并" in c[0].content]
    assert len(merge_calls) >= 2
    assert "合并后的一条" in model.calls[-1][-1].content


def test_synthesize_with_no_items_says_so():
    model = FunctionModel(respond=lambda messages: AIMessage("无"))
    synthesize(model, "格式", "写周报", [])
    assert "没有提取到相关工作项" in model.calls[0][-1].content


@pytest.mark.parametrize(("text", "expected"), [("中文" * 17, 20), ("abcd" * 10, 10), ("", 0)])
def test_estimate_tokens(text, expected):
    assert estimate_tokens(text) == expected
