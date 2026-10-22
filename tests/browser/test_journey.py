import pytest

pytestmark = pytest.mark.browser


def test_candidate_separate_evidence_warns_before_explicit_selection(web_page):
    from .flows import send, preview
    from playwright.sync_api import expect
    page = web_page
    page.locator('[data-tab="chat"]').click()
    answer = send(page, "提取待办：整理测试记录。\n负责人另处依据：李强负责测试。\n期限另处依据：截止2026-10-23。")
    dialog = preview(page, answer)
    expect(dialog.locator("[data-item='0'] .notice")).to_contain_text("依据与任务原话不在同一处")
    expect(dialog.locator("[data-selected]")).not_to_be_checked()
    expect(dialog.locator("[data-description]")).to_have_value(__import__('re').compile("李强负责测试.*截止2026-10-23", __import__('re').S))
    assert page.request.get(page.url.rstrip("/") + "/todos").json() == []


def test_failed_chat_history_is_visible_after_reload(web_page):
    from playwright.sync_api import expect
    page = web_page
    page.locator('[data-tab="chat"]').click()
    page.locator("#input").fill("模拟失败")
    page.locator("#send").click()
    expect(page.locator(".msg.assistant").last).to_contain_text("出错了")
    page.reload()
    page.locator('[data-tab="chat"]').click()
    page.locator("#session-list li").filter(has_text="模拟失败").click()
    expect(page.locator(".msg.assistant").last).to_contain_text("本轮调用失败，未生成回答")
    assert page.locator(".msg.user").count() == 1


@pytest.mark.parametrize("prompt,warning", [("证据缺口：整理当前输入", "本次回答基于部分内容"), ("虚报成功：保存待办", "操作未执行")])
def test_reliability_warning_is_visible_after_reload_without_business_write(web_page, prompt, warning):
    from .flows import send
    from playwright.sync_api import expect
    page = web_page
    page.locator('[data-tab="chat"]').click()
    latest = send(page, prompt)
    expect(latest.locator(".notice")).to_contain_text(warning)
    page.reload()
    page.locator('[data-tab="chat"]').click()
    page.locator("#session-list li").filter(has_text=prompt).click()
    expect(page.locator(".msg.assistant").last.locator(".notice")).to_contain_text(warning)
    assert page.request.get(page.url.rstrip("/") + "/todos").json() == []


def test_meeting_to_todo_progress_weekly_and_export(web_page):
    from .flows import run_journey
    run_journey(web_page)


def test_save_network_failure_keeps_preview_and_retry_does_not_duplicate(web_page):
    from .flows import preview, start_meeting
    page = web_page
    dialog = preview(page, start_meeting(page))
    dialog.locator("[data-selected]").check()
    pattern = "**/todo-import/*"
    def fail_after_save(route):
        if route.request.method == "POST":
            # 后端已提交但客户端未收到回执，重试必须复用幂等结果。
            response = route.fetch()
            assert response.ok
            route.abort()
        else:
            route.continue_()
    page.route(pattern, fail_after_save)
    dialog.locator("[data-save]").click()
    from playwright.sync_api import expect
    expect(dialog.locator("[data-error]")).to_be_visible()
    expect(dialog.locator("[data-save]")).to_be_enabled()
    page.unroute(pattern, fail_after_save)
    dialog.locator("[data-save]").click()
    expect(dialog).to_contain_text("已保存 1 条待办")
    assert len(page.request.get(page.url.rstrip("/")+"/todos").json()) == 1


def test_model_failure_and_warning_recover_without_auto_resend(web_page):
    from .flows import send
    page = web_page
    page.locator('[data-tab="chat"]').click()
    from playwright.sync_api import expect
    page.locator("#input").fill("模拟失败")
    page.locator("#send").click()
    expect(page.locator(".msg.assistant").last).to_contain_text("出错了")
    expect(page.locator("#input")).to_have_value("模拟失败")
    latest = send(page, "异常提醒：请总结支付联调情况")
    expect(latest.locator(".notice")).to_contain_text("核对")
    requests = []
    page.on("request", lambda r: requests.append(r.url) if r.method == "POST" and r.url.endswith("/chat") else None)
    latest.locator("[data-retry]").click()
    expect(page.locator("#input")).to_have_value("异常提醒：请总结支付联调情况")
    assert requests == []


def test_week_load_disables_actions_and_failure_can_be_retried(web_page):
    from playwright.sync_api import expect
    page = web_page
    def unavailable(route):
        route.fulfill(status=503, json={"detail":"测试：本周暂时不可读"})
    page.route("**/weekly/week", unavailable)
    page.locator('[data-tab="weekly"]').click()
    expect(page.locator("#week-msg")).to_contain_text("加载周报失败")
    expect(page.locator("#week-go")).to_be_disabled()
    expect(page.locator("#week-panel")).to_have_attribute("inert", "")
    page.unroute("**/weekly/week", unavailable)
    page.locator('[data-tab="weekly"]').click()
    expect(page.locator("#week-go")).to_be_enabled()
    expect(page.locator("#week-panel")).not_to_have_attribute("inert", "")


def test_late_week_response_cannot_replace_new_selection(web_page):
    from playwright.sync_api import expect
    page = web_page
    base = page.url.rstrip("/")
    old = page.request.post(base+"/weekly/week", data={"day":"2026-10-05"}).json()
    new = page.request.post(base+"/weekly/week", data={"day":"2026-09-28"}).json()
    page.locator('[data-tab="weekly"]').click()
    expect(page.locator("#week-go")).to_be_enabled()
    held = []
    page.route(f"**/weekly/{old['id']}", lambda route: held.append(route))
    with page.expect_request(f"**/weekly/{old['id']}"):
        page.locator(f"#weekly-list [data-id='{old['id']}']").click()
    expect(page.locator("#week-go")).to_be_disabled()
    page.locator(f"#weekly-list [data-id='{new['id']}']").click()
    expect(page.locator("#week-title")).to_contain_text("09.28")
    with page.expect_response(f"**/weekly/{old['id']}"):
        held[0].continue_()
    page.wait_for_load_state("networkidle")
    expect(page.locator("#week-title")).to_contain_text("09.28")


def test_mobile_core_journey(web_page):
    from .flows import run_journey
    web_page.set_viewport_size({"width":390,"height":844})
    run_journey(web_page)
    assert web_page.locator("body").evaluate("el => el.scrollWidth <= window.innerWidth")
