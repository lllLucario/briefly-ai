"""卡片原始说明默认可见，用户选择后才追加进展。"""
import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("width", [1440, 320])
def test_description_visible_and_progress_opt_in(web_page, width):
    p = web_page
    p.set_viewport_size({"width": width, "height": 1000})
    base = p.url.rstrip("/")
    text = "保留原始负责人和期限，不把推测当事实。\n" * 12 + "最后一条原始说明"
    task = p.request.post(base + "/todos", data={"title": "核对原始任务", "description": text}).json()
    p.reload()
    row = p.locator(f'[data-todo="{task["id"]}"]')
    row.locator("summary.todo-ticket-row").click()
    description = row.locator(".ticket-description")
    expect(description).to_be_visible()
    assert description.inner_text() == text
    assert description.evaluate("e=>e.scrollHeight>e.clientHeight")
    expect(row.locator("[data-description-toggle]")).to_have_attribute("aria-expanded", "false")
    row.locator("[data-description-toggle]").click()
    assert description.evaluate("e=>e.scrollHeight<=e.clientHeight+1")
    form = row.locator("[data-inline-note]")
    expect(form).not_to_be_visible()
    expect(form.locator("textarea")).to_be_disabled()
    row.locator("[data-note-toggle]").click()
    form.locator("textarea").fill("这是一份尚未保存的进展")
    # 收起/重开不提交，也不丢草稿。
    row.locator("[data-note-toggle]").click()
    expect(form).not_to_be_visible()
    row.locator("[data-note-toggle]").click()
    expect(form.locator("textarea")).to_have_value("这是一份尚未保存的进展")
    assert p.request.get(base + f'/todos/{task["id"]}/timeline').json()[-1]["event_type"] == "created"
    form.locator('[type="submit"]').click()
    expect(row.locator(".ticket-subtitle")).to_contain_text("尚未保存的进展")
    expect(form).not_to_be_visible()
    assert p.request.get(base + f'/todos/{task["id"]}').json()["status"] == "todo"
    expect(description).to_be_visible()
    expect(row.locator("[data-description-toggle]")).to_have_attribute("aria-expanded", "true")
    assert p.locator("body").evaluate("e=>e.scrollWidth<=innerWidth")


def test_failed_append_keeps_open_draft_and_explicit_cancel_discards(web_page):
    p = web_page
    base = p.url.rstrip("/")
    task = p.request.post(base + "/todos", data={"title": "短说明任务", "description": "核对付款信息。"}).json()
    p.reload()
    row = p.locator(f'[data-todo="{task["id"]}"]')
    row.locator("summary.todo-ticket-row").click()
    expect(row.locator(".ticket-description")).to_have_text("核对付款信息。")
    expect(row.locator("[data-description-toggle]")).to_have_count(0)
    row.locator("[data-note-toggle]").click()
    form = row.locator("[data-inline-note]")
    form.locator("textarea").fill("失败后仍需保留的进展")
    held = []
    p.route(f'**/todos/{task["id"]}/notes', lambda r: held.append(r))
    form.locator('[type="submit"]').click()
    expect(form.locator("textarea")).to_be_disabled()
    assert len(held) == 1
    held[0].fulfill(status=503, json={"detail": "模拟追加失败"})
    expect(row.locator("[data-inline-notice]")).to_contain_text("模拟追加失败")
    expect(form).to_be_visible()
    expect(form.locator("textarea")).to_be_enabled()
    expect(form.locator("textarea")).to_have_value("失败后仍需保留的进展")
    row.locator("[data-note-cancel]").click()
    expect(form).not_to_be_visible()
    row.locator("[data-note-toggle]").click()
    expect(form.locator("textarea")).to_have_value("")
