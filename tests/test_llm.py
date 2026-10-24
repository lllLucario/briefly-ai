"""连接层限制必须在SDK创建前传入；不连接网络、不调用真实模型。"""
import pytest

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


@pytest.mark.parametrize("key,model,missing", [
    ("", "fake-model", ["BRIEFLY_API_KEY"]),
    ("fake-key", "", ["BRIEFLY_MODEL"]),
    (" \t", " \n", ["BRIEFLY_API_KEY", "BRIEFLY_MODEL"]),
])
def test_missing_configuration_fails_before_client_creation(monkeypatch, key, model, missing):
    monkeypatch.setattr(config, "API_KEY", key)
    monkeypatch.setattr(config, "MODEL", model)

    def unexpected_client(**kwargs):
        pytest.fail("缺少配置时不应创建模型客户端")

    monkeypatch.setattr(llm, "ChatOpenAI", unexpected_client)
    with pytest.raises(llm.ModelConfigurationError) as error:
        llm.create_chat_model()
    message = str(error.value)
    assert all(name in message for name in missing)
    assert "重启" in message
    assert "fake-key" not in message


def test_cli_missing_configuration_exits_without_traceback_or_database(monkeypatch, tmp_path):
    from briefly import cli

    monkeypatch.setattr(config, "API_KEY", "")
    monkeypatch.setattr(config, "MODEL", "fake-model")
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "chat.db"))
    monkeypatch.setattr(cli.sys, "argv", ["briefly"])
    with pytest.raises(SystemExit, match="BRIEFLY_API_KEY"):
        cli.main()
    assert not (tmp_path / "chat.db").exists()
