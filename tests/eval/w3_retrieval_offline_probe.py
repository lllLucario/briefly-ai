"""独立新进程：加载本地模型、重开索引及特殊问题检查；禁止网络连接。"""
import argparse
import json
from pathlib import Path
import socket
import time

from briefly.llm import create_local_embeddings
from w3_retrieval_support import CandidateIndex, rrf


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("result_dir",type=Path)
    args=parser.parse_args()
    attempts=[]
    def blocked(*args,**kwargs):
        attempts.append("blocked")
        raise RuntimeError("离线探针禁止联网")
    socket.socket.connect=blocked
    socket.socket.connect_ex=blocked
    started=time.perf_counter()
    embedder=create_local_embeddings(".tmp/w3-models/bge-small-zh-v1.5")
    manifest=json.loads((args.result_dir/"manifest.json").read_text(encoding="utf-8"))
    if embedder.identity!=manifest["identity"]:
        raise ValueError("模型身份不同")
    indices={}
    results=[]
    try:
        for backend in ("scalar","vec0"):
            indices[backend]=CandidateIndex(args.result_dir/(backend+".db"),embedder.identity,backend=backend)
        for query,scope in [("上海住宿上限是多少？",["office-policy-v2","office-exception"]),
            ("住宿",["office-policy-v2","office-exception"]),("元",["office-policy-v2"]),
            ("E403",["manual-v2","manual-errors"]),("CSV",["manual-v2","manual-maintenance"]),
            ("650",["office-policy-v2","office-exception"]),("C++ \"$()\" % _",["manual-v2","manual-errors"])]:
            vec=embedder.embed_query(query)
            dense=indices["scalar"].dense(vec,scope,3)
            native=indices["vec0"].dense(vec,scope,3)
            lexical=indices["scalar"].lexical(query,scope,3)
            results.append(dict(query=query,scope=scope,dense=dense,lexical=lexical,rrf=rrf([dense,lexical]),
                backends_equal=[i for i,_ in dense]==[i for i,_ in native],
                dense_documents=[indices["scalar"].contents(i)[0] for i,_ in dense]))
        record=dict(new_process=True,seconds=time.perf_counter()-started,network_attempts=attempts,
                    results=results,all_backends_equal=all(r["backends_equal"] for r in results))
        (args.result_dir/"offline-new-process.json").write_text(json.dumps(record,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        print(json.dumps(dict(network_attempts=len(attempts),backends_equal=record["all_backends_equal"])))
        if attempts or not record["all_backends_equal"]:
            raise SystemExit(1)
    finally:
        for index in indices.values():
            index.close()


if __name__=="__main__":
    main()
