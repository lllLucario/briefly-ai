"""材料选择器与跨页添加：搜索/筛选保留已选、取消不写入、已有项不可重复；上传途中切换任务不误关联。隔离数据、假模型。"""
from datetime import date

import pytest
from playwright.sync_api import expect

from .test_material_library import add, base, link, todo

pytestmark = pytest.mark.browser


def test_todo_picker_filters_keep_selection_and_cancel_does_not_write(web_page):
    page = web_page
    target = todo(page, "整理接口文档")
    other = todo(page, "季度复盘")
    linked = add(page, "已关联的说明")
    a = add(page, "接口说明", "第一版：支付回调字段。")
    add(page, "接口说明", "第二版：退款流程补充。")
    link(page, target, linked)
    link(page, other, a)
    page.reload()
    page.evaluate(f"openTodo({target})")
    expect(page.locator("#todo-title")).to_have_text("整理接口文档")
    page.locator("#todo-material-section > summary").click()

    page.locator("#todo-pick").click()
    picker = page.locator("#mat-picker")
    expect(picker).to_be_visible()
    expect(picker.locator(f'input[value="{linked}"]')).to_be_disabled()
    expect(picker.locator(f'input[value="{linked}"]')).to_be_checked()
    row = picker.locator("label.pick-row").filter(has_text="第一版")
    expect(row).to_contain_text("粘贴文字")
    expect(row).to_contain_text("待办：季度复盘")
    row.locator("input").check()
    picker.locator("#mat-picker-q").fill("不存在")
    expect(picker).to_contain_text("没有匹配的材料")
    picker.locator("#mat-picker-q").fill("")
    picker.locator("#mat-picker-source").select_option("feishu")
    expect(picker.locator("label.pick-row")).to_have_count(0)
    picker.locator("#mat-picker-source").select_option("all")
    expect(picker.locator(f'input[value="{a}"]')).to_be_checked()  # 隐藏后再显示，选择没丢
    expect(picker.locator("#mat-picker-count")).to_have_text("已选 1 份")
    picker.locator("[data-close]").click()  # 取消
    assert len(page.request.get(base(page) + f"/todos/{target}/materials").json()) == 1

    expect(picker).to_be_hidden()
    page.locator("#todo-pick").click()
    expect(picker).to_be_visible()
    expect(picker.locator(f'input[value="{a}"]')).not_to_be_checked()  # 重新打开不带上次取消的勾选
    picker.locator(f'input[value="{a}"]').check()
    picker.locator("#mat-picker-done").click()
    expect(page.locator("#todo-msg")).to_contain_text("已关联 1 份")
    ids = {m["material_id"] for m in page.request.get(base(page) + f"/todos/{target}/materials").json()}
    assert ids == {linked, a}


def test_assistant_picker_search_keeps_selection(web_page):
    page = web_page
    first = add(page, "会议纪要（虚构）")
    add(page, "接口说明（虚构）")
    page.locator('.tabs [data-tab="chat"]').click()
    page.locator("#pick-materials").click()
    dialog = page.locator("#picker")
    dialog.locator(f'input[value="{first}"]').check()
    dialog.locator("#picker-q").fill("接口")
    expect(dialog.locator("label.pick-row")).to_have_count(1)
    expect(dialog.locator("label.pick-row")).to_contain_text("粘贴文字 · 固定副本")
    dialog.locator("#picker-q").fill("")
    expect(dialog.locator(f'input[value="{first}"]')).to_be_checked()
    dialog.locator("#picker-done").click()
    expect(page.locator("#picked")).to_contain_text("会议纪要（虚构）")


def test_upload_finishing_after_switching_task_links_to_original_task(web_page):
    page = web_page
    first = todo(page, "第一项任务")
    second = todo(page, "第二项任务")
    page.reload()
    held = []
    page.route(f"**/todos/{first}/materials/upload", lambda route: held.append(route))
    page.evaluate(f"openTodo({first})")
    expect(page.locator("#todo-title")).to_have_text("第一项任务")
    page.locator("#todo-upload").set_input_files({"name": "记录（虚构）.md", "mimeType": "text/markdown", "buffer": "虚构记录".encode()})
    page.wait_for_timeout(300)
    assert held
    page.evaluate(f"openTodo({second})")
    expect(page.locator("#todo-title")).to_have_text("第二项任务")
    held[0].continue_()
    expect(page.locator("#todo-load-msg")).to_contain_text("第一项任务")
    expect(page.locator("#todo-title")).to_have_text("第二项任务")  # 没被旧响应切回
    assert len(page.request.get(base(page) + f"/todos/{first}/materials").json()) == 1
    assert page.request.get(base(page) + f"/todos/{second}/materials").json() == []


def test_assistant_add_stays_in_place_and_weekly_picker_keeps_suggestions(web_page):
    page = web_page
    page.locator('.tabs [data-tab="chat"]').click()
    page.locator("#chat-add").click()
    page.locator('[data-add="paste"]').click()
    page.locator("#paste-title").fill("助理用的纪要（虚构）")
    page.locator("#paste-content").fill("虚构纪要：确认上线窗口。")
    page.locator("#paste-form").get_by_role("button", name="保存为材料").click()
    expect(page.locator("#add-msg")).to_contain_text("已保存")
    page.locator("#add-dialog [data-close]").click()
    expect(page.locator("#tab-chat")).to_be_visible()  # 不自动跳到材料库
    expect(page.locator("#picked")).to_contain_text("助理用的纪要")
    page.locator("#chat-add").click()
    page.locator('[data-add="paste"]').click()
    page.locator("#paste-content").fill("虚构纪要：确认上线窗口。")  # 相同内容：复用已有材料
    page.locator("#paste-form").get_by_role("button", name="保存为材料").click()
    expect(page.locator("#add-msg")).to_contain_text("已存在")
    page.locator("[data-import-library]").click()
    expect(page.locator("#lib-batch")).to_contain_text("本次添加的 1 份")
    expect(page.locator("#material-rows")).to_contain_text("助理用的纪要（虚构）")
    page.locator("#lib-batch-leave").click()

    task = todo(page, "本周任务")
    linked = add(page, "任务材料（虚构）")
    link(page, task, linked)
    page.request.post(base(page) + "/weekly/prepare", data={"day": date.today().isoformat()})  # 审核清单里有这个待办才给建议
    page.locator('.tabs [data-tab="weekly"]').click()
    page.locator("#week-pick").click()
    picker = page.locator("#mat-picker")
    expect(picker).to_be_visible()
    row = picker.locator("label.pick-row").filter(has_text="任务材料（虚构）")
    expect(row).to_contain_text("本周待办的材料")
    expect(row).to_contain_text("待办：本周任务")
    row.locator("input").check()
    picker.locator("#mat-picker-tracking").select_option("tracked")
    expect(picker.locator("label.pick-row")).to_have_count(0)
    picker.locator("#mat-picker-tracking").select_option("all")
    picker.locator("#mat-picker-done").click()
    expect(page.locator("#week-materials")).to_contain_text("任务材料（虚构）")
    page.locator("#week-pick").click()
    expect(picker).to_be_visible()
    expect(picker.locator(f'input[value="{linked}"]')).to_be_disabled()
