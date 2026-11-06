"""知识库重命名和删除的页面：按钮、确认框、列表刷新、旧引用提示、聊天页的选择。"""
import pytest

pytest.importorskip("sqlite_vec", reason="知识网页回归需安装.[rag]")
from playwright.sync_api import expect

from .flows import send
from .test_knowledge import allow_egress, ask_assistant, create, import_file, open_knowledge

pytestmark = pytest.mark.browser


def open_delete(page):
    page.locator("#kb-open-settings").click()
    page.locator("#kb-delete").click()
    expect(page.locator("#ui-dialog")).to_be_visible()


def test_rename_updates_card_title_and_chat_list(web_page):
    p = web_page
    open_knowledge(p)
    kb = create(p, "旧名称")
    p.locator("#kb-open-settings").click()
    p.locator("#kb-rename").click()
    field = p.locator('#ui-fields [data-ui="0"]')
    expect(field).to_have_value("旧名称")
    field.fill("新名称")
    p.locator("#ui-ok").click()
    expect(p.locator("#kb-current-name")).to_have_text("新名称")
    expect(p.locator(f'[data-kb-base="{kb}"]')).to_contain_text("新名称")
    expect(p.locator("#kb-select option:checked")).to_have_text("新名称")
    expect(p.locator("#kb-message")).to_contain_text("已重命名")
    expect(p.locator(f'#chat-kb option[value="{kb}"]')).to_contain_text("新名称")
    # 空名称不提交；取消不改
    p.locator("#kb-open-settings").click()
    p.locator("#kb-rename").click()
    field.fill("  ")
    p.locator("#ui-ok").click()
    expect(p.locator("#ui-error")).to_contain_text("请填写")
    p.locator("#ui-cancel").click()
    expect(p.locator("#kb-current-name")).to_have_text("新名称")


def test_delete_confirm_states_consequences_then_list_refreshes(web_page):
    p = web_page
    open_knowledge(p)
    keep = create(p, "保留的库")
    gone = create(p, "要删除的库")
    import_file(p)
    open_delete(p)
    box = p.locator("#ui-message")
    for text in ("要删除的库", "不能恢复", "1 份文件", "切片", "索引", "外发授权", "工作区材料不受影响", "来源已删除"):
        expect(box).to_contain_text(text)
    # 取消：什么都不发生
    p.locator("#ui-cancel").click()
    expect(p.locator(f'[data-kb-base="{gone}"]')).to_be_visible()
    open_delete(p)
    with p.expect_response(lambda r: r.request.method == "DELETE") as response:
        p.locator("#ui-ok").click()
    assert response.value.status == 200 and "confirm=true" in response.value.url
    expect(p.locator(f'[data-kb-base="{gone}"]')).to_have_count(0)
    expect(p.locator("#kb-current-name")).to_have_text("保留的库")  # 自动选中剩下的库
    expect(p.locator("#kb-select")).to_have_value(keep)
    expect(p.locator("#kb-message")).to_contain_text("已删除知识库《要删除的库》")
    expect(p.locator("[data-kb-document]")).to_have_count(0)
    expect(p.locator(f'#chat-kb option[value="{gone}"]')).to_have_count(0)


def test_deleting_last_base_shows_empty_state(web_page):
    p = web_page
    open_knowledge(p)
    create(p, "唯一的库")
    open_delete(p)
    p.locator("#ui-ok").click()
    expect(p.locator("#kb-empty")).to_be_visible()
    expect(p.locator("#kb-content")).to_be_hidden()
    expect(p.locator("#kb-base-cards [data-kb-base]")).to_have_count(0)


def test_confirm_mentions_workspace_links(web_page):
    p = web_page
    open_knowledge(p)
    create(p, "带关联的库")
    import_file(p)
    p.locator("[data-kb-copy]").click()
    expect(p.locator("#kb-message")).to_contain_text("已把")
    open_delete(p)
    for text in ("1 条与本库的关联会断开", "1 份已关联的材料删除后继续禁止发给模型", "在材料页解除限制", "工作区材料不会删除"):
        expect(p.locator("#ui-message")).to_contain_text(text)
    p.locator("#ui-ok").click()
    expect(p.locator("#kb-empty")).to_be_visible()
    p.locator('.tabs [data-tab="materials"]').click()
    expect(p.locator("#tab-materials")).to_contain_text("制度")  # 材料还在


