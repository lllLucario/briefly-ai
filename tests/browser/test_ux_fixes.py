"""W4-04 体验走查修复：接口失败、断网、重复提交、空状态、长文本、深色对比度。

web_page 固件会断言没有未捕获的页面错误，所以这里的失败场景同时验证“不会漏成未处理的 Promise”。
"""
import os
import re

import pytest
from playwright.sync_api import expect

from briefly import config
from briefly.llm import create_chat_model

from .flows import send
from .support import MEETING, live_app

pytestmark = pytest.mark.browser

SERVER_ERROR = {"detail": "服务内部错误", "request_id": "req-ux-1"}


def server_error(route):
    route.fulfill(status=500, json=SERVER_ERROR, headers={"X-Request-ID": "req-ux-1"})


def base(page):
    return re.match(r"https?://[^/]+", page.url)[0]


def tab(page, name):
    page.locator(f'.tabs [data-tab="{name}"]').click()


def seed_todo(page, title="待办一", **fields):
    response = page.request.post(base(page) + "/todos", data={"title": title, "status": "todo", **fields})
    assert response.ok, response.text()
    return response.json()


def ready(page):
    page.wait_for_load_state("networkidle")
    return page


# ---------- 断网和服务错误


def test_offline_messages_are_chinese_and_notice_clears_after_recovery(web_page):
    page = ready(web_page)
    page.context.set_offline(True)
    tab(page, "chat")
    expect(page.locator("#session-list")).to_contain_text("连不上服务")
    page.locator("#input").fill("断网时发送")
    page.locator("#send").click()
    expect(page.locator(".msg.assistant .bubble").last).to_contain_text("连不上服务")
    expect(page.locator("#input")).to_have_value("断网时发送")  # 输入保留，恢复后可以直接重发
    tab(page, "knowledge")
    expect(page.locator("#kb-load-error")).to_contain_text("连不上服务")
    tab(page, "weekly")
    expect(page.locator("#week-msg")).to_contain_text("连不上服务")
    assert "Failed to fetch" not in page.locator("body").inner_text()
    # 没有专门处理的操作走全局兜底：顶部状态条提示，恢复连接后自动消失
    tab(page, "todos")
    page.evaluate("() => { api('/todos').then(() => {}); }")
    expect(page.locator("#app-status")).to_contain_text("操作没有完成")
    page.context.set_offline(False)
    page.locator("[data-retry-todos]").click()
    expect(page.locator("#todo-load-msg")).to_be_hidden()
    expect(page.locator("#app-status")).to_be_hidden()


def test_first_load_failure_still_loads_current_page_and_can_be_dismissed(web_page):
    page = web_page
    seed_todo(page, "能看到的待办")
    page.route("**/chat/sessions", server_error)
    page.route("**/knowledge-bases", server_error)
    page.reload()
    expect(page.locator("#todo-list")).to_contain_text("能看到的待办")
    status = page.locator("#app-status")
    expect(status).to_contain_text("部分内容没有加载成功")
    expect(status).to_contain_text("追踪号：req-ux-1")
    status.get_by_role("button", name="关闭").click()
    expect(status).to_be_hidden()
    # 首屏失败后切到助理页能恢复会话列表，不能一直显示空
    expect(page.locator("#session-list")).to_contain_text("会话记录加载失败")
    page.unroute("**/chat/sessions")
    tab(page, "chat")
    expect(page.locator("#session-list")).to_contain_text("还没有会话")


def test_validation_error_is_not_raw_json(web_page):
    page = ready(web_page)
    page.route("**/todos", lambda route: route.fulfill(status=422, json={"detail": [{"type": "date_from_datetime_parsing", "loc": ["body", "due_date"], "msg": "Input should be a valid date"}]}) if route.request.method == "POST" else route.continue_())
    page.locator("#new-todo").click()
    page.locator("#ui-fields input").first.fill("校验错误")
    page.locator("#ui-ok").click()
    message = page.locator("#ui-message")
    expect(message).to_contain_text("提交的内容格式不正确")
    assert "date_from_datetime_parsing" not in message.inner_text()


def test_action_failure_without_own_handler_reports_in_status_bar(web_page):
    page = ready(web_page)
    tab(page, "chat")
    send(page, "请整理这份会议纪要")
    page.route("**/chat/sessions/*", server_error)
    page.locator(".session-options summary").click()
    page.locator("#delete-session").click()
    page.locator("#ui-ok").click()
    expect(page.locator("#app-status")).to_contain_text("操作没有完成")
    expect(page.locator("#app-status")).to_contain_text("追踪号：req-ux-1")


def test_action_failure_inside_dialog_is_not_hidden_behind_it(web_page):
    page = ready(web_page)
    tab(page, "weekly")
    expect(page.locator("#week-go")).to_be_enabled()
    page.locator("#open-settings").click()
    expect(page.locator("#settings-dialog")).to_be_visible()
    page.route("**/weekly/items/1/sources", server_error)
    page.evaluate("() => { showEvidenceSources(1); }")
    expect(page.locator("#ui-dialog")).to_be_visible()
    expect(page.locator("#ui-title")).to_have_text("操作没有完成")


# ---------- 材料


