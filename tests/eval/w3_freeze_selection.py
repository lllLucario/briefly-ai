"""仅用开发结果冻结候选；验证标签只做字节哈希，不读取问题内容或调参。"""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from w3_model_comparison import ROOT,INPUT_NAMES,SOURCE_PATHS,PROFILES,corpus_dataset,digest
from w3_chunk_pilot import chunks


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev-run",type=Path,required=True)
    parser.add_argument("--corpus-dir",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():
        raise ValueError("选择清单已存在；禁止覆盖冻结的选择")
    analysis=read(args.dev_run/"analysis.json")
    frozen=read(args.dev_run/"frozen.json")
    if frozen["split"]!="dev" or not analysis["audit"]["all_checks"]:
        raise ValueError("必须来自通过完整性审计的开发运行")
    if read(args.dev_run/"selected-dataset.json")!=corpus_dataset(args.corpus_dir,"dev"):
        raise ValueError("开发输入与当前语料不一致")
    eligible=[]
    for profile in analysis["profiles"]:
        primary=next(r for r in profile["pipeline_summary"] if r["method"]=="rrf")
        eligible.append(((-primary["all_required"],profile["query_embedding"]["p95_ms"],profile["peak_working_set_bytes"]),profile))
    selected=sorted(eligible,key=lambda x:x[0])[0][1]
    cfg=next(c for c in PROFILES if c["name"]==selected["profile"])
    mapping=read(args.dev_run/(cfg["chunks"]+"-chunks.json"))
    own_equal=None
    if cfg["name"]=="bge-common256":
        from tokenizers import Tokenizer
        tokenizer=Tokenizer.from_file(str(ROOT/".tmp/w3-models/bge-small-zh-v1.5/tokenizer.json"))
        tokenizer.no_truncation()
        tokenizer.no_padding()
        own={}
        for doc in read(args.dev_run/"dataset.json")["documents"]:
            text=(ROOT/doc["parsed_path"]).read_text(encoding="utf-8")
            for chunk in chunks(text,doc["title"],lambda t:len(tokenizer.encode(t).ids),budget=256,overlap=0):
                own[str(len(own)+1)]=dict(document=doc["id"],**chunk)
        own_equal=own==mapping
        if not own_equal:
            raise ValueError("共同切片与所选BGE单模型切片不同，需另行开发比较")
    decision=dict(created_at_sydney=datetime.now(ZoneInfo("Australia/Sydney")).isoformat(),
         selected_profile=cfg["name"],split_used_for_selection="dev",
         selection_rule="先比较开发集RRF Top5+2000字符上限的必要依据全齐题数；相同则依次选择查询Embedding P95更低、峰值工作集更低者。不读取验证结果。",
         context_char_budget=2000,final_top_k=5,per_route_candidates=20,rrf_constant=60,overlap=0,title_prefix=True,
         inputs_sha256={name:digest(args.corpus_dir/name) for name in sorted(INPUT_NAMES)},
         source_sha256={relative:digest(ROOT/relative) for relative in SOURCE_PATHS},
         analysis_sha256=digest(ROOT/"tests/eval/w3_model_results.py"),
         model_prepared_sha256=digest(ROOT/".tmp/w3-models"/cfg["directory"]/"prepared.json"),
         dev_analysis_sha256=digest(args.dev_run/"analysis.json"),common_chunks_sha256=digest(args.dev_run/"common-chunks.json"),
         bge_only_chunking_matches_common=own_equal,decision_script_sha256=digest(Path(__file__).resolve()),
         limitations="选择只覆盖检索候选与当前公开假设，不代表生成/拒答、表格关系、服务器容量或完整RAG交付；验证运行后不再按本题集调参。")
    args.output.write_text(json.dumps(decision,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(cfg["name"],"frozen before validation",flush=True)


if __name__=="__main__":
    main()
