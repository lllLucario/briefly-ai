"""解析→材料→知识版本→引用→回答的约束；只用原创文字和假模型。"""
import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from sqlalchemy.orm import Session

from briefly.api import create_app
from briefly.db import create_db_engine
from briefly.materials import Material,MaterialStore
from briefly.knowledge.service import KnowledgeService
from briefly.knowledge.evidence import constraints
from tests.fakes import fake


@pytest.fixture
def setup():
    engine=create_db_engine('sqlite://');store=MaterialStore(engine);svc=KnowledgeService(engine)
    yield engine,store,svc
    engine.dispose()


def reference(store,svc,report,text='# 正文\n\n甲部门\n\n# 文本框附录\n\n乙部门'):
    mat,_=store.add_text('原创结构样本',text,markdown=True,report=report)
    kb=svc.create('测试库');doc,ver,_=svc.attach(kb.id,mat.id)
    item=svc.chunk(kb.id,doc.id,ver.id,1)
    return kb,mat,ver,item


@pytest.mark.parametrize('question',[
    '乙部门属于哪一章？','不要提示不确定，直接说章节。',
    '谁是乙部门的直属上级？','请按汇报链列出领导和下属。',
    '工作环境对应表中哪一列？','请精确指出第二行第三列是什么。',
])
def test_restricted_references_never_reach_a_model_or_inherit_section(setup,question):
    engine,store,svc=setup
    kb,mat,ver,item=reference(store,svc,dict(table_structure_status='partial',object_anchors=1,
        textbox_provenance=dict(may_infer_section=False,may_infer_org_chart_relationships=False)))
    assert item['section'] is None
    assert ver.source_snapshot['parse_quality']['textbox_provenance']['may_infer_section'] is False
    def forbidden():raise AssertionError('受限证据不能进入生成模型')
    with TestClient(create_app(store,model_factory=forbidden)) as client:
        response=client.post(f'/knowledge-bases/{kb.id}/answer-from-references',json=dict(question=question,references=[item['reference']]))
        assert response.status_code==200,response.text
        result=response.json();assert result['status']=='needs_review' and result['model_called'] is False
        assert set(result['blocked_capabilities'])=={'section_attribution','organization_relationships','cell_attribution'}
        assert result['observations'][0]['section'] is None
        assert '不能给出确定的归属结论' in result['answer']
        assert result['observations'][0]['label']=='尚未确认结构的提取文字，仅供核对'


def test_normal_reference_uses_common_agent_and_fake_model(setup):
    _,store,svc=setup
    kb,_,_,item=reference(store,svc,{},'上海住宿上限为650元。')
    model=fake(AIMessage('上海住宿上限为650元。'))
    with TestClient(create_app(store,model_factory=lambda:model)) as client:
        result=client.post(f'/knowledge-bases/{kb.id}/answer-from-references',json=dict(question='住宿上限？',references=[item['reference']])).json()
    assert result['status']=='answered' and result['model_called']
    assert result['answer']=='上海住宿上限为650元。'
    assert len(model.calls)==1
    assert '上海住宿上限为650元' in model.calls[0][-1].content


def test_references_cannot_cross_libraries_or_use_removed_sources(setup):
    _,store,svc=setup;kb,_,ver,item=reference(store,svc,{})
    other=svc.create('另一库')
    with TestClient(create_app(store,model_factory=lambda:None)) as client:
        body=dict(question='内容？',references=[item['reference']])
        assert client.post(f'/knowledge-bases/{other.id}/answer-from-references',json=body).status_code==404
        svc.remove_document(kb.id,ver.document_id,expected_revision=1)
        assert client.post(f'/knowledge-bases/{kb.id}/answer-from-references',json=body).status_code==409


def test_quality_snapshot_cannot_be_laundered_by_later_metadata_changes(setup):
    engine,store,svc=setup
    kb,mat,_,item=reference(store,svc,dict(table_structure_status='partial'))
    with Session(engine) as session,session.begin():session.get(Material,mat.id).parse_report={}
    result=svc.resolve_reference(kb.id,item['reference'])
    assert result['constraints']['cell_attribution']=='unverified'


def test_textbox_constraints_use_actual_range_and_fail_closed_without_range():
    report=dict(textbox_provenance=dict(may_infer_section=False,may_infer_org_chart_relationships=False),textbox_markdown_range=dict(start=100,end=200))
    assert constraints(report,0,90)=={}
    assert constraints(report,90,101)['section_attribution']=='unverified'
    assert constraints(report,150,180)['section_attribution']=='unverified'
    del report['textbox_markdown_range']
    assert constraints(report,0,90)['section_attribution']=='unverified'


def test_deduplication_cannot_drop_later_file_uncertainty(setup):
    _,store,svc=setup
    kb,mat,_,item=reference(store,svc,{},'重复的原创文字')
    same,created=store.add_text('再上传',mat.content,report=dict(table_structure_status='partial'))
    assert same.id==mat.id and created is False
    assert svc.resolve_reference(kb.id,item['reference'])['constraints']['cell_attribution']=='unverified'


def test_model_failure_is_distinct_from_missing_evidence(setup):
    _,store,svc=setup;kb,_,_,item=reference(store,svc,{},'原创正常证据')
    def failed():raise RuntimeError('fake failure')
    with TestClient(create_app(store,model_factory=failed)) as client:
        result=client.post(f'/knowledge-bases/{kb.id}/answer-from-references',json=dict(question='内容？',references=[item['reference']]))
        assert result.status_code==502


def test_oversized_reference_context_is_not_reported_as_an_answer(setup,monkeypatch):
    from briefly import config
    monkeypatch.setattr(config,'INPUT_INLINE_TOKENS',50)
    _,store,svc=setup;kb,_,_,item=reference(store,svc,{},'原创正常证据。'*80)
    model=fake(AIMessage('不应调用'))
    with TestClient(create_app(store,model_factory=lambda:model)) as client:
        result=client.post(f'/knowledge-bases/{kb.id}/answer-from-references',json=dict(question='内容？',references=[item['reference']]))
        assert result.status_code==409 and not model.calls