def test_materials_failure_shows_error_and_retry(web_page):
    page = ready(web_page)
    page.request.post(base(page) + "/materials", data={"title": "材料一", "content": MEETING})
    page.route(re.compile(r"/materials(/library)?(\?.*)?$"), server_error)
    tab(page, "materials")
    box = page.locator("#material-rows .notice.error")
    expect(box).to_contain_text("材料加载失败")
    expect(box).to_contain_text("追踪号：req-ux-1")
    page.unroute(re.compile(r"/materials(/library)?(\?.*)?$"))
    page.locator("[data-lib-retry]").click()
    expect(page.locator("#material-rows")).to_contain_text("材料一")


def test_tracked_files_failure_is_not_shown_as_empty(web_page):
    page = ready(web_page)
    tab(page, "materials")
    page.route("**/tracking/**", server_error)
    page.locator('#mat-tabs [data-mt="tracked"]').click()
    expect(page.locator("#tracking-msg")).to_contain_text("追踪的文件加载失败")
    expect(page.locator("#tracked-label")).to_have_text("追踪信息暂时读取不到")
    page.unroute("**/tracking/**")
    page.locator("[data-tracking-retry]").click()
    expect(page.locator("#tracking-msg")).to_be_hidden()
    expect(page.locator("#tracked-label")).to_have_text("还没有追踪的文件")


def test_double_click_save_material_sends_one_request(web_page):
    page = ready(web_page)
    posts = []
    page.on("request", lambda r: posts.append(r) if r.method == "POST" and r.url.endswith("/materials") else None)
    tab(page, "materials")
    page.locator("#open-add").click()
    page.locator('[data-add="paste"]').click()
    page.locator("#paste-content").fill("连点保存的内容")
    page.locator("#paste-form button").dblclick()
    expect(page.locator("#add-msg")).to_contain_text("已保存")
    page.wait_for_load_state("networkidle")
    assert len(posts) == 1


def test_double_click_add_tracked_path_sends_one_request(web_page, tmp_path):
    page = ready(web_page)
    source = tmp_path / "追踪的文件.txt"
    source.write_text("第一版内容", encoding="utf-8")
    posts = []
    page.on("request", lambda r: posts.append(r) if r.method == "POST" and r.url.endswith("/tracking/sources") else None)
    tab(page, "materials")
    page.locator('#mat-tabs [data-mt="tracked"]').click()
    expect(page.locator("#add-source-box")).to_have_attribute("open", "")
    page.locator("#source-paths").fill(str(source))
    page.locator("#source-add").dblclick()
    expect(page.locator("#tracking-msg")).to_contain_text("已添加")
    page.wait_for_load_state("networkidle")
    assert len(posts) == 1


def test_double_click_import_feishu_link_sends_one_request(web_page):
    page = ready(web_page)
    posts = []
    def refuse(route):
        posts.append(route.request)
        route.fulfill(status=400, json={"detail": "读取失败"})
    page.route("**/materials/feishu", refuse)
    tab(page, "materials")
    page.locator("#open-add").click()
    page.locator('#add-tabs [data-add="cloud"]').click()
    page.locator('#cloud-tabs [data-add="link"]').click()
    page.locator("#feishu-url").fill("https://example.feishu.cn/docx/abc")
    page.locator("#feishu-form button").dblclick()
    expect(page.locator("#add-msg")).to_contain_text("读取失败")
    expect(page.locator("#feishu-form button")).to_be_enabled()  # 失败后可以重试
    page.wait_for_load_state("networkidle")
    assert len(posts) == 1


# ---------- 助理


def test_failed_session_switch_stays_on_current_session(web_page):
    page = ready(web_page)
    tab(page, "chat")
    send(page, "请整理这份会议纪要")
    first_thread = page.evaluate("() => state.threadId")
    page.locator("#new-session").click()
    send(page, "只保留李强的行动项")
    second_thread = page.evaluate("() => state.threadId")
    assert first_thread != second_thread
    page.locator("#input").fill("还没发送的草稿")
    page.route(f"**/chat/sessions/{first_thread}", server_error)
    page.locator(f'#session-list li[data-id="{first_thread}"]').click()
    expect(page.locator("#chat-msg")).to_contain_text("打开会话失败")
    expect(page.locator("#chat-msg")).to_contain_text("追踪号：req-ux-1")
    assert page.evaluate("() => state.threadId") == second_thread
    expect(page.locator("#input")).to_have_value("还没发送的草稿")
    page.unroute(f"**/chat/sessions/{first_thread}")
    page.locator(f'#session-list li[data-id="{first_thread}"]').click()
    expect(page.locator("#chat-msg")).to_be_hidden()
    expect(page.locator(".msg.user").first).to_contain_text("请整理这份会议纪要")
    assert page.evaluate("() => state.threadId") == first_thread


def test_session_error_notice_and_list_behave_on_failure(web_page):
    page = ready(web_page)
    tab(page, "chat")
    send(page, "请整理这份会议纪要")
    thread = page.evaluate("() => state.threadId")
    page.locator("#new-session").click()
    # 刷新列表失败时，已有的会话还能点
    page.route("**/chat/sessions", server_error)
    tab(page, "todos")
    tab(page, "chat")
    expect(page.locator("#session-list .session-error")).to_contain_text("会话记录加载失败")
    expect(page.locator(f'#session-list li[data-id="{thread}"]')).to_be_visible()
    # 错误行不能被单行省略截断，否则看不到重试按钮
    assert page.locator("#session-list .session-error").evaluate("li => li.scrollWidth <= li.clientWidth")
    expect(page.locator("[data-retry-sessions]")).to_be_visible()
    page.unroute("**/chat/sessions")
    # 打开失败的提示，在下一次发送时清掉
    page.route(f"**/chat/sessions/{thread}", server_error)
    page.locator(f'#session-list li[data-id="{thread}"]').click()
    expect(page.locator("#chat-msg")).to_be_visible()
    page.unroute(f"**/chat/sessions/{thread}")
    send(page, "只保留李强的行动项")
    expect(page.locator("#chat-msg")).to_be_hidden()
    expect(page.locator("#session-list .session-error")).to_have_count(0)


