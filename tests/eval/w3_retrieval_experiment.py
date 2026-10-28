"""W3-02 CPU 离线检索选型；先 dev，冻结配置后再运行 holdout。"""
import argparse
from dataclasses import asdict
from datetime import datetime
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import platform
import socket
import sqlite3
import statistics
import subprocess
import time
from zoneinfo import ZoneInfo

import psutil

from briefly import config
from briefly.db import create_db_engine
from briefly.llm import create_local_embeddings
from briefly.materials import MaterialStore
from w3_rag_baseline import FIXTURES, REPO, load_fixtures, save
from w3_retrieval_support import CandidateIndex, rrf


def evidence_score(case, evidence, hits, contents):
    groups = [any(contents[i][0] == evidence[ref]["document"] and evidence[ref]["quote"] in contents[i][1]
                  for ref in group for i, _ in hits) for group in case["evidence_groups"]]
    return dict(recall=sum(groups)/len(groups) if groups else None,
                all_required=all(groups) if groups else None, groups_found=groups)


def memory():
    p = psutil.Process()
    info = p.memory_info()
    return dict(rss_bytes=info.rss, peak_working_set_bytes=getattr(info, "peak_wset", None),
                cpu_user_s=p.cpu_times().user, cpu_system_s=p.cpu_times().system)


