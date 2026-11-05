"""本地POI HSLF直接读旧版PowerPoint .ppt；与.doc直读共用Java桥和并发槽，模型和网络不参与。

Java端按Docling pptx后端的阅读顺序（自上而下成行、行内自左向右，组合形状展开）输出每页的标题、段落、
列表、表格、图片数与备注；这里还原成同样标签的DoclingDocument，再走与pptx相同的按页导出与清理。
开启图片文字识别时，Java端另把位图（PNG/JPEG/DIB）各写出一份，按阅读顺序还原成带像素的图片项。
"""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from .errors import UserInputError
from .direct_word import BRIDGE, _paths, _slots
from .legacy_word import OLE_HEADER, _run

MAX_RESULT_BYTES = 50 * 1024 * 1024
ADAPTER_VERSION = 2


class DirectSlidesUnsupported(UserInputError):
    """加密、PowerPoint 95、结构损坏或内存不足等直读不了的情况；调用方改走LibreOffice转换。"""


def parse_ppt(data: bytes):
    """返回 Docling 文档；直读不了时抛 DirectSlidesUnsupported（可转换）或 ValueError。"""
    if not data.startswith(OLE_HEADER):
        raise UserInputError("文件不是可识别的旧版 PowerPoint .ppt 文档；仅更改后缀不能转换文件格式")
    from .ocr import enabled

    java, jar = _paths()
    pictures = ["-Dbriefly.pictures=true"] if enabled() else []
    if not _slots.acquire(timeout=5):
        raise UserInputError("旧版文档直接解析任务繁忙，请稍后重试")
    try:
        with TemporaryDirectory(prefix="briefly-ppt-direct-") as temporary:
            folder = Path(temporary)
            (folder/"input-0.ppt").write_bytes(data)
            try:
                _run([java, "-Xmx512m", "-XX:ActiveProcessorCount=4", "-Dfile.encoding=UTF-8", "-Djava.awt.headless=true",
                      *pictures, "-cp", jar + os.pathsep + str(BRIDGE), "LegacyPptParser", str(folder/"output"),
                      str(folder/"input-0.ppt")], action="直接解析", label="旧版 .ppt")
            except ValueError as exc:  # 超时或Java异常退出（如图片过多内存不足）
                raise DirectSlidesUnsupported(str(exc)) from exc
            target = folder/"output"/"0"
            if (target/"error.code").is_file():
                if (target/"error.code").read_text(encoding="utf-8").strip() == "unsupported_structure":
                    raise DirectSlidesUnsupported("演示文稿已加密或为PowerPoint 95等早期格式")
                raise DirectSlidesUnsupported("直接解析失败，文件结构可能损坏")
            output = target/"slides.json"
            if not output.is_file() or not 0 < output.stat().st_size <= MAX_RESULT_BYTES:
                raise DirectSlidesUnsupported("直接解析没有有效结果，或结果超过50MB")
            return slide_documents(json.loads(output.read_text(encoding="utf-8")), target/"pictures")
    finally:
        _slots.release()


def slide_documents(value: dict, pictures: Path | None = None) -> list:
    """按 Docling pptx 后端的标签规则重建，每页一份文档（页码都是 1）：标题占位符→TITLE，其他段落→PARAGRAPH，
    项目符号/编号→列表，表格按合并跨度建格，隐藏页内容放到不可见层，备注放到 NOTES 层。
    导出了像素的图片（pic 项，文件在 pictures 目录）放在阅读顺序里，其余图片只计数，补在页末。"""
    from docling_core.types.doc import (BoundingBox, ContentLayer, DocItemLabel, DoclingDocument, GroupLabel,
                                        ImageRef, ProvenanceItem, Size, TableCell, TableData)
    from PIL import Image

    def prov(text):
        return ProvenanceItem(page_no=1, bbox=BoundingBox(l=0, t=0, r=0, b=0), charspan=(0, len(text)))

    docs = []
    for n, slide in enumerate(value["slides"], start=1):
        doc = DoclingDocument(name=f"slide-{n}")
        doc.add_page(page_no=1, size=Size(width=720, height=540))
        docs.append(doc)
        parent = doc.add_group(name=f"slide-{n - 1}", label=GroupLabel.CHAPTER)
        layer = ContentLayer.INVISIBLE if slide["hidden"] else ContentLayer.BODY
        lists = []  # 打开中的列表（外层在前）：[组, 层级, 编号计数, 最后一项]
        placed = 0
        for item in slide["items"]:
            kind, text = item["t"], item.get("x", "")
            if kind != "li":
                lists.clear()
            if kind == "pic":
                path = pictures / Path(item["f"]).name if pictures else None
                try:
                    with Image.open(path) as opened:
                        image = ImageRef.from_pil(image=opened.convert("RGBA") if opened.mode == "P" else opened.copy(), dpi=72)
                except Exception:  # 文件缺失、格式不认识：只计数
                    continue
                doc.add_picture(parent=parent, image=image, prov=prov(""), content_layer=layer)
                placed += 1
            elif kind == "title":
                doc.add_title(text=text, parent=parent, prov=prov(text), content_layer=layer)
            elif kind == "p":
                doc.add_text(label=DocItemLabel.PARAGRAPH, text=text, parent=parent, prov=prov(text), content_layer=layer)
            elif kind == "li":
                level = item["lv"]
                while len(lists) > 1 and lists[-1][1] > level:
                    lists.pop()
                if not lists or level > lists[-1][1]:
                    owner = lists[-1][3] if lists else parent
                    lists.append([doc.add_list_group(name="list", parent=owner, content_layer=layer), level, 0, None])
                current, marker = lists[-1], ""
                if item["num"]:
                    current[2] = (current[2] or item["start"] - 1) + 1
                    marker = f"{current[2]}."
                current[3] = doc.add_list_item(text=text, enumerated=item["num"], marker=marker, parent=current[0],
                                               prov=prov(text), content_layer=layer)
            elif kind == "table":
                rows = item["rows"]
                cells = [TableCell(text=cell["x"], row_span=cell["rs"], col_span=cell["cs"],
                                   start_row_offset_idx=r, end_row_offset_idx=r + cell["rs"],
                                   start_col_offset_idx=c, end_col_offset_idx=c + cell["cs"],
                                   column_header=r == 0, row_header=False)
                         for r, row in enumerate(rows) for c, cell in enumerate(row) if cell["x"]]
                if cells:
                    doc.add_table(data=TableData(num_rows=len(rows), num_cols=max(map(len, rows)), table_cells=cells),
                                  parent=parent, prov=prov(""), content_layer=layer)
        for _ in range(slide["pictures"] - placed):
            doc.add_picture(parent=parent, prov=prov(""), content_layer=layer)
        for note in slide["notes"]:
            doc.add_text(label=DocItemLabel.TEXT, text=note, parent=parent, prov=prov(note), content_layer=ContentLayer.NOTES)
    return docs
