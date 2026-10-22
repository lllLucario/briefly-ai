"""飞书新版文档（docx）：读取为 Markdown、把 Markdown 写成新文档。

读取用“获取文档所有块”接口还原标题、列表、表格等结构（纯文本接口 raw_content 会丢掉结构，章节和分块就做不准）。
写入用“Markdown 转换为块” + “创建嵌套块”，表格块要先去掉只读的 merge_info，单次最多插入 1000 个块。
"""
import uuid
from dataclasses import dataclass, field
from urllib.parse import unquote

from .. import config
from .client import FeishuClient, FeishuError

# 块类型（官方 BlockType 枚举）
PAGE, TEXT = 1, 2
HEADINGS = range(3, 12)  # heading1–heading9
BULLET, ORDERED, CODE, QUOTE, TODO = 12, 13, 14, 15, 17
BITABLE, CALLOUT, DIVIDER, FILE, GRID, GRID_COLUMN, IMAGE = 18, 19, 22, 23, 24, 25, 27
SHEET, TABLE, TABLE_CELL, QUOTE_CONTAINER = 30, 31, 32, 34
# 块类型 → 块里存放内容的字段名
_KEYS = {
    PAGE: "page", TEXT: "text", BULLET: "bullet", ORDERED: "ordered", CODE: "code", QUOTE: "quote", TODO: "todo",
    **{h: f"heading{h - 2}" for h in HEADINGS},
}  # fmt: skip
# 读不出文字的块：写一个占位说明，并在解析报告里提醒
_PLACEHOLDERS = {IMAGE: "[图片]", BITABLE: "[内嵌多维表格，未读取]", SHEET: "[内嵌电子表格，未读取]", 21: "[流程图]", 29: "[思维笔记]"}
MAX_BLOCKS_PER_CALL = 1000


@dataclass
class FeishuDoc:
    document_id: str
    title: str
    markdown: str
    revision_id: int | None = None
    warnings: list[str] = field(default_factory=list)


def _inline(elements: list[dict]) -> str:
    out = []
    for e in elements or []:
        if "text_run" in e:
            run = e["text_run"]
            text = run.get("content", "")
            style = run.get("text_element_style") or {}
            if text.strip():
                if style.get("inline_code"):
                    text = f"`{text}`"
                else:
                    if style.get("bold"):
                        text = f"**{text}**"
                    if style.get("strikethrough"):
                        text = f"~~{text}~~"
                link = (style.get("link") or {}).get("url")
                if link:
                    text = f"[{text}]({unquote(link)})"
            out.append(text)
        elif "mention_user" in e:
            out.append("@" + (e["mention_user"].get("name") or "用户"))
        elif "mention_doc" in e:
            doc = e["mention_doc"]
            title = doc.get("title") or "文档"
            out.append(f"[{title}]({unquote(doc['url'])})" if doc.get("url") else title)
        elif "equation" in e:
            out.append(f"${e['equation'].get('content', '').strip()}$")
        elif "reminder" in e:
            out.append("[提醒]")
        elif "file" in e:
            out.append("[附件]")
    return "".join(out)


