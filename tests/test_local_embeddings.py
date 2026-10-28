"""本地适配器不加载真实模型；验证离线参数与长度/文件完整性。"""
import hashlib
import json
import sys
from types import SimpleNamespace

import pytest

from briefly.llm import create_local_embeddings


@pytest.fixture
def local_model(tmp_path,monkeypatch):
    from briefly.local_embeddings import LocalBgeEmbeddings
    files=[]
    for name in ("model_optimized.onnx","tokenizer.json","config.json","tokenizer_config.json","special_tokens_map.json"):
        (tmp_path/name).write_bytes(b"fixture")
        files.append(dict(file=name,sha256=hashlib.sha256(b"fixture").hexdigest()))
    (tmp_path/"prepared.json").write_text(json.dumps(dict(model="BAAI/bge-small-zh-v1.5",revision="fake",files=files)),encoding="utf-8")
    options=[]
    class Tokenizer:
        limit = None
        @staticmethod
        def from_file(path):
            return Tokenizer()
        def no_truncation(self):
            pass
        def no_padding(self):
            pass
        def enable_truncation(self, max_length):
            self.limit = max_length
        def encode(self,text):
            n = min(len(text)+2, self.limit) if self.limit else len(text)+2
            return SimpleNamespace(ids=list(range(n)))
    class Model:
        def __init__(self,**kwargs):
            options.append(kwargs)
            self.model = SimpleNamespace(tokenizer=Tokenizer())
            self.dimension = 768 if kwargs["model_name"].startswith("jinaai/") else LocalBgeEmbeddings.dimension
        def embed(self,texts,**kwargs):
            for text in texts:
                yield SimpleNamespace(tolist=lambda:[1.]+[0.]*(self.dimension-1))
    monkeypatch.setitem(sys.modules,"fastembed",SimpleNamespace(TextEmbedding=Model))
    monkeypatch.setitem(sys.modules,"tokenizers",SimpleNamespace(Tokenizer=Tokenizer))
    return tmp_path,options


def test_factory_is_offline_and_langchain_compatible(local_model):
    from langchain_core.embeddings import Embeddings
    folder,options=local_model
    model=create_local_embeddings(str(folder))
    assert isinstance(model,Embeddings)
    assert options[0]["local_files_only"] is True
    assert options[0]["providers"]==["CPUExecutionProvider"]
    assert len(model.embed_query("问题"))==512
    assert len(model.embed_documents(["材料"]))==1
    assert model.embed_documents([])==[]


def test_no_silent_truncation(local_model):
    model=create_local_embeddings(str(local_model[0]))
    with pytest.raises(ValueError,match="未静默截断"):
        model.embed_documents(["字"*511])
    with pytest.raises(ValueError,match="未静默截断"):
        model.embed_query("字"*500)
    with pytest.raises(ValueError,match="不能为空"):
        model.embed_query(" ")


def test_modified_model_file_is_rejected(local_model):
    folder,_=local_model
    (folder/"model_optimized.onnx").write_bytes(b"modified")
    with pytest.raises(ValueError,match="清单不一致"):
        create_local_embeddings(str(folder))


def prepare_fake_jina(folder):
    manifest = json.loads((folder/"prepared.json").read_text(encoding="utf-8"))
    manifest["model"] = "jinaai/jina-embeddings-v2-base-zh"
    (folder/"onnx").mkdir()
    (folder/"model_optimized.onnx").rename(folder/"onnx/model.onnx")
    (folder/"config.json").write_text(json.dumps(dict(model_max_length=8192)),encoding="utf-8")
    (folder/"tokenizer_config.json").write_text(json.dumps(dict(model_max_length=512)),encoding="utf-8")
    for entry in manifest["files"]:
        if entry["file"] == "model_optimized.onnx":
            entry["file"] = "onnx/model.onnx"
        entry["sha256"] = hashlib.sha256((folder/entry["file"]).read_bytes()).hexdigest()
    (folder/"prepared.json").write_text(json.dumps(manifest),encoding="utf-8")


def test_jina_default_respects_artifact_tokenizer_limit(local_model):
    folder,options = local_model
    prepare_fake_jina(folder)
    model = create_local_embeddings(str(folder))
    assert model.max_tokens == 512
    assert model.identity["pooling"] == "mean"
    assert model.identity["query_prefix"] == ""
    assert len(model.embed_query("问题")) == 768
    assert options[-1]["local_files_only"] is True


def test_jina_explicit_long_budget_changes_runtime_and_identity(local_model):
    folder,_ = local_model
    prepare_fake_jina(folder)
    default = create_local_embeddings(str(folder))
    long = create_local_embeddings(str(folder),max_input_tokens=768)
    assert long.identity != default.identity
    assert long.max_tokens == long._model.model.tokenizer.limit == 768
    assert long.token_length("字"*600) == len(long._model.model.tokenizer.encode("字"*600).ids) == 602
    assert len(long.embed_documents(["字"*600])[0]) == 768
    with pytest.raises(ValueError,match="未静默截断"):
        long.embed_documents(["字"*767])
    with pytest.raises(ValueError,match="预算"):
        create_local_embeddings(str(folder),max_input_tokens=8193)


def test_embedding_unknown_model_and_manifest_escape_are_rejected(local_model):
    folder,_ = local_model
    path = folder/"prepared.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["files"].append(dict(file="../outside",sha256="fake"))
    path.write_text(json.dumps(manifest),encoding="utf-8")
    with pytest.raises(ValueError,match="清单不一致"):
        create_local_embeddings(str(folder))
    manifest["model"] = "unapproved/model"
    path.write_text(json.dumps(manifest),encoding="utf-8")
    with pytest.raises(ValueError,match="白名单"):
        create_local_embeddings(str(folder))
