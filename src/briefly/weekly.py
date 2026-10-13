"""周报：用户选素材、写简述 → 后台生成 → 预览（带出处标记）→ 修改 → 导出 Markdown。

生成流程由代码控制（见 summarize.py）：逐块提取并核对原文 → 按周报格式合成 → 数字核对。
另一种输入是文件追踪中用户已确认的工作记录（evidence_ids）：直接作为工作项，出处标为〔E编号〕，不再提取。
"""
import re
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import date, datetime, timedelta

from langchain_core.language_models import BaseChatModel
from sqlalchemy import JSON, Date, DateTime, Integer, String, Text, select, update
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from .context_store import ContextStore
from .db import Base
from .llm import create_chat_model
from .materials import MaterialNotFound, MaterialStore
from .output_check import find_internal_terms
from .skills import read_prompt
from .summarize import CATEGORY_NAMES, Source, WorkItem, extract_items, hard_terms, missing_terms, synthesize

# 提取时告诉模型这次的用途：重要度（high / medium / low）按周报的读者判断，与“完整总结”的标准不同
WEEKLY_PURPOSE = "周报，读者是直属上级：结果、里程碑、决定、关键数据、问题风险算 high；一般工作算 medium；例会、回复消息等日常琐事算 low"

PENDING, RUNNING, DONE, FAILED = "pending", "running", "done", "failed"
_SOURCE_TAG = re.compile(r"〔[^〕]*〕")
_NUMBER = re.compile(r"\d+(?:[.:：/\-]\d+)*")
_LIST_ORDINAL = re.compile(r"(?m)^(\s*)\d+[.、)）]\s*")


class WeeklyReport(Base):
    __tablename__ = "weekly_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    brief: Mapped[str] = mapped_column(Text, default="")
    options: Mapped[dict] = mapped_column(JSON, default=dict)  # 读者、姓名
    material_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(10), default=PENDING)
    content: Mapped[str] = mapped_column(Text, default="")  # 周报正文，带出处标记；用户修改后覆盖
    items: Mapped[list] = mapped_column(JSON, default=list)  # 通过核对的工作项（含原文），材料删了也能查来源
    generation: Mapped[dict] = mapped_column(JSON, default=dict)  # 生成报告
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, onupdate=datetime.now)


class ReportNotFound(MaterialNotFound):
    """周报不存在。复用 MaterialNotFound，接口层统一返回 404。"""


def current_week(today: date | None = None) -> tuple[date, date]:
    """本周一到本周日。由代码计算，不让模型算日期。"""
    today = today or date.today()
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=6)


def strip_source_tags(text: str) -> str:
    """去掉出处标记〔1-3〕〔简述〕，用于导出。"""
    return re.sub(r"[ \t]+\n", "\n", _SOURCE_TAG.sub("", text)).strip()


REMINDER_HEADING = "【给用户的提醒】"
RISK_HEADING = "【问题与风险】"
_REF_TAG = re.compile(r"〔([^〕]*)〕")


def section_lines(text: str, heading: str) -> list[str]:
    """某个栏目下的非空行（到下一个【…】标题为止）。"""
    _, found, rest = text.partition(heading)
    if not found:
        return []
    lines = []
    for line in rest.splitlines():
        if line.strip().startswith("【"):
            break
        if line.strip():
            lines.append(line.strip())
    return lines


def citing_lines(body: str) -> dict[int, list[str]]:
    """每个编号被哪些行引用（编号尚未换回出处）。"""
    out: dict[int, list[str]] = {}
    for line in body.splitlines():
        for n in dict.fromkeys(int(x) for x in re.findall(r"#(\d+)", line)):
            out.setdefault(n, []).append(line)
    return out


def partial_terms(item: WorkItem, lines: list[str]) -> list[str]:
    """合并遗漏：引用了这个事实的行里，缺了它的哪些硬信息（数字、日期、英文标识等）。"""
    text = " ".join(re.sub(r"〔[^〕]*〕", "", line) for line in lines)
    return missing_terms(hard_terms(item.point), text)