class _Renderer:
    def __init__(self, blocks: list[dict]) -> None:
        self.by_id = {b["block_id"]: b for b in blocks}
        self.skipped: dict[str, int] = {}

    def text_of(self, block: dict) -> str:
        key = _KEYS.get(block.get("block_type"))
        return _inline((block.get(key) or {}).get("elements", [])) if key else ""

    def children(self, block: dict) -> list[dict]:
        return [self.by_id[c] for c in block.get("children") or [] if c in self.by_id]

    def render(self, blocks: list[dict], depth: int = 0) -> list[str]:
        """渲染一串同级块，返回 Markdown 段落（段落之间空一行）。"""
        paras: list[str] = []
        number = 0
        for b in blocks:
            t = b.get("block_type")
            number = number + 1 if t == ORDERED else 0
            paras += self.block(b, depth, number)
        return paras

    def block(self, b: dict, depth: int, number: int) -> list[str]:
        t = b.get("block_type")
        indent = "  " * depth
        if t == TEXT:
            text = self.text_of(b)
            return [text] if text.strip() else []
        if t in HEADINGS:
            text = self.text_of(b).strip()
            return [f"{'#' * min(t - 2, 6)} {text}"] if text else []
        if t in (BULLET, ORDERED, TODO):
            if t == BULLET:
                mark = "-"
            elif t == ORDERED:
                mark = f"{number}."
            else:
                mark = "- [x]" if (b.get("todo") or {}).get("style", {}).get("done") else "- [ ]"
            item = f"{indent}{mark} {self.text_of(b).strip()}"
            nested = self.render(self.children(b), depth + 1)
            return ["\n".join([item, *nested])]  # 列表项和子项之间不空行
        if t == CODE:
            return [f"```\n{self.text_of(b)}\n```"]
        if t == QUOTE:
            return ["> " + self.text_of(b).strip()]
        if t in (CALLOUT, QUOTE_CONTAINER):
            inner = self.render(self.children(b), depth)
            return ["\n".join("> " + line if line else ">" for p in inner for line in p.split("\n"))] if inner else []
        if t == DIVIDER:
            return ["---"]
        if t == TABLE:
            return [self.table(b)]
        if t in _PLACEHOLDERS:
            self.skipped[_PLACEHOLDERS[t]] = self.skipped.get(_PLACEHOLDERS[t], 0) + 1
            return [_PLACEHOLDERS[t]]
        if t == FILE:
            name = (b.get("file") or {}).get("name", "")
            self.skipped["[附件]"] = self.skipped.get("[附件]", 0) + 1
            return [f"[附件：{name}]" if name else "[附件]"]
        # 分栏等容器：按顺序渲染子块；不认识且没有子块的跳过
        return self.render(self.children(b), depth)

    def table(self, b: dict) -> str:
        prop = (b.get("table") or {}).get("property") or {}
        cols = int(prop.get("column_size") or 0)
        cells = (b.get("table") or {}).get("cells") or [c for c in b.get("children") or []]
        if not cols or not cells:
            return ""
        rows = []
        for i in range(0, len(cells), cols):
            row = []
            for cid in cells[i : i + cols]:
                cell = self.by_id.get(cid, {})
                text = " ".join(p.replace("\n", " ") for p in self.render(self.children(cell)))
                row.append(text.replace("|", "\\|").strip())
            rows.append(row)
        lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * cols]
        lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        return "\n".join(lines)


def blocks_to_markdown(blocks: list[dict]) -> tuple[str, str, list[str]]:
    """返回 (标题, 正文 Markdown, 提示)。blocks 是“获取文档所有块”的结果（含根节点 page）。"""
    r = _Renderer(blocks)
    root = next((b for b in blocks if b.get("block_type") == PAGE), None)
    if root is None:
        return "", "\n\n".join(r.render(blocks)), []
    title = r.text_of(root).strip()
    body = "\n\n".join(p for p in r.render(r.children(root)) if p.strip())
    warnings = [f"文档中有 {n} 处{name}，只保留了占位说明" for name, n in r.skipped.items()]
    return title, body, warnings


def read_document(client: FeishuClient, document_id: str) -> FeishuDoc:
    info = client.get(f"/open-apis/docx/v1/documents/{document_id}").get("document") or {}
    blocks = client.paginate("GET", f"/open-apis/docx/v1/documents/{document_id}/blocks", params={"page_size": 500})
    title, body, warnings = blocks_to_markdown(blocks)
    title = info.get("title") or title or "飞书文档"
    return FeishuDoc(document_id, title, body, info.get("revision_id"), warnings)


def document_revision(client: FeishuClient, document_id: str) -> tuple[str, int | None]:
    """只取标题和版本号（判断文档有没有变化，变化了才读全部块）。"""
    info = client.get(f"/open-apis/docx/v1/documents/{document_id}").get("document") or {}
    return info.get("title", ""), info.get("revision_id")


def doc_url(document_id: str, kind: str = "docx") -> str:
    domain = (config.FEISHU_DOC_DOMAIN or "https://feishu.cn").rstrip("/")
    return f"{domain}/{kind}/{document_id}"


def _batches(first_ids: list[str], blocks: dict[str, dict]) -> list[tuple[list[str], list[dict]]]:
    """按顶层块分批，每批连同所有子孙块不超过 MAX_BLOCKS_PER_CALL。"""

    def subtree(bid: str) -> list[dict]:
        out, stack = [], [bid]
        while stack:
            b = blocks.get(stack.pop())
            if b is None:
                continue
            out.append(b)
            stack.extend(reversed(b.get("children") or []))
        return out

    batches: list[tuple[list[str], list[dict]]] = []
    ids: list[str] = []
    desc: list[dict] = []
    for bid in first_ids:
        tree = subtree(bid)
        if len(tree) > MAX_BLOCKS_PER_CALL:
            raise FeishuError(f"有一个表格或列表过大（{len(tree)} 个块），超过飞书单次写入上限 {MAX_BLOCKS_PER_CALL}")
        if desc and len(desc) + len(tree) > MAX_BLOCKS_PER_CALL:
            batches.append((ids, desc))
            ids, desc = [], []
        ids.append(bid)
        desc += tree
    if ids:
        batches.append((ids, desc))
    return batches


