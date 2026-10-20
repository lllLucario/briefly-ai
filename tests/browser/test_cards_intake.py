"""横向待办概览及单窗多来源导入；云端响应均为模拟，不连接真实飞书。"""
import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


def test_closed_drawer_response_cannot_replace_next_task(web_page):
    page=web_page
    base=page.url.rstrip("/")
    first=page.request.post(base+"/todos",data={"title":"延迟返回的任务"}).json()
    second=page.request.post(base+"/todos",data={"title":"当前查看的任务"}).json()
    page.reload()
    held=[]
    page.route(f"**/todos/{first['id']}",lambda route:held.append(route))
    first_row = page.locator(f"#todo-list [data-todo='{first['id']}']")
    first_row.locator("summary").click()
    with page.expect_request(f"**/todos/{first['id']}"):
        first_row.locator(".ticket-overview-head [data-inline-detail]").click()
    expect(page.locator("#todo-loading")).to_contain_text("正在读取")
    expect(page.locator("#todo-detail-body")).to_be_hidden()
    page.locator("#todo-close").click()
    second_row = page.locator(f"#todo-list [data-todo='{second['id']}']")
    second_row.locator("summary").click()
    second_row.locator(".ticket-overview-head [data-inline-detail]").click()
    expect(page.locator("#todo-title")).to_have_text("当前查看的任务")
    with page.expect_response(f"**/todos/{first['id']}"):
        held[0].continue_()
    page.wait_for_load_state("networkidle")
    expect(page.locator("#todo-title")).to_have_text("当前查看的任务")


def test_row_expands_children_then_detail_modal_and_returns_to_list(web_page):
    page = web_page
    page.locator("#new-todo").click()
    page.locator('#ui-fields [data-ui="0"]').fill("本周发布准备")
    page.locator("#ui-ok").click()
    expect(page.locator("#todo-title")).to_have_text("本周发布准备")
    page.locator("#todo-add-child").click()
    page.locator('#ui-fields [data-ui="0"]').fill("核对发布清单")
    page.locator("#ui-ok").click()
    child = page.locator("#todo-children [data-open-todo]")
    expect(child).to_contain_text("核对发布清单")
    child.click()
    expect(page.locator("#todo-title")).to_have_text("核对发布清单")
    expect(page.locator("#todo-add-child")).to_be_hidden()
    expect(page.locator("#todo-status")).to_have_value("todo")
    page.locator("#todo-status").select_option("done")
    expect(page.locator("#todo-msg")).to_contain_text("已完成")
    page.keyboard.press("Escape")
    expect(page.locator("#todo-panel")).not_to_be_visible()
    expect(page.locator(".todo-ticket")).to_have_count(1)
    page.locator(".todo-ticket-row").focus()
    page.keyboard.press("Enter")
    expect(page.locator(".todo-ticket")).to_have_attribute("open", "")
    expect(page.locator("#todo-panel")).not_to_be_visible()
    expect(page.locator(".ticket-expanded")).to_contain_text("1 / 1 项已完成")
    page.locator(".ticket-overview-head [data-inline-detail]").click()
    expect(page.locator("#todo-title")).to_have_text("本周发布准备")
    expect(page.locator("#todo-children")).to_contain_text("已完成")
    page.locator("#todo-close").click()
    page.locator('[data-tab="materials"]').click()
    page.locator('[data-tab="todos"]').click()
    expect(page.locator("#todo-panel")).not_to_be_visible()


@pytest.mark.parametrize("width",[1440,390])
def test_local_and_cloud_share_modal_keep_selection_and_assistant_sources(web_page,width):
    page = web_page
    page.set_viewport_size({"width":width,"height":960})
    page.route("**/feishu/status", lambda r:r.fulfill(json={"configured":True,"user":{"name":"虚构用户","open_id":"fake","missing_scopes":[]}}))
    cloud=[{"kind":"docx","kind_name":"飞书文档","name":"云端会议（虚构）","token":"fake_doc", "url":"https://example.test/docx/fake_doc","importable":True}]
    def unavailable(route):
        route.fulfill(status=503,json={"detail":"模拟云空间暂不可用"})
    page.route("**/feishu/drive/files*",unavailable)
    def import_cloud(route):
        # 真正写入隔离材料库，替代飞书HTTP读取；不伪造“已保存”但没有业务数据的回执。
        saved=page.request.post(page.url.rstrip("/")+"/materials",data={"title":"云端会议（虚构）","content":"李强负责核对会议结论。"})
        assert saved.ok
        route.fulfill(json=saved.json())
    page.route("**/materials/feishu",import_cloud)
    page.locator('[data-tab="chat"]').click()
    page.locator("#chat-add").click()
    dialog=page.locator("#add-dialog")
    expect(dialog.locator("#drive-list")).to_contain_text("模拟云空间暂不可用")
    expect(dialog.locator("#drop")).to_be_visible()
    page.unroute("**/feishu/drive/files*",unavailable)
    page.route("**/feishu/drive/files*",lambda r:r.fulfill(json=cloud))
    dialog.locator("[data-drive-retry]").click()
    choice=dialog.locator("#drive-list [data-pick]")
    choice.check()
    dialog.locator("#file").set_input_files({"name":"本地记录.md","mimeType":"text/markdown","buffer":"本地虚构工作记录。".encode("utf-8")})
    expect(dialog.locator("#add-msg")).to_contain_text("本地记录")
    expect(choice).to_be_checked()
    dialog.locator('[data-add="paste"]').click()
    dialog.locator("#paste-content").fill("尚未保存的文字草稿")
    dialog.locator("#drive-import").click()
    expect(dialog.locator("#drive-msg")).to_contain_text("已导入")
    expect(dialog.locator("#paste-content")).to_have_value("尚未保存的文字草稿")
    expect(page.locator("dialog[open]")).to_have_count(1)
    assert dialog.evaluate("el=>el.scrollWidth<=el.clientWidth")
    dialog.locator("[data-close]").click()
    expect(page.locator("#picked")).to_contain_text("本地记录")
    expect(page.locator("#picked")).to_contain_text("云端会议")
    assert len(page.request.get(page.url.rstrip("/")+"/materials").json())==2
