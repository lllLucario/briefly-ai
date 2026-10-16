"""周报测试的公共工具：用本周材料 + 本周说明登记一份周报，等待生成（与网页上不经过文件追踪、直接加材料写说明一样）。"""
from datetime import date

from briefly.weekly.service import WeeklyService


def draft(
    weekly: WeeklyService,
    material_ids: list[int],
    note: str = "",
    start: date | None = None,
    end: date | None = None,  # 周报按自然周，end 只为与 start 成对书写，不起作用
    audience: str | None = None,
    name: str | None = None,
):
    """那一周的周报：设好本周材料和本周说明，确认清单并登记生成（之后调用 weekly.run_generation）。"""
    report = weekly.week(start)
    weekly.set_materials(report.id, material_ids)
    weekly.set_note(report.id, note)
    return weekly.start_generation(report.id, audience, name)
