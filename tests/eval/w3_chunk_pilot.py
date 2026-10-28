"""W3-02 真实长文快照切片开发实验；全部本地，不作为独立验证或生产入库。"""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import socket
import time
from zoneinfo import ZoneInfo

from w3_retrieval_support import CandidateIndex, rrf

ROOT = Path(__file__).resolve().parents[2]
CONFIGS = [dict(name="t256-o0-title", budget=256, overlap=0, title=True),
           dict(name="t384-o0-title", budget=384, overlap=0, title=True),
           dict(name="t256-o48-title", budget=256, overlap=48, title=True),
           dict(name="t256-o0-no-title", budget=256, overlap=0, title=False)]


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def chunks(text, title, counter, *, budget, overlap=0, **ignored):
    """保留精确字符偏移；标题计入token；段落优先，过长段落按token窗口切。"""
    if budget < 8 or overlap < 0 or overlap >= budget // 2:
        raise ValueError("切片预算/重叠无效")
    prefix = title + "\n" if title else ""
    if counter(prefix) >= budget // 2:
        raise ValueError("标题占用过多预算")
    result = []
    start = 0
    while start < len(text):
        low, high = start + 1, len(text)
        best = start
        while low <= high:
            mid = (low + high) // 2
            if counter(prefix + text[start:mid]) <= budget:
                best, low = mid, mid + 1
            else:
                high = mid - 1
        if best <= start:
            raise ValueError("预算无法容纳一个字符")
        end = best
        if end < len(text):
            boundary = text.rfind("\n\n", start + (end - start) * 3 // 4, end)
            if boundary > start:
                end = boundary + 2
        value = prefix + text[start:end]
        # 最终再次计算，不能依赖分词数量对字符长度严格单调。
        while counter(value) > budget and end > start:
            end -= 1
            value = prefix + text[start:end]
        if end <= start:
            raise ValueError("切片无法前进")
        if text[start:end].strip():
            result.append(dict(start=start, end=end, text=value, tokens=counter(value)))
        if end == len(text):
            break
        next_start = end
        if overlap:
            low, high = start + 1, end
            while low <= high:
                mid = (low + high) // 2
                if counter(text[mid:end]) <= overlap:
                    next_start, high = mid, mid - 1
                else:
                    low = mid + 1
        start = max(start + 1, next_start)
    return result


def covered(start, end, ranges):
    cursor = start
    for left, right in sorted(ranges):
        if left > cursor:
            break
        cursor = max(cursor, right)
        if cursor >= end:
            return True
    return False


def score(groups, hits, mapping):
    found, single = [], []
    for alternatives in groups:
        union_ok, single_ok = False, False
        for evidence in alternatives:
            ranges = [(mapping[i]["start"], mapping[i]["end"]) for i, _ in hits
                      if mapping[i]["document"] == evidence["document"]]
            union_ok |= covered(evidence["start"], evidence["end"], ranges)
            single_ok |= any(a <= evidence["start"] and b >= evidence["end"] for a, b in ranges)
        found.append(union_ok)
        single.append(single_ok)
    return dict(recall=sum(found) / len(found) if found else None,
                all_required=all(found) if found else None, groups_found=found,
                single_chunk_recall=sum(single) / len(single) if single else None)


def load_dataset(path, *, expected_split="dev"):
    dataset = json.loads(path.read_text(encoding="utf-8"))
    texts = {}
    for doc in dataset["documents"]:
        parsed, original = ROOT / doc["parsed_path"], ROOT / doc["original_path"]
        # 用户将根目录resouce更名为resources；旧冻结输入保留当时路径，仍核对原件字节哈希。
        relative_original = Path(doc["original_path"])
        if relative_original.parts and relative_original.parts[0] == "resouce":
            original = ROOT / "resources" / Path(*relative_original.parts[1:])
        if sha(parsed) != doc["parsed_sha256"] or sha(original) != doc["original_sha256"]:
            raise ValueError("语料或原件指纹与冻结清单不符")
        texts[doc["id"]] = parsed.read_text(encoding="utf-8")
    if len(texts) != len(dataset["documents"]):
        raise ValueError("重复文档ID")
    for case in dataset["cases"]:
        if case["split"] != expected_split:
            raise ValueError("题集分组与本次明确选择的分组不符")
        if not set(case["scope"]) <= texts.keys():
            raise ValueError("范围含未知文档")
        for alternatives in case["evidence_groups"]:
            for evidence in alternatives:
                text = texts[evidence["document"]]
                if text.count(evidence["quote"]) != 1:
                    raise ValueError("依据不存在或出现多次，需明确归属")
                evidence["start"] = text.index(evidence["quote"])
                evidence["end"] = evidence["start"] + len(evidence["quote"])
                if evidence["document"] not in case["scope"]:
                    raise ValueError("正例依据不在允许范围")
    return dataset, texts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--output", type=Path, help="逐次输出目录；默认.tmp，结束后按主题归并")
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("重复次数必须为正")
    dataset, texts = load_dataset(args.dataset)
    attempts = []
    def deny(*args, **kwargs):
        attempts.append("blocked")
        raise RuntimeError("真实文档切片实验禁止网络连接")
    socket.socket.connect = deny
    socket.socket.connect_ex = deny
    from briefly.llm import create_local_embeddings
    model = create_local_embeddings(str(ROOT / ".tmp/w3-models/bge-small-zh-v1.5"), threads=4)
    stamp = datetime.now(ZoneInfo("Australia/Sydney")).strftime("%Y%m%d-%H%M%S-%f")
    output = args.output or ROOT / ".tmp" / ("w3-chunk-pilot-" + stamp)
    output.mkdir(parents=True, exist_ok=False)
    save(output / "frozen.json", dict(configurations=CONFIGS, dataset_sha256=sha(args.dataset),
                                    model=model.identity, repeats=args.repeats, split="dev"))
    save(output / "dataset.json", dataset)
    queries = {c["id"]:model.embed_query(c["question"]) for c in dataset["cases"]}
    summaries = []
    raw = []
    for cfg in CONFIGS:
        mapping = {}
        for doc in dataset["documents"]:
            for chunk in chunks(texts[doc["id"]], doc["title"] if cfg["title"] else "", model.token_length,
                                budget=cfg["budget"], overlap=cfg["overlap"]):
                mapping[len(mapping) + 1] = dict(document=doc["id"], **chunk)
        save(output / (cfg["name"] + "-chunks.json"), mapping)
        started = time.perf_counter()
        vectors = model.embed_documents([c["text"] for c in mapping.values()])
        encode_ms = (time.perf_counter() - started) * 1000
        index = CandidateIndex(output / (cfg["name"] + ".db"), model.identity, backend="scalar")
        try:
            for doc in dataset["documents"]:
                index.replace_document(doc["id"], [(i, c["text"], vectors[i-1]) for i,c in mapping.items()
                                                    if c["document"] == doc["id"]])
            rows = []
            for repeat in range(args.repeats):
                for case in dataset["cases"]:
                    dense = index.dense(queries[case["id"]], case["scope"], 20)
                    lexical = index.lexical(case["question"], case["scope"], 20)
                    for method, ranking in (("dense", dense), ("lexical", lexical), ("rrf", rrf([dense, lexical]))):
                        for k in (3, 5, 10):
                            hits = ranking[:k]
                            if any(mapping[i]["document"] not in case["scope"] for i, _ in hits):
                                raise ValueError("发现范围泄漏")
                            rows.append(dict(config=cfg["name"], case=case["id"], repeat=repeat+1,
                                             method=method, k=k, hits=hits, candidate_count=len(hits),
                                             **score(case["evidence_groups"], hits, mapping)))
            raw.extend(rows)
            for method in ("dense", "lexical", "rrf"):
                for k in (3, 5, 10):
                    first = [r for r in rows if r["method"] == method and r["k"] == k and r["repeat"] == 1]
                    positive = [r for r in first if r["recall"] is not None]
                    negative = [r for r in first if r["recall"] is None]
                    summaries.append(dict(config=cfg["name"], method=method, k=k, chunks=len(mapping), encode_ms=encode_ms,
                                          positive_cases=len(positive), recall=sum(r["recall"] for r in positive)/len(positive),
                                          all_required_rate=sum(r["all_required"] for r in positive)/len(positive),
                                          single_chunk_recall=sum(r["single_chunk_recall"] for r in positive)/len(positive),
                                          negative_cases=len(negative), negatives_with_candidates=sum(r["candidate_count"] > 0 for r in negative)))
        finally:
            index.close()
        save(output / "raw.json", raw)
        save(output / "summary.json", summaries)
        print(cfg["name"], len(mapping), "chunks", flush=True)
    snapshot_paths = [Path(__file__).resolve(), ROOT / "tests/eval/w3_retrieval_support.py", ROOT / "src/briefly/local_embeddings.py"]
    hashes = {}
    for path in snapshot_paths:
        relative = path.relative_to(ROOT)
        target = output / "source-snapshot" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(path.read_bytes())
        hashes[str(relative)] = sha(path)
    save(output / "manifest.json", dict(dataset_sha256=sha(args.dataset), source_sha256=hashes,
         network_attempts=len(attempts), external_inference_calls=0, model=model.identity,
         limitations="仅人事制度开发样本、既有解析快照；不是生产质量复核。全部问题参与分析，不称独立验证。只比较BGE-small切片配置，不代表多模型结论。未调用生成，候选存在不代表答案/拒答通过。"))
    lines = ["# W3-02 真实长文切片开发实验", "", f"{len(texts)}份此前已解析的DOC快照，{len(dataset['cases'])}道开发题；各配置重复{args.repeats}次。重复不增加独立题数。", "",
             "只在本地读取、向量化和检索，不调用生成。原件和解析文本均按清单指纹核对；引用按文档和原文字符区间计分，重叠片段不会重复加分。标题计入token预算；标签不入索引。", "",
             "全部题用于开发分析，不称留出/独立验证；语料主要是人事制度，不能代表项目资料和产品手册。样本中的历史制度条款只作为原文检索目标，不作当前制度或法律建议。", ""]
    for cfg in CONFIGS:
        lines.extend(["## " + cfg["name"], ""])
        for row in summaries:
            if row["config"] == cfg["name"] and row["k"] in (3, 5):
                lines.append(f"- {row['method']} @{row['k']}：{row['chunks']}块；必要依据平均覆盖{row['recall']:.2%}；全部依据齐全题占比{row['all_required_rate']:.2%}；完整单块依据覆盖{row['single_chunk_recall']:.2%}；无答案/范围外题中有候选{row['negatives_with_candidates']}/{row['negative_cases']}（不是幻觉率）。")
        lines.append("")
    lines.extend(["raw.json保留逐题/逐次/逐K命中和依据覆盖；dataset.json记录样本来源、原件/解析快照哈希、问题、范围和依据偏移。*-chunks.json可回原文核对。", "",
                  "尚未比较第二Embedding模型、768token长上下文方案、自动邻接扩展及生产切片器；未验收解析、正式问答或独立验证集。token窗口属于实验配置，不自动改变现有材料或块号。", ""])
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(str(output), flush=True)
    if attempts:
        raise SystemExit("发现网络尝试，实验不通过")


if __name__ == "__main__":
    main()
