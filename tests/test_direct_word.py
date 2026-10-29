from pathlib import Path

import pytest

from briefly import config,direct_word
from briefly.loaders import load_document
from briefly.word_xhtml import xhtml_document

HTML=b'''<html><head><meta name="briefly:footnotes" content="1"/></head><body>
<h1>Policy</h1><p>Before booking: 650 CNY, formal staff only.</p>
<table><tr><td>City</td><td>Staff</td><td>Limit</td></tr>
<tr><td>A</td><td rowspan="2">Formal</td><td>650</td></tr>
<tr><td>B</td><td>480</td></tr><tr><td>C</td><td colspan="2">Per room</td></tr></table>
<p># literal heading</p><p>Footnote source.</p></body></html>'''


@pytest.fixture
def native(monkeypatch,tmp_path):
    java,jar=tmp_path/'java.exe',tmp_path/'tika.jar'
    java.write_bytes(b'fake');jar.write_bytes(b'fake')
    monkeypatch.setattr(config,'JAVA_PATH',str(java))
    monkeypatch.setattr(config,'TIKA_APP_JAR',str(jar))
    monkeypatch.setattr(config,'DOC_PARSER','auto')
    commands=[]
    def run(command,**kwargs):
        commands.append(command)
        output=Path(command[command.index('LegacyDocParser')+1])
        sources=command[command.index('LegacyDocParser')+2:]
        for i,source in enumerate(sources):
            target=output/str(i);target.mkdir(parents=True)
            payload=Path(source).read_bytes()
            if payload.endswith(b'unsupported'):
                (target/'error.code').write_text('unsupported_structure',encoding='utf-8')
            elif payload.endswith(b'bad'):
                (target/'error.code').write_text('parse_failed',encoding='utf-8')
            else:
                (target/'0.xhtml').write_bytes(HTML)
    monkeypatch.setattr(direct_word,'_run',run)
    return commands


def test_direct_default_preserves_native_structure(native):
    doc=load_document('中文旧制度.doc',direct_word.OLE_HEADER+b'fixture')
    assert doc.report['parser']=='tika-poi' and doc.report['conversion'] is None
    assert doc.report['footnotes']==1
    assert '# Policy' in doc.text
    assert '| B | Formal | 480 |' in doc.text
    assert '| C | Per room |  |' in doc.text
    assert '\\# literal heading' in doc.text
    assert not Path(native[0][-1]).exists()


def test_batch_uses_one_process_and_isolates_failure(native):
    result=direct_word.parse_docs([direct_word.OLE_HEADER+b'ok',direct_word.OLE_HEADER+b'bad',b'invalid',direct_word.OLE_HEADER+b'unsupported'])
    assert len(native)==1
    assert isinstance(result[0],tuple)
    assert isinstance(result[1],ValueError) and not isinstance(result[1],direct_word.DirectWordUnsupported)
    assert isinstance(result[2],ValueError)
    assert isinstance(result[3],direct_word.DirectWordUnsupported)


def test_unsupported_only_is_eligible_for_visible_fallback(native,monkeypatch):
    from briefly import legacy_word
    from test_legacy_word import docx_bytes
    calls=[]
    def convert(data):
        calls.append(data)
        return docx_bytes()
    monkeypatch.setattr(legacy_word,'convert_doc',convert)
    result=load_document('a.doc',direct_word.OLE_HEADER+b'unsupported')
    assert calls and any('不支持此结构' in w for w in result.report['warnings'])
    calls.clear()
    with pytest.raises(ValueError,match='直接解析失败'):
        load_document('a.doc',direct_word.OLE_HEADER+b'bad')
    assert not calls


def test_strict_direct_does_not_convert_unsupported(native,monkeypatch):
    monkeypatch.setattr(config,'DOC_PARSER','direct')
    with pytest.raises(direct_word.DirectWordUnsupported):
        load_document('a.doc',direct_word.OLE_HEADER+b'unsupported')


def test_rtf_fallback_reports_actual_format_and_separate_timings(native, monkeypatch):
    from briefly import legacy_word
    from test_legacy_word import docx_bytes
    monkeypatch.setattr(legacy_word, 'convert_doc', lambda _: docx_bytes())
    result = load_document('renamed.doc', b'{\\rtf1 fixture}')
    assert result.report['detected_format'] == 'rtf'
    assert any('RTF' in w for w in result.report['warnings'])
    assert not native  # RTF识别不启动Java。
    times = result.report['timings']
    assert times['conversion_seconds'] >= 0 and times['document_parse_seconds'] > 0
    assert times['parse_total_seconds'] >= times['conversion_seconds'] + times['document_parse_seconds']


def test_missing_direct_runtime_can_select_existing_conversion(native,monkeypatch):
    from briefly import legacy_word
    from test_legacy_word import docx_bytes
    monkeypatch.setattr(config,'JAVA_PATH','missing-java')
    monkeypatch.setattr(legacy_word,'convert_doc',lambda _:docx_bytes())
    doc=load_document('a.doc',direct_word.OLE_HEADER+b'ok')
    assert doc.report['conversion']=='libreoffice-to-docx'
    assert any('依赖未就绪' in w for w in doc.report['warnings'])
    monkeypatch.setattr(config,'DOC_PARSER','direct')
    with pytest.raises(ValueError,match='Java 17'):
        load_document('a.doc',direct_word.OLE_HEADER+b'ok')


def test_batch_limits_and_empty_batch(native):
    assert direct_word.parse_docs([])==[]
    with pytest.raises(ValueError,match='16份'):
        direct_word.parse_docs([b'x']*17)
    assert not native


