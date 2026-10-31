"""解析→材料→知识版本→引用→回答的约束；只用原创文字，受限依据走程序分支、不调用模型。"""
import pytest
from sqlalchemy.orm import Session

from briefly.db import create_db_engine
from briefly.materials import Material,MaterialStore
from briefly.knowledge.service import KnowledgeService, KnowledgeNotFound
from briefly.knowledge.evidence import constraints, restricted_answer


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
    # 程序分支，与问题措辞无关
    result=restricted_answer([svc.resolve_reference(kb.id,item['reference'])])
    assert result['status']=='needs_review' and result['model_called'] is False
    assert set(result['blocked_capabilities'])=={'section_attribution','organization_relationships','cell_attribution'}
    assert result['observations'][0]['section'] is None
    assert '不能给出确定的归属结论' in result['answer']
    assert result['observations'][0]['label']=='尚未确认结构的提取文字，仅供核对'


def test_references_cannot_cross_libraries_or_use_removed_sources(setup):
    _,store,svc=setup;kb,_,ver,item=reference(store,svc,{})
    other=svc.create('另一库')
    with pytest.raises(KnowledgeNotFound):
        svc.resolve_reference(other.id,item['reference'])
    svc.remove_document(kb.id,ver.document_id,expected_revision=1)
    assert svc.resolve_reference(kb.id,item['reference'])['status']=='document_removed'


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