def report_check(items: list[WorkItem]) -> Callable[[str], list[str]]:
    """写完周报后的规则检查（不调用模型）：

    1. 【问题与风险】的每一条，必须引用至少一个已核实的问题风险项（或来自用户简述）；
    2. 同一句原文拆出的几项，不能只写其中一部分；
    3. 候选重要度为 high 的事实必须写进周报；
    4. 引用了某个事实的行，要写全它的数字、日期等硬信息（合并时只写了一部分）。

    待办、进行中、待复核的问题都不能单独撑起一条风险。分类由提取阶段的结构化输出决定，这里由代码检查，不靠写作时自觉。
    """

    def check(text: str) -> list[str]:
        body = text.partition(REMINDER_HEADING)[0]
        problems = []
        for line in section_lines(body, RISK_HEADING):
            refs = [r for tag in _REF_TAG.findall(line) for r in re.findall(r"#(\d+)", tag)]
            from_brief = any("简述" in tag for tag in _REF_TAG.findall(line))
            if not refs or from_brief:
                continue  # “无”、只来自简述的：不检查
            numbers = [int(r) for r in refs if 1 <= int(r) <= len(items)]
            if numbers and not any(items[n - 1].verified_risk for n in numbers):
                labels = "、".join(f"#{n}（{CATEGORY_NAMES.get(items[n - 1].category, '其他') if not items[n - 1].needs_review else '待复核'}）" for n in numbers)
                problems.append(
                    f"【问题与风险】中的“{line[:60]}”引用的 {labels} 不是已核实的问题或风险，"
                    "请移到对应栏目（计划写进【下周计划】或【进行中】，已完成写进【本周完成】），不要写进【问题与风险】"
                )
        # 同一句原文拆出的几项（如“确认上线时间”和“上线前补齐压测报告”）只写了一部分：常见的漏写方式
        used = {int(n) for n in re.findall(r"#(\d+)", body)}
        groups: dict[str, list[int]] = {}
        for n, item in enumerate(items, start=1):
            groups.setdefault(re.sub(r"\s+", "", item.quote), []).append(n)
        for numbers in groups.values():
            if len(numbers) > 1 and used & set(numbers):
                for n in numbers:
                    if n not in used and not items[n - 1].needs_review:
                        problems.append(f"#{n}（{items[n - 1].point}）与 #{min(used & set(numbers))} 出自同一句原文，是另一项内容，没有写进周报，请补上")
        reported = {p.split("（", 1)[0] for p in problems}
        for n, item in enumerate(items, start=1):
            if n not in used and item.importance == "high" and not item.needs_review and f"#{n}" not in reported:
                problems.append(f"#{n}（{item.point}）是重要内容，没有写进周报，请补上")
        for n, lines in citing_lines(body).items():
            if 1 <= n <= len(items):
                lost = partial_terms(items[n - 1], lines)
                if lost:
                    problems.append(f"#{n}（{items[n - 1].point}）写进周报时缺少 {'、'.join(lost)}，请按要点写全")
        return problems

    return check


def split_reminders(text: str) -> tuple[str, list[str]]:
    """把模型写在周报之后的【给用户的提醒】拆出来：提醒只给用户看，不进入周报正文和导出。"""
    report, _, rest = text.partition(REMINDER_HEADING)
    reminders = [line.strip().lstrip("-•* ").strip() for line in rest.splitlines()]
    return report.strip(), [r for r in reminders if r]


def unverified_numbers(report: str, evidence: str) -> list[str]:
    """周报里在素材、简述、周期中都找不到的数字和日期。只用于提醒：换算出的日期、合计等是合法推导。"""
    text = _LIST_ORDINAL.sub(r"\1", strip_source_tags(report))  # 列表序号不算
    pool = re.sub(r"\s+", "", evidence)
    return [n for n in dict.fromkeys(_NUMBER.findall(text)) if n not in pool]


