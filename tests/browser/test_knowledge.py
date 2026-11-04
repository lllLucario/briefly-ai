"""真实浏览器＋隔离后端：知识库入库、来源、跨库草稿与迟到响应。"""
import re

import pytest
pytest.importorskip("sqlite_vec", reason="知识网页回归需安装.[rag]")
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


def open_knowledge(page):
    page.locator('.tabs [data-tab="knowledge"]').click()
    expect(page.locator("#kb-empty-create:visible, #kb-open-create:visible")).to_be_visible()
    # 按钮可见时，切页触发的加载可能还没返回；它返回后会重绘列表并关掉上传弹窗。这里等一轮完整加载，后续操作才不和它抢。
    page.evaluate("() => window.loadKnowledge()")


def create(page, name):
    # 这些用例需要空库作为范围/错误测试的前置数据；创建弹窗必须带文件，另有完整交互用例。
    response = page.request.post(page.url.rstrip("/") + "/knowledge-bases", data={"name": name})
    assert response.ok
    page.evaluate("() => window.loadKnowledge()")
    page.locator(f'[data-kb-base="{response.json()["id"]}"]').click()
    expect(page.locator("#kb-select option:checked")).to_have_text(name)
    return page.locator("#kb-select").input_value()


def allow_egress(page):
    """外发授权在本库设置弹窗里，标题旁的状态标签同步显示。"""
    expect(page.locator("#kb-egress-chip")).to_contain_text("未开启")
    page.locator("#kb-open-settings").click()
    page.locator("#kb-external").check()
    expect(page.locator("#kb-settings-status")).to_contain_text("已允许本知识库外发")
    page.locator("#kb-settings-close").click()
    expect(page.locator("#kb-egress-chip")).to_contain_text("已允许")


def ask_assistant(page, kb, question):
    from .flows import send
    page.locator('.tabs [data-tab="chat"]').click()
    page.locator("#chat-kb").select_option(kb)
    return send(page, question)


def import_file(page, content="# 制度\n正式员工出差须在预订前审批。"):
    if not page.locator("#kb-upload-dialog").is_visible():
        page.locator("#kb-open-upload").click()
    page.locator("#kb-files").set_input_files({"name":"制度.md", "mimeType":"text/markdown", "buffer":content.encode()})
    page.locator("#kb-upload").click()
    expect(page.locator("[data-kb-job]").first).to_contain_text("已完成", timeout=30000)
    expect(page.locator("#kb-preview, #kb-confirm")).to_have_count(0)
    page.locator("[data-kb-document]").first.click()
    expect(page.locator(".kb-outline-row").first).to_be_visible()


def test_auto_import_external_choice_and_original_reference(web_page):
    page = web_page
    open_knowledge(page)
    expect(page.locator("#kb-tokens")).to_have_value("512")
    kb = create(page, "办公制度")
    import_file(page)
    expect(page.locator("#kb-documents")).to_contain_text("已有有效索引")
    expect(page.locator("#kb-question, #kb-ask, [data-kb-scope]")).to_have_count(0)  # 单次问答已移除，提问走助理
    allow_egress(page)
    answer = ask_assistant(page, kb, "请结合制度帮我列两点出差审批注意事项")
    answer.locator("[data-chat-reference]").first.click()
    expect(page.locator("#kb-source-text")).to_contain_text("预订前审批")
    expect(page.locator("#kb-source-info")).to_contain_text("当前有效版本")


def test_updated_document_keeps_old_source_and_removed_document_is_not_searched(web_page):
    page = web_page
    open_knowledge(page)
    kb = create(page, "版本检查")
    import_file(page)
    allow_egress(page)
    answer = ask_assistant(page, kb, "请结合制度帮我列两点出差审批注意事项")
    old_ref = answer.locator("[data-chat-reference]").first.get_attribute("data-chat-reference")
    open_knowledge(page)
    page.locator("[data-kb-update]").click()
    import_file(page, "# 制度\n正式员工出差须在预订前审批，新增书面申请要求。")
    expect(page.locator("#kb-documents")).to_contain_text("修订 2", timeout=30000)
    page.locator('.tabs [data-tab="chat"]').click()
    old = page.locator(f'[data-chat-reference="{old_ref}"]').first
    old.click()
    expect(page.locator("#kb-source-info")).to_contain_text("历史版本")
    expect(page.locator("#kb-source-text")).not_to_contain_text("新增书面")
    page.locator("#kb-source-close").click()
    open_knowledge(page)
    page.locator("[data-kb-remove]").click()
    page.locator("#ui-ok").click()
    expect(page.locator("#kb-documents")).to_contain_text("当前库暂无文档")


def test_knowledge_controls_fit_narrow_screen(web_page):
    page = web_page
    page.set_viewport_size({"width":320, "height":1000})
    open_knowledge(page)
    create(page, "窄屏资料库")
    page.locator("#kb-open-upload").click()
    expect(page.locator("#kb-upload")).to_be_visible()
    assert page.locator("body").evaluate("element => element.scrollWidth <= innerWidth")


def test_assistant_knowledge_answer_shows_egress_and_references(web_page):
    """WP2：助理路径（追问/多问）也显示本次外发了多少片段，引用由程序记录、可打开原文。"""
    from .flows import send
    page = web_page
    open_knowledge(page)
    kb = create(page, "出差制度")
    import_file(page)
    allow_egress(page)
    page.locator('.tabs [data-tab="chat"]').click()
    page.locator("#chat-kb").select_option(kb)
    answer = send(page, "请结合制度帮我列两点出差审批注意事项")
    egress = answer.locator(".knowledge-refs [data-egress]")
    expect(egress).to_contain_text(re.compile(r"已将本轮 \d+ 个片段经规则处理后发给外部生成服务，连同对话历史"))
    answer.locator("[data-chat-reference]").first.click()
    expect(page.locator("#kb-source-text")).to_contain_text("预订前审批")
