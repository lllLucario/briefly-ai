"""云空间浏览与搜索（需要用户授权）：列出文件夹内容、按关键词搜索云文档，结果交给网页勾选导入。"""
import re
from dataclasses import asdict, dataclass

from .client import FeishuClient
from .docx import doc_url

# 能导入为材料的类型（其余类型列出来但不能导入，网页上置灰）
IMPORTABLE = {"docx", "bitable", "wiki"}
_KIND_NAMES = {
    "docx": "文档", "doc": "旧版文档", "sheet": "电子表格", "bitable": "多维表格", "mindnote": "思维笔记",
    "file": "文件", "folder": "文件夹", "slides": "幻灯片", "wiki": "知识库", "shortcut": "快捷方式",
}  # fmt: skip
_TAG = re.compile(r"</?[^>]+>")


@dataclass
class DriveItem:
    token: str
    name: str
    kind: str  # docx / bitable / folder / sheet ...
    kind_name: str
    url: str
    importable: bool
    modified: int | None = None  # 秒级时间戳
    owner: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _item(token: str, name: str, kind: str, url: str = "", modified=None, owner: str = "") -> DriveItem:
    kind = (kind or "").lower()
    return DriveItem(
        token=token,
        name=name or "（无标题）",
        kind=kind,
        kind_name=_KIND_NAMES.get(kind, kind),
        url=url or (doc_url(token, kind) if kind in IMPORTABLE else ""),
        importable=kind in IMPORTABLE,
        modified=int(modified) if str(modified or "").isdigit() else None,
        owner=owner,
    )


def list_folder(client: FeishuClient, folder_token: str = "") -> list[DriveItem]:
    """列出文件夹内容；不传 folder_token 时是“我的空间”根目录（飞书规定根目录不分页）。快捷方式换成指向的文件。"""
    params = {"page_size": 200, "order_by": "EditedTime", "direction": "DESC"}
    if folder_token:
        params["folder_token"] = folder_token
    # 返回的列表字段是 files、下一页标记是 next_page_token（不是通用的 items / page_token）
    files = client.paginate("GET", "/open-apis/drive/v1/files", params=params, items_key="files", token_key="next_page_token")
    items = []
    for f in files:
        kind, token = f.get("type", ""), f.get("token", "")
        if kind == "shortcut" and f.get("shortcut_info"):
            kind, token = f["shortcut_info"].get("target_type", kind), f["shortcut_info"].get("target_token", token)
        items.append(_item(token, f.get("name", ""), kind, f.get("url", "") if kind == f.get("type") else "", f.get("modified_time")))
    folders = [x for x in items if x.kind == "folder"]
    return folders + [x for x in items if x.kind != "folder"]  # 文件夹在前


def search(client: FeishuClient, query: str, limit: int = 20) -> list[DriveItem]:
    """按关键词搜索云文档和知识库（每页最多 20 条，这里只取第一页）。"""
    query = query.strip()[:30]  # 接口限制 30 个字符
    if not query:
        return []
    data = client.post(
        "/open-apis/search/v2/doc_wiki/search",
        {"query": query, "doc_filter": {}, "wiki_filter": {}, "page_size": min(limit, 20)},
        idempotent=True,
    )
    items = []
    for unit in data.get("res_units") or []:
        meta = unit.get("result_meta") or {}
        url = meta.get("url", "")
        kind = (meta.get("doc_types") or unit.get("entity_type") or "").lower()
        match = re.search(r"/(docx|wiki|base|sheets|docs|mindnotes|file|slides)/([A-Za-z0-9]+)", url)
        token = match.group(2) if match else meta.get("token", "")
        if match and match.group(1) in ("docx", "wiki", "base"):
            kind = {"base": "bitable"}.get(match.group(1), match.group(1))
        title = _TAG.sub("", unit.get("title_highlighted") or meta.get("title") or "")
        items.append(_item(token, title, kind, url, meta.get("update_time"), meta.get("owner_name", "")))
    return items
