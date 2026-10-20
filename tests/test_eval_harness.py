"""实测脚本本身的测试：用假模型验证检查规则和运行流程，不调用真实模型。"""
import sys
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import AIMessage

sys.path.insert(0, str(Path(__file__).parent / "eval"))

from cases import CASES, Case, Turn, has_table, no_new_numbers, reply_risks_none, risks_none, sections, shorter_than_previous, weekly_rule_cases  # noqa: E402
from fakes import FunctionModel, structured  # noqa: E402
from run_eval import run_once  # noqa: E402


def test_case_ids_are_unique_and_fixtures_exist():
    assert len({c.id for c in CASES}) == len(CASES)
    for case in CASES:
        names = [m for t in case.turns for m in t.materials] + list((case.weekly or {}).get("materials", []))
        for name in names:
            if not name.startswith("@"):
                assert (Path(__file__).parent / "eval" / "fixtures" / name).exists(), name


def test_no_new_numbers_ignores_list_ordinals_and_allows_wan():
    check = no_new_numbers("日均 12000 次，解决率 58%")
    assert check.fn("1. 日均 1.2 万次\n2. 解决率 58%")
    assert not check.fn("1. 解决率 60%")


def test_simple_checks():
    assert sections("【a】", "【b】").fn("【a】x【b】y")
    assert has_table().fn("| a | b |\n|---|---|")
    assert shorter_than_previous().fn("短", ["这是一段比较长的上一轮输出"])
    assert not shorter_than_previous().fn("一样长一样长", ["一样长一样长"])
    report = SimpleNamespace(content="【问题与风险】\n- 无\n【下周计划】\n1. 压测", generation={})
    assert risks_none().fn(report)


def test_preference_case_enforces_the_requested_100_character_limit():
    case = next(c for c in CASES if c.id == "M3")
    check = case.turns[-1].checks[0]
    assert check.fn("甲" * 100)
    assert not check.fn("甲" * 101)


def test_task_switch_case_releases_explicit_capability_and_checks_new_task():
    case = next(c for c in CASES if c.id == "M5")
    assert case.turns[0].skill == "todo" and case.turns[1].skill is None
    good = "【优化后】预算尚缺5万元，请审批。\n【主要修改】调整措辞。"
    assert all(check.fn(good, []) for check in case.turns[1].checks)
    bad = "【待办清单】\n| 任务 |\n|---|\n| 处理性能告警 |"
    assert not all(check.fn(bad, []) for check in case.turns[1].checks)


def test_weekly_empty_risk_check_rejects_added_risks():
    check = reply_risks_none()
    assert check.fn("【问题与风险】\n- 无。\n【下周计划】\n- 补测试")
    assert not check.fn("【问题与风险】\n- 无明确问题，但测试不足可能影响上线。")
    assert not check.fn("【下周计划】\n- 补测试")


def test_weekly_rule_checks_catch_invented_details_and_lost_explicit_risk():
    cases = {c.id: c for c in weekly_rule_cases()}
    # 两个入口都必须拒绝“有问题就推断影响 / 已联系就推断支持”；换到别的栏目也不能放过。
    bad = "【周报标题】周报\n【本周完成】登录页开发与联调完成\n【进行中】无\n【问题与风险】接口报错，已联系老赵\n【下周计划】上线并编写测试用例，需要后端协助，可能影响下周上线"
    assert not all(c.fn(bad) for c in cases["CW1"].turns[0].checks)
    report = SimpleNamespace(status="done", content=bad, generation={})
    assert not all(c.fn(report) for c in cases["FW1"].checks)
    good = "【周报标题】周报\n【本周完成】登录页开发与联调完成\n【进行中】无\n【问题与风险】接口报错未解决，已联系老赵\n【下周计划】上线并编写测试用例"
    assert all(c.fn(good) for c in cases["CW1"].turns[0].checks)
    report.content = good
    assert all(c.fn(report) for c in cases["FW1"].checks)
    # 不能把“所有风险都删掉”当作修复：明确的影响和支持被漏掉时也要失败。
    lost = "【周报标题】周报\n【本周完成】无\n【进行中】无\n【问题与风险】测试环境未恢复\n【下周计划】补齐压测报告"
    assert not all(c.fn(lost) for c in cases["CW3"].turns[0].checks)
    report.content = lost
    assert not all(c.fn(report) for c in cases["FW3"].checks)
    explicit = lost.replace("测试环境未恢复", "测试环境未恢复，已导致接口联调延期，需要运维协助恢复")
    assert all(c.fn(explicit) for c in cases["CW3"].turns[0].checks)
    report.content = explicit
    assert all(c.fn(report) for c in cases["FW3"].checks)


def test_run_once_chat_case():
    model = FunctionModel(respond=lambda messages: AIMessage("【一句话总结】项目进展\n| a | b |\n|---|---|"))
    case = Case("T", "测试", "测试", turns=[Turn("总结", materials=("report.md",), checks=[has_table()])])
    record = run_once(case, lambda: model)
    assert record["error"] is None and record["checks"] == [{"turn": 1, "name": "有表格", "ok": True}]


def test_run_once_weekly_case():
    def respond(messages):
        if "撰写周报" in messages[0].content:
            return AIMessage("【本周完成】\n- 修复登录 bug〔简述〕\n【问题与风险】\n- 无")
        return structured("Extraction", items=[])

    case = Case(
        "T", "测试", "测试",
        weekly={"materials": [], "brief": "修复登录 bug", "period_start": "2026-10-05", "period_end": "2026-10-11"},
        checks=[risks_none()],
    )
    record = run_once(case, lambda: FunctionModel(respond=respond))
    assert record["error"] is None and record["checks"][0]["ok"]
