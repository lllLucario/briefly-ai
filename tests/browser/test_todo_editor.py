"""筛选常态尺寸、详情原位编辑和父子返回；假服务与隔离数据。"""
import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("width", [1440, 390, 320])
def test_filter_edit_save_cancel_and_parent_return(web_page, width):
    p = web_page
    p.set_viewport_size({"width": width, "height": 1000})
    base = p.url.rstrip("/")
    parent = p.request.post(base + "/todos", data={"title": "发布准备", "description": "原始说明", "due_date": "2026-10-23"}).json()
    child = p.request.post(base + "/todos", data={"title": "子待办核对", "parent_id": parent["id"]}).json()
    p.reload()
    filter_box = p.locator(".todo-filter-control").bounding_box()
    add_box = p.locator("#new-todo").bounding_box()
    assert filter_box["height"] == add_box["height"] == 44
    expect(p.locator('.todo-filter-control use')).to_have_attribute("href", "#i-filter")
    p.locator("#todo-filter").focus()
    assert p.locator(".todo-filter-control").bounding_box()["height"] == 44
    p.locator("#todo-filter").select_option("all")
    row = p.locator(f'[data-todo="{parent["id"]}"]')
    row.locator("summary.todo-ticket-row").click()
    row.locator(".ticket-overview-head [data-inline-detail]").click()
    expect(p.locator("#todo-title")).to_have_text("发布准备")
    expect(p.locator("#todo-back")).to_be_hidden()
    expect(p.locator("#todo-info input[type=date]")).to_have_value("2026-10-23")
    expect(p.locator("#todo-info input[type=date]")).to_be_disabled()
    p.locator("#todo-edit").click()
    expect(p.locator("#ui-dialog")).not_to_be_visible()
    expect(p.locator("#todo-edit-form")).to_be_visible()
    p.locator("#todo-edit-title").fill("更新后的发布准备")
    p.locator("#todo-edit-description").fill("在详情中直接编辑的说明")
    p.locator("#todo-edit-due").fill("2026-11-02")
    p.locator("#todo-edit-save").click()
    expect(p.locator("#todo-title")).to_have_text("更新后的发布准备")
    expect(p.locator("#todo-edit-form")).not_to_be_visible()
    expect(p.locator("#todo-info")).to_contain_text("在详情中直接编辑的说明")
    expect(p.locator("#todo-info input[type=date]")).to_have_value("2026-11-02")
    saved = p.request.get(base + f'/todos/{parent["id"]}').json()
    assert saved["created_at"] == parent["created_at"] and saved["status"] == "todo"
    p.locator("#todo-edit").click()
    p.locator("#todo-edit-description").fill("取消的修改")
    p.locator("#todo-edit-cancel").click()
    expect(p.locator("#todo-info")).to_contain_text("在详情中直接编辑的说明")
    p.locator(f'#todo-children [data-open-todo="{child["id"]}"]').click()
    expect(p.locator("#todo-title")).to_have_text("子待办核对")
    p.locator("#todo-back").click()
    expect(p.locator("#todo-title")).to_have_text("更新后的发布准备")
    expect(p.locator("#todo-back")).not_to_be_visible()
    assert p.locator(".todo-modal-nav").evaluate("e=>getComputedStyle(e).borderBottomWidth") == "0px"
    assert p.locator("body").evaluate("e=>e.scrollWidth<=innerWidth")


def test_failed_edit_preserves_draft_and_blocks_duplicate_save(web_page):
    p = web_page
    base = p.url.rstrip("/")
    task = p.request.post(base + "/todos", data={"title": "保留编辑草稿"}).json()
    p.reload()
    row = p.locator(f'[data-todo="{task["id"]}"]')
    row.locator("summary.todo-ticket-row").click()
    row.locator("[data-inline-detail]").click()
    p.locator("#todo-edit").click()
    p.locator("#todo-edit-description").fill("失败后保留这份说明")
    p.locator("#todo-edit-due").fill("2026-12-01")
    held = []
    p.route(f'**/todos/{task["id"]}', lambda r: held.append(r) if r.request.method == "PUT" else r.continue_())
    p.locator("#todo-edit-save").click()
    expect(p.locator("#todo-edit-save")).to_be_disabled()
    p.mouse.click(2, 2)
    p.keyboard.press("Escape")
    expect(p.locator("#todo-panel")).to_be_visible()
    assert len(held) == 1
    held[0].fulfill(status=503, json={"detail": "模拟保存失败"})
    expect(p.locator("#todo-msg")).to_contain_text("保存失败")
    expect(p.locator("#todo-edit-description")).to_have_value("失败后保留这份说明")
    expect(p.locator("#todo-edit-due")).to_have_value("2026-12-01")
    p.unroute(f'**/todos/{task["id"]}')
    p.locator("#todo-edit-save").click()
    expect(p.locator("#todo-edit-form")).not_to_be_visible()
    assert p.request.get(base + f'/todos/{task["id"]}').json()["due_date"] == "2026-12-01"
    # 显式清空截止日期同样可以保存。
    p.locator("#todo-edit").click()
    p.locator("#todo-edit-due").fill("")
    p.locator("#todo-edit-save").click()
    expect(p.locator("#todo-info")).to_contain_text("未设置")
    assert p.request.get(base + f'/todos/{task["id"]}').json()["due_date"] is None