def test_reference_failure_is_visible(web_page):
    from .test_knowledge import allow_egress, ask_assistant, create, import_file, open_knowledge

    pytest.importorskip("sqlite_vec", reason="知识网页回归需安装.[rag]")
    page = ready(web_page)
    open_knowledge(page)
    kb = create(page, "引用失败")
    import_file(page)
    allow_egress(page)
    answer = ask_assistant(page, kb, "请结合制度帮我列两点出差审批注意事项")
    page.route("**/references/**", server_error)
    answer.locator("[data-chat-reference]").first.click()
    expect(page.locator("#ui-title")).to_have_text("打不开原文")
    expect(page.locator("#ui-message")).to_contain_text("追踪号：req-ux-1")


def test_markdown_links_open_in_new_tab(web_page):
    html = ready(web_page).evaluate("() => renderMarkdown('[说明](https://example.invalid/a)')")
    assert 'target="_blank"' in html and "noopener" in html


# ---------- 模型未配置


@pytest.fixture
def no_model_page(tmp_path, monkeypatch):
    from playwright.sync_api import sync_playwright

    monkeypatch.setattr(config, "API_KEY", "")
    monkeypatch.setattr(config, "MODEL", "")
    with live_app(tmp_path, model_factory=create_chat_model) as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=os.getenv("BRIEFLY_BROWSER_CHANNEL") or None)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(url + "/") else route.abort())
        page = context.new_page()
        page.set_default_timeout(10000)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(url)
            yield page
            assert not errors, errors
        finally:
            context.close()
            browser.close()


def test_missing_model_is_explained_before_sending_and_other_pages_work(no_model_page):
    page = ready(no_model_page)
    notice = page.locator("#model-status")
    expect(notice).to_be_hidden()  # 待办不用模型，不打扰
    seed_todo(page)
    page.reload()
    expect(page.locator("#todo-list")).to_contain_text("待办一")
    tab(page, "chat")
    expect(notice).to_contain_text("还没有配置模型")
    assert "BRIEFLY_" not in notice.inner_text()
    page.locator("#input").fill("请整理纪要")
    page.locator("#send").click()
    expect(page.locator(".msg.assistant .bubble").last).to_contain_text("模型配置不完整")
    expect(page.locator("#input")).to_have_value("请整理纪要")
    tab(page, "materials")
    expect(notice).to_be_hidden()
    tab(page, "weekly")
    expect(notice).to_be_visible()


# ---------- 周报


def test_weekly_load_failure_offers_reload_instead_of_stuck_loading(web_page):
    page = ready(web_page)
    page.route("**/weekly/week*", server_error)
    tab(page, "weekly")
    go = page.locator("#week-go")
    expect(go).to_have_text("重新加载")
    expect(go).to_be_enabled()
    expect(page.locator("#week-msg")).to_contain_text("追踪号：req-ux-1")
    expect(page.locator(".week-steps")).to_be_hidden()
    go.click()  # 还失败：仍可重试
    expect(go).to_have_text("重新加载")
    page.unroute("**/weekly/week*")
    go.click()
    expect(go).to_have_text("整理本周工作")
    expect(page.locator(".week-steps")).to_be_visible()
    expect(page.locator("#week-msg")).to_be_hidden()


def test_generation_polling_survives_a_failed_refresh(web_page):
    page = ready(web_page)
    seed_todo(page)
    tab(page, "weekly")
    page.locator("#week-go").click()
    expect(page.locator("#week-checklist [data-ev]")).to_have_count(1)
    report = page.evaluate("() => weekly.current.id")
    page.route(f"**/weekly/{report}", server_error)
    page.evaluate("() => { weekly.current.status = 'running'; renderWeek(); }")
    expect(page.locator("#week-msg")).to_contain_text("正在自动重试", timeout=8000)
    page.unroute(f"**/weekly/{report}")
    # 恢复后继续刷新到真实状态：这一周其实没有在生成
    expect(page.locator("#week-msg")).to_be_hidden(timeout=8000)
    expect(page.locator("#week-go")).to_have_text("确认并生成周报")


def test_double_click_add_evidence_creates_one_record(web_page):
    page = ready(web_page)
    seed_todo(page)
    tab(page, "weekly")
    page.locator("#week-go").click()
    expect(page.locator("#week-checklist [data-ev]")).to_have_count(1)
    posts = []
    page.on("request", lambda r: posts.append(r) if r.method == "POST" and r.url.endswith("/items") else None)
    page.locator("#add-evidence").click()
    page.locator("#ev-title").fill("补充记录")
    page.locator("#ev-desc").fill("文件体现不了的工作")
    page.locator("#evidence-form button.primary").dblclick()
    expect(page.locator("#evidence-dialog")).to_be_hidden()
    page.wait_for_load_state("networkidle")
    assert len(posts) == 1


