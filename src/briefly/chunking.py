"""章节识别与分块：纯函数，不涉及数据库。偏移量都指向规范化后的材料全文。

分块规则：以章节标题为天然边界；小章节整体放进一块，多个相邻小章节可合并成一块；
明显超过块大小的章节在内部按行、句切开。这样标题不会被切成单独的碎块，大章节也不会撑爆一块。
"""
import re
from dataclasses import dataclass

from langchain_text_splitters import RecursiveCharacterTextSplitter

from . import config

MAX_HEADING_LEN = 40  # 猜测标题时，超过该长度的行视为正文
MAX_COLON_HEADING_LEN = 15  # “一、目的：”这类带编号的冒号短行仍算标题；去掉冒号后超过该长度视为正文
# 章节只略超块大小时整节保留，不切出一小块尾巴；块大小是目标值，不是模型的硬限制
SECTION_STRETCH = 1.25


@dataclass(frozen=True)
class Section:
    level: int
    title: str
    path: str  # 如“周三 > 支付”
    start: int
    end: int  # 到下一个同级或更高级标题为止


@dataclass(frozen=True)
class Chunk:
    start: int
    end: int
    content: str
    section: str  # 块起点所在章节的路径；在第一个标题之前时为空


_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_CN_NUM = "一二三四五六七八九十百零〇两"
# 章/节/条的上下级固定；其余编号写法的上下级因文档而异，按在文档中首次出现的先后定
_FIXED = [
    (re.compile(rf"^第[{_CN_NUM}\d]+[章篇部]"), "chapter"),
    (re.compile(rf"^第[{_CN_NUM}\d]+节"), "section"),
    (re.compile(rf"^第[{_CN_NUM}\d]+条"), "article"),
]
_RELATIVE = [
    (re.compile(rf"^[{_CN_NUM}]+[、．.](?!\d)"), "cn"),
    (re.compile(rf"^[（(][{_CN_NUM}]+[）)]"), "cn-paren"),
    (re.compile(r"^[（(]\d{1,3}[）)]"), "arabic-paren"),
]
_DOTTED = re.compile(r"^(\d+(?:\.\d+)+)[.．、]?\s*(?=[^\d.\s])")  # 1.1 / 1.2.3 这类多级编号
_SINGLE = re.compile(r"^\d{1,3}([、．.])(?!\d)")  # 1、目的 / 1. 总则
_SINGLE_SPACED = re.compile(r"^\d{1,3}\s+(?=[^\d\s])")  # 1 适用：只在编号加粗时认，免得“3 个月内完成”成了标题
_BOLD_NUMBER = re.compile(r"^(\*\*|__)\d{1,3}\1\s")
_LIST_PREFIX = re.compile(r"^[-*+]\s+")  # docling 把编号段落转成列表项：“- 一、 申请商品采购流程图”
_EMPHASIS = re.compile(r"\*\*|__")
_SENTENCE_END = tuple("。；，,;！？!?：:")
_INNER_PUNCT = re.compile(r"[：:。；;！？!?]")  # 编号后的文字里带这些，是“安全使命：构筑……”这类条目，不是标题
_TOC_ENTRY = re.compile(r"(?:\.{3,}|…{2,}|-{3,}|－{3,}|·{3,})\s*(?:第?\s*\d{1,4}\s*页?)?$|\s\d{1,4}$")  # 目录行：引导线或页码结尾


@dataclass(frozen=True)
class _Guess:
    key: str  # 编号写法，判断列表用：同一写法的行紧挨着才是列表
    family: str  # 层级归属：多级编号与单级阿拉伯数字同属 arabic，按点数分上下级
    depth: int
    title: str
    inline: bool  # 条号与正文同在一行，章节名只取条号


def _lines(text: str):
    pos = 0
    for line in text.splitlines(keepends=True):
        yield pos, line.rstrip("\r\n")
        pos += len(line)


def _clean(line: str) -> tuple[str, bool]:
    """去掉列表前缀和行首尾的格式标记，只用于猜标题和章节名，不改原文与偏移。返回 (文字, 编号是否加粗)。"""
    s = _LIST_PREFIX.sub("", line.strip(), count=1)  # 先判列表前缀“* ”，再剥斜体“*…*”
    if s.startswith("\\#"):  # word_xhtml 给 # 开头的正文加的转义
        s = s[1:]
    bold_number = bool(_BOLD_NUMBER.match(s))
    s = _EMPHASIS.sub("", s)  # “一、 **采购申请提出**”加粗常在行中间
    if len(s) > 2 and s[0] == s[-1] and s[0] in "*_":
        s = s[1:-1]
    return re.sub(r"\s+", " ", s).strip(), bold_number


