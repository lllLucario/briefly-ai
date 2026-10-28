""".doc实验用XHTML结构转换；不把生成摘要当原文。"""
import re
import xml.etree.ElementTree as ET

from briefly.tables import merged_value


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
    def table(node):
        rows=[r for r in node.iter() if tag(r)=="tr"]
        occupied={}
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
                content=plain(cell).replace("|","\\|")
                for rr in range(r,r+rs):
                    for cc in range(col,col+cs):
                        occupied[rr,cc]=merged_value(content,rr,cc,r,col,r==0 or tag(cell)=="th") or ""
                col+=cs
                width=max(width,col)
        if not rows:
            return ""
        lines=["| "+" | ".join(occupied.get((r,c),"") for c in range(width))+" |" for r in range(len(rows))]
        return "\n".join([lines[0],"| "+" | ".join("---" for _ in range(width))+" |",*lines[1:]])
    def render(node):
        kind=tag(node)
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


def normalized(text):
    return re.sub(r"\s+","",text)
