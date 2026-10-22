"""材料库页面：搜索、组合筛选、分页、空状态、竞态与失败反馈、关联待办与添加结果。隔离数据、假模型。"""
import re

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


def base(page) -> str:
    return page.url.split("#")[0].rstrip("/")


def add(page, title: str, content: str | None = None) -> int:
    r = page.request.post(base(page) + "/materials", data={"title": title, "content": content or f"{title}：虚构正文，用于区分同名材料。"})
    return r.json()["material"]["id"]


def todo(page, title: str, parent: int | None = None) -> int:
    return page.request.post(base(page) + "/todos", data={"title": title, "parent_id": parent}).json()["id"]


def link(page, todo_id: int, material_id: int) -> None:
    page.request.post(base(page) + f"/todos/{todo_id}/materials", data={"material_id": material_id})


def open_library(page):
    page.locator('.tabs [data-tab="materials"]').click()
    expect(page.locator("#mat-library")).to_have_attribute("aria-busy", "false")


def rows(page):
    return page.locator("#material-rows tr[data-id]")


def test_search_filters_reset_and_empty_states(web_page):
    page = web_page
    open_library(page)
    expect(page.locator("#material-rows")).to_contain_text("还没有材料")
    parent = todo(page, "支付改造")
    child = todo(page, "接口联调", parent)
    a = add(page, "接口说明", "第一版接口说明：支付回调字段。")
    b = add(page, "接口说明", "第二版接口说明：退款流程补充。")
    add(page, "周会纪要")
    link(page, parent, a)
    link(page, child, b)
    open_library(page)
    expect(rows(page)).to_have_count(3)
    expect(page.locator("#lib-count")).to_have_text("共 3 份材料")

    # 同名材料靠正文预览和关联待办区分；共享关系明确标出
    same = rows(page).filter(has_text="接口说明")
    expect(same).to_have_count(2)
    expect(page.locator(f'tr[data-id="{a}"]')).to_contain_text("第一版接口说明")
    expect(page.locator(f'tr[data-id="{a}"]')).to_contain_text("来自父待办")
    expect(page.locator(f'tr[data-id="{b}"]')).to_contain_text("支付改造 › 接口联调")

    page.locator("#lib-q").fill("接口")
    expect(rows(page)).to_have_count(2)
    expect(page.locator("#lib-count")).to_have_text("找到 2 份材料")
    page.locator("#lib-relation").select_option("unlinked")
    expect(page.locator("#material-rows")).to_contain_text("没有匹配的材料")
    expect(page.locator("#material-rows")).not_to_contain_text("还没有材料")
    page.locator("#material-rows [data-lib-reset]").click()
    expect(rows(page)).to_have_count(3)
    expect(page.locator("#lib-q")).to_have_value("")
    expect(page.locator("#lib-relation")).to_have_value("all")

    # 按具体待办：子待办包含父待办共享的材料
    page.locator("#lib-relation").select_option("todo")
    dialog = page.locator("#lib-todo-dialog")
    dialog.locator("#lib-todo-q").fill("联调")
    dialog.get_by_role("button", name="支付改造 › 接口联调").click()
    expect(rows(page)).to_have_count(2)
    expect(page.locator("#lib-todo-chip")).to_contain_text("支付改造 › 接口联调")
    page.locator("#lib-reset").click()
    expect(rows(page)).to_have_count(3)

    # 点关联待办打开待办详情，不打开材料详情；关闭后回到材料库，条件不变
    page.locator("#lib-q").fill("接口")
    expect(rows(page)).to_have_count(2)
    page.locator(f'tr[data-id="{b}"] [data-lib-todo]').first.click()
    expect(page.locator("#todo-title")).to_have_text("接口联调")
    assert page.locator("#material-detail").is_hidden()
    page.locator("#todo-back").click()  # 详情内转到父待办，仍记得来源
    expect(page.locator("#todo-title")).to_have_text("支付改造")
    page.locator("#todo-close").click()
    expect(page.locator("#tab-materials")).to_be_visible()
    expect(page.locator('.tabs [data-tab="materials"]')).to_have_attribute("aria-current", "page")
    expect(page.locator("#lib-q")).to_have_value("接口")
    expect(rows(page)).to_have_count(2)

    # 在详情里转去别的页面时不跳回材料库
    page.locator(f'tr[data-id="{b}"] [data-lib-todo]').first.click()
    expect(page.locator("#todo-title")).to_have_text("接口联调")
    page.locator("#todo-week").click()
    expect(page.locator("#tab-weekly")).to_be_visible()
    page.wait_for_timeout(300)
    expect(page.locator("#tab-weekly")).to_be_visible()


def test_pagination_and_deleting_last_item_on_last_page(web_page):
    page = web_page
    for i in range(51):
        add(page, f"材料{i:02d}")
    open_library(page)
    expect(rows(page)).to_have_count(50)
    expect(page.locator("#lib-page")).to_have_text("1 / 2")
    page.locator("#lib-next").click()
    expect(rows(page)).to_have_count(1)
    expect(rows(page).first).to_contain_text("材料00")
    page.locator("#material-rows [data-delete]").click()
    page.locator("#ui-ok").click()
    expect(page.locator("#lib-page")).to_be_hidden()
    expect(rows(page)).to_have_count(50)
    expect(page.locator("#lib-count")).to_have_text("共 50 份材料")


