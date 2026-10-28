"""仅下载公开 Embedding 文件；不读语料、.env、业务库，不调用云端推理。"""
import hashlib
import argparse
import json
from pathlib import Path
import time

import requests

ROOT = Path(__file__).resolve().parents[2]
DEST = ROOT / ".tmp" / "w3-models" / "bge-small-zh-v1.5"
REPOSITORY = "Qdrant/bge-small-zh-v1.5"


def main():
    global DEST, REPOSITORY
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screen-minilm", action="store_true", help="仅下载第二候选的分词配置，不下载权重")
    args=parser.parse_args()
    if args.screen_minilm:
        DEST=ROOT/".tmp"/"w3-models"/"minilm-screen"
        REPOSITORY="Qdrant/paraphrase-multilingual-MiniLM-L12-v2-onnx-Q"
    DEST.mkdir(parents=True, exist_ok=True)
    response = requests.get(f"https://huggingface.co/api/models/{REPOSITORY}", timeout=30)
    response.raise_for_status()
    metadata = response.json()
    revision = metadata["sha"]
    files = [s["rfilename"] for s in metadata["siblings"] if s["rfilename"] in {
        "model_optimized.onnx", "config.json", "tokenizer.json", "tokenizer_config.json",
        "special_tokens_map.json", "vocab.txt", "README.md", "LICENSE",
    }]
    if args.screen_minilm:
        files=[name for name in files if not name.endswith(".onnx")]
    started = time.perf_counter()
    records = []
    for name in files:
        target = DEST / name
        with requests.get(f"https://huggingface.co/{REPOSITORY}/resolve/{revision}/{name}", stream=True, timeout=(30, 120)) as download:
            download.raise_for_status()
            with target.open("wb") as output:
                for block in download.iter_content(1024 * 1024):
                    output.write(block)
        records.append(dict(file=name, bytes=target.stat().st_size, sha256=hashlib.sha256(target.read_bytes()).hexdigest()))
        print(name, target.stat().st_size, flush=True)
    manifest = dict(model="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2" if args.screen_minilm else "BAAI/bge-small-zh-v1.5", repository=REPOSITORY, revision=revision,
                    files=records, seconds=time.perf_counter()-started,
                    bytes=sum(r["bytes"] for r in records), license="Apache-2.0" if args.screen_minilm else "MIT", external_inference_calls=0)
    (DEST / "prepared.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
