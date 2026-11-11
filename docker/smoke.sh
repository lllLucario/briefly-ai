#!/usr/bin/env bash
# 镜像冒烟检查：服务能启动、不以 root 运行，随镜像带的转换程序和本地模型真的能用。不调用真实模型。
# 用法：docker/smoke.sh <镜像> [PostgreSQL 地址]
# 第二个参数给了就再用 PostgreSQL 后端启动一次（容器用主机网络连接它）。
set -euo pipefail

image="$1"
pg_url="${2:-}"
name="briefly-smoke-$$"
trap 'docker rm -f "$name" >/dev/null 2>&1 || true' EXIT

start() {  # 其余参数原样传给 docker run
  docker rm -f "$name" >/dev/null 2>&1 || true
  docker run -d --name "$name" -e BRIEFLY_API_KEY=fake-key-for-smoke -e BRIEFLY_ACCESS_TOKEN=smoke-only-token-not-a-secret "$@" "$image" >/dev/null
  for _ in $(seq 60); do
    if docker exec "$name" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)" 2>/dev/null; then
      return 0
    fi
    if [ "$(docker inspect -f '{{.State.Running}}' "$name")" != "true" ]; then break; fi
    sleep 2
  done
  echo "服务没有就绪，容器日志：" >&2
  docker logs "$name" >&2 || true
  return 1
}

py() { docker exec -i "$name" python -; }

echo "== 默认 SQLite 启动"
start
test "$(docker exec "$name" id -u)" != "0"
docker exec "$name" test -f data/app.db

echo "== 没有访问令牌时拒绝启动"
if docker run --rm -e BRIEFLY_API_KEY=fake-key-for-smoke "$image" >/dev/null 2>&1; then
  echo "没有访问令牌也启动了" >&2
  exit 1
fi

echo "== sqlite-vec 和 FTS5"
py <<'EOF'
import sqlite3, sqlite_vec
db = sqlite3.connect(":memory:")
db.enable_load_extension(True)
sqlite_vec.load(db)
db.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
print(sqlite3.sqlite_version, db.execute("select vec_version()").fetchone()[0])
EOF

echo "== 本地 Embedding 模型（核对清单哈希并实际推理）"
py <<'EOF'
from briefly import config, llm
embeddings = llm.create_local_embeddings(config.EMBEDDING_DIR)
vector = embeddings.embed_query("年假怎么申请")
assert len(vector) == 512, len(vector)
print(embeddings.identity["revision"], len(vector))
EOF

echo "== 图片文字识别依赖"
py <<'EOF'
import onnxruntime, rapidocr
print(onnxruntime.__version__)
EOF

echo "== 旧版 Word：LibreOffice 生成 .doc，再走直接解析和转换两条路径"
docker exec "$name" java -version
py <<'EOF'
import subprocess, tempfile
from pathlib import Path
from briefly import direct_word, legacy_word
from briefly.loaders import load_document

assert direct_word.direct_available(), "Java 或 Tika 不可用"
with tempfile.TemporaryDirectory() as folder:
    source = Path(folder) / "sample.txt"
    source.write_text("镜像冒烟检查\n年假需要提前三个工作日申请。\n", encoding="utf-8")
    subprocess.run([legacy_word.converter_path(), "--headless", f"-env:UserInstallation=file://{folder}/profile",
                    "--convert-to", "doc:MS Word 97", "--outdir", folder, str(source)],
                   check=True, timeout=120, stdout=subprocess.DEVNULL)
    data = (Path(folder) / "sample.doc").read_bytes()
from briefly import config
for mode in ("direct", "convert"):  # 两条路径都要能读出原文
    config.DOC_PARSER = mode
    doc = load_document("sample.doc", data)
    assert "提前三个工作日" in doc.text, (mode, doc.text[:200])
    print(mode, "ok", doc.report.get("conversion", "direct"))
EOF

if [ -n "$pg_url" ]; then
  echo "== PostgreSQL 后端启动并建表"
  start --network host -e DATABASE_URL="$pg_url"
  py <<'EOF'
import os, psycopg
with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
    version = connection.execute("select version_num from alembic_version").fetchone()
    vector = connection.execute("select extversion from pg_extension where extname = 'vector'").fetchone()
assert version and vector, (version, vector)
print("alembic", version[0], "pgvector", vector[0])
EOF
fi

echo "冒烟检查通过"
