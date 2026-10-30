"""轮次结果记录：助理一轮结束时，按实际调用的工具和产出的结构写一份记录，界面和路由读这份记录。

- 用户消息 additional_kwargs["intent"]["resolved"]：本轮实际能力，取值沿用意图漏斗的意图名；推不出时为 None。
  漏斗当时的判断 intent["intent"] 不改（评测要用）。
- 回复 response_metadata["result"]：{"kinds": [...], "items": [{"index": 1, "id": "...", "kind": "todo"}]}。
  kinds 是本轮实际产出的结果类型，只用于安排结果操作；items 是结构化列表各条的序号（按 kind 各自从 1 起）与稳定 ID，
  给以后的槽位功能定位“第二条”。两者都不是业务保存依据：保存待办仍走结构化预览和勾选确认，不从这里建任务。

助理只输出文字，结构来自各能力规定的输出栏目（【待办清单】等）；记录在轮次收尾时生成一次并存下，
网页和路由读记录，不再各自解析回答。2026-10-30 之前的旧消息没有记录，读取方按“未知”处理，网页回退到 output_kinds。
"""
import re

# 能力输出栏目 → 结果类型（与 prompts/ 中各能力的输出格式对应）
MARKERS = {"summarize": "一句话总结", "minutes": "会议信息|行动项", "weekly": "周报标题", "todo": "待办清单", "polish": "优化后"}
# 带条目的栏目：栏目名 → 条目类型
LIST_SECTIONS = {"待办清单": "todo", "行动项": "action_item"}
# 实际调用的工具 → 能力（只列能单独说明本轮做了什么的工具；材料读取、完整提炼等通用工具不算）
TOOL_CAPABILITY = {"search_knowledge": "knowledge", "read_todos": "business_query", "read_weekly_reports": "business_query"}

SECTION = re.compile(r"(?m)^[ \t]*(?:#{1,6}[ \t]*)?(?:\*\*)?【([^】\n]+)】(?:\*\*)?")
HEADING = re.compile(r"(?m)^[ \t]*#{1,6}[ \t]")
LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.、)）])\s+(\S.*)$")
TABLE_SEPARATOR = re.compile(r"^\|?[\s:|-]*-{3,}[\s:|-]*$")
TASK_COLUMN = re.compile(r"任务|事项|待办|行动")
OWNER_COLUMN = re.compile(r"负责|截止|期限")
TODO_REQUEST = re.compile(r"待办|任务|todo|提醒", re.I)
EMPTY = {"无", "暂无", "没有", "无。"}


def output_kinds(text: str) -> list[str]:
    """按能力输出栏目认出结果类型；也是旧消息没有记录时网页的回退。"""
    return [k for k, marker in MARKERS.items() if re.search(rf"(?m)^\s*(?:#{{1,6}}\s*)?(?:\*\*)?【(?:{marker})】", text)]


def turn_record(text: str, *, intent: dict | None, skill: str | None, tools: list[str], user_text: str = "",
                message_id: str | None = None, system: bool = False) -> tuple[str | None, dict]:
    """返回 (resolved, result)。system 为程序给出的失败或提示类回复：不算产出了结果，也推不出能力。"""
    if system:
        return None, {"kinds": [], "items": []}
    used = set(tools)
    knowledge = "search_knowledge" in used
    kinds = output_kinds(text)
    rows = _section_entries(text)
    if not rows and not knowledge:
        # 没写栏目的待办：有“任务＋负责人/期限”列的表；待办类请求下也认列表（知识回答里的职责表不算）
        requested = (intent or {}).get("intent") == "todo" or skill == "todo" or (
            (intent or {}).get("intent") == "business_action" and TODO_REQUEST.search(user_text or ""))
        rows = [("todo", r) for r in _task_table(text) or (_list_items(text) if requested else [])]
    if any(kind == "todo" for kind, _ in rows) and "todo" not in kinds:
        kinds.append("todo")
    items, counts = [], {}
    for kind, _ in rows:
        counts[kind] = counts.get(kind, 0) + 1
        items.append({"index": counts[kind], "id": f"{message_id or 'reply'}:{kind}:{counts[kind]}", "kind": kind})
    return _resolve(kinds, used, skill, (intent or {}).get("intent")), {"kinds": kinds, "items": items}


def _resolve(kinds: list[str], tools: set[str], skill: str | None, inferred: str | None) -> str | None:
    """工具/产出 → 能力。多种结果并存时只用用户指定或漏斗推断的能力来选，选不出记 None，不猜。"""
    if "search_knowledge" in tools:
        return "knowledge"
    if len(kinds) == 1:
        return kinds[0]
    if kinds:
        return skill if skill in kinds else inferred if inferred in kinds else None
    found = {TOOL_CAPABILITY[t] for t in tools if t in TOOL_CAPABILITY}
    return found.pop() if len(found) == 1 else None


def _section_entries(text: str) -> list[tuple[str, str]]:
    marks = list(SECTION.finditer(text))
    rows = []
    for i, m in enumerate(marks):
        kind = LIST_SECTIONS.get(m[1].strip())
        if not kind:
            continue
        body = text[m.end(): marks[i + 1].start() if i + 1 < len(marks) else len(text)]
        heading = HEADING.search(body)
        body = body[: heading.start()] if heading else body
        entries = _table_rows(body) or _list_items(body)
        if not entries:  # “【行动项】整理记录”这类栏目后同行直接写的一条
            first = body.split("\n", 1)[0].strip()
            entries = [first] if first and first not in EMPTY else []
        rows += [(kind, e) for e in entries]
    return rows


def _tables(text: str) -> list[list[list[str]]]:
    tables, current = [], []
    for line in [*text.splitlines(), ""]:
        if line.strip().startswith("|"):
            current.append([c.strip() for c in line.strip().strip("|").split("|")])
        else:
            if len(current) >= 2 and TABLE_SEPARATOR.match("|".join(current[1])):
                tables.append([current[0], *current[2:]])
            current = []
    return tables


def _table_rows(text: str) -> list[str]:
    tables = _tables(text)
    return [" | ".join(r) for r in tables[0][1:] if any(c and c not in EMPTY for c in r)] if tables else []


def _task_table(text: str) -> list[str]:
    for header, *rows in _tables(text):
        if any(TASK_COLUMN.search(c) for c in header) and any(OWNER_COLUMN.search(c) for c in header):
            return [" | ".join(r) for r in rows if any(c and c not in EMPTY for c in r)]
    return []


def _list_items(text: str) -> list[str]:
    return [m[1] for line in text.splitlines() if (m := LIST_ITEM.match(line)) and m[1].strip() not in EMPTY]
