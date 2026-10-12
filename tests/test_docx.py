"""docx 解析（docling）：合并单元格、文本框、页眉页脚、脚注。"""
import re
import zipfile
from io import BytesIO

from docx import Document

from briefly.loaders import load_document


def to_bytes(doc) -> bytes:
    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


def table_doc(rows: list[tuple[str, ...]]):
    doc = Document()
    table = doc.add_table(rows=len(rows), cols=len(rows[0]))
    for r, row in enumerate(rows):
        for c, value in enumerate(row):
            table.cell(r, c).text = value
    return doc, table


def test_vertical_merge_fills_down_and_horizontal_merge_keeps_first_cell():
    doc, table = table_doc([("任务", "负责人", "截止"), ("支付改造", "李经理", "10-20"), ("压测", "", "10-18"), ("导出", "", "")])
    table.cell(1, 1).merge(table.cell(2, 1))  # 两个任务共用负责人（单元格里会有两段文字）
    table.cell(3, 1).merge(table.cell(3, 2)).text = "我（时间待定）"
    text = load_document("a.docx", to_bytes(doc)).text
    assert "| 压测 | 李经理 | 10-18 |" in text
    assert "| 导出 | 我（时间待定） |  |" in text  # 不复制进“截止”列
    assert text.count("李经理") == 2  # 多段文字的单元格不在表格后重复输出


def test_header_row_horizontal_merge_is_repeated():
    doc, table = table_doc([("工时", "", "任务"), ("计划", "实际", ""), ("8", "10", "支付")])
    table.cell(0, 0).merge(table.cell(0, 1)).text = "工时"
    text = load_document("a.docx", to_bytes(doc)).text
    assert text.splitlines()[0] == "| 工时 | 工时 | 任务 |"


def _with_textbox_and_footnote() -> bytes:
    """python-docx 不支持脚注和文本框，直接改 XML。"""
    doc = Document()
    doc.sections[0].header.paragraphs[0].text = "页眉：内部资料"
    doc.sections[0].footer.paragraphs[0].text = "页脚：第 1 页"
    doc.add_paragraph("正文引用了一个数据FNREF。")
    doc.add_paragraph("TEXTBOX")
    src = zipfile.ZipFile(BytesIO(to_bytes(doc)))
    out = BytesIO()
    z = zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED)
    for item in src.infolist():
        data = src.read(item.filename)
        if item.filename == "word/document.xml":
            s = data.decode().replace(
                "FNREF。</w:t></w:r>",
                '</w:t></w:r><w:r><w:footnoteReference w:id="1"/></w:r><w:r><w:t>。</w:t></w:r>',
            )
            box = (
                '<w:r><w:pict><v:shape xmlns:v="urn:schemas-microsoft-com:vml"><v:textbox><w:txbxContent>'
                "<w:p><w:r><w:t>文本框里的关键数据：预算 30 万</w:t></w:r></w:p>"
                "</w:txbxContent></v:textbox></v:shape></w:pict></w:r>"
            )
            data = re.sub(r"<w:r>(?:(?!<w:r>).)*?<w:t>TEXTBOX</w:t></w:r>", box, s).encode()
        elif item.filename == "word/_rels/document.xml.rels":
            data = data.replace(
                b"</Relationships>",
                b'<Relationship Id="rIdFn" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                b'relationships/footnotes" Target="footnotes.xml"/></Relationships>',
            )
        elif item.filename == "[Content_Types].xml":
            data = data.replace(
                b"</Types>",
                b'<Override PartName="/word/footnotes.xml" ContentType="application/'
                b'vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"/></Types>',
            )
        z.writestr(item, data)
    z.writestr(
        "word/footnotes.xml",
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>'
        '<w:footnote w:id="1"><w:p><w:r><w:t>数据来源为 2026 年财务报表</w:t></w:r></w:p></w:footnote>'
        "</w:footnotes>",
    )
    z.close()
    return out.getvalue()


def test_textbox_and_footnotes_are_kept_header_footer_dropped():
    doc = load_document("a.docx", _with_textbox_and_footnote())
    assert "文本框里的关键数据：预算 30 万" in doc.text
    assert doc.text.endswith("脚注：\n[1] 数据来源为 2026 年财务报表")
    assert doc.report["footnotes"] == 1
    assert "页眉" not in doc.text and "页脚" not in doc.text
