"""飞书用户授权：授权地址与 PKCE、换取与刷新令牌、先用户后应用、云空间浏览与搜索、接口。全部用模拟响应。"""
import json
import time
from datetime import date
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from briefly import config
from briefly.db import create_db_engine
from briefly.feishu.client import FallbackClient, with_user
from briefly.feishu.drive import list_folder, search
from briefly.feishu.oauth import FeishuNotAuthorized, UserTokens, _challenge, authorize_url, take_verifier
from fakes import FakeFeishu, feishu_error

API = "/open-apis"
DOC = "doxcnAbCdEfGhIjKlMnOpQrStUv"


@pytest.fixture(autouse=True)
def app_config(monkeypatch):
    monkeypatch.setattr(config, "FEISHU_APP_ID", "cli_test")
    monkeypatch.setattr(config, "FEISHU_APP_SECRET", "secret")


def token_reply(n: int, refresh: bool = True) -> httpx.Response:
    body = {"code": 0, "access_token": f"u-{n}", "expires_in": 7200, "scope": "offline_access drive:drive:readonly", "token_type": "Bearer"}
    if refresh:
        body |= {"refresh_token": f"r-{n}", "refresh_token_expires_in": 604800}
    return httpx.Response(200, json=body)


def form(request) -> dict:
    return json.loads(request.content)


def connected_tokens(fake: FakeFeishu, path, expires_in: float = 7200) -> UserTokens:
    tokens = UserTokens(path, transport=fake.transport)
    now = time.time()
    tokens._save({"name": "王五", "open_id": "ou_me", "access_token": "u-0", "expires_at": now + expires_in,
                  "refresh_token": "r-0", "refresh_expires_at": now + 86400, "scope": "", "updated_at": now})  # fmt: skip
    return tokens


# ---- 授权地址
def test_authorize_url_uses_pkce_and_state():
    url = urlparse(authorize_url())
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
    assert q["client_id"] == "cli_test" and q["code_challenge_method"] == "S256"
    assert "offline_access" in q["scope"].split() and q["redirect_uri"].endswith("/feishu/oauth/callback")
    verifier = take_verifier(q["state"])
    assert _challenge(verifier) == q["code_challenge"]
    with pytest.raises(FeishuNotAuthorized, match="重新点击"):
        take_verifier(q["state"])  # 只能用一次


def test_unknown_state_is_rejected():
    with pytest.raises(FeishuNotAuthorized):
        take_verifier("forged")


# ---- 换取与刷新
def test_exchange_saves_tokens(tmp_path):
    fake = FakeFeishu({("POST", "/open-apis/authen/v2/oauth/token"): token_reply(1)})
    state = parse_qs(urlparse(authorize_url()).query)["state"][0]
    tokens = UserTokens(tmp_path / "t.json", transport=fake.transport)
    tokens.exchange("code-1", state, lambda token: {"name": "王五", "open_id": "ou_me"} if token == "u-1" else {})
    sent = form(fake.requests[0])
    assert sent["grant_type"] == "authorization_code" and sent["code"] == "code-1"
    assert sent["client_secret"] == "secret" and len(sent["code_verifier"]) >= 43
    assert tokens.connected()["name"] == "王五"
    assert tokens.access_token() == "u-1"  # 没过期：不刷新
    assert len(fake.requests) == 1


def test_expiring_token_is_refreshed_and_rotated(tmp_path):
    fake = FakeFeishu({("POST", "/open-apis/authen/v2/oauth/token"): token_reply(2)})
    tokens = connected_tokens(fake, tmp_path / "t.json", expires_in=60)  # 5 分钟内过期
    assert tokens.access_token() == "u-2"
    assert form(fake.requests[0]) | {} == {"grant_type": "refresh_token", "refresh_token": "r-0", "client_id": "cli_test", "client_secret": "secret"}
    assert tokens.load()["refresh_token"] == "r-2"  # 新的 refresh_token 替换旧的
    assert tokens.connected()["name"] == "王五"


def test_stale_token_refresh_happens_once(tmp_path):
    fake = FakeFeishu({("POST", "/open-apis/authen/v2/oauth/token"): token_reply(3)})
    tokens = connected_tokens(fake, tmp_path / "t.json")
    assert tokens.access_token(refresh=True, stale="u-0") == "u-3"
    assert tokens.access_token(refresh=True, stale="u-0") == "u-3"  # 另一个线程拿着旧令牌：不再刷新
    assert len(fake.requests) == 1


def test_revoked_refresh_token_clears_authorization(tmp_path):
    revoked = httpx.Response(400, json={"code": 20064, "error": "invalid_grant", "error_description": "refresh token revoked"})
    fake = FakeFeishu({("POST", "/open-apis/authen/v2/oauth/token"): revoked})
    tokens = connected_tokens(fake, tmp_path / "t.json", expires_in=0)
    with pytest.raises(FeishuNotAuthorized, match="重新连接"):
        tokens.access_token()
    assert tokens.connected() is None and not (tmp_path / "t.json").exists()


