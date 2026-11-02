"""知识库卡片、持久目录、分页片段与迟到响应隔离。"""
import json

import pytest
from playwright.sync_api import expect

from .test_knowledge import open_knowledge, create, import_file

pytestmark = pytest.mark.browser


def test_document_outline_is_compact_and_has_no_body(web_page):
    p = web_page
    open_knowledge(p)
    create(p, "文档预览库")
    import_file(p, "# 出差审批\n正式员工出差须在预订前审批。\n\n" + "原创虚构资料，请对照原文核对。" * 500)
    p.locator("[data-kb-document]").first.click()
    expect(p.locator("#kb-inspector")).to_contain_text("当前可检索版本")
    expect(p.locator(".kb-outline")).to_contain_text("出差审批")
    expect(p.locator(".kb-file-stats")).to_contain_text("512")
    expect(p.locator(".kb-outline-row")).to_have_count(1)  # 同一章节的多片合为骨干一行
    expect(p.locator(".kb-chunk-card, .kb-inspect-pager, #kb-inspector pre")).to_have_count(0)
    # 骨架只附一行短摘录帮助理解本节内容，不展开长片段；片段数只标在末级章节。
    expect(p.locator(".kb-outline-preview")).to_contain_text("正式员工出差须在预订前审批")
    assert len(p.locator(".kb-outline-preview").inner_text()) <= 61
    expect(p.locator(".kb-outline-count")).to_have_attribute("title", __import__("re").compile(r"本节 \d+ 个片段"))
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
    with p.expect_file_chooser() as picker:
        p.locator("#kb-pick-folder").click()
    assert picker.value.element.evaluate("e => e.webkitdirectory")
    picker.value.set_files(str(folder))
    expect(p.locator("#kb-upload-selection")).to_contain_text("已选 2 份资料")
    expect(p.locator("#kb-upload-selection")).to_contain_text("企业资料/人事/制度.md")
    p.locator("#kb-upload").click()
    expect(p.locator("[data-kb-job]")).to_have_count(2)
    for index in range(2):
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
    expect(p.locator("#kb-inspector")).to_contain_text("财务制度")


def test_card_switch_discards_late_preview_and_can_open_assistant(web_page):
    p = web_page
    open_knowledge(p)
    a = create(p, "库A")
    import_file(p)
    b = create(p, "库B")
    held = []
    p.route(f"**/knowledge-bases/{a}/documents/*/versions/*/outline", lambda route: held.append(route))
    with p.expect_request(lambda request: f"/knowledge-bases/{a}/documents/" in request.url and "/outline" in request.url):
        p.locator(f'[data-kb-base="{a}"]').click()
    expect(p.locator("#kb-inspector")).to_contain_text("正在读取")
    assert held
    # 等待请求到达；跨库返回也不能显示旧库内容。
    p.locator(f'[data-kb-base="{b}"]').click()
    expect(p.locator("#kb-current-name")).to_have_text("库B")
    for route in held:
        route.fulfill(content_type="application/json", body=json.dumps({"total":1,"items":[{"index":1,"section":"库A迟到片段","constraints":{}}]}))
    expect(p.locator("#kb-inspector")).not_to_contain_text("库A迟到片段")
    p.locator("#kb-to-chat").click()
    expect(p.locator("#tab-chat")).to_be_visible()
    expect(p.locator("#chat-kb")).to_have_value(b)


@pytest.mark.parametrize("width", [320, 1440])
def test_explorer_layout_fits_with_outline(web_page, width):
    p = web_page
    p.set_viewport_size(dict(width=width,height=1000))
    open_knowledge(p)
    create(p, "窄屏资料库")
    import_file(p)
    p.locator("[data-kb-document]").click()
    expect(p.locator(".kb-outline")).to_be_visible()
    assert p.locator("body").evaluate("e => e.scrollWidth <= innerWidth")
    assert p.locator("#kb-inspector").evaluate("e => e.scrollWidth <= e.clientWidth")


def test_outline_error_is_visible_and_can_retry(web_page):
    p = web_page
    open_knowledge(p)
    create(p, "结构读取")
    import_file(p)
    p.route("**/versions/*/outline", lambda route: route.fulfill(status=503, content_type="application/json", body='{"detail":"结构暂不可用"}'))
    p.reload()
    p.locator('[data-tab="knowledge"]').click()
    expect(p.locator("#kb-inspector")).to_contain_text("结构暂不可用")
    p.unroute("**/versions/*/outline")
    p.locator("[data-kb-inspect-retry]").click()
    expect(p.locator(".kb-outline")).to_contain_text("制度")
    p.route("**/versions/*/outline", lambda route: route.fulfill(content_type="application/json", body='{"total":1,"status":"source_deleted","items":[{"index":1,"section":null,"constraints":{}}]}'))
    p.reload()
    p.locator('[data-tab="knowledge"]').click()
    expect(p.locator("#kb-inspector")).to_contain_text("原文已变化或不可用")
    expect(p.locator(".kb-outline")).to_have_count(0)


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


def test_add_material_buttons_open_distinct_pickers_and_show_selection(web_page):
    p = web_page
    open_knowledge(p)
    create(p, "选择资料")
    p.locator("#kb-open-upload").click()
    expect(p.locator("#kb-pick-files")).to_be_visible()
    expect(p.locator("#kb-pick-folder")).to_be_visible()
    with p.expect_file_chooser() as picker:
        p.locator("#kb-pick-files").press("Enter")
    assert picker.value.element.get_attribute("id") == "kb-files"
    assert not picker.value.element.evaluate("e => e.webkitdirectory")
    picker.value.set_files([
        dict(name="资料1.md", mimeType="text/markdown", buffer=b"first"),
        dict(name="资料2.md", mimeType="text/markdown", buffer=b"second"),
    ])
    expect(p.locator("#kb-upload-selection")).to_contain_text("已选 2 份资料")
    p.locator("[data-upload-remove]").first.click()
    expect(p.locator("#kb-upload-selection")).to_contain_text("已选 1 份资料")
    expect(p.locator("#kb-upload-selection")).not_to_contain_text("资料1.md")
    p.locator("[data-upload-clear]").click()
    expect(p.locator("#kb-upload-selection")).to_contain_text("尚未选择资料")
    p.locator("#kb-upload-close").click()
    import_file(p)
    p.locator("[data-kb-update]").click()
    expect(p.locator("#kb-pick-folder")).to_be_disabled()
    expect(p.locator("#kb-pick-files")).to_be_enabled()
