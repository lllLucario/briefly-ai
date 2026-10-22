"""F11/F16：半成品与发送不确定状态不能显示为成功。所有外部响应均模拟。"""
import pytest
from playwright.sync_api import expect

from .support import PROGRESS

pytestmark = pytest.mark.browser


def open_generated_report(page):
    base = page.url.rstrip("/")
    report = page.request.post(base + "/weekly/week", data={"day": "2026-10-12"}).json()
    rid = report["id"]
    assert page.request.put(base + f"/weekly/{rid}/note", data={"note": PROGRESS}).ok
    assert page.request.post(base + f"/weekly/{rid}/generate", data={}).ok
    page.route("**/feishu/status", lambda r: r.fulfill(json={"configured": True, "notify_user": False, "folder": False, "user": None}))
    page.route("**/mail/status", lambda r: r.fulfill(json={"configured": True, "sender": "me@example.com", "default_to": "boss@example.com"}))
    page.reload()
    page.locator('[data-tab="weekly"]').click()
    page.locator(f"#weekly-list [data-id='{rid}']").click()
    expect(page.locator("#weekly-preview")).to_contain_text("本周周报", timeout=20000)
    return rid


def test_partial_feishu_record_links_to_document_and_retry_requires_choice(web_page):
    page = web_page
    posts = []
    partial = {"id": 1, "title": "半成品周报", "url": "https://example.feishu.cn/docx/partial", "created_at": "2026-10-22T12:00:00", "status": "failed", "error": "写入未完成", "share_error": ""}
    def export(route):
        if route.request.method == "POST":
            posts.append(route.request.url)
            route.fulfill(json=partial)
        else:
            route.fulfill(json=[partial])
    page.route("**/weekly/*/feishu", export)
    open_generated_report(page)
    box = page.locator("#weekly-feishu")
    expect(box).to_contain_text("上次写入未完成")
    expect(box.locator("a")).to_have_attribute("href", partial["url"])
    page.locator("#feishu-weekly").click()
    dialog = page.get_by_role("dialog").filter(has_text="再次写入会新建文档")
    expect(dialog).to_be_visible()
    dialog.get_by_role("button", name="取消", exact=True).click()
    assert posts == []
    page.locator("#feishu-weekly").click()
    dialog.get_by_role("button", name="确定", exact=True).click()
    expect(box).to_contain_text("飞书写入未完成")
    assert len(posts) == 1


def test_unknown_mail_result_is_visible_in_history(web_page):
    page = web_page
    page.route("**/weekly/*/email", lambda r: r.fulfill(json=[{"created_at": "2026-10-22T12:00:00", "status": "unknown", "to": "boss@example.com", "cc": "", "error": "邮件可能已发出，请先检查已发送邮件再重试"}]))
    open_generated_report(page)
    expect(page.locator("#weekly-mail")).to_contain_text("发送结果待核对")
    expect(page.locator("#weekly-mail")).to_contain_text("可能已发出")
    assert "发送失败" not in page.locator("#weekly-mail").inner_text()


def test_export_history_response_after_switching_week_does_not_start_write(web_page):
    page = web_page
    base = page.url.rstrip("/")
    other = page.request.post(base + "/weekly/week", data={"day": "2026-09-28"}).json()["id"]
    held, posts = [], []
    state = {"hold": False}
    def export(route):
        if route.request.method == "POST":
            posts.append(route.request.url)
            route.fulfill(json={})
        elif state["hold"]:
            state["hold"] = False
            held.append(route)
        else:
            route.fulfill(json=[])
    page.route("**/weekly/*/feishu", export)
    rid = open_generated_report(page)
    state["hold"] = True
    with page.expect_request(f"**/weekly/{rid}/feishu"):
        page.locator("#feishu-weekly").click()
    page.locator(f"#weekly-list [data-id='{other}']").click()
    expect(page.locator("#week-title")).to_contain_text("09.28")
    held[0].fulfill(json=[])
    page.wait_for_load_state("networkidle")
    assert posts == []
