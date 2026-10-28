"""模型比较的独立结果审计/上下文预算计分；不加载任何模型。"""
import argparse
import hashlib
import json
from pathlib import Path
import statistics

from w3_chunk_pilot import score

CONTEXT_CHAR_BUDGET=2000


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def save(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")


def analyze(folder,decision=None):
    frozen=read(folder/"frozen.json")
    if frozen["split"]=="validation":
        if decision is None or hashlib.sha256(Path(__file__).read_bytes()).hexdigest()!=decision["analysis_sha256"] or decision["context_char_budget"]!=CONTEXT_CHAR_BUDGET:
            raise ValueError("验证评分实现或上下文预算与选择时不符")
    dataset=read(folder/"dataset.json")
    cases={case["id"]:case for case in dataset["cases"]}
    profiles=[]
    failures=[]
    budget_raw=[]
    pipeline_raw=[]
    checks=[]
    for cfg in frozen["profiles"]:
        result=read(folder/(cfg["name"]+".json"))
        mapping={int(k):v for k,v in read(folder/(cfg["chunks"]+"-chunks.json")).items()}
        groups={}
        for row in result["raw"]:
            groups.setdefault((row["case"],row["method"],row["k"]),[]).append(row)
            checks.append(all(mapping[i]["document"] in cases[row["case"]]["scope"] for i,_ in row["hits"]))
            if row["repeat"]==1 and row["k"] in (3,5) and row["all_required"] is False:
                failures.append(dict(profile=cfg["name"],case=row["case"],method=row["method"],k=row["k"],
                                     question=cases[row["case"]]["question"],groups_found=row["groups_found"],hits=row["hits"]))
            if row["repeat"]==1 and row["k"]==10:
                selected=[]
                used=0
                for ident,value in row["hits"]:
                    length=len(mapping[ident]["text"])
                    if used+length>CONTEXT_CHAR_BUDGET:
                        break
                    selected.append((ident,value))
                    used+=length
                budget_raw.append(dict(profile=cfg["name"],case=row["case"],category=cases[row["case"]].get("category","office"),
                                       method=row["method"],used_characters=used,retained_chunks=len(selected),
                                       **score(cases[row["case"]]["evidence_groups"],selected,mapping)))
            if row["repeat"]==1 and row["k"]==5:
                prefix=[]
                used=0
                for ident,value in row["hits"]:
                    length=len(mapping[ident]["text"])
                    if used+length>CONTEXT_CHAR_BUDGET:
                        break
                    prefix.append((ident,value))
                    used+=length
                pipeline_raw.append(dict(profile=cfg["name"],case=row["case"],category=cases[row["case"]].get("category","office"),
                                         method=row["method"],used_characters=used,retained_chunks=len(prefix),
                                         **score(cases[row["case"]]["evidence_groups"],prefix,mapping)))
        expected={(case,method,k) for case in cases for method in ("dense","lexical","rrf") for k in (3,5,10)}
        if set(groups)!=expected:
            raise ValueError("缺少逐题或逐方法结果，拒绝将不完整运行判通过")
        checks.extend(len(rows)==frozen["repeats"] and all(r["hits"]==rows[0]["hits"] for r in rows) for rows in groups.values())
        contexts={}
        summaries=[]
        for method in ("dense","lexical","rrf"):
            for k in (3,5):
                rows=[r for r in result["raw"] if r["repeat"]==1 and r["method"]==method and r["k"]==k and r["recall"] is not None]
                contexts[f"{method}@{k}"]=statistics.median(sum(len(mapping[i]["text"]) for i,_ in r["hits"]) for r in rows)
            for category in ("all",*sorted({c.get("category","office") for c in cases.values()})):
                rows=[r for r in budget_raw if r["profile"]==cfg["name"] and r["method"]==method and (category=="all" or r["category"]==category)]
                positive=[r for r in rows if r["recall"] is not None]
                if positive:
                    summaries.append(dict(method=method,category=category,char_budget=CONTEXT_CHAR_BUDGET,positive_cases=len(positive),
                                          all_required=sum(r["all_required"] for r in positive),
                                          recall=sum(r["recall"] for r in positive)/len(positive)))
        profiles.append(dict(profile=cfg["name"],chunk_count=result["chunk_count"],summary=result["summary"],
                             budget_summary=summaries,median_context_characters=contexts,
                             query_embedding={k:v for k,v in result["query_embedding"].items() if k!="raw_ms"},
                             document_encode_ms=result["document_encode_ms"],load_ms=result["load_ms"],
                             peak_working_set_bytes=result["peak_working_set_bytes"],network_attempts=result["network_attempts"]))
        pipeline_summary=[]
        for method in ("dense","lexical","rrf"):
            rows=[r for r in pipeline_raw if r["profile"]==cfg["name"] and r["method"]==method and r["recall"] is not None]
            pipeline_summary.append(dict(method=method,final_top_k=5,char_budget=CONTEXT_CHAR_BUDGET,positive_cases=len(rows),
                                         all_required=sum(r["all_required"] for r in rows),recall=sum(r["recall"] for r in rows)/len(rows)))
        profiles[-1]["pipeline_summary"]=pipeline_summary
        checks.append(result["network_attempts"]==0)
    audit=dict(split=frozen["split"],cases=len(cases),positive_cases=sum(bool(c["evidence_groups"]) for c in cases.values()),
               negative_cases=sum(not c["evidence_groups"] for c in cases.values()),checks=len(checks),all_checks=all(checks),
               context_budget_note="按原排序连续取完整块，遇2000字符上限即停止，不跳过大块挑后续小块；字符不是DeepSeek实际计费token。")
    if not audit["all_checks"]:
        raise ValueError("重复/范围/离线检查失败")
    result=dict(audit=audit,profiles=profiles)
    save(folder/"analysis.json",result)
    save(folder/"failures.json",failures)
    save(folder/"context-budget-raw.json",budget_raw)
    save(folder/"pipeline-raw.json",pipeline_raw)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder",type=Path)
    parser.add_argument("--decision",type=Path)
    args=parser.parse_args()
    result=analyze(args.folder,read(args.decision) if args.decision else None)
    print(json.dumps(dict(audit=result["audit"],profiles=[dict(profile=p["profile"],budget_summary=[r for r in p["budget_summary"] if r["method"]=="rrf"]) for p in result["profiles"]]),ensure_ascii=False))


if __name__=="__main__":
    main()
