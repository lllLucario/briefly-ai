"""POI HTML只作同HWPF调试参照：检查层级，不将输出表数当成功标准。"""
import argparse,json
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('folder',type=Path);args=parser.parse_args();folder=args.folder.resolve()
    reference=folder/'poi-html';root=ET.fromstring((reference/'full.xhtml').read_bytes())
    parents={c:p for p in root.iter() for c in p}
    def ancestors(n):
        while n in parents:n=parents[n];yield n
    tables=list(root.iter('table'))
    roots=[t for t in tables if not any(a.tag=='table' for a in ancestors(t))]
    promoted=[dict(start=int(t.get('data-probe-source-start')),end=int(t.get('data-probe-source-end')),
                   source_level=int(t.get('data-probe-source-level'))) for t in roots if int(t.get('data-probe-source-level','0'))>1]
    local=[]
    for path in sorted(reference.glob('local-*/output.xhtml')):
        node=ET.fromstring(path.read_bytes())
        local.append(dict(source_start=int(path.parent.name.split('-')[1]),status=(path.parent/'status.txt').read_text(),
            html_tables=len(list(node.iter('table'))),html_rows=len(list(node.iter('tr'))),html_cells=len(list(node.iter('td'))),text=''.join(node.itertext())))
    with ZipFile('files/eval/w3/doc-real/reference/12/input.docx') as archive:xml=ET.fromstring(archive.read('word/document.xml'))
    w='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}';xp={c:p for p in xml.iter() for c in p}
    def xa(n):
        while n in xp:n=xp[n];yield n
    xmltables=list(xml.iter(w+'tbl'));xmlroots=[n for n in xmltables if not any(a.tag==w+'tbl' for a in xa(n))]
    targets={25283,25434,25446,25495,25518,25528}
    summary=dict(full_converter_status=(reference/'full.status').read_text(),same_hwpf_model=True,independent_fallback=False,
        html_tables=len(tables),html_roots=len(roots),html_nested=len(tables)-len(roots),
        conversion_reference_tables=len(xmltables),conversion_reference_roots=len(xmlroots),conversion_reference_nested=len(xmltables)-len(xmlroots),
        source_nested_tables_promoted_to_root=promoted,
        six_target_local_probes=[r for r in local if r['source_start'] in targets],zero_row_parent_probes=[r for r in local if r['source_start'] not in targets],
        acceptance='NOT recovered: count equality does not establish parent-cell ownership. Local children can be rendered separately, but parent ranges remain unresolved.',
        limits='Both implementations share HWPF. Failures or empty row bounds do not prove original file irrecoverable. No converter output merged into production.')
    (folder/'poi-html-audit.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('HTML roots/nested',len(roots),len(tables)-len(roots),'vs reference',len(xmlroots),len(xmltables)-len(xmlroots))
    print('promoted source nested',promoted)
    print('six local children',[(r['source_start'],r['html_rows'],r['html_cells']) for r in local if r['source_start'] in targets])


if __name__=='__main__':main()
