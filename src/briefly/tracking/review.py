"""Human-in-the-loop：生成周报前审核候选工作记录。

所有操作都写审核记录（EvidenceReview，记录操作前后的内容，只追加）。
- 确认 → CONFIRMED，归属 USER_CONFIRMED；排除 → REJECTED，归属 EXCLUDED；撤销 → 回到 PENDING。
- 修改描述不等于确认；用户补充的记录是用户自己写的，直接算确认。
- 合并、拆分产生新的待确认记录，原记录标 MERGED / SPLIT 保留以便追溯。
- 还有待确认的记录时不能确认清单（可以随时退出，草稿保留）；清单确认后再修改，清单回到草稿状态。
"""
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from langchain_core.language_models import BaseChatModel
from sqlalchemy import select, update
from sqlalchemy.orm import sessionmaker

from ..llm import create_chat_model
from ..summarize import CATEGORY_NAMES
from .evidence import EvidenceBuilder, FilePlan
from .models import (
    CHECKLIST_CONFIRMED,
    CONFIRMED,
    DRAFT,
    EXCLUDED,
    MERGED,
    PENDING,
    REJECTED,
    SPLIT,
    USER_CONFIRMED,
    EvidenceReview,
    ReportPrep,
    WorkEvidence,
)

VISIBLE = (PENDING, CONFIRMED, REJECTED)
FIELDS = ("title", "description", "category", "review_status", "attribution", "source_refs", "flags")


def snapshot_of(e: WorkEvidence) -> dict:
    return {k: getattr(e, k) for k in FIELDS}


@dataclass
class SourceView:
    """一条记录的依据：文件、版本、位置、原文、变化。"""

    path: str
    versions: str
    captured: str
    locations: list[str]
    quotes: list[dict]
    hunks: list[str]
    notes: list[str]


