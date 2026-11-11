# syntax=docker/dockerfile:1
# Briefly AI 服务镜像：后端 + 网页，自带旧格式转换（LibreOffice、Java + Tika）和本地 Embedding 模型。
# 由 .github/workflows/image.yml 在 GitHub Actions 上构建并推送到 GHCR，本机不需要构建。
# 只支持 linux/amd64（uv.lock 只锁了这个平台）。

FROM ghcr.io/astral-sh/uv:0.13.7 AS uv

# ---- 依赖和项目：按锁文件装进 /app/.venv ----
FROM python:3.14-slim-trixie AS build
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# 先只装第三方依赖：源码改动不会让这一层失效
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --extra rag --extra ocr --extra postgres
COPY pyproject.toml uv.lock ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-editable --extra rag --extra ocr --extra postgres

# ---- 外部文件：固定版本并核对 SHA256，内容变了构建直接失败 ----
FROM scratch AS assets
# 本地 Embedding 模型。版本和文件哈希进入索引指纹，必须和 docker/bge-small-zh-v1.5.prepared.json 一致，
# 换版本等于换模型：已有索引要重建，相关度下限要复标。
ARG BGE=https://huggingface.co/Qdrant/bge-small-zh-v1.5/resolve/46fbe35fd4374a00fee7de77dfddaeb6dd6a2c59
ADD --chmod=644 --checksum=sha256:d8d7a46d41a1a37fe4f0a5f637bf55c649310185329127d8a2204632e480be17 ${BGE}/README.md /models/bge-small-zh-v1.5/README.md
ADD --chmod=644 --checksum=sha256:9088751d39abbf86ec3d19ffca92ad62ad19075f7e59712e6c71217fa125d1d3 ${BGE}/config.json /models/bge-small-zh-v1.5/config.json
ADD --chmod=644 --checksum=sha256:1294ea4b6331115a353d81f96b85e8c8d7fdcc284453d5b2fab5b016230aad38 ${BGE}/model_optimized.onnx /models/bge-small-zh-v1.5/model_optimized.onnx
ADD --chmod=644 --checksum=sha256:b6d346be366a7d1d48332dbc9fdf3bf8960b5d879522b7799ddba59e76237ee3 ${BGE}/special_tokens_map.json /models/bge-small-zh-v1.5/special_tokens_map.json
ADD --chmod=644 --checksum=sha256:48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26 ${BGE}/tokenizer.json /models/bge-small-zh-v1.5/tokenizer.json
ADD --chmod=644 --checksum=sha256:e6f3b96db926a37d4039995fbf5ad17de158dfb8f6343d607e4dbaad18d75f5a ${BGE}/tokenizer_config.json /models/bge-small-zh-v1.5/tokenizer_config.json
ADD --chmod=644 --checksum=sha256:45bbac6b341c319adc98a532532882e91a9cefc0329aa57bac9ae761c27b291c ${BGE}/vocab.txt /models/bge-small-zh-v1.5/vocab.txt
COPY docker/bge-small-zh-v1.5.prepared.json /models/bge-small-zh-v1.5/prepared.json
# 旧版 .doc/.ppt 直接解析用的 Tika
ADD --chmod=644 --checksum=sha256:f29d4e0d2cff1fc32396f6ac5ce436dfc7b19f9303d3b899e1f56fde85711c96 https://archive.apache.org/dist/tika/3.3.3/tika-app-3.3.3.jar /tika/tika-app-3.3.3.jar

# ---- 运行镜像 ----
FROM python:3.14-slim-trixie
# LibreOffice（不带界面）做旧格式转换的兜底；Java 给 Tika 和随包的解析桥接用
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libreoffice-writer-nogui libreoffice-calc-nogui libreoffice-impress-nogui \
        default-jre-headless \
    && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 10001 briefly \
    && mkdir -p /app/data \
    && chown briefly:briefly /app /app/data
COPY --from=assets /models /opt/briefly/models
COPY --from=assets /tika /opt/briefly/tika
COPY --from=build /app/.venv /app/.venv

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    BRIEFLY_HOST=0.0.0.0 \
    BRIEFLY_PORT=8000 \
    BRIEFLY_EMBEDDING_DIR=/opt/briefly/models/bge-small-zh-v1.5 \
    BRIEFLY_LIBREOFFICE=/usr/bin/soffice \
    BRIEFLY_JAVA=/usr/bin/java \
    BRIEFLY_TIKA_APP_JAR=/opt/briefly/tika/tika-app-3.3.3.jar
WORKDIR /app
USER briefly
# 默认的 SQLite 库、知识库索引、追踪快照都在 /app/data，挂卷才能在容器重建后保留
VOLUME /app/data
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]
# 绑定 0.0.0.0 时必须设置 BRIEFLY_ACCESS_TOKEN，否则服务拒绝启动
CMD ["briefly-api"]
