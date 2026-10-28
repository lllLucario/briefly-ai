"""修复验收：26组标签的源范围、相邻空格/单元格和正文归属；不拼Node文本。"""
import argparse,hashlib,json,os,subprocess
from pathlib import Path
from xml.etree import ElementTree as ET

from briefly import config
from briefly.word_xhtml import xhtml_document,xhtml_structure_report


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('folder',type=Path);args=parser.parse_args();folder=args.folder.resolve()
    row=json.loads((folder/'source.json').read_text(encoding='utf-8'));source=Path(row['path']).resolve()
    assert hashlib.sha256(source.read_bytes()).hexdigest()==row['sha256']
    with (folder/'textbox-validation-process.log').open('wb') as log:
        run=subprocess.run([config.JAVA_PATH,'-Xmx256m','-XX:ActiveProcessorCount=4','-Dfile.encoding=UTF-8','-cp',config.TIKA_APP_JAR+os.pathsep+str(Path('src/briefly/java/legacy-doc-parser.jar').resolve()),'LegacyDocStructured',str(folder/'final-java'),str(source)],stdout=log,stderr=log,timeout=60,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    assert run.returncode==0
    payload=(folder/'final-java/0.xhtml').read_bytes();root=ET.fromstring(payload)
    story=json.loads(Path('files/eval/w3/doc-textbox/poi-ranges/ranges-raw.json').read_text())['textbox']
    raw=story['text'];labels=['修订标记','修订处数','修订日期','审批签字'];groups=[];marks=0
    for box in (n for n in root.iter() if n.get('class')=='briefly-textbox'):
        start,end=int(box.get('data-source-start')),int(box.get('data-source-end'))
        assert story['start']<=start<=end<=story['end']
        cells=[p for p in box if p.tag.rsplit('}',1)[-1]=='p']
        for p in cells:
            if p.get('data-source-cell-end')=='true':
                assert raw[int(p.get('data-source-end'))-story['start']]=='\x07';marks+=1
        hits=[p for p in cells if p.text in labels]
        if not hits:continue
        assert [p.text for p in hits]==labels
        assert [int(p.get('data-source-cell')) for p in hits]==[0,1,2,3]
        assert all(p.get('data-source-cell-end')=='true' for p in hits)
        for p in hits:
            assert raw[int(p.get('data-source-start'))-story['start']:int(p.get('data-source-end'))-story['start']]==p.text
        assert any(p.get('data-source-cell')=='4' and not p.text for p in cells),'Adjacent empty cell missing'
        assert not any('\ufffd' in (p.text or '') for p in cells)
        groups.append(dict(source_group=int(box.get('data-source-group')),source_start=start,source_end=end,
            cells=[dict(label=p.text,source_cell=int(p.get('data-source-cell')),start=int(p.get('data-source-start')),end=int(p.get('data-source-end'))) for p in hits],
            adjacent_empty_cell_preserved=True,body_anchor='unbound; not inferred'))
    assert len(groups)==26 and len({g['source_group'] for g in groups})==26
    assert marks==raw.count('\x07')==390
    text,_=xhtml_document(payload);report=xhtml_structure_report(payload)
    appendix=text.split('# 文本框附录（原文正文位置未绑定）',1)[1]
    assert appendix.count('修订标记\n\n修订处数\n\n修订日期\n\n审批签字')==26
    assert '\ufffd' not in appendix and report['textbox_body_anchor_status']=='unbound'
    assert report['textbox_range_boundaries_unverified']==0
    assert report['table_structure_status']=='partial','Do not hide unresolved tables'
    (folder/'final.md').write_text(text,encoding='utf-8')
    (folder/'textbox-validation.json').write_text(json.dumps(dict(label_groups=groups,passed_label_groups=26,source_cell_marks_checked=390,
        no_node_text_used=True,source_story='main-textbox',header_footer_not_reintroduced=True,
        original_literal_replacement_char_preservation='Separately covered by synthetic Java/Python regression',
        report=report),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    # Public upload path: isolated memory DB, no model and no format conversion allowed.
    from fastapi.testclient import TestClient
    from briefly import legacy_word,loaders
    from briefly.api import create_app
    from briefly.db import create_db_engine
    from briefly.materials import MaterialStore
    def forbidden(*args,**kwargs):raise AssertionError('No model or conversion in this validation')
    original_convert,original_docling=legacy_word.convert_doc,loaders._docling_converter
    original_mode=config.DOC_PARSER
    engine=create_db_engine('sqlite://')
    try:
        config.DOC_PARSER='auto'
        legacy_word.convert_doc=loaders._docling_converter=forbidden
        with TestClient(create_app(MaterialStore(engine),model_factory=forbidden,agent_factory=forbidden)) as client:
            response=client.post('/materials/upload',files={'file':(source.name,source.read_bytes())})
            assert response.status_code==201,response.text
            material=response.json()['material']
            detail=client.get('/materials/'+str(material['id'])).json()
            assert detail['content'].count('修订标记\n\n修订处数\n\n修订日期\n\n审批签字')==26
            assert '文本框附录（原文正文位置未绑定）' in detail['content']
            assert detail['parse_report']['textbox_provenance']['may_infer_section'] is False
            assert detail['parse_report']['textbox_provenance']['may_infer_org_chart_relationships'] is False
            (folder/'real-api-validation.json').write_text(json.dumps(dict(status_code=response.status_code,
                label_groups=26,auto_direct_only=True,conversion_calls=0,model_calls=0,
                isolated_memory_database=True,parse_report=detail['parse_report'],source_unchanged=hashlib.sha256(source.read_bytes()).hexdigest()==row['sha256']),
                ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    finally:
        engine.dispose()
        legacy_word.convert_doc,loaders._docling_converter=original_convert,original_docling
        config.DOC_PARSER=original_mode
    print('26 label groups, native cell identities and 390 boundaries verified; unknown table status remains partial.')


if __name__=='__main__':main()