class ReviewService:
    def __init__(self, svc, model_factory: Callable[[], BaseChatModel] = create_chat_model) -> None:
        self.svc = svc
        self._model_factory = model_factory
        self._session = sessionmaker(svc.engine, expire_on_commit=False)

    # ---- 查询
    def prep(self, prep_id: int) -> ReportPrep:
        with self._session() as s:
            prep = s.get(ReportPrep, prep_id)
        if prep is None:
            raise ValueError(f"周报准备 {prep_id} 不存在")
        return prep

    def items(self, prep_id: int, all_statuses: bool = False) -> list[WorkEvidence]:
        query = select(WorkEvidence).where(WorkEvidence.prep_id == prep_id).order_by(WorkEvidence.id)
        if not all_statuses:
            query = query.where(WorkEvidence.review_status.in_(VISIBLE))
        with self._session() as s:
            return list(s.scalars(query))

    def get(self, evidence_id: int, prep_id: int | None = None) -> WorkEvidence:
        with self._session() as s:
            e = s.get(WorkEvidence, evidence_id)
        if e is None or (prep_id is not None and e.prep_id != prep_id):
            raise ValueError(f"记录 E{evidence_id} 不存在")
        return e

    def history(self, prep_id: int) -> list[EvidenceReview]:
        with self._session() as s:
            return list(s.scalars(select(EvidenceReview).where(EvidenceReview.prep_id == prep_id).order_by(EvidenceReview.id)))

    def sources(self, evidence_id: int, hunk_limit: int = 20) -> list[SourceView]:
        from .diff import Differ, format_hunk

        e = self.get(evidence_id)
        differ = Differ(self.svc)
        views = []
        for ref in e.source_refs:
            hunks: list[str] = []
            captured = ""
            to_id, from_id = ref.get("to_snapshot_id"), ref.get("from_snapshot_id")
            if to_id:
                end = self.svc.snapshot(to_id)
                base = self.svc.snapshot(from_id) if from_id else None
                captured = f"{base.captured_at:%m-%d %H:%M} → " if base else ""
                captured += f"{end.captured_at:%m-%d %H:%M}"
                result = differ.diff(base, end)
                hunks = [format_hunk(h, limit=200) for h in result.hunks[:hunk_limit]]
                if len(result.hunks) > hunk_limit:
                    hunks.append(f"……另有 {len(result.hunks) - hunk_limit} 处变化")
            versions = f"v{ref.get('from_version') or '-'} → v{ref.get('to_version') or '-'}"
            views.append(
                SourceView(ref.get("path", ""), versions, captured, ref.get("locations", []), ref.get("quotes", []), hunks, ref.get("notes", []))
            )
        return views

    # ---- 审核操作
    def confirm(self, prep_id: int, ids: list[int], note: str = "") -> None:
        self._set_status(prep_id, ids, CONFIRMED, USER_CONFIRMED, "confirm", note)

    def exclude(self, prep_id: int, ids: list[int], note: str = "") -> None:
        self._set_status(prep_id, ids, REJECTED, EXCLUDED, "exclude", note)

    def reset(self, prep_id: int, ids: list[int]) -> None:
        """撤销确认或排除，回到待确认；归属恢复为系统判断（从审核记录里找回）。"""
        for evidence_id in ids:
            e = self._editable(prep_id, evidence_id)
            original = self._system_attribution(e)
            self._apply(e, {"review_status": PENDING, "attribution": original}, "reset", "")

    def edit(self, prep_id: int, evidence_id: int, title: str | None = None, description: str | None = None, category: str | None = None) -> WorkEvidence:
        e = self._editable(prep_id, evidence_id)
        values: dict = {"user_edited": True}
        if title is not None and title.strip():
            values["title"] = title.strip()[:200]
        if description is not None and description.strip():
            values["description"] = description.strip()
        if category is not None:
            if category not in CATEGORY_NAMES:
                raise ValueError(f"类别只能是 {', '.join(CATEGORY_NAMES)}")
            values["category"] = category
        if len(values) == 1:
            raise ValueError("没有要修改的内容")
        return self._apply(e, values, "edit", "")

    def add(self, prep_id: int, title: str, description: str = "", category: str = "other") -> WorkEvidence:
        """补充文件无法体现的工作（会议、沟通、线下工作等）：用户自己写的，直接算确认。"""
        if not title.strip():
            raise ValueError("标题不能为空")
        if category not in CATEGORY_NAMES:
            raise ValueError(f"类别只能是 {', '.join(CATEGORY_NAMES)}")
        e = WorkEvidence(
            prep_id=prep_id,
            title=title.strip()[:200],
            description=description.strip(),
            category=category,
            origin="manual",
            attribution=USER_CONFIRMED,
            attribution_basis=["用户补充"],
            review_status=CONFIRMED,
            user_edited=True,
        )
        with self._session() as s:
            s.add(e)
            s.flush()
            s.add(EvidenceReview(evidence_id=e.id, prep_id=prep_id, action="add", after=snapshot_of(e)))
            self._reopen(s, prep_id)
            s.commit()
        return e

    def merge(self, prep_id: int, ids: list[int], title: str | None = None, description: str | None = None) -> WorkEvidence:
        if len(set(ids)) < 2:
            raise ValueError("至少选择两条记录合并")
        olds = [self._editable(prep_id, i) for i in dict.fromkeys(ids)]
        refs = [r for e in olds for r in e.source_refs]
        attrs = {e.attribution for e in olds}
        merged = WorkEvidence(
            prep_id=prep_id,
            title=(title or olds[0].title).strip()[:200],
            description=(description or "；".join(e.description.rstrip("。") for e in olds if e.description) + "。").strip(),
            category=olds[0].category,
            origin="merged",
            source_refs=refs,
            attribution=attrs.pop() if len(attrs) == 1 else "UNKNOWN",
            attribution_basis=[f"E{e.id}：{b}" for e in olds for b in e.attribution_basis],
            review_status=PENDING,
            flags=list(dict.fromkeys(f for e in olds for f in e.flags)),
            user_edited=True,
            related_ids=[e.id for e in olds],
        )
        with self._session() as s:
            s.add(merged)
            s.flush()
            for e in olds:
                s.execute(update(WorkEvidence).where(WorkEvidence.id == e.id).values(review_status=MERGED, related_ids=[*e.related_ids, merged.id]))
                s.add(EvidenceReview(evidence_id=e.id, prep_id=prep_id, action="merge", before=snapshot_of(e), after={"into": merged.id}))
            s.add(EvidenceReview(evidence_id=merged.id, prep_id=prep_id, action="merge", after=snapshot_of(merged), note=f"由 {', '.join(f'E{e.id}' for e in olds)} 合并"))
            self._reopen(s, prep_id)
            s.commit()
        return merged

    def split(self, prep_id: int, evidence_id: int, parts: list[tuple[str, str]]) -> list[WorkEvidence]:
        """拆成几条：每条都保留原记录的全部来源（用户可再编辑）。"""
        parts = [(t.strip(), d.strip()) for t, d in parts if t.strip()]
        if len(parts) < 2:
            raise ValueError("至少拆成两条")
        old = self._editable(prep_id, evidence_id)
        news = [
            WorkEvidence(
                prep_id=prep_id,
                title=t[:200],
                description=d,
                category=old.category,
                origin="split",
                source_refs=old.source_refs,
                attribution=old.attribution,
                attribution_basis=old.attribution_basis,
                review_status=PENDING,
                flags=old.flags,
                user_edited=True,
                related_ids=[old.id],
            )
            for t, d in parts
        ]
        with self._session() as s:
            s.add_all(news)
            s.flush()
            s.execute(update(WorkEvidence).where(WorkEvidence.id == old.id).values(review_status=SPLIT, related_ids=[*old.related_ids, *(n.id for n in news)]))
            s.add(EvidenceReview(evidence_id=old.id, prep_id=prep_id, action="split", before=snapshot_of(old), after={"into": [n.id for n in news]}))
            for n in news:
                s.add(EvidenceReview(evidence_id=n.id, prep_id=prep_id, action="split", after=snapshot_of(n), note=f"由 E{old.id} 拆分"))
            self._reopen(s, prep_id)
            s.commit()
        return news

    def regenerate(self, prep_id: int, evidence_id: int, force: bool = False) -> WorkEvidence:
        """按来源重新生成描述（重新比较版本、重新写描述）。用户改过的描述需要 force 才覆盖。"""
        e = self._editable(prep_id, evidence_id)
        if e.user_edited and not force:
            raise ValueError("这条记录的描述你修改过，重新生成会覆盖；确定的话请使用强制重新生成")
        if e.origin == "manual" or not e.source_refs or not e.source_refs[0].get("to_snapshot_id"):
            raise ValueError("这条记录没有可用的文件版本，不能重新生成")
        builder = EvidenceBuilder(self.svc, self._model_factory)
        descriptions = []
        title, category = e.title, e.category
        for ref in e.source_refs:
            if not ref.get("to_snapshot_id"):
                continue
            f = self.svc.file(ref["file_id"])
            base = self.svc.snapshot(ref["from_snapshot_id"]) if ref.get("from_snapshot_id") else None
            end = self.svc.snapshot(ref["to_snapshot_id"])
            plan = FilePlan(f, base, end, [end], list(e.flags), list(ref.get("notes", [])))
            _, hunks, _ = builder._changes(plan)
            draft, quotes, _, _ = builder._describe(self._model_factory(), plan, hunks, list(e.flags), self._period(prep_id))
            ref["quotes"] = quotes[:20]
            descriptions.append(draft.description)
            if len(e.source_refs) == 1:
                title, category = draft.title, draft.category
        values = {"title": title[:200], "description": "；".join(d.rstrip("。") for d in descriptions) + "。", "category": category, "source_refs": e.source_refs, "user_edited": False}
        return self._apply(e, values, "regenerate", "")

    def confirm_checklist(self, prep_id: int) -> ReportPrep:
        pending = [e for e in self.items(prep_id) if e.review_status == PENDING]
        if pending:
            raise ValueError(f"还有 {len(pending)} 条记录待确认（{', '.join(f'E{e.id}' for e in pending[:10])}），请先确认或排除；也可以退出，草稿会保留")
        confirmed = [e for e in self.items(prep_id) if e.review_status == CONFIRMED]
        with self._session() as s:
            s.execute(update(ReportPrep).where(ReportPrep.id == prep_id).values(status=CHECKLIST_CONFIRMED, confirmed_at=datetime.now()))
            s.add(EvidenceReview(prep_id=prep_id, action="confirm_checklist", after={"confirmed": [e.id for e in confirmed]}, note=f"确认清单：{len(confirmed)} 条工作记录"))
            s.commit()
        from ..todos.evidence import TodoEvidence

        TodoEvidence(self.svc.engine, self.svc.store).freeze(prep_id)  # 冻结关联待办的审核快照
        return self.prep(prep_id)

    # ---- 内部
    def _period(self, prep_id: int) -> str:
        prep = self.prep(prep_id)
        return f"{prep.period_start.isoformat()} 至 {prep.period_end.isoformat()}"

    def _editable(self, prep_id: int, evidence_id: int) -> WorkEvidence:
        e = self.get(evidence_id, prep_id)
        if e.review_status not in VISIBLE:
            raise ValueError(f"记录 E{evidence_id} 已被合并、拆分或替换，不能再修改")
        return e

    def _system_attribution(self, e: WorkEvidence) -> str:
        if e.origin == "manual":
            return USER_CONFIRMED
        with self._session() as s:
            rows = s.scalars(select(EvidenceReview).where(EvidenceReview.evidence_id == e.id).order_by(EvidenceReview.id)).all()
        for r in rows:
            if r.before.get("attribution") not in (None, USER_CONFIRMED, EXCLUDED):
                return r.before["attribution"]
        return e.attribution if e.attribution not in (USER_CONFIRMED, EXCLUDED) else "UNKNOWN"

    def _set_status(self, prep_id: int, ids: list[int], status: str, attribution: str, action: str, note: str) -> None:
        if not ids:
            raise ValueError("请指定记录编号")
        targets = [self._editable(prep_id, i) for i in dict.fromkeys(ids)]
        for e in targets:
            self._apply(e, {"review_status": status, "attribution": attribution}, action, note)

    def _apply(self, e: WorkEvidence, values: dict, action: str, note: str) -> WorkEvidence:
        before = snapshot_of(e)
        with self._session() as s:
            s.execute(update(WorkEvidence).where(WorkEvidence.id == e.id).values(**values))
            fresh = s.get(WorkEvidence, e.id)
            s.refresh(fresh)
            s.add(EvidenceReview(evidence_id=e.id, prep_id=e.prep_id, action=action, before=before, after=snapshot_of(fresh), note=note))
            self._reopen(s, e.prep_id)
            s.commit()
            return fresh

    def _reopen(self, s, prep_id: int) -> None:
        """清单确认后又有修改：回到草稿，需要重新确认。"""
        prep = s.get(ReportPrep, prep_id)
        if prep is not None and prep.status != DRAFT:
            prep.status = DRAFT
            s.add(EvidenceReview(prep_id=prep_id, action="reopen", note="清单确认后有修改，回到草稿"))
