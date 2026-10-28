"""仅下载固定版本公开Jina ONNX/配置；不读.env、语料或业务库，不执行远端Python。"""
import hashlib
import json
from pathlib import Path
import time

import requests

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY = "jinaai/jina-embeddings-v2-base-zh"
REVISION = "c1ff9086a89a1123d7b5eff58055a665db4fb4b9"
FILES = ("README.md", "config.json", "onnx/model.onnx", "special_tokens_map.json", "tokenizer.json", "tokenizer_config.json")


def main():
    folder = ROOT / ".tmp/w3-models/jina-v2-base-zh"
    folder.mkdir(parents=True, exist_ok=True)
    if (folder / "prepared.json").exists():
        raise SystemExit("已准备，拒绝覆盖固定模型清单")
    session = requests.Session()
    session.trust_env = False
    response = session.get(f"https://huggingface.co/api/models/{REPOSITORY}/revision/{REVISION}", params={"blobs":"true"}, timeout=30)
    response.raise_for_status()
    metadata = response.json()
    if metadata["sha"] != REVISION or metadata.get("gated"):
        raise ValueError("固定公开版本不可用")
    sizes = {f["rfilename"]:f for f in metadata["siblings"]}
    started = time.perf_counter()
    records = []
    for name in FILES:
        target = folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_suffix(target.suffix + ".part")
        digest = hashlib.sha256()
        with session.get(f"https://huggingface.co/{REPOSITORY}/resolve/{REVISION}/{name}", stream=True, timeout=(30,120)) as download:
            download.raise_for_status()
            with partial.open("wb") as stream:
                for block in download.iter_content(1024 * 1024):
                    stream.write(block)
                    digest.update(block)
        record = dict(file=name, bytes=partial.stat().st_size, sha256=digest.hexdigest())
        expected = sizes[name]
        if record["bytes"] != expected["size"] or (expected.get("lfs", {}).get("sha256") and record["sha256"] != expected["lfs"]["sha256"]):
            raise ValueError("公开文件大小或LFS哈希不符；未发布准备清单")
        partial.replace(target)
        records.append(record)
        print(name, record["bytes"], flush=True)
    manifest = dict(model=REPOSITORY, repository=REPOSITORY, revision=REVISION, files=records,
                    bytes=sum(r["bytes"] for r in records), license="Apache-2.0",
                    license_source=f"https://huggingface.co/{REPOSITORY}/blob/{REVISION}/README.md",
                    seconds=time.perf_counter()-started, external_inference_calls=0)
    (folder / "prepared.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print("prepared", manifest["bytes"], flush=True)


if __name__ == "__main__":
    main()
