"""主页面直接管理子待办：保存、状态独立、可恢复移除及失败恢复。"""
import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


def setup_parent(page):
    base=page.url.rstrip("/")
    parent=page.request.post(base+"/todos",data={"title":"保持进行中的主任务","status":"doing"}).json()
    page.request.post(base+"/todos",data={"title":"独立的第二项任务"})
    page.reload()
    row=page.locator(f"#todo-list > [data-todo='{parent['id']}']")
    row.locator("summary").click()
    return base,parent,row


@pytest.mark.parametrize("width",[1440,390])
def test_add_status_and_recoverable_remove_in_expanded_row(web_page,width):
    page=web_page
    page.set_viewport_size({"width":width,"height":960})
    base,parent,row=setup_parent(page)
    form=row.locator("[data-inline-add]")
    form.locator('[name="title"]').fill("核对回滚清单")
    form.locator('[name="status"]').select_option("doing")
    form.locator('[name="due"]').fill("2026-10-23")
    form.locator('button[type="submit"]').click()
    expect(row.locator(".inline-children > li")).to_have_count(1)
    expect(row.locator("[data-inline-notice]")).to_contain_text("已添加")
    expect(page.locator("#todo-panel")).not_to_be_visible()
    select=row.locator("[data-inline-status]")
    expect(select).to_have_value("doing")
    select.select_option("done")
    expect(row.locator("[data-inline-notice]")).to_contain_text("状态已更新")
    expect(row.locator(".inline-child-row > .ticket-status")).to_have_attribute("aria-label","已完成")
    children=page.request.get(base+f"/todos/{parent['id']}").json()["children"]
    child=children[0]
    assert child["status"]=="done" and child["due_date"]=="2026-10-23"
    assert page.request.get(base+f"/todos/{parent['id']}").json()["status"]=="doing"
    first,second=page.locator("#todo-list > li").all()
    assert second.bounding_box()["y"]-first.bounding_box()["y"]-first.bounding_box()["height"]>=10
    assert page.locator("body").evaluate("el=>el.scrollWidth<=window.innerWidth")
    row.locator("[data-inline-remove]").click()
    page.locator("#ui-cancel").click()
    expect(row.locator(".inline-children > li")).to_have_count(1)
    row.locator("[data-inline-remove]").click()
    page.locator("#ui-ok").click()
    expect(row.locator(".inline-children > li")).to_have_count(0)
    expect(row.locator("[data-inline-notice]")).to_contain_text("可在“已归档”中恢复")
    archived=page.request.get(base+f"/todos/{child['id']}").json()
    assert archived["archived_at"] and archived["parent_id"]==parent["id"]
    assert page.request.get(base+f"/todos/{parent['id']}").json()["status"]=="doing"


def test_failed_add_keeps_draft_blocks_duplicate_submit_and_status_rolls_back(web_page):
    page=web_page
    base,parent,row=setup_parent(page)
    held=[]
    def hold(route):
        if route.request.method=="POST":held.append(route)
        else:route.continue_()
    page.route("**/todos",hold)
    form=row.locator("[data-inline-add]")
    form.locator('[name="title"]').fill("不能丢掉的子待办草稿")
    with page.expect_request(lambda r:r.url==base+"/todos" and r.method=="POST"):
        form.locator('button[type="submit"]').click()
    expect(form.locator('button[type="submit"]')).to_be_disabled()
    form.evaluate("el=>el.requestSubmit()")
    held[0].fulfill(status=503,json={"detail":"模拟保存失败"})
    expect(row.locator("[data-inline-notice]")).to_contain_text("模拟保存失败")
    assert len(held)==1
    expect(form.locator('[name="title"]')).to_have_value("不能丢掉的子待办草稿")
    expect(form.locator('button[type="submit"]')).to_be_enabled()
    page.unroute("**/todos",hold)
    form.locator('button[type="submit"]').click()
    expect(row.locator("[data-inline-status]")).to_have_count(1)
    expect(row.locator("[data-inline-notice]")).to_contain_text("已添加")
    page.route("**/todos/*/status",lambda r:r.fulfill(status=503,json={"detail":"模拟状态保存失败"}))
    row.locator("[data-inline-status]").select_option("done")
    expect(row.locator("[data-inline-notice]")).to_contain_text("模拟状态保存失败")
    expect(row.locator("[data-inline-status]")).to_have_value("todo")
    child=page.request.get(base+f"/todos/{parent['id']}").json()["children"][0]
    assert child["status"]=="todo"
