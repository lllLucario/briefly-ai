"""实测脚本本身的测试：用假模型验证检查规则和运行流程，不调用真实模型。"""
import sys
from pathlib import Path
from types import SimpleNamespace

from langchain_core.messages import AIMessage

sys.path.insert(0, str(Path(__file__).parent / "eval"))

from cases import CASES, Case, Turn, has_table, no_new_numbers, risks_none, sections, shorter_than_previous  # noqa: E402
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
