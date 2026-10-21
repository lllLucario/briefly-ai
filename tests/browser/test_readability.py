"""高密度内容的渐进显示、选择边界和跨对象隔离。"""
import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("width", [1440, 320])
def test_many_tasks_progress_history_and_material_entry(web_page, width):
    p = web_page
    p.set_viewport_size({"width": width, "height": 1000})
    base = p.url.rstrip("/")
    tasks = [p.request.post(base + "/todos", data={"title": f"任务{i}", "due_date": "2020-01-01"}).json() for i in range(20)]
    first = tasks[0]["id"]
    for i in range(30):
        assert p.request.post(base + f"/todos/{first}/notes", data={"text": f"原始进展{i}"}).ok
    p.reload()
    row = p.locator(f'[data-todo="{first}"]')
    expect(row.locator(".ticket-subtitle")).to_contain_text("原始进展29")
    expect(row.locator(".ticket-due")).to_contain_text("已逾期")
    row.locator("summary.todo-ticket-row").click()
    row.locator("[data-inline-note] input").fill("等待业务确认，不代表完成")
    row.locator("[data-inline-note] button").click()
    expect(row.locator(".ticket-subtitle")).to_contain_text("等待业务确认")
    assert p.request.get(base + f"/todos/{first}").json()["status"] == "todo"
    row.locator("[data-inline-detail]").click()
    expect(p.locator("#todo-history-section")).not_to_have_attribute("open", "")
    p.locator("#todo-history-section > summary").click()
    expect(p.locator("#todo-timeline .tl")).to_have_count(5)
    p.locator("[data-more-activities]").click()
    expect(p.locator("#todo-timeline .tl")).to_have_count(32)
    p.locator("#todo-close").click()
    second = p.locator(f'[data-todo="{tasks[1]["id"]}"]')
    second.locator("summary.todo-ticket-row").click()
    second.locator("[data-inline-detail]").click()
    expect(p.locator("#todo-history-section")).not_to_have_attribute("open", "")
    p.locator("#todo-close").click()
    p.locator('[data-tab="materials"]').click()
    p.locator("#open-add").click()
    expect(p.locator("#cloud-intake")).not_to_be_visible()
    p.locator('[data-add="paste"]').click()
    p.locator("#paste-content").fill("虚构的原始材料")
    p.locator("#paste-form button").click()
    expect(p.locator("#import-next")).to_be_visible()
    p.locator("[data-import-todo]").click()
    p.locator('[data-ui="0"]').select_option(str(first))
    p.locator("#ui-ok").click()
    expect(p.locator("#todo-materials")).to_contain_text("虚构的原始材料")
    assert p.locator("body").evaluate("e=>e.scrollWidth<=innerWidth")


def test_thirty_review_records_fold_without_implicit_review(web_page):
    p = web_page
    base = p.url.rstrip("/")
    for i in range(30):
        assert p.request.post(base + "/todos", data={"title": f"工作{i}"}).ok
    r = p.request.post(base + "/weekly/prepare", data={}).json()
    ids = [x["id"] for x in r["checklist_items"]]
    assert len(ids) == 30
    p.request.post(base + f'/weekly/{r["id"]}/review', data={"action": "confirm", "ids": ids[:10]})
    p.request.post(base + f'/weekly/{r["id"]}/review', data={"action": "exclude", "ids": ids[10:20]})
    p.reload()
    p.locator('[data-tab="weekly"]').click()
    pending = p.locator('[data-review-group="PENDING"]')
    confirmed = p.locator('[data-review-group="CONFIRMED"]')
    rejected = p.locator('[data-review-group="REJECTED"]')
    expect(pending).to_have_attribute("open", "")
    expect(confirmed).not_to_have_attribute("open", "")
    expect(rejected).not_to_have_attribute("open", "")
    pending.locator("[data-pick]").first.check()
    pending.locator(":scope > summary").click()
    expect(p.locator(".prep-actions")).to_contain_text("已选 1 条")
    assert p.request.get(base + f'/weekly/{r["id"]}').json()["counts"]["PENDING"] == 10
    p.locator("#bulk-exclude").click()
    expect(p.locator("#week-counts")).to_contain_text("待确认 9")
    expect(pending).not_to_have_attribute("open", "")
    expect(p.locator("#week-go")).to_be_disabled()
    confirmed.locator(":scope > summary").click()
    confirmed.locator('[data-act="reset"]').first.click()
    expect(p.locator("#week-counts")).to_contain_text("待确认 10")


def test_long_answer_navigation_keeps_complete_original_text(web_page):
    p = web_page
    p.locator('[data-tab="chat"]').click()
    text = "【议题与结论】\n" + "完整原始结论。" * 60 + "\n【行动项】\n负责人未提及，期限待定。\n【遗留问题】\n尚需核对。"
    p.evaluate("text=>appendMessage({role:'assistant',text})", text)
    expect(p.locator(".answer-nav button")).to_have_count(3)
    assert p.locator(".msg.assistant .bubble").inner_text() == text
    p.locator(".answer-nav button").filter(has_text="行动项").click()
    expect(p.locator(".answer-heading").filter(has_text="行动项")).to_be_focused()