def _numbering(s: str, bold_number: bool) -> tuple[str, str, int, str] | None:
    """返回 (编号写法, 层级归属, 深度, 编号本身)。"""
    for pattern, key in _FIXED:
        if m := pattern.match(s):
            return key, key, 0, m.group(0)
    for pattern, key in _RELATIVE:
        if m := pattern.match(s):
            return key, key, 0, m.group(0)
    if m := _DOTTED.match(s):
        depth = m.group(1).count(".")
        return f"dotted{depth}", "arabic", depth, m.group(1)
    if m := _SINGLE.match(s):
        return f"single{m.group(1)}", "arabic", 0, m.group(0)
    if bold_number and (m := _SINGLE_SPACED.match(s)):
        return "single-spaced", "arabic", 0, m.group(0).strip()
    return None


def _guess(line: str) -> _Guess | None:
    """按常见的中文编号写法猜标题：长句、句末带标点的是正文；带编号的冒号短行算标题。

    “第三条 本制度适用于……。”条款常与正文同行，整行不像标题，仍以该行为章节起点，章节名只取条号。
    """
    s, bold_number = _clean(line)
    if not s or _TOC_ENTRY.search(s) or not (found := _numbering(s, bold_number)):
        return None
    key, family, depth, number = found
    colon = s.endswith(("：", ":"))
    core = s[:-1].rstrip() if colon else s
    rest = core[len(number):].lstrip(" ：:")  # “第一条：原始凭证……”编号后的冒号是分隔，不算正文标点
    if (len(core) <= (MAX_COLON_HEADING_LEN if colon else MAX_HEADING_LEN)
            and not core.endswith(_SENTENCE_END) and not _INNER_PUNCT.search(rest)):
        return _Guess(key, family, depth, core, False)
    if key == "article":
        return _Guess(key, family, depth, number, True)
    return None


def uses_markdown(text: str, markdown: bool | None) -> bool:
    """markdown 由文件格式决定（md、转换后的 docx/pdf 为 True，txt 为 False）；粘贴的文本为 None，按内容判断。"""
    if markdown is not None:
        return markdown
    return any(_MD_HEADING.match(line) for _, line in _lines(text))


def find_headings(text: str, markdown: bool | None = None) -> list[tuple[int, int, str]]:
    """返回 [(偏移, 级别, 标题)]。Markdown 模式先认 # 标题；其余正文行按中文编号写法猜，排在 # 标题之下。

    Word 多半不用标题样式，转成 Markdown 后没有 # 行，标题全写在正文里，所以两种模式都要猜。
    级别只表示上下级，按文档里实际出现的层级重新编为 1、2、3……
    """
    marked: list[tuple[int, tuple[int, int, int], str]] = []
    guessed: list[tuple[int, _Guess] | None] = []  # 与非空行一一对应，不是候选标题时为 None
    in_fence = False
    md = uses_markdown(text, markdown)
    for offset, line in _lines(text):
        if not line.strip():
            continue
        if _FENCE.match(line):
            in_fence = not in_fence
            guessed.append(None)
        elif in_fence or _is_table_line(line):
            guessed.append(None)
        elif md and (m := _MD_HEADING.match(line)):
            marked.append((offset, (0, len(m.group(1)), 0), m.group(2).strip()))
            guessed.append(None)
        else:
            found = _guess(line)
            guessed.append((offset, found) if found else None)
    headings = marked + _rank(_drop_lists(guessed))
    ranks = {key: i + 1 for i, key in enumerate(sorted({key for _, key, _ in headings}))}
    return sorted((offset, ranks[key], title) for offset, key, title in headings)


def _drop_lists(guessed: list[tuple[int, _Guess] | None]) -> list[tuple[int, _Guess]]:
    """排除列表：同一种编号写法的行紧挨着、中间没有正文的，是列表而不是标题。

    条号与正文同行的条款本身带着正文：它自己不算列表，作邻居时也按正文看。
    """
    keep = []
    for i, item in enumerate(guessed):
        if item is None:
            continue
        neighbors = [guessed[j] for j in (i - 1, i + 1) if 0 <= j < len(guessed)]
        if item[1].inline or not any(n is not None and not n[1].inline and n[1].key == item[1].key
                                     for n in neighbors):
            keep.append(item)
    return keep