def _clean(block: dict) -> dict:
    """转换结果直接用于插入前的清理：表格的 merge_info 只读，传入会报错；parent_id 由插入位置决定。"""
    block = {k: v for k, v in block.items() if k != "parent_id"}
    if block.get("block_type") == TABLE and isinstance(block.get("table"), dict):
        table = dict(block["table"])
        table["property"] = {k: v for k, v in (table.get("property") or {}).items() if k != "merge_info"}
        block["table"] = table
    return block


def create_document(client: FeishuClient, title: str, markdown: str, folder_token: str = "", on_created=None) -> str:
    """新建文档并写入 Markdown 内容，返回 document_id。"""
    body = {"title": title[:800]}
    if folder_token:
        body["folder_token"] = folder_token
    # 新建文档没有去重标识：网络错误或 5xx 时不重试（可能已经建好），由用户决定是否重新写入
    document_id = client.post("/open-apis/docx/v1/documents", body)["document"]["document_id"]
    if on_created is not None:
        on_created(document_id)  # 先持久化文档身份，再进行转换、分批写入
    if markdown.strip():
        converted = client.post(  # 只做格式转换，不改任何文档，可以重试
            "/open-apis/docx/v1/documents/blocks/convert",
            {"content_type": "markdown", "content": markdown},
            idempotent=True,
        )
        blocks = {b["block_id"]: _clean(b) for b in converted.get("blocks") or []}
        for i, (ids, desc) in enumerate(_batches(converted.get("first_level_block_ids") or [], blocks)):
            if i:
                client.sleep(0.4)  # 单篇文档编辑每秒最多 3 次
            client.post(  # client_token 去重：超时后重试不会把同一批块插入两次
                f"/open-apis/docx/v1/documents/{document_id}/blocks/{document_id}/descendant",
                {"children_id": ids, "descendants": desc, "index": -1},
                idempotent=True,
                client_token=str(uuid.uuid4()),
            )
    return document_id


def member_type(user: str) -> str:
    user = user.strip()
    if "@" in user:
        return "email"
    if user.startswith("ou_"):
        return "openid"
    if user.startswith("on_"):
        return "unionid"
    return "userid"


def share_with(client: FeishuClient, token: str, user: str, doc_type: str = "docx", perm: str = "full_access") -> None:
    """把用户加为文档协作者（应用新建的文档默认只有应用自己能看到）。"""
    client.post(
        f"/open-apis/drive/v1/permissions/{token}/members",
        {"member_type": member_type(user), "member_id": user.strip(), "perm": perm, "type": "user"},
        type=doc_type,
        need_notification="false",
    )


def document_url(client: FeishuClient, token: str, doc_type: str = "docx") -> str:
    """文档访问链接：优先用接口返回的（带企业域名），取不到时按配置拼。"""
    try:
        data = client.post(
            "/open-apis/drive/v1/metas/batch_query",
            {"request_docs": [{"doc_token": token, "doc_type": doc_type}], "with_url": True},
            idempotent=True,  # 查询
        )
        metas = data.get("metas") or []
        if metas and metas[0].get("url"):
            return metas[0]["url"]
    except FeishuError:
        pass
    return doc_url(token, doc_type)


def document_meta(client: FeishuClient, token: str, doc_type: str = "docx") -> dict:
    """最后编辑人、最后编辑时间（归属判断用）；没有权限时返回空。"""
    try:
        data = client.post(
            "/open-apis/drive/v1/metas/batch_query",
            {"request_docs": [{"doc_token": token, "doc_type": doc_type}]},
            idempotent=True,  # 查询
            user_id_type="open_id",
        )
    except FeishuError:
        return {}
    metas = data.get("metas") or []
    if not metas:
        return {}
    m = metas[0]
    out = {}
    if m.get("latest_modify_user"):
        out["last_modified_by_id"] = m["latest_modify_user"]
    if m.get("latest_modify_time"):
        out["modified"] = m["latest_modify_time"]
    if m.get("owner_id"):
        out["owner_id"] = m["owner_id"]
    return out
