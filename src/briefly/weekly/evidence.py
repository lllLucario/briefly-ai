"""文件变化 → 审核清单里的候选工作记录（Work Evidence）：把这一周追踪文件的变化整理出来，供用户审核。

每个文件的比较方式：
- 基线：周期开始前的最后一个版本；周期内才开始追踪（baseline）的文件用 baseline——只比较接手之后的变化；
  可能由别的文件移动/改名而来的，用原文件的版本作基线；周期内新出现的文件没有基线，整份内容作为“新出现的文件”，并注明。
- 终点：周期结束前的最后一个版本。净变化为空但中间版本有过增删时，也列出（周四加、周五删）。
文字描述复用 summarize.extract_items 的逐块提取（提示词 evidence_extract，引用必须能在变化记录里找到，找不到的丢弃），
每个文件再用一次小调用写标题和描述；模型没有提出要点时用确定性的描述（“修改了《x》N 处”），不编造。
Diff 分批，每批不超过 TRACKING_DIFF_BATCH_TOKENS，每次调用都在 input_limit 之内。

幂等与保护用户审核：
- 指纹 =（文件、基线版本、终点版本、算法版本）。指纹相同的记录已存在时不重新生成（不重复调用模型）。
- 该文件已有未审核、未修改的候选 → 标为 SUPERSEDED，换成新的。
- 已有审核过（确认、排除、修改、合并、拆分）的记录 → 不覆盖，标记“有更新”，只为之后的新变化生成一条新的待确认记录。
"""
import difflib
import hashlib
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, time

from langchain_core.language_models import BaseChatModel
from pydantic import BaseModel, Field
from sqlalchemy import select, update
from sqlalchemy.orm import sessionmaker

from .. import clock, config
from ..context_builder import input_limit
from ..context_store import ContextStore
from ..llm import create_chat_model
from ..skills import read_prompt
from ..summarize import CATEGORY_NAMES, Source, WorkItem, extract_items
from ..tokens import estimate_tokens
from ..tracking.attribution import judge
from ..tracking.diff import ALGO, DiffResult, Differ, Hunk, format_hunk
from ..tracking.models import BASELINE, DELETED, UNKNOWN, FileChange, FileSnapshot, TrackedFile
from .models import (
    CONFIRMED,
    MERGED,
    PENDING,
    REJECTED,
    SPLIT,
    SUPERSEDED,
    EvidenceReview,
    WeeklyReport,
    WorkEvidence,
)

log = logging.getLogger(__name__)
EVIDENCE_ALGO = f"evidence-v1/{ALGO}"
HUNK_CHARS = 600  # 单段变化交给模型时的最大字数（超长的段落截断，原文仍在版本里）
MAX_QUOTES = 20  # 每条证据保存的原文引用数
ACTIVE_STATUSES = (PENDING, CONFIRMED, REJECTED)


class EvidenceDraft(BaseModel):
    """一条候选工作记录。"""

    title: str = Field(description="不超过 20 个字，以动词开头")
    description: str = Field(description="一到两句话，只写要点里有的内容")
    category: str = Field(description="done / in_progress / issue / plan / other")


@dataclass
class FilePlan:
    """一个文件在本周期内要比较的版本。"""

    file: TrackedFile
    base: FileSnapshot | None
    end: FileSnapshot
    chain: list[FileSnapshot]  # 基线之后、终点之前（含终点）的版本，用于发现中间有过的变化
    flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class BuildResult:
    created: list[int] = field(default_factory=list)
    unchanged: list[int] = field(default_factory=list)  # 指纹相同、未重新生成
    superseded: list[int] = field(default_factory=list)
    updated_reviewed: list[int] = field(default_factory=list)  # 已审核、有新变化的记录
    skipped: list[str] = field(default_factory=list)  # 没有变化的文件等说明
    calls: int = 0
    max_input_tokens: int = 0


def period_bounds(report: WeeklyReport) -> tuple[datetime, datetime]:
    if not config.TRACKING_TIMEZONE:
        return datetime.combine(report.period_start, time.min), datetime.combine(report.period_end, time.max)
    zone = clock.tzinfo()
    return (clock.system_naive(datetime.combine(report.period_start, time.min, tzinfo=zone)),
            clock.system_naive(datetime.combine(report.period_end, time.max, tzinfo=zone)))


