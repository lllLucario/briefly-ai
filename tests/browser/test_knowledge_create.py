"""知识库空状态、带资料创建、后台上传与批次隔离。"""
import json

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.browser


def start(page):
    page.locator('.tabs [data-tab="knowledge"]').click()
    expect(page.locator("#kb-empty")).to_be_visible()
    page.locator("#kb-empty-create").click()
    expect(page.locator("#kb-create-dialog")).to_be_visible()


def file(name="制度.md"):
    return dict(name=name, mimeType="text/markdown", buffer="# 制度\n正式员工出差须在预订前审批。".encode())


def test_create_needs_files_and_help_explains_default(web_page):
    p = web_page
    start(p)
    p.locator("#kb-name").fill("办公制度")
    expect(p.locator("#kb-create-tokens")).to_have_value("512")
    p.locator(".kb-help summary").click()
    expect(p.locator(".kb-help")).to_contain_text("不等于字数")
    expect(p.locator(".kb-help")).to_contain_text("确认后才建立可检索的索引")
    p.locator("#kb-create").click()
    expect(p.locator("#kb-create-error")).to_contain_text("至少选择一份")
    assert p.request.get(p.url.rstrip("/") + "/knowledge-bases").json() == []
    p.locator("#kb-create-files").set_input_files([file(), file("不支持.exe")])
    expect(p.locator("#kb-create-selection")).to_contain_text("已选 1 份")
    expect(p.locator(".kb-skipped")).to_contain_text("1 份文件格式不支持")
    p.locator("#kb-create-cancel").click()
    expect(p.locator("#kb-empty")).to_be_visible()
    assert p.request.get(p.url.rstrip("/") + "/knowledge-bases").json() == []


def test_create_closes_before_upload_and_keeps_preview_confirmation(web_page):
    p = web_page
    start(p)
    p.locator("#kb-name").fill("办公制度")
    p.locator("#kb-create-files").set_input_files(file())
    held = []
    p.route("**/knowledge-bases/*/imports", lambda route: held.append(route) if route.request.method == "POST" else route.continue_())
    p.locator("#kb-create").click()
    expect(p.locator("#kb-create-dialog")).to_be_hidden()
    expect(p.locator("#kb-select option:checked")).to_have_text("办公制度")
    expect(p.locator("#kb-transfers")).to_contain_text("制度.md")
    expect(p.locator("#kb-transfers progress")).to_have_count(1)
    assert held
    # 等待上传时，页面可以切走；任务不占住弹窗。
    p.locator('.tabs [data-tab="materials"]').click()
    held[0].continue_()
    p.locator('.tabs [data-tab="knowledge"]').click()
    expect(p.locator("#kb-jobs")).to_contain_text("等待核对切片", timeout=30000)
    assert p.request.get(p.url.rstrip("/") + f'/knowledge-bases/{p.locator("#kb-select").input_value()}/documents').json() == []
    p.locator("[data-kb-preview]").click()
    expect(p.locator("#kb-preview-body")).to_contain_text("预订前审批")
    expect(p.locator("#kb-confirm")).to_be_disabled()
    p.locator("#kb-preview-checked").check()
    p.locator("#kb-confirm").click()
    expect(p.locator("#kb-jobs")).to_contain_text("已完成", timeout=30000)
    expect(p.locator("#kb-external")).not_to_be_checked()


def test_folder_splits_batches_and_retry_reuses_request_key(web_page, tmp_path):
    p = web_page
    folder = tmp_path / "企业资料"
    folder.mkdir()
    nested = folder / "子文件夹"
    nested.mkdir()
    for i in range(17):
        (nested / f"制度{i}.md").write_text("原创虚构制度。", encoding="utf-8")
    (folder / "跳过.exe").write_text("不支持", encoding="utf-8")
    start(p)
    p.locator("#kb-name").fill("文件夹资料")
    p.locator("#kb-create-folder").set_input_files(str(folder))
    expect(p.locator("#kb-create-selection")).to_contain_text("已选 17 份")
    expect(p.locator("#kb-create-selection")).to_contain_text("子文件夹")
    requests = []
    def upload(route):
        if route.request.method != "POST":
            return route.continue_()
        requests.append(route.request.post_data_buffer)
        if len(requests) == 1:
            route.fulfill(status=503, content_type="application/json", body='{"detail":"上传暂不可用"}')
        else:
            route.fulfill(status=202, content_type="application/json", body='{"files":[]}')
    p.route("**/knowledge-bases/*/imports", upload)
    p.locator("#kb-create").click()
    expect(p.locator("#kb-transfers")).to_contain_text("上传暂不可用")
    expect(p.locator("#kb-transfers progress")).to_have_count(0)
    assert len(requests) == 2
    assert requests[0].count(b'name="files"') == 16
    assert requests[1].count(b'name="files"') == 1
    assert b"512" in requests[0]
    p.locator("[data-kb-transfer-retry]").click()
    expect(p.locator("#kb-transfers")).to_be_empty()
    assert requests[0].split(b'name="request_key"')[1].split(b'\r\n')[2] == requests[2].split(b'name="request_key"')[1].split(b'\r\n')[2]


def test_failed_create_preserves_input_and_selection(web_page):
    p = web_page
    start(p)
    p.locator("#kb-name").fill("待创建资料库")
    p.locator("#kb-create-files").set_input_files(file())
    p.route("**/knowledge-bases", lambda route: route.fulfill(status=503, content_type="application/json", body=json.dumps({"detail":"暂时不可用"})) if route.request.method == "POST" else route.continue_())
    p.locator("#kb-create").click()
    expect(p.locator("#kb-create-dialog")).to_be_hidden()
    expect(p.locator("#kb-create-progress")).to_contain_text("暂时不可用")
    p.locator("#kb-create-edit").click()
    expect(p.locator("#kb-name")).to_have_value("待创建资料库")
    expect(p.locator("#kb-create-selection")).to_contain_text("制度.md")


@pytest.mark.parametrize("width", [320, 1440])
def test_create_dialog_fits_and_does_not_change_other_pages(web_page, width):
    p = web_page
    p.set_viewport_size(dict(width=width, height=1000))
    start(p)
    p.locator(".kb-help summary").click()
    assert p.locator("#kb-create-dialog").evaluate("e => e.scrollWidth <= e.clientWidth")
    assert p.locator("body").evaluate("e => e.scrollWidth <= innerWidth")
    p.locator("#kb-create-close").click()
    for tab in ["todos", "chat", "materials", "weekly", "knowledge"]:
        p.locator(f'.tabs [data-tab="{tab}"]').click()
        expect(p.locator(f"#tab-{tab}")).to_be_visible()
