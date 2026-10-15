import pytest

from briefly import config


@pytest.fixture(autouse=True)
def _isolated_feishu_user(tmp_path, monkeypatch):
    """测试不得读到本机真实的飞书用户令牌（data/feishu_user_token.json）：每个测试用自己的临时文件。"""
    monkeypatch.setattr(config, "FEISHU_USER_TOKEN_FILE", str(tmp_path / "feishu_user_token.json"))
