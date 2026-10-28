"""W3-02 BGE/Jina相同切片及Jina768方案开发比较；隔离进程、离线，不作独立验证。"""
import argparse
from datetime import datetime
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

import psutil

from w3_chunk_pilot import chunks, load_dataset, score
from w3_retrieval_support import CandidateIndex, rrf
from w3_scoped_lexical import bounded_lexical

ROOT = Path(__file__).resolve().parents[2]
PROFILES = [dict(name="bge-common256", directory="bge-small-zh-v1.5", runtime_budget=None, chunks="common"),
            dict(name="jina-common256", directory="jina-v2-base-zh", runtime_budget=512, chunks="common"),
            dict(name="jina-own768", directory="jina-v2-base-zh", runtime_budget=768, chunks="long")]
SOURCE_PATHS=("tests/eval/w3_model_comparison.py","src/briefly/local_embeddings.py","src/briefly/llm.py",
              "tests/eval/w3_chunk_pilot.py","tests/eval/w3_retrieval_support.py","tests/eval/w3_scoped_lexical.py",
              "tests/eval/w3_model_results.py")
INPUT_NAMES={"documents.json","cases-dev.json","cases-validation.json","family-split.json"}


def corpus_dataset(folder, split):
    documents=json.loads((folder/"documents.json").read_text(encoding="utf-8"))
    families=json.loads((folder/"family-split.json").read_text(encoding="utf-8"))
    if set(families["dev"]) & set(families["validation"]):
        raise ValueError("文档家族在开发/验证之间泄漏")
    cases=json.loads((folder/("cases-"+split+".json")).read_text(encoding="utf-8"))["cases"]
    if any(case["split"]!=split or case["family"] not in families[split] for case in cases):
        raise ValueError("题目分组与冻结的家族分组不符")
    return dict(documents=documents["documents"],cases=cases)


def validate_decision(folder,decision):
    if set(decision["inputs_sha256"])!=INPUT_NAMES or set(decision["source_sha256"])!=set(SOURCE_PATHS):
        raise ValueError("冻结清单缺少必要输入或实现身份")
    for name,expected in decision["inputs_sha256"].items():
        if digest(folder/name)!=expected:
            raise ValueError("验证语料或题集在冻结选择后改变")
    for relative,expected in decision["source_sha256"].items():
        if digest(ROOT/relative)!=expected:
            raise ValueError("检索/切片实现与选择时冻结的版本不符")
    selected=[cfg for cfg in PROFILES if cfg["name"]==decision["selected_profile"]]
    if len(selected)!=1 or decision["split_used_for_selection"]!="dev":
        raise ValueError("选择清单不合法")
    if decision["context_char_budget"]!=2000 or decision["analysis_sha256"]!=decision["source_sha256"]["tests/eval/w3_model_results.py"]:
        raise ValueError("验证评分预算或实现未冻结")
    prepared=ROOT/".tmp/w3-models"/selected[0]["directory"]/"prepared.json"
    if digest(prepared)!=decision["model_prepared_sha256"]:
        raise ValueError("选择后模型制品身份改变")
    return selected


def save(path,data):
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")


def digest(path):
    h=hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda:stream.read(1048576),b""):
            h.update(block)
    return h.hexdigest()


def offline(attempts):
    def deny(*args,**kwargs):
        attempts.append("blocked")
        raise RuntimeError("模型比较禁止网络连接")
    socket.socket.connect=deny
    socket.socket.connect_ex=deny