def test_not_connected(tmp_path):
    with pytest.raises(FeishuNotAuthorized, match="连接飞书"):
        UserTokens(tmp_path / "none.json").access_token()


# ---- 先用户、后应用
def test_user_identity_first_then_app(tmp_path):
    def doc(request):
        if request.headers["Authorization"] == "Bearer u-0":
            return feishu_error(1770032, "forBidden", 403)  # 用户看不到
        return {"document": {"title": "应用能看到"}}

    fake = FakeFeishu({("GET", f"{API}/docx/v1/documents/{DOC}"): doc, ("GET", f"{API}/x"): {"ok": 1}})
    tokens = connected_tokens(fake, tmp_path / "t.json")
    with fake.client() as app_client:
        client = with_user(app_client, tokens)
        assert isinstance(client, FallbackClient)
        assert client.get(f"{API}/x") == {"ok": 1}
        assert fake.requests[-1].headers["Authorization"] == "Bearer u-0"  # 用户身份
        assert client.get(f"{API}/docx/v1/documents/{DOC}")["document"]["title"] == "应用能看到"
        assert fake.requests[-1].headers["Authorization"].startswith("Bearer t-")  # 退回应用身份


def test_without_connection_only_app_identity(tmp_path):
    fake = FakeFeishu()
    with fake.client() as app_client:
        assert with_user(app_client, UserTokens(tmp_path / "none.json")) is app_client


def test_other_errors_do_not_fall_back(tmp_path):
    fake = FakeFeishu({("GET", f"{API}/x"): feishu_error(1770002, "not found")})
    tokens = connected_tokens(fake, tmp_path / "t.json")
    with fake.client() as app_client, pytest.raises(Exception, match="文档不存在"):
        with_user(app_client, tokens).get(f"{API}/x")
    assert len(fake.requests) == 1


# ---- 云空间
def test_list_root_folder():
    files = {"files": [
        {"token": DOC, "name": "周报草稿", "type": "docx", "url": f"https://x.feishu.cn/docx/{DOC}", "modified_time": "1791031017"},
        {"token": "fldA", "name": "项目", "type": "folder"},
        {"token": "s1", "name": "快捷方式", "type": "shortcut", "shortcut_info": {"target_type": "bitable", "target_token": "bascnAbCdEfGhIjKl"}},
        {"token": "sht", "name": "表格", "type": "sheet"},
    ]}  # fmt: skip
    fake = FakeFeishu({("GET", f"{API}/drive/v1/files"): files})
    with fake.client() as client:
        items = list_folder(client)
    assert [x.kind for x in items] == ["folder", "docx", "bitable", "sheet"]
    assert [x.importable for x in items] == [False, True, True, False]
    assert items[2].token == "bascnAbCdEfGhIjKl" and "/bitable/bascnAbCdEfGhIjKl" in items[2].url
    assert items[1].modified == 1791031017
    assert "folder_token" not in fake.requests[0].url.params  # 根目录


def test_search_docs():
    data = {"res_units": [
        {"title_highlighted": "<em>周报</em>草稿", "entity_type": "DOC", "result_meta": {"doc_types": "DOCX", "url": f"https://x.feishu.cn/docx/{DOC}", "owner_name": "王五", "update_time": 1791031017}},
        {"title_highlighted": "客户跟进", "entity_type": "WIKI", "result_meta": {"doc_types": "BITABLE", "url": "https://x.feishu.cn/wiki/wikcnAbCdEfGhIjKl"}},
    ], "has_more": False}  # fmt: skip
    fake = FakeFeishu({("POST", f"{API}/search/v2/doc_wiki/search"): data})
    with fake.client() as client:
        items = search(client, "周报")
    assert [(x.name, x.kind, x.importable) for x in items] == [("周报草稿", "docx", True), ("客户跟进", "wiki", True)]
    assert FakeFeishu.body(fake.requests[0])["query"] == "周报"


# ---- 接口
@pytest.fixture
def api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from briefly.api import create_app
    from briefly.materials import MaterialStore

    monkeypatch.setattr(config, "FEISHU_NOTIFY_USER", "")
    fake = FakeFeishu({
        ("POST", "/open-apis/authen/v2/oauth/token"): token_reply(1),
        ("GET", f"{API}/authen/v1/user_info"): {"name": "王五", "open_id": "ou_me"},
        ("GET", f"{API}/drive/v1/files"): {"files": [{"token": DOC, "name": "周报草稿", "type": "docx"}]},
    })  # fmt: skip
    engine = create_db_engine("sqlite://")
    tokens = UserTokens(tmp_path / "t.json", transport=fake.transport)
    app = create_app(store=MaterialStore(engine), model_factory=lambda: None, feishu_factory=fake.client, feishu_tokens=tokens)
    with TestClient(app, follow_redirects=False) as client:
        yield client, fake, tokens, app
    engine.dispose()


