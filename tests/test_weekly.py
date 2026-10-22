from datetime import date
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from briefly.api import create_app
from briefly.db import create_db_engine
from briefly.materials import MaterialNotFound, MaterialStore
from briefly.weekly.engine import current_week, split_reminders, strip_source_tags, unverified_numbers
from briefly.weekly.models import DONE, FAILED, PENDING_GEN
from briefly.weekly.service import WeeklyService
from fakes import FunctionModel, structured
from weekly_util import draft

LOG = "# 周报素材\n## 周三\n定位到支付回调超时的原因。\n## 周四\n支付回调耗时从 1.8 秒降到 200 毫秒。"
REPORT = "【周报标题】2026-10-12 至 2026-10-18\n【本周完成】\n- 支付回调耗时从 1.8 秒降到 200 毫秒〔#1〕\n【下周计划】\n1. 支付上线〔简述〕"


def test_first_settings_read_is_safe_when_requests_race(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy import event

    engine = create_db_engine(f"sqlite:///{(tmp_path / 'settings.db').as_posix()}")
    service = WeeklyService(MaterialStore(engine), model_factory=make_model)
    barrier = Barrier(2)

    def synchronize_inserts(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO weekly_settings"):
            barrier.wait(timeout=10)

    event.listen(engine, "before_cursor_execute", synchronize_inserts)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(lambda _: service.settings(), range(2)))
        assert [row.id for row in rows] == [1, 1]
        assert service.settings().schedule_enabled is False
    finally:
        event.remove(engine, "before_cursor_execute", synchronize_inserts)
        engine.dispose()


def resolved(material_id: int, text: str = REPORT) -> str:
    """模型写的编号〔#1〕换回出处后的周报。"""
    return text.replace("〔#1〕", f"〔{material_id}-1〕")


def make_model(report: str = REPORT, extract=None):
    """提取调用返回原文中的一句；撰写周报的调用返回 report。"""

    def respond(messages):
        system = messages[0].content
        if "撰写周报" in system:
            if isinstance(report, Exception):
                raise report
            return AIMessage(report)
        if extract is not None:
            return extract(messages)
        return structured("Extraction", items=[{"category": "done", "point": "耗时下降", "quote": "支付回调耗时从 1.8 秒降到 200 毫秒。"}])

    return FunctionModel(respond=respond)


@pytest.fixture
def store():
    engine = create_db_engine("sqlite://")
    yield MaterialStore(engine)
    engine.dispose()


@pytest.fixture
def material(store):
    m, _ = store.add_text("周报素材", LOG)
    return m


def service(store, model=None) -> tuple[WeeklyService, FunctionModel]:
    model = model or make_model()
    return WeeklyService(store, lambda: model), model


# ---- 工具函数 ----


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 10, 12), (date(2026, 10, 12), date(2026, 10, 18))),  # 周一
        (date(2026, 10, 18), (date(2026, 10, 12), date(2026, 10, 18))),  # 周日
        (date(2026, 10, 15), (date(2026, 10, 12), date(2026, 10, 18))),
    ],
)
def test_current_week_is_monday_to_sunday(today, expected):
    assert current_week(today) == expected


def test_strip_source_tags():
    assert strip_source_tags("- 完成联调〔1-3〕〔2-5〕\n- 上线〔简述〕") == "- 完成联调\n- 上线"


def test_unverified_numbers_ignore_list_ordinals_and_known_numbers():
    report = "1. 耗时降到 200 毫秒〔1-2〕\n2. 完成率 99%\n3. 周期 2026-10-12"
    assert unverified_numbers(report, "耗时从 1.8 秒降到 200 毫秒 2026-10-12 至 2026-10-18") == ["99"]


# ---- 生成 ----


def test_generate_end_to_end(store, material):
    weekly, model = service(store)
    report = draft(weekly, [material.id], note="主要做支付改造", start=date(2026, 10, 12), end=date(2026, 10, 18))
    assert report.status == PENDING_GEN
    weekly.run_generation(report.id)
    done = weekly.report(report.id)
    assert done.status == DONE and done.content == resolved(material.id)
    [item] = done.items
    assert {k: item[k] for k in ("category", "point", "quote", "source", "needs_review")} == {
        "category": "done",
        "point": "耗时下降",
        "quote": "支付回调耗时从 1.8 秒降到 200 毫秒。",
        "source": f"{material.id}-1",
        "needs_review": False,
    }
    assert item["fact_id"] == f"{material.id}-1.1"  # 稳定的事实编号：材料-块.序号
    assert material.content[item["span_start"] :].startswith(item["quote"])  # 原文位置可回查
    g = done.generation
    assert g["chunks_total"] == g["chunks_processed"] == 1
    assert g["calls"] == 2 and g["unverified_numbers"] == [] and g["warnings"] == []
    request = model.calls[-1][-1].content
    assert "周期：2026-10-12 至 2026-10-18" in request and "用户简述〔简述〕：主要做支付改造" in request
    assert "- #1 【已完成】耗时下降" in request
    assert "【周报标题】" in model.calls[-1][0].content  # 用的是 weekly.md 的格式


