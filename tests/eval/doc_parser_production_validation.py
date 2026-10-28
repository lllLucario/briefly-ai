"""在受控基准同一DOC输入上验证正式解析器、逐文件失败隔离和API。"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import statistics
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from briefly import config
from briefly.api import create_app
from briefly.db import create_db_engine
from briefly.direct_word import parse_docs
from briefly.materials import MaterialStore
from doc_parser_comparison import measured,quality,save


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('baseline',type=Path)
    args=parser.parse_args()
    repo=Path(__file__).resolve().parents[2]
    output=repo/'.tmp/eval-runs'/('doc-direct-validation-'+datetime.now(ZoneInfo('Australia/Sydney')).strftime('%Y%m%d-%H%M%S-%f'))
    output.mkdir(parents=True,exist_ok=False)
    config.DOC_PARSER='direct'
    cases=json.loads((args.baseline/'cases.json').read_text(encoding='utf-8'))
    inputs=[(args.baseline/'samples'/(c['id']+'.doc')).read_bytes() for c in cases]
    records=[]
    for repeat in range(1,4):
        parsed,stats=measured(lambda:parse_docs(inputs))
        for case,item in zip(cases,parsed):
            assert not isinstance(item,Exception),(case['id'],str(item))
            text,report=item
            checks=quality(text,case)
            records.append(dict(case=case['id'],repeat=repeat,quality=checks,report=report))
            (output/(str(repeat)+'-'+case['id']+'.md')).write_text(text,encoding='utf-8')
            assert all(all(v) for v in checks.values()),(case['id'],checks)
        save(output/('batch-'+str(repeat)+'.json'),stats)
    # 一个损坏文件不会让同批其余文件被丢弃。
    from briefly.legacy_word import OLE_HEADER
    mixed=parse_docs([inputs[0],OLE_HEADER+b'broken',inputs[2]])
    assert isinstance(mixed[0],tuple) and isinstance(mixed[1],ValueError) and isinstance(mixed[2],tuple)
    def forbidden(*args,**kwargs):raise AssertionError('本实验不得调用模型')
    engine=create_db_engine('sqlite://')
    try:
        with TestClient(create_app(MaterialStore(engine),model_factory=forbidden,agent_factory=forbidden)) as client:
            result=client.post('/materials/upload',files={'file':('中文合并表格.doc',inputs[2])})
            assert result.status_code==201,result.text
            material=result.json()['material']
            assert material['file_type']=='doc' and material['structure']=='word_styles'
            detail=client.get('/materials/'+str(material['id'])).json()
            assert '| 杭州 | 正式员工 | 480元 |' in detail['content']
            save(output/'api.json',dict(upload=result.json(),table_mapping_preserved=True))
    finally:
        engine.dispose()
    save(output/'results.json',records)
    save(output/'summary.json',dict(runs=len(records),all_checks_passed=True,mixed_batch_isolated=True,resources_read=False,model_calls=0))
    print(output.relative_to(repo).as_posix(),flush=True)


if __name__=='__main__':main()