def test_api_connect_flow(api):
    client, fake, tokens, _ = api
    assert client.get("/feishu/status").json()["user"] is None
    assert client.get("/feishu/drive/files").status_code == 401  # 还没连接
    login = client.get("/feishu/oauth/login")
    assert login.status_code == 307 and login.headers["location"].startswith("https://accounts.feishu.cn/")
    state = parse_qs(urlparse(login.headers["location"]).query)["state"][0]
    back = client.get(f"/feishu/oauth/callback?code=abc&state={state}")
    assert back.status_code == 307 and "feishu=connected" in back.headers["location"]
    assert client.get("/feishu/status").json()["user"]["name"] == "王五"
    user_info = fake.calls("GET", f"{API}/authen/v1/user_info")[0]
    assert user_info.headers["Authorization"] == "Bearer u-1"
    files = client.get("/feishu/drive/files").json()
    assert files[0]["name"] == "周报草稿" and files[0]["importable"]
    assert fake.calls("GET", f"{API}/drive/v1/files")[0].headers["Authorization"] == "Bearer u-1"  # 用户身份
    assert client.post("/feishu/oauth/logout").json() == {"ok": True}
    assert client.get("/feishu/status").json()["user"] is None


def test_api_callback_errors(api):
    client, _, _, _ = api
    denied = client.get("/feishu/oauth/callback?error=access_denied")
    assert "feishu_error=access_denied" in denied.headers["location"]
    forged = client.get("/feishu/oauth/callback?code=abc&state=forged")
    assert "feishu_error=" in forged.headers["location"]


def test_export_as_owner_skips_sharing(api, monkeypatch):
    from briefly.weekly import DONE

    client, fake, tokens, app = api
    connected_tokens(fake, tokens.path)
    monkeypatch.setattr(config, "FEISHU_NOTIFY_USER", "ou_me")
    converted = {"first_level_block_ids": ["a"], "blocks": [{"block_id": "a", "block_type": 2, "text": {"elements": [{"text_run": {"content": "x"}}]}}]}
    fake.routes |= {
        ("POST", f"{API}/docx/v1/documents"): {"document": {"document_id": DOC}},
        ("POST", f"{API}/docx/v1/documents/blocks/convert"): converted,
        ("POST", f"{API}/docx/v1/documents/{DOC}/blocks/{DOC}/descendant"): {},
        ("POST", f"{API}/drive/v1/metas/batch_query"): {"metas": [{"doc_token": DOC, "owner_id": "ou_me", "url": f"https://x.feishu.cn/docx/{DOC}"}]},
    }  # fmt: skip
    r = app.state.weekly.create([], "联调", date(2026, 10, 12), date(2026, 10, 18), "", "")
    app.state.weekly._update(r.id, status=DONE, content="【本周完成】\n- 完成")
    out = client.post(f"/weekly-reports/{r.id}/feishu").json()
    assert out["shared_with"] == "你（文档所有者）" and not out["share_error"]
    assert fake.calls("POST", f"{API}/docx/v1/documents")[0].headers["Authorization"] == "Bearer u-0"  # 以用户身份新建
    assert not fake.calls("POST", f"{API}/drive/v1/permissions/{DOC}/members")


def test_missing_scopes_after_config_change(tmp_path, monkeypatch):
    fake = FakeFeishu()
    tokens = connected_tokens(fake, tmp_path / "t.json")
    data = tokens.load(); data["scope"] = "offline_access drive:drive:readonly"; tokens._save(data)
    monkeypatch.setattr(config, "FEISHU_OAUTH_SCOPES", "offline_access drive:drive:readonly minutes:minutes.basic:read")
    assert tokens.connected()["missing_scopes"] == ["minutes:minutes.basic:read"]


def test_user_scope_error_says_reconnect(tmp_path):
    fake = FakeFeishu({("GET", f"{API}/minutes/v1/minutes/obcn1234567890"): feishu_error(99991679, "Unauthorized"),
                       ("GET", f"{API}/minutes/v1/minutes/obcn1234567890/x"): feishu_error(2091005, "permission deny")})
    tokens = connected_tokens(fake, tmp_path / "t.json")
    with fake.client() as app_client, pytest.raises(Exception, match="没有这条妙记的权限"):
        with_user(app_client, tokens).get(f"{API}/minutes/v1/minutes/obcn1234567890/x")  # 用户、应用都没权限：报应用的错误
    with fake.client() as app_client, pytest.raises(Exception, match="重新连接飞书"):
        app_client.as_user(tokens).get(f"{API}/minutes/v1/minutes/obcn1234567890")
