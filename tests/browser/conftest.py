import os

import pytest

from .support import live_app


@pytest.fixture
def web_page(tmp_path):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.fail('先安装可选依赖：pip install -e ".[dev,browser]"')
    with live_app(tmp_path) as url, sync_playwright() as playwright:
        channel = os.getenv("BRIEFLY_BROWSER_CHANNEL") or None
        browser = playwright.chromium.launch(channel=channel)
        context = browser.new_context(viewport={"width":1440,"height":1000}, timezone_id="Australia/Sydney")
        # 单元回归不依赖CDN和外部网络；页面须能退回纯文本展示。
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(url+"/") else route.abort())
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(error.stack or str(error)))
        try:
            page.goto(url)
            yield page
            assert not errors, errors
        except BaseException:
            page.screenshot(path=str(tmp_path/"failure.png"), full_page=True)
            raise
        finally:
            context.close()
            browser.close()
