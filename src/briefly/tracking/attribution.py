"""工作归属：判断一处文件变化是否可能属于当前用户，并记下判断依据。

证据强弱：
- DOCX 修订记录（Track Changes：w:ins / w:del 的作者、时间）：作者与用户署名一致 → ATTRIBUTED。
- 文档属性“最后修改人”（docx / xlsx 的 docProps/core.xml）、飞书文档的最后编辑人（云文档元数据）：
  只说明最后一次保存的人，可能被别人覆盖 → 最多 INFERRED。
- 追踪时间、文件修改时间、首次发现时间：不作为归属依据。
- 用户确认 → USER_CONFIRMED；用户排除 → EXCLUDED（在 review.py 中设置）。
没有可靠身份来源的变化一律 UNKNOWN，进入候选清单等用户确认。
"""
import zipfile
from io import BytesIO
from xml.etree import ElementTree

from .models import ATTRIBUTED, INFERRED, UNKNOWN

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
CORE = {
    "creator": "{http://purl.org/dc/elements/1.1/}creator",
    "last_modified_by": "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}lastModifiedBy",
    "modified": "{http://purl.org/dc/terms/}modified",
    "revision": "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}revision",
}
MAX_REVISIONS = 500
MAX_XML_BYTES = 50 * 1024 * 1024  # 解压后的 XML 上限，防止压缩炸弹


def _read_xml(z: zipfile.ZipFile, name: str):
    try:
        info = z.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_XML_BYTES:
        raise ValueError(f"{name} 过大")
    return ElementTree.fromstring(z.read(name))


def extract_meta(kind: str, data: bytes) -> dict:
    """采集快照时读取归属相关的元数据：文档属性；DOCX 另读修订记录。其他类型返回空。"""
    if kind not in ("docx", "xlsx"):
        return {}
    with zipfile.ZipFile(BytesIO(data)) as z:
        meta: dict = {}
        core = _read_xml(z, "docProps/core.xml")
        if core is not None:
            for key, tag in CORE.items():
                node = core.find(tag)
                if node is not None and (node.text or "").strip():
                    meta[key] = node.text.strip()
        if kind == "docx":
            body = _read_xml(z, "word/document.xml")
            if body is not None:
                meta["revisions"] = _revisions(body)
    return meta


def _revisions(root) -> list[dict]:
    """Word 修订记录：插入、删除、移动、格式修改，各取作者、时间和文字（截断）。"""
    out = []
    kinds = {"ins": "insert", "del": "delete", "moveTo": "move", "moveFrom": "move", "rPrChange": "format", "pPrChange": "format"}
    for tag, kind in kinds.items():
        for node in root.iter(f"{W}{tag}"):
            text_tag = "delText" if tag in ("del", "moveFrom") else "t"
            text = "".join(t.text or "" for t in node.iter(f"{W}{text_tag}"))
            out.append(
                {
                    "type": kind,
                    "author": node.get(f"{W}author", ""),
                    "date": node.get(f"{W}date", ""),
                    "text": text[:200],
                }
            )
            if len(out) >= MAX_REVISIONS:
                return out
    return out


def _norm(name: str) -> str:
    return "".join(name.split()).casefold()


def _key(r: dict) -> tuple:
    return (r.get("type"), r.get("author"), r.get("date"), r.get("text"))


def judge(owner_names: list[str], before: dict | None, after: dict) -> tuple[str, list[str], list[str]]:
    """比较前后两个版本的元数据，返回 (归属状态, 依据, 标记)。

    标记：others（有他人的修订）、mixed（你和他人都有修订）、no_owner（未设置署名）。
    """
    owners = {_norm(n) for n in owner_names if n.strip()}
    basis: list[str] = []
    flags: list[str] = []
    if not owners:
        flags.append("no_owner")
        basis.append("未设置你在文档中的署名（briefly track create/owner --owner 名字），无法用作者信息判断")

    old = {_key(r) for r in (before or {}).get("revisions", [])}
    new = [r for r in after.get("revisions", []) if _key(r) not in old and r.get("type") != "format"]
    if new:
        authors = {r["author"] for r in new if r.get("author")}
        mine = {a for a in authors if _norm(a) in owners}
        others = authors - mine
        if mine and not others:
            basis.append(f"新增修订 {len(new)} 处，作者均为 {'、'.join(sorted(mine))}（Word 修订记录）")
            return ATTRIBUTED, basis, flags
        if mine:
            n_other = sum(1 for r in new if r.get("author") in others)
            basis.append(
                f"新增修订 {len(new)} 处：{'、'.join(sorted(mine))} 的修订可归属给你；"
                f"另有 {n_other} 处作者为 {'、'.join(sorted(others))}（他人），请确认描述只包含你的部分"
            )
            flags += ["mixed", "others"]
            return ATTRIBUTED, basis, flags
        if others:
            basis.append(f"新增修订 {len(new)} 处，作者为 {'、'.join(sorted(others))}，不是你的署名，可能是他人的工作")
            flags.append("others")
            return UNKNOWN, basis, flags
        basis.append(f"新增修订 {len(new)} 处，但没有作者信息")

    last = after.get("last_modified_by", "")
    label = "飞书文档的“最后编辑人”" if "last_modified_by_id" in after else "文档属性“最后修改人”"
    if last:
        # 飞书文档的最后编辑人可能只有 open_id（应用没有通讯录权限时），署名里写 open_id 也能匹配
        if owners and (_norm(last) in owners or _norm(after.get("last_modified_by_id", "")) in owners):
            basis.append(f"{label}为 {last}（只说明最后一次保存的人，不能证明全部修改都是你做的）")
            return INFERRED, basis, flags
        basis.append(f"{label}为 {last}" + ("，不是你的署名" if owners else ""))
        if owners:
            flags.append("others")
        return UNKNOWN, basis, flags
    basis.append("文件没有可用的作者或修订信息；修改时间和追踪时间不能说明是谁做的")
    return UNKNOWN, basis, flags
