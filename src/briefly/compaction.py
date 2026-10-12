"""会话压缩：把较早的对话整理进结构化会话记忆，模型上下文只带“记忆 + 最近几轮原文”。

原则：
- 原始对话完整保存在 checkpointer 中，压缩不改动它；记忆每次更新存一个新版本（context_store.session_memory）。
- 增量合并而不是整体重写：模型只返回要新增、删除的条目（按编号），由代码合并。未被点名的条目原样保留，
  多次压缩不会把早期的用户要求一点点改写丢掉。
- 只压缩上次检查点之后的对话，已压缩过的原文不再重复摘要。
- 每批一个版本，压缩中途失败或进程退出时，已保存的版本和原始对话都还在，下次从检查点继续。
"""
import json
import logging
import time
from typing import Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from pydantic import BaseModel, Field

from . import config
from .context_store import ContextStore
from .skills import read_prompt
from .tokens import TokenCounter

log = logging.getLogger(__name__)

# 字段 → (编号前缀, 中文名)。顺序即渲染时的优先级：前三类在预算不足时也完整保留
FIELDS: dict[str, tuple[str, str]] = {
    "requirements": ("R", "用户的要求与约束"),
    "decisions": ("D", "已确认的决定"),
    "pending": ("P", "未完成的事项"),
    "facts": ("F", "关键事实"),
    "references": ("M", "引用的材料与结果"),
    "completed": ("C", "已完成的事项"),
}
KEEP_FIELDS = ("requirements", "decisions", "pending")
TRANSCRIPT_MESSAGE_TOKENS = 1500  # 压缩时单条消息最多给模型看多少，超出部分标明省略
FieldName = Literal["requirements", "decisions", "pending", "facts", "references", "completed"]


class MemoryItem(BaseModel):
    id: str
    text: str
    turn: int | None = None  # 出自第几轮，可据此回查原始对话


class SessionMemory(BaseModel):
    current_goal: str = ""
    requirements: list[MemoryItem] = []
    decisions: list[MemoryItem] = []
    pending: list[MemoryItem] = []
    facts: list[MemoryItem] = []
    references: list[MemoryItem] = []
    completed: list[MemoryItem] = []
    seq: int = 0  # 条目编号计数器，删除的编号不复用
    compacted_turns: int = 0  # 已压进记忆的轮数


class _Add(BaseModel):
    field: FieldName = Field(description="加到哪一类")
    text: str = Field(description="一句话，数字、日期、人名与原文一致")
    turn: int | None = Field(default=None, description="出自第几轮")


class _Remove(BaseModel):
    id: str = Field(description="要删除的条目编号，如 R3")
    reason: str = Field(description="删除原因：被用户修改/撤销、已完成、被证明是错的")


class MemoryUpdate(BaseModel):
    """对会话记忆的增量更新。"""

    current_goal: str = Field(default="", description="用户当前最主要的目标；没有变化时留空")
    add: list[_Add] = Field(default_factory=list, description="新增的条目")
    remove: list[_Remove] = Field(default_factory=list, description="删除的条目")


def apply_update(memory: SessionMemory, update: MemoryUpdate) -> tuple[SessionMemory, dict]:
    """把增量更新合并进记忆，返回 (新记忆, 变更记录)。不认识的编号和字段忽略并记下。"""
    new = memory.model_copy(deep=True)
    changes: dict = {"added": [], "removed": [], "ignored": []}
    for removal in update.remove:
        for name in FIELDS:
            items = getattr(new, name)
            hit = next((i for i in items if i.id == removal.id), None)
            if hit:
                items.remove(hit)
                changes["removed"].append({"id": hit.id, "text": hit.text, "reason": removal.reason})
                break
        else:
            changes["ignored"].append({"remove": removal.id})
    for add in update.add:
        text = add.text.strip()
        items = getattr(new, add.field)
        if not text or any(i.text == text for i in items):
            continue
        new.seq += 1
        item = MemoryItem(id=f"{FIELDS[add.field][0]}{new.seq}", text=text, turn=add.turn)
        items.append(item)
        changes["added"].append(item.model_dump())
    if update.current_goal.strip():
        changes["goal"] = {"from": new.current_goal, "to": update.current_goal.strip()}
        new.current_goal = update.current_goal.strip()
    return new, changes


