import os
import sys
from pathlib import Path

import pytest

# .venv 里的 briefly 是指向主检出 src 的可编辑安装；在 .claude/worktrees 里跑测试时，先用本检出的 src，
# 否则测的是主检出的代码，改动不生效、测试照样全过
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sqlalchemy import StaticPool  # noqa: E402

from briefly import config  # noqa: E402
import pg_mode  # noqa: E402

pg_mode.install()  # 设了 BRIEFLY_TEST_DATABASE_URL 时把数据库入口换成 PostgreSQL，必须早于下面和用例模块的导入

import db_util  # noqa: E402


def pytest_addoption(parser):
    parser.addoption("--run-browser", action="store_true", help="运行需要Playwright和浏览器的假模型网页回归")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-browser"):
        for item in items:
            if "browser" in item.keywords:
                item.add_marker(pytest.mark.skip(reason="网页回归需显式使用 --run-browser"))
    if pg_mode.enabled():
        from pg_skips import SKIPS

        for item in items:
            for prefix, reason in SKIPS.items():
                if item.nodeid.startswith(prefix):
                    item.add_marker(pytest.mark.skip(reason=f"只适用于 SQLite：{reason}"))
                    break
    else:
        for item in items:
            if "postgres" in item.keywords:
                item.add_marker(pytest.mark.skip(reason="需要设置 BRIEFLY_TEST_DATABASE_URL"))


@pytest.fixture(autouse=True)
def _file_engines(tmp_path, tmp_path_factory):
    db_util.start_test(tmp_path, tmp_path_factory)
    pg_mode.start_test()
    yield
    db_util.finish_test()
    pg_mode.finish_test()


@pytest.fixture(autouse=True)
def _no_background_threads_on_shared_connection(monkeypatch):
    """内存库共用一条连接，后台线程会撤销测试线程未提交的写入（说明见 db_util）：直接报错，不留偶发失败。"""
    from briefly.workspace_index import WorkspaceIndex

    real = WorkspaceIndex.__init__

    def init(self, engine, *args, **kwargs):
        if isinstance(engine.pool, StaticPool):
            pytest.fail('create_app 会启动后台线程，不能配内存库：把 create_db_engine("sqlite://") 换成 db_util.file_engine()')
        real(self, engine, *args, **kwargs)

    monkeypatch.setattr(WorkspaceIndex, "__init__", init)


@pytest.fixture(autouse=True)
def _isolated_feishu_user(tmp_path, monkeypatch):
    """测试不得读到本机真实的飞书用户令牌（data/feishu_user_token.json）：每个测试用自己的临时文件。"""
    monkeypatch.setattr(config, "FEISHU_USER_TOKEN_FILE", str(tmp_path / "feishu_user_token.json"))


@pytest.fixture(autouse=True)
def _isolated_session_db(tmp_path, monkeypatch):
    """测试不得打开本机真实的会话记录库（data/briefly.db）：用默认智能体的用例（例如 /chat 校验失败前已经建好智能体）
    会用 sqlite_checkpointer 打开 DB_PATH。每个测试用自己的临时文件。"""
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "briefly.db"))


@pytest.fixture(autouse=True)
def _no_local_embedding(monkeypatch):
    """单元测试不加载本机 .env 配置的真实 Embedding 模型：意图漏斗只用规则层，结果不随本机环境变化。"""
    monkeypatch.setattr(config, "EMBEDDING_DIR", "")


@pytest.fixture(autouse=True)
def _isolated_workspace_index(tmp_path, monkeypatch):
    """工作区语义索引文件放在每个测试自己的临时目录，不写进仓库的 data/。"""
    monkeypatch.setattr(config, "WORKSPACE_INDEX_PATH", str(tmp_path / "workspace.sqlite"))


@pytest.fixture(autouse=True)
def _no_office_profile_template(monkeypatch):
    """转换用替身时不建 LibreOffice 配置模板（模板初始化会多一次 soffice 调用）；模板逻辑在专门的测试里开。"""
    monkeypatch.setattr(config, "OFFICE_PROFILE_TEMPLATE", False)


