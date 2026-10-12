from briefly.layout_cleaner import Line, clean_pages, garbled_ratio, join_text, merge_wrapped, scanned_pages


def page(index: int, *texts: str, size: float = 10.0) -> list[Line]:
    """从上到下排列的一页文字。"""
    return [Line(t, size, 800 - 20 * i, index) for i, t in enumerate(texts)]


def texts(pages: list[list[Line]]) -> list[list[str]]:
    return [[l.text for l in p] for p in pages]


# ---- 乱码 ----


def test_garbled_lines_are_removed():
    watermark = "\udc80\udc9c\x7fQ\x00 \x00w\x00w\x00w\x00.\x00g\x00u"
    cleaned, removed = clean_pages([page(0, watermark, "正常的正文内容")])
    assert texts(cleaned) == [["正常的正文内容"]]
    assert removed == [("乱码", watermark)]


def test_stray_garbage_chars_are_stripped_but_line_kept():
    assert garbled_ratio("正常文字很多很多\x00") < 0.3
    cleaned, _ = clean_pages([page(0, "正常文字很多很多\x00")])
    assert texts(cleaned) == [["正常文字很多很多"]]


def test_ligatures_are_normalized():
    cleaned, _ = clean_pages([page(0, "the ﬁrst ﬂoor")])
    assert texts(cleaned) == [["the first floor"]]


# ---- 页眉页脚、水印、页码 ----


def test_repeated_header_and_footer_with_changing_page_numbers_are_removed():
    bodies = ["甲的正文", "乙的正文", "丙的正文", "丁的正文", "戊的正文"]
    pages = [page(i, "提示词工程", bodies[i], f"版权所有 {i + 1}/5") for i in range(5)]
    cleaned, removed = clean_pages(pages)
    assert texts(cleaned) == [[b] for b in bodies]
    assert len(removed) == 10


def test_repeated_phrase_in_middle_of_page_is_kept():
    # “说明”出现在一半页面的正文中间，不应被当成页眉页脚删掉
    pages = [page(i, "页眉", "正文开头", "说明" if i % 2 else "别的内容", "正文继续", "正文结尾", "页脚") for i in range(6)]
    cleaned, _ = clean_pages(pages)
    assert sum(t == "说明" for p in texts(cleaned) for t in p) == 3


def test_watermark_anywhere_on_almost_every_page_is_removed():
    pages = [page(i, "开头", "正文", "内部资料 请勿外传", "更多正文", "结尾") for i in range(5)]
    pages[0][0].text = "首页开头"
    cleaned, removed = clean_pages(pages)
    assert all("内部资料 请勿外传" not in p for p in texts(cleaned))


def test_few_pages_skip_repeat_detection():
    pages = [page(i, "同样的标题", f"正文{i}") for i in range(2)]
    cleaned, removed = clean_pages(pages)
    assert texts(cleaned)[0][0] == "同样的标题" and not removed


def test_page_numbers_at_page_edges_are_removed():
    cleaned, removed = clean_pages([page(0, "正文第一行", "正文第二行", "正文第三行", "- 3 -")])
    assert texts(cleaned) == [["正文第一行", "正文第二行", "正文第三行"]]
    assert removed == [("页码", "- 3 -")]


def test_number_in_middle_of_page_is_kept():
    cleaned, _ = clean_pages([page(0, "开头", "第二行", "42", "倒数第二行", "结尾")])
    assert "42" in texts(cleaned)[0]


# ---- 扫描页 ----


def test_scanned_pages_are_pages_without_real_text():
    assert scanned_pages([page(0, "封面"), page(1, "这一页有足够多的正文内容，所以不是扫描页。"), []]) == [0, 2]


# ---- 段落合并 ----

FULL = "这一整行正文排满了行宽，以句号结束。"  # 18 字，代表排满行宽、本来就结束的行


def prose(index: int, *texts: str) -> list[Line]:
    """成段排版的一页：前面几行排满行宽，后面接要测的行。"""
    return page(index, FULL, FULL, FULL, *texts)


def test_wrapped_chinese_lines_are_merged():
    p = prose(0, "这是一段很长的正文被排版拆成了两行", "现在应该合并回来。", "这一行很短。")
    assert [l.text for l in merge_wrapped([p])][3:] == ["这是一段很长的正文被排版拆成了两行现在应该合并回来。", "这一行很短。"]


def test_line_ending_with_punctuation_is_not_merged():
    p = prose(0, "这一行排满了行宽而且以句号结束了。", "下一行是新的一句。")
    assert len(merge_wrapped([p])) == 5


def test_short_lines_like_poems_are_not_merged():
    poem = page(0, "Just the place for a Snark", "the Bellman cried", "As he landed his crew", "with care", "Supporting each man")
    assert len(merge_wrapped([poem])) == 5


def test_list_items_and_headings_are_not_merged_into():
    p = prose(0, "这是一段很长的正文没有用标点结尾的行", "1. 第一项内容", "这是一段很长的正文没有用标点结尾的行", "- 第二项内容")
    heading = prose(1, "这是一段很长的正文没有用标点结尾的行", "下一行是标题")
    heading[-1].heading = 2
    assert len(merge_wrapped([p])) == 7
    assert len(merge_wrapped([heading])) == 5


def test_merge_across_pages():
    first = prose(0, FULL, "句子在这一页的最后一行还没有说完的")
    second = page(1, "延续到下一页。")
    merged = merge_wrapped([first, second])
    assert merged[-1].text == "句子在这一页的最后一行还没有说完的延续到下一页。"


def test_join_text():
    assert join_text("中文前半", "后半") == "中文前半后半"
    assert join_text("English first", "second") == "English first second"
    assert join_text("fear-", "stricken") == "fear-stricken"  # 连字符保留
