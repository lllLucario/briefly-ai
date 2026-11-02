import pytest
from fastapi.testclient import TestClient

from briefly.api import create_app
from briefly.db import create_db_engine
from briefly.materials import MaterialStore


@pytest.fixture
def client():
    engine=create_db_engine("sqlite://")
    try:
        with TestClient(create_app(MaterialStore(engine))) as client:
            yield client
    finally:
        engine.dispose()


def material(client,title,content):
    return client.post("/materials",json=dict(title=title,content=content)).json()["material"]["id"]


def test_full_metadata_api_flow_and_scope_is_explicit(client):
    kb=client.post("/knowledge-bases",json={"name":"制度库"}).json()["id"]
    mat=material(client,"制度v1","上海住宿500元")
    response=client.post(f"/knowledge-bases/{kb}/documents",json={"material_id":mat})
    assert response.status_code==201
    data=response.json()
    doc,ver=data["document"]["id"],data["version"]["id"]
    assert data["version"]["business_version"] is None
    assert client.post(f"/knowledge-bases/{kb}/documents",json={"material_id":mat}).status_code==200
    assert client.post("/knowledge-scope",json={"kb_ids":[kb]}).json()["status"]=="not_ready"
    assert client.post("/knowledge-scope",json={"kb_ids":[]}).json()["material_ids"]==[]
    assert client.post("/knowledge-scope",json={"kb_ids":[kb,"missing"]}).status_code==404
    chunk=client.get(f"/knowledge-bases/{kb}/documents/{doc}/versions/{ver}/chunks/1").json()
    assert chunk["page"] is None and chunk["content"]=="上海住宿500元"
    assert chunk["status"]=="not_indexed"
    assert client.get(f"/knowledge-bases/{kb}/references/{chunk['reference']}").json()==chunk
    newer=material(client,"制度v2","上海住宿650元")
    response=client.post(f"/knowledge-bases/{kb}/documents/{doc}/versions",json={"material_id":newer,"expected_revision":1})
    assert response.status_code==201
    assert response.json()["document"]["revision"]==2
    assert client.post(f"/knowledge-bases/{kb}/documents/{doc}/versions",json={"material_id":mat,"expected_revision":1}).status_code==409
    versions=client.get(f"/knowledge-bases/{kb}/documents/{doc}/versions").json()
    assert len(versions["versions"])==2 and versions["active_generation_id"] is None
    assert client.delete(f"/knowledge-bases/{kb}/documents/{doc}?expected_revision=2").status_code==204
    assert client.get(f"/materials/{mat}").status_code==200
    assert client.get(f"/knowledge-bases/{kb}/references/{chunk['reference']}").json()["status"]=="document_removed"


def test_cross_library_is_404_and_no_ready_write_endpoint(client):
    a=client.post("/knowledge-bases",json={"name":"A"}).json()["id"]
    b=client.post("/knowledge-bases",json={"name":"B"}).json()["id"]
    mat=material(client,"制度","专属资料")
    result=client.post(f"/knowledge-bases/{a}/documents",json={"material_id":mat}).json()
    doc,ver=result["document"]["id"],result["version"]["id"]
    assert client.get(f"/knowledge-bases/{b}/documents/{doc}/versions").status_code==404
    assert client.get(f"/knowledge-bases/{b}/documents/{doc}/versions/{ver}/chunks/1").status_code==404
    assert client.post(f"/knowledge-bases/{a}/documents/{doc}/ready").status_code==404


@pytest.mark.parametrize("body",[{"name":""},{"name":"x"*201}])
def test_invalid_base_names(client,body):
    assert client.post("/knowledge-bases",json=body).status_code==422


def test_invalid_reference_and_whitespace_name(client):
    assert client.post("/knowledge-bases",json={"name":"   "}).status_code==400
    kb=client.post("/knowledge-bases",json={"name":"库"}).json()["id"]
    assert client.get(f"/knowledge-bases/{kb}/references/not-a-ref").status_code==400
    assert client.post(f"/knowledge-bases/{kb}/documents",json={"material_id":99999}).status_code==404


def test_chunk_page_bounds_scope_and_source_integrity(client):
    from sqlalchemy.orm import Session
    from briefly.materials import Material
    kb = client.post("/knowledge-bases", json={"name":"预览库"}).json()["id"]
    other = client.post("/knowledge-bases", json={"name":"其他库"}).json()["id"]
    mat = material(client, "多段原文", "第一章\n" + "这是用于检索的原创虚构材料。" * 500)
    attached = client.post(f"/knowledge-bases/{kb}/documents", json={"material_id":mat}).json()
    doc, version = attached["document"]["id"], attached["version"]["id"]
    path = f"/knowledge-bases/{kb}/documents/{doc}/versions/{version}/chunks"
    page = client.get(path, params={"limit":1}).json()
    assert page["total"] > 1 and len(page["items"]) == 1
    first = page["items"][0]
    assert first == client.get(path + "/1").json()
    assert first["content"] and first["status"] == "not_indexed"
    assert client.get(path, params={"offset":page["total"]}).json()["items"] == []
    assert client.get(path, params={"offset":1,"limit":1}).json()["items"][0]["chunk"] != first["chunk"]
    assert client.get(path, params={"limit":51}).status_code == 422
    assert client.get(path, params={"offset":-1}).status_code == 422
    assert client.get(path.replace(kb,other,1)).status_code == 404
    assert client.get(path.replace(version,"missing",1)).status_code == 404
    second = material(client, "其他文档", "另一份独立原文")
    other_version = client.post(f"/knowledge-bases/{kb}/documents", json={"material_id":second}).json()["version"]["id"]
    assert client.get(path.replace(version,other_version,1)).status_code == 404
    with Session(client.app.state.store.engine) as session:
        session.get(Material, mat).content = "被修改的正文"
        session.get(Material, mat).content_hash = "0" * 64
        session.commit()
    changed = client.get(path, params={"limit":1}).json()["items"][0]
    assert changed["content"] is None and changed["status"] == "source_changed"


def test_folder_paths_are_persistent_distinct_and_validated_before_enqueue(client, monkeypatch):
    import json
    from briefly.knowledge.ingestion import ImportService
    monkeypatch.setattr(ImportService, "parse", lambda *args: None)
    kb = client.post("/knowledge-bases", json={"name":"目录库"}).json()["id"]
    path = f"/knowledge-bases/{kb}/imports"
    files = [("files",("制度.txt",b"one","text/plain")),("files",("制度.txt",b"two","text/plain"))]
    paths = ["人事/制度.txt", "财务/制度.txt"]
    response = client.post(path, files=files, data={"request_key":"folders","relative_paths":json.dumps(paths)})
    assert response.status_code == 202
    jobs = response.json()["files"]
    assert [job["relative_path"] for job in jobs] == paths
    assert len({job["source_key"] for job in jobs}) == 2
    assert {job["relative_path"] for job in client.get(path).json()} == set(paths)
    same = client.post(path, files=files, data={"request_key":"folders","relative_paths":json.dumps(paths)})
    assert [job["id"] for job in same.json()["files"]] == [job["id"] for job in jobs]
    for invalid in (["../制度.txt","财务/制度.txt"], ["/制度.txt","财务/制度.txt"], ["C:/制度.txt","财务/制度.txt"], ["人事/别名.txt","财务/制度.txt"], ["制度.txt"], [None, "制度.txt"], "not a list"):
        assert client.post(path, files=files, data={"request_key":"invalid","relative_paths":json.dumps(invalid)}).status_code == 400
    assert len(client.get(path).json()) == 2