def test_split_reminders():
    text = "【本周完成】\n- 完成联调〔1-1〕\n\n【给用户的提醒】\n- 压测报告没有写进度\n• 简述与材料日期不一致\n"
    assert split_reminders(text) == ("【本周完成】\n- 完成联调〔1-1〕", ["压测报告没有写进度", "简述与材料日期不一致"])
    assert split_reminders("只有周报") == ("只有周报", [])


def test_reminders_are_kept_out_of_report_and_export(store, material):
    text = REPORT + "\n\n【给用户的提醒】\n- 材料提到上线前要补齐压测报告，但没写进度，完成率 99% 也请核对"
    weekly, model = service(store, make_model(report=text))
    report = draft(weekly, [material.id], start=date(2026, 10, 12), end=date(2026, 10, 18))
    weekly.run_generation(report.id)
    done = weekly.report(report.id)
    assert done.content == resolved(material.id)
    assert done.generation["reminders"] == ["材料提到上线前要补齐压测报告，但没写进度，完成率 99% 也请核对"]
    assert done.generation["unverified_numbers"] == []  # 提醒里的数字不参与核对
    assert "给用户的提醒" not in weekly.export_markdown(report.id)[1]
    system = model.calls[-1][0].content
    assert "不要自己推断风险" in system and "【给用户的提醒】" in system


def two_items(messages):
    return structured(
        "Extraction",
        items=[
            {"category": "done", "point": "定位超时原因", "quote": "定位到支付回调超时的原因。"},
            {"category": "done", "point": "耗时下降", "quote": "支付回调耗时从 1.8 秒降到 200 毫秒。"},
        ],
    )


def test_items_left_out_of_report_become_reminders(store, material):
    weekly, _ = service(store, make_model(report="【本周完成】\n- 耗时下降〔#2〕", extract=two_items))
    report = draft(weekly, [material.id])
    weekly.run_generation(report.id)
    g = weekly.report(report.id).generation
    assert g["coverage_checked"] is True
    assert [i["point"] for i in g["uncovered"]] == ["定位超时原因"]
    assert f"这一项没有写进周报，如需要请补充：定位超时原因〔{material.id}-1〕" in g["reminders"]


def test_invalid_refs_and_missing_refs_are_warned(store, material):
    weekly, _ = service(store, make_model(report="- 编的〔#7〕\n- 真的〔#1、#2〕", extract=two_items))
    report = draft(weekly, [material.id])
    weekly.run_generation(report.id)
    assert any("不存在的工作项编号 [7]" in w for w in weekly.report(report.id).generation["warnings"])

    weekly, _ = service(store, make_model(report="- 没标编号", extract=two_items))
    report = draft(weekly, [material.id])
    weekly.run_generation(report.id)
    g = weekly.report(report.id).generation
    assert g["coverage_checked"] is False and g["uncovered"] == []
    assert any("未能核对是否漏写" in w for w in g["warnings"])


def test_default_period_is_current_week(store, material):
    weekly, _ = service(store)
    report = draft(weekly, [material.id])
    assert (report.period_start, report.period_end) == current_week()


def test_unverified_numbers_and_failed_chunks_become_warnings(store, monkeypatch):
    monkeypatch.setattr("briefly.config.CHUNK_SIZE", 20)
    monkeypatch.setattr("briefly.config.CHUNK_OVERLAP", 0)
    m, _ = store.add_text("长", LOG)

    def extract(messages):
        if "第 1 块" in messages[-1].content:
            raise ConnectionError("网络错误")
        return structured("Extraction", items=[])

    weekly, _ = service(store, make_model(report="完成率 99%〔简述〕", extract=extract))
    report = draft(weekly, [m.id])
    weekly.run_generation(report.id)
    g = weekly.report(report.id).generation
    assert any(f"材料 {m.id} 第 1 块处理失败" in w for w in g["warnings"])
    assert g["unverified_numbers"] == ["99"]
    assert any("请核对：99" in w for w in g["warnings"])


