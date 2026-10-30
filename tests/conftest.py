import sys
from pathlib import Path

import pytest

# .venv 里的 briefly 是指向主检出 src 的可编辑安装；在 .claude/worktrees 里跑测试时，先用本检出的 src，
# 否则测的是主检出的代码，改动不生效、测试照样全过
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from briefly import config  # noqa: E402


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
