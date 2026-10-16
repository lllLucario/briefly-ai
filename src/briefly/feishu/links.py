"""识别飞书链接：文档（docx）、知识库页面（wiki）、多维表格（base）、妙记（minutes）。"""
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

DOCX, WIKI, BITABLE, MINUTES, FOLDER = "docx", "wiki", "bitable", "minutes", "folder"
_HOSTS = ("feishu.cn", "larksuite.com", "larkoffice.com", "feishu.net")
_KINDS = {"docx": DOCX, "wiki": WIKI, "base": BITABLE, "minutes": MINUTES, "folder": FOLDER}  # 文件夹：/drive/folder/<token>，只能追踪
_UNSUPPORTED = {
    "docs": "旧版飞书文档（/docs/）不支持，请在飞书中升级为新版文档后再粘贴链接",
    "sheets": "飞书电子表格暂不支持，可以导出为 Excel 后上传",
    "mindnotes": "思维笔记不支持",
    "file": "云空间中的普通文件请下载后上传",
    "slides": "幻灯片不支持",
}


@dataclass(frozen=True)
class FeishuLink:
    kind: str  # docx / wiki / bitable / minutes / folder
    token: str
    table_id: str = ""  # 多维表格：链接里的 ?table=
    view_id: str = ""  # 多维表格：链接里的 ?view=
    url: str = ""


def looks_like_feishu(text: str) -> bool:
    try:
        host = urlparse(text.strip()).hostname or ""
    except ValueError:
        return False
    return any(host == h or host.endswith("." + h) for h in _HOSTS)


def parse_link(url: str) -> FeishuLink:
    """解析飞书链接；不是飞书链接或类型不支持时抛出 ValueError（中文说明）。"""
    url = url.strip()
    if not looks_like_feishu(url):
        raise ValueError(f"不是飞书链接：{url[:100]}")
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    for i, part in enumerate(parts[:-1]):
        if part in _UNSUPPORTED:
            raise ValueError(_UNSUPPORTED[part])
        if part in _KINDS:
            token = parts[i + 1]
            if not re.fullmatch(r"[A-Za-z0-9]{10,64}", token):
                raise ValueError(f"链接中的文档标识不完整：{token}")
            query = parse_qs(parsed.query)
            return FeishuLink(
                _KINDS[part],
                token,
                table_id=(query.get("table") or [""])[0],
                view_id=(query.get("view") or [""])[0],
                url=url,
            )
    raise ValueError("无法识别的飞书链接：支持新版文档（/docx/）、知识库页面（/wiki/）、多维表格（/base/）和妙记（/minutes/）")


def find_links(text: str) -> list[str]:
    """从一段文字里找出飞书链接（对话中用户直接粘贴时用）。"""
    found = []
    for m in re.finditer(r"https?://[^\s<>\"'，。；、）)】]+", text):
        if looks_like_feishu(m.group(0)) and m.group(0) not in found:
            found.append(m.group(0))
    return found