def test_double_click_save_report_sends_one_request(web_page):
    page = ready(web_page)
    seed_todo(page)
    tab(page, "weekly")
    page.locator("#week-go").click()
    page.locator('#week-checklist [data-act="confirm"]').click()
    expect(page.locator("#week-go")).to_be_enabled()
    page.locator("#week-go").click()
    expect(page.locator("#edit-weekly")).to_be_enabled(timeout=30000)
    expect(page.locator("#weekly-preview")).not_to_be_empty(timeout=30000)
    puts = []
    page.on("request", lambda r: puts.append(r) if r.method == "PUT" and r.url.endswith("/content") else None)
    page.locator("#edit-weekly").click()
    page.locator("#weekly-text").fill("人工改过的正文")
    page.locator("#save-weekly").dblclick()
    expect(page.locator("#weekly-preview")).to_contain_text("人工改过的正文")
    page.wait_for_load_state("networkidle")
    assert len(puts) == 1


# ---------- 待办


def test_todo_empty_state_guides_only_first_use(web_page):
    page = ready(web_page)
    guide = page.locator("#todo-empty-panel")
    expect(guide).to_be_visible()
    expect(page.locator("#todo-list .cards-empty")).to_have_count(0)  # 不和引导重复
    # 归档为空只是一句说明，不引导创建
    page.locator("#todo-filter").select_option("archived")
    expect(page.locator("#todo-list")).to_contain_text("没有归档的待办")
    expect(guide).to_be_hidden()
    page.locator("#todo-filter").select_option("open")
    expect(guide).to_be_visible()
    # 全部完成不是“第一次用”
    done = seed_todo(page, "已完成的事")
    page.request.post(base(page) + f"/todos/{done['id']}/status", data={"status": "done", "confirm": True})
    page.reload()
    expect(page.locator("#todo-list")).to_contain_text("没有未完成的待办")
    expect(guide).to_be_hidden()


def test_long_titles_are_clamped_in_lists_and_stay_readable(web_page):
    page = ready(web_page)
    title = "很长的待办标题" * 28
    seed_todo(page, title)
    page.set_viewport_size({"width": 375, "height": 812})
    page.reload()
    name = page.locator("#todo-list .todo-name").first
    expect(name).to_be_visible()
    assert name.get_attribute("title") == title
    line = float(name.evaluate("el => parseFloat(getComputedStyle(el).lineHeight)"))
    assert name.bounding_box()["height"] <= line * 3 + 1
    assert page.locator("body").evaluate("el => el.scrollWidth <= innerWidth")
    page.locator("#todo-list summary").first.click()  # 展开后显示完整标题
    assert name.bounding_box()["height"] > line * 3 + 1


def test_expanded_row_survives_render_before_toggle_event(web_page):
    """点开待办行的同时列表重新渲染（toggle 事件还没处理），行不能自己收起来；再点一次能收起。"""
    page = ready(web_page)
    seed_todo(page, "竞态探测")
    page.reload()
    page.wait_for_selector("details[data-ticket]")
    page.wait_for_load_state("networkidle")
    click_then_render = """() => {
      document.querySelector('details[data-ticket] summary').click();
      renderTodoList();
      return new Promise(res => setTimeout(() => res(document.querySelector('details[data-ticket]').open), 300));
    }"""
    assert page.evaluate(click_then_render) is True
    assert page.evaluate(click_then_render) is False
    # 点状态图标不改变展开状态
    page.locator("details[data-ticket] [data-status-menu]").first.click()
    expect(page.locator("#todo-status-menu")).to_be_visible()
    page.keyboard.press("Escape")
    assert page.evaluate("() => { renderTodoList(); return document.querySelector('details[data-ticket]').open; }") is False


def test_new_todo_title_has_length_limit(web_page):
    page = ready(web_page)
    page.locator("#new-todo").click()
    page.locator("#ui-fields input").first.fill("长" * 300)
    assert len(page.locator("#ui-fields input").first.input_value()) == 200


# ---------- 知识库


def test_knowledge_reload_keeps_open_upload_dialog_and_update_target(web_page):
    """登记表第 30 条：切回知识库页、列表还没返回时打开弹窗，返回后弹窗不能自己关，更新目标不能丢。"""
    from .test_knowledge import create, import_file, open_knowledge

    pytest.importorskip("sqlite_vec", reason="知识网页回归需安装.[rag]")
    page = ready(web_page)
    open_knowledge(page)
    create(page, "推迟加载")
    import_file(page)
    tab(page, "todos")
    held = []
    page.route("**/knowledge-bases", lambda route: held.append(route))
    tab(page, "knowledge")
    page.wait_for_function("() => document.querySelector('#tab-knowledge').classList.contains('active')")
    page.locator("[data-kb-update]").click()
    dialog = page.locator("#kb-upload-dialog")
    expect(dialog).to_be_visible()
    expect(page.locator("#kb-update-target")).to_contain_text("正在更新")
    assert held  # 确实卡在加载中
    for route in held:
        route.continue_()
    page.unroute("**/knowledge-bases")
    page.wait_for_load_state("networkidle")
    expect(dialog).to_be_visible()
    expect(page.locator("#kb-update-target")).to_contain_text("正在更新")


def test_remove_from_knowledge_confirmation_names_the_action(web_page):
    from .test_knowledge import create, import_file, open_knowledge

    pytest.importorskip("sqlite_vec", reason="知识网页回归需安装.[rag]")
    page = ready(web_page)
    open_knowledge(page)
    create(page, "移出确认")
    import_file(page)
    page.locator("[data-kb-remove]").click()
    expect(page.locator("#ui-title")).to_have_text("移出知识库")
    expect(page.locator("#ui-ok")).to_have_text("移出")
    expect(page.locator("#ui-ok")).to_have_class("danger-solid")
    page.locator("#ui-cancel").click()
    expect(page.locator("#kb-documents")).to_contain_text("制度.md")  # 取消后没有移出


