"""知识网页演示只用此处原创虚构资料；由journey_browser统一控制真实模型。"""
from pathlib import Path

from playwright.sync_api import expect

SCENARIOS = (
    ("原创办公制度", "制度.md", "# 原创虚构制度\n正式员工国内出差在上海住宿上限为每晚650元，按实际发票报销，不适用于实习生和境外差旅。", "上海住宿上限和适用对象是什么？", ("650", "正式员工")),
    ("原创项目资料", "项目.md", "# 原创虚构项目\n陈禾负责接口联调，截止2026-11-03；发布前必须完成回归并由林遥签字。", "联调由谁负责、何时截止？", ("陈禾", "2026-11-03")),
    ("原创产品手册", "手册.md", "# 原创虚构手册\n工具3.0的E403表示没有导出权限，应联系管理员开通导出权限后重试；本手册未规定企业定价。", "E403怎么处理？", ("权限", "管理员")),
)


def run_knowledge_journey(page, *, screenshot: Path):
    page.locator('.tabs [data-tab="knowledge"]').click()
    cases = []
    for index, (name, filename, content, question, required) in enumerate(SCENARIOS, 1):
        page.locator("#kb-name").fill(name)
        page.locator("#kb-create").click()
        expect(page.locator("#kb-select option:checked")).to_have_text(name)
        page.locator("#kb-files").set_input_files(dict(name=filename, mimeType="text/markdown", buffer=content.encode()))
        page.locator("#kb-upload").click()
        expect(page.locator("#kb-jobs")).to_contain_text("等待核对切片", timeout=30000)
        page.locator("[data-kb-preview]").click()
        expect(page.locator("#kb-preview-body")).to_contain_text(content.split("\n", 1)[1])
        page.locator("#kb-preview-checked").check()
        page.locator("#kb-confirm").click()
        expect(page.locator("#kb-jobs")).to_contain_text("已完成", timeout=30000)
        page.locator("#kb-question").fill(question)
        page.locator("#kb-ask").click()
        expect(page.locator("#kb-result")).to_contain_text("等待外发确认")
        page.locator("#kb-external").check()
        page.locator("#kb-ask").click()
        expect(page.locator("#kb-ask")).to_be_enabled(timeout=90000)
        result = page.locator("#kb-result").inner_text()
        assert "草稿回答" in result, result
        assert all(value in result for value in required), result
        page.locator("[data-kb-reference]").first.click()
        expect(page.locator("#kb-source-text")).to_contain_text(content.split("\n", 1)[1])
        expect(page.locator("#kb-source-info")).to_contain_text("当前有效版本")
        page.locator("#kb-source-close").click()
        image = screenshot.with_name(f"{screenshot.stem}-scenario-{index}.png")
        page.screenshot(path=str(image), full_page=True)
        cases.append(dict(scenario=name, answer=result, source_matches=True, screenshot=image.name))
    return dict(knowledge_scenarios=cases)