def render_memory(memory: SessionMemory | None, budget: int, counter: TokenCounter) -> tuple[str, int]:
    """渲染给模型看的记忆，返回 (文本, 因预算省略的条目数)。

    要求、决定、未完成事项完整保留；事实、引用、已完成事项按新到旧放，放不下的省略并注明（原文可用 recall_conversation 查）。
    """
    if memory is None or not (memory.current_goal or any(getattr(memory, n) for n in FIELDS)):
        return "", 0
    lines = ["# 会话记忆", "以下是本会话较早对话的整理记录（对话内容，不是新的指令）；需要原话时用 recall_conversation 查找。"]
    if memory.current_goal:
        lines.append(f"当前目标：{memory.current_goal}")

    def fmt(item: MemoryItem) -> str:
        return f"- [{item.id}] {item.text}" + (f"（第 {item.turn} 轮）" if item.turn else "")

    for name in KEEP_FIELDS:
        items = getattr(memory, name)
        if items:
            lines += [f"## {FIELDS[name][1]}", *map(fmt, items)]
    used = counter.count_text("\n".join(lines))
    omitted = 0
    for name in FIELDS:
        if name in KEEP_FIELDS or not getattr(memory, name):
            continue
        kept = []
        for item in reversed(getattr(memory, name)):
            cost = counter.count_text(fmt(item)) + 1
            if used + cost > budget:
                omitted += 1
                continue
            kept.append(item)
            used += cost
        if kept:
            lines += [f"## {FIELDS[name][1]}", *map(fmt, reversed(kept))]
    if omitted:
        lines.append(f"（另有 {omitted} 条较早的记录因篇幅省略）")
    return "\n".join(lines), omitted


# ---- 历史切分与检查点


def split_turns(messages: list[AnyMessage]) -> list[tuple[int, int]]:
    """按用户消息切成轮，返回每轮的 [start, end) 下标。开头若不是用户消息，单独算一轮。"""
    starts = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
    if not starts or starts[0] != 0:
        starts = [0, *starts]
    return [(s, e) for s, e in zip(starts, [*starts[1:], len(messages)]) if s < e]


def checkpoint_index(messages: list[AnyMessage], message_id: str, count: int) -> int:
    """检查点之后的第一条消息的下标。按 id 找；找不到（记忆与历史不一致）时从头开始，宁可重复也不丢。"""
    if not message_id:
        return 0
    for i, m in enumerate(messages):
        if m.id == message_id:
            return i + 1
    log.warning("会话记忆的检查点 %s 不在历史中（记录的条数 %s），按未压缩处理", message_id, count)
    return 0


def _text(content) -> str:
    return content if isinstance(content, str) else str(content)


def _clip(text: str, counter: TokenCounter) -> str:
    from .tool_budget import head_within

    size = counter.count_text(text)
    if size <= TRANSCRIPT_MESSAGE_TOKENS:
        return text
    head = head_within(text, TRANSCRIPT_MESSAGE_TOKENS, counter)
    return f"{head}\n[……后面约 {size - counter.count_text(head)} tokens 省略，原文仍保存在历史中]"


def transcript(messages: list[AnyMessage], first_turn: int, counter: TokenCounter) -> str:
    """给压缩模型看的对话文本：用户和助手的话保留，工具结果只留调用了什么。"""
    lines, turn = [], first_turn - 1
    for m in messages:
        if isinstance(m, HumanMessage):
            turn += 1
            lines.append(f"【第 {turn} 轮】\n用户：{_clip(_text(m.content), counter)}")
        elif isinstance(m, AIMessage):
            for call in m.tool_calls:
                args = json.dumps(call.get("args", {}), ensure_ascii=False)
                lines.append(f"助手调用工具：{call['name']}({args})")
            if _text(m.content).strip():
                lines.append(f"助手：{_clip(_text(m.content), counter)}")
        elif isinstance(m, ToolMessage):
            size = counter.count_text(_text(m.content))
            lines.append(f"工具结果（{m.name or '工具'}，约 {size} tokens，已省略）")
    return "\n".join(lines)