def test_running_import_blocks_delete(web_page):
    p = web_page
    open_knowledge(p)
    kb = create(p, "入库中的库")
    p.route(f"**/knowledge-bases/{kb}/deletion-preview", lambda route: route.fulfill(json=dict(
        kb_id=kb, name="入库中的库", documents=2, active_imports=1, workspace_links=0)))
    open_delete(p)
    expect(p.locator("#ui-message")).to_contain_text("1 份文件正在入库")
    expect(p.locator("#ui-cancel")).to_be_hidden()  # 只能知道了，没有删除的入口
    p.locator("#ui-ok").click()
    expect(p.locator(f'[data-kb-base="{kb}"]')).to_be_visible()


def test_server_conflict_is_explained_and_base_stays(web_page):
    p = web_page
    open_knowledge(p)
    kb = create(p, "竞争的库")
    p.route(f"**/knowledge-bases/{kb}?confirm=true", lambda route: route.fulfill(status=409, json=dict(detail="这个知识库还有 1 份文件正在入库，请等任务结束后再删除")))
    open_delete(p)
    p.locator("#ui-ok").click()
    expect(p.locator("#ui-title")).to_have_text("没有删除")
    expect(p.locator("#ui-message")).to_contain_text("正在入库")
    p.locator("#ui-ok").click()
    expect(p.locator(f'[data-kb-base="{kb}"]')).to_be_visible()


def test_old_reference_and_chat_selection_after_delete(web_page):
    p = web_page
    open_knowledge(p)
    kb = create(p, "将被删除的库")
    import_file(p)
    allow_egress(p)
    answer = ask_assistant(p, kb, "请结合制度帮我列两点出差审批注意事项")
    reference = answer.locator("[data-chat-reference]").first
    text = answer.locator(".bubble").inner_text()
    expect(reference).to_be_visible()
    open_knowledge(p)
    open_delete(p)
    p.locator("#ui-ok").click()
    expect(p.locator("#kb-empty")).to_be_visible()

    p.locator('.tabs [data-tab="chat"]').click()
    expect(p.locator("#chat-kb")).to_have_value("")  # 选择退回“不查知识库”，并说明原因
    expect(p.locator("#chat-kb-notice")).to_contain_text("已删除或不可用")
    assert answer.locator(".bubble").inner_text() == text  # 历史回答不改写
    reference.click()
    expect(p.locator("#kb-source")).to_be_visible()
    expect(p.locator("#kb-source-info")).to_have_text("来源已删除")
    expect(p.locator("#kb-source-text")).to_contain_text("知识库已删除")
    expect(p.locator("#kb-source-title")).not_to_be_empty()
    p.locator("#kb-source-close").click()
    # 继续提问不报错
    send(p, "再帮我想想别的注意事项")
    expect(p.locator(".msg .danger")).to_have_count(0)


def test_stale_page_selection_is_explained_after_chat(web_page):
    """另一个页面删除了库，这个页面的下拉框还是旧的：提问不报错，说明这一轮没有查询。"""
    p = web_page
    open_knowledge(p)
    kb = create(p, "别处删除的库")
    import_file(p)
    p.locator('.tabs [data-tab="chat"]').click()
    p.locator("#chat-kb").select_option(kb)
    assert p.request.delete(p.url.rstrip("/") + f"/knowledge-bases/{kb}?confirm=true").ok
    send(p, "请帮我列两点出差审批注意事项")
    expect(p.locator(".msg.user").last.locator("[data-kb-removed]")).to_contain_text("所选知识库已删除")
    expect(p.locator(f'#chat-kb option[value="{kb}"]')).to_have_count(0)
    expect(p.locator("#chat-kb")).to_have_value("")


