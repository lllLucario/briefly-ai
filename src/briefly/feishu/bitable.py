"""飞书多维表格：读取为 Markdown 表格（每个数据表一节），作为材料参与总结和周报。"""
from dataclasses import dataclass, field
from datetime import datetime

from .client import FeishuClient

MAX_TABLES = 10
MAX_RECORDS = 5000
# 字段类型（官方 FieldType）：日期、创建时间、修改时间存为毫秒时间戳
_DATE_TYPES = {5, 1001, 1002}
_CHECKBOX = 7


@dataclass
class FeishuTable:
    app_token: str
    title: str
    markdown: str
    warnings: list[str] = field(default_factory=list)


def _date(ms) -> str:
    try:
        dt = datetime.fromtimestamp(int(ms) / 1000)
    except (TypeError, ValueError, OSError):
        return str(ms)
    return dt.strftime("%Y-%m-%d") if (dt.hour, dt.minute) == (0, 0) else dt.strftime("%Y-%m-%d %H:%M")


def format_value(value, field_type: int | None = None, titles: dict[str, str] | None = None) -> str:
    """把一个单元格的值转成文字。不同字段类型的返回结构不一样，这里按结构判断，认不出的转成字符串。

    titles：记录编号 → 该记录主字段的文字，用于把关联字段显示成被关联记录的标题。
    """
    if value is None:
        return ""
    if field_type in _DATE_TYPES and isinstance(value, (int, float)):
        return _date(value)
    if field_type == _CHECKBOX or isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:g}" if value != int(value) else str(int(value))
    if isinstance(value, (int, str)):
        return str(value)
    if isinstance(value, dict):
        if "value" in value:  # 查找引用、公式
            return format_value(value["value"], value.get("type", field_type), titles)
        if "text" in value and value.get("link"):  # 超链接
            return f"[{value['text']}]({value['link']})"
        if "link_record_ids" in value:  # 单向 / 双向关联；没有关联时实测为 null
            ids = value["link_record_ids"] or []
            if titles is not None and ids and all(i in titles for i in ids):
                return "、".join(titles[i] for i in ids)
            return f"关联 {len(ids)} 条记录" if ids else ""
        for key in ("text", "name", "full_address", "en_name"):
            if value.get(key):
                return str(value[key])
        return ""
    if isinstance(value, list):
        if value and all(isinstance(v, dict) and "text" in v and v.get("type") in ("text", "mention", "url", None) for v in value):
            return "".join(str(v.get("text", "")) for v in value)  # 多行文本的富文本片段
        parts = [format_value(v, field_type, titles) for v in value]
        return "、".join(p for p in parts if p)
    return str(value)


def _cell(text: str) -> str:
    return text.replace("\r", "").replace("\n", " ").replace("|", "\\|").strip()


def read_table(client: FeishuClient, app_token: str, table_id: str = "", view_id: str = "") -> FeishuTable:
    """读取多维表格。指定 table_id 时只读这一张数据表，否则读全部（最多 MAX_TABLES 张）。

    先读完所有数据表，再统一转文字：关联字段显示被关联记录的主字段（标题），模型能看出记录之间的关系；
    被关联的记录不在本次读取范围内（只读了一张表）时显示“关联 N 条记录”。
    """
    warnings: list[str] = []
    name = (client.get(f"/open-apis/bitable/v1/apps/{app_token}").get("app") or {}).get("name") or "多维表格"
    tables = client.paginate("GET", f"/open-apis/bitable/v1/apps/{app_token}/tables", params={"page_size": 100})
    if table_id:
        tables = [t for t in tables if t.get("table_id") == table_id] or [{"table_id": table_id, "name": table_id}]
        name = f"{name} · {tables[0].get('name') or table_id}"  # 只读一张表时，标题里写明是哪张
    elif len(tables) > MAX_TABLES:
        warnings.append(f"共有 {len(tables)} 张数据表，只读取了前 {MAX_TABLES} 张；可以粘贴具体数据表的链接")
        tables = tables[:MAX_TABLES]

    loaded = []
    for t in tables:
        tid = t["table_id"]
        base = f"/open-apis/bitable/v1/apps/{app_token}/tables/{tid}"
        params = {"page_size": 100} | ({"view_id": view_id} if view_id and table_id else {})
        fields = client.paginate("GET", f"{base}/fields", params=params)
        body = {"view_id": view_id} if view_id and table_id else {}
        records = client.paginate(
            "POST", f"{base}/records/search", params={"page_size": 500}, json=body, limit=MAX_RECORDS // 500
        )
        loaded.append((t, fields, records))

    titles: dict[str, str] = {}  # 记录编号 → 主字段文字
    for _, fields, records in loaded:
        primary = next((f for f in fields if f.get("is_primary")), fields[0] if fields else None)
        if primary is None:
            continue
        for r in records:
            text = _cell(format_value((r.get("fields") or {}).get(primary["field_name"]), primary.get("type")))
            if r.get("record_id") and text:
                titles[r["record_id"]] = text

    sections = []
    for t, fields, records in loaded:
        names = [f["field_name"] for f in fields] or sorted({k for r in records for k in (r.get("fields") or {})})
        types = {f["field_name"]: f.get("type") for f in fields}
        rows = []
        for r in records:
            values = r.get("fields") or {}
            row = [_cell(format_value(values.get(n), types.get(n), titles)) for n in names]
            if any(row):
                rows.append(row)
        heading = f"## {t.get('name') or t['table_id']}"
        if not names or not rows:
            sections.append(f"{heading}\n\n（没有记录）")
            continue
        lines = ["| " + " | ".join(_cell(n) for n in names) + " |", "|" + " --- |" * len(names)]
        lines += ["| " + " | ".join(row) + " |" for row in rows]
        sections.append(f"{heading}\n\n" + "\n".join(lines))
    return FeishuTable(app_token, name, "\n\n".join(sections), warnings)
