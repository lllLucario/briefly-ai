import pytest
from fastapi.testclient import TestClient

from briefly.api import create_app
from briefly.db import create_db_engine
from briefly.materials import MaterialStore


@pytest.fixture
def client():
    engine = create_db_engine("sqlite://")
    with TestClient(create_app(MaterialStore(engine))) as c:
        yield c
    engine.dispose()


WEEKLY = "# 周报\n## 周一\n完成登录联调。\n## 周三\n定位支付回调超时。"


def test_paste_text_then_duplicate(client):
    first = client.post("/materials", json={"title": "周报", "content": WEEKLY})
    assert first.status_code == 201
    body = first.json()
    assert body["created"] is True
    m = body["material"]
    assert (m["title"], m["source"], m["file_type"]) == ("周报", "paste", None)
    assert m["section_count"] == 3 and m["chunk_count"] == 1 and m["structure"] == "markdown"

    again = client.post("/materials", json={"title": "别的标题", "content": WEEKLY})
    assert again.status_code == 200 and again.json()["created"] is False
    assert again.json()["material"]["id"] == m["id"]


def test_upload_file_with_chinese_name(client):
    resp = client.post("/materials/upload", files={"file": ("本周进展.md", "# 进展\n完成联调".encode())})
    assert resp.status_code == 201
    m = resp.json()["material"]
    assert m["title"] == "本周进展.md" and m["file_type"] == "md"


def test_upload_gbk_csv(client):
    resp = client.post("/materials/upload", files={"file": ("进度.csv", "任务,负责人\n压测,小王\n".encode("gbk"))})
    assert resp.status_code == 201
    detail = client.get(f"/materials/{resp.json()['material']['id']}").json()
    assert "| 压测 | 小王 |" in detail["content"]


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ({"file": ("a.pptx", b"x")}, "不支持的文件类型"),
        ({"file": ("坏.docx", b"not a zip")}, "文件无法解析"),
        ({"file": ("空.txt", b"   ")}, "未从文件中提取到文字"),
    ],
)
def test_upload_errors_are_readable(client, files, message):
    resp = client.post("/materials/upload", files=files)
    assert resp.status_code == 400 and message in resp.json()["detail"]


def test_empty_paste_is_rejected(client):
    resp = client.post("/materials", json={"content": "  "})
    assert resp.status_code == 400 and "为空" in resp.json()["detail"]


def test_missing_content_field_is_422(client):
    assert client.post("/materials", json={"title": "x"}).status_code == 422


def test_list_detail_and_delete(client):
    a = client.post("/materials", json={"title": "周报", "content": WEEKLY}).json()["material"]["id"]
    client.post("/materials", json={"title": "另一份", "content": "没有标题的一段话"})

    listed = client.get("/materials").json()
    assert [m["title"] for m in listed] == ["周报", "另一份"]
    assert "content" not in listed[0]
    assert listed[1]["structure"] == "none"

    detail = client.get(f"/materials/{a}").json()
    assert detail["content"] == WEEKLY
    assert [s["path"] for s in detail["sections"]] == ["周报", "周报 > 周一", "周报 > 周三"]
    assert detail["parse_report"]["structure"] == "markdown"

    assert client.delete(f"/materials/{a}").status_code == 204
    assert client.get(f"/materials/{a}").status_code == 404
    assert client.delete(f"/materials/{a}").status_code == 404


def test_not_found_message(client):
    resp = client.get("/materials/999")
    assert resp.status_code == 404 and resp.json()["detail"] == "材料 999 不存在"


def test_openapi_docs_are_available(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert {"/materials", "/materials/upload", "/materials/{material_id}"} <= set(paths)


def test_every_endpoint_is_in_one_documented_group():
    """接口文档（/docs）按功能分组：新接口必须标明分组，否则会落进没有说明的“default”。"""
    from briefly.api_docs import OPENAPI_TAGS

    engine = create_db_engine("sqlite://")
    spec = create_app(store=MaterialStore(engine), model_factory=lambda: None).openapi()
    names = {t["name"] for t in OPENAPI_TAGS}
    for path, operations in spec["paths"].items():
        for method, op in operations.items():
            assert len(op.get("tags", [])) == 1 and op["tags"][0] in names, f"{method.upper()} {path} 没有分组"
    assert [t["name"] for t in spec["tags"]] == [t["name"] for t in OPENAPI_TAGS]
    engine.dispose()
