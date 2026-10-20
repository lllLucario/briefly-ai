"""读取真实待办和正式周报；不创建、修改、生成或发送。"""
from langchain_core.tools import tool
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..materials import wrap_material
from ..todos.models import Todo
from ..weekly.models import WeeklyReport


def business_tools(store):
    @tool
    def read_todos(todo_id: int | None = None, offset: int = 0, page: int = 1) -> str:
        """只读查询实际保存的待办。用户问“我的待办/当前任务”时调用；不指定编号分页列出未归档任务（每页20条），指定todo_id按page读取说明（每页3000字）。不能修改状态。"""
        if offset < 0 or page < 1:
            raise ValueError("offset不能小于0，page从1开始")
        with Session(store.engine) as s:
            if todo_id is not None:
                t = s.get(Todo, todo_id)
                if t is None:
                    raise ValueError("待办不存在")
                text = t.description or "未提及说明"
                pages = max(1, (len(text)+2999)//3000)
                if page > pages:
                    raise ValueError(f"页码超出范围，共{pages}页")
                return wrap_material(f"待办 {t.id}《{t.title}》", f"状态：{t.status}；截止：{t.due_date or '待定'}；父待办：{t.parent_id or '无'}；已归档：{bool(t.archived_at)}\n说明（第{page}/{pages}页）：{text[(page-1)*3000:page*3000]}")
            rows = list(s.scalars(select(Todo).where(Todo.archived_at.is_(None)).order_by(Todo.id).offset(offset).limit(21)))
            if not rows:
                return "没有已保存的未归档待办；对话候选需预览确认后才会保存。"
            return wrap_material("已保存待办", "\n".join(f"- {t.id}《{t.title}》：{t.status}，截止 {t.due_date or '待定'}" for t in rows[:20])) + (f"\n还有更多，用 offset={offset+20} 继续。" if len(rows)>20 else "")

    @tool
    def read_weekly_reports(report_id: int | None = None, page: int = 1, offset: int = 0) -> str:
        """只读查询正式周报。用户问“本周/上周已保存周报”时调用；不指定编号分页列出周期与状态（每页10份），指定report_id按page读取正文（每页3000字）。聊天草稿不在其中，不会生成或发送。"""
        if page < 1 or offset < 0:
            raise ValueError("page从1开始，offset不能小于0")
        with Session(store.engine) as s:
            if report_id is None:
                rows = list(s.scalars(select(WeeklyReport).order_by(WeeklyReport.period_start.desc()).offset(offset).limit(11)))
                if not rows:
                    return "尚未保存正式周报；对话草稿不会自动出现在周报历史。"
                return wrap_material("正式周报列表", "\n".join(f"- {r.id}：{r.period_start} 至 {r.period_end}，状态 {r.status}" for r in rows[:10])) + (f"\n还有更多，用 offset={offset+10} 继续。" if len(rows)>10 else "")
            r = s.get(WeeklyReport, report_id)
            if r is None:
                raise ValueError("正式周报不存在")
            text = r.content or "正文尚未生成，需在周报页核对后生成。"
            pages = max(1, (len(text)+2999)//3000)
            if page > pages:
                raise ValueError(f"页码超出范围，共{pages}页")
            return wrap_material(f"正式周报 {report_id}（{r.period_start} 至 {r.period_end}，{r.status}，第{page}/{pages}页）", text[(page-1)*3000:page*3000])

    return [read_todos, read_weekly_reports]
