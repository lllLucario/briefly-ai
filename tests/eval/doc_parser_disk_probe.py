"""只统计解析临时目录关闭前的体积，不把它误称为全过程峰值。"""
import argparse
import json
from pathlib import Path

from briefly import config,direct_word,legacy_word
from briefly.loaders import load_document


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('baseline',type=Path)
    parser.add_argument('output',type=Path)
    args=parser.parse_args()
    records=[]
    for case in ('03-合并表格','07-长文'):
        data=(args.baseline/'samples'/(case+'.doc')).read_bytes()
        for method,module in (('conversion',legacy_word),('direct',direct_word)):
            original=module.TemporaryDirectory
            captured=[]
            class Tracked:
                def __init__(self,*a,**kw):self.directory=original(*a,**kw)
                def __enter__(self):
                    self.path=Path(self.directory.__enter__());return str(self.path)
                def __exit__(self,*exc):
                    files=[p for p in self.path.rglob('*') if p.is_file()]
                    captured.extend(dict(name=p.relative_to(self.path).as_posix(),bytes=p.stat().st_size) for p in files)
                    return self.directory.__exit__(*exc)
            module.TemporaryDirectory=Tracked
            try:
                if method=='conversion':
                    config.DOC_PARSER='convert';load_document('sample.doc',data)
                else:
                    result=direct_word.parse_docs([data])[0]
                    assert not isinstance(result,Exception),result
            finally:
                module.TemporaryDirectory=original
            records.append(dict(case=case,method=method,input_bytes=len(data),closing_temp_bytes=sum(x['bytes'] for x in captured),
                files=captured,docx_intermediate_bytes=sum(x['bytes'] for x in captured if x['name'].endswith('.docx'))))
    def total(path):return sum(p.stat().st_size for p in path.rglob('*') if p.is_file())
    result=dict(records=records,runtime_bytes=dict(libreoffice=total(Path('.tmp/word-converter/runtime')),
        jre=total(Path('.tmp/doc-direct/runtime')),tika=Path(config.TIKA_APP_JAR).stat().st_size,bridge=direct_word.BRIDGE.stat().st_size),
        note='Runtime totals exclude downloaded installer archives and development compiler. Temp bytes measured immediately before cleanup, not peak disk usage.')
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('Disk measurements saved',flush=True)


if __name__=='__main__':main()
