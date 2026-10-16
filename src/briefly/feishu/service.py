"""飞书与材料、周报的衔接：粘贴链接导入为材料、周报写入飞书文档（并记录写入历史）。"""
import re
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, select
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from .. import config
from ..db import Base
from ..materials import Material, MaterialStore
from .bitable import read_table
from .client import FeishuClient, FeishuError
from .docx import create_document, document_meta, document_url, read_document, share_with
from .links import BITABLE, DOCX, FOLDER, MINUTES, WIKI, FeishuLink, parse_link
from .minutes import read_minutes

# 周报的栏目行（【本周完成】）写成飞书文档的二级标题
_SECTION = re.compile(r"(?m)^【([^】\n]+)】(.*)$")
FEISHU_SOURCE = "feishu"  # 材料来源
DOC_TYPE, TABLE_TYPE, MINUTES_TYPE = "feishu", "bitable", "minutes"  # 材料类型


@dataclass
class Fetched:
    kind: str  # docx / bitable
    token: str  # 实际的文档 token（知识库链接已换成文档本身的 token）
    title: str
    markdown: str
    url: str
    revision_id: int | None = None
    warnings: list[str] = field(default_factory=list)


def resolve(client: FeishuClient, link: FeishuLink) -> FeishuLink:
    """知识库链接换成实际的文档或多维表格。"""
    if link.kind != WIKI:
        return link
    node = client.get("/open-apis/wiki/v2/spaces/get_node", token=link.token).get("node") or {}
    obj_type, obj_token = node.get("obj_type"), node.get("obj_token")
    if obj_type == "docx":
        return FeishuLink(DOCX, obj_token, url=link.url)
    if obj_type == "bitable":
        return FeishuLink(BITABLE, obj_token, link.table_id, link.view_id, link.url)
    names = {"doc": "旧版文档", "sheet": "电子表格", "mindnote": "思维笔记", "file": "文件", "slides": "幻灯片"}
    raise ValueError(f"知识库页面是{names.get(obj_type, obj_type or '未知类型')}，目前只支持新版文档和多维表格")


def fetch(client: FeishuClient, url: str) -> Fetched:
    link = resolve(client, parse_link(url))
    if link.kind == FOLDER:
        raise ValueError("文件夹不能直接导入：可以把整个文件夹加入周报计划持续追踪，或展开后勾选其中的文档导入")
    if link.kind == DOCX:
        doc = read_document(client, link.token)
        return Fetched(DOCX, link.token, doc.title, doc.markdown, url, doc.revision_id, doc.warnings)
    if link.kind == MINUTES:
        m = read_minutes(client, link.token)
        return Fetched(MINUTES, link.token, m.title, m.text, url, None, m.warnings)
    table = read_table(client, link.token, link.table_id, link.view_id)
    return Fetched(BITABLE, link.token, table.title, table.markdown, url, None, table.warnings)


def import_link(client: FeishuClient, store: MaterialStore, url: str) -> tuple[Material, bool]:
    """读取飞书文档、多维表格或妙记文字稿的当前内容，存为材料（内容相同的不重复存）。"""
    got = fetch(client, url)
    if not got.markdown.strip() or (got.kind == MINUTES and got.warnings):
        raise ValueError(got.warnings[0] if got.warnings else f"《{got.title}》没有可读取的文字内容")
    is_markdown = got.kind != MINUTES  # 妙记文字稿是纯文本（说话人 + 时间戳），章节按规则识别
    report = {
        "structure": "markdown" if is_markdown else "",
        "feishu": {"kind": got.kind, "token": got.token, "url": got.url, "revision_id": got.revision_id,
                   "fetched_at": datetime.now().isoformat(timespec="seconds")},
    }  # fmt: skip
    if got.warnings:
        report["warnings"] = got.warnings
    kind = {DOCX: DOC_TYPE, BITABLE: TABLE_TYPE, MINUTES: MINUTES_TYPE}[got.kind]
    return store.add_text(
        got.title, got.markdown, source=FEISHU_SOURCE, kind=kind, markdown=True if is_markdown else None, report=report
    )


class FeishuExport(Base):
    """写入飞书的记录：哪份周报、写成了哪篇文档。"""

    __tablename__ = "feishu_exports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_id: Mapped[int] = mapped_column(Integer, index=True)
    document_id: Mapped[str] = mapped_column(String(64))
    url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(String(200))
    shared_with: Mapped[str] = mapped_column(String(200), default="")
    share_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


def export_weekly(client: FeishuClient, weekly, report_id: int, me: str = "") -> FeishuExport:
    """把周报（去掉出处标记的正文）写成一篇新的飞书文档，并把用户加为协作者。

    me：已连接飞书账号时用户的 open_id。文档以用户身份新建时用户就是所有者，不用再加协作者。
    """
    report = weekly.report(report_id)
    _, text = weekly.export_markdown(report_id)
    text = _SECTION.sub(lambda m: f"## {m.group(1)}" + (f"\n\n{m.group(2).strip()}" if m.group(2).strip() else ""), text)
    name = (report.inputs or {}).get("name")
    title = f"{name + ' ' if name else ''}周报 {report.period_start:%m.%d}–{report.period_end:%m.%d}"
    document_id = create_document(client, title, text, config.FEISHU_FOLDER_TOKEN)
    shared, error = "", ""
    owner = document_meta(client, document_id).get("owner_id", "") if me else ""
    if me and owner == me:
        shared = "你（文档所有者）"
    elif config.FEISHU_NOTIFY_USER:
        try:
            share_with(client, document_id, config.FEISHU_NOTIFY_USER)
            shared = config.FEISHU_NOTIFY_USER
        except FeishuError as e:
            error = str(e)
            if e.code == 1063001:  # 实测：填普通邮箱（如 QQ 邮箱）时飞书找不到这个用户
                error = (
                    "文档已写入，但没能把你加为协作者：FEISHU_NOTIFY_USER 要填飞书 open_id（ou_ 开头）"
                    f"或飞书邮箱，普通邮箱飞书找不到对应用户（{e}）"
                )
    elif not config.FEISHU_FOLDER_TOKEN:
        error = "未设置 FEISHU_NOTIFY_USER 或 FEISHU_FOLDER_TOKEN：文档在应用自己的空间里，你可能看不到"
    export = FeishuExport(
        report_id=report_id,
        document_id=document_id,
        url=document_url(client, document_id),
        title=title,
        shared_with=shared,
        share_error=error,
    )
    with sessionmaker(weekly.store.engine, expire_on_commit=False)() as s:
        s.add(export)
        s.commit()
    return export


def exports_of(engine, report_id: int) -> list[FeishuExport]:
    with sessionmaker(engine, expire_on_commit=False)() as s:
        return list(s.scalars(select(FeishuExport).where(FeishuExport.report_id == report_id).order_by(FeishuExport.id.desc())))