def test_knowledge_load_failure_has_retry(web_page):
    page = ready(web_page)
    page.route("**/knowledge-bases", server_error)
    tab(page, "knowledge")
    expect(page.locator("#kb-load-error")).to_contain_text("加载知识库失败")
    expect(page.locator("#kb-load-error")).to_contain_text("追踪号：req-ux-1")
    page.unroute("**/knowledge-bases")
    page.locator("#kb-load-retry").click()
    expect(page.locator("#kb-load-error")).to_be_hidden()
    expect(page.locator("#kb-empty-create")).to_be_visible()


def test_knowledge_upload_error_shows_request_id_and_validates_tokens(web_page):
    """登记表第 17 条：上传走 XMLHttpRequest，也要有追踪号。"""
    from .test_knowledge import create, open_knowledge

    pytest.importorskip("sqlite_vec", reason="知识网页回归需安装.[rag]")
    page = ready(web_page)
    open_knowledge(page)
    create(page, "上传失败")
    page.locator("#kb-open-upload").click()
    page.locator("#kb-files").set_input_files({"name": "a.md", "mimeType": "text/markdown", "buffer": b"# a\nb"})
    page.locator("#kb-tokens").fill("9999")
    page.locator("#kb-upload").click()
    expect(page.locator("#kb-upload-error")).to_contain_text("32～512")
    expect(page.locator("#kb-upload-dialog")).to_be_visible()
    page.locator("#kb-tokens").fill("512")
    page.route("**/imports", lambda route: server_error(route) if route.request.method == "POST" else route.continue_())
    page.locator("#kb-upload").click()
    transfer = page.locator(".kb-transfer")
    expect(transfer).to_contain_text("服务内部错误")
    expect(transfer).to_contain_text("追踪号：req-ux-1")


# ---------- 登录


def test_login_page_wording_button_and_request_id(tmp_path):
    from playwright.sync_api import sync_playwright

    with live_app(tmp_path, access_token="ux-token") as url, sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=os.getenv("BRIEFLY_BROWSER_CHANNEL") or None)
        context = browser.new_context()
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(url + "/") else route.abort())
        page = context.new_page()
        page.set_default_timeout(10000)
        try:
            page.goto(url)
            expect(page.locator(".login-card")).to_contain_text("联系部署这个服务的人")
            assert "BRIEFLY_" not in page.locator(".login-card").inner_text()
            expect(page.locator("#login-submit")).to_have_class("primary")
            page.route("**/auth/login", server_error)
            page.locator("#login-token").fill("ux-token")
            page.locator("#login-submit").click()
            expect(page.locator("#login-msg")).to_contain_text("追踪号：req-ux-1")
            expect(page.locator("#login-submit")).to_have_text("登录")
            expect(page.locator("#login-submit")).to_be_enabled()
        finally:
            context.unroute_all(behavior="ignoreErrors")
            context.close()
            browser.close()


# ---------- 对比度


CONTRAST = """() => {
  const lum = c => { const a = c.slice(0, 3).map(v => { v /= 255; return v <= .03928 ? v / 12.92 : Math.pow((v + .055) / 1.055, 2.4); }); return .2126 * a[0] + .7152 * a[1] + .0722 * a[2]; };
  const parse = s => { const m = s.match(/[\\d.]+/g).map(Number); return m.length >= 4 ? m : [...m, 1]; };
  const bgOf = el => { for (let e = el; e; e = e.parentElement) { const s = getComputedStyle(e); if (s.backgroundImage !== 'none') return null; const c = parse(s.backgroundColor); if (c[3] > .9) return c; } return null; };
  const low = [];
  for (const el of document.querySelectorAll('body *')) {
    if (el.closest('svg,script,style,template') || !el.getClientRects().length || el.disabled) continue;
    const text = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent.trim()).join('');
    if (!text) continue;
    const s = getComputedStyle(el);
    if (s.visibility === 'hidden' || +s.opacity < .5) continue;
    const bg = bgOf(el);
    if (!bg) continue;
    const a = lum(parse(s.color)), b = lum(bg), ratio = (Math.max(a, b) + .05) / (Math.min(a, b) + .05);
    if (ratio < (parseFloat(s.fontSize) >= 18.66 ? 3 : 4.5)) low.push([ratio.toFixed(2), s.fontSize, text.slice(0, 24)]);
  }
  return low;
}"""


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_text_contrast_on_all_pages(web_page, scheme):
    page = ready(web_page)
    page.emulate_media(color_scheme=scheme)
    seed_todo(page)
    page.request.post(base(page) + "/materials", data={"title": "材料一", "content": MEETING})
    low = {}
    for name in ["todos", "chat", "materials", "knowledge", "weekly"]:
        tab(page, name)
        page.wait_for_load_state("networkidle")
        found = page.evaluate(CONTRAST)
        if found:
            low[name] = found
    assert not low, low


# ---------- 晚到的材料列表响应


def wait_until(page, condition, what):
    for _ in range(1000):
        if condition():
            return
        page.wait_for_timeout(10)  # 让路由处理函数有机会运行；只是轮询，条件满足立刻返回
    raise AssertionError(f"没有等到：{what}")