def _rank(kept: list[tuple[int, _Guess]]) -> list[tuple[int, tuple[int, int, int], str]]:
    """章/节/条在上；其余编号写法按首次出现的先后往下排，多级编号跟着首段，“1.1”在“1”之下。"""
    fixed = {"chapter": 0, "section": 1, "article": 2}
    order: dict[str, int] = {}
    result = []
    for offset, g in kept:
        if g.family in fixed:
            key = (1, fixed[g.family], 0)
        else:
            key = (2, order.setdefault(g.family, len(order)), g.depth)
        result.append((offset, key, g.title))
    return result


def parse_sections(text: str, markdown: bool | None = None) -> list[Section]:
    headings = find_headings(text, markdown)
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    for i, (start, level, title) in enumerate(headings):
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        end = next((h[0] for h in headings[i + 1 :] if h[1] <= level), len(text))
        sections.append(Section(level, title, " > ".join(t for _, t in stack), start, end))
    return sections


def _split_long(text: str, start: int, end: int) -> list[tuple[int, int]]:
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=config.CHUNK_SIZE,
        chunk_overlap=config.CHUNK_OVERLAP,
        separators=["\n", "。", "！", "？", "；", " ", ""],
        keep_separator="end",
        add_start_index=True,
    )
    pieces = []
    for doc in splitter.create_documents([text[start:end]]):
        offset = start + doc.metadata["start_index"]
        pieces.append((offset, offset + len(doc.page_content)))
    return pieces


def _section_at(sections: list[Section], pos: int) -> str:
    path = ""
    for s in sections:  # 按起点排序，后出现的嵌套更深
        if s.start <= pos < s.end:
            path = s.path
    return path


def _is_table_line(line: str) -> bool:
    return line.lstrip().startswith("|")


def _split_with_tables(text: str, start: int, end: int) -> list[tuple[int, int, str]]:
    """含 Markdown 表格的长章节按行装填；表格被切开时，后面的块以表头开头，否则读不懂各列含义。

    返回 [(起, 止, 前缀)]，前缀是补上的表头（不在原文这个位置上）。
    """
    pieces: list[tuple[int, int, str]] = []
    piece_start, prefix = start, ""
    header, row_no, pos = "", 0, start
    for line in text[start:end].splitlines(keepends=True):
        line_start, pos = pos, pos + len(line)
        if _is_table_line(line):
            row_no += 1
            if row_no <= 2:  # 表头行 + 分隔行 |---|
                header += line if line.endswith("\n") else line + "\n"
        else:
            header, row_no = "", 0
        if line_start > piece_start and pos - piece_start + len(prefix) > config.CHUNK_SIZE:
            pieces.append((piece_start, line_start, prefix))
            piece_start = line_start
            prefix = header if row_no > 2 else ""
    pieces.append((piece_start, end, prefix))
    return pieces


def chunk_text(text: str, sections: list[Section] | None = None) -> list[Chunk]:
    if sections is None:
        sections = parse_sections(text)
    bounds = sorted({0, len(text), *(s.start for s in sections)})
    units: list[tuple[int, int, str]] = []
    for a, b in zip(bounds, bounds[1:]):
        if b - a <= config.CHUNK_SIZE * SECTION_STRETCH:
            units.append((a, b, ""))
        elif any(_is_table_line(line) for line in text[a:b].splitlines()):
            units.extend(_split_with_tables(text, a, b))
        else:
            units.extend((x, y, "") for x, y in _split_long(text, a, b))

    spans: list[tuple[int, int, str]] = []
    for a, b, prefix in units:
        if spans and not prefix and b - spans[-1][0] + len(spans[-1][2]) <= config.CHUNK_SIZE:
            spans[-1] = (spans[-1][0], max(spans[-1][1], b), spans[-1][2])
        else:
            spans.append((a, b, prefix))

    return [
        Chunk(a, b, prefix + content, _section_at(sections, a))
        for a, b, prefix in spans
        if (content := text[a:b].strip())
    ]
