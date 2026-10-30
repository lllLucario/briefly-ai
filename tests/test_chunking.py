import pytest

from briefly import config
from briefly.chunking import chunk_text, find_headings, parse_sections


@pytest.fixture
def small_chunks(monkeypatch):
    monkeypatch.setattr(config, "CHUNK_SIZE", 100)
    monkeypatch.setattr(config, "CHUNK_OVERLAP", 0)


# ---- 标题识别 ----


def titles(text: str) -> list[tuple[int, str]]:
    return [(level, title) for _, level, title in find_headings(text)]


def test_markdown_headings():
    assert titles("# 周报\n正文\n## 周一\n### 上午 ###\n#没有空格不算") == [(1, "周报"), (2, "周一"), (3, "上午")]


def test_markdown_headings_inside_code_fence_are_ignored():
    assert titles("# 标题\n```\n# 注释\n```\n## 小节") == [(1, "标题"), (2, "小节")]


def test_numbered_headings_in_markdown_rank_below_hash_headings():
    text = "# 纪要\n一、讨论上线\n正文。\n# 附录\n（一）名单\n正文。"
    assert titles(text) == [(1, "纪要"), (2, "一、讨论上线"), (1, "附录"), (3, "（一）名单")]


def test_guessed_chinese_headings():
    text = "第一章 总体情况\n一、本周进展\n（一）支付\n1.1 回调优化\n1.1.1 细节\n正文内容。"
    assert titles(text) == [(1, "第一章 总体情况"), (2, "一、本周进展"), (3, "（一）支付"), (4, "1.1 回调优化"), (5, "1.1.1 细节")]


def test_format_decides_whether_hash_lines_are_headings():
    text = "一、背景\n示例提示词如下：\n# Role\n你是助理"
    assert [t for _, _, t in find_headings(text, markdown=False)] == ["一、背景"]  # txt / 无结构的 pdf
    assert [t for _, _, t in find_headings(text, markdown=True)] == ["一、背景", "Role"]  # md
    assert [t for _, _, t in find_headings(text)] == ["一、背景", "Role"]  # 粘贴：按内容判断


# ---- Word 转出的 Markdown：标题写在正文里 ----


def test_word_markdown_without_hash_headings_gets_numbered_sections():
    text = "**第一章 总则**\n总则正文。\n一、目的\n目的正文。\n二、范围\n范围正文。\n**第二章 职责**\n职责正文。"
    sections = parse_sections(text, markdown=True)
    assert [s.path for s in sections] == ["第一章 总则", "第一章 总则 > 一、目的", "第一章 总则 > 二、范围", "第二章 职责"]


def test_parser_inserted_textbox_appendix_does_not_hide_body_headings():
    text = "一、目的\n正文。\n二、范围\n正文。\n# 文本框附录（原文正文位置未绑定）\n框内文字"
    assert titles_md(text) == [(2, "一、目的"), (2, "二、范围"), (1, "文本框附录（原文正文位置未绑定）")]


def titles_md(text: str) -> list[tuple[int, str]]:
    return [(level, title) for _, level, title in find_headings(text, markdown=True)]


def test_inline_article_takes_only_the_article_number():
    text = "第一章 总则\n第一条 为加强管理，特制定本制度。\n第二条 本制度适用于全体员工。\n第三条：费用报销基本流程\n正文。"
    assert titles_md(text) == [(1, "第一章 总则"), (2, "第一条"), (2, "第二条"), (2, "第三条：费用报销基本流程")]


def test_heading_after_inline_article_is_not_taken_for_a_list():
    text = "第一条 为加强管理，特制定本制度。\n第二条 公司财务部门的职能是:\n(一)认真贯彻执行国家有关的财务管理制度。"
    assert titles_md(text) == [(1, "第一条"), (1, "第二条 公司财务部门的职能是")]


def test_emphasis_and_list_prefix_are_stripped_from_heading_names():
    text = ("**第一章 总则**\n正文。\n**5.1** 信息的分类与密级划分\n正文。\n- 一、 **采购申请提出**\n正文。\n"
            "**1** 适用\n正文。")
    assert [t for _, t in titles_md(text)] == ["第一章 总则", "5.1 信息的分类与密级划分", "一、 采购申请提出", "1 适用"]


