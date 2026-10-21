"""主页面原位展开只读概览，明确点击才打开任务详情。"""
import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("width,scheme",[(1440,"light"),(390,"light"),(390,"dark")])
def test_ticket_rows_expand_in_place_keep_status_at_end_and_do_not_write(web_page,width,scheme):
    page=web_page
    base=page.url.rstrip("/")
    parent=page.request.post(base+"/todos",data={"title":"发布准备","description":"核对上线范围\n确认回滚说明","due_date":"2026-10-23"}).json()
    page.request.post(base+"/todos",data={"title":"另一个主任务"})
    child=page.request.post(base+"/todos",data={"title":"核对灰度记录","parent_id":parent["id"]}).json()
    page.set_viewport_size({"width":width,"height":900})
    page.emulate_media(color_scheme=scheme)
    page.reload()
    expect(page.locator(".todo-ticket")).to_have_count(2)
    writes=[]
    page.on("request",lambda r:writes.append(r.url) if r.method in ["POST","PUT","PATCH","DELETE"] else None)
    row=page.locator(f"#todo-list > [data-todo='{parent['id']}']")
    row.locator("summary.todo-ticket-row").click()
    expect(row.locator(".ticket-expanded")).to_be_visible()
    expect(page.locator("#todo-panel")).not_to_be_visible()
    expect(row.locator(".ticket-description")).to_contain_text("确认回滚说明")
    expect(row.locator(".inline-children")).to_contain_text("核对灰度记录")
    summary=row.locator("summary.todo-ticket-row").bounding_box()
    status=row.locator("summary > .ticket-status").bounding_box()
    assert status["x"]+status["width"]>=summary["x"]+summary["width"]-30
    first,second=page.locator(".todo-ticket-row").all()
    assert second.bounding_box()["y"]>first.bounding_box()["y"]+first.bounding_box()["height"]
    assert page.locator("body").evaluate("el=>el.scrollWidth<=window.innerWidth")
    page.locator('[data-tab="materials"]').click()
    page.locator('[data-tab="todos"]').click()
    expect(row.locator("details.todo-ticket")).to_have_attribute("open", "")
    row.locator(f'.inline-children [data-inline-detail="{child["id"]}"]').click()
    expect(page.locator("#todo-title")).to_have_text("核对灰度记录")
    page.keyboard.press("Escape")
    expect(row.locator(".ticket-expanded")).to_be_visible()
    row.locator("summary.todo-ticket-row").click()
    expect(row.locator(".ticket-expanded")).not_to_be_visible()
    assert writes==[]