def fingerprint(file_id: int, base: FileSnapshot | None, end: FileSnapshot) -> str:
    raw = json.dumps([EVIDENCE_ALGO, file_id, base.id if base else None, end.id])
    return hashlib.sha256(raw.encode()).hexdigest()


def batch_hunks(hunks: list[Hunk], budget: int) -> list[str]:
    """把变化段按 token 预算分批，每批是一段文本。超长的单段截断到 HUNK_CHARS。"""
    batches, current, size = [], [], 0
    for h in hunks:
        line = format_hunk(h, limit=HUNK_CHARS)
        cost = estimate_tokens(line) + 2
        if current and size + cost > budget:
            batches.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += cost
    if current:
        batches.append("\n".join(current))
    return batches


class EvidenceBuilder:
    def __init__(self, svc, model_factory: Callable[[], BaseChatModel] = create_chat_model, owner_names: list[str] | None = None) -> None:
        """svc：TrackingService；owner_names：你在文档里的名字（周报设置），归属判断用。"""
        self.svc = svc
        self.owner_names = owner_names or []
        self.differ = Differ(svc)
        self._model_factory = model_factory
        self._session = sessionmaker(svc.engine, expire_on_commit=False)
        self.cache = ContextStore(svc.engine)

    # ---- 规划：每个文件比较哪两个版本
    def plans(self, report: WeeklyReport) -> tuple[list[FilePlan], list[str]]:
        start, end = period_bounds(report)
        notes = []
        with self._session() as s:
            files = list(s.scalars(select(TrackedFile)))
            snaps = list(s.scalars(select(FileSnapshot).order_by(FileSnapshot.file_id, FileSnapshot.version)))
        by_file: dict[int, list[FileSnapshot]] = {}
        for snap in snaps:
            by_file.setdefault(snap.file_id, []).append(snap)
        plans = []
        for f in files:
            history = by_file.get(f.id, [])
            before = [x for x in history if x.captured_at < start]
            within = [x for x in history if start <= x.captured_at <= end]
            if not within:
                continue  # 本周期没有新版本
            flags: list[str] = []
            plan_notes: list[str] = []
            if before:
                base, chain = before[-1], within
            elif within[0].kind == BASELINE:
                base, chain = within[0], within[1:]  # 本周期内才开始追踪：只比较接手后的变化
                plan_notes.append(f"从 {within[0].captured_at:%m-%d %H:%M} 开始追踪，只比较之后的变化")
            elif f.moved_from_id:
                old_history = by_file.get(f.moved_from_id, [])
                prior = [x for x in old_history if x.captured_at <= within[0].captured_at]
                base, chain = (prior[-1], within) if prior else (None, within)
                flags.append("possibly_moved")
                plan_notes.append(f"可能由文件 {f.moved_from_id} 移动或改名而来，以原文件的版本为基线")
            else:
                base, chain = None, within
                flags.append("first_seen")
                plan_notes.append("本周期内首次发现的文件：无法确认创建时间和作者，整份内容作为候选")
            if not chain:
                continue
            if any(x.gap_before for x in chain):
                flags.append("gap")
                plan_notes.append("期间扫描有中断，部分变化被合并为一次，时间无法细分")
            plans.append(FilePlan(f, base, chain[-1], chain, flags, plan_notes))
        return plans, notes

    def _changes(self, plan: FilePlan) -> tuple[DiffResult, list[Hunk], list[str]]:
        """返回 (净变化, 用于生成描述的变化段, 额外标记)。净变化为空但中间版本有变化时，取中间的变化。"""
        net = self.differ.diff(plan.base, plan.end) if plan.base is None or plan.base.id != plan.end.id else None
        if net is not None and net.hunks:
            return net, net.hunks, []
        steps: list[Hunk] = []
        prev = plan.base
        for snap in plan.chain:
            if prev is not None and prev.id != snap.id:
                steps += self.differ.diff(prev, snap).hunks
            prev = snap
        if steps:
            return net or DiffResult("", None, plan.end.id), steps, ["reverted"]
        return net or DiffResult("", None, plan.end.id), [], []

    # ---- 生成
    def build(self, report: WeeklyReport) -> BuildResult:
        result = BuildResult()
        with self._session() as s:
            existing = list(
                s.scalars(select(WorkEvidence).where(WorkEvidence.report_id == report.id, WorkEvidence.origin == "generated"))
            )
        plans, _ = self.plans(report)
        model = None
        period = f"{report.period_start.isoformat()} 至 {report.period_end.isoformat()}"
        for plan in plans:
            fp = fingerprint(plan.file.id, plan.base, plan.end)
            same = next((e for e in existing if e.fingerprint == fp and e.review_status != SUPERSEDED), None)
            if same:
                result.unchanged.append(same.id)
                continue
            prior = [
                e
                for e in existing
                if e.review_status != SUPERSEDED and any(r.get("file_id") == plan.file.id for r in e.source_refs)
            ]
            reviewed = [e for e in prior if _is_reviewed(e)]
            if reviewed:
                # 已审核的不动；只为之后的新变化生成记录：基线换成已审核记录的终点版本
                last_end = max(r.get("to_snapshot_id") or 0 for e in reviewed for r in e.source_refs if r.get("file_id") == plan.file.id)
                if last_end == plan.end.id:
                    continue
                if last_end:
                    plan.base = self.svc.snapshot(last_end)
                    plan.chain = [x for x in plan.chain if x.version > plan.base.version]
                    if not plan.chain:
                        continue
                    plan.flags = [f for f in plan.flags if f != "first_seen"]
                    plan.notes.append("这是在你已审核的记录之后发生的新变化")
                fp = fingerprint(plan.file.id, plan.base, plan.end)
                if any(e.fingerprint == fp for e in existing if e.review_status != SUPERSEDED):
                    continue
            net, hunks, extra = self._changes(plan)
            flags = list(dict.fromkeys(plan.flags + extra + net.flags))
            if not hunks and not ("parse_failed" in flags or "format_only" in flags):
                result.skipped.append(f"《{_name(plan.file)}》内容没有变化")
                continue
            if model is None:
                model = self._model_factory()
            draft, quotes, calls, max_tokens = self._describe(model, plan, hunks, flags, period)
            draft.category, downgraded = guard_done(draft.category, hunks)
            if downgraded:
                flags.append("done_downgraded")
            result.calls += calls
            result.max_input_tokens = max(result.max_input_tokens, max_tokens)
            status, basis, attr_flags = judge(self.owner_names, plan.base.meta if plan.base else None, plan.end.meta)
            flags = list(dict.fromkeys(flags + attr_flags))
            if net.truncated:
                flags.append("truncated")
            ref = {
                "file_id": plan.file.id,
                "path": plan.file.path,
                "file_type": plan.file.file_type,
                "from_snapshot_id": plan.base.id if plan.base else None,
                "to_snapshot_id": plan.end.id,
                "from_version": plan.base.version if plan.base else None,
                "to_version": plan.end.version,
                "locations": list(dict.fromkeys(h.location for h in hunks if h.location))[:20],
                "quotes": quotes[:MAX_QUOTES],
                "stats": net.stats() if net.hunks else {"added": 0, "removed": 0, "modified": 0, "truncated": 0},
                "notes": plan.notes + net.notes,
            }
            evidence = WorkEvidence(
                report_id=report.id,
                title=draft.title[:200],
                description=draft.description,
                category=draft.category if draft.category in CATEGORY_NAMES else "other",
                source_refs=[ref],
                attribution=status,
                attribution_basis=basis,
                review_status=PENDING,
                flags=flags,
                fingerprint=fp,
            )
            with self._session() as s:
                s.add(evidence)
                s.flush()
                for e in prior:
                    if _is_reviewed(e):
                        s.execute(
                            update(WorkEvidence)
                            .where(WorkEvidence.id == e.id)
                            .values(has_update=True, related_ids=list(dict.fromkeys([*e.related_ids, evidence.id])))
                        )
                        result.updated_reviewed.append(e.id)
                    elif e.review_status == PENDING:
                        s.execute(update(WorkEvidence).where(WorkEvidence.id == e.id).values(review_status=SUPERSEDED))
                        s.add(EvidenceReview(evidence_id=e.id, report_id=report.id, action="supersede", after={"by": evidence.id}, note="文件有新变化，未审核的候选已替换"))
                        result.superseded.append(e.id)
                s.commit()
            existing.append(evidence)
            result.created.append(evidence.id)
        self._deleted_files(report, existing, result)
        self._dedupe(report, result)
        return result

    def _describe(
        self, model: BaseChatModel, plan: FilePlan, hunks: list[Hunk], flags: list[str], period: str
    ) -> tuple[EvidenceDraft, list[dict], int, int]:
        """返回 (标题描述, 原文引用, 调用次数, 单次最大输入 tokens)。"""
        name = _name(plan.file)
        if not hunks:
            reason = "解析失败，无法比较内容" if "parse_failed" in flags else "正文文字没有变化，可能只改了格式"
            return EvidenceDraft(title=f"修改《{name}》", description=f"文件有新版本，{reason}，请查看文件确认。", category="other"), [], 0, 0
        batches = batch_hunks(hunks, config.TRACKING_DIFF_BATCH_TOKENS)
        sources = [Source(plan.file.id, i, f"{name} 的变化", text) for i, text in enumerate(batches, start=1)]
        extraction = extract_items(model, sources, period=period, cache=self.cache, prompt="evidence_extract")
        calls = extraction.usage.calls
        max_tokens = max((estimate_tokens(b) + estimate_tokens(read_prompt("evidence_extract")) for b in batches), default=0)
        quotes = [{"point": i.point, "quote": i.quote, "category": i.category} for i in extraction.items]
        if extraction.failed:
            flags.append("extract_failed")
        if not extraction.items:
            return _fallback(name, hunks, plan), quotes, calls, max_tokens
        draft, used, tokens = self._compose(model, name, extraction.items, plan)
        return draft, quotes, calls + used, max(max_tokens, tokens)

    def _compose(self, model: BaseChatModel, name: str, items: list[WorkItem], plan: FilePlan) -> tuple[EvidenceDraft, int, int]:
        """按文件写标题和描述。要点过多时只给前面能放下的，并在输入里注明，描述只依据给出的要点。"""
        budget = min(config.TRACKING_DIFF_BATCH_TOKENS * 2, input_limit())
        lines, size, omitted = [], 0, 0
        for item in items:
            line = f"- 【{CATEGORY_NAMES.get(item.category, '其他')}】{item.point}"
            cost = estimate_tokens(line) + 2
            if lines and size + cost > budget:
                omitted += 1
                continue
            lines.append(line)
            size += cost
        context = "；".join(plan.notes) or "无"
        user = f"文件：《{name}》\n说明：{context}\n要点：\n" + "\n".join(lines)
        if omitted:
            user += f"\n（另有 {omitted} 条要点未列出，描述中不要提及未列出的内容）"
        messages = [("system", read_prompt("evidence_compose")), ("user", user)]
        tokens = sum(estimate_tokens(t) for _, t in messages)
        try:
            draft = model.with_structured_output(EvidenceDraft, method="function_calling").invoke(messages)
            if not isinstance(draft, EvidenceDraft) or not draft.title.strip():
                raise ValueError("输出格式不对")
            return draft, 1, tokens
        except Exception as e:
            log.warning("候选记录描述生成失败，改用要点拼接: %s", e)
            points = "；".join(i.point for i in items[:5])
            more = f"等 {len(items)} 项" if len(items) > 5 else ""
            return EvidenceDraft(title=f"修改《{name}》"[:20], description=f"{points}{more}。", category="other"), 1, tokens

    def _deleted_files(self, report: WeeklyReport, existing: list[WorkEvidence], result: BuildResult) -> None:
        """周期内被删除的文件：作为一条待确认的候选（删除文件本身未必是工作，由用户判断）。"""
        start, end = period_bounds(report)
        with self._session() as s:
            changes = list(
                s.scalars(
                    select(FileChange).where(
                        FileChange.change_type == DELETED,
                        FileChange.detected_at >= start,
                        FileChange.detected_at <= end,
                    )
                )
            )
        for ch in changes:
            fp = hashlib.sha256(json.dumps([EVIDENCE_ALGO, "deleted", ch.id]).encode()).hexdigest()
            if any(e.fingerprint == fp for e in existing):
                continue
            f = self.svc.file(ch.file_id)
            evidence = WorkEvidence(
                report_id=report.id,
                title=f"删除《{_name(f)}》"[:200],
                description=f"文件 {f.path} 在本周期内被删除（{ch.detected_at:%m-%d %H:%M} 扫描时发现）。删除本身未必是工作成果，请判断是否保留。",
                category="other",
                source_refs=[{"file_id": f.id, "path": f.path, "file_type": f.file_type, "from_snapshot_id": ch.from_snapshot_id, "to_snapshot_id": None, "change_id": ch.id, "locations": [], "quotes": [], "notes": [ch.details.get("basis", "")]}],
                attribution=UNKNOWN,
                attribution_basis=["文件删除没有作者信息"],
                flags=["deleted"],
                fingerprint=fp,
            )
            with self._session() as s:
                s.add(evidence)
                s.commit()
            existing.append(evidence)
            result.created.append(evidence.id)

    def _dedupe(self, report: WeeklyReport, result: BuildResult) -> None:
        """本次新生成、未审核的候选中，描述高度相似的（如同一内容的 md 和 docx 两份）合并为一条，保留各自的来源。"""
        with self._session() as s:
            fresh = list(s.scalars(select(WorkEvidence).where(WorkEvidence.id.in_(result.created), WorkEvidence.review_status == PENDING)))
            fresh = [e for e in fresh if "deleted" not in e.flags]
            merged: set[int] = set()
            for i, a in enumerate(fresh):
                if a.id in merged:
                    continue
                for b in fresh[i + 1 :]:
                    if b.id in merged:
                        continue
                    text_a, text_b = f"{a.title}{a.description}", f"{b.title}{b.description}"
                    if difflib.SequenceMatcher(None, text_a, text_b).ratio() < 0.85:
                        continue
                    a.source_refs = [*a.source_refs, *b.source_refs]
                    a.flags = list(dict.fromkeys([*a.flags, *b.flags]))
                    a.attribution_basis = [*a.attribution_basis, *(f"《{_ref_name(b)}》：{x}" for x in b.attribution_basis)]
                    if a.attribution != b.attribution:
                        a.attribution = UNKNOWN if UNKNOWN in (a.attribution, b.attribution) else a.attribution
                    a.related_ids = [*a.related_ids, b.id]
                    b.review_status, b.related_ids = MERGED, [*b.related_ids, a.id]
                    s.add(EvidenceReview(evidence_id=b.id, report_id=report.id, action="auto_merge", after={"into": a.id}, note="与另一条候选高度相似，自动合并"))
                    merged.add(b.id)
            s.commit()
        result.created = [x for x in result.created if x not in merged]


