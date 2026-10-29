import pytest

from briefly import config


def pytest_addoption(parser):
    parser.addoption("--run-browser", action="store_true", help="运行需要Playwright和浏览器的假模型网页回归")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-browser"):
        for item in items:
            if "browser" in item.keywords:
                item.add_marker(pytest.mark.skip(reason="网页回归需显式使用 --run-browser"))


@pytest.fixture(autouse=True)
def _isolated_feishu_user(tmp_path, monkeypatch):
    """测试不得读到本机真实的飞书用户令牌（data/feishu_user_token.json）：每个测试用自己的临时文件。"""
    monkeypatch.setattr(config, "FEISHU_USER_TOKEN_FILE", str(tmp_path / "feishu_user_token.json"))


@pytest.fixture(autouse=True)
def _no_local_embedding(monkeypatch):
    """单元测试不加载本机 .env 配置的真实 Embedding 模型：意图漏斗只用规则层，结果不随本机环境变化。"""
    monkeypatch.setattr(config, "EMBEDDING_DIR", "")