class HeldMaterials:
    """扣住 GET /materials，请求发出时就取到当时的数据，由用例决定何时交给页面。

    启动时的刷新可能晚于路由安装（首屏会取两次材料），所以不能按请求序号判断哪个是旧快照，
    只按响应内容区分。退出时放行全部，不留下永远不返回的请求，否则浏览器关闭会卡住。

    退出时不 unroute，路由留到页面关闭，之后的请求直接放行。原因见 HeldRequests。
    """

    def __init__(self, page):
        self.page = page
        self.held = []  # [(route, response, 材料编号集合)]
        self.closed = False

    def __enter__(self):
        self.page.route("**/materials", self._handle)
        # 统计页面上还没结束的加载，放行后等它们真正处理完，不靠固定等待
        self.page.evaluate("""() => {
          if (window.materialsPending !== undefined) return;
          window.materialsPending = 0;
          const real = loadMaterials;
          loadMaterials = () => { window.materialsPending++; return real().finally(() => { window.materialsPending--; }); };
        }""")
        return self

    def __exit__(self, *_):
        try:
            self.drain()
        finally:
            self.closed = True
            self.release()  # drain 之后才进来的，或者 drain 没等到就退出时剩下的

    def _handle(self, route):
        if route.request.method != "GET":
            return route.fallback()
        if self.closed:
            return route.continue_()
        response = route.fetch()
        if self.closed:  # 取数据期间用例已经退出，没有人再放行它
            return route.fulfill(response=response)
        self.held.append((route, response, {m["id"] for m in response.json()}))

    def _poll(self, condition, what):
        wait_until(self.page, condition, what)

    def pending(self):
        return self.page.evaluate("() => window.materialsPending")

    def wait_all_held(self):
        """页面上发出的加载都已被扣住：这之后添加的材料，一定不在这些快照里。"""
        self._poll(lambda: self.held and len(self.held) >= self.pending(), "全部 /materials 请求被扣住")

    def wait_snapshot(self, matches, what):
        self._poll(lambda: any(matches(ids) for _, _, ids in self.held), what)

    def release(self, matches=lambda ids: True):
        for item in [x for x in self.held if matches(x[2])]:
            self.held.remove(item)
            item[0].fulfill(response=item[1])

    def release_until(self, done, matches, what):
        """持续放行符合条件的快照，直到页面状态满足 done。"""
        def step():
            self.release(matches)
            return self.page.evaluate(done)
        self._poll(step, what)

    def drain(self):
        """放行全部，直到页面上的加载都结束。"""
        def step():
            self.release()
            return not self.held and self.pending() == 0
        self._poll(step, "/materials 请求全部结束")


def picked_ids(page):
    return page.evaluate("() => [...state.picked]")


def material_ids(page):
    return page.evaluate("() => state.materials.map(m => m.id)")


def add_for_assistant(page, title):
    page.locator("#chat-add").click()
    page.locator('[data-add="paste"]').click()
    page.locator("#paste-title").fill(title)
    page.locator("#paste-content").fill(f"{title}的正文")
    page.locator("#paste-form").get_by_role("button", name="保存为材料").click()


def stale_load(page, extra):
    """发一个加载；extra 时再多混一个，模拟启动刷新等额外请求。"""
    page.evaluate("() => { loadMaterials(); }")
    if extra:
        page.evaluate("() => { loadMaterials(); }")


EXTRA = pytest.mark.parametrize("extra", [False, True], ids=["", "extra_request"])


@EXTRA
@pytest.mark.parametrize("old_first", [True, False], ids=["old_first", "new_first"])
def test_late_materials_response_keeps_just_added_material(web_page, old_first, extra):
    """添加前发出的列表响应无论先到后到，都不能删掉刚选的材料。"""
    page = ready(web_page)
    tab(page, "chat")
    with HeldMaterials(page) as held:
        stale_load(page, extra)
        held.wait_all_held()
        add_for_assistant(page, "刚导入的材料")
        expect(page.locator("#add-msg")).to_contain_text("已保存")
        added = picked_ids(page)
        assert len(added) == 1
        new = added[0]
        fresh = lambda ids: new in ids  # noqa: E731
        stale = lambda ids: new not in ids  # noqa: E731
        held.wait_snapshot(fresh, "添加后的刷新")
        applied = f"() => state.materials.some(m => m.id === {new})"
        if old_first:
            held.release(stale)
            held._poll(lambda: picked_ids(page) == added, "旧快照到达后已选材料还在")
            held.release_until(applied, fresh, "新列表生效")
        else:
            held.release_until(applied, fresh, "新列表生效")
        held.drain()  # 其余旧快照都到达
        assert picked_ids(page) == added
        assert new in material_ids(page)  # 旧响应不能覆盖较新的列表
    page.locator("#add-dialog [data-close]").click()
    expect(page.locator("#picked")).to_contain_text("刚导入的材料")


@EXTRA
def test_late_materials_response_without_newer_request_keeps_pick(web_page, extra):
    """多文件上传时，每份之间不刷新：旧响应到达时还没有更新的请求，也不能删掉。"""
    page = ready(web_page)
    tab(page, "chat")
    with HeldMaterials(page) as held:
        stale_load(page, extra)
        held.wait_all_held()
        added = page.request.post(base(page) + "/materials", data={"title": "上传中的材料", "content": "上传中的正文"}).json()["material"]["id"]
        page.evaluate("id => { state.picked.add(id); renderPicked(); }", added)
        held.drain()
        assert picked_ids(page) == [added]
    expect(page.locator("#picked .chip")).to_have_count(1)


