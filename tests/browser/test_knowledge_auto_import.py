"""取消知识导入审批后，自动发布、失败恢复与只读结构展示。"""
import pytest
from playwright.sync_api import expect
from .test_knowledge import open_knowledge, create, import_file

pytestmark = pytest.mark.browser


def test_uploaded_file_publishes_without_confirmation_request(web_page):
    p = web_page
    open_knowledge(p)
    create(p, "自动入库")
    sent = []
    p.on("request", lambda request: sent.append(request.url) if request.method == "POST" and request.url.endswith("/confirm") else None)
    import_file(p, "# 制度\n正式员工出差须在预订前审批。\n\n" + "这段正文不应在文件骨架里展示。" * 450)
    expect(p.locator("#kb-preview, #kb-confirm, #kb-preview-checked")).to_have_count(0)
    expect(p.locator(".kb-outline")).to_contain_text("制度")
    assert len(p.locator(".kb-outline-preview").inner_text()) <= 61  # 只有一行短摘录，不展开正文
    assert sent == []
    expect(p.locator("#kb-external")).not_to_be_checked()


def test_empty_file_stays_failed_on_retry_without_approval(web_page):
    p = web_page
    open_knowledge(p)
    kb = create(p, "失败重试")
    # 真实隔离服务的空文件重试仍失败，不能因为移除审批而冒充成功。
    p.locator("#kb-open-upload").click()
    p.locator("#kb-files").set_input_files(dict(name="empty.txt", mimeType="text/plain", buffer=b"   "))
    p.locator("#kb-upload").click()
    expect(p.locator("#kb-jobs")).to_contain_text("处理失败", timeout=30000)
    expect(p.locator("[data-kb-retry]")).to_be_visible()
    p.locator("[data-kb-retry]").click()
    expect(p.locator("#kb-jobs")).to_contain_text("处理失败", timeout=30000)
    assert p.request.get(p.url.rstrip("/") + f"/knowledge-bases/{kb}/documents").json() == []
    expect(p.locator("#kb-confirm")).to_have_count(0)
