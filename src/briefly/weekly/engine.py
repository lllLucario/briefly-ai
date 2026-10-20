"""周报的写作引擎：把一周的输入写成周报正文（不管流程，流程在 service.py）。

输入：审核清单里已确认的工作记录（直接作为工作项，出处〔E编号〕）、本周材料（逐块提取并核对原文）、本周说明。
流程由代码控制（见 summarize.py）：提取 → 冲突检测 → 按周报格式合成 → 规则检查与重写 → 数字核对、提醒。
"""
import re
import time
from collections.abc import Callable
from dataclasses import asdict, replace
from datetime import date, timedelta

from langchain_core.language_models import BaseChatModel
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from ..conflicts import (
    ATTRIBUTES,
    BRIEF,
    UNRESOLVED,
    FactConflict,
    brief_conflict_problems,
    classify,
    conflict_problems,
    detect_conflicts,
    describe,
)
from ..context_store import ContextStore
from ..llm import create_chat_model
from ..materials import MaterialNotFound, MaterialStore
from ..output_check import find_internal_terms
from ..skills import read_prompt
from ..summarize import CATEGORY_NAMES, ExtractionResult, Source, WorkItem, coverage_terms, extract_items, missing_terms, synthesize
from .models import CONFIRMED, WeeklyReport, WorkEvidence

# 提取时告诉模型这次的用途：重要度（high / medium / low）按周报的读者判断，与“完整总结”的标准不同
WEEKLY_PURPOSE = "周报，读者是直属上级：结果、里程碑、决定、关键数据、问题风险算 high；一般工作算 medium；例会、回复消息等日常琐事算 low"

_SOURCE_TAG = re.compile(r"〔[^〕]*〕")
_NUMBER = re.compile(r"\d+(?:[.:：/\-]\d+)*")
_LIST_ORDINAL = re.compile(r"(?m)^(\s*)\d+[.、)）]\s*")


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


# 给写作模型看的说明（format_items 附在工作项后面）：如果出现在周报正文里，说明模型把说明当成了内容
HINT_PHRASES = ("原文中还写了", "概括里没有", "出自同一句原文", "风险依据", "待复核", "同样要写进周报", "所属待办", "需保留的原文要点")


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
    return missing_terms(coverage_terms(item), text)


