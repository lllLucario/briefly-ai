"""逐对象验收：独立原始范围、父格、行列合并、内容和多余碎片。"""
import argparse,json
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile
from doc_independent_properties import Independent
from doc_table_object_mapping import canonical,tag,parents,ancestors,norm,W


def own_text(cell,pmap,celltag,texttag=None):
    if texttag:
        return ''.join(n.text or '' for n in cell.iter(texttag) if next((a for a in ancestors(n,pmap) if a.tag==celltag),None) is cell)
    parts=[]
    def walk(n):
        if n is not cell and tag(n)=='table':return
        if n.text:parts.append(n.text)
        for c in n:
            walk(c)
            if c.tail:parts.append(c.tail)
    walk(cell);return ''.join(parts)


def reference_grid(t,pmap):
    anchors={};occupied={}
    for r,row in enumerate(t.findall(W+'tr')):
        col=0
        for cell in row.findall(W+'tc'):
            gs=cell.find(W+'tcPr/'+W+'gridSpan');cs=int(gs.get(W+'val')) if gs is not None else 1
            vm=cell.find(W+'tcPr/'+W+'vMerge');continuation=vm is not None and vm.get(W+'val','continue')=='continue'
            if continuation:
                assert (r-1,col) in occupied
                origin=occupied[(r-1,col)];a=anchors[origin];assert a['colspan']==cs;a['rowspan']=r-origin[0]+1
            else:
                origin=(r,col);anchors[origin]=dict(row=r,col=col,rowspan=1,colspan=cs,text=own_text(cell,pmap,W+'tc',W+'t'))
            for cc in range(col,col+cs):occupied[(r,cc)]=origin
            col+=cs
    return list(anchors.values())


