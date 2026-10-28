"""逐对象诊断；内容命中不是父格证明，不将转换参考当原始真值。"""
import argparse
import hashlib
import json
import re
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


def tag(n):
    return n.tag.rsplit('}', 1)[-1]


def norm(s, glyph=False):
    # Parenthesis removal is ONLY a labelled diagnostic comparison for legacy checkbox glyphs.
    return re.sub(r'[\s\x00-\x1f()]' if glyph else r'[\s\x00-\x1f]', '', s)


def parents(root):
    return {c: p for p in root.iter() for c in p}


def ancestors(n, mapping):
    while n in mapping:
        n = mapping[n]
        yield n


def canonical(n):
    # Only diagnostic attributes differ; preserve text, spans, order and nested structure.
    return (tag(n), tuple(sorted((k, v) for k, v in n.attrib.items() if not k.startswith('data-probe-'))),
            (n.text or '').strip(), tuple((canonical(c), (c.tail or '').strip()) for c in n))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path)
    folder = parser.parse_args().folder.resolve()
    data = json.loads((folder/'object-map/objects.json').read_text(encoding='utf-8'))
    raw = json.loads((folder/'source-pieces/main-pre-properties.json').read_text(encoding='utf-8'))['text']
    pieces = json.loads((folder/'source-pieces/decoded-pieces.json').read_text(encoding='utf-8'))
    poi_main = json.loads(Path('files/eval/w3/doc-textbox/poi-ranges/ranges-raw.json').read_text(encoding='utf-8'))['main']['text']
    assert raw == poi_main, 'Independent decoded text differs; CP mapping invalid'
    normalized = {g: norm(raw, g) for g in (False, True)}
    offsets = {g: [i for i, c in enumerate(raw) if norm(c, g)] for g in (False, True)}
    reference_path = Path('files/eval/w3/doc-real/reference/12/input.docx')
    with ZipFile(reference_path) as archive:
        reference = ET.fromstring(archive.read('word/document.xml'))
    rp = parents(reference)
    reference_tables = list(reference.iter(W+'tbl'))
    roots = [t for t in reference_tables if not any(a.tag == W+'tbl' for a in ancestors(t, rp))]
    ids = {}
    def assign(t, name):
        ids[t] = name
        children = [c for c in t.iter(W+'tbl') if c is not t and next(a for a in ancestors(c, rp) if a.tag == W+'tbl') is t]
        for i, child in enumerate(children, 1):
            assign(child, name+'/'+str(i))
    for i, t in enumerate(roots, 1):
        assign(t, 'R'+str(i))
    def reference_parent(t):
        cell = next((a for a in ancestors(t, rp) if a.tag == W+'tc'), None)
        parent = next((a for a in ancestors(t, rp) if a.tag == W+'tbl'), None)
        if parent is None:
            return None
        for r, row in enumerate(parent.findall(W+'tr')):
            for c, candidate in enumerate(row.findall(W+'tc')):
                if candidate is cell:
                    return ids[parent]+'/r'+str(r)+'c'+str(c)
        raise AssertionError('Reference parent cell unavailable')
    trace = ET.parse(folder/'object-map/bridge-tables.xhtml').getroot()
    final = ET.parse(folder/'final-java/0.xhtml').getroot()
    trace_tables = [n for n in trace.iter() if tag(n) == 'table']
    final_tables = [n for n in final.iter() if tag(n) == 'table']
    assert len(trace_tables) == len(final_tables) == 255
    assert [canonical(t) for t in trace_tables] == [canonical(t) for t in final_tables]
    markdown_ids = {t.get('data-probe-id'): i for i, t in enumerate(trace_tables, 1)}
    # First check the ORIGINAL CLX FC->CP translator, not HWPF's rebuilt singleton table.
    flags = ['in_table', 'level', 'row_end', 'embedded_cell_end']
    matches = []
    for source in data['raw_fkp_properties']:
        current = [p for p in data['hwpf_paragraphs'] if p['start'] < source['end'] <= p['end']]
        assert len(current) == 1
        current = current[0]
        same = all(source[k] == current[k] for k in flags)
        matches.append(dict(start=source['start'], end=source['end'], properties_equal=same))
    assert len(matches) == 478 and all(m['properties_equal'] for m in matches)
    records = []
    for t in reference_tables:
        value = ''.join(n.text or '' for n in t.iter(W+'t'))
        hits = []
        mode = 'exact_after_whitespace_and_control_removal'
        for glyph in (False, True):
            needle = norm(value, glyph)
            if not needle:
                break
            for match in re.finditer(re.escape(needle), normalized[glyph]):
                hits.append(dict(start=offsets[glyph][match.start()], end=offsets[glyph][match.end()-1]+1))
                if len(hits) > 50:
                    break
            if hits:
                mode = 'parenthesis_insensitive_checkbox_comparison' if glyph else mode
                break
        record = dict(reference_id=ids[t], reference_parent_cell=reference_parent(t),
                      reference_rows=len(t.findall(W+'tr')),
                      reference_cells=sum(len(r.findall(W+'tc')) for r in t.findall(W+'tr')),
                      content_prefix=value[:80], content_source_candidates=hits,
                      content_comparison=mode,
                      warning='Content extent excludes empty trailing rows/cells; it is not a source table bound or proof of parent ownership.')
        if len(hits) == 1:
            start, end = hits[0]['start'], hits[0]['end']
            record['overlapping_natural_objects'] = [x['id'] for x in data['natural_hwpf_tables'] if x['start'] < end and x['end'] > start]
            record['overlapping_bridge_objects'] = [dict(id=x['id'], kind=x['render_kind'], parent=x['parent_cell'],
                rows=len(x['rows']), markdown_table_id=markdown_ids.get(x['id'])) for x in data['bridge_objects'] if x['start'] < end and x['end'] > start]
        records.append(record)
    by_id = {r['reference_id']: r for r in records}
    anomalies = []
    for i in range(1, 8):
        record = dict(by_id['R2/'+str(i)])
        assert len(record['content_source_candidates']) == 1
        start, end = record['content_source_candidates'][0].values()
        stop = next(p['start'] for p in data['hwpf_paragraphs'] if p['start'] >= end and p['in_table'] and p['level'] == 1)
        row_marks = [p for p in data['hwpf_paragraphs'] if start <= p['start'] < stop and p['level'] == 2 and p['row_end']]
        gaps = [p for p in data['hwpf_paragraphs'] if start <= p['start'] < stop and not p['in_table']]
        natural = [x for x in data['natural_hwpf_tables'] if x['level'] == 2 and x['start'] < stop and x['end'] > start]
        record.update(source_region_end_before_next_level1=stop,
            source_level2_row_end_candidates=[dict(start=p['start'], end=p['end']) for p in row_marks],
            source_non_table_paragraphs=[dict(start=p['start'],end=p['end'],text=p['raw_text']) for p in gaps],
            natural_level2_fragments=[dict(id=x['id'],rows=len(x['rows']),parent_cell=x['parent_cell']) for x in natural],
            existence='source text exists; no verified complete corresponding production table',
            hierarchy='unverified: source fragments do not form a complete parent tree',
            parent_cell='unverified; R2/r0c0 is conversion-reference ownership only',
            rows_columns='unverified; candidate source row ends must not be treated as verified complete cells',
            markdown='prose preserved with partial warning; R2/7 also has an empty tail table attached to a partial root')
        assert len(row_marks) == record['reference_rows']
        anomalies.append(record)
    html = ET.parse('files/eval/w3/doc-textbox/poi-html/full.xhtml').getroot()
    hp = parents(html)
    html_tables = [n for n in html.iter() if tag(n) == 'table']
    promoted = []
    for n in html_tables:
        if int(n.get('data-probe-source-level', '0')) <= 1 or any(tag(a) == 'table' for a in ancestors(n, hp)):
            continue
        start, end = int(n.get('data-probe-source-start')), int(n.get('data-probe-source-end'))
        owner = 'R2/6' if start == 25283 else 'R2/7'
        assert start in {25283,25434,25446,25495,25518,25528,25557}
        promoted.append(dict(start=start,end=end,reference_content_region=owner,
            evidence='content and source range' if start in {25283,25446,25495,25528} else 'empty interval between mapped labels and source row markers; candidate only',
            complete_reference_object=False,parent_cell_verified=False))
    assert len(promoted) == 7
    control = next(x for x in data['bridge_objects'] if x['id'] == 'J:25837:26659:2')
    assert len(control['rows']) == 27 and sum(len(r['cells']) for r in control['rows']) == 55
    assert control['parent_cell'] == 'J:25823:26661:1/r0c0'
    by_id['R3/1']['verified_control'] = dict(exists=True,level=2,
        source_parent_cell=control['parent_cell'],rows=27,cells=55,markdown_table=5,
        limitation='Control only; does not validate anomalous region')
    result = dict(source_sha256=json.loads((folder/'source.json').read_text(encoding='utf-8'))['sha256'],
        reference_sha256=hashlib.sha256(reference_path.read_bytes()).hexdigest(),
        independent_main_text_equal=True,main_characters=len(raw),
        source_piece_prms_in_region=[dict(start=p['startCp'],end=p['endCp'],prm=p['prm']) for p in pieces['pieces'] if p['startCp']<25738 and p['endCp']>23882],
        original_fkp_to_current_paragraph_checks=matches,canonical_production_trace_tables_equal=255,
        reference_total=len(reference_tables),reference_roots=len(roots),html_total=len(html_tables),
        html_roots=sum(not any(tag(a)=='table' for a in ancestors(t,hp)) for t in html_tables),
        all_reference_inventory=records,anomalous_reference_children=anomalies,
        seven_promoted_html_objects=promoted,
        healthy_control=by_id['R3/1'],
        conclusion='Seven promoted HTML objects are fragments in two reference child regions (one in R2/6, six in R2/7); not seven recovered reference children. Empty fragment ownership remains a candidate. Source row/cell ownership recovery is not implemented.')
    (folder/'table-object-mapping.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('261 reference objects inventoried; seven anomalous reference children mapped; seven HTML promotions separately mapped; no production reattachment.')


if __name__ == '__main__':
    main()
