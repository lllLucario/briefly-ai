"""在仅允许本地请求时验证渲染依赖、降级与模型配置提示。"""
import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


def test_local_markdown_renders_and_sanitizes_without_external_requests(web_page):
    page = web_page
    requests = []
    page.on("request", lambda request: requests.append(request.url))
    page.reload()
    assert page.evaluate("typeof marked.parse === 'function' && typeof DOMPurify.sanitize === 'function'")
    assert requests and all(url.startswith(page.url.rstrip("/") + "/") for url in requests)
    page.locator('[data-tab="chat"]').click()
    text = ('| 任务 | 状态 |\n| --- | --- |\n| **联调** | 完成 |\n\n'
            '<img src=x onerror="window.injected=true"><script>window.injected=true</script>')
    page.evaluate("text => appendMessage({role:'assistant', text})", text)
    bubble = page.locator(".msg.assistant .bubble").last
    expect(bubble.locator("table")).to_have_count(1)
    expect(bubble.locator("strong")).to_have_text("联调")
    expect(bubble.locator("script, [onerror]")).to_have_count(0)
    assert page.evaluate("window.injected !== true")


def test_missing_vendor_keeps_safe_plain_text_fallback(web_page):
    page = web_page
    page.route("**/web/vendor/*", lambda route: route.abort())
    page.reload()
    page.locator('[data-tab="chat"]').click()
    text = '**保留原文** <script>window.injected=true</script>'
    page.evaluate("text => appendMessage({role:'assistant', text})", text)
    bubble = page.locator(".msg.assistant .bubble").last
    expect(bubble).to_have_text(text)
    expect(bubble.locator("script, strong")).to_have_count(0)


def test_missing_model_configuration_is_visible_in_chat(web_page, monkeypatch, tmp_path):
    from briefly import config
    from briefly.llm import create_chat_model
    from .support import live_app

    monkeypatch.setattr(config, "API_KEY", "")
    monkeypatch.setattr(config, "MODEL", "fake-model")
    # 复用浏览器，但用真实模型工厂的配置校验；缺少密钥会在联网前失败。
    with live_app(tmp_path / "missing-config", model_factory=create_chat_model) as url:
        page = web_page
        page.context.route(url + "/**", lambda route: route.continue_())
        page.goto(url)
        page.locator('[data-tab="chat"]').click()
        page.locator("#input").fill("你好")
        page.locator("#send").click()
        expect(page.locator(".msg.assistant").last).to_contain_text("BRIEFLY_API_KEY")
        expect(page.locator(".msg.assistant").last).to_contain_text("重启")
        expect(page.locator("#send")).to_be_enabled()
        assert page.request.get(url + "/chat/sessions").json() == []
        assert page.request.post(url + "/materials", data={"content": "无需模型即可保存"}).status == 201
