"""W4-01：启用令牌后的登录、退出、会话过期。"""
import os

import pytest

from .support import live_app

pytestmark = pytest.mark.browser

TOKEN = "browser-test-token"


@pytest.fixture
def secured(tmp_path):
    from playwright.sync_api import sync_playwright

    with live_app(tmp_path, access_token=TOKEN) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=os.getenv("BRIEFLY_BROWSER_CHANNEL") or None)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(url + "/") else route.abort())
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            yield page, context, url
            assert not errors, errors
        finally:
            context.unroute_all(behavior="ignoreErrors")  # 用例结束时可能还有被拦截的请求，别让关闭等它们
            context.close()
            browser.close()


def login(page, token=TOKEN):
    page.locator("#login-token").fill(token)
    page.locator("#login-submit").click()


def test_login_flow_wrong_token_success_and_logout(secured):
    from playwright.sync_api import expect

    page, _, url = secured
    page.goto(url)
    expect(page).to_have_url(f"{url}/login")
    page.locator("#login-submit").click()
    expect(page.locator("#login-msg")).to_contain_text("请输入访问令牌")
    login(page, "wrong")
    expect(page.locator("#login-msg")).to_contain_text("访问令牌不正确")
    expect(page).to_have_url(f"{url}/login")
    login(page)
    expect(page).to_have_url(f"{url}/")
    expect(page.locator("#logout")).to_be_visible()
    expect(page.locator("#app-status")).to_be_hidden()
    page.locator("#logout").click()
    expect(page).to_have_url(f"{url}/login")
    page.goto(url)
    expect(page).to_have_url(f"{url}/login")  # 退出后确实进不去


def test_login_page_styles_load_without_login(secured):
    page, _, url = secured
    page.goto(f"{url}/login")
    assert page.evaluate("getComputedStyle(document.querySelector('.login-card')).borderRadius") == "16px"


def test_expired_session_redirects_to_login_on_next_action(secured):
    from playwright.sync_api import expect

    page, context, url = secured
    page.goto(url)
    login(page)
    expect(page.locator("#logout")).to_be_visible()
    page.wait_for_load_state("networkidle")  # 初始化请求要先结束，否则它们拿到 401 就先跳走了，测不到“下一步操作”
    context.clear_cookies()
    page.locator('.tabs [data-tab="materials"]').click()
    expect(page).to_have_url(f"{url}/login")


def test_knowledge_upload_with_expired_session_is_not_silent(secured):
    from playwright.sync_api import expect

    page, _, url = secured
    page.goto(url)
    login(page)
    expect(page.locator("#logout")).to_be_visible()
    page.locator('.tabs [data-tab="knowledge"]').click()
    page.locator("#kb-empty-create").click()
    page.locator("#kb-name").fill("过期上传")
    page.locator("#kb-create-files").set_input_files(dict(name="制度.md", mimeType="text/markdown", buffer=b"# x\ny"))
    # 只让上传这一步碰到会话过期：先建库的请求还要成功，下一条用例核对真实接口返回的就是这个响应
    page.route("**/knowledge-bases/*/imports", lambda route: route.continue_() if route.request.method != "POST" else route.fulfill(
        status=401, content_type="application/json", body='{"detail":"请先登录。","code":"auth_required"}'))
    page.locator("#kb-create").click()
    expect(page.locator("#kb-transfers")).to_contain_text("登录已过期")
    assert "/login" not in page.url  # 不自动跳走，待传的文件还在


def relogin_in_new_tab(context, url):
    tab = context.new_page()
    tab.goto(url)
    login(tab)
    tab.wait_for_url(f"{url}/")
    tab.close()


def test_upload_after_real_expiry_stays_and_retry_after_relogin_succeeds(secured):
    """真实清 Cookie、不拦截请求：/openapi.json 也是 401，不能跳走丢文件。"""
    from playwright.sync_api import expect

    page, context, url = secured
    page.goto(url)
    login(page)
    expect(page.locator("#logout")).to_be_visible()
    assert context.request.post(f"{url}/knowledge-bases", data={"name": "已有库"}).ok
    page.locator('.tabs [data-tab="knowledge"]').click()
    expect(page.locator("#kb-open-upload")).to_be_visible()
    page.locator("#kb-open-upload").click()
    page.locator("#kb-files").set_input_files(dict(name="制度.md", mimeType="text/markdown", buffer=b"# x\ny"))
    context.clear_cookies()
    page.locator("#kb-upload").click()
    transfers = page.locator("#kb-transfers")
    expect(transfers).to_contain_text("登录已过期")
    expect(transfers).to_contain_text("制度.md")
    expect(transfers.locator('a[href="/login"]')).to_be_visible()
    assert page.url.startswith(f"{url}/") and "/login" not in page.url
    relogin_in_new_tab(context, url)
    transfers.locator("[data-kb-transfer-retry]").click()
    expect(page.locator("#kb-jobs")).to_contain_text("制度.md", timeout=30000)
    expect(transfers).not_to_contain_text("登录已过期")


def test_create_after_real_expiry_keeps_selected_files(secured):
    from playwright.sync_api import expect

    page, context, url = secured
    page.goto(url)
    login(page)
    expect(page.locator("#logout")).to_be_visible()
    page.locator('.tabs [data-tab="knowledge"]').click()
    page.locator("#kb-empty-create").click()
    page.locator("#kb-name").fill("过期新建")
    page.locator("#kb-create-files").set_input_files(dict(name="手册.md", mimeType="text/markdown", buffer=b"# x\ny"))
    context.clear_cookies()
    page.locator("#kb-create").click()
    progress = page.locator("#kb-create-progress")
    expect(progress).to_contain_text("登录已过期")
    expect(progress.locator('a[href="/login"]')).to_be_visible()
    assert "/login" not in page.url
    page.locator("#kb-create-edit").click()
    expect(page.locator("#kb-create-selection")).to_contain_text("手册.md")
    relogin_in_new_tab(context, url)
    page.locator("#kb-create").click()
    expect(page.locator("#kb-jobs")).to_contain_text("手册.md", timeout=30000)


def test_real_upload_endpoint_returns_auth_required_after_expiry(secured):
    from playwright.sync_api import expect

    page, context, url = secured
    page.goto(url)
    login(page)
    expect(page.locator("#logout")).to_be_visible()
    page.wait_for_load_state("networkidle")  # 否则初始化请求拿到 401 会在用例收尾时跳转，撞上关闭浏览器后可能卡死
    context.clear_cookies()
    response = context.request.post(f"{url}/knowledge-bases/1/imports", timeout=10000,
                                    multipart=dict(files=dict(name="a.txt", mimeType="text/plain", buffer=b"x")))
    assert response.status == 401
    assert response.json()["code"] == "auth_required"


def test_no_logout_entry_without_token(web_page):
    from playwright.sync_api import expect

    expect(web_page.locator(".tabs")).to_be_visible()
    web_page.wait_for_load_state("networkidle")
    expect(web_page.locator("#logout")).to_be_hidden()