def test_late_old_response_does_not_override_new_query(web_page):
    page = web_page
    add(page, "甲方案")
    add(page, "乙方案")
    held = []
    page.route("**/materials/library?*q=%E7%94%B2*", lambda route: held.append(route))
    open_library(page)
    page.locator("#lib-q").fill("甲")
    page.wait_for_timeout(400)
    assert held, "甲的请求应已发出并被挂起"
    page.locator("#lib-q").fill("乙")
    expect(rows(page)).to_have_count(1)
    expect(rows(page).first).to_contain_text("乙方案")
    held[0].continue_()  # 甲的旧响应晚到
    page.wait_for_timeout(300)
    expect(rows(page)).to_have_count(1)
    expect(rows(page).first).to_contain_text("乙方案")
    expect(page.locator("#lib-q")).to_have_value("乙")


def test_first_failure_is_not_empty_and_refresh_failure_keeps_results(web_page):
    page = web_page
    add(page, "保留的材料")
    fail = lambda route: route.fulfill(status=500, json={"detail": "服务暂不可用"})  # noqa: E731
    page.route("**/materials/library*", fail)
    page.locator('.tabs [data-tab="materials"]').click()
    expect(page.locator("#material-rows")).to_contain_text("材料加载失败")
    expect(page.locator("#material-rows")).not_to_contain_text("还没有材料")
    page.unroute("**/materials/library*", fail)
    page.locator("#material-rows [data-lib-retry]").click()
    expect(rows(page)).to_have_count(1)
    page.route("**/materials/library*", fail)
    page.locator("#lib-sort").select_option("title")
    expect(page.locator("#lib-error")).to_contain_text("刷新失败")
    expect(rows(page)).to_have_count(1)
    page.unroute("**/materials/library*", fail)
    page.locator("#lib-retry").click()
    expect(page.locator("#lib-error")).to_be_hidden()


def test_added_materials_are_highlighted_and_batch_view_restores_filters(web_page):
    page = web_page
    existing = "已有材料：内容相同时复用。"
    add(page, "已有材料", existing)
    open_library(page)
    page.locator("#lib-source").select_option("feishu")
    expect(page.locator("#material-rows")).to_contain_text("没有匹配的材料")
    page.locator("#open-add").click()
    page.locator("#file").set_input_files([
        {"name": "新文件（虚构）.md", "mimeType": "text/markdown", "buffer": "# 新文件\n虚构内容".encode()},
        {"name": "重复（虚构）.md", "mimeType": "text/markdown", "buffer": existing.encode()},
        {"name": "坏文件.pptx", "mimeType": "application/octet-stream", "buffer": b"x"},
    ])
    expect(page.locator("#add-msg")).to_contain_text("坏文件.pptx")
    page.locator("#add-dialog [data-close]").first.click()
    msg = page.locator("#material-msg")
    expect(msg).to_contain_text("新增 1 份")
    expect(msg).to_contain_text("直接复用")
    expect(msg).to_contain_text("失败 1 份")
    expect(page.locator("#lib-source")).to_have_value("feishu")  # 不自动清空筛选
    msg.get_by_role("button", name="查看本次添加").click()
    expect(rows(page)).to_have_count(2)
    expect(page.locator("#lib-batch")).to_contain_text("本次添加的 2 份")
    expect(page.locator("#material-rows .is-new")).to_have_count(2)
    expect(page.locator("#material-rows")).to_contain_text("新添加")
    page.locator("#lib-batch-leave").click()
    expect(page.locator("#lib-source")).to_have_value("feishu")
    expect(page.locator("#material-rows")).to_contain_text("没有匹配的材料")


@pytest.mark.parametrize("width", [390, 320])
def test_narrow_layout_has_no_horizontal_overflow(web_page, width):
    page = web_page
    parent = todo(page, "一个标题非常长的父待办用于检查窄屏换行是否正常")
    child = todo(page, "子待办同样有很长的名称需要换行显示", parent)
    m = add(page, "很长很长的材料标题" * 4)
    for t in (parent, child, todo(page, "第三个待办"), todo(page, "第四个待办")):
        link(page, t, m)
    page.set_viewport_size({"width": width, "height": 800})
    open_library(page)
    expect(page.locator("#lib-filters")).to_be_hidden()
    page.locator("#lib-filter-toggle").click()
    expect(page.locator("#lib-source")).to_be_visible()
    page.locator("#lib-tracking").select_option("fixed")
    expect(page.locator("#lib-filter-count")).to_have_text("1")
    expect(page.locator("#material-rows")).to_contain_text("另 2 项")
    overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert overflow <= 0
    delete = page.locator("#material-rows [data-delete]").first
    box = delete.bounding_box()
    assert box and box["x"] + box["width"] <= width