def worker(args,attempts):
    from briefly.llm import create_local_embeddings
    cfg=next(c for c in PROFILES if c["name"]==args.worker)
    dataset=json.loads((args.output/"dataset.json").read_text(encoding="utf-8"))
    mapping={int(k):v for k,v in json.loads((args.output/(cfg["chunks"]+"-chunks.json")).read_text(encoding="utf-8")).items()}
    started=time.perf_counter()
    model=create_local_embeddings(str(ROOT/".tmp/w3-models"/cfg["directory"]),threads=4,max_input_tokens=cfg["runtime_budget"])
    load_ms=(time.perf_counter()-started)*1000
    checks=[model.token_length(c["text"])<=model.max_tokens for c in mapping.values()]
    if not all(checks):
        raise ValueError("模型输入超限；未截断")
    long_probe=None
    if cfg["runtime_budget"]==768:
        text="项目审批需要保留原始签字材料和完整验收条件。"*55
        tokens=model.token_length(text)
        runtime_tokens=len(model._model.model.tokenizer.encode(text).ids)
        vec=model.embed_documents([text])[0]
        long_probe=dict(tokens=tokens,runtime_tokens=runtime_tokens,dimension=len(vec),
                        ok=512<tokens<=768 and tokens==runtime_tokens and len(vec)==768)
        if not long_probe["ok"]:
            raise ValueError("实际分词器长输入验证失败")
    started=time.perf_counter()
    vectors=model.embed_documents([c["text"] for c in mapping.values()])
    encode_ms=(time.perf_counter()-started)*1000
    index=CandidateIndex(args.output/(cfg["name"]+".db"),model.identity,backend="scalar")
    started=time.perf_counter()
    try:
        for doc in dataset["documents"]:
            index.replace_document(doc["id"],[(i,c["text"],vectors[i-1]) for i,c in mapping.items() if c["document"]==doc["id"]])
        build_ms=(time.perf_counter()-started)*1000
        raw=[]
        query_times=[]
        for repeat in range(1,args.repeats+1):
            for case in dataset["cases"]:
                started=time.perf_counter()
                query=model.embed_query(case["question"])
                query_ms=(time.perf_counter()-started)*1000
                query_times.append(query_ms)
                dense=index.dense(query,case["scope"],20)
                lexical=index.lexical(case["question"],case["scope"],20)
                bounded,plan=bounded_lexical(index,case["question"],case["scope"],20)
                if lexical!=bounded:
                    raise ValueError("词法候选改变排序")
                for method,hits in (("dense",dense),("lexical",lexical),("rrf",rrf([dense,lexical]))):
                    for k in (3,5,10):
                        top=hits[:k]
                        if any(mapping[i]["document"] not in case["scope"] for i,_ in top):
                            raise ValueError("范围泄漏")
                        raw.append(dict(profile=cfg["name"],case=case["id"],repeat=repeat,method=method,k=k,
                                        hits=top,query_ms=query_ms,candidates=len(top),
                                        **score(case["evidence_groups"],top,mapping)))
        summary=[]
        for method in ("dense","lexical","rrf"):
            for k in (3,5,10):
                rows=[r for r in raw if r["repeat"]==1 and r["method"]==method and r["k"]==k]
                positive=[r for r in rows if r["recall"] is not None]
                negative=[r for r in rows if r["recall"] is None]
                summary.append(dict(method=method,k=k,positive_cases=len(positive),
                                    recall=sum(r["recall"] for r in positive)/len(positive),
                                    all_required=sum(r["all_required"] for r in positive),
                                    negative_with_candidates=sum(r["candidates"]>0 for r in negative),negative_cases=len(negative)))
        mem=psutil.Process().memory_info()
        import numpy as np
        save(args.output/(cfg["name"]+".json"),dict(profile=cfg,identity=model.identity,chunk_count=len(mapping),
             token_min=min(model.token_length(c["text"]) for c in mapping.values()),
             token_max=max(model.token_length(c["text"]) for c in mapping.values()),
             load_ms=load_ms,document_encode_ms=encode_ms,index_build_ms=build_ms,
             query_embedding=dict(count=len(query_times),p50_ms=float(np.percentile(query_times,50)),p95_ms=float(np.percentile(query_times,95)),raw_ms=query_times),
             rss_bytes=mem.rss,peak_working_set_bytes=getattr(mem,"peak_wset",None),
             long_probe=long_probe,summary=summary,raw=raw,network_attempts=len(attempts)))
    finally:
        index.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset",type=Path,default=ROOT/"files/eval/w3/retrieval/strengthening/dataset.json")
    parser.add_argument("--corpus-dir",type=Path,help="按家族分组的新三类语料；开发运行不读取验证问题文件")
    parser.add_argument("--split",choices=("dev","validation"),default="dev")
    parser.add_argument("--decision",type=Path,help="验证前固定的选择与输入/源码哈希清单")
    parser.add_argument("--output",type=Path)
    parser.add_argument("--repeats",type=int,default=3)
    parser.add_argument("--worker",choices=[c["name"] for c in PROFILES])
    args=parser.parse_args()
    if args.repeats<1:
        parser.error("重复次数必须为正")
    attempts=[]
    offline(attempts)
    if args.worker:
        worker(args,attempts)
        return
    from tokenizers import Tokenizer
    if args.split=="validation" and (args.corpus_dir is None or args.decision is None):
        parser.error("验证必须提供独立题库及先前冻结的选择清单")
    stamp=datetime.now(ZoneInfo("Australia/Sydney")).strftime("%Y%m%d-%H%M%S-%f")
    args.output=args.output or ROOT/".tmp"/("w3-model-comparison-"+stamp)
    args.output.mkdir(parents=True,exist_ok=False)
    selected=PROFILES
    if args.corpus_dir:
        if args.split=="validation":
            decision=json.loads(args.decision.read_text(encoding="utf-8"))
            selected=validate_decision(args.corpus_dir,decision)
            save(args.output/"decision.json",decision)
        save(args.output/"selected-dataset.json",corpus_dataset(args.corpus_dir,args.split))
        args.dataset=args.output/"selected-dataset.json"
    dataset,texts=load_dataset(args.dataset,expected_split=args.split)
    save(args.output/"dataset.json",dataset)
    tokenizers=[]
    for directory in ("bge-small-zh-v1.5","jina-v2-base-zh"):
        tokenizer=Tokenizer.from_file(str(ROOT/".tmp/w3-models"/directory/"tokenizer.json"))
        tokenizer.no_truncation()
        tokenizer.no_padding()
        tokenizers.append(tokenizer)
    mappings={}
    for name,budget,counter in (("common",256,lambda t:max(len(tok.encode(t).ids) for tok in tokenizers)),
                                ("long",768,lambda t:len(tokenizers[1].encode(t).ids))):
        mapping={}
        for doc in dataset["documents"]:
            for chunk in chunks(texts[doc["id"]],doc["title"],counter,budget=budget,overlap=0):
                mapping[len(mapping)+1]=dict(document=doc["id"],**chunk)
        mappings[name]=mapping
        save(args.output/(name+"-chunks.json"),mapping)
    save(args.output/"frozen.json",dict(profiles=selected,common_budget=256,long_budget=768,
         dataset_sha256=digest(args.dataset),chunk_sha256={name:digest(args.output/(name+"-chunks.json")) for name in mappings},
         repeats=args.repeats,split=args.split,note="配置在本次比较前冻结；验证只执行先前选择的配置，不据验证结果回调参数。"))
    runs=[]
    for cfg in selected:
        cmd=[sys.executable,str(Path(__file__).resolve()),"--output",str(args.output),"--worker",cfg["name"],"--repeats",str(args.repeats)]
        with (args.output/(cfg["name"]+".log")).open("w",encoding="utf-8") as stream:
            try:
                finished=subprocess.run(cmd,stdout=stream,stderr=subprocess.STDOUT,timeout=300)
                status=dict(profile=cfg["name"],exit_code=finished.returncode)
            except subprocess.TimeoutExpired:
                status=dict(profile=cfg["name"],timeout_seconds=300)
        runs.append(status)
        save(args.output/"runs.json",runs)
        print(json.dumps(status),flush=True)
    snapshot=args.output/"source-snapshot"
    snapshot.mkdir()
    paths=[ROOT/relative for relative in SOURCE_PATHS]
    hashes={}
    for path in paths:
        relative=path.relative_to(ROOT)
        target=snapshot/relative
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(path.read_bytes())
        hashes[relative.as_posix()]=digest(path)
    save(args.output/"manifest.json",dict(source_sha256=hashes,dependencies={name:version(name) for name in ("fastembed","langchain-core","tokenizers","onnxruntime","sqlite-vec")},
         network_attempts=len(attempts),external_inference_calls=0,
         limitations="各配置独立进程；峰值含模型/编码/索引/Python，未隔离CPU，缓存和批大小不同；只比较开发资料，不是三类独立质量验收。"))
    print(args.output,flush=True)
    if any(r.get("exit_code")!=0 for r in runs):
        raise SystemExit("候选存在失败；见runs.json和日志")


if __name__=="__main__":
    main()
