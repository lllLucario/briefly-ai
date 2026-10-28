"""英文/中英混合本地编码冒烟检查；不把少量输入可运行当检索质量验收。"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import socket

ROOT=Path(__file__).resolve().parents[2]
DOCUMENTS=[
    dict(language="english",text="The export service accepts CSV files up to 20 MB. Only editors can upload documents. A timeout does not confirm that a request failed; check the task receipt before retrying."),
    dict(language="mixed",text="知识库 supports TXT and DOCX。上传前需要 editor permission；E504 表示 execution status unknown，先查询 task receipt 再决定是否 retry。"),
    dict(language="chinese",text="员工差旅申请须由主管审批，财务核对发票后安排报销。")]
QUERIES=["What should I do after an export request times out?", "出现 E504 timeout 后能直接 retry 吗？", "上传文档需要什么权限？"]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,default=ROOT/".tmp/w3-bilingual-smoke.json")
    args=parser.parse_args()
    attempts=[]
    def deny(*args,**kwargs):
        attempts.append("blocked")
        raise RuntimeError("本地编码检查禁止网络连接")
    socket.socket.connect=deny
    socket.socket.connect_ex=deny
    from briefly.llm import create_local_embeddings
    results=[]
    for folder in ("bge-small-zh-v1.5","jina-v2-base-zh"):
        model=create_local_embeddings(str(ROOT/".tmp/w3-models"/folder),threads=4)
        documents=model.embed_documents([d["text"] for d in DOCUMENTS])
        queries=[model.embed_query(q) for q in QUERIES]
        vectors=documents+queries
        valid=all(len(v)==model.dimension and all(math.isfinite(x) for x in v)
                  and abs(math.sqrt(sum(x*x for x in v))-1)<1e-4 for v in vectors)
        if not valid:
            raise ValueError("编码维度、有限性或归一化验证失败")
        results.append(dict(model=model.identity["model"],revision=model.identity["revision"],dimension=model.dimension,
                            document_languages=[d["language"] for d in DOCUMENTS],documents=len(documents),queries=len(queries),
                            token_counts=[model.token_length(d["text"]) for d in DOCUMENTS],valid_vectors=valid))
    result=dict(results=results,documents=DOCUMENTS,queries=QUERIES,network_attempts=len(attempts),external_inference_calls=0,
                source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                limitations="只证明当前两个适配器可离线编码英文、中英混合与中文文本/问题；未计检索排序、跨语言召回率、长文切片或问答质量，不证明自动识别语言/切换模型。")
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(dict(results=results,network_attempts=len(attempts)),ensure_ascii=False))
    if attempts:
        raise SystemExit("发现网络尝试，检查不通过")


if __name__=="__main__":
    main()
