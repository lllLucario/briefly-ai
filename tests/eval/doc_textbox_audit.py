"""独立核对52条文本框未命中；诊断归一化不用于改写正式内容。"""
import argparse
from collections import Counter
import hashlib,json,re,unicodedata
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile


def normalized(text):return re.sub(r'\s+','',unicodedata.normalize('NFKC',text))


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('folder',type=Path);args=parser.parse_args()
    folder=args.folder.resolve();base=Path('files/eval/w3/doc-nested')
    audit=json.loads((base/'paragraph-audit.json').read_text(encoding='utf-8'))
    targets=[r['text'] for r in audit['records'] if r['category']=='textbox' and not r['exact']]
    node=json.loads((folder/'node/api-raw.json').read_text(encoding='utf-8'))
    poi=json.loads((folder/'poi-ranges/ranges-raw.json').read_text(encoding='utf-8'))
    for name,row in poi.items():
        (folder/'poi-ranges'/f'{name}.raw.txt').write_text(row['text'],encoding='utf-8')
        (folder/'poi-ranges'/f'{name}.normalized.txt').write_text(normalized(row['text']),encoding='utf-8')
        (folder/'poi-ranges'/f'{name}.cell-boundary-diagnostic.txt').write_text(normalized(row['text'].replace('\x07',' ')),encoding='utf-8')
    def xhtml_text(path):
        root=ET.fromstring(path.read_bytes());body=next(n for n in root.iter() if n.tag.rsplit('}',1)[-1]=='body')
        return ''.join(body.itertext())
    tika=xhtml_text(base/'raw-tika/0.xhtml')
    bridge=xhtml_text(base/'final-native/0.xhtml')
    markdown=(base/'short-temp-comparison/direct-1/output.md').read_text(encoding='utf-8')
    for name,text in [('tika-body',tika),('bridge-body',bridge),('python-markdown',markdown)]:
        (folder/f'{name}.raw.txt').write_text(text,encoding='utf-8')
        (folder/f'{name}.normalized.txt').write_text(normalized(text),encoding='utf-8')
    results=[]
    for target in dict.fromkeys(targets):
        row=dict(target=target,reference_occurrences=targets.count(target),node={},poi={})
        for name,text in node.items():row['node'][name]=dict(raw_exact=target in text,normalized_occurrences=normalized(text).count(normalized(target)))
        for name,item in poi.items():
            text=item['text'];row['poi'][name]=dict(raw_exact=target in text,normalized_occurrences=normalized(text).count(normalized(target)),
                cell_boundary_diagnostic_occurrences=normalized(text.replace('\x07',' ')).count(normalized(target)))
        tokens=['修订标记','修订处数','修订日期','审批签字']
        pattern='[\\s\ufffd]*'.join(map(re.escape,tokens))
        row['known_control_serialization_pattern']={name:len(re.findall(pattern,text)) for name,text in [('tika',tika),('bridge',bridge),('python',markdown)]}
        row['source_textbox_controls']=dict(cell_marks=poi['textbox']['text'].count('\x07'),replacement_characters=poi['textbox']['text'].count('\ufffd'))
        results.append(row)
    with ZipFile('files/eval/w3/doc-real/reference/12/input.docx') as archive:
        root=ET.fromstring(archive.read('word/document.xml'))
    parents={c:n for n in root.iter() for c in n}
    def ancestors(n):
        while n in parents:n=parents[n];yield n
    w='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
    groups={id(n):i for i,n in enumerate((n for n in root.iter() if n.tag.rsplit('}',1)[-1]=='AlternateContent'),1)}
    branches=[]
    for p in root.iter(w+'p'):
        own=''.join(t.text or '' for t in p.iter(w+'t') if next((a for a in ancestors(t) if a.tag==w+'p'),None) is p)
        if own not in targets:continue
        ac=next((n for n in ancestors(p) if n.tag.rsplit('}',1)[-1]=='AlternateContent'),None)
        branch=next((n.tag.rsplit('}',1)[-1] for n in ancestors(p) if n.tag.rsplit('}',1)[-1] in {'Choice','Fallback'}),'none')
        branches.append(dict(text=own,branch=branch,alternate_content_group=groups.get(id(ac))))
    source=json.loads((folder/'source.json').read_text(encoding='utf-8'))
    assert hashlib.sha256(Path(source['path']).read_bytes()).hexdigest()==source['sha256']
    summary=dict(target_reference_records=len(targets),unique_target_strings=len(set(targets)),
        alternate_content_groups=len({r['alternate_content_group'] for r in branches}),branches=dict(Counter(r['branch'] for r in branches)),
        node_body_textbox_occurrences=results[0]['node']['textboxes_body']['normalized_occurrences'],
        node_header_footer_textbox_occurrences=results[0]['node']['textboxes_header_footer']['normalized_occurrences'],
        poi_body_textbox_cell_boundary_matches=results[0]['poi']['textbox']['cell_boundary_diagnostic_occurrences'],
        poi_header_textbox_chars=poi['header_textbox']['end']-poi['header_textbox']['start'],
        stage='Original body textbox words exist in Node and POI; Tika XHTMLContentHandler substitutes U+0007 with U+FFFD; direct ToXMLContentHandler does not. Prior reference audit double-counted Choice/Fallback.',
        no_node_text_merged_into_production=True,source_unchanged=True,
        limits='Raw API is still internally cleaned. Pattern-local allowance of known U+FFFD boundaries is diagnostic-only; no global deletion or production normalization change. Identical text counts do not establish individual shape identity, reading order, or graph links.')
    for name,value in [('crosscheck.json',results),('reference-branches.json',branches),('summary.json',summary)]:
        (folder/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
