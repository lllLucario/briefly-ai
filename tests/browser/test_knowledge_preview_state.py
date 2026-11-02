"""导入完成、读取失败与可确认状态必须明确区分。"""
import json

import pytest
from playwright.sync_api import expect

from .test_knowledge import open_knowledge, create, import_file

pytestmark = pytest.mark.browser


def test_completed_import_is_read_only_even_when_paging(web_page):
    p = web_page
    open_knowledge(p)
    create(p, "完成状态")
    import_file(p, "# 制度\n正式员工出差须在预订前审批。\n\n" + "这是用于界面回归的原创虚构条款。" * 450)
    p.locator("#kb-import-history > summary").click()
    button = p.locator("[data-kb-preview]")
    expect(button).to_have_text("查看已入库片段")
    sent = []
    p.on("request", lambda request: sent.append(request.url) if request.method == "POST" and request.url.endswith("/confirm") else None)
    button.click()
    expect(p.locator("#kb-preview-title")).to_have_text("查看已入库片段")
    expect(p.locator("#kb-preview-state")).to_contain_text("无需再次确认")
    expect(p.locator("#kb-preview-review")).to_be_hidden()
    expect(p.locator("#kb-confirm")).to_be_hidden()
    expect(p.locator("#kb-confirm")).to_be_disabled()
    p.locator("#kb-preview-next").click()
    expect(p.locator("#kb-preview-info")).to_contain_text("第11～")
    expect(p.locator("#kb-preview-review")).to_be_hidden()
    expect(p.locator("#kb-confirm")).to_be_disabled()
    assert sent == []


def test_pending_preview_error_cannot_enable_confirmation_and_reopens_cleanly(web_page):
    p = web_page
    open_knowledge(p)
    kb = create(p, "预览读取失败")
    uploaded = p.request.post(p.url.rstrip("/") + f"/knowledge-bases/{kb}/imports", multipart={
        "files":{"name":"制度.md","mimeType":"text/markdown","buffer":"# 制度\n正式员工出差须在预订前审批。".encode()},
        "request_key":"pending-preview"}).json()["files"][0]
    p.evaluate("() => window.loadKnowledge()")
    expect(p.locator("#kb-jobs")).to_contain_text("等待核对切片", timeout=30000)
    route_path = f"**/knowledge-bases/{kb}/imports/{uploaded['id']}/chunks?*"
    p.route(route_path, lambda route: route.fulfill(status=503, content_type="application/json", body=json.dumps({"detail":"暂时读不到原文"})))
    p.locator("[data-kb-preview]").click()
    expect(p.locator("#kb-preview-error")).to_contain_text("暂时读不到原文")
    expect(p.locator("#kb-preview-checked")).to_be_disabled()
    expect(p.locator("#kb-confirm")).to_be_visible()
    expect(p.locator("#kb-confirm")).to_be_disabled()
    p.locator("#kb-preview-close").click()
    p.unroute(route_path)
    p.locator("[data-kb-preview]").click()
    expect(p.locator("#kb-preview-checked")).to_be_enabled()
    expect(p.locator("#kb-preview-state")).to_be_hidden()
    expect(p.locator("#kb-confirm")).to_be_disabled()
    p.locator("#kb-preview-checked").check()
    expect(p.locator("#kb-confirm")).to_be_enabled()
