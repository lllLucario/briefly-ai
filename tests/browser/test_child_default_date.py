"""新增子待办按设备本地时间默认日期，覆盖切点、跨年及手动覆盖。"""
import pytest
from playwright.sync_api import expect

from .test_inline_children import setup_parent

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("instant,expected", [
    ("2026-10-21T15:29:59+11:00", "2026-10-21"),
    ("2026-10-21T15:30:00+11:00", "2026-10-22"),
    ("2026-10-21T00:05:00+11:00", "2026-10-21"),
    ("2026-12-31T23:59:00+11:00", "2027-01-01"),
])
def test_default_date_uses_local_cutoff(web_page, instant, expected):
    page = web_page
    page.clock.set_fixed_time(instant)
    base, parent, row = setup_parent(page)
    form = row.locator("[data-inline-add]")
    expect(form.locator('[name="due"]')).to_have_value(expected)
    button = form.get_by_role("button", name="添加子待办", exact=True)
    icon = button.locator("svg").bounding_box()
    status_icon = row.locator("summary .status-symbol").bounding_box()
    title = form.locator('[name="title"]').bounding_box()
    hit = button.bounding_box()
    assert icon["width"] == status_icon["width"] == 22
    assert abs(icon["x"] + icon["width"] / 2 - hit["x"] - hit["width"] / 2) < 0.5
    assert abs(icon["y"] + icon["height"] / 2 - title["y"] - title["height"] / 2) < 0.5
    expect(button.locator("circle")).to_have_attribute("r", "9")
    expect(button.locator("path")).to_have_attribute("d", "M12 7v10M7 12h10")
    form.locator('[name="title"]').fill("本地默认日期任务")
    form.locator('[name="title"]').press("Enter")
    expect(row.locator(".inline-children > li")).to_have_count(1)
    children = page.request.get(base + f"/todos/{parent['id']}").json()["children"]
    assert children[0]["due_date"] == expected and children[0]["status"] == "todo"
    expect(form.locator('[name="due"]')).to_have_value(expected)


def test_pristine_date_refreshes_but_manual_date_survives(web_page):
    page = web_page
    page.clock.set_fixed_time("2026-10-21T15:29:00+11:00")
    base, parent, row = setup_parent(page)
    form = row.locator("[data-inline-add]")
    expect(form.locator('[name="due"]')).to_have_value("2026-10-21")
    page.clock.set_fixed_time("2026-10-21T15:31:00+11:00")
    form.locator('[name="title"]').focus()
    expect(form.locator('[name="due"]')).to_have_value("2026-10-22")
    form.locator('[name="title"]').fill("保留手动日期")
    form.locator('[name="due"]').fill("2026-11-06")
    # 另一项任务的状态保存会重绘整张列表，不能覆盖当前草稿的日期。
    other = page.locator('#todo-list > li').nth(1)
    other.locator("summary > .ticket-status").click()
    page.locator('[data-status-choice="doing"]').click()
    expect(other.locator("summary > .ticket-status")).to_have_attribute("aria-label", "更改独立的第二项任务的状态：进行中")
    expect(form.locator('[name="due"]')).to_have_value("2026-11-06")
    form.locator('[name="title"]').press("Enter")
    expect(row.locator(".inline-children > li")).to_have_count(1)
    assert page.request.get(base + f"/todos/{parent['id']}").json()["children"][0]["due_date"] == "2026-11-06"