@EXTRA
def test_really_deleted_material_is_removed_and_late_response_does_not_restore_it(web_page, extra):
    page = ready(web_page)
    tab(page, "chat")
    gone = page.request.post(base(page) + "/materials", data={"title": "要删除的材料", "content": "要删除的正文"}).json()["material"]["id"]
    # 确实选中后再开始：只有已经在列表里的材料，才能验证“删除后移除”
    page.wait_for_function("""async id => {
      await loadMaterials();
      if (!state.materials.some(m => m.id === id)) return false;
      state.picked.add(id); renderPicked();
      return true;
    }""", arg=gone)
    expect(page.locator("#picked")).to_contain_text("要删除的材料")
    with HeldMaterials(page) as held:
        stale_load(page, extra)  # 这些快照里还有它
        held.wait_all_held()
        assert all(gone in ids for _, _, ids in held.held if ids)
        assert page.request.delete(base(page) + f"/materials/{gone}").ok
        page.evaluate("() => { window.freshLoad = loadMaterials().then(() => { window.freshDone = true; }); }")
        without = lambda ids: gone not in ids  # noqa: E731
        held.release_until("() => window.freshDone === true", without, "删除后的刷新")
        assert picked_ids(page) == []  # 真的删除了：从已选列表移除
        expect(page.locator("#picked .chip")).to_have_count(0)
        held.drain()  # 还带着它的旧快照晚到
        assert gone not in material_ids(page)
        assert picked_ids(page) == []


# ---------- 周报：响应顺序和按下期间的重建


class HeldRequests:
    """扣住第一个符合条件的请求，由用例决定放行。其余请求直接放行。退出时放行，不留下永远不返回的请求。

    退出时不 unroute，路由留到页面关闭。页面可能随时发新请求（比如丢弃旧响应后的刷新），
    而撤掉页面上最后一个路由时，Playwright 服务端会把在途的请求转给 context 路由，
    客户端这边它也会转过去：同一个请求被放行两次，第二次报 Route is already handled，
    错误还要到下一次 API 调用才抛出。这个窗口在用例里关不掉，只能不撤。
    """

    def __init__(self, page, pattern, method, at_response=True):
        self.page, self.pattern, self.method, self.at_response = page, pattern, method, at_response
        self.route = self.response = None
        self.ready = self.released = False

    def __enter__(self):
        self.page.route(self.pattern, self._handle)
        return self

    def __exit__(self, *_):
        self.release()

    def _handle(self, route):
        if route.request.method != self.method or self.route is not None or self.released:
            return route.continue_()  # 只放行这一层：这里的请求都是同源的
        self.route = route  # fetch 期间可能再来同类请求：先占住，否则它会顶掉这个，这个就再也不放行了
        if self.at_response:  # 请求已由服务端处理，只是响应晚到
            self.response = route.fetch()
        self.ready = True

    def wait(self):
        wait_until(self.page, lambda: self.ready, f"{self.method} {self.pattern}")

    def release(self):
        if self.released:
            return
        self.released = True  # 放行期间新来的请求走上面的直接放行
        if self.route is None:
            return
        self.wait()  # 可能还在 fetch
        if self.at_response:
            self.route.fulfill(response=self.response)
        else:
            self.route.continue_()


TODO_STATUS_PUT = "**/weekly/*/items/*/todos/*"


def prepared_week(page):
    """整理好只有一条待确认记录的本周，返回这条记录。"""
    ready(page)
    seed_todo(page)
    tab(page, "weekly")
    page.locator("#week-go").click()
    expect(page.locator('#week-checklist [data-act="confirm"]')).to_have_count(1)
    expect(page.locator("#week-go")).to_be_disabled()
    row = page.locator("#week-checklist [data-ev]").first
    row.locator("details summary").click()
    return row


def server_pending(page):
    report = page.evaluate("() => weekly.current.id")
    return page.request.get(base(page) + f"/weekly/{report}").json()["pending"]


@pytest.mark.parametrize("at_response", [True, False], ids=["response_late", "request_late"])
def test_earlier_status_response_does_not_undo_confirm(web_page, at_response):
    """先改状态、再确认，确认先返回：不能退回待确认，两个操作的结果都要显示出来。

    request_late：服务端先处理确认，所以晚到的状态响应才是最新的，不能只是丢弃。
    """
    page = web_page
    row = prepared_week(page)
    go = page.locator("#week-go")
    with HeldRequests(page, TODO_STATUS_PUT, "PUT", at_response) as held:
        row.locator("[data-todo-status]").select_option("doing")
        held.wait()
        row.locator('[data-act="confirm"]').click()
        expect(go).to_be_enabled()
        report = page.evaluate("() => weekly.current.id")
        with page.expect_response(lambda r: r.url.endswith(f"/weekly/{report}") and r.request.method == "GET"):
            held.release()
    expect(page.locator("#week-checklist [data-todo-status]")).to_have_value("doing")
    expect(go).to_be_enabled()
    assert server_pending(page) == 0
    assert page.evaluate("() => weekly.current.pending") == 0


