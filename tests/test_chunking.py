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


def test_markdown_mode_ignores_chinese_patterns():
    # 有 Markdown 标题时只认 Markdown，避免把正文里的“一、”当标题
    assert titles("# 纪要\n一、讨论上线") == [(1, "纪要")]


def test_guessed_chinese_headings():
    text = "第一章 总体情况\n一、本周进展\n（一）支付\n1.1 回调优化\n1.1.1 细节\n正文内容。"
    assert titles(text) == [(1, "第一章 总体情况"), (2, "一、本周进展"), (3, "（一）支付"), (2, "1.1 回调优化"), (3, "1.1.1 细节")]


def test_sentences_and_long_lines_are_not_headings():
    text = "一、本周完成了接口联调。\n1.1 版本已发布，\n" + "一、" + "很长" * 30 + "\n1. 普通列表项"
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
