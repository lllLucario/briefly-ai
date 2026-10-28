"""真实浏览器＋隔离后端：知识库入库、来源、跨库草稿与迟到响应。"""
import json

import pytest
pytest.importorskip("sqlite_vec", reason="知识网页回归需安装.[rag]")
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


def open_knowledge(page):
    page.locator('.tabs [data-tab="knowledge"]').click()
    expect(page.locator("#kb-name")).to_be_visible()


def create(page, name):
    page.locator("#kb-name").fill(name)
    page.locator("#kb-create").click()
    expect(page.locator("#kb-select option:checked")).to_have_text(name)
    return page.locator("#kb-select").input_value()


def import_file(page, content="# 制度\n正式员工出差须在预订前审批。"):
    page.locator("#kb-files").set_input_files({"name":"制度.md", "mimeType":"text/markdown", "buffer":content.encode()})
    page.locator("#kb-upload").click()
    expect(page.locator("#kb-jobs")).to_contain_text("等待核对切片", timeout=20000)
    page.locator("[data-kb-preview]").first.click()
    expect(page.locator("#kb-preview")).to_be_visible()
    expect(page.locator("#kb-preview-body")).to_contain_text("预订前审批")
    expect(page.locator("#kb-confirm")).to_be_disabled()
    page.locator("#kb-preview-checked").check()
    page.locator("#kb-confirm").click()
    expect(page.locator("#kb-preview")).not_to_be_visible()
    expect(page.locator("[data-kb-job]").first).to_contain_text("已完成", timeout=20000)


def test_import_confirm_external_choice_and_original_reference(web_page):
    page = web_page
    open_knowledge(page)
    create(page, "办公制度")
    page.locator("#kb-question").fill("它什么时候？")
    page.locator("#kb-ask").click()
    expect(page.locator("#kb-result")).to_contain_text("请补充问题")
    import_file(page)
    expect(page.locator("#kb-documents")).to_contain_text("已有有效索引")
    page.locator("#kb-question").fill("什么时候审批？")
    page.locator("#kb-ask").click()
    expect(page.locator("#kb-result")).to_contain_text("等待外发确认")
    page.locator("#kb-external").check()
    page.locator("#kb-ask").click()
    expect(page.locator("#kb-result")).to_contain_text("草稿回答")
    page.locator("[data-kb-reference]").first.click()
    expect(page.locator("#kb-source-text")).to_contain_text("预订前审批")
    expect(page.locator("#kb-source-info")).to_contain_text("当前有效版本")
    page.locator("#kb-source-close").click()
    page.locator("#kb-all").uncheck()
    page.locator("[data-kb-scope]").uncheck()
    page.locator("#kb-ask").click()
    expect(page.locator("#kb-result")).to_contain_text("范围为空")


def test_late_answer_stays_with_original_library_and_drafts_return(web_page):
    page = web_page
    open_knowledge(page)
    a = create(page, "制度A")
    b = create(page, "制度B")
    page.locator("#kb-select").select_option(a)
    page.locator("#kb-question").fill("A库原始问题")
    held = []
    page.route(f"**/knowledge-bases/{a}/answer", lambda route: held.append(route))
    page.locator("#kb-ask").click()
    expect(page.locator("#kb-ask")).to_be_disabled()
    page.locator("#kb-select").select_option(b)
    page.locator("#kb-question").fill("B库草稿")
    assert held
    held[0].fulfill(status=200, content_type="application/json", body=json.dumps(dict(status="answered", answer="A库迟到结果", evidence=[])))
    expect(page.locator("#kb-result")).not_to_contain_text("A库迟到结果")
    expect(page.locator("#kb-question")).to_have_value("B库草稿")
    page.locator("#kb-select").select_option(a)
    expect(page.locator("#kb-question")).to_have_value("A库原始问题")
    expect(page.locator("#kb-result")).to_contain_text("A库迟到结果")
    page.locator('.tabs [data-tab="materials"]').click()
    open_knowledge(page)
    expect(page.locator("#kb-question")).to_have_value("A库原始问题")


def test_preview_and_answer_errors_are_visible_and_retryable(web_page):
    page = web_page
    open_knowledge(page)
    kb = create(page, "错误恢复")
    page.route(f"**/knowledge-bases/{kb}/answer", lambda route: route.fulfill(status=409, content_type="application/json", body=json.dumps({"detail":"资料已经变化，请重新核验"})))
    page.locator("#kb-question").fill("如何审批？")
    page.locator("#kb-ask").click()
    expect(page.locator("#kb-message")).to_contain_text("资料已经变化")
    expect(page.locator("#kb-ask")).to_be_enabled()
    expect(page.locator("#kb-question")).to_have_value("如何审批？")


def test_updated_document_keeps_old_source_and_removed_document_is_not_searched(web_page):
    page = web_page
    open_knowledge(page)
    create(page, "版本检查")
    import_file(page)
    page.locator("#kb-question").fill("如何审批？")
    page.locator("#kb-ask").click()
    expect(page.locator("#kb-result")).to_contain_text("等待外发确认")
    old_ref = page.locator("[data-kb-reference]").first.get_attribute("data-kb-reference")
    page.locator("[data-kb-update]").click()
    import_file(page, "# 制度\n正式员工出差须在预订前审批，新增书面申请要求。")
    expect(page.locator("#kb-result")).to_contain_text("文档已更新或移出")
    assert page.locator("[data-kb-reference]").first.get_attribute("data-kb-reference") == old_ref
    page.locator("[data-kb-reference]").first.click()
    expect(page.locator("#kb-source-info")).to_contain_text("历史版本")
    expect(page.locator("#kb-source-text")).not_to_contain_text("新增书面")
    page.locator("#kb-source-close").click()
    page.locator("[data-kb-remove]").click()
    page.locator("#ui-ok").click()
    expect(page.locator("#kb-documents")).to_contain_text("当前库暂无文档")
    page.locator("#kb-ask").click()
    expect(page.locator("#kb-result")).to_contain_text("库内暂无可用资料")


def test_knowledge_controls_fit_narrow_screen(web_page):
    page = web_page
    page.set_viewport_size({"width":320, "height":1000})
    open_knowledge(page)
    create(page, "窄屏资料库")
    expect(page.locator("#kb-upload")).to_be_visible()
    assert page.locator("body").evaluate("element => element.scrollWidth <= innerWidth")
