"""真实浏览器中的业务主线；仅由页面操作写入，接口读取用于核对持久化结果。"""
import re

from playwright.sync_api import expect

from .support import MEETING, PROGRESS


def send(page, text):
    page.locator("#input").fill(text)
    with page.expect_response(lambda r: r.url.endswith("/chat") and r.request.method == "POST", timeout=90000) as response:
        page.locator("#send").click()
    assert response.value.ok, response.value.text()
    expect(page.locator("#send")).to_be_enabled(timeout=90000)
    expect(page.locator(".msg.assistant:not(.pending)").last.locator(".msg-actions")).to_be_visible()
    # 交给助理处理的轮次不显示系统的理解（标签只在捷径和直接回复时出现，见 test_intent_visibility.py）
    expect(page.locator(".msg.user").last.locator(".intent-meta")).to_have_count(0)
    return page.locator(".msg.assistant:not(.pending)").last


def start_meeting(page):
    page.locator('[data-tab="chat"]').click()
    page.locator("#chat-add").click()
    page.locator('[data-add="paste"]').click()
    page.locator("#paste-title").fill("支付改造会议（虚构）")
    page.locator("#paste-content").fill(MEETING)
    page.locator("#paste-form").get_by_role("button", name="保存为材料").click()
    expect(page.locator("#add-msg")).to_contain_text("已保存")
    page.locator("#add-dialog [data-close]").click()
    expect(page.locator("#picked")).to_contain_text("支付改造会议")
    first = send(page, "请整理这份会议纪要，明确行动项，不把讨论建议当成已决定的任务。")
    expect(first).to_contain_text("李强")
    expect(first).to_contain_text("赵琳")
    latest = send(page, "现在只保留李强负责的行动项，删去赵琳和竞品建议，保留原始期限。")
    expect(latest).to_contain_text("李强")
    expect(latest).to_contain_text(re.compile(r"2026\s*[-年/]\s*10\s*[-月/]\s*23"))
    return latest


def preview(page, message):
    message.locator("[data-todos]").click()
    dialog = page.locator("dialog[open]")
    expect(dialog.locator("[data-item]")).to_have_count(1, timeout=90000)
    expect(dialog).to_contain_text("李强")
    expect(dialog.get_by_text(re.compile(r"^原文负责人：李强"))).to_be_visible()
    expect(dialog.locator("[data-due]")).to_have_value("2026-10-23")
    assert page.request.get(page.url.rstrip("/")+"/todos").json() == []
    return dialog


def save_candidate(page, dialog):
    dialog.locator("[data-selected]").check()
    dialog.locator("[data-save]").click()
    expect(dialog).to_contain_text("已保存 1 条待办", timeout=90000)
    rows = page.request.get(page.url.rstrip("/")+"/todos").json()
    assert len(rows) == 1 and rows[0]["due_date"] == "2026-10-23", rows
    todo_id = rows[0]["id"]
    dialog.locator("[data-view]").click()
    expect(page.locator("#todo-title")).to_contain_text("测试记录")
    expect(page.locator("#todo-info")).to_contain_text("李强")
    return todo_id


