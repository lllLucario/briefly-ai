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


@pytest.mark.parametrize("field", ["API_KEY", "MODEL"])
def test_missing_model_config_keeps_non_model_features_available(monkeypatch, tmp_path, field):
    from briefly import config

    monkeypatch.setattr(config, "API_KEY", "fake-key")
    monkeypatch.setattr(config, "MODEL", "fake-model")
    monkeypatch.setattr(config, field, "")
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "chat.db"))
    engine = create_db_engine("sqlite://")
    try:
        with TestClient(create_app(MaterialStore(engine))) as client:
            assert client.get("/").status_code == 200
            assert client.get("/materials").status_code == 200
            assert client.post("/materials", json={"content": "无需模型即可保存"}).status_code == 201
            response = client.post("/chat", json={"text": "你好"})
            assert response.status_code == 503
            assert "BRIEFLY_" + field in response.json()["detail"]
            assert "重启" in response.json()["detail"]
            assert "fake-key" not in response.text
            assert client.get("/chat/sessions").json() == []
            assert client.app.state.agent is None
            assert not (tmp_path / "chat.db").exists()
    finally:
        engine.dispose()


def test_local_vendor_scripts_are_served(chat_client):
    client, _ = chat_client
    page = client.get("/").text
    assert "cdn.jsdelivr.net" not in page
    for name in ("marked.min.js", "purify.min.js"):
        path = f"/web/vendor/{name}"
        assert f'src="{path}"' in page
        response = client.get(path)
        assert response.status_code == 200
        assert "javascript" in response.headers["content-type"]
        assert len(response.content) > 10000


def test_unresolved_output_checks_reach_history_without_exposing_internal_details():
    engine, model, client = make_client(text_msg("【一句话总结】见第 2 块。"), text_msg("【一句话总结】仍见第 2 块。"))
    try:
        with client:
            result = client.post("/chat", json={"text":"总结", "skill":"summarize"}).json()
            answer = client.get(f"/chat/sessions/{result['thread_id']}").json()["messages"][-1]
            assert len(model.calls) == 2
            assert answer["warnings"] and "请核对原文" in answer["warnings"][0]
            assert "第 2 块" not in "".join(answer["warnings"])
            assert answer["result_kinds"] == ["summarize"]
            assert answer["skill"] == "summarize"
            assert client.get(f"/chat/sessions/{result['thread_id']}").json()["messages"][-1]["warnings"] == answer["warnings"]
    finally:
        engine.dispose()


def test_presentation_detects_results_not_requested_skill_and_keeps_recovery_information():
    from langchain_core.messages import AIMessage, HumanMessage
    from briefly.api import visible_messages
    from briefly.context_builder import system_reply

    messages = [
        HumanMessage("原请求", additional_kwargs={"skill":"todo", "material_ids":[3]}),
        HumanMessage("不显示的内部提示", additional_kwargs={"internal":True}),
        AIMessage("【会议信息】会议\n【行动项】整理记录\n【待办清单】整理记录", id="answer", response_metadata={"missing_conflicts":["私有原文片段"]}),
        HumanMessage("换个任务"),
        system_reply("本次预算不足"),
    ]
    result = visible_messages(messages)
    assert len(result) == 4
    assert result[1].result_kinds == ["minutes", "todo"]
    assert len(result[1].warnings) == 1 and "私有原文片段" not in result[1].warnings[0]
    assert result[-1].system_reply and not result[-1].result_kinds and result[-1].skill is None


@pytest.mark.parametrize(("text","kinds"), [
    ("【优化后】已润色\n【主要修改】措辞", ["polish"]),
    ("【周报标题】本周", ["weekly"]),
    ("## **【行动项】**\n- 整理记录", ["minutes"]),
    ("请提供可处理的内容", []),
])
def test_result_kinds_do_not_depend_on_stale_explicit_capability(text, kinds):
    from langchain_core.messages import AIMessage, HumanMessage
    from briefly.api import visible_messages
    assert visible_messages([HumanMessage("请求", additional_kwargs={"skill":"todo"}), AIMessage(text)])[-1].result_kinds == kinds