def report_check(items: list[WorkItem], conflicts: list[FactConflict] | None = None) -> Callable[[str], list[str]]:
    """写完周报后的规则检查（不调用模型）：

    1. 【问题与风险】的每一条，必须引用至少一个已核实的问题风险项（或来自用户简述）；
    2. 同一句原文拆出的几项，不能只写其中一部分；
    3. 候选重要度为 high 的事实必须写进周报；
    4. 引用了某个事实的行，要写全它的数字、日期等硬信息（合并时只写了一部分）；
    5. 材料之间说法不一致（未解决、场景不同）的，不能只写其中一种说法；
    6. 简述与材料说法不一致（未解决）的：正文同一条不能同时写两种说法、出处要标对；提醒里要列出两种说法和出处（正文与提醒分开检查）。

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
        cited = citing_lines(body)
        problems += conflict_problems(conflicts or [], lambda n: cited.get(n, []))
        problems += brief_conflict_problems(conflicts or [], text, REMINDER_HEADING)
        leaked = [h for h in HINT_PHRASES if h in body]
        if leaked:
            problems.append(f"周报正文里出现了给你看的说明文字（{'、'.join(leaked)}），这些不是工作内容，请删掉，只写工作本身")
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


REPORT_STATUS = {"todo": "未开始", "doing": "进行中", "done": "已完成"}


def placed(category: str, status: str | None) -> str:
    """用户给出本次状态时，工作项放进哪个栏目：已完成 → 本周完成，进行中 → 进行中；
    未开始不等于“下周计划”，原本是计划、问题或其他的保持不变，原本判断为完成或进行中的改为其他。"""
    if status == "done":
        return "done"
    if status == "doing":
        return "in_progress"
    if status == "todo" and category in ("done", "in_progress"):
        return "other"
    return category


class ReportWriter:
    def __init__(self, store: MaterialStore, model_factory: Callable[[], BaseChatModel] = create_chat_model) -> None:
        self.store = store
        self._model_factory = model_factory
        self._session = sessionmaker(store.engine, expire_on_commit=False)
        self.cache = ContextStore(store.engine)  # 逐块提取结果缓存：重新生成、中断后重跑时不重算

    def write(self, report: WeeklyReport) -> tuple[str, list[dict], dict]:
        """返回 (正文, 工作项, 生成报告)。出错时抛出异常，由调用方记为失败。"""
        started = time.monotonic()
        inputs = report.inputs or {}
        model = self._model_factory()
        sources, warnings = [], []
        # 登记之后、生成之前从材料库删除的素材（删除时已从 material_ids 去掉，记在 removed_materials）
        warnings += [f"材料 {x['id']} 已被删除，未纳入" for x in report.removed_materials or []]
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
        evidence_items = self._evidence_items(inputs.get("evidence_ids", []), warnings, inputs.get("todo_refs", []))
        extraction = extract_items(model, sources, focus=report.note, period=period, cache=self.cache, purpose=WEEKLY_PURPOSE)
        extraction.items[:0] = evidence_items  # 已确认的工作记录排在前面，与提取的工作项一起合成
        for failed in extraction.failed:
            material_id, chunk = failed["source"].split("-")
            warnings.append(f"材料 {material_id} 第 {chunk} 块处理失败，未纳入：{failed['error'][:80]}")
        if extraction.dropped:
            warnings.append(f"有 {len(extraction.dropped)} 条提取结果在原文中找不到依据，已丢弃")

        request = "\n".join(
            [
                f"周期：{period}",
                f"读者：{inputs.get('audience') or '直属上级'}",
                f"姓名：{inputs.get('name') or '未提供'}",
                f"用户简述〔简述〕：{report.note or '无'}",
                "请撰写本周周报。",
            ]
        )
        # 简述里可核实的事实（日期、金额、数量等）：单独提取一次，只用于和材料比对冲突，不进入正文工作项（避免重复）
        brief_items, brief_calls = self._brief_facts(model, report.note, period)
        comparison = list(extraction.items)
        references = {i.fact_id: n for n, i in enumerate(extraction.items, 1) if i.fact_id}
        evidence_fact_calls = evidence_fact_cached = 0
        # 两条以上记录互相核对；单条只在另一来源有可比对事实时核对。批量提取并复用缓存。
        if len(evidence_items) > 1 or (evidence_items and any(i.attribute for i in [*extraction.items, *brief_items])):
            facts = self._evidence_facts(model, evidence_items, period, warnings)
            evidence_fact_calls, evidence_fact_cached = facts.usage.calls, facts.cached
            extraction.usage.calls += facts.usage.calls
            extraction.usage.input_tokens += facts.usage.input_tokens
            extraction.usage.output_tokens += facts.usage.output_tokens
            by_source = {i.source: n for n, i in enumerate(extraction.items, 1)}
            references.update({i.fact_id: by_source[i.source] for i in facts.items})
            comparison += facts.items
        if extraction.unverified_facts:
            warnings.append(f"有 {len(extraction.unverified_facts)} 项主体或取值无法核对，相关事实未参与冲突比较，请核对原文")
        # 多来源冲突：在任何合并之前由代码检测（分批合并可能丢掉其中一种说法），有冲突时调用一次模型归类（含主体候选确认）
        conflicts = detect_conflicts(comparison, brief_items, reference_numbers=references, as_of=report.period_end)
        conflict_calls = classify(model, conflicts, comparison, brief_items) + brief_calls
        if conflicts:
            request += "\n\n" + describe(conflicts, extraction.items)
        system_prompt = read_prompt("weekly") + "\n\n" + read_prompt("weekly_report")
        synthesis = synthesize(
            model,
            system_prompt,
            request,
            extraction.items,
            aside_heading=REMINDER_HEADING,
            check=report_check(extraction.items, conflicts),
        )
        content, reminders = split_reminders(synthesis.text)
        # 标为问题风险但原文没有写明阻碍、异常、延期或依赖的：不写进【问题与风险】，请用户确认
        review = [i for i in extraction.items if i.needs_review]
        reminders += [f"这一项可能是问题或风险，但原文没有写明具体的阻碍或影响，没有写进【问题与风险】，请确认：{i.point}〔{i.source}〕" for i in review]
        # 输出覆盖：没写进周报的工作项列给用户，由用户决定要不要补（有简述时，次要工作本来可以不写）
        reminders += [f"这一项没有写进周报，如需要请补充：{i.point}〔{i.source}〕" for i in synthesis.uncovered if not i.needs_review]
        # 看不出哪个为准的冲突：由代码列进提醒（不依赖写作时自觉），每种说法带出处
        for c in conflicts:
            if c.resolution != UNRESOLVED:
                continue
            name = f"{c.subject} 的{ATTRIBUTES[c.attribute]}" + (f"（{c.aspect}）" if c.aspect else "")
            materials = [(v, src) for v, src, o in zip(c.values, c.sources, c.origins) if o != BRIEF]
            if "material_vs_material" in c.pairs or "evidence_vs_material" in c.pairs:
                sides = " / ".join(f"{v}〔{src}〕" for v, src in materials)
                label = "工作记录与其他来源" if any(o == "evidence" for o in c.origins) else "材料之间"
                reminders.append(f"{label}{'可能' if c.possible else ''}说法不一致：{name}为 {sides}，未说明哪个为准，请核对")
            if "evidence_vs_evidence" in c.pairs:
                sides = " / ".join(f"{v}〔{src}〕" for v, src in materials)
                reminders.append(f"已确认工作记录之间说法不一致：{name}为 {sides}，请核对")
            if any(p.startswith("brief_vs_") for p in c.pairs):
                said = " / ".join(v for v, o in zip(c.values, c.origins) if o == BRIEF)
                sides = " / ".join(f"{v}〔{src}〕" for v, src in materials)
                label = "其他来源" if "evidence" in c.origins else "材料"
                reminders.append(f"简述与{label}{'可能' if c.possible else ''}说法不一致：{name}，简述写的是 {said}〔简述〕，{label}写的是 {sides}；正文应只采用其中一种，请核对")
        facts, partial = fact_trace(extraction.items, synthesis.raw)
        # 合并遗漏：引用了，但只写了一部分
        reminders += [f"这一项只写了一部分，缺少 {'、'.join(lost)}（合成周报时遗漏），请补充：{i.point}〔{i.source}〕" for i, lost in partial]
        # 提取遗漏：原文写了，但提取出的要点里没有，周报里也没有
        for o in extraction.incomplete:
            lost = missing_terms(o["missing"], strip_source_tags(content))
            if lost:
                source = o["fact_ids"][0].rsplit(".", 1)[0]
                reminders.append(f"原文中的 {'、'.join(lost)} 没有写进周报（提取要点时遗漏），请核对：{o['quote'][:80]}〔{source}〕")
        # 遗漏已有逐项提醒；冲突、出处、内部提示泄漏等其余失败也必须显示，不能静默放过。
        omission_messages = ("是重要内容，没有写进周报", "出自同一句原文", "写进周报时缺少")
        warnings += [f"重写后仍未通过核对：{p}，请核对" for p in synthesis.problems
                     if not any(marker in p for marker in omission_messages)]
        if synthesis.invalid_refs:
            warnings.append(f"周报引用了不存在的工作项编号 {synthesis.invalid_refs}，相关内容可能没有依据，请核对")
        if not synthesis.coverage_checked:
            warnings.append("周报没有按要求标注出处编号，未能核对是否漏写了素材中的内容")

        evidence = " ".join([report.note, period, *(f"{i.point} {i.quote} {i.group}" for i in extraction.items)])
        numbers = unverified_numbers(content, evidence)
        if numbers:
            warnings.append(f"以下数字未在素材或简述中找到原文，请核对：{'、'.join(numbers)}")
        # 内部处理信息（块号等）混进正文：只提示人工核对，不删除（原文可能真有这样的话），不另调模型审核
        originals = [report.note, *(s.content for s in sources), *(f"{i.point} {i.quote} {i.group}" for i in extraction.items)]
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
            "calls": usage.calls + synthesis.usage.calls + conflict_calls,
            "input_tokens": usage.input_tokens + synthesis.usage.input_tokens,
            "output_tokens": usage.output_tokens + synthesis.usage.output_tokens,
            "seconds": round(time.monotonic() - started, 1),
            "facts": facts,  # 每个事实（fact_id）写进了哪几行、和哪些事实合并、是否只写了一部分，用于定位遗漏发生在哪一步
            "map_omissions": extraction.incomplete,
            "evidence_fact_calls": evidence_fact_calls,
            "evidence_fact_cached": evidence_fact_cached,
            "conflicts": [c.to_dict() for c in conflicts],  # 多来源冲突及归类（unresolved / superseded / context_difference）
            "warnings": warnings,  # 系统提示：处理失败、丢弃、未核实数字等
            "reminders": reminders,  # 模型给用户的提醒：可能想补充或核对的地方，不进入周报
        }
        return content, [asdict(i) for i in extraction.items], generation

    def _evidence_facts(self, model: BaseChatModel, items: list[WorkItem], period: str, warnings: list[str]) -> ExtractionResult:
        """仅补比较字段，不改写已确认工作项、类别或用户选择的本次状态。

        原话只能归属到唯一记录；模糊归属、提取失败不猜测，不阻止原记录进入周报。
        """
        text = "\n\n".join(f"【记录 {i.source}】\n{i.point}" for i in items)
        result = extract_items(model, [Source(0, 0, "已确认工作记录：仅核对事实", text)], period=period,
                               cache=self.cache, purpose="仅用于多来源冲突比较，逐项提取明确的主体、属性和值；quote不得跨越记录，不改写工作记录")
        mapped = []
        for fact in result.items:
            if not fact.attribute:
                continue
            normalized = re.sub(r"\s+", "", fact.quote)
            matches = [i for i in items if normalized in re.sub(r"\s+", "", i.point) and re.sub(r"\s+", "", fact.subject) in re.sub(r"\s+", "", i.point)]
            if len(matches) != 1:
                warnings.append("有工作记录事实无法唯一定位到原话，未参与冲突比较，请核对")
                continue
            original = matches[0]
            mapped.append(replace(fact, source=original.source, fact_id=f"{original.source}.fact.{len(mapped) + 1}"))
        if result.failed or result.dropped or result.unverified_facts:
            warnings.append("部分已确认工作记录的事实核对未完成，冲突检测可能不完整；原工作记录仍保留")
        result.items = mapped
        return result

    def _brief_facts(self, model, brief: str, period: str) -> tuple[list[WorkItem], int]:
        """从简述提取可核实的事实，出处标为“简述”。只用于冲突检测。

        只有简述里可能有可比对的取值（数字，或完成、上线、延期等状态词）时才调用模型，否则不增加调用。
        """
        if not re.search(r"\d|完成|上线|延期|推迟|进行中|未开始|暂停|阻塞|负责", brief):
            return [], 0
        result = extract_items(model, [Source(0, 0, "用户简述", brief)], period=period, cache=self.cache, purpose=WEEKLY_PURPOSE)
        items = [
            replace(i, source="简述", fact_id=f"简述.{k}", span_start=-1)
            for k, i in enumerate(result.items, start=1)
            if i.attribute and i.value
        ]
        return items, result.usage.calls

    def _evidence(self, ids: list[int]):
        """已确认的工作记录（文件追踪）。"""
        if not ids:
            return []
        with self._session() as s:
            rows = s.scalars(select(WorkEvidence).where(WorkEvidence.id.in_(ids), WorkEvidence.review_status == CONFIRMED))
            return sorted(rows, key=lambda e: ids.index(e.id))

    def _evidence_items(self, ids: list[int], warnings: list[str], todo_refs: list[dict] | None = None) -> list[WorkItem]:
        """已确认的工作记录 → 工作项：要点是标题和描述（用户改过的以用户为准），原文是描述。

        不附来源文件名：文件名不是工作内容，附上会被当成要写进周报的信息（出处已由〔E编号〕标注）。
        关联了待办的记录带所属的顶级待办（子待办的记录写父待办），并把同一个父待办的记录排在一起：
        模型基本按给出的顺序写，靠提示词让它重新排序不可靠（实测 W9）。一条记录关联多个待办时都列出。
        """
        groups: dict[int, list[str]] = {}
        statuses: dict[int, str] = {}  # 用户在审核清单里给出的本次状态（todo / doing / done）
        for ref in todo_refs or []:
            if ref.get("report_status"):
                statuses.setdefault(ref["evidence_id"], ref["report_status"])
            todo = ref.get("todo") or {}
            top = todo.get("parent_title") or todo.get("title") or ref["group"]
            groups.setdefault(ref["evidence_id"], [])
            if top not in groups[ref["evidence_id"]]:
                groups[ref["evidence_id"]].append(top)
        rows = self._evidence(ids)
        found = {e.id for e in rows}
        warnings += [f"工作记录 E{i} 已被删除或撤销确认，未纳入" for i in ids if i not in found]
        items = []
        for e in rows:
            status = statuses.get(e.id)
            title = f"{e.title}（{REPORT_STATUS[status]}）" if status else e.title
            point = f"{title}：{e.description}" if e.description else title
            quote = e.description or e.title
            # 用户明确确认过的工作都要写进周报：按高重要度处理，漏写会触发重写
            items.append(WorkItem(placed(e.category, status), point, quote, f"E{e.id}", importance="high", group="、".join(groups.get(e.id, []))))
        first: dict[str, int] = {}
        for n, item in enumerate(items):
            first.setdefault(item.group or f"#{n}", n)
        return [item for _, item in sorted(enumerate(items), key=lambda x: (first[x[1].group or f"#{x[0]}"], x[0]))]
