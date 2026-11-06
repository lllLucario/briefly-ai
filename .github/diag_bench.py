"""诊断（只在诊断分支）：量 CPU、文件、sqlite、进程启动、建库建应用各花多少时间。"""
import json
import os
import platform
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()) / "diag-bench"
ROOT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, "src")
out = {"platform": platform.platform(), "cpus": os.cpu_count()}


def timed(name, fn, n=1):
    start = time.perf_counter()
    for i in range(n):
        fn(i)
    out[name] = round((time.perf_counter() - start) * 1000 / n, 2)  # 每次毫秒


def cpu(_):
    total = 0
    for i in range(2_000_000):
        total += i * i


def churn(sync):
    def run(i):
        path = ROOT / f"f{i}.bin"
        with open(path, "wb") as f:
            f.write(b"x" * 4096)
            if sync:
                f.flush()
                os.fsync(f.fileno())
        path.unlink()
    return run


def sqlite_commits(_):
    path = ROOT / "commit.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS t (x)")
    for i in range(20):
        db.execute("INSERT INTO t VALUES (?)", (i,))
        db.commit()
    db.close()
    path.unlink()


timed("cpu_2m_loop_ms", cpu, 3)
timed("file_create_delete_ms", churn(False), 500)
timed("file_create_fsync_delete_ms", churn(True), 200)
timed("sqlite_20_commits_ms", sqlite_commits, 5)
timed("spawn_python_ms", lambda _: subprocess.run([sys.executable, "-c", "pass"], check=True), 5)
timed("import_briefly_api_ms", lambda _: subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, 'src'); import briefly.api"], check=True), 2)

from briefly.api import create_app  # noqa: E402
from briefly.db import create_db_engine  # noqa: E402
from briefly.materials import MaterialStore  # noqa: E402


def build_engine(i):
    engine = create_db_engine(f"sqlite:///{(ROOT / f'e{i}.db').as_posix()}")
    engine.dispose()


def build_memory(_):
    create_db_engine("sqlite://").dispose()


timed("create_db_engine_file_ms", build_engine, 3)
timed("create_db_engine_memory_ms", build_memory, 3)
engine = create_db_engine(f"sqlite:///{(ROOT / 'app.db').as_posix()}")
timed("create_app_ms", lambda _: create_app(MaterialStore(engine)), 3)
print("DIAG_BENCH " + json.dumps(out, ensure_ascii=False))
