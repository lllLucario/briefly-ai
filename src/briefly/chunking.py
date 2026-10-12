"""章节识别与分块：纯函数，不涉及数据库。偏移量都指向规范化后的材料全文。

分块规则：以章节标题为天然边界；小章节整体放进一块，多个相邻小章节可合并成一块；
明显超过块大小的章节在内部按行、句切开。这样标题不会被切成单独的碎块，大章节也不会撑爆一块。
"""
import re
from dataclasses import dataclass

from langchain_text_splitters import RecursiveCharacterTextSplitter

from . import config

MAX_HEADING_LEN = 40  # 猜测标题时，超过该长度的行视为正文
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
_PATTERNS = [
    (re.compile(rf"^第[{_CN_NUM}\d]+[章篇部]"), 1),
    (re.compile(rf"^第[{_CN_NUM}\d]+节"), 2),
    (re.compile(rf"^[{_CN_NUM}]+[、．]"), 2),
    (re.compile(rf"^[（(][{_CN_NUM}]+[）)]"), 3),
]
_DOTTED = re.compile(r"^(\d+(?:\.\d+)+)\s*[^\d.\s]")  # 1.1 / 1.2.3 这类多级编号
_SENTENCE_END = tuple("。；，,;！？!?：:")


def _lines(text: str):
    pos = 0
    for line in text.splitlines(keepends=True):
        yield pos, line.rstrip("\r\n")
        pos += len(line)


def _guess_level(line: str) -> tuple[int, str] | None:
    """没有 Markdown 标题的文本（txt、无字号与书签的 pdf、粘贴）按常见的中文标题写法猜。

    返回 (级别, 编号写法)；编号写法用于区分列表，如“一、”和“1.3”级别相同但不是同一组。
    """
    s = line.strip()
    if not s or len(s) > MAX_HEADING_LEN or s.endswith(_SENTENCE_END):
        return None
    for pattern, level in _PATTERNS:
        if pattern.match(s):
            return level, pattern.pattern
    if m := _DOTTED.match(s):
        depth = m.group(1).count(".") + 1
        return depth, f"dotted{depth}"
    return None


def uses_markdown(text: str, markdown: bool | None) -> bool:
    """markdown 由文件格式决定（md、转换后的 docx/pdf 为 True，txt 为 False）；粘贴的文本为 None，按内容判断。"""
    if markdown is not None:
        return markdown
    return any(_MD_HEADING.match(line) for _, line in _lines(text))


def find_headings(text: str, markdown: bool | None = None) -> list[tuple[int, int, str]]:
    """返回 [(偏移, 级别, 标题)]。Markdown 模式只认 # 标题；否则按中文编号写法猜。"""
    lines = list(_lines(text))
    if uses_markdown(text, markdown):
        headings: list[tuple[int, int, str]] = []
        in_fence = False
        for offset, line in lines:
            if _FENCE.match(line):
                in_fence = not in_fence
            elif not in_fence and (m := _MD_HEADING.match(line)):
                headings.append((offset, len(m.group(1)), m.group(2).strip()))
        return headings
    return _drop_lists(lines)


def _drop_lists(lines: list[tuple[int, str]]) -> list[tuple[int, int, str]]:
    """按写法猜标题，并排除列表：同一种编号写法的行紧挨着、中间没有正文的，是列表而不是标题。"""
    guessed: list[tuple[int, int, str, str] | None] = []  # 与非空行一一对应，不是候选标题时为 None
    for offset, line in lines:
        if line.strip():
            found = _guess_level(line)
            guessed.append((offset, found[0], line.strip(), found[1]) if found else None)
    keep = []
    for i, item in enumerate(guessed):
        if item is None:
            continue
        neighbors = [guessed[j] for j in (i - 1, i + 1) if 0 <= j < len(guessed)]
        if not any(n is not None and n[3] == item[3] for n in neighbors):
            keep.append(item[:3])
    return keep


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


def chunk_text(text: str, sections: list[Section] | None = None) -> list[Chunk]:
    if sections is None:
        sections = parse_sections(text)
    bounds = sorted({0, len(text), *(s.start for s in sections)})
    units: list[tuple[int, int]] = []
    for a, b in zip(bounds, bounds[1:]):
        keep_whole = b - a <= config.CHUNK_SIZE * SECTION_STRETCH
        units.extend([(a, b)] if keep_whole else _split_long(text, a, b))

    spans: list[tuple[int, int]] = []
    for a, b in units:
        if spans and b - spans[-1][0] <= config.CHUNK_SIZE:
            spans[-1] = (spans[-1][0], max(spans[-1][1], b))
        else:
            spans.append((a, b))

    return [
        Chunk(a, b, content, _section_at(sections, a))
        for a, b in spans
        if (content := text[a:b].strip())
    ]
