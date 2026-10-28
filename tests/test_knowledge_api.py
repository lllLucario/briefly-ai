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
