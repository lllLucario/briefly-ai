"""整任务隔离worker；磁盘传输有限结果，不使用pickle或业务数据库。"""
import json
from pathlib import Path
import sys
from time import monotonic, sleep


def main():
    folder = Path(sys.argv[1])
    deadline = monotonic() + 30
    while not (folder / "start").exists():
        if monotonic() > deadline:
            return 1
        sleep(0.01)
    from . import config
    from .loaders import load_document
    from .parse_process import MAX_RESULT_BYTES

    request = json.loads((folder / "request.json").read_text(encoding="utf-8"))
    for key, value in request["settings"].items():
        setattr(config, key, value)
    try:
        doc = load_document(request["filename"], (folder / "input").read_bytes())
        result = dict(text=doc.text, markdown=doc.markdown, report=doc.report)
    except ValueError as exc:
        result = dict(error=str(exc))
    payload = json.dumps(result, ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_RESULT_BYTES:
        return 2
    (folder / "result.json").write_bytes(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
