"""意图判断与纠正记录：每条消息走了哪一层、判成什么、分数多少、助理实际做了什么、用户有没有纠正。

只存本机业务库（单独一张表），不外发、不进 Git；用于以后按真实说法调漏斗的规则和门槛。
原文默认不存，只存长度和命中的规则（判断原因）；BRIEFLY_INTENT_LOG_TEXT=on 时才存原文。
BRIEFLY_INTENT_LOG=off 时不记录。

命令行：briefly intent-log export [-o 文件] [--since 日期]，导出成 tests/eval/intent_eval.py 能读的用例（标注留空）。
"""
import argparse
import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from sqlalchemy import JSON, Boolean, DateTime, Engine, Float, Integer, String, Text, select, update
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker

from . import config
from .db import Base
from .intent import TurnState


class IntentLogEntry(Base):
    __tablename__ = "intent_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    thread_id: Mapped[str] = mapped_column(String(64), index=True)
    message_id: Mapped[str] = mapped_column(String(64), index=True)  # 用户消息 ID，纠正时按它找到原记录
    layer: Mapped[int] = mapped_column(Integer)
    intent: Mapped[str] = mapped_column(String(32))  # 漏斗的判断（intent.intent）
    reason: Mapped[str] = mapped_column(String(200))  # 命中的规则或判断原因，不含原文
    confidence: Mapped[float | None] = mapped_column(Float)
    gap: Mapped[float | None] = mapped_column(Float)  # 语义层第一名与第二名的差距
    kb_score: Mapped[float | None] = mapped_column(Float)  # 知识库路由相似度（未达下限也记）
    semantic: Mapped[dict | None] = mapped_column(JSON)  # 语义层第一名 {intent, example, score, gap}
    resolved: Mapped[str | None] = mapped_column(String(32))  # 轮次结果记录写回的实际能力（turn_record.py）
    skill: Mapped[str | None] = mapped_column(String(32))  # 用户显式指定的能力
    state: Mapped[dict] = mapped_column(JSON, default=dict)  # 判断时的会话状态（TurnState）
    skip_funnel: Mapped[bool] = mapped_column(Boolean, default=False)  # 用户点了“交给助理处理”后的重发
    corrects: Mapped[str | None] = mapped_column(String(64))  # 重发纠正的是哪条用户消息
    corrected: Mapped[bool] = mapped_column(Boolean, default=False)  # 这条判断事后被用户纠正
    text_length: Mapped[int] = mapped_column(Integer)
    text: Mapped[str | None] = mapped_column(Text)  # 只在 INTENT_LOG_TEXT 开启时存
    matched: Mapped[str | None] = mapped_column(String(200))  # 规则层命中的原文片段，同样只在存原文时记


class IntentLog:
    def __init__(self, engine: Engine) -> None:
        self._session = sessionmaker(engine, expire_on_commit=False)

    def record(self, thread_id: str, message_id: str, text: str, intent: dict, state: TurnState,
               skill: str | None = None, corrects: str | None = None) -> None:
        """intent 为用户消息元数据里的 intent（含 resolved）。纠正重发时 corrects 为原消息 ID，同时把原记录标为已纠正。"""
        if not config.INTENT_LOG:
            return
        keep = config.INTENT_LOG_TEXT
        semantic = intent.get("semantic")
        entry = IntentLogEntry(
            thread_id=thread_id, message_id=message_id, layer=intent["layer"], intent=intent["intent"],
            reason=intent.get("reason") or "", confidence=intent.get("confidence"),
            gap=semantic["gap"] if semantic else None, kb_score=intent.get("kb_score"), semantic=semantic,
            resolved=intent.get("resolved"), skill=skill,
            state=asdict(state), skip_funnel=bool(intent.get("skip_funnel")), corrects=corrects,
            text_length=len(text), text=text if keep else None, matched=intent.get("matched") if keep else None)
        with self._session() as s:
            s.add(entry)
            if corrects:
                s.execute(update(IntentLogEntry).where(IntentLogEntry.message_id == corrects).values(corrected=True))
            s.commit()

    def entries(self, since: datetime | None = None) -> list[IntentLogEntry]:
        query = select(IntentLogEntry).order_by(IntentLogEntry.id)
        if since is not None:
            query = query.where(IntentLogEntry.created_at >= since)
        with self._session() as s:
            return list(s.scalars(query))

    def export_cases(self, since: datetime | None = None) -> tuple[list[dict], int]:
        """导出成 intent_eval.py 的用例格式，期望意图留空待人工标注。

        跳过纠正重发（那是用户的指令，不是漏斗判断）和没存原文的记录；返回 (用例, 因没有原文跳过的条数)。
        """
        cases, skipped = [], 0
        for e in self.entries(since):
            if e.skip_funnel:
                continue
            if e.text is None:
                skipped += 1
                continue
            state = e.state or {}
            note = "用户点了“交给助理处理”纠正了这次判断" if e.corrected else ""
            cases.append(dict(
                id=f"log-{e.id}", text=e.text,
                state=dict(kb_selected=bool(state.get("kb_id")), has_materials=bool(state.get("has_materials")), previous_turn=None),
                turn_state=dict(earlier_materials=bool(state.get("earlier_materials")),
                                has_previous_reply=bool(state.get("has_previous_reply")), last_intent=state.get("last_intent")),
                expected_intent="", acceptable_intents=[], difficulty=None, note=note,
                logged=dict(time=e.created_at.isoformat(timespec="seconds"), intent=e.intent, layer=e.layer,
                            confidence=e.confidence, gap=e.gap, kb_score=e.kb_score, resolved=e.resolved,
                            corrected=e.corrected)))
        return cases, skipped


def main(argv: list[str] | None = None) -> int:
    from .db import create_db_engine

    ap = argparse.ArgumentParser(prog="briefly intent-log", description="意图判断与纠正记录（只在本机）")
    sub = ap.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export", help="导出成 intent_eval.py 的用例格式（JSONL，期望意图留空待标注）")
    export.add_argument("-o", "--output", help="写入文件，默认打印")
    export.add_argument("--since", type=datetime.fromisoformat, help="只导出此时间之后的记录，如 2026-11-01")
    args = ap.parse_args(argv)

    engine = create_db_engine()
    try:
        cases, skipped = IntentLog(engine).export_cases(args.since)
    finally:
        engine.dispose()
    lines = "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in cases)
    if args.output:
        Path(args.output).write_text(lines, encoding="utf-8")
    else:
        sys.stdout.write(lines)
    note = f"，{skipped} 条没有存原文已跳过（设置 BRIEFLY_INTENT_LOG_TEXT=on 后的新记录才有原文）" if skipped else ""
    print(f"导出 {len(cases)} 条{note}", file=sys.stderr)
    return 0