def test_invalid_xhtml_and_expansion_are_rejected():
    for bad in (b'<html>',b'<html/>',b'<!DOCTYPE a><html/>'):
        with pytest.raises(ValueError):xhtml_document(bad)
    with pytest.raises(ValueError,match='跨度'):
        xhtml_document(b'<html><body><table><tr><td rowspan="1001">x</td></tr></table></body></html>')


def test_packaged_bridge_matches_its_source_and_build_manifest():
    import hashlib,json
    folder=direct_word.BRIDGE.parent
    build=json.loads((folder/'build.json').read_text(encoding='utf-8'))
    digest=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    assert digest(direct_word.BRIDGE)==build['jar_sha256']
    assert build['target_java']==17
    for name,expected in build['sources'].items():
        assert digest(folder/name)==expected


def test_nested_tables_keep_rows_separate_and_reference_source_parent_cell():
    payload=b'''<html><body><table><tr><th>City</th><th>Policy</th></tr>
    <tr><td>Shanghai</td><td>Before<table><tr><td>Type</td><td>Limit</td></tr>
    <tr><td>Staff</td><td>650</td></tr></table>After</td></tr>
    <tr><td>Hangzhou</td><td>480</td></tr></table></body></html>'''
    text,_=xhtml_document(payload)
    assert '| Shanghai | Before' in text and '| Hangzhou | 480 |' in text
    assert '| Staff | 650 |' in text
    assert text.count('| Staff | 650 |')==1
    assert '子表 2（父表 1，第2行，第2列）' in text
    outer=text.split('子表 2（')[0]
    assert '| Staff |' not in outer
    assert 'After |' in outer


def test_nested_table_inside_merged_cell_has_explicit_parent_range():
    payload=b'''<html><body><table><tr><td rowspan="2">Shared<table><tr><td>650</td></tr></table></td><td>A</td></tr>
    <tr><td>B</td></tr></table></body></html>'''
    text,_=xhtml_document(payload)
    assert '第1至2行，第1列' in text
    assert text.count('| 650 |')==1
    assert '| Shared' in text and '| B |' in text


def test_structure_uncertainty_and_objects_are_visible_in_report():
    from briefly.word_xhtml import xhtml_structure_report
    report=xhtml_structure_report(b'''<html><head><meta name="briefly:tables" content="3"/>
    <meta name="briefly:nested-tables" content="1"/><meta name="briefly:unverified-table-segments" content="1"/>
    <meta name="briefly:object-anchors" content="2"/></head><body><p>Text</p></body></html>''')
    assert report['tables']==3 and report['nested_tables']==1
    assert report['table_structure_status']=='partial'
    assert len(report['warnings'])==2


def test_nested_table_depth_and_overlapping_spans_are_rejected():
    nested=b'<table><tr><td>'*19+b'body'+b'</td></tr></table>'*19
    with pytest.raises(ValueError,match='嵌套'):
        xhtml_document(b'<html><body>'+nested+b'</body></html>')
    with pytest.raises(ValueError,match='重叠'):
        xhtml_document(b'''<html><body><table><tr><td>A</td><td rowspan="2">B</td></tr>
        <tr><td colspan="2">C</td></tr></table></body></html>''')


def test_textbox_appendix_keeps_cell_labels_and_literal_unknown_characters():
    payload='''<html><body><h1>最后正文</h1><p>BODY-END</p>
    <div class="briefly-textboxes"><h1>文本框附录（原文正文位置未绑定）</h1>
    <div class="briefly-textbox" data-source-group="1"><p data-source-cell="0">标签甲</p><p data-source-cell="1"/>
    <p data-source-cell="2">标签乙</p><p>真实�文本</p></div>
    <div class="briefly-textbox" data-source-group="2"><p>邻框标签</p></div></div></body></html>'''.encode()
    text,_=xhtml_document(payload)
    assert text.index('BODY-END')<text.index('# 文本框附录')<text.index('标签甲')
    assert '标签甲\n\n标签乙' in text and '标签甲标签乙' not in text
    assert '真实�文本' in text
    assert '---\n\n邻框标签' in text


def test_textbox_report_keeps_main_story_and_unbound_body_anchor_explicit():
    from briefly.word_xhtml import xhtml_structure_report
    report=xhtml_structure_report(b'''<html><head><meta name="briefly:textbox-ranges" content="2"/>
    <meta name="briefly:textbox-cell-boundaries" content="5"/>
    <meta name="briefly:textbox-range-boundaries-unverified" content="0"/></head><body/></html>''')
    assert report['textbox_ranges']==2 and report['textbox_cell_boundaries']==5
    assert report['textbox_body_anchor_status']=='unbound'
    assert report['textbox_range_boundaries_unverified']==0
    assert report['textbox_provenance']['may_infer_section'] is False
    assert report['textbox_provenance']['may_infer_org_chart_relationships'] is False
    assert any('未绑定正文段落' in w for w in report['warnings'])


def test_repaired_paragraphs_require_review_even_when_no_zero_row_segments_remain():
    from briefly.word_xhtml import xhtml_structure_report
    report=xhtml_structure_report(b'''<html><head><meta name="briefly:restored-source-paragraphs" content="9"/>
    <meta name="briefly:unverified-table-segments" content="0"/></head><body/></html>''')
    assert report['restored_source_paragraphs']==9
    assert report['table_structure_status']=='partial'
    assert report['table_structure_review_reasons']==['original_papx_paragraph_repair']
    assert any('仍需核对' in w for w in report['warnings'])
