"""大DOC内容/父表范围核验；区分正文、目录页码、文本框及未确认区域。"""
import argparse
from collections import Counter
import json
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile
import re

from doc_real_probe import norm
from doc_parser_comparison import save

W='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder',type=Path);args=parser.parse_args();folder=args.folder.resolve()
    with ZipFile('files/eval/w3/doc-real/reference/12/input.docx') as archive:
        reference=ET.fromstring(archive.read('word/document.xml'))
    native=ET.fromstring((folder/'final-native/0.xhtml').read_bytes())
    text=(folder/'short-temp-comparison/direct-1/output.md').read_text(encoding='utf-8')
    normalized=norm(text)
    no_parentheses=normalized.replace('(','').replace(')','')
    parents={child:parent for parent in reference.iter() for child in parent}
    native_parents={child:parent for parent in native.iter() for child in parent}
    def ancestors(node,mapping):
        while node in mapping:
            node=mapping[node];yield node
    records=[]
    for p in reference.iter(W+'p'):
        # Nested textbox/table paragraphs belong to themselves, never concatenate into their anchor paragraph.
        own=''.join(t.text or '' for t in p.iter(W+'t') if next((a for a in ancestors(t,parents) if a.tag==W+'p'),None) is p)
        if len(norm(own))<8:continue
        style=p.find(W+'pPr/'+W+'pStyle');style=style.get(W+'val','') if style is not None else ''
        category='textbox' if any(a.tag==W+'txbxContent' for a in ancestors(p,parents)) else ('toc' if style.startswith('TOC') else 'body')
        exact=norm(own) in normalized
        label=norm(re.sub(r'\d+\s*$','',own)) if category=='toc' else ''
        glyph=norm(own).replace('(','').replace(')','') in no_parentheses if category=='body' else False
        records.append(dict(text=own,category=category,exact=exact,
                            toc_label_without_regenerated_page=bool(label and label in normalized),
                            punctuation_only_difference=not exact and glyph))
    save(folder/'paragraph-audit.json',dict(records=records,counts={category:dict(total=sum(r['category']==category for r in records),exact=sum(r['category']==category and r['exact'] for r in records),
        toc_label=sum(r['category']==category and (r['exact'] or r['toc_label_without_regenerated_page']) for r in records),
        punctuation_only=sum(r['category']==category and r['punctuation_only_difference'] for r in records)) for category in ['body','toc','textbox']}))
    def tag(node):return node.tag.rsplit('}',1)[-1]
    ref_roots=[t for t in reference.iter(W+'tbl') if not any(a.tag==W+'tbl' for a in ancestors(t,parents))]
    native_roots=[t for t in native.iter() if tag(t)=='table' and not any(tag(a)=='table' for a in ancestors(t,native_parents))]
    def source_cell_text(cell):
        pieces=[]
        for p in cell.iter(W+'p'):
            if next((a for a in ancestors(p,parents) if a.tag==W+'tc'),None) is not cell:continue
            pieces.append(''.join(t.text or '' for t in p.iter(W+'t') if next((a for a in ancestors(t,parents) if a.tag==W+'p'),None) is p))
        return ' '.join(pieces)
    def native_cell_text(cell):
        parts=[]
        def walk(n):
            if tag(n)=='table':return
            if n.text:parts.append(n.text)
            for c in n:
                walk(c)
                if c.tail:parts.append(c.tail)
        walk(cell);return norm(' '.join(parts))
    rows=[];hierarchy=[]
    if len(ref_roots)==len(native_roots):
        for index,(left,right) in enumerate(zip(ref_roots,native_roots),1):
            native_rows=[]
            for child in right:
                if tag(child)=='tr':native_rows.append(child)
                elif tag(child) in {'tbody','thead','tfoot'}:native_rows.extend(c for c in child if tag(c)=='tr')
            native_strings=[' '.join(native_cell_text(c) for c in row if tag(c) in {'td','th'}) for row in native_rows]
            ref_children=sum(1 for t in left.iter(W+'tbl'))-1
            native_children=sum(1 for t in right.iter() if tag(t)=='table')-1
            hierarchy.append(dict(root=index,reference_children=ref_children,native_children=native_children))
            for tr in left.findall(W+'tr'):
                cells=[norm(source_cell_text(c)) for c in tr.findall(W+'tc')]
                cells=[c for c in cells if len(c)>=2]
                if len(cells)<2:continue
                rows.append(dict(root=index,cells=cells,found_within_same_parent_table=any(all(c in row for c in cells) for row in native_strings)))
    save(folder/'table-audit.json',dict(reference_roots=len(ref_roots),native_roots=len(native_roots),hierarchy=hierarchy,rows=rows,
        limitations='Root sequence and parent table scope checked; row matching is not exact cell-coordinate or visual verification. Only nonempty top-level row text compared; controlled fixtures separately verify nested parent cells and merged ranges.'))
    print('paragraph categories',Counter(r['category'] for r in records))
    print('roots',len(ref_roots),len(native_roots),'rows',sum(r['found_within_same_parent_table'] for r in rows),'/',len(rows))
    print('root nesting differences',[r for r in hierarchy if r['reference_children']!=r['native_children']])


if __name__=='__main__':main()
