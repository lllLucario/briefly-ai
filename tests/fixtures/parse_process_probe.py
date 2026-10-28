"""隔离协议/进程树故障替身；不加载解析器或模型。"""
import json
from pathlib import Path
import subprocess
import sys
from time import sleep

folder, mode = Path(sys.argv[1]), sys.argv[2]
while not (folder / "start").exists():
    sleep(0.01)
if mode == "tree":
    marker = sys.argv[3]
    subprocess.Popen([sys.executable, "-c", "from pathlib import Path; from time import sleep; import sys; sleep(2); Path(sys.argv[1]).write_text('leaked')", marker])
    (folder / "spawned").touch()
    sleep(60)
elif mode == "exit":
    raise SystemExit(3)
elif mode == "bad":
    (folder / "result.json").write_text("{broken")
elif mode == "empty":
    (folder / "result.json").write_text(json.dumps(dict(text=" ", markdown=True, report={})))
elif mode == "error":
    (folder / "result.json").write_text(json.dumps(dict(error="文件损坏，请核对原件")))
