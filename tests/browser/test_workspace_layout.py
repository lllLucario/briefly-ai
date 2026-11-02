"""全局去重标题、知识库管理层级与设置归属。"""
import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("width", [320, 1440])
def test_compact_pages_and_library_hierarchy(web_page, width):
    p = web_page
    p.set_viewport_size(dict(width=width, height=1000))
    base = p.url.rstrip("/")
    for name in ("制度库", "项目资料"):
        assert p.request.post(base + "/knowledge-bases", data={"name":name}).ok
    p.reload()
    expect(p.locator(".workspace-heading, #page-title, #page-description")).to_have_count(0)
    for tab, label in (("todos","待办"),("chat","助理"),("materials","材料"),("weekly","周报"),("knowledge","知识库")):
        p.locator(f'.tabs [data-tab="{tab}"]').click()
        expect(p).to_have_title(f"{label} · Briefly AI")
        expect(p.get_by_role("main", name=label, exact=True)).to_be_visible()
        first = p.locator(f"#tab-{tab} > section, #tab-{tab} > aside").first.bounding_box()
        nav = p.locator(".topbar").bounding_box()
        assert 0 <= first["y"] - nav["y"] - nav["height"] <= 24, tab
        assert p.locator("body").evaluate("e => e.scrollWidth <= innerWidth"), tab
    region = p.get_by_role("region", name="制度库", exact=True)
    expect(region).to_be_visible()
    expect(p.get_by_role("heading", name="我的知识库", exact=True)).to_have_count(0)
    for selector in ("#kb-open-upload", "#kb-to-chat", "#kb-open-settings", ".kb-explorer", ".kb-settings"):
        expect(region.locator(selector)).to_have_count(1)
    expect(region.locator("#kb-open-create")).to_have_count(0)
    card = p.locator(".kb-base-card").first.bounding_box()
    assert card["height"] <= 64
    create = p.locator("#kb-open-create").bounding_box()
    if width == 1440:
        assert card["x"] + card["width"] < create["x"]
        assert abs(card["y"] + card["height"]/2 - create["y"] - create["height"]/2) < 2
    assert region.bounding_box()["y"] >= p.locator("#kb-library-bar").bounding_box()["y"] + p.locator("#kb-library-bar").bounding_box()["height"]


def test_library_settings_stay_with_selected_library(web_page):
    p = web_page
    base = p.url.rstrip("/")
    ids = [p.request.post(base + "/knowledge-bases", data={"name":name}).json()["id"] for name in ("制度库", "项目库")]
    p.reload()
    p.locator('[data-tab="knowledge"]').click()
    p.locator("#kb-open-settings").click()
    expect(p.locator("#kb-open-settings")).to_have_attribute("aria-expanded", "true")
    expect(p.locator("#kb-external")).to_be_visible()
    expect(p.locator("#kb-external")).not_to_be_checked()
    p.locator("#kb-question").fill("制度库的问题草稿")
    with p.expect_request(lambda request: request.url.endswith(f"/{ids[0]}/egress") and request.method == "PUT"):
        p.locator("#kb-external").check()
    expect(p.locator("#kb-message")).to_contain_text("已允许")
    p.locator(f'[data-kb-base="{ids[1]}"]').click()
    expect(p.locator("#kb-current-name")).to_have_text("项目库")
    expect(p.locator("#kb-external")).not_to_be_checked()
    expect(p.locator("#kb-question")).to_have_value("")
    p.locator(f'[data-kb-base="{ids[0]}"]').click()
    expect(p.locator("#kb-external")).to_be_checked()
    expect(p.locator("#kb-question")).to_have_value("制度库的问题草稿")
    p.locator("#kb-open-settings").click()
    expect(p.locator("#kb-open-settings")).to_have_attribute("aria-expanded", "false")