DONE_WORDS = ("完成", "已上线", "上线了", "已修复", "修复了", "已解决", "解决了", "已发布", "发布了", "已交付", "已提交", "已通过", "已关闭")


def guard_done(category: str, hunks: list[Hunk]) -> tuple[str, bool]:
    """文件被修改不代表任务完成：变化内容里没有明确的完成表述时，done 降为 other。返回 (类别, 是否降级)。"""
    if category != "done":
        return category, False
    added = " ".join(h.new for h in hunks if h.op != "removed")
    return ("done", False) if any(w in added for w in DONE_WORDS) else ("other", True)


def _is_reviewed(e: WorkEvidence) -> bool:
    return e.review_status in (CONFIRMED, REJECTED, MERGED, SPLIT) or e.user_edited


def _name(f: TrackedFile) -> str:
    return f.path.replace("\\", "/").rsplit("/", 1)[-1]


def _ref_name(e: WorkEvidence) -> str:
    return e.source_refs[0]["path"].replace("\\", "/").rsplit("/", 1)[-1] if e.source_refs else "?"


def _fallback(name: str, hunks: list[Hunk], plan: FilePlan) -> EvidenceDraft:
    """模型没有提出要点：只用统计和位置描述，不编造内容。"""
    counts = {op: sum(1 for h in hunks if h.op == op) for op in ("added", "modified", "removed")}
    parts = [f"{label} {counts[op]} 处" for op, label in (("added", "新增"), ("modified", "修改"), ("removed", "删除")) if counts[op]]
    where = list(dict.fromkeys(h.location for h in hunks if h.location))[:3]
    where_text = f"，涉及 {'、'.join(where)}" if where else ""
    verb = "新出现" if "first_seen" in plan.flags else "修改"
    return EvidenceDraft(
        title=f"{verb}《{name}》"[:20],
        description=f"《{name}》{'、'.join(parts)}{where_text}。未能自动概括具体内容，请查看变化后补充描述。",
        category="other",
    )
