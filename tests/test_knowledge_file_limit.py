"""知识库单文件上限（登记表第 96 条）：知识库入口按 MAX_KNOWLEDGE_FILE_MB，普通材料仍按 MAX_UPLOAD_MB。"""
import pytest

pytest.importorskip("sqlite_vec", reason="知识库上传回归需安装.[rag]")
from fastapi.testclient import TestClient

from briefly import config
from briefly.api import create_app
from briefly.knowledge.ingestion import ImportService
from briefly.knowledge.index import LocalIndex
from briefly.loaders import LoadedDocument
from briefly.materials import MaterialStore
from briefly.parse_process import ParseFailure, load_isolated
from db_util import file_engine
from test_knowledge_slicing import Encoder

MB = 1024 * 1024


def test_default_and_env_name():
    assert config.MAX_KNOWLEDGE_FILE_MB == 50 and config.MAX_UPLOAD_MB == 10


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
    engine = file_engine()
    app = create_app(MaterialStore(engine))
    imports = app.state.knowledge_imports
    imports.encoder_factory = Encoder
    seen = []

    def parser(filename, data):  # 不真解析大文件，只看到达解析的字节数
        seen.append(len(data))
        return LoadedDocument("# 制度\n审批须在预订前完成。", True)

    imports.parser = parser
    with TestClient(app) as c:
        c.seen = seen
        c.kb = c.post("/knowledge-bases", json={"name": "资料库"}).json()["id"]
        yield c
    engine.dispose()


def upload(client, *files, key="k"):
    return client.post(f"/knowledge-bases/{client.kb}/imports", data={"request_key": key, "tokens": "64"},
                       files=[("files", (name, b"x" * size)) for name, size in files])


@pytest.mark.parametrize("size", [11 * MB, 49 * MB, 50 * MB], ids=["11MB", "49MB", "exactly-50MB"])
def test_file_within_limit_is_accepted(client, size):
    response = upload(client, ("大文件.md", size))
    assert response.status_code == 202
    assert response.json()["files"][0]["status"] != "rejected"
    assert client.seen == [size]
    assert client.get(f"/knowledge-bases/{client.kb}/imports").json()[0]["status"] == "ready"


@pytest.mark.parametrize("size", [50 * MB + 1, 51 * MB], ids=["50MB-plus-1", "51MB"])
def test_oversized_file_is_rejected_per_file_with_the_limit(client, size):
    response = upload(client, ("过大.md", size))
    assert response.status_code == 202
    [rejected] = response.json()["files"]
    assert rejected["status"] == "rejected" and "单文件 50MB" in rejected["error"]
    assert client.seen == []
    assert client.get(f"/knowledge-bases/{client.kb}/imports").json() == []


def test_oversized_file_does_not_stop_the_rest_of_the_batch(client):
    response = upload(client, ("过大.md", 51 * MB), ("小.md", 1024))
    assert response.status_code == 202
    assert [f["status"] for f in response.json()["files"]][0] == "rejected"
    assert response.json()["files"][1]["status"] != "rejected"
    assert client.seen == [1024]


def test_batch_limit_is_unchanged(client):
    response = upload(client, ("一.md", 30 * MB), ("二.md", 30 * MB))
    assert response.status_code == 413 and "批次超过50MB" in response.json()["detail"]
    assert client.seen == []


def test_config_controls_the_limit(client, monkeypatch):
    monkeypatch.setattr(config, "MAX_KNOWLEDGE_FILE_MB", 2)
    [rejected] = upload(client, ("过大.md", 3 * MB)).json()["files"]
    assert rejected["status"] == "rejected" and "单文件 2MB" in rejected["error"]
    assert client.get("/knowledge-limits").json() == dict(file_mb=2, batch_mb=50, batch_files=16)
    # 上传上限调小不影响知识库
    monkeypatch.setattr(config, "MAX_KNOWLEDGE_FILE_MB", 50)
    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 1)
    assert upload(client, ("中等.md", 3 * MB), key="m").json()["files"][0]["status"] != "rejected"


def test_ordinary_material_upload_keeps_its_limit(client):
    response = client.post("/materials/upload", files={"file": ("过大.md", b"x" * (config.MAX_UPLOAD_MB * MB + 1))})
    assert response.status_code == 413 and f"{config.MAX_UPLOAD_MB}MB" in response.json()["detail"]


def test_enqueue_uses_the_knowledge_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MAX_KNOWLEDGE_FILE_MB", 1)
    engine = file_engine()
    imports = ImportService(engine, tmp_path, LocalIndex(tmp_path / "i.sqlite"), Encoder)
    kb = imports.knowledge.create("库")
    with pytest.raises(ValueError, match="单文件 1MB"):
        imports.enqueue(kb.id, "a.md", b"x" * (MB + 1), request_key="a")
    assert imports.enqueue(kb.id, "a.md", b"x" * MB, request_key="b")[1] is True


def test_isolated_parser_boundary_uses_the_knowledge_limit(monkeypatch):
    monkeypatch.setattr(config, "MAX_KNOWLEDGE_FILE_MB", 1)
    monkeypatch.setattr(config, "MAX_UPLOAD_MB", 100)
    with pytest.raises(ParseFailure, match="1MB") as info:
        load_isolated("a.txt", b"x" * (MB + 1))
    assert info.value.code == "invalid_size"
