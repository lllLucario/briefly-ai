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


def format_value(value, field_type: int | None = None) -> str:
    """把一个单元格的值转成文字。不同字段类型的返回结构不一样，这里按结构判断，认不出的转成字符串。"""
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
            return format_value(value["value"], value.get("type", field_type))
        if "text" in value and value.get("link"):  # 超链接
            return f"[{value['text']}]({value['link']})"
        if "link_record_ids" in value:
            return f"关联 {len(value['link_record_ids'])} 条记录"
        for key in ("text", "name", "full_address", "en_name"):
            if value.get(key):
                return str(value[key])
        return ""
    if isinstance(value, list):
        if value and all(isinstance(v, dict) and "text" in v and v.get("type") in ("text", "mention", "url", None) for v in value):
            return "".join(str(v.get("text", "")) for v in value)  # 多行文本的富文本片段
        parts = [format_value(v, field_type) for v in value]
        return "、".join(p for p in parts if p)
    return str(value)


def _cell(text: str) -> str:
    return text.replace("\r", "").replace("\n", " ").replace("|", "\\|").strip()


def read_table(client: FeishuClient, app_token: str, table_id: str = "", view_id: str = "") -> FeishuTable:
    """读取多维表格。指定 table_id 时只读这一张数据表，否则读全部（最多 MAX_TABLES 张）。"""
    warnings: list[str] = []
    name = (client.get(f"/open-apis/bitable/v1/apps/{app_token}").get("app") or {}).get("name") or "多维表格"
    tables = client.paginate("GET", f"/open-apis/bitable/v1/apps/{app_token}/tables", params={"page_size": 100})
    if table_id:
        tables = [t for t in tables if t.get("table_id") == table_id] or [{"table_id": table_id, "name": table_id}]
    elif len(tables) > MAX_TABLES:
        warnings.append(f"共有 {len(tables)} 张数据表，只读取了前 {MAX_TABLES} 张；可以粘贴具体数据表的链接")
        tables = tables[:MAX_TABLES]

    sections = []
    for t in tables:
        tid = t["table_id"]
        base = f"/open-apis/bitable/v1/apps/{app_token}/tables/{tid}"
        params = {"page_size": 100} | ({"view_id": view_id} if view_id and table_id else {})
        fields = client.paginate("GET", f"{base}/fields", params=params)
        body = {"view_id": view_id} if view_id and table_id else {}
        records = client.paginate(
            "POST", f"{base}/records/search", params={"page_size": 500}, json=body, limit=MAX_RECORDS // 500
        )
        names = [f["field_name"] for f in fields] or sorted({k for r in records for k in (r.get("fields") or {})})
        types = {f["field_name"]: f.get("type") for f in fields}
        rows = []
        for r in records:
            values = r.get("fields") or {}
            row = [_cell(format_value(values.get(n), types.get(n))) for n in names]
            if any(row):
                rows.append(row)
        heading = f"## {t.get('name') or tid}"
        if not names or not rows:
            sections.append(f"{heading}\n\n（没有记录）")
            continue
        lines = ["| " + " | ".join(_cell(n) for n in names) + " |", "|" + " --- |" * len(names)]
        lines += ["| " + " | ".join(row) + " |" for row in rows]
        sections.append(f"{heading}\n\n" + "\n".join(lines))
    return FeishuTable(app_token, name, "\n\n".join(sections), warnings)
