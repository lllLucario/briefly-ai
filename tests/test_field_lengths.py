"""有长度上限的字段：用户输入超长时明确报错（422），不静默截断；刚好到上限的照常保存，不丢字。

问题登记表第 89 条：这些列是 String(n)。SQLite 不检查长度，PostgreSQL 超长直接报错变 500；
之前周报标题、署名等在服务层悄悄截断，用户不知道内容少了。CI 在两种后端上都跑这些用例。
"""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from briefly.api import create_app
from briefly.materials import MaterialStore
from db_util import file_engine


@pytest.fixture
def client():
    engine = file_engine()
    with TestClient(create_app(MaterialStore(engine))) as c:
        yield c
    engine.dispose()


def _week(client) -> int:
    return client.post("/weekly/week", json={"day": date(2026, 11, 9).isoformat()}).json()["id"]


def _rejected(response, field: str, limit: int):
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "validation_error"
    assert any(e["field"].endswith(field) and e["message"] == f"不能超过 {limit} 字" for e in body["errors"]), body


def test_weekly_item_title_over_limit_is_rejected_not_truncated(client):
    report = _week(client)
    _rejected(client.post(f"/weekly/{report}/items", json={"title": "长" * 201, "description": "说明"}), "title", 200)

    added = client.post(f"/weekly/{report}/items", json={"title": "长" * 200, "description": "说明"})
    assert added.status_code == 201, added.text
    [item] = added.json()["checklist_items"]
    assert item["title"] == "长" * 200

    _rejected(client.put(f"/weekly/{report}/items/{item['id']}", json={"title": "改" * 201}), "title", 200)
    _rejected(client.post(f"/weekly/{report}/items/{item['id']}/split",
                          json={"parts": [{"title": "一"}, {"title": "二" * 201}]}), "parts.1.title", 200)
    _rejected(client.post(f"/weekly/{report}/merge", json={"ids": [item["id"]], "title": "合" * 201}), "title", 200)


def test_weekly_settings_over_limit_are_rejected_and_full_length_is_kept(client):
    _rejected(client.put("/weekly-settings", json={"author": "名" * 101}), "author", 100)
    _rejected(client.put("/weekly-settings", json={"audience": "读" * 101}), "audience", 100)

    saved = client.put("/weekly-settings", json={"author": "名" * 100, "audience": "读" * 100})
    assert saved.status_code == 200, saved.text
    assert (len(saved.json()["author"]), len(saved.json()["audience"])) == (100, 100)



@pytest.mark.parametrize("field", ["name", "audience"])
def test_generation_signature_over_limit_is_rejected(client, field):
    # 生成时临时填的署名会进飞书文档标题（上限 200）和邮件主题（上限 300），超长在接口挡住
    report = _week(client)
    _rejected(client.post(f"/weekly/{report}/generate", json={field: "名" * 101}), field, 100)

def test_pasted_material_title_over_limit_is_rejected(client):
    _rejected(client.post("/materials", json={"title": "题" * 201, "content": "正文"}), "title", 200)
    created = client.post("/materials", json={"title": "题" * 200, "content": "正文"})
    assert created.status_code == 201 and len(created.json()["material"]["title"]) == 200


@pytest.mark.parametrize(("field", "limit"), [("thread_id", 64), ("corrects", 64), ("skill", 32), ("kb_id", 32)])
def test_chat_identifiers_over_column_limit_are_rejected_before_the_model(client, field, limit):
    # 这些编号由客户端给出，存进有长度上限的列；在接口挡住，不进模型，也不会在 PostgreSQL 上变成 500
    try:
        _rejected(client.post("/chat", json={"text": "你好", field: "x" * (limit + 1)}), field, limit)
    finally:
        # 校验失败前 FastAPI 已经解析了依赖、建好智能体；它的会话记录连接要自己关，否则留到后面的用例里才被回收
        agent = client.app.state.agent
        if agent is not None:
            agent.graph.checkpointer.conn.close()


def test_local_mail_settings_do_not_leak_into_tests(client):
    # 问题登记表第 90 条：本机 .env 配了邮箱也不影响测试；要“已配置”的用例自己设置并注入替身
    from briefly import config, mail

    assert not mail.configured()
    assert (config.SMTP_HOST, config.SMTP_USER, config.MAIL_DEFAULT_TO, config.MAIL_FROM_NAME) == ("", "", "", "")
    assert client.get("/mail/status").json() == {"configured": False, "sender": "", "default_to": ""}
