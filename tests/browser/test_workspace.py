"""执行主线与并行助理入口：页面选择、明确提交和不同屏幕下的可达性。"""
import pytest
from playwright.sync_api import expect

from .support import PROGRESS, TASK

pytestmark = pytest.mark.browser


def test_task_first_journey_upload_progress_and_review(web_page):
    page = web_page
    expect(page.locator('[data-tab="todos"]')).to_have_attribute("aria-current", "page")
    expect(page.locator("#page-title")).to_have_text("待办与进展")
    page.locator("#start-todo").click()
    dialog = page.locator("#ui-dialog")
    dialog.locator('[data-ui="0"]').fill("整理测试记录")
    dialog.locator('[data-ui="1"]').fill(TASK)
    dialog.locator("#ui-ok").click()
    expect(page.locator("#todo-title")).to_have_text("整理测试记录")
    expect(page.locator("#todo-status")).to_have_attribute("data-state", "todo")
    page.locator("#todo-upload").set_input_files({
        "name":"测试记录（虚构）.md", "mimeType":"text/markdown", "buffer":TASK.encode("utf-8")})
    expect(page.locator("#todo-materials")).to_contain_text("测试记录（虚构）")
    expect(page.locator("#todo-materials")).to_contain_text("副本")
    page.locator("#todo-note").fill(PROGRESS)
    page.locator("#todo-note-form button").click()
    expect(page.locator("#todo-msg")).to_contain_text("进展已记录")
    expect(page.locator("#todo-status")).to_have_attribute("data-state", "todo")
    page.locator("#todo-week").click()
    expect(page.locator('.week-steps [aria-current="step"]')).to_contain_text("收集素材")
    page.locator("#week-go").click()
    expect(page.locator("#week-checklist")).to_contain_text(PROGRESS)
    expect(page.locator('.week-steps [aria-current="step"]')).to_contain_text("核对清单")
    expect(page.locator("#week-go")).to_be_disabled()
    buttons = page.locator('#week-checklist [data-act="confirm"]')
    for _ in range(buttons.count()):
        count = buttons.count()
        with page.expect_response(lambda r: r.url.endswith("/review") and r.request.method == "POST"):
            buttons.first.click()
        expect(buttons).to_have_count(count-1)
    expect(page.locator("#week-go")).to_be_enabled()
    page.locator("#week-go").click()
    expect(page.locator("#weekly-preview")).to_contain_text("测试记录")
    expect(page.locator('.week-steps [aria-current="step"]')).to_contain_text("生成周报")
    # 整条主线不依赖助理对话；上传、记录和汇报都不会自动完成任务。
    base = page.url.rstrip("/")
    assert page.request.get(base+"/chat/sessions").json() == []
    assert page.request.get(base+"/todos").json()[0]["status"] == "todo"


def test_assistant_start_selects_skill_without_sending_or_losing_draft(web_page):
    page = web_page
    page.locator("#start-assistant").click()
    expect(page.locator('[data-tab="chat"]')).to_have_attribute("aria-current", "page")
    page.locator('[data-start="minutes"]').click()
    expect(page.locator("#skill")).to_have_value("minutes")
    expect(page.locator("#input")).to_have_value("请整理成会议纪要。")
    page.locator("#input").fill("这是尚未发送的原始输入")
    page.locator('[data-start="polish"]').click()
    expect(page.locator("#input")).to_have_value("这是尚未发送的原始输入")
    expect(page.locator("#skill")).to_have_value("polish")
    assert page.request.get(page.url.rstrip("/")+"/chat/sessions").json() == []
    page.locator("#new-session").click()
    expect(page.locator(".welcome")).to_be_visible()
    page.locator('[data-start="todo"]').click()
    expect(page.locator("#skill")).to_have_value("todo")


def test_initial_task_load_error_visible_and_retry_recovers(web_page):
    page = web_page
    page.route("**/todos?*", lambda route: route.fulfill(status=503,json={"detail":"暂不可用"}))
    page.reload()
    expect(page.locator("#todo-load-msg")).to_contain_text("加载待办失败")
    page.unroute("**/todos?*")
    page.locator("[data-retry-todos]").click()
    expect(page.locator("#todo-load-msg")).to_be_hidden()
    expect(page.locator("#todo-list")).to_contain_text("没有未完成的待办")
    page.route("**/skills", lambda route: route.fulfill(status=503,json={"detail":"初始化暂不可用"}))
    page.reload()
    expect(page.locator("#app-status")).to_contain_text("连接服务失败")
    expect(page.locator("#tab-todos")).to_be_visible()
    page.unroute("**/skills")
    page.reload()
    expect(page.locator("#app-status")).to_be_hidden()


@pytest.mark.parametrize("width,scheme", [(1440,"light"),(390,"light"),(390,"dark"),(768,"dark")])
def test_navigation_and_start_actions_fit_viewport(web_page,width,scheme):
    page = web_page
    page.set_viewport_size({"width":width,"height":900})
    page.emulate_media(color_scheme=scheme,reduced_motion="reduce")
    for tab in ["todos","chat","materials","weekly"]:
        page.locator(f'[data-tab="{tab}"]').click()
        expect(page.locator(".tabs [aria-current='page']")).to_have_count(1)
        expect(page.locator(f"#tab-{tab}")).to_be_visible()
        assert page.locator("body").evaluate("el => el.scrollWidth <= window.innerWidth"),tab
    page.locator('[data-tab="chat"]').click()
    page.locator('[data-start="polish"]').click()  # 手机滚动后也能到达次级能力
    expect(page.locator("#input")).to_be_focused()
    page.locator(".session-options summary").click()
    expect(page.locator("#show-context")).to_be_visible()
    page.keyboard.press("Tab")