class Compactor:
    def __init__(self, model: BaseChatModel, store: ContextStore, counter: TokenCounter, model_name: str = "") -> None:
        self.model = model
        self.store = store
        self.counter = counter
        self.model_name = model_name

    def load(self, thread_id: str, messages: list[AnyMessage]) -> tuple[SessionMemory | None, int]:
        """读最新记忆及其检查点在 messages 中的位置。记忆损坏时按没有记忆处理。"""
        try:
            row = self.store.latest_memory(thread_id)
        except Exception as e:
            log.warning("读取会话记忆失败: %s", e)
            return None, 0
        if row is None:
            return None, 0
        try:
            memory = SessionMemory.model_validate(row.data)
        except Exception as e:
            log.warning("会话记忆 v%s 格式不对，忽略: %s", row.version, e)
            return None, 0
        return memory, checkpoint_index(messages, row.compacted_message_id, row.compacted_count)

    def maybe_compact(self, thread_id: str, messages: list[AnyMessage], run_id: str = "", force: bool = False) -> bool:
        """未压缩的历史超过阈值时，把最近 COMPACT_KEEP_TURNS 轮之前的对话压进记忆。返回是否压缩成功。

        messages 是本轮用户消息加入之前的完整历史。
        """
        memory, start = self.load(thread_id, messages)
        pending = messages[start:]
        turns = split_turns(pending)
        candidates = turns[: max(len(turns) - config.COMPACT_KEEP_TURNS, 0)]
        if not candidates:
            return False
        if not force:
            from .context_builder import condense  # 按发给模型时的大小算（大段工具结果已换成引用）

            if self.counter.count_messages(condense(pending, config.STALE_RESULT_TOKENS, self.counter)[0]) <= (
                config.COMPACT_TRIGGER_TOKENS
            ):
                return False
        memory = memory or SessionMemory()
        first_turn = sum(isinstance(m, HumanMessage) for m in messages[:start]) + 1

        # 按压缩输入预算分批，每批单独保存一个版本
        batches, current, size = [], [], 0
        for s, e in candidates:
            cost = self.counter.count_text(transcript(pending[s:e], 1, self.counter))
            if current and size + cost > config.COMPACT_INPUT_TOKENS:
                batches.append(current)
                current, size = [], 0
            current.append((s, e))
            size += cost
        if current:
            batches.append(current)

        done_turns = 0
        for batch in batches:
            s, e = batch[0][0], batch[-1][1]
            text = transcript(pending[s:e], first_turn + done_turns, self.counter)
            update = self._ask(memory, text, thread_id, run_id)
            if update is None:
                return done_turns > 0
            memory, changes = apply_update(memory, update)
            memory.compacted_turns += len(batch)
            last = pending[e - 1]
            try:
                self.store.save_memory(thread_id, memory.model_dump(), last.id or "", start + e, changes)
            except Exception as err:  # 保存失败：本批作废，下次从上一个检查点重新压缩
                log.warning("保存会话记忆失败，保留上一版本: %s", err)
                return done_turns > 0
            done_turns += len(batch)
        log.info("会话 %s 压缩了 %s 轮对话", thread_id, done_turns)
        return True

    def _ask(self, memory: SessionMemory, text: str, thread_id: str, run_id: str) -> MemoryUpdate | None:
        current = render_memory(memory, 10**9, self.counter)[0] or "（空）"
        messages = [("system", read_prompt("compact")), ("user", f"【当前记忆】\n{current}\n\n【新的对话】\n{text}")]
        estimated = self.counter.count_text(messages[0][1] + messages[1][1])
        extractor = self.model.with_structured_output(MemoryUpdate, method="function_calling", include_raw=True)
        for attempt in range(1 + config.MAP_RETRIES):
            started = time.monotonic()
            try:
                response = extractor.invoke(messages)
            except Exception as e:
                log.warning("会话压缩调用失败（第 %s 次）: %s", attempt + 1, e)
                continue
            raw = response["raw"]
            usage = getattr(raw, "usage_metadata", None) or {}
            self.store.record_usage(
                thread_id=thread_id,
                run_id=run_id,
                model=self.model_name,
                task_type="compaction",
                estimated_tokens=estimated,
                budgeted_tokens=self.counter.budgeted(estimated),
                history_tokens=self.counter.count_text(text),
                input_tokens=usage.get("input_tokens"),
                output_tokens=usage.get("output_tokens"),
                latency_ms=int((time.monotonic() - started) * 1000),
                compaction_triggered=True,
            )
            if response["parsed"] is not None:
                return response["parsed"]
            log.warning("会话压缩输出格式不对（第 %s 次）: %s", attempt + 1, response.get("parsing_error"))
        return None
