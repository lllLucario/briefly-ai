"""详情与轻量确认的真实浏览器回归，不调用真实服务。"""
import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("width", [1440, 390])
def test_detail_status_buttons_backdrop_and_popconfirm(web_page, width):
    page = web_page
    page.set_viewport_size({"width": width, "height": 960})
    base = page.url.rstrip("/")
    task = page.request.post(base + "/todos", data={"title": "核对详情细节"}).json()
    page.reload()
    row = page.locator(f'[data-todo="{task["id"]}"]')
    row.locator("summary.todo-ticket-row").click()
    row.locator("[data-inline-detail]").click()
    expect(page.locator("#todo-status")).to_have_attribute("data-state", "todo")
    expect(page.locator("select#todo-status")).to_have_count(0)
    page.locator("#todo-material-section > summary").click()
    upload = page.locator('label:has(#todo-upload)').bounding_box()
    pick = page.locator("#todo-pick").bounding_box()
    assert abs(upload["height"] - pick["height"]) < 1
    close = page.locator("#todo-close").bounding_box()
    assert close["width"] == close["height"] == 34
    # 内容和内边距不是遮罩；点击内部不关闭。
    page.locator("#todo-title").click()
    expect(page.locator("#todo-panel")).to_be_visible()
    page.locator("#todo-status").click()
    page.locator('[data-status-choice="doing"]').click()
    expect(page.locator("#todo-status")).to_have_attribute("data-state", "doing")
    assert page.request.get(base + f'/todos/{task["id"]}').json()["status"] == "doing"
    page.mouse.click(2, 2)
    expect(page.locator("#todo-panel")).not_to_be_visible()
    trigger = row.locator("summary > .ticket-status")
    trigger.click()
    page.locator('[data-status-choice="remove"]').click()
    pop = page.locator("#todo-status-menu")
    expect(pop).to_have_attribute("role", "dialog")
    expect(pop).to_contain_text("无法恢复")
    expect(page.locator("#ui-dialog")).not_to_be_visible()
    assert page.request.get(base + f'/todos/{task["id"]}').status == 200
    page.keyboard.press("Escape")
    expect(pop).not_to_be_visible()
    trigger.click()
    page.locator('[data-status-choice="remove"]').click()
    page.mouse.click(2, 2)
    expect(pop).not_to_be_visible()
    assert page.request.get(base + f'/todos/{task["id"]}').status == 200
    # 通用输入弹窗点遮罩按取消处理，Promise 不悬挂，也不创建任务。
    page.locator("#new-todo").click()
    page.locator('[data-ui="0"]').fill("取消的草稿")
    page.mouse.click(2, 2)
    expect(page.locator("#ui-dialog")).not_to_be_visible()
    assert len(page.request.get(base + "/todos").json()) == 1
    page.locator("#new-todo").click()
    expect(page.locator("#ui-dialog")).to_be_visible()
    page.locator("#ui-cancel").click()