def test_list_prefix_and_italic_are_told_apart():
    assert [t for _, t in titles_md("* 1.1 背景\n正文。\n*1.2 范围*\n正文。")] == ["1.1 背景", "1.2 范围"]


def test_spaced_single_number_needs_bold():
    assert titles_md("3 个月内完成\n正文。") == []


def test_single_level_arabic_numbering():
    assert [t for _, t in titles_md("1、目的\n正文。\n2. 总则\n正文。")] == ["1、目的", "2. 总则"]


def test_relative_levels_follow_first_appearance():
    cn_first = "一、总则\n正文。\n1、目的\n正文。\n二、职责\n正文。"
    assert titles_md(cn_first) == [(1, "一、总则"), (2, "1、目的"), (1, "二、职责")]
    arabic_first = "1、总则\n正文。\n一、目的\n正文。\n2、职责\n正文。"
    assert titles_md(arabic_first) == [(1, "1、总则"), (2, "一、目的"), (1, "2、职责")]


def test_numbering_restart_outweighs_first_appearance():
    # 序言下先出现“1、”，但之后“1、”两次在“一、”系列后从 1 重新编号，说明“1、”是下级
    text = ("**序言**\n正文。\n1、现状\n正文。\n2、小结\n正文。\n一、目的\n正文。\n二、职责\n正文。\n1、人事部\n正文。\n"
            "三、方式\n正文。\n1、理论学习\n正文。")
    assert titles_md(text) == [(2, "1、现状"), (2, "2、小结"), (1, "一、目的"), (1, "二、职责"), (2, "1、人事部"),
                               (1, "三、方式"), (2, "1、理论学习")]


def test_fixed_and_relative_levels_together():
    text = "第一章 总则\n正文。\n一、目的\n正文。\n（一）适用对象\n正文。\n1.1 细则\n正文。\n1.1.1 说明\n正文。"
    assert titles_md(text) == [(1, "第一章 总则"), (2, "一、目的"), (3, "（一）适用对象"), (4, "1.1 细则"), (5, "1.1.1 说明")]


def test_dotted_numbering_sits_under_its_single_level_parent():
    text = "**1** 范围\n正文。\n**5.1** 分类\n正文。\n**6** 管理\n正文。\n**6.1** 职责\n正文。"
    assert titles_md(text) == [(1, "1 范围"), (2, "5.1 分类"), (1, "6 管理"), (2, "6.1 职责")]
    assert titles_md("5.1 分类\n正文。\n5.1.1 细则\n正文。\n一、附则\n正文。") == [(1, "5.1 分类"), (2, "5.1.1 细则"), (3, "一、附则")]


def test_short_numbered_line_ending_with_colon_is_a_heading():
    assert [t for _, t in titles_md("一、目的：\n正文。\n二、适用范围：\n正文。")] == ["一、目的", "二、适用范围"]
    assert [t for _, t in titles_md("**一、公司内部讲师培养计划方案说明：**\n正文。")] == ["一、公司内部讲师培养计划方案说明"]
    assert titles_md("背景：\n正文。") == []  # 不带编号的冒号短句仍是正文


def test_label_and_content_lines_are_not_headings():
    text = "1.2.2 安全使命：构筑安全基石，建设和谐企业\n正文。\n（3）课程开发能力：内部讲师仅处在教材编写阶段\n正文。"
    assert titles_md(text) == []


def test_table_of_contents_entries_are_not_headings():
    text = ("目 录\n第一章 总则 1\n1、目的和范围..........\n1.1、目的..........\n02、企业概况 ......\n"
            "第一章 总则\n正文。")
    assert titles_md(text) == [(1, "第一章 总则")]


def test_numbered_list_items_are_not_headings():
    sentences = "1. 请购五金配件、办公用品等杂项由部门经理核准。\n正文。"
    short_list = "一、准时\n二、安全\n三、整洁\n正文。"
    sentence_list = "1、报销人应取得真实合法的原始凭证。\n2、发票须加盖公章。\n3、按月报销。"
    assert titles_md(sentences) == titles_md(short_list) == titles_md(sentence_list) == []


