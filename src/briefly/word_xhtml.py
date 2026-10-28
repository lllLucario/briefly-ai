"""本地Word结构化输出转Markdown，沿用表格合并规则。"""
import re
import xml.etree.ElementTree as ET

from .tables import merged_value


def plain(node):
    return " ".join("".join(node.itertext()).split())


def tag(node):
    return node.tag.rsplit("}",1)[-1]


def xhtml_markdown(payload):
    if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
        raise ValueError("不接受XML实体声明")
    root=ET.fromstring(payload)
    body=next((n for n in root.iter() if tag(n)=="body"),None)
    if body is None:
        raise ValueError("解析结果缺少正文")
    warnings=[]
    table_ids={id(n):i for i,n in enumerate((n for n in body.iter() if tag(n)=="table"),1)}
    def table(node,depth=0):
        if depth>16:
            raise ValueError("表格嵌套超过16层")
        rows=[]
        for child in node:
            if tag(child)=="tr":
                rows.append(child)
            elif tag(child) in {"thead","tbody","tfoot"}:
                rows.extend(r for r in child if tag(r)=="tr")
        occupied={}
        children=[]
        cells=0
        width=0
        for r,row in enumerate(rows):
            col=0
            for cell in row:
                if tag(cell) not in {"td","th"}:
                    continue
                while (r,col) in occupied:
                    col+=1
                rs,cs=int(cell.attrib.get("rowspan","1")),int(cell.attrib.get("colspan","1"))
                if not 1<=rs<=1000 or not 1<=cs<=1000:
                    raise ValueError("表格合并跨度无效")
                cells+=rs*cs
                if cells>250000:
                    raise ValueError("表格展开超过25万格，请拆分文档")
                def cell_text(part):
                    if tag(part)=="table":
                        children.append((part,r,col,rs,cs))
                        return f"〔子表 {table_ids[id(part)]}〕"
                    parts=[part.text or ""]
                    for child in part:
                        parts.extend((cell_text(child),child.tail or ""))
                    return " ".join(parts)
                content=" ".join(cell_text(cell).split()).replace("|","\\|")
                for rr in range(r,r+rs):
                    for cc in range(col,col+cs):
                        if (rr,cc) in occupied:
                            raise ValueError("表格合并区域重叠")
                        occupied[rr,cc]=merged_value(content,rr,cc,r,col,r==0 or tag(cell)=="th") or ""
                col+=cs
                width=max(width,col)
        if not rows:
            return ""
        lines=["| "+" | ".join(occupied.get((r,c),"") for c in range(width))+" |" for r in range(len(rows))]
        result="\n".join([lines[0],"| "+" | ".join("---" for _ in range(width))+" |",*lines[1:]])
        for child,r,c,rs,cs in children:
            row_label=str(r+1) if rs==1 else f"{r+1}至{r+rs}"
            col_label=str(c+1) if cs==1 else f"{c+1}至{c+cs}"
            result+=f"\n\n子表 {table_ids[id(child)]}（父表 {table_ids[id(node)]}，第{row_label}行，第{col_label}列）：\n\n"+table(child,depth+1)
        return result
    def render(node):
        kind=tag(node)
        if kind=="div" and node.attrib.get("class")=="briefly-textboxes":
            parts=[]
            for child in node:
                parts.append(render(child))
            return "\n\n---\n\n".join(p for p in parts if p)
        if kind=="table":
            return table(node)
        if kind in {"head","script","style"}:
            return ""
        if kind in {"h1","h2","h3","h4","h5","h6"}:
            return "#"*int(kind[1])+" "+plain(node)
        if kind in {"p","pre"}:
            value=plain(node)
            return "\\"+value if value.startswith("#") else value
        if kind in {"ul","ol"}:
            return "\n".join((f"{i}. " if kind=="ol" else "- ")+plain(child)
                             for i,child in enumerate(node,1) if tag(child)=="li")
        if not len(node):
            return plain(node)
        parts=[]
        if node.text and node.text.strip():
            parts.append(node.text.strip())
        for child in node:
            parts.append(render(child))
            if child.tail and child.tail.strip():
                parts.append(child.tail.strip())
        return "\n\n".join(p for p in parts if p)
    return render(body).strip(),warnings



