"""会话工具：查本会话的原始对话、读回被省略的工具结果。

上下文里只带会话记忆和最近几轮，原始对话完整保存在 checkpointer 中；这两个工具让模型在需要时取回原文，
返回量受工具预算（output_limit）限制，超出时分页。
"""
from langchain.tools import ToolRuntime
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool, tool

from ..context import Context
from ..context_store import ContextStore
from ..tool_budget import output_limit

SNIPPET_RADIUS = 150


def _text(content) -> str:
    return content if isinstance(content, str) else str(content)


def _page(text: str, page: int, chars: int) -> tuple[str, int]:
    pages = max(1, -(-len(text) // chars))
    if not 1 <= page <= pages:
        raise ValueError(f"页码超出范围，共 {pages} 页")
    return text[(page - 1) * chars : page * chars], pages


def _page_chars() -> int:
    # 中文约 1.7 字符/token，按 1.5 取保守值，并给页眉留余量
    return max(int((output_limit() - 200) * 1.5), 500)


def context_tools(store: ContextStore) -> list[BaseTool]:
    @tool
    def recall_conversation(runtime: ToolRuntime[Context], keywords: str = "", turn: int | None = None) -> str:
        """查找本会话较早的原始对话（上下文里只保留了会话记忆和最近几轮）。
        keywords 用空格分隔多个词，返回包含这些词的用户和助手原话片段及轮次；turn 填轮次时返回那一轮的完整对话。两者填一个。
        需要用户早先说过的原话、之前回答里的具体内容，而会话记忆里没有时调用。"""
        messages = (runtime.state or {}).get("messages", [])
        numbered, n = [], 0
        for m in messages:
            if isinstance(m, HumanMessage):
                n += 1
                numbered.append((n, "用户", _text(m.content)))
            elif isinstance(m, AIMessage) and _text(m.content).strip():
                numbered.append((n, "助手", _text(m.content)))
        if turn is not None:
            hits = [f"{who}：{text}" for t, who, text in numbered if t == turn]
            if not hits:
                raise ValueError(f"没有第 {turn} 轮，本会话共 {n} 轮")
            body, pages = _page("\n\n".join(hits), 1, _page_chars())
            more = f"\n（这一轮较长，只返回了开头部分）" if pages > 1 else ""
            return f"【第 {turn} 轮原始对话】\n{body}{more}"
        words = [w.lower() for w in keywords.split() if w]
        if not words:
            raise ValueError("keywords 和 turn 至少填一个")
        out, size, budget = [], 0, _page_chars()
        for t, who, text in reversed(numbered):  # 新的在前
            lower = text.lower()
            pos = min((p for w in words if (p := lower.find(w)) >= 0), default=-1)
            if pos < 0:
                continue
            s, e = max(0, pos - SNIPPET_RADIUS), pos + SNIPPET_RADIUS
            snippet = ("…" if s else "") + text[s:e] + ("…" if e < len(text) else "")
            line = f"- 第 {t} 轮 {who}：{snippet}"
            if size + len(line) > budget:
                out.append("（更多结果因篇幅省略，请换更具体的关键词，或按轮次查看）")
                break
            out.append(line)
            size += len(line)
        return "\n".join(out) if out else f"本会话中没有找到包含“{keywords}”的对话。"

    @tool
    def read_tool_result(result_id: str, runtime: ToolRuntime[Context], page: int = 1) -> str:
        """读回之前被省略或只给了开头的工具结果。result_id 是提示里给出的编号（r_ 开头，或工具调用编号）；
        内容较长时分页返回，page 从 1 开始。"""
        content = None
        record = store.tool_result(result_id) if result_id.startswith("r_") else None
        if record is not None:
            if record.thread_id and runtime.context and runtime.context.thread_id not in ("", record.thread_id):
                raise ValueError(f"工具结果 {result_id} 不属于当前会话")
            content = record.content
        else:
            messages = (runtime.state or {}).get("messages", [])
            hit = next((m for m in messages if isinstance(m, ToolMessage) and m.tool_call_id == result_id), None)
            content = _text(hit.content) if hit else None
        if content is None:
            raise ValueError(f"找不到工具结果 {result_id}")
        body, pages = _page(content, page, _page_chars())
        tail = f"\n（第 {page}/{pages} 页，继续请用 page={page + 1}）" if page < pages else f"\n（第 {page}/{pages} 页，已读完）"
        return f"【工具结果 {result_id}】\n{body}{tail}"

    return [recall_conversation, read_tool_result]
