"""对话接口与网页入口。用假模型，不调用真实 API。"""
import pytest
from fastapi.testclient import TestClient
from langchain_core.tools import tool

from briefly.agent import Agent
from briefly.api import create_app
from briefly.db import create_db_engine
from briefly.materials import MaterialStore
from fakes import fake, text_msg, tool_msg


@tool
def get_current_date() -> str:
    """d"""
    return "2026-10-13 星期二"


def make_client(*responses, error: Exception | None = None):
    engine = create_db_engine("sqlite://")
    store = MaterialStore(engine)
    model = fake(*responses)
    if error is not None:
        model.messages = iter([])  # 没有预设回复，调用时报错

    def factory(s: MaterialStore) -> Agent:
        return Agent(model, tools=[get_current_date], store=s)

    return engine, model, TestClient(create_app(store, agent_factory=factory))


@pytest.fixture
def chat_client(request):
    responses = getattr(request, "param", (text_msg("第一条回复"), text_msg("第二条回复")))
    engine, model, client = make_client(*responses)
    with client:
        yield client, model
    engine.dispose()


def test_new_session_then_continue(chat_client):
    client, model = chat_client
    first = client.post("/chat", json={"text": "  帮我总结这段话：今天完成了登录联调  "})
    assert first.status_code == 200
    thread = first.json()["thread_id"]
    assert first.json()["reply"] == "第一条回复"

    second = client.post("/chat", json={"text": "再短一点", "thread_id": thread})
    assert second.json() == {"thread_id": thread, "reply": "第二条回复"}
    assert [m.content for m in model.calls[1][1:]][-1] == "再短一点"  # 同一会话带着历史

    sessions = client.get("/chat/sessions").json()
    assert len(sessions) == 1 and sessions[0]["title"] == "帮我总结这段话：今天完成了登录联调"

    detail = client.get(f"/chat/sessions/{thread}").json()
    assert [(m["role"], m["text"]) for m in detail["messages"]] == [
        ("user", "  帮我总结这段话：今天完成了登录联调  "),
        ("assistant", "第一条回复"),
        ("user", "再短一点"),
        ("assistant", "第二条回复"),
    ]


def test_history_shows_original_input_not_attached_material(chat_client):
    client, model = chat_client
    mid = client.post("/materials", json={"title": "进展", "content": "完成支付回调排查"}).json()["material"]["id"]
    resp = client.post("/chat", json={"text": "总结", "material_ids": [mid], "skill": "summarize"})
    assert "完成支付回调排查" in model.calls[0][-1].content  # 发给模型的消息附了材料全文
    msg = client.get(f"/chat/sessions/{resp.json()['thread_id']}").json()["messages"][0]
    assert (msg["text"], msg["material_ids"], msg["skill"]) == ("总结", [mid], "summarize")


@pytest.mark.parametrize("chat_client", [(tool_msg("get_current_date", {}), text_msg("今天周二"))], indirect=True)
def test_history_lists_tools_used(chat_client):
    client, _ = chat_client
    thread = client.post("/chat", json={"text": "今天周几"}).json()["thread_id"]
    messages = client.get(f"/chat/sessions/{thread}").json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["tools"] == ["get_current_date"] and messages[1]["text"] == "今天周二"


def test_context_usage_is_reported(chat_client):
    client, _ = chat_client
    thread = client.post("/chat", json={"text": "你好"}).json()["thread_id"]
    ctx = client.get(f"/chat/sessions/{thread}/context").json()
    assert ctx["usage"]["estimated_tokens"] > 0 and ctx["memory_version"] is None


def test_delete_session(chat_client):
    client, _ = chat_client
    thread = client.post("/chat", json={"text": "你好"}).json()["thread_id"]
    assert client.delete(f"/chat/sessions/{thread}").status_code == 204
    assert client.get("/chat/sessions").json() == []
    assert client.get(f"/chat/sessions/{thread}").status_code == 404
    assert client.delete(f"/chat/sessions/{thread}").status_code == 404


@pytest.mark.parametrize(
    ("body", "code", "message"),
    [
        ({"text": "  "}, 400, "不能为空"),
        ({"text": "x", "skill": "dance"}, 400, "未知能力"),
        ({"text": "x", "thread_id": "nope"}, 404, "会话 nope 不存在"),
        ({"text": "x", "material_ids": [999]}, 404, "材料 999 不存在"),
    ],
)
def test_chat_errors_are_readable(chat_client, body, code, message):
    client, _ = chat_client
    resp = client.post("/chat", json=body)
    assert resp.status_code == code and message in resp.json()["detail"]
    assert client.get("/chat/sessions").json() == []  # 出错不留下空会话


def test_model_error_becomes_502():
    engine, _, client = make_client(error=RuntimeError())
    with client:
        resp = client.post("/chat", json={"text": "你好"})
        assert resp.status_code == 502 and "模型调用失败" in resp.json()["detail"]
    engine.dispose()


def test_skills_and_web_page(chat_client):
    client, _ = chat_client
    skills = client.get("/skills").json()
    assert {s["name"] for s in skills} == {"summarize", "minutes", "weekly", "todo", "polish"}
    page = client.get("/")
    assert page.status_code == 200 and "Briefly" in page.text
    script = client.get("/web/app.js")
    assert script.status_code == 200
    assert page.headers["cache-control"] == script.headers["cache-control"] == "no-cache"  # 更新后刷新即生效
    assert "cache-control" not in client.get("/skills").headers