def test_status_response_during_press_does_not_lose_the_click(web_page):
    """按下和抬起之间响应到达：按钮不能被换掉，否则这次确认静默丢失。"""
    page = web_page
    row = prepared_week(page)
    with HeldRequests(page, TODO_STATUS_PUT, "PUT") as held:
        row.locator("[data-todo-status]").select_option("doing")
        held.wait()
        box = row.locator('[data-act="confirm"]').bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        with page.expect_response(lambda r: "/items/" in r.url and r.request.method == "PUT"):
            held.release()
        # 响应已经被页面处理（写入了 weekly.current），重建被推迟
        page.wait_for_function("""() => weekly.renderDeferred""")
        with page.expect_response(lambda r: r.url.endswith("/review")):
            page.mouse.up()
    expect(page.locator("#week-go")).to_be_enabled()
    assert server_pending(page) == 0
    expect(page.locator("#week-checklist [data-todo-status]")).to_have_value("doing")


def test_older_refresh_does_not_undo_confirm(web_page):
    """定时刷新的响应晚于确认：同样不能盖掉。"""
    page = web_page
    row = prepared_week(page)
    report = page.evaluate("() => weekly.current.id")
    with HeldRequests(page, f"**/weekly/{report}", "GET") as held:
        page.evaluate("() => { window.oldRefresh = refreshWeek(); }")
        held.wait()
        row.locator('[data-act="confirm"]').click()
        expect(page.locator("#week-go")).to_be_enabled()
        held.release()
        page.evaluate("() => window.oldRefresh")
    expect(page.locator("#week-go")).to_be_enabled()
    assert page.evaluate("() => weekly.current.pending") == 0


def previous_week_day(page):
    return page.evaluate("""() => {
      const [y, m, d] = String(weekly.current.period_start).split("-").map(Number);
      const day = new Date(y, m - 1, d - 7);
      return `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, "0")}-${String(day.getDate()).padStart(2, "0")}`;
    }""")


def test_late_response_of_previous_week_does_not_replace_opened_week(web_page):
    page = web_page
    row = prepared_week(page)
    day = previous_week_day(page)
    with HeldRequests(page, TODO_STATUS_PUT, "PUT") as held:
        row.locator("[data-todo-status]").select_option("doing")
        held.wait()
        page.evaluate("day => openWeek({ day })", day)
        assert page.evaluate("() => weekly.current.period_start") == day
        with page.expect_response(lambda r: "/items/" in r.url and r.request.method == "PUT"):
            held.release()
        # 放行后页面处理这个响应；再取一次当前状态，确认它已经处理完
        page.evaluate("async () => { await api('/weekly/week'); }")
    assert page.evaluate("() => weekly.current.period_start") == day
    expect(page.locator("#week-checklist [data-ev]")).to_have_count(0)


def test_note_typed_before_switching_week_is_saved_to_its_own_week(web_page):
    """这一周还没有记录时写说明，等创建期间换了周：说明存到原来那周，页面停在新打开的一周。"""
    page = web_page
    ready(page)
    tab(page, "weekly")
    expect(page.locator("#week-go")).to_have_text("整理本周工作")
    assert page.evaluate("() => weekly.current.id") is None
    this_week = page.evaluate("() => weekly.current.period_start")
    day = previous_week_day(page)
    with HeldRequests(page, "**/weekly/week", "POST") as held:
        page.locator("#week-note").fill("本周说明")
        page.locator("#week-note").blur()
        held.wait()
        page.evaluate("day => openWeek({ day })", day)
        with page.expect_response(lambda r: r.url.endswith("/note")) as saved:
            held.release()
    assert saved.value.json()["period_start"] == this_week
    assert saved.value.json()["note"] == "本周说明"
    page.evaluate("async () => { await api('/weekly/week'); }")
    assert page.evaluate("() => weekly.current.period_start") == day


# ---------- 扣请求的辅助类本身


@pytest.mark.parametrize("make_helper", [
    lambda page: HeldRequests(page, "**/materials", "GET"),
    HeldMaterials,
], ids=["HeldRequests", "HeldMaterials"])
def test_held_helpers_survive_request_in_flight_at_exit(web_page, make_helper):
    """请求已被服务端交给页面路由，客户端还没处理时用例退出：这个请求只能放行一次。

    扣住事件的投递来固定顺序，不靠时机：退出做完以后才把事件交给 Playwright 客户端。
    """
    page = ready(web_page)
    impl = page._impl_obj
    deliver, parked = impl._on_route, []

    async def park(route):
        parked.append(route)

    impl._on_route = park
    try:
        with make_helper(page):
            page.evaluate("() => { window.lateDone = false; fetch('/materials').then(() => { window.lateDone = true; }); }")
            wait_until(page, lambda: parked, "请求到达页面路由")
    finally:
        impl._on_route = deliver
    tasks = [impl._loop.create_task(deliver(route)) for route in parked]
    wait_until(page, lambda: all(task.done() for task in tasks), "事件处理完")
    page.wait_for_function("() => window.lateDone")
    assert [task.exception() for task in tasks] == [None] * len(tasks)


def test_held_requests_keeps_first_when_another_arrives_during_fetch(web_page):
    """第一个请求还在 route.fetch 时来了第二个：扣住的还是第一个，两个最后都要返回。"""
    page = ready(web_page)
    with HeldRequests(page, "**/weekly/week", "GET") as held:
        page.evaluate("""() => {
          window.finished = 0;
          for (let i = 0; i < 2; i++) fetch('/weekly/week').then(() => { window.finished++; });
        }""")
        held.wait()
        page.wait_for_function("() => window.finished === 1")  # 第二个已直接放行，第一个还扣着
        held.release()
    page.wait_for_function("() => window.finished === 2")