def run_journey(page, screenshot=None):
    latest = start_meeting(page)
    chat_replies = page.locator(".msg.assistant .bubble").all_inner_texts()
    dialog = preview(page, latest)
    # 关闭预览不会保存；重新打开仍能正常确认。
    dialog.locator("[data-close-action]").click()
    assert page.request.get(page.url.rstrip("/")+"/todos").json() == []
    dialog = preview(page, latest)
    todo_id = save_candidate(page, dialog)
    page.locator("#todo-note").fill(PROGRESS)
    page.locator("#todo-note-form button").click()
    expect(page.locator("#todo-msg")).to_contain_text("进展已记录")
    expect(page.locator("#todo-status")).to_have_attribute("data-state", "todo")
    page.reload()
    page.locator('[data-tab="chat"]').click()
    page.locator("#session-list li").first.click()
    latest = page.locator(".msg.assistant:not(.pending)").last
    expect(latest.locator("[data-save-state]")).to_contain_text("已保存 1 条待办")
    expect(latest.locator("[data-todos]")).to_be_disabled()
    latest.locator(".result-more summary").click()
    latest.locator("[data-week]").click()
    dialog = page.locator("dialog[open]")
    expect(dialog.locator("[data-commit]")).to_be_visible()
    expect(dialog).to_contain_text("材料会整份参与正式周报")
    dialog.locator("[data-mid]").check()
    dialog.locator("[data-note]").fill("本周重点：李强整理测试记录，已完成初稿，正在复核异常分支。")
    dialog.locator("[data-commit]").click()
    expect(page.locator("#week-go")).to_have_text("整理本周工作")
    page.locator("#week-go").click()
    expect(page.locator('#week-checklist [data-act="confirm"]')).to_have_count(1)
    expect(page.locator("#week-go")).to_be_disabled()
    row = page.locator("#week-checklist [data-ev]").first
    row.locator("details summary").click()
    # 本周报表述修改不能直接改待办当前状态。
    with page.expect_response(lambda r: "/items/" in r.url and r.request.method == "PUT"):
        row.locator("[data-todo-status]").select_option("doing")
    assert page.request.get(page.url.rstrip("/")+f"/todos/{todo_id}").json()["status"] == "todo"
    row = page.locator("#week-checklist [data-ev]").first
    row.locator('[data-act="confirm"]').click()
    expect(page.locator("#week-go")).to_be_enabled()
    page.locator("#week-go").click()
    expect(page.locator("#weekly-preview")).to_contain_text("测试记录", timeout=90000)
    expect(page.locator("#weekly-preview")).to_contain_text("异常分支")
    expect(page.locator("#week-stage")).to_contain_text("查看与交付")
    body = page.locator("#weekly-preview").inner_text()
    # 栏目本身也会暗示承诺；保留“未决定”不能让未采纳建议混进工作安排。
    assert "竞品分析" not in body, body
    href = page.locator("#export-md").get_attribute("href")
    report_id = int(re.search(r"/weekly/(\d+)", href)[1])
    saved = page.request.get(page.url.rstrip("/")+f"/weekly/{report_id}").json()
    assert saved["status"] == "done" and len(saved["material_ids"]) == 1
    assert any(ref["todo_id"] == todo_id and ref["report_status"] == "doing"
               for ref in saved["inputs"]["todo_refs"])
    assert page.request.get(page.url.rstrip("/")+f"/todos/{todo_id}").json()["status"] == "todo"
    content = page.locator("#weekly-preview").inner_text()
    # 改正文、历史版本和下载均走页面。
    page.locator("#edit-weekly").click()
    editor = page.locator("#weekly-text")
    expect(editor).to_be_visible()
    editor.fill(editor.input_value()+"\n人工核对：初稿已完成。")
    page.locator("#save-weekly").click()
    expect(page.locator("#weekly-preview")).to_contain_text("人工核对")
    with page.expect_download() as download:
        page.locator("#export-md").click()
    exported = download.value.path().read_text(encoding="utf-8")
    assert "人工核对" in exported and not re.search(r"〔(?:E\d+|\d+-\d+)〕", exported)
    page.locator("#versions-weekly").click()
    expect(page.locator("dialog[open]")).to_contain_text("历史版本")
    expect(page.locator("dialog[open]")).to_contain_text("修改")
    page.locator("#source-dialog button").filter(has_text="关闭").click()
    if screenshot:
        page.screenshot(path=str(screenshot), full_page=True)
    return {"todo_id":todo_id,"report_id":report_id,"chat_replies":chat_replies,
            "report":content,"content_with_sources":saved["content"],"export":exported,
            "warnings":saved["generation"].get("warnings", []),
            "reminders":saved["generation"].get("reminders", [])}
