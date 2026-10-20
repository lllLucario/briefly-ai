"""连接层限制必须在SDK创建前传入；不连接网络、不调用真实模型。"""
from briefly import config, llm


def test_eval_limits_reach_client_constructor(monkeypatch):
    captured = {}
    monkeypatch.setattr(config, "API_KEY", "fake-key")
    monkeypatch.setattr(config, "MODEL", "fake-model")
    def build(**kwargs):
        captured.update(kwargs)
        return object()
    monkeypatch.setattr(llm, "ChatOpenAI", build)
    llm.create_chat_model(max_retries=0, timeout=60)
    assert captured["max_retries"] == 0 and captured["timeout"] == 60


def test_normal_calls_preserve_provider_defaults(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "fake-key")
    monkeypatch.setattr(config, "MODEL", "fake-model")
    monkeypatch.setattr(llm, "ChatOpenAI", lambda **kwargs: kwargs)
    values = llm.create_chat_model()
    assert "max_retries" not in values and "timeout" not in values