def test_brief_only_report(store):
    weekly, model = service(store, make_model(report="【本周完成】\n- 修复登录 bug〔简述〕"))
    report = draft(weekly, [], note="修复了登录 bug")
    weekly.run_generation(report.id)
    assert weekly.report(report.id).status == DONE
    assert "没有提取到相关工作项" in model.calls[-1][-1].content


def test_model_error_marks_report_failed(store, material):
    weekly, _ = service(store, make_model(report=RuntimeError("API 欠费")))
    report = draft(weekly, [material.id])
    weekly.run_generation(report.id)
    failed = weekly.report(report.id)
    assert failed.status == FAILED and "API 欠费" in failed.error


def test_material_deleted_before_generation_is_reported(store, material):
    weekly, _ = service(store)
    other, _ = store.add_text("另一份", "整理需求池。")
    report = draft(weekly, [material.id, other.id])
    store.delete(other.id)
    weekly.run_generation(report.id)
    assert f"材料 {other.id} 已被删除，未纳入" in weekly.report(report.id).generation["warnings"]


def test_generation_needs_something_to_write(store):
    weekly, _ = service(store)
    report = weekly.week(date(2026, 10, 12))
    with pytest.raises(ValueError, match="无法生成周报"):
        weekly.start_generation(report.id)
    with pytest.raises(MaterialNotFound, match="材料 99 不存在"):
        weekly.set_materials(report.id, [99])
    assert (report.period_start, report.period_end) == (date(2026, 10, 12), date(2026, 10, 18))


def test_new_service_does_not_interrupt_active_reports(store, material):
    weekly, _ = service(store)
    report = draft(weekly, [material.id])
    restarted, _ = service(store)
    interrupted = restarted.report(report.id)
    assert interrupted.status == PENDING_GEN and not interrupted.error


def test_edit_and_export(store, material):
    weekly, _ = service(store)
    report = draft(weekly, [material.id], start=date(2026, 10, 12), end=date(2026, 10, 18))
    with pytest.raises(ValueError, match="还没有生成完成"):
        weekly.update_content(report.id, "x")
    weekly.run_generation(report.id)
    with pytest.raises(ValueError, match="不能为空"):
        weekly.update_content(report.id, "  ")
    weekly.update_content(report.id, "【本周完成】\n- 改过的内容〔1-1〕")
    filename, text = weekly.export_markdown(report.id)
    assert filename == "周报_2026-10-12_2026-10-18.md"
    assert text == "【本周完成】\n- 改过的内容\n"


# ---- 接口 ----


@pytest.fixture
def client(store):
    with TestClient(create_app(store, lambda: make_model())) as c:
        yield c


def test_weekly_api_flow(client, material):
    week = client.post("/weekly/week", json={"day": "2026-10-14"}).json()
    report_id = week["id"]
    assert (week["period_start"], week["period_end"]) == ("2026-10-12", "2026-10-18")
    client.put(f"/weekly/{report_id}/materials", json={"material_ids": [material.id]})
    client.put(f"/weekly/{report_id}/note", json={"note": "支付改造"})
    created = client.post(f"/weekly/{report_id}/generate", json={})
    assert created.status_code == 202

    detail = client.get(f"/weekly/{report_id}").json()  # 测试客户端会在返回前跑完后台任务
    assert detail["status"] == DONE and detail["content"] == resolved(material.id)
    assert detail["items"][0]["source"] == f"{material.id}-1"
    assert detail["generation"]["chunks_processed"] == 1

    assert [r["id"] for r in client.get("/weekly").json()] == [report_id]

    edited = client.put(f"/weekly/{report_id}/content", json={"content": "- 改过〔1-1〕"})
    assert edited.status_code == 200 and edited.json()["content"] == "- 改过〔1-1〕"

    exported = client.get(f"/weekly/{report_id}/export.md")
    assert exported.status_code == 200 and exported.text == "- 改过\n"
    assert exported.headers["content-type"].startswith("text/markdown")
    assert unquote(exported.headers["content-disposition"].split("''")[1]) == "周报_2026-10-12_2026-10-18.md"

    assert client.delete(f"/weekly/{report_id}").status_code == 204
    assert client.get(f"/weekly/{report_id}").status_code == 404


def test_weekly_api_errors(client):
    week = client.post("/weekly/week", json={}).json()
    assert client.post(f"/weekly/{week['id']}/generate", json={}).status_code == 400
    missing = client.put(f"/weekly/{week['id']}/materials", json={"material_ids": [42]})
    assert missing.status_code == 404 and missing.json()["detail"] == "材料 42 不存在"
    assert client.post("/weekly/week", json={"day": "不是日期"}).status_code == 422
    assert client.get("/weekly/999").json()["detail"] == "周报 999 不存在"
