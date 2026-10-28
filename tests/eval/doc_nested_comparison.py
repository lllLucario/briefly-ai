"""同一真实DOC的直接/转换路径隔离测量；预算覆盖整个解析进程。"""
import argparse
import faulthandler
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import tempfile


def save(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def worker(source,folder,method):
    from briefly import config,legacy_word,loaders
    config.DOC_PARSER='direct' if method=='direct' else 'convert'
    payload=source.read_bytes()
    events=[]
    started=time.perf_counter()
    def record(phase,state):
        events.append(dict(phase=phase,state=state,seconds=time.perf_counter()-started))
        save(folder/'phases.json',events)
    def wrap(action,phase):
        def call(*args,**kwargs):
            record(phase,'start')
            try:return action(*args,**kwargs)
            finally:record(phase,'end')
        return call
    legacy_word.convert_doc=wrap(legacy_word.convert_doc,'libreoffice_conversion')
    factory=loaders._docling_converter
    class Converter:
        def __init__(self,original):self.original=original
        def convert(self,*args,**kwargs):return wrap(self.original.convert,'docling_backend')(*args,**kwargs)
    def observed_factory():
        return Converter(wrap(factory,'docling_factory')())
    loaders._docling_converter=observed_factory
    loaders._load_docx=wrap(loaders._load_docx,'docx_total')
    faulthandler.dump_traceback_later(30,repeat=True)
    try:
        doc=loaders.load_document(source.name,payload)
        seconds=time.perf_counter()-started
        (folder/'output.md').write_text(doc.text,encoding='utf-8')
        save(folder/'result.json',dict(ok=True,parse_seconds=seconds,chars=len(doc.text),report=doc.report))
    except Exception as exc:
        save(folder/'result.json',dict(ok=False,error=type(exc).__name__+': '+str(exc)))
        raise
    finally:faulthandler.cancel_dump_traceback_later()


def measured_child(source,folder,method,budget):
    import psutil
    folder.mkdir()
    # Keep a short Windows TEMP root: LibreOffice creates deeply nested extension paths.
    temporary=Path(tempfile.mkdtemp(prefix='dnc-',dir=Path(__file__).resolve().parents[2]/'.tmp'))
    environment={**os.environ,'TEMP':str(temporary),'TMP':str(temporary),'PYTHONIOENCODING':'utf-8'}
    stats=dict(peak_family_rss=0,peak_temp_bytes=0)
    stop=threading.Event()
    start=time.perf_counter()
    with (folder/'process.log').open('wb') as log:
        process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),str(source),str(folder),'--worker','--method',method],
            env=environment,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        parent=psutil.Process(process.pid)
        def sample():
            while not stop.is_set():
                rss=0
                try:family=[parent,*parent.children(recursive=True)]
                except psutil.Error:family=[]
                for p in family:
                    try:rss+=p.memory_info().rss
                    except psutil.Error:pass
                stats['peak_family_rss']=max(stats['peak_family_rss'],rss)
                sizes=0
                for p in temporary.rglob('*'):
                    try:
                        if p.is_file():sizes+=p.stat().st_size
                    except OSError:pass
                stats['peak_temp_bytes']=max(stats['peak_temp_bytes'],sizes)
                stop.wait(.05)
        thread=threading.Thread(target=sample,daemon=True);thread.start()
        timeout=False
        try:process.wait(timeout=budget)
        except subprocess.TimeoutExpired:
            timeout=True
            try:family=parent.children(recursive=True)
            except psutil.Error:family=[]
            for p in [*reversed(family),parent]:
                try:p.kill()
                except psutil.Error:pass
            process.wait(timeout=10)
        finally:stop.set();thread.join()
    save(folder/'budget.json',dict(method=method,timeout=timeout,exit_code=process.returncode,wall_seconds=time.perf_counter()-start,budget_seconds=budget,temp_root=str(temporary),**stats))
    print(method,folder.name,'TIMEOUT' if timeout else process.returncode,flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path)
    parser.add_argument('folder',type=Path)
    parser.add_argument('--worker',action='store_true')
    parser.add_argument('--method',choices=['direct','convert','both'],default='both')
    parser.add_argument('--budget',type=float,default=120)
    args=parser.parse_args()
    source=args.source.resolve();folder=args.folder.resolve()
    if args.worker:
        worker(source,folder,args.method);return
    folder.mkdir(exist_ok=True)
    if args.method in {'direct','both'}:
        for repeat in range(1,4):measured_child(source,folder/f'direct-{repeat}','direct',args.budget)
    if args.method in {'convert','both'}:measured_child(source,folder/'convert-1','convert',args.budget)


if __name__=='__main__':main()