def html_grid(t):
    anchors=[];occupied=set()
    rows=[n for n in t.iter() if tag(n)=='tr' and next((a for a in ancestors(n,parents(t)) if tag(a)=='table'),None) is t]
    for r,row in enumerate(rows):
        col=0
        for cell in row:
            if tag(cell) not in {'td','th'}:continue
            while (r,col) in occupied:col+=1
            rs,cs=int(cell.get('rowspan','1')),int(cell.get('colspan','1'))
            anchors.append(dict(row=r,col=col,rowspan=rs,colspan=cs,text=own_text(cell,{},'td')))
            for rr in range(r,r+rs):
                for cc in range(col,col+cs):assert (rr,cc) not in occupied;occupied.add((rr,cc))
            col+=cs
    return anchors


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('folder',type=Path)
    parser.add_argument('--adjudication',type=Path);parser.add_argument('--output',type=Path)
    args=parser.parse_args();folder=args.folder
    decision=json.loads(args.adjudication.read_text(encoding='utf-8')) if args.adjudication else None
    if decision:
        assert decision['status']=='word_visual_and_logical_verified'
        import hashlib
        assert hashlib.sha256(Path(decision['visual_evidence']['path']).read_bytes()).hexdigest()==decision['visual_evidence']['sha256']
        word=json.loads(Path(decision['logical_evidence']['path']).read_text(encoding='utf-8-sig'))
        assert word['source_sha256']==decision['source_sha256']
        assert word['table_start']==25254 and word['table_end']==25325 and len(word['cells'])==14
    with ZipFile('files/eval/w3/doc-real/reference/12/input.docx') as z:ref=ET.fromstring(z.read('word/document.xml'))
    rp=parents(ref);roots=[t for t in ref.iter(W+'tbl') if not any(a.tag==W+'tbl' for a in ancestors(t,rp))]
    trace=ET.parse(folder/'object-map/bridge-tables.xhtml').getroot();actual=ET.parse(folder/'after/0.xhtml').getroot()
    tables=[n for n in actual.iter() if tag(n)=='table'];tt=[n for n in trace.iter() if tag(n)=='table']
    assert [canonical(t) for t in tt]==[canonical(t) for t in tables]
    tp=parents(trace);objects=json.loads((folder/'object-map/objects.json').read_text(encoding='utf-8'))['bridge_objects']
    reader=Independent(folder/'before');source=[];cp=23882
    while cp<25738:
        r=reader.at(cp);assert r['start']==cp;source.append(r);cp=r['end']
    assert cp==25738
    parent=next(t for t in tt if t.get('data-probe-id')=='J:23882:25738:1')
    assert len([t for t in parent.iter() if tag(t)=='table'])==8
    # Parent cell mark + outer TTP are explicit source evidence, not proximity to a child.
    end=reader.at(25736);rowend=reader.at(25737)
    assert end['final_properties']['level']==rowend['final_properties']['level']==1
    assert end['original_terminator_hex']=='0700' and rowend['original_terminator_hex']=='0700'
    assert not end['final_properties']['row_end'] and rowend['final_properties']['row_end']
    children=[t for t in roots[1].iter(W+'tbl') if t is not roots[1]]
    native_children=[t for t in parent.iter() if t is not parent and tag(t)=='table']
    assert len(children)==len(native_children)==7
    results=[]
    for i,(reference,child) in enumerate(zip(children,native_children),1):
        obj=next(x for x in objects if x['id']==child.get('data-probe-id'))
        sg=[p for p in source if obj['start']<=p['start']<obj['end']]
        assert sg and sg[0]['start']==obj['start'] and sg[-1]['end']==obj['end']
        assert all(p['final_properties']['in_table'] and p['final_properties']['level']==2 for p in sg)
        assert obj['parent_cell']=='J:23882:25738:1/r0c0'
        # Independent source cell markers must reproduce every row's cell count.
        counts=[];cells=0;cell_start=obj['start'];cell_sources=[]
        source_text=json.loads(Path('files/eval/w3/doc-boundaries/source-pieces/main-pre-properties.json').read_text(encoding='utf-8'))['text']
        for p in sg:
            f=p['final_properties']
            if f['row_end']:counts.append(cells);cells=0;cell_start=p['end']
            elif f['embedded_cell_end']:
                original=source_text[cell_start:p['end']]
                native_cell=obj['rows'][len(counts)]['cells'][cells]
                assert norm(original)==norm(native_cell['own_text'])
                cell_sources.append(dict(row=len(counts),cell=cells,start=cell_start,end=p['end'],text=original,
                    cell_end_papx_offset=p['source']['papx_offset'],cell_end_papx_hex=p['source']['papx_hex']))
                cells+=1;cell_start=p['end']
        assert cells==0 and counts==[len(r['cells']) for r in obj['rows']]
        assert len(obj['rows'])==len(reference.findall(W+'tr'))
        rg,hg=reference_grid(reference,rp),html_grid(child)
        geom=lambda a:(a['row'],a['col'],a['rowspan'],a['colspan'])
        assert [geom(x) for x in rg]==[geom(x) for x in hg],(i,'merge or geometry mismatch')
        checks=[]
        for a,b in zip(rg,hg):
            exact=norm(a['text'])==norm(b['text']);glyph=norm(a['text'],True)==norm(b['text'],True)
            conflict=not exact and not (i==4 and glyph)
            checks.append(dict(geometry=geom(a),exact_text=exact,checkbox_parenthesis_only=not exact and glyph,
                reference_conflict=conflict,reference_text=a['text'] if conflict else None,output_text=b['text'] if conflict else None))
        conflicts=[c for c in checks if c['reference_conflict']]
        assert not conflicts or i==6,('Unexpected new reference conflict',i,conflicts)
        adjudicated=False
        if conflicts and decision:
            assert decision['reference_id']=='R2/6'
            assert len(conflicts)==len(decision['corrected_expectations'])==4
            for a in hg:
                cell=next(c for c in word['cells'] if c['row']==a['row']+1 and c['column']==a['col']+1)
                assert norm(cell['text'])==norm(a['text'])
                # Word logical rows, geometry and original source ranges independently corroborate the result.
                assert cell['start_row']==cell['end_row']==a['row']+1
                native=obj['rows'][a['row']]['cells'][a['col']]
                assert cell['start']==native['start'] and cell['end']==native['end']
                assert abs(cell['width_points']-native['width']/20)<0.11
            for c in conflicts:
                r,col,rs,cs=c['geometry']
                correction=next(x for x in decision['corrected_expectations'] if x['row']==r+1 and x['column']==col+1)
                assert norm(correction['converted_text'])==norm(c['reference_text'])
                assert norm(correction['expected_text'])==norm(c['output_text'])
                c['resolution']='corrected_reference_expectation_from_Word_and_original_markers'
            adjudicated=True
        # Full text is conserved; disputed cell ownership must remain separate from content coverage.
        assert norm(''.join(a['text'] for a in rg),i==4)==norm(''.join(a['text'] for a in hg),i==4)
        results.append(dict(reference_id='R2/'+str(i),source_id=obj['id'],source_start=obj['start'],source_end=obj['end'],
            source_parent_cell=obj['parent_cell'],reference_parent_cell='R2/r0c0',
            content_covered=True,boundaries_verified=True,parent_cell_verified=True,
            row_cells_from_independent_markers=counts,rows_columns_verified=True,merges_verified=True,
            excess_fragments=0,cell_checks=checks,source_cells=cell_sources,reference_cell_ownership_agrees=not conflicts,
            reference_conflict_adjudicated=adjudicated,
            acceptance='passed_with_corrected_reference_expectations' if adjudicated else 'pending_word_original_display' if conflicts else 'passed_source_and_reference_checks'))
    # Content and geometry outside the target region must not change.
    old=ET.parse('files/eval/w3/doc-boundaries/final-java/0.xhtml').getroot()
    old_roots=[n for n in old.iter() if tag(n)=='table' and not any(tag(a)=='table' for a in ancestors(n,parents(old)))]
    new_roots=[n for n in actual.iter() if tag(n)=='table' and not any(tag(a)=='table' for a in ancestors(n,parents(actual)))]
    assert len(old_roots)==len(new_roots)==224
    assert [canonical(t) for i,t in enumerate(old_roots) if i!=1]==[canonical(t) for i,t in enumerate(new_roots) if i!=1]
    text=(folder/'after/output.md').read_text(encoding='utf-8')
    assert text.count('修订标记\n\n修订处数\n\n修订日期\n\n审批签字')==26
    destination=args.output or folder/'seven-table-acceptance.json'
    destination.write_text(json.dumps(dict(objects=results,
        all_seven_passed=bool(decision),passed_objects=7 if decision else 6,
        reference_disagreement_objects=['R2/6'],unresolved_reference_disagreements=[] if decision else ['R2/6'],
        adjudication=str(args.adjudication) if decision else None,
        canonical_trace_matches_production=True,unaffected_outer_tables_equal=223,
        independent_parent_cell_end=25736,independent_parent_row_end=25737,
        reference_is_not_ground_truth='Acceptance is corroborated by independent original boundaries/cell markers; no rule depends on reference table count.'),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('7 objects pass; R2/6 reference expectations corrected from Word visual/logical and source evidence; 223 other roots unchanged' if decision else '6 objects pass source+reference checks; R2/6 cell-ownership disagreement retained; 223 other outer tables unchanged')


if __name__=='__main__':main()
