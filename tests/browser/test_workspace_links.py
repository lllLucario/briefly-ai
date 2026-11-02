"""真实浏览器＋隔离后端：工作区与知识库文档的关联——复制到工作区、材料页疑似重复提示与确认、知识库文档页改稿提醒。"""
import time

import pytest
pytest.importorskip("sqlite_vec", reason="知识网页回归需安装.[rag]")
from playwright.sync_api import expect

from .test_knowledge import allow_egress, ask_assistant, create, import_file, open_knowledge
from .test_material_library import base, open_library, rows

pytestmark = pytest.mark.browser

POLICY = ("# 门店值班制度\n## 值班安排\n每天开店前三十分钟由值班主管完成设备检查并在系统中签到。\n"
          "值班期间发生客诉，值班主管须在十五分钟内到场处理并记录。\n节假日值班表由店长在前一周的周五之前发布，加班餐须预订前审批。")


def open_material(page, title):
    open_library(page)
    page.locator("#lib-q").fill(title)
    expect(rows(page)).to_have_count(1)
    expect(rows(page).first).to_contain_text(title)  # 等筛选结果替换掉之前的列表
    rows(page).first.click()
    panel = page.locator("#material-panel")
    expect(panel.locator("#material-title")).to_have_text(title)
    return panel


def test_copy_suggest_confirm_and_document_update_hint(web_page):
    page = web_page
    open_knowledge(page)
    create(page, "运营制度")
    import_file(page, POLICY)
    # 复制到工作区：显式关联，材料页说明外发随知识库授权
    page.locator("[data-kb-copy]").first.click()
    expect(page.locator("#kb-message")).to_contain_text("已把《制度.md》复制到工作区")
    panel = open_material(page, "制度.md")
    expect(panel.locator("[data-link]")).to_contain_text("已关联知识库“运营制度”中的《制度.md》")
    expect(panel.locator("[data-link]")).to_contain_text("这份文件的内容不会发给模型")
    page.locator("#material-close").click()

    # 工作区另有一份改稿：后台检测后材料页提示，用户确认
    revised = POLICY.replace("十五分钟", "十分钟")
    saved = page.request.post(base(page) + "/materials", data={"title": "值班制度修订稿", "content": revised})
    material_id = saved.json()["material"]["id"]
    for _ in range(100):  # 等后台检测写入疑似关联
        if page.request.get(base(page) + f"/materials/{material_id}").json()["links"]:
            break
        time.sleep(0.1)
    panel = open_material(page, "值班制度修订稿")
    hint = panel.locator("[data-link]")
    expect(hint).to_contain_text("看起来是知识库“运营制度”中的《制度.md》（第 1 版）的改稿，要关联吗？")
    hint.locator('[data-link-decide="confirm"]').click()
    expect(panel.locator("[data-link]")).to_contain_text("已关联知识库“运营制度”")
    page.locator("#material-close").click()

    # 知识库文档页：工作区有更新，知识库仍为第 1 版，不自动发布
    open_knowledge(page)
    expect(page.locator("[data-kb-workspace-update]")).to_contain_text("《值班制度修订稿》），知识库仍为第 1 版")
    expect(page.locator("#kb-documents")).to_contain_text("修订 1")


def test_joint_answer_labels_workspace_evidence_and_counts_egress_by_origin(web_page):
    page = web_page
    saved = page.request.post(base(page) + "/materials", data={"title": "我的值班笔记", "content": "周三客诉两起，值班主管都在十分钟左右到场，加班餐须预订前审批。"})
    assert saved.ok
    open_knowledge(page)
    kb = create(page, "运营制度")
    import_file(page, POLICY)
    allow_egress(page)
    result = ask_assistant(page, kb, "值班主管客诉多久到场？").locator(".knowledge-refs")
    expect(result).to_contain_text("个知识库片段和 1 个工作区片段")
    expect(result.locator("[data-chat-material]")).to_contain_text("工作区 · 我的值班笔记")
    expect(result.locator("[data-chat-reference]").first).to_contain_text("知识库 · 制度.md")
    result.locator("[data-chat-material]").click()
    expect(page.locator("#material-title")).to_have_text("我的值班笔记")
