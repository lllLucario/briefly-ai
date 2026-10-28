"""复核真实DOC结果与已落盘的LibreOffice参考；不重复读取未入选材料。"""
import argparse
import hashlib
import json
from pathlib import Path
import re

from doc_real_probe import norm
from doc_parser_comparison import save


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder',type=Path)
    args=parser.parse_args()
    folder=args.folder.resolve()
    references=json.loads((folder/'reference.json').read_text(encoding='utf-8'))
    for reference in references:
        id=reference['id']
        path=folder/f'{id}-1.md'
        mode='direct'
        if not path.exists():
            path=folder/f'auto-{id}.md';mode='conversion'
        text=path.read_text(encoding='utf-8') if path.exists() else ''
        reference['output_mode']=mode if text else 'unavailable'
        reference['direct_exists']=(folder/f'{id}-1.md').exists()
        reference['output_chars']=len(text)
        for anchor in reference.get('anchors',[]):anchor['found']=norm(anchor['text']) in norm(text)
        lines=[norm(line) for line in text.splitlines() if line.startswith('|')]
        for row in reference.get('rows',[]):
            cells=[norm(c) for c in row['cells'] if len(norm(c))>=2]
            row['all_cells_on_same_output_row']=any(all(cell in line for cell in cells) for line in lines)
        print(id,reference['output_mode'],'anchors',sum(a['found'] for a in reference.get('anchors',[])),len(reference.get('anchors',[])),
              'rows',sum(r['all_cells_on_same_output_row'] for r in reference.get('rows',[])),len(reference.get('rows',[])))
    save(folder/'audit.json',references)
    repo=Path(__file__).resolve().parents[2]
    selected=json.loads((folder/'selected.json').read_text(encoding='utf-8'))
    initial={r['id']:r['source_sha256'] for r in json.loads((folder/'before-fix/results.json').read_text(encoding='utf-8'))}
    hashes={r['id']:hashlib.sha256((repo/r['path']).read_bytes()).hexdigest() for r in selected}
    assert all(hashes[id]==initial[id] for id in hashes),'Selected sources changed during testing'
    save(folder/'integrity.json',dict(source_files_unchanged=True,source_sha256=hashes))
    source_paths=['tests/eval/doc_real_probe.py','tests/eval/doc_real_audit.py','tests/eval/java/WordCellProbe.java',
                  'src/briefly/java/LegacyDocStructured.java','src/briefly/java/legacy-doc-parser.jar','src/briefly/java/build.json']
    manifest=dict(source_sha256={},originals_unchanged=True,external_model_calls=0,
        authorization='User explicitly authorized selecting and reading DOC files in resources for real parsing tests; selected list limits body reads.',
        limitations='Purposive small sample, not random population estimate. LibreOffice round-trip comparisons are implementation agreement, not ground truth. Blank forms do not validate filled data attribution. Latency is local, not production SLA.')
    for name in source_paths:
        source=repo/name
        manifest['source_sha256'][name]=hashlib.sha256(source.read_bytes()).hexdigest()
        target=folder/'source-snapshot'/name
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(source.read_bytes())
    save(folder/'manifest.json',manifest)


if __name__=='__main__':main()
