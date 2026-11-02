"""知识库卡片、持久目录、分页片段与迟到响应隔离。"""
import json

import pytest
from playwright.sync_api import expect

from .test_knowledge import open_knowledge, create, import_file

pytestmark = pytest.mark.browser


def test_published_document_can_be_read_and_paged_without_model(web_page):
    p = web_page
    open_knowledge(p)
    create(p, "文档预览库")
    import_file(p, "# 出差审批\n正式员工出差须在预订前审批。\n\n" + "原创虚构资料，请对照原文核对。" * 500)
    p.locator("[data-kb-document]").first.click()
    expect(p.locator("#kb-inspector")).to_contain_text("当前可检索版本")
    expect(p.locator(".kb-chunk-card")).to_have_count(10)
    expect(p.locator(".kb-chunk-card").first).to_contain_text("出差审批")
    expect(p.locator(".kb-file-stats")).to_contain_text("512")
    first = p.locator(".kb-chunk-heading").first.text_content()
    p.locator('[data-inspect-page="10"]').click()
    expect(p.locator(".kb-chunk-heading").first).not_to_have_text(first)
    p.locator('[data-inspect-page="0"]').click()
    expect(p.locator(".kb-chunk-heading").first).to_have_text(first)
    p.locator("#kb-file-search").fill("不存在的文件")
    expect(p.locator("#kb-documents")).to_contain_text("没有匹配的文件")
    p.locator("#kb-file-search").fill("")
    expect(p.locator("[data-kb-document]")).to_have_count(1)


def test_folder_tree_persists_with_same_named_files_and_can_collapse(web_page, tmp_path):
    p = web_page
    folder = tmp_path / "企业资料"
    for department in ("人事", "财务"):
        target = folder / department
        target.mkdir(parents=True)
        (target / "制度.md").write_text(f"# {department}制度\n{department}原创虚构内容。", encoding="utf-8")
    open_knowledge(p)
    kb = create(p, "目录资料")
    p.locator("#kb-open-upload").click()
    p.locator("#kb-add-folder").set_input_files(str(folder))
    p.locator("#kb-upload").click()
    expect(p.locator("#kb-jobs")).to_contain_text("等待核对切片", timeout=30000)
    expect(p.locator("[data-kb-preview]")).to_have_count(2)
    for index in range(2):
        p.locator("[data-kb-preview]").nth(index).click()
        expect(p.locator("#kb-preview-checked")).to_be_enabled()
        p.locator("#kb-preview-checked").check()
        p.locator("#kb-confirm").click()
        expect(p.locator("[data-kb-job]").nth(index)).to_contain_text("已完成", timeout=30000)
    p.reload()
    p.locator('[data-tab="knowledge"]').click()
    expect(p.locator("[data-kb-document]")).to_have_count(2)
    expect(p.locator("#kb-documents")).to_contain_text("人事")
    expect(p.locator("#kb-documents")).to_contain_text("财务")
    root = p.locator(".kb-folder").first
    root.locator(":scope > summary").click()
    expect(p.locator("[data-kb-document]").first).to_be_hidden()
    root.locator(":scope > summary").click()
    files = p.request.get(p.url.rstrip("/") + f"/knowledge-bases/{kb}/documents").json()
    assert {doc["relative_path"] for doc in files} == {"企业资料/人事/制度.md", "企业资料/财务/制度.md"}
    p.locator('[data-kb-document][title="企业资料/财务/制度.md"]').click()
    expect(p.locator("#kb-inspector")).to_contain_text("财务原创虚构内容")


def test_card_switch_discards_late_preview_and_can_open_assistant(web_page):
    p = web_page
    open_knowledge(p)
    a = create(p, "库A")
    import_file(p)
    b = create(p, "库B")
    held = []
    p.route(f"**/knowledge-bases/{a}/documents/*/versions/*/chunks?*", lambda route: held.append(route))
    with p.expect_request(lambda request: f"/knowledge-bases/{a}/documents/" in request.url and "/chunks?" in request.url):
        p.locator(f'[data-kb-base="{a}"]').click()
    expect(p.locator("#kb-inspector")).to_contain_text("正在读取")
    assert held
    # 等待请求到达；跨库返回也不能显示旧库内容。
    p.locator(f'[data-kb-base="{b}"]').click()
    expect(p.locator("#kb-current-name")).to_have_text("库B")
    for route in held:
        route.fulfill(content_type="application/json", body=json.dumps({"total":1,"items":[{"chunk":1,"content":"库A迟到片段","constraints":{}}]}))
    expect(p.locator("#kb-inspector")).not_to_contain_text("库A迟到片段")
    p.locator("#kb-to-chat").click()
    expect(p.locator("#tab-chat")).to_be_visible()
    expect(p.locator("#chat-kb")).to_have_value(b)


@pytest.mark.parametrize("width", [320, 1440])
def test_explorer_layout_fits_and_keeps_manual_review(web_page, width):
    p = web_page
    p.set_viewport_size(dict(width=width,height=1000))
    open_knowledge(p)
    create(p, "窄屏资料库")
    import_file(p)
    p.locator("[data-kb-document]").click()
    expect(p.locator(".kb-chunk-card")).to_be_visible()
    assert p.locator("body").evaluate("e => e.scrollWidth <= innerWidth")
    assert p.locator("#kb-inspector").evaluate("e => e.scrollWidth <= e.clientWidth")


def test_running_old_server_can_preview_through_original_chunk_endpoint(web_page):
    p = web_page
    open_knowledge(p)
    create(p, "兼容预览")
    import_file(p)
    p.route("**/versions/*/chunks?*", lambda route: route.fulfill(status=404, content_type="application/json", body='{"detail":"Not Found"}'))
    p.reload()
    p.locator('[data-tab="knowledge"]').click()
    expect(p.locator(".kb-chunk-card")).to_contain_text("预订前审批")


def test_old_server_does_not_silently_flatten_folder_upload(web_page, tmp_path):
    p = web_page
    directory = tmp_path / "资料"
    directory.mkdir()
    (directory / "制度.md").write_text("原创内容。", encoding="utf-8")
    open_knowledge(p)
    create(p, "目录兼容")
    p.route("**/openapi.json", lambda route: route.fulfill(content_type="application/json", body='{"paths":{}}'))
    sent = []
    p.on("request", lambda request: sent.append(request.url) if request.method == "POST" and request.url.endswith("/imports") else None)
    p.locator("#kb-open-upload").click()
    p.locator("#kb-add-folder").set_input_files(str(directory))
    p.locator("#kb-upload").click()
    expect(p.locator("#kb-transfers")).to_contain_text("文件尚未发送")
    expect(p.locator("[data-kb-transfer-retry]")).to_be_enabled()
    assert sent == []