@pytest.fixture(autouse=True)
def _auth_defaults(monkeypatch):
    """开发者本机 .env 里的令牌不影响测试；TestClient 默认的 Host 是 testserver，只在测试里允许，不放宽生产默认值。"""
    monkeypatch.setattr(config, "ACCESS_TOKEN", "")
    monkeypatch.setattr(config, "ALLOWED_HOSTS", [*config.DEFAULT_ALLOWED_HOSTS.split(","), "testserver"])


@pytest.fixture(autouse=True)
def _restore_logging():
    """configure_logging 改的是全局状态（命令行入口的测试也会调用）：每个测试后恢复，不影响后面的 caplog。"""
    import logging

    root, ours = logging.getLogger(), logging.getLogger("briefly")
    before = list(root.handlers), root.level, ours.level
    yield
    for handler in [h for h in root.handlers if h not in before[0]]:
        root.removeHandler(handler)
        handler.close()
    root.setLevel(before[1])
    ours.setLevel(before[2])


@pytest.fixture(autouse=True)
def _no_real_ocr(monkeypatch):
    """单元测试不跑真实图片文字识别（慢，且结果随模型变化）：默认关闭；OCR 用例自己打开并注入替身引擎。"""
    from briefly import ocr

    monkeypatch.setattr(config, "OCR", "off")
    monkeypatch.setattr(ocr, "_engine", None)
    monkeypatch.setattr(ocr, "DEADLINE", None)


# ---- 联网禁令：自动测试不得连接本机以外的地址（真实模型、飞书、SMTP 都算），本机和 CI 都生效 ----
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "", None}
_external_network_attempts = []


def _deny_external_network(event, args):
    """审计钩子能看到本进程所有 socket 连接和域名解析；子进程（Playwright 的浏览器等）不在此列。
    抛 AssertionError 而不是 OSError，避免被业务代码的网络错误处理吃掉；另记一笔，就算被更宽的 except 吃掉也会在收尾时报错。"""
    if event == "socket.connect":
        host = args[1][0] if isinstance(args[1], tuple) else None  # 非元组是 Unix 套接字路径
    elif event == "socket.getaddrinfo":
        host = args[0]
    else:
        return
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if host not in _LOOPBACK_HOSTS:
        _external_network_attempts.append(f"{event}: {host}（{os.getenv('PYTEST_CURRENT_TEST')}）")
        raise AssertionError(f"测试不得连接外部地址：{host}")


sys.addaudithook(_deny_external_network)


@pytest.fixture
def external_network_attempts():
    return _external_network_attempts


@pytest.fixture(scope="session", autouse=True)
def _no_external_network_attempted():
    yield
    assert not _external_network_attempts, f"测试尝试连接外部地址：{_external_network_attempts}"


@pytest.fixture(autouse=True)
def _no_real_feishu_credentials(monkeypatch):
    """本机 .env 里的飞书凭证不进测试：否则定时提醒的用例会真的连飞书（CI 没有凭证，以前只在本机发生）。"""
    for name in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_NOTIFY_USER", "FEISHU_FOLDER_TOKEN"):
        monkeypatch.setattr(config, name, "")


@pytest.fixture(autouse=True)
def _no_real_mail_settings(monkeypatch):
    """本机 .env 里的邮箱配置不进测试：否则“是否已配置邮箱”、默认收件人、发件人随机器不同（问题登记表第 90 条）。

    要测已配置邮箱的情况，用例自己设置这些值，并注入 SMTP 替身。
    """
    for name in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "MAIL_FROM_NAME", "MAIL_DEFAULT_TO"):
        monkeypatch.setattr(config, name, "")
    monkeypatch.setattr(config, "SMTP_PORT", 465)


@pytest.fixture(autouse=True)
def _llm_cache_off(monkeypatch):
    """模型响应缓存默认关闭：很多用例用假模型重复提问，并按顺序断言回复。缓存行为由 test_llm_cache.py 打开后专门测。"""
    monkeypatch.setattr(config, "LLM_CACHE", False)