def checks(index, identity, embedder, path):
    """独立作用域的可复核生命周期；不改业务数据库。"""
    results = []
    vec = embedder.embed_documents(["橙叶审批额度111元", "橙叶审批额度222元"])
    query = embedder.embed_query("橙叶审批额度")
    index.replace_document("lifecycle", [(10001, "橙叶审批额度111元", vec[0])])
    before = index.dense(query, ["lifecycle"], 1)
    results.append(dict(name="add", ok=len(before)==1 and before[0][0]==10001))
    # 更新内容并同步两路索引，旧ID必须消失。
    index.replace_document("lifecycle", [(10002, "橙叶审批额度222元", vec[1])])
    results.append(dict(name="update_both_paths", ok=index.dense(query,["lifecycle"],1)[0][0]==10002
                        and not index.lexical("111",["lifecycle"]) and bool(index.lexical("222",["lifecycle"]))))
    try:
        index.replace_document("lifecycle", [(10003, "坏向量", [1.])])
    except ValueError:
        results.append(dict(name="failed_update_keeps_old", ok=index.contents(10002) is not None))
    else:
        results.append(dict(name="failed_update_keeps_old", ok=False))
    # 已有ID冲突发生于事务中，检验不仅是预校验失败。
    try:
        index.replace_document("lifecycle", [(1, "冲突", vec[0])])
    except sqlite3.IntegrityError:
        results.append(dict(name="transaction_rollback", ok=index.contents(10002) is not None and bool(index.lexical("222",["lifecycle"]))))
    else:
        results.append(dict(name="transaction_rollback", ok=False))
    results.append(dict(name="scope_before_top1", ok=index.dense(vec[0],["lifecycle"],1)[0][0]==10002))
    results.append(dict(name="empty_scope", ok=not index.dense(query,[],1) and not index.lexical("橙叶",[])))
    index.delete_document("lifecycle")
    results.append(dict(name="delete_both_paths", ok=not index.dense(query,["lifecycle"],1) and not index.lexical("橙叶",["lifecycle"])))
    for name, changed in (("model_change", {**identity, "revision":"different"}), ("dimension_change", {**identity,"dimension":384})):
        try:
            other = CandidateIndex(path, changed, backend=index.backend)
        except ValueError:
            results.append(dict(name=name, ok=True))
        else:
            other.close()
            results.append(dict(name=name, ok=False))
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", default=".tmp/w3-models/bge-small-zh-v1.5")
    parser.add_argument("--split", choices=("dev","holdout"), default="dev")
    parser.add_argument("-n", type=int, default=3)
    args = parser.parse_args()
    if args.n < 1:
        parser.error("重复次数必须为正数")
    stamp = datetime.now(ZoneInfo("Australia/Sydney")).strftime("%Y%m%d-%H%M%S-%f")
    output = REPO/".tmp"/"eval-runs"/("w3-retrieval-"+stamp)
    output.mkdir(parents=True,exist_ok=False)
    network_attempts = []
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    def deny(*args, **kwargs):
        network_attempts.append("socket connection blocked")
        raise RuntimeError("离线评测禁止网络连接")
    socket.socket.connect = deny
    socket.socket.connect_ex = deny
    try:
        before = memory()
        started = time.perf_counter()
        embedder = create_local_embeddings(args.model_dir)
        load_ms = (time.perf_counter()-started)*1000
        loaded = memory()
        corpus, spec = load_fixtures()
        config.CHUNK_SIZE,config.CHUNK_OVERLAP = 1500,150
        engine = create_db_engine("sqlite://")
        indices={}
        try:
            store=MaterialStore(engine)
            ids={}
            contents={}
            chunk_keys={}
            token_counts=[]
            for doc in corpus["documents"]:
                material,_=store.add_text(doc["title"],doc["text"],markdown=True)
                ids[doc["id"]]=material.id
                for chunk in store.chunks(material.id):
                    ident=len(contents)+1
                    contents[ident]=(doc["id"],chunk.content)
                    chunk_keys[(material.id,chunk.index)]=ident
                    token_counts.append(dict(document=doc["id"],chunk=chunk.index,tokens=embedder.token_length(chunk.content)))
            start=time.perf_counter()
            vectors=embedder.embed_documents([c[1] for c in contents.values()])
            encode_ms=(time.perf_counter()-start)*1000
            builds={}
            for backend in ("scalar","vec0"):
                start=time.perf_counter()
                index=CandidateIndex(output/(backend+".db"),embedder.identity,backend=backend)
                indices[backend]=index
                for doc in corpus["documents"]:
                    index.replace_document(doc["id"],[(i,content,vectors[i-1]) for i,(d,content) in contents.items() if d==doc["id"]])
                builds[backend]=(time.perf_counter()-start)*1000
            results=[]
            cases=[c for c in spec["cases"] if c["split"]==args.split]
            for repeat in range(1,args.n+1):
                for case in cases:
                    if not case["question"].strip():
                        results.append(dict(case_id=case["id"],repeat=repeat,status="invalid_query",methods={}))
                        continue
                    start=time.perf_counter()
                    query=embedder.embed_query(case["question"])
                    embedding_ms=(time.perf_counter()-start)*1000
                    methods={}
                    timings={}
                    for backend,index in indices.items():
                        start=time.perf_counter()
                        methods[backend]=index.dense(query,case["scope"],20)
                        timings[backend]=(time.perf_counter()-start)*1000
                    start=time.perf_counter()
                    methods["fts_bigram"]=indices["vec0"].lexical(case["question"],case["scope"],20)
                    timings["fts_bigram"]=(time.perf_counter()-start)*1000
                    start=time.perf_counter()
                    methods["rrf"]=rrf([methods["scalar"],methods["fts_bigram"]],60)
                    timings["rrf_fusion"]=(time.perf_counter()-start)*1000
                    start=time.perf_counter()
                    methods["raw_keyword"]=[(chunk_keys[(h.material_id,h.chunk)],h.hits) for h in store.search(case["question"],[ids.get(d,2_000_000_000) for d in case["scope"]])]
                    timings["raw_keyword"]=(time.perf_counter()-start)*1000
                    start=time.perf_counter()
                    methods["manual_keyword"]=[(chunk_keys[(h.material_id,h.chunk)],h.hits) for h in store.search(case["keywords"],[ids.get(d,2_000_000_000) for d in case["scope"]])]
                    timings["manual_keyword"]=(time.perf_counter()-start)*1000
                    scored={}
                    for method,hits in methods.items():
                        scored[method]=dict(hits=[dict(chunk_id=i,document=contents[i][0],score=s) for i,s in hits],
                            evidence={str(k):evidence_score(case,spec["evidence"],hits[:k],contents) for k in (1,3,5,10)},
                            scope_leaks=[i for i,_ in hits if contents[i][0] not in case["scope"]])
                    results.append(dict(case_id=case["id"],scenario=case["scenario"],kind=case["kind"],repeat=repeat,
                                        embedding_ms=embedding_ms,timings_ms=timings,methods=scored,status="ok"))
            lifecycle={}
            for backend,index in list(indices.items()):
                path=output/(backend+".db")
                lifecycle[backend]=checks(index,embedder.identity,embedder,path)
                previous=index.dense(vectors[0],["office-policy-v1"],1)
                index.close()
                indices.pop(backend)
                reopened=CandidateIndex(path,embedder.identity,backend=backend)
                try:
                    lifecycle[backend].append(dict(name="restart",ok=previous==reopened.dense(vectors[0],["office-policy-v1"],1)))
                finally:
                    reopened.close()
            long_rejected=False
            try:
                embedder.embed_documents(["住宿审批"*400])
            except ValueError:
                long_rejected=True
            save(output/"results.json",results)
            save(output/"lifecycle.json",lifecycle)
            summary=[]
            for method in ("raw_keyword","manual_keyword","fts_bigram","vec0","scalar","rrf"):
                for k in (1,3,5,10):
                    rows=[r["methods"][method]["evidence"][str(k)] for r in results if r["status"]=="ok" and r["methods"][method]["evidence"][str(k)]["recall"] is not None]
                    negatives=[r for r in results if r["status"]=="ok" and r["methods"][method]["evidence"][str(k)]["recall"] is None]
                    summary.append(dict(method=method,k=k,scored_runs=len(rows),macro_evidence_recall=statistics.mean(r["recall"] for r in rows),all_required_runs=sum(r["all_required"] for r in rows),
                                        no_evidence_runs=len(negatives),no_evidence_with_hits=sum(bool(r["methods"][method]["hits"]) for r in negatives)))
            save(output/"summary.json",summary)
            save(output/"chunks.json",[dict(id=i,document=d,content=c) for i,(d,c) in contents.items()])
            source_paths=[Path(__file__),Path(__file__).with_name("w3_retrieval_support.py"),REPO/"src/briefly/local_embeddings.py",REPO/"src/briefly/llm.py",FIXTURES/"documents.json",FIXTURES/"expectations.json"]
            save(output/"manifest.json",dict(split=args.split,repeats=args.n,questions=len(cases),
                identity=embedder.identity,token_counts=token_counts,load_ms=load_ms,document_encode_ms=encode_ms,
                index_build_ms=builds,db_bytes={b:(output/(b+".db")).stat().st_size for b in ("scalar","vec0")},
                memory_before=before,memory_loaded=loaded,memory_final=memory(),cpu_count=psutil.cpu_count(),ram_bytes=psutil.virtual_memory().total,
                python=platform.python_version(),sqlite=sqlite3.sqlite_version,
                packages={p:version(p) for p in ("fastembed","onnxruntime","sqlite-vec","psutil","langchain-core","tokenizers")},
                git_head=subprocess.check_output(["git","rev-parse","HEAD"],cwd=REPO,text=True).strip(),
                sha256={p.relative_to(REPO).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths},
                network_attempts=network_attempts,external_inference_calls=0,long_input_rejected=long_rejected,
                parameters=dict(threads=4,query_prefix=embedder.query_prefix,candidate_k=20,rrf_k=60,threshold=None),
                timing="模型本进程首次加载（文件缓存可能已热）；编码含实际CPU推理，查询含重复热查询；RSS与进程历史峰值，非整机增量内存"))
            failures=[(b,c["name"]) for b,checks_ in lifecycle.items() for c in checks_ if not c["ok"]]
            leaks=sum(bool(m["scope_leaks"]) for r in results for m in r["methods"].values())
            print(output.relative_to(REPO).as_posix(),flush=True)
            print(json.dumps(dict(lifecycle_failures=failures,scope_leaks=leaks,network_attempts=len(network_attempts)),ensure_ascii=False))
            if failures or leaks or network_attempts or not long_rejected:
                raise SystemExit(1)
        finally:
            for index in indices.values():
                index.close()
            engine.dispose()
    finally:
        socket.socket.connect=original_connect
        socket.socket.connect_ex=original_connect_ex


if __name__ == "__main__":
    main()
