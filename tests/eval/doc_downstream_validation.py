"""原始DOC本地解析→材料→知识版本/引用→受限问答，禁止外部推理和格式转换。"""
import argparse,hashlib,json
from pathlib import Path
from fastapi.testclient import TestClient
from briefly import legacy_word,loaders
from briefly.api import create_app
from briefly.db import create_db_engine
from briefly.knowledge.evidence import restricted_answer
from briefly.materials import MaterialStore


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('folder',type=Path);out=parser.parse_args().folder
    source=json.loads(Path('files/eval/w3/doc-properties/source.json').read_text(encoding='utf-8'));path=Path(source['path']);payload=path.read_bytes()
    assert hashlib.sha256(payload).hexdigest()==source['sha256']
    def forbidden(*args,**kwargs):raise AssertionError('No generation, conversion or Docling in restricted-evidence validation')
    legacy_word.convert_doc=loaders._docling_converter=forbidden
    engine=create_db_engine('sqlite://');store=MaterialStore(engine);records=[]
    try:
        with TestClient(create_app(store,model_factory=forbidden,agent_factory=forbidden)) as client:
            response=client.post('/materials/upload',files={'file':(path.name,payload)});assert response.status_code==201,response.text
            mid=response.json()['material']['id'];material=client.get(f'/materials/{mid}').json();report=material['parse_report']
            assert report['document_quality']['status']=='partial'
            assert report['document_quality']['facets']['textbox_anchors']=='unbound'
            kb=client.post('/knowledge-bases',json={'name':'本地结构约束验收'}).json()['id']
            version=client.post(f'/knowledge-bases/{kb}/documents',json={'material_id':mid}).json();doc=version['document']['id'];ver=version['version']['id']
            chunks=store.chunks(mid);box=next(c for c in chunks if '修订标记\n\n修订处数' in c.content);table=next(c for c in chunks if '| 容易失误的工作环节' in c.content)
            for repeat in range(1,4):
                for key,chunk,question,capability in [
                    ('unknown_section',box,'这些修订标签属于原文哪个业务章节？','section_attribution'),
                    ('unknown_graph',box,'根据图中的位置，谁是它的直属上级？请给确定答案。','organization_relationships'),
                    ('disputed_cell',table,'工作环境精确属于第几行第几列？不要加保留说明。','cell_attribution'),
                ]:
                    evidence=client.get(f'/knowledge-bases/{kb}/documents/{doc}/versions/{ver}/chunks/{chunk.index}').json()
                    if key=='unknown_section':assert evidence['section'] is None
                    data=restricted_answer([evidence])
                    assert data['status']=='needs_review' and not data['model_called'] and capability in data['blocked_capabilities']
                    records.append(dict(repeat=repeat,case=key,passed=True,result=data))
            (out/'real-downstream.json').write_text(json.dumps(dict(source_sha256=source['sha256'],parse_report=report,cases=records,
                model_calls=0,conversion_calls=0,source_unchanged=hashlib.sha256(path.read_bytes()).hexdigest()==source['sha256']),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    finally:engine.dispose()
    print('9 real-DOC referenced-answer checks passed; no model/conversion; partial facets persisted')


if __name__=='__main__':main()
