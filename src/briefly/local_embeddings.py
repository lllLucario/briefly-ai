"""LangChain 本地 CPU Embeddings 适配；实例由 llm.create_local_embeddings 创建。"""
import hashlib
import json
from pathlib import Path

from langchain_core.embeddings import Embeddings


class _LocalOnnxEmbeddings(Embeddings):
    dimension = 512
    max_tokens = 512
    query_prefix = "为这个句子生成表示以用于检索相关文章："
    model_name = "BAAI/bge-small-zh-v1.5"
    model_file = "model_optimized.onnx"
    pooling = "CLS"
    batch_size = 8

    def __init__(self, model_dir: str, *, threads: int = 4, max_input_tokens: int | None = None):
        from fastembed import TextEmbedding
        from tokenizers import Tokenizer

        folder = Path(model_dir).resolve()
        manifest = json.loads((folder / "prepared.json").read_text(encoding="utf-8"))
        if manifest["model"] != self.model_name:
            raise ValueError("本地模型与适配器不匹配")
        if threads < 1:
            raise ValueError("CPU线程数必须为正")
        required = {self.model_file, "tokenizer.json", "config.json", "tokenizer_config.json", "special_tokens_map.json"}
        names = {entry["file"] for entry in manifest["files"]}
        if not required <= names or len(names) != len(manifest["files"]):
            raise ValueError("本地模型文件清单不完整")
        for item in manifest["files"]:
            path = (folder / item["file"]).resolve()
            if not path.is_relative_to(folder) or not path.is_file():
                raise ValueError("本地模型文件与准备清单不一致")
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda:stream.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != item["sha256"]:
                raise ValueError("本地模型文件与准备清单不一致")
        if self.model_name == "jinaai/jina-embeddings-v2-base-zh":
            options = json.loads((folder / "tokenizer_config.json").read_text(encoding="utf-8"))
            limits = [options[key] for key in ("model_max_length", "max_length")
                      if isinstance(options.get(key), int) and not isinstance(options[key], bool) and options[key] > 0]
            if not limits:
                raise ValueError("本地分词器没有可核对的长度预算")
            if max_input_tokens is None:
                self.max_tokens = min(self.max_tokens, *limits)
            else:
                architecture = json.loads((folder / "config.json").read_text(encoding="utf-8"))
                if architecture.get("model_max_length") != 8192:
                    raise ValueError("长上下文模型配置与已验证架构不匹配")
        if max_input_tokens is not None:
            if isinstance(max_input_tokens, bool) or not isinstance(max_input_tokens, int) or not 1 <= max_input_tokens <= self.max_tokens:
                raise ValueError("显式输入预算超过模型上限或无效")
            self.max_tokens = max_input_tokens
        self.identity = dict(model=manifest["model"], revision=manifest["revision"], dimension=self.dimension,
                             artifact_sha256={i["file"]: i["sha256"] for i in manifest["files"]},
                             query_prefix=self.query_prefix, pooling=self.pooling, normalize=True, max_tokens=self.max_tokens)
        self.tokenizer = Tokenizer.from_file(str(folder / "tokenizer.json"))
        self.tokenizer.no_truncation()
        self.tokenizer.no_padding()
        self._model = TextEmbedding(model_name=manifest["model"], specific_model_path=str(folder),
                                    local_files_only=True, threads=threads, providers=["CPUExecutionProvider"])
        if max_input_tokens is not None:
            # FastEmbed 0.9.x按tokenizer_config默认截断（此Jina制品默认512）。
            # 显式预算必须同时作用于实际运行分词器；不修改带哈希的原始下载配置。
            runtime_tokenizer = getattr(getattr(self._model, "model", None), "tokenizer", None)
            if runtime_tokenizer is None:
                raise ValueError("无法设置实际运行分词器的输入预算")
            runtime_tokenizer.enable_truncation(max_length=self.max_tokens)

    def token_length(self, text: str) -> int:
        return len(self.tokenizer.encode(text).ids)

    def _check(self, texts: list[str]):
        if any(not t.strip() for t in texts):
            raise ValueError("Embedding 输入不能为空")
        lengths = [self.token_length(t) for t in texts]
        if any(n > self.max_tokens for n in lengths):
            raise ValueError(f"文本超过本地 Embedding 的{self.max_tokens} token上限，请重新切片；未静默截断")

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self._check(texts)
        return [row.tolist() for row in self._model.embed(texts, batch_size=self.batch_size)] if texts else []

    def embed_query(self, text: str) -> list[float]:
        if not text.strip():
            raise ValueError("检索问题不能为空")
        value = self.query_prefix + text
        self._check([value])
        return next(iter(self._model.embed([value]))).tolist()


class LocalBgeEmbeddings(_LocalOnnxEmbeddings):
    """保留原BGE-small参数及身份，既有索引不改变。"""


class LocalJinaEmbeddings(_LocalOnnxEmbeddings):
    """第二候选，仅预备好的ONNX文件；不执行远端模型Python。"""
    model_name = "jinaai/jina-embeddings-v2-base-zh"
    model_file = "onnx/model.onnx"
    dimension = 768
    max_tokens = 8192
    query_prefix = ""
    pooling = "mean"
    batch_size = 2