def fact_trace(items: list[WorkItem], raw: str) -> tuple[list[dict], list[tuple[WorkItem, list[str]]]]:
    """逐个事实记录去向：written（单独写成一行）/ merged（和别的事实写在同一行）/ uncovered（合成时整条遗漏）。

    返回 (追踪记录, 只写了一部分的事实及缺少的硬信息)。
    """
    body = raw.partition(REMINDER_HEADING)[0]
    cited = citing_lines(body)
    trace, partial = [], []
    for n, item in enumerate(items, start=1):
        lines = cited.get(n, [])
        lost = partial_terms(item, lines) if lines else []
        together = sorted({int(x) for line in lines for x in re.findall(r"#(\d+)", line)} - {n})
        status = "uncovered" if not lines else ("merged" if together else "written")
        trace.append(
            {
                "fact_id": item.fact_id,
                "n": n,
                "importance": item.importance,
                "status": status,
                "merged_with": [items[m - 1].fact_id for m in together if 1 <= m <= len(items)],
                "missing": lost,
            }
        )
        if lost:
            partial.append((item, lost))
    return trace, partial


class WeeklyService:
    def __init__(self, store: MaterialStore, model_factory: Callable[[], BaseChatModel] = create_chat_model) -> None:
        self.store = store
        self._model_factory = model_factory
        self._session = sessionmaker(store.engine, expire_on_commit=False)
        self.cache = ContextStore(store.engine)  # 逐块提取结果缓存：重新生成、中断后重跑时不重算
        self._fail_interrupted()

    def _fail_interrupted(self) -> None:
        """服务重启时还在生成中的周报不会再继续，标记为失败，免得一直显示“生成中”。"""
        with self._session() as s:
            s.execute(
                update(WeeklyReport)
                .where(WeeklyReport.status.in_([PENDING, RUNNING]))
                .values(status=FAILED, error="服务重启，生成中断，请重新生成")
            )
            s.commit()

    def create(
        self,
        material_ids: list[int],
        brief: str = "",
        period_start: date | None = None,
        period_end: date | None = None,
        audience: str | None = None,
        name: str | None = None,
        evidence_ids: list[int] | None = None,
    ) -> WeeklyReport:
        """登记一份待生成的周报；真正的生成由 generate 在后台执行。

        evidence_ids：文件追踪中用户已确认的工作记录，存在 options 里（不改表结构）。
        """
        evidence_ids = list(dict.fromkeys(evidence_ids or []))
        if not material_ids and not brief.strip() and not evidence_ids:
            raise ValueError("请至少选择一份素材，或写一段简述")
        for material_id in material_ids:
            self.store.get(material_id)  # 不存在时报 404
        if evidence_ids:
            confirmed = {e.id for e in self._evidence(evidence_ids)}
            missing = [i for i in evidence_ids if i not in confirmed]
            if missing:
                raise ValueError(f"工作记录 {', '.join(f'E{i}' for i in missing)} 不存在或未经确认，不能用于周报")
        default_start, default_end = current_week()
        start, end = period_start or default_start, period_end or default_end
        if start > end:
            raise ValueError("周期的开始日期不能晚于结束日期")
        report = WeeklyReport(
            period_start=start,
            period_end=end,
            brief=brief.strip(),
            options={k: v for k, v in {"audience": audience, "name": name, "evidence_ids": evidence_ids}.items() if v},
            material_ids=list(dict.fromkeys(material_ids)),
        )
        with self._session() as s:
            s.add(report)
            s.commit()
        return report

    def generate(self, report_id: int) -> None:
        """后台执行：出错时记为失败并保存原因，不向外抛出。"""
        report = self.get(report_id)
        self._update(report_id, status=RUNNING, error="")
        try:
            content, items, generation = self._run(report)
        except Exception as e:  # 后台任务没有调用方接异常，必须记下来
            self._update(report_id, status=FAILED, error=f"{type(e).__name__}: {e}"[:1000])
            return
        self._update(report_id, status=DONE, content=content, items=items, generation=generation)

    def _run(self, report: WeeklyReport) -> tuple[str, list[dict], dict]:
        started = time.monotonic()
        model = self._model_factory()
        sources, warnings = [], []
        for material_id in report.material_ids:
            try:
                material = self.store.get(material_id)
            except MaterialNotFound:
                warnings.append(f"材料 {material_id} 已被删除，未纳入")
                continue
            chunks = self.store.chunks(material_id)
            sources += [Source(material_id, c.index, material.title, c.content, c.start) for c in chunks]
            warnings += [f"《{material.title}》：{w}" for w in material.parse_report.get("warnings", [])]

        period = f"{report.period_start.isoformat()} 至 {report.period_end.isoformat()}"
        evidence_items = self._evidence_items(report.options.get("evidence_ids", []), warnings)
        extraction = extract_items(model, sources, focus=report.brief, period=period, cache=self.cache, purpose=WEEKLY_PURPOSE)
        extraction.items[:0] = evidence_items  # 已确认的工作记录排在前面，与提取的工作项一起合成
        for failed in extraction.failed:
            material_id, chunk = failed["source"].split("-")
            warnings.append(f"材料 {material_id} 第 {chunk} 块处理失败，未纳入：{failed['error'][:80]}")
        if extraction.dropped:
            warnings.append(f"有 {len(extraction.dropped)} 条提取结果在原文中找不到依据，已丢弃")

        request = "\n".join(
            [
                f"周期：{period}",
                f"读者：{report.options.get('audience') or '直属上级'}",
                f"姓名：{report.options.get('name') or '未提供'}",
                f"用户简述〔简述〕：{report.brief or '无'}",
                "请撰写本周周报。",
            ]
        )
        system_prompt = read_prompt("weekly") + "\n\n" + read_prompt("weekly_report")
        synthesis = synthesize(
            model, system_prompt, request, extraction.items, aside_heading=REMINDER_HEADING, check=report_check(extraction.items)
        )
        content, reminders = split_reminders(synthesis.text)
        # 标为问题风险但原文没有写明阻碍、异常、延期或依赖的：不写进【问题与风险】，请用户确认
        review = [i for i in extraction.items if i.needs_review]
        reminders += [f"这一项可能是问题或风险，但原文没有写明具体的阻碍或影响，没有写进【问题与风险】，请确认：{i.point}〔{i.source}〕" for i in review]
        # 输出覆盖：没写进周报的工作项列给用户，由用户决定要不要补（有简述时，次要工作本来可以不写）
        reminders += [f"这一项没有写进周报，如需要请补充：{i.point}〔{i.source}〕" for i in synthesis.uncovered if not i.needs_review]
        facts, partial = fact_trace(extraction.items, synthesis.raw)
        # 合并遗漏：引用了，但只写了一部分
        reminders += [f"这一项只写了一部分，缺少 {'、'.join(lost)}（合成周报时遗漏），请补充：{i.point}〔{i.source}〕" for i, lost in partial]
        # 提取遗漏：原文写了，但提取出的要点里没有，周报里也没有
        for o in extraction.incomplete:
            lost = missing_terms(o["missing"], strip_source_tags(content))
            if lost:
                source = o["fact_ids"][0].rsplit(".", 1)[0]
                reminders.append(f"原文中的 {'、'.join(lost)} 没有写进周报（提取要点时遗漏），请核对：{o['quote'][:80]}〔{source}〕")
        # 重写后仍有的问题：风险栏引用不对的写进系统提示；漏写的已由输出覆盖列进提醒，不重复
        warnings += [f"{p.split('，请移到')[0]}，请核对" for p in synthesis.problems if "请移到" in p]
        if synthesis.invalid_refs:
            warnings.append(f"周报引用了不存在的工作项编号 {synthesis.invalid_refs}，相关内容可能没有依据，请核对")
        if not synthesis.coverage_checked:
            warnings.append("周报没有按要求标注出处编号，未能核对是否漏写了素材中的内容")

        evidence = " ".join([report.brief, period, *(f"{i.point} {i.quote}" for i in extraction.items)])
        numbers = unverified_numbers(content, evidence)
        if numbers:
            warnings.append(f"以下数字未在素材或简述中找到原文，请核对：{'、'.join(numbers)}")
        # 内部处理信息（块号等）混进正文：只提示人工核对，不删除（原文可能真有这样的话），不另调模型审核
        originals = [report.brief, *(s.content for s in sources), *(f"{i.point} {i.quote}" for i in extraction.items)]
        terms = find_internal_terms(strip_source_tags(content), originals)
        if terms:
            warnings.append(f"周报正文中出现了程序内部的处理信息：{'、'.join(terms)}，请核对后删除")

        usage = extraction.usage
        generation = {
            "chunks_total": extraction.total,
            "chunks_processed": extraction.processed,
            "chunks_cached": extraction.cached,
            "failed": extraction.failed,
            "dropped": extraction.dropped,
            "unverified_numbers": numbers,
            "uncovered": [asdict(i) for i in synthesis.uncovered],  # 没有写进周报的工作项
            "coverage_checked": synthesis.coverage_checked,
            "merge_levels": synthesis.merge_levels,
            "calls": usage.calls + synthesis.usage.calls,
            "input_tokens": usage.input_tokens + synthesis.usage.input_tokens,
            "output_tokens": usage.output_tokens + synthesis.usage.output_tokens,
            "seconds": round(time.monotonic() - started, 1),
            "facts": facts,  # 每个事实（fact_id）写进了哪几行、和哪些事实合并、是否只写了一部分，用于定位遗漏发生在哪一步
            "map_omissions": extraction.incomplete,
            "warnings": warnings,  # 系统提示：处理失败、丢弃、未核实数字等
            "reminders": reminders,  # 模型给用户的提醒：可能想补充或核对的地方，不进入周报
        }
        return content, [asdict(i) for i in extraction.items], generation

    def _evidence(self, ids: list[int]):
        """已确认的工作记录（文件追踪）。"""
        from .tracking.models import CONFIRMED, WorkEvidence

        if not ids:
            return []
        with self._session() as s:
            rows = s.scalars(select(WorkEvidence).where(WorkEvidence.id.in_(ids), WorkEvidence.review_status == CONFIRMED))
            return sorted(rows, key=lambda e: ids.index(e.id))

    def _evidence_items(self, ids: list[int], warnings: list[str]) -> list[WorkItem]:
        """已确认的工作记录 → 工作项：要点是标题和描述（用户改过的以用户为准），原文是描述和来源文件。"""
        rows = self._evidence(ids)
        found = {e.id for e in rows}
        warnings += [f"工作记录 E{i} 已被删除或撤销确认，未纳入" for i in ids if i not in found]
        items = []
        for e in rows:
            files = "、".join(dict.fromkeys(r["path"].replace("\\", "/").rsplit("/", 1)[-1] for r in e.source_refs if r.get("path")))
            point = f"{e.title}：{e.description}" if e.description else e.title
            quote = e.description or e.title
            if files:
                quote += f"（来源文件：{files}）"
            items.append(WorkItem(e.category, point, quote, f"E{e.id}"))
        return items

    def list_reports(self) -> list[WeeklyReport]:
        with self._session() as s:
            return list(s.scalars(select(WeeklyReport).order_by(WeeklyReport.id.desc())))

    def get(self, report_id: int) -> WeeklyReport:
        with self._session() as s:
            report = s.get(WeeklyReport, report_id)
        if report is None:
            raise ReportNotFound(f"周报 {report_id} 不存在")
        return report

    def update_content(self, report_id: int, content: str) -> WeeklyReport:
        report = self.get(report_id)
        if report.status != DONE:
            raise ValueError("周报还没有生成完成，不能修改")
        if not content.strip():
            raise ValueError("周报内容不能为空")
        self._update(report_id, content=content.strip())
        return self.get(report_id)

    def delete(self, report_id: int) -> None:
        report = self.get(report_id)
        with self._session() as s:
            s.delete(s.merge(report))
            s.commit()

    def export_markdown(self, report_id: int) -> tuple[str, str]:
        """返回 (文件名, 去掉出处标记的正文)。"""
        report = self.get(report_id)
        if report.status != DONE:
            raise ValueError("周报还没有生成完成，不能导出")
        return f"周报_{report.period_start}_{report.period_end}.md", strip_source_tags(report.content) + "\n"

    def _update(self, report_id: int, **values) -> None:
        with self._session() as s:
            s.execute(update(WeeklyReport).where(WeeklyReport.id == report_id).values(**values))
            s.commit()
