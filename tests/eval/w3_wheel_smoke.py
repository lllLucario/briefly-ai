"""在独立目录加载本地wheel，校验入口/静态文件及隔离解析；复用当前依赖，不联网。"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    with ZipFile(args.wheel) as archive:
        if any(not (root / name).resolve().is_relative_to(root) for name in archive.namelist()):
            raise ValueError("安装包含越界路径")
        archive.extractall(root)
    script = """
import json
from pathlib import Path
import briefly
from fastapi.testclient import TestClient
from briefly.api import create_app
from briefly.db import create_db_engine
from briefly.materials import MaterialStore
from briefly.parse_process import load_isolated
assert Path(briefly.__file__).resolve().is_relative_to(Path.cwd())
engine = create_db_engine('sqlite://')
try:
    with TestClient(create_app(MaterialStore(engine))) as client:
        assert client.get('/').status_code == 200
        assert client.get('/web/knowledge.js').status_code == 200
        kb = client.post('/knowledge-bases', json={'name':'wheel-smoke'}).json()['id']
        assert client.post('/knowledge-scope', json={'kb_ids':[kb]}).json()['status'] == 'empty_library'
    assert load_isolated('smoke.txt', b'packaged parser').text == 'packaged parser'
finally:
    engine.dispose()
print(json.dumps(dict(package=str(Path(briefly.__file__).resolve()),api=True,web=True,worker=True)))
"""
    env = {**os.environ, "PYTHONPATH":str(root), "PYTHON_DOTENV_DISABLED":"1", "PYTHONIOENCODING":"utf-8"}
    options = {"creationflags":subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    result = subprocess.run([sys.executable, "-c", script], cwd=root, env=env,
        stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", timeout=60, check=True, **options)
    report = json.loads(result.stdout)
    (root.parent / "wheel-smoke.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
