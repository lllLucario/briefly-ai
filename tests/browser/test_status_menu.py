"""圆圈独立操作、中文菜单、子待办数量进度和删除确认。"""
import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("width", [1440, 390])
def test_status_menu_independent_keyboard_and_progress(web_page, width):
    page = web_page
    base = page.url.rstrip("/")
    parent = page.request.post(base + "/todos", data={"title": "发布准备"}).json()
    child = page.request.post(base + "/todos", data={"title": "核对清单", "parent_id": parent["id"]}).json()
    empty = page.request.post(base + "/todos", data={"title": "独立任务"}).json()
    page.set_viewport_size({"width": width, "height": 900})
    page.reload()
    row = page.locator(f'[data-todo="{parent["id"]}"]')
    other = page.locator(f'[data-todo="{empty["id"]}"]')
    expect(other.locator("progress")).to_have_count(0)
    expect(row.locator(".ticket-progress")).to_have_text("已完成 0 / 1 个子待办")
    trigger = row.locator("summary > .ticket-status")
    trigger.click()
    expect(row.locator("details.todo-ticket")).not_to_have_attribute("open", "")
    menu = page.locator("#todo-status-menu")
    expect(menu).to_be_visible()
    page.set_viewport_size({"width": width, "height": 920})
    expect(menu).to_be_visible()
    expect(menu.locator("button")).to_have_text(["未开始✓", "进行中", "已完成", "移除"])
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    expect(trigger).to_have_attribute("aria-label", "更改发布准备的状态：进行中")
    # 点击完成父任务必须确认；取消不能更改父子状态。
    trigger.click()
    menu.locator('[data-status-choice="done"]').click()
    expect(page.locator("#ui-dialog")).to_be_visible()
    page.locator("#ui-cancel").click()
    expect(trigger).to_have_attribute("aria-label", "更改发布准备的状态：进行中")
    row.locator("summary.todo-ticket-row").click()
    row.locator(".inline-child-row .ticket-status").click()
    menu.locator('[data-status-choice="done"]').click()
    expect(row.locator("progress")).to_have_attribute("value", "1")
    expect(row.locator(".ticket-progress")).to_have_text("已完成 1 / 1 个子待办")
    assert page.request.get(base + f'/todos/{parent["id"]}').json()["status"] == "doing"
    assert page.request.get(base + f'/todos/{child["id"]}').json()["status"] == "done"
    trigger.click()
    menu.locator('[data-status-choice="remove"]').click()
    expect(menu).to_contain_text("所有子待办")
    expect(page.locator("#ui-dialog")).not_to_be_visible()
    menu.locator("[data-remove-cancel]").click()
    assert page.request.get(base + f'/todos/{parent["id"]}').status == 200
    assert page.locator("body").evaluate("el=>el.scrollWidth<=innerWidth")
    trigger.click()
    page.keyboard.press("Escape")
    expect(menu).not_to_be_visible()