def test_page_moves_on_when_current_base_was_deleted_elsewhere(web_page):
    p = web_page
    open_knowledge(p)
    keep = create(p, "留下的库")
    gone = create(p, "别处删除的库")
    assert p.request.delete(p.url.rstrip("/") + f"/knowledge-bases/{gone}?confirm=true").ok
    p.locator('.tabs [data-tab="knowledge"]').click()
    # 轮询或下一次刷新遇到 404：改选其他库，不反复提示刷新失败
    p.evaluate("() => window.loadKnowledge()")
    expect(p.locator("#kb-select")).to_have_value(keep)
    expect(p.locator("#kb-load-error")).to_be_hidden()


def test_material_page_shows_hold_and_release_needs_confirmation(web_page):
    p = web_page
    open_knowledge(p)
    create(p, "未授权的库")
    import_file(p)
    p.locator("[data-kb-copy]").click()
    expect(p.locator("#kb-message")).to_contain_text("已把")
    open_delete(p)
    p.locator("#ui-ok").click()
    expect(p.locator("#kb-empty")).to_be_visible()
    p.locator('.tabs [data-tab="materials"]').click()
    p.evaluate("""async () => {
        const items = (await (await fetch('/materials/library')).json()).items;
        await showMaterialDetail(items[0].id);
    }""")
    hold = p.locator("[data-egress-hold]")
    expect(hold).to_contain_text("未授权的库")
    expect(hold).to_contain_text("内容不会发给模型")
    p.locator("[data-hold-release]").click()
    expect(p.locator("#ui-message")).to_contain_text("可能发给模型")
    p.locator("#ui-cancel").click()  # 取消：标记还在
    expect(hold).to_be_visible()
    p.locator("[data-hold-release]").click()
    with p.expect_response(lambda r: "egress-holds/release" in r.url) as response:
        p.locator("#ui-ok").click()
    assert response.value.ok and "confirm=true" in response.value.url
    expect(hold).to_have_count(0)


def test_authorized_base_confirm_has_no_hold_sentence(web_page):
    p = web_page
    open_knowledge(p)
    create(p, "已授权的库")
    import_file(p)
    allow_egress(p)
    p.locator("[data-kb-copy]").click()
    expect(p.locator("#kb-message")).to_contain_text("已把")
    open_delete(p)
    expect(p.locator("#ui-message")).to_contain_text("1 条与本库的关联会断开")
    expect(p.locator("#ui-message")).not_to_contain_text("继续禁止")
    p.locator("#ui-cancel").click()


def test_oversized_file_is_stopped_before_upload(web_page, tmp_path):
    p = web_page
    big = tmp_path / "过大.md"
    with big.open("wb") as f:
        f.truncate(51 * 1024 * 1024)
    posted = []
    p.on("request", lambda r: posted.append(r.url) if r.method == "POST" and r.url.endswith("/imports") else None)
    open_knowledge(p)
    create(p, "上限检查")
    p.locator("#kb-open-upload").click()
    expect(p.locator("#kb-upload-dialog [data-kb-limits]")).to_contain_text("单个文件不超过50MB")
    p.locator("#kb-files").set_input_files(str(big))
    p.locator("#kb-upload").click()
    expect(p.locator("#kb-upload-error")).to_contain_text("超过单个文件 50MB")
    expect(p.locator("#kb-upload-dialog")).to_be_visible()
    assert posted == []


def test_file_between_old_and_new_limit_is_accepted(web_page, tmp_path):
    p = web_page
    file = tmp_path / "十一兆.md"
    file.write_bytes(("# 制度\n" + "正式员工出差须在预订前审批。\n" * 40).encode() + b" " * (11 * 1024 * 1024))
    open_knowledge(p)
    create(p, "十一兆库")
    p.locator("#kb-open-upload").click()
    p.locator("#kb-files").set_input_files(str(file))
    p.locator("#kb-upload").click()
    expect(p.locator("[data-kb-job]").first).to_contain_text("已完成", timeout=60000)