def test_numbers_in_tables_and_code_are_not_headings():
    text = "| 1、目的 | 说明 |\n| - | - |\n| 2、范围 | 说明 |\n```\n一、示例\n```\n正文。"
    assert titles_md(text) == []


def test_consecutive_same_level_numbered_lines_are_a_list_not_headings():
    text = "一、本周进展\n完成了以下工作：\n1.1 登录联调\n1.2 支付改造\n1.3 订单分页\n二、下周计划\n继续推进。"
    assert [t for _, _, t in find_headings(text, markdown=False)] == ["一、本周进展", "二、下周计划"]


def test_sentences_and_long_lines_are_not_headings():
    text = "一、本周完成了接口联调。\n1.1 版本已发布，\n" + "一、" + "很长" * 30 + "\n1. 普通列表项。"
    assert titles(text) == []


# ---- 章节 ----


def test_section_paths_and_ranges():
    text = "# 周报\n## 周一\n联调\n## 周二\n评审\n# 附录\n无"
    sections = parse_sections(text)
    assert [s.path for s in sections] == ["周报", "周报 > 周一", "周报 > 周二", "附录"]
    weekly, monday, tuesday, appendix = sections
    assert text[monday.start : monday.end] == "## 周一\n联调\n"
    assert text[weekly.start : weekly.end] == "# 周报\n## 周一\n联调\n## 周二\n评审\n"
    assert appendix.end == len(text)


def test_no_headings_means_no_sections():
    assert parse_sections("只是一段普通文字") == []


# ---- 分块 ----


def test_short_text_is_one_chunk():
    chunks = chunk_text("# 标题\n内容")
    assert [(c.content, c.section) for c in chunks] == [("# 标题\n内容", "标题")]


def test_text_before_first_heading_has_empty_section(small_chunks):
    text = "前言" * 40 + "\n# 正文\n" + "内容" * 40
    chunks = chunk_text(text)
    assert chunks[0].section == "" and chunks[-1].section == "正文"


def test_headings_are_not_split_into_tiny_chunks(small_chunks):
    day = "\n".join(["这一行是十个字的内容。"] * 13)  # 每天约 150 字，超过一块
    text = "# 周报\n\n" + "\n\n".join(f"## 周{d}\n\n{day}" for d in "一二三")
    chunks = chunk_text(text)
    assert all(len(c.content) > 20 for c in chunks)
    assert all("这一行" in c.content for c in chunks)  # 不存在只有标题的块


def test_section_slightly_over_size_is_kept_whole(small_chunks):
    text = "\n".join(f"## 周{d}\n" + "内容" * 55 for d in "一二")  # 每节约 116 字，略超 100
    chunks = chunk_text(text)
    assert [c.section for c in chunks] == ["周一", "周二"]


def test_small_sections_are_packed_together(small_chunks):
    text = "\n".join(f"## 事项{i}\n内容{i}" for i in range(6))  # 每节约 12 字
    chunks = chunk_text(text)
    assert len(chunks) == 1
    assert chunks[0].section == "事项0"


def test_long_section_is_split_with_overlap_and_offsets(monkeypatch):
    monkeypatch.setattr(config, "CHUNK_SIZE", 50)
    monkeypatch.setattr(config, "CHUNK_OVERLAP", 15)
    text = "# 长章节\n" + "".join(f"第{i}句内容比较长一些。" for i in range(30))
    chunks = chunk_text(text)
    assert len(chunks) > 3
    assert all(len(c.content) <= 50 for c in chunks)
    assert all(text[c.start : c.end].strip() == c.content for c in chunks)
    assert all(c.section == "长章节" for c in chunks)
    assert chunks[1].start < chunks[0].end  # 相邻块有重叠
    assert chunks[1].content.endswith("。")  # 优先在句号处切分


def test_chunks_cover_the_whole_text(small_chunks):
    text = "# 甲\n" + "甲内容。" * 50 + "\n# 乙\n" + "乙内容。" * 5
    chunks = chunk_text(text)
    covered = "".join(text[c.start : c.end] for c in chunks)
    for piece in ["甲内容。", "# 乙", "乙内容。"]:
        assert piece in covered
    assert chunks[0].start == 0 and chunks[-1].end == len(text)