def xhtml_document(payload):
    try:
        text, _ = xhtml_markdown(payload)
        root = ET.fromstring(payload)
        footnotes = next((int(n.attrib.get("content", "0")) for n in root.iter()
                          if tag(n) == "meta" and n.attrib.get("name") == "briefly:footnotes"), 0)
        return text, footnotes
    except (ET.ParseError, TypeError, OverflowError) as exc:
        raise ValueError("旧Word直接解析返回了无效结构") from exc


def xhtml_structure_report(payload):
    root=ET.fromstring(payload)
    meta={n.attrib.get("name"):n.attrib.get("content", "0") for n in root.iter() if tag(n)=="meta"}
    report={}
    for label,key in (("tables","briefly:tables"),("nested_tables","briefly:nested-tables"),("restored_source_paragraphs","briefly:restored-source-paragraphs"),
                      ("unverified_table_segments","briefly:unverified-table-segments"),("object_anchors","briefly:object-anchors")):
        value=int(meta.get(key,"0"))
        if value<0 or value>1000000:
            raise ValueError("旧Word结构计数无效")
        report[label]=value
    warnings=[]
    if report['unverified_table_segments']:
        warnings.append(f"有{report['unverified_table_segments']}段表格标记缺少可靠行界，已保留为正文；这些段落的行列关系未确认")
        report['table_structure_status']='partial'
    if report['restored_source_paragraphs']:
        report['table_structure_status']='partial'
        report['table_structure_review_reasons']=['original_papx_paragraph_repair']
        warnings.append(f"有{report['restored_source_paragraphs']}处段落按原始表格属性恢复；行列归属仍需核对，不能仅以表格数量判定完整")
    if report['object_anchors']:
        warnings.append("文档含图形或嵌入对象锚点；未执行嵌入对象或识别图片文字，请核对图表及其含义")
    for label,key in (("textbox_ranges","briefly:textbox-ranges"),("textbox_cell_boundaries","briefly:textbox-cell-boundaries"),("textbox_range_boundaries_unverified","briefly:textbox-range-boundaries-unverified")):
        value=int(meta.get(key,"0"))
        if not 0<=value<=1000000:
            raise ValueError("旧Word文本框计数无效")
        if key in meta:
            report[label]=value
    if report.get('textbox_ranges'):
        report['textbox_body_anchor_status']='unbound'
        report['textbox_provenance']={
            'story':'main-textbox','placement':'appendix','body_anchor_status':'unbound',
            'section_attribution_status':'unverified','graph_relationships_status':'unverified',
            'may_infer_section':False,'may_infer_org_chart_relationships':False,
        }
        text,_=xhtml_document(payload)
        start=text.rfind('# 文本框附录（原文正文位置未绑定）')
        if start>=0:
            report['textbox_markdown_range']={'start':start,'end':len(text)}
        warnings.append("正文文本框按原生范围独立列入附录；未绑定正文段落或推断图形连接关系")
        if report.get('textbox_range_boundaries_unverified'):
            warnings.append("文本框分组边界无法确认，仅保留文本框子文档范围")
    if warnings:
        report['warnings']=warnings
    facets={
        'tables':'needs_review' if report.get('table_structure_status')=='partial' else 'not_flagged',
        'textbox_anchors':'unbound' if report.get('textbox_ranges') else 'not_present',
        'graphics':'unverified' if report.get('object_anchors') else 'not_present',
    }
    report['document_quality']={'status':'partial' if any(v in {'needs_review','unbound','unverified'} for v in facets.values()) else 'not_flagged','facets':facets}
    return report