def test_material_detail_opens_as_modal_and_returns_to_list(web_page):
    page = web_page
    for i in range(3):
        add(page, f"材料{i}", f"第{i}份虚构正文")
    open_library(page)
    page.locator("#lib-q").fill("材料1")
    expect(rows(page)).to_have_count(1)
    rows(page).first.click()
    panel = page.locator("#material-panel")
    expect(panel).to_be_visible()
    expect(panel.locator("#material-title")).to_have_text("材料1")
    expect(panel).to_contain_text("第1份虚构正文")
    expect(rows(page).first).to_have_class(re.compile("active"))
    page.locator("#material-close").click()
    expect(panel).to_be_hidden()
    expect(page.locator("#lib-q")).to_have_value("材料1")
    expect(rows(page).first).not_to_have_class(re.compile("active"))

    # 弹窗本身不滚动：鼠标在标题上滚轮也滚正文，标题和操作常驻
    add(page, "长材料", "\n\n".join(f"第{i}段虚构正文。" * 8 for i in range(80)))
    page.evaluate("loadLibrary()")
    page.locator("#lib-q").fill("长材料")
    expect(rows(page)).to_have_count(1)
    expect(rows(page).first).to_contain_text("长材料")
    rows(page).first.click()
    title = panel.locator("#material-title")
    expect(title).to_have_text("长材料")
    box = title.bounding_box()
    page.mouse.move(box["x"] + 10, box["y"] + 5)
    page.mouse.wheel(0, 600)
    page.wait_for_function("document.querySelector('#material-body').scrollTop > 0")
    assert page.evaluate("document.querySelector('#material-panel').scrollTop") == 0
    expect(title).to_be_in_viewport()
    page.locator("#material-close").click()
    page.locator("#lib-q").fill("材料1")
    expect(rows(page)).to_have_count(1)
    expect(rows(page).first).to_contain_text("材料1")

    rows(page).first.locator(".lib-open").click()
    expect(panel).to_be_visible()
    page.keyboard.press("Escape")
    expect(panel).to_be_hidden()

    rows(page).first.click()
    panel.locator("#use-in-chat").click()
    expect(panel).to_be_hidden()
    expect(page.locator("#tab-chat")).to_be_visible()
    expect(page.locator("#picked")).to_contain_text("材料1")


MARKED_STUB = r"""window.marked = {parse: (t) => t.split("\n").map((l) => l.startsWith("## ") ? `<h2>${l.slice(3)}</h2>`
  : l.startsWith("![") ? `<img src="https://example.invalid/x.png">` : `<p>${l}</p>`).join("")};"""
PURIFY_STUB = """window.DOMPurify = {sanitize: (h, o) => { const d = document.createElement("div"); d.innerHTML = h;
  (o?.FORBID_TAGS || []).forEach((t) => d.querySelectorAll(t).forEach((e) => e.remove())); return d.innerHTML; }};"""


def test_markdown_and_word_preview_render_with_raw_toggle(web_page):
    page = web_page
    md = page.request.post(base(page) + "/materials/upload", multipart={"file": {
        "name": "方案（虚构）.md", "mimeType": "text/markdown", "buffer": "## 二、计划\n正文第一段\n![图](x.png)".encode()}}).json()["material"]["id"]
    txt = page.request.post(base(page) + "/materials/upload", multipart={"file": {
        "name": "记录（虚构）.txt", "mimeType": "text/plain", "buffer": "## 不是标题的纯文本".encode()}}).json()["material"]["id"]

    # 渲染库不可用（单元回归不连外网）：仍显示原文，不报错
    open_library(page)
    page.locator(f'tr[data-id="{md}"]').click()
    body = page.locator("#material-body")
    expect(body).to_contain_text("## 二、计划")
    page.locator("#material-close").click()

    page.route("**/marked@*/marked.min.js", lambda r: r.fulfill(body=MARKED_STUB, content_type="application/javascript"))
    page.route("**/dompurify@*/dist/purify.min.js", lambda r: r.fulfill(body=PURIFY_STUB, content_type="application/javascript"))
    page.reload()
    open_library(page)
    expect(page.locator(f'tr[data-id="{md}"] .lib-preview')).to_have_text("二、计划 正文第一段 ![图](x.png)")
    page.locator(f'tr[data-id="{md}"]').click()
    expect(body.locator("h2")).to_have_text("二、计划")
    expect(body.locator("img")).to_have_count(0)  # 不加载外部图片
    expect(page.locator('[data-preview="rich"]')).to_have_attribute("aria-pressed", "true")
    page.locator('[data-preview="raw"]').click()
    expect(body).to_contain_text("## 二、计划")
    expect(body.locator("h2")).to_have_count(0)
    page.locator('[data-preview="rich"]').click()
    expect(body.locator("h2")).to_have_count(1)
    page.locator("#material-close").click()

    page.locator(f'tr[data-id="{txt}"]').click()
    expect(body).to_contain_text("## 不是标题的纯文本")  # 纯文本不排版
    expect(page.locator("[data-preview]")).to_have_count(0)
