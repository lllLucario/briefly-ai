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
    context.clear_cookies()
    page.locator('[data-tab="materials"]').click()
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


def test_real_upload_endpoint_returns_auth_required_after_expiry(secured):
    from playwright.sync_api import expect

    page, context, url = secured
    page.goto(url)
    login(page)
    expect(page.locator("#logout")).to_be_visible()
    context.clear_cookies()
    result = page.evaluate("""async () => {
      const form = new FormData();
      form.append("files", new File(["x"], "a.txt"));
      return await new Promise(resolve => {
        const xhr = new XMLHttpRequest();
        xhr.open("POST", "/knowledge-bases/1/imports");
        xhr.onload = () => resolve({status: xhr.status, code: JSON.parse(xhr.responseText).code});
        xhr.onerror = () => resolve({status: 0});
        xhr.send(form);
      });
    }""")
    assert result == {"status": 401, "code": "auth_required"}


def test_no_logout_entry_without_token(web_page):
    from playwright.sync_api import expect

    expect(web_page.locator(".tabs")).to_be_visible()
    web_page.wait_for_load_state("networkidle")
    expect(web_page.locator("#logout")).to_be_hidden()
