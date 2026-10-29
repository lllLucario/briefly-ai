import pytest
pytest.importorskip("sqlite_vec", reason="知识索引接口回归需安装.[rag]")
from fastapi.testclient import TestClient

from briefly.api import create_app
from briefly.db import create_db_engine
from briefly.loaders import LoadedDocument
from briefly.materials import MaterialStore
from test_knowledge_slicing import Encoder


@pytest.fixture
def client(tmp_path, monkeypatch):
    from briefly import config
    monkeypatch.setattr(config, "KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
    engine = create_db_engine("sqlite:///" + (tmp_path / "api.db").as_posix())
    app = create_app(MaterialStore(engine))
    app.state.knowledge_imports.encoder_factory = Encoder
    def parser(filename, data):
        if filename.endswith(".docx"):
            raise ValueError("文档损坏")
        return LoadedDocument(data.decode(), True)
    app.state.knowledge_imports.parser = parser
    try:
        with TestClient(app) as client:
            yield client
    finally:
        engine.dispose()


def test_batch_partial_failure_preview_confirm_and_replay(client):
    kb = client.post("/knowledge-bases", json={"name": "资料库"}).json()["id"]
    base = f"/knowledge-bases/{kb}/imports"
    files = [("files", ("one.md", "# 制度\n审批须在预订前完成。".encode())), ("files", ("broken.docx", b"bad"))]
    response = client.post(base, data={"request_key": "request", "tokens": "64"}, files=files)
    assert response.status_code == 202
    records = client.get(base).json()
    valid = next(x for x in records if x["filename"] == "one.md")
    bad = next(x for x in records if x["filename"] == "broken.docx")
    assert valid["status"] == "preview" and bad["status"] == "failed"
    assert "审批须" in client.get(f"{base}/{valid['id']}/chunks").json()[0]["content"]
    replay = client.post(base, data={"request_key": "request", "tokens": "64"}, files=files).json()
    assert {x["id"] for x in replay["files"]} == {x["id"] for x in records}
    assert client.post(f"{base}/{valid['id']}/confirm", json={"digest": "a" * 64}).status_code == 409
    assert client.post(f"{base}/{valid['id']}/confirm", json={"digest": valid["preview_digest"]}).status_code == 202
    rows = client.get(base).json()
    assert next(x for x in rows if x["id"] == valid["id"])["status"] == "ready"
    assert client.post("/knowledge-scope", json={"kb_ids": [kb]}).json()["status"] == "ready"
    search = client.post(f"/knowledge-bases/{kb}/search", json={"question": "审批"})
    assert search.status_code == 200 and search.json()["evidence"][0]["reference"].startswith("kbref:")
    answer = client.post(f"/knowledge-bases/{kb}/answer", json={"question": "审批"})
    assert answer.status_code == 200 and answer.json()["status"] == "external_authorization_required"
    assert answer.json()["model_called"] is False
    other = client.post("/knowledge-bases", json={"name": "隔离库"}).json()["id"]
    assert client.get(f"/knowledge-bases/{other}/imports/{valid['id']}/chunks").status_code == 404
    assert client.post(f"{base}/{valid['id']}/retry", json={}).status_code == 409


def test_batch_bounds_and_duplicate_conflicts(client):
    kb = client.post("/knowledge-bases", json={"name": "库"}).json()["id"]
    base = f"/knowledge-bases/{kb}/imports"
    assert client.post(base, data={"request_key": "one"}, files=[("files", ("x.txt", b"x"))] * 17).status_code == 400
    assert client.post(base, data={"request_key": "one"}, files=[("files", ("x.txt", b"x"))]).status_code == 202
    assert client.post(base, data={"request_key": "one"}, files=[("files", ("x.txt", b"changed"))]).status_code == 409
    assert client.post(base, data={"request_key": "two", "tokens": "8"}, files=[("files", ("x.txt", b"x"))]).status_code == 422
