"""仅测试显式清单中的本地DOC；不扫描/上传正文，不调用模型，不写业务数据库。"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import statistics
import unicodedata
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from briefly import config
from briefly.loaders import load_document
from briefly.direct_word import parse_docs
from briefly.legacy_word import OLE_HEADER, convert_doc
from doc_parser_comparison import measured, save

REPO=Path(__file__).resolve().parents[2]
NS={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}


def norm(text):
    return re.sub(r'\s+','',unicodedata.normalize('NFKC',text)).replace('\\|','|')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder',type=Path)
    parser.add_argument('--reference',action='store_true')
    parser.add_argument('--auto-id')
    args=parser.parse_args()
    folder=args.folder.resolve()
    selected=json.loads((folder/'selected.json').read_text(encoding='utf-8'))
    payloads=[(REPO/row['path']).read_bytes() for row in selected]
    assert len(set(row['path'] for row in selected))==len(selected)
    if args.auto_id:
        config.DOC_PARSER='auto'
        i=next(i for i,row in enumerate(selected) if row['id']==args.auto_id)
        try:
            doc,stats=measured(lambda:load_document(Path(selected[i]['path']).name,payloads[i]))
            (folder/f"auto-{args.auto_id}.md").write_text(doc.text,encoding='utf-8')
            save(folder/f"auto-{args.auto_id}.json",dict(ok=True,stats=stats,chars=len(doc.text),report=doc.report))
            print('auto',args.auto_id,'ok',len(doc.text),stats['seconds'],flush=True)
        except Exception as exc:
            save(folder/f"auto-{args.auto_id}.json",dict(ok=False,error=type(exc).__name__+': '+str(exc)))
            raise
        return
    if not args.reference:
        records=[]
        for repeat in range(1,4):
            parsed,stats=measured(lambda:parse_docs(payloads))
            save(folder/f'batch-{repeat}.json',stats)
            for row,data,item in zip(selected,payloads,parsed):
                record=dict(id=row['id'],repeat=repeat,source_sha256=hashlib.sha256(data).hexdigest(),
                            ole=data.startswith(OLE_HEADER),bytes=len(data))
                if isinstance(item,Exception):
                    record.update(ok=False,error_type=type(item).__name__,error=str(item))
                else:
                    text,report=item
                    target=folder/f"{row['id']}-{repeat}.md"
                    target.write_text(text,encoding='utf-8')
                    record.update(ok=True,chars=len(text),report=report,
                        headings=sum(bool(re.match(r'^#{1,6} ',line)) for line in text.splitlines()),
                        table_lines=sum(line.startswith('|') for line in text.splitlines()),
                        replacement_chars=text.count('\ufffd'),text_sha256=hashlib.sha256(text.encode()).hexdigest())
                records.append(record)
                print(row['id'],repeat,'ok '+str(record.get('chars')) if record['ok'] else record['error_type'],flush=True)
            save(folder/'results.json',records)
        save(folder/'summary.json',dict(selected=len(selected),binary_docs=sum(data.startswith(OLE_HEADER) for data in payloads),
            batches=3,batch_p50_s=statistics.median(json.loads((folder/f'batch-{i}.json').read_text())['seconds'] for i in range(1,4)),
            sources_with_text=sum(r['ok'] for r in records if r['repeat']==1),
            partial_structure_ids=[r['id'] for r in records if r['repeat']==1 and r['ok'] and r['report'].get('table_structure_status')=='partial'],
            warning_ids=[r['id'] for r in records if r['repeat']==1 and r['ok'] and r['report'].get('warnings')],
            quality_claim='Text returned is not lossless structural acceptance; inspect warnings and independent audits.',
            results_stable=all(len({(r['ok'],r.get('text_sha256'),r.get('error_type')) for r in records if r['id']==row['id']})==1 for row in selected),
            external_model_calls=0,business_database_writes=0))
        return
    # LibreOffice另行读取原始DOC并导出DOCX；只解析XML提取参考，不调用慢Docling。
    # 转换是第二种实现，不是人工金标准；只核对正文片段及表格行，不宣布准确率。
    references=[]
    for row,data in zip(selected,payloads):
        if row['id'] not in {'01','04','05','06','07','09','10','12'}:continue
        target=folder/'reference'/row['id']
        target.mkdir(parents=True,exist_ok=True)
        source=target/'input.doc';source.write_bytes(data)
        try:
            converted,stats=measured(lambda:convert_doc(data))
            (target/'input.docx').write_bytes(converted)
            with ZipFile(target/'input.docx') as archive:body=ET.fromstring(archive.read('word/document.xml'))
            paragraphs=[''.join(t.text or '' for t in p.findall('.//w:t',NS)) for p in body.findall('.//w:p',NS)]
            (target/'paragraphs.json').write_text(json.dumps(paragraphs,ensure_ascii=False,indent=2),encoding='utf-8')
            candidates=[text for text in dict.fromkeys(paragraphs) if 8<=len(norm(text))<=160]
            indices=sorted({round(i*(len(candidates)-1)/min(19,len(candidates)-1)) for i in range(min(20,len(candidates)))}) if len(candidates)>1 else list(range(len(candidates)))
            anchors=[candidates[i] for i in indices]
            direct_path=folder/f"{row['id']}-1.md"
            direct=direct_path.read_text(encoding='utf-8') if direct_path.exists() else ''
            rows=[]
            for table in body.findall('.//w:tbl',NS):
                for tr in table.findall('w:tr',NS):
                    cells=[' '.join(t.text or '' for t in tc.findall('.//w:t',NS)) for tc in tr.findall('w:tc',NS)]
                    nonempty=[c for c in cells if len(norm(c))>=2]
                    if len(nonempty)>=2:
                        rows.append(dict(cells=cells,all_cells_on_same_output_row=any(all(norm(cell) in norm(line) for cell in nonempty) for line in direct.splitlines() if line.startswith('|'))))
            reference=dict(id=row['id'],stats=stats,paragraphs=len(paragraphs),tables=len(body.findall('.//w:tbl',NS)),
                reference_chars=sum(len(p) for p in paragraphs),anchors=[dict(text=a,found=norm(a) in norm(direct)) for a in anchors],rows=rows,
                direct_exists=bool(direct),note='Agreement with LibreOffice round-trip, not ground truth; spanning cells/line breaks can cause literal mismatches; empty form cells not validated by row token matching.')
            references.append(reference)
            print('reference',row['id'],'tables',reference['tables'],'anchors',sum(a['found'] for a in reference['anchors']),'/',len(anchors),'rows',sum(r['all_cells_on_same_output_row'] for r in rows),'/',len(rows),flush=True)
        except Exception as exc:
            references.append(dict(id=row['id'],error=type(exc).__name__+': '+str(exc)))
            print('reference',row['id'],'error',type(exc).__name__,flush=True)
        save(folder/'reference.json',references)


if __name__=='__main__':main()
