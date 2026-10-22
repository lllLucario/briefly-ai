"""从程序记录识别本轮证据缺口，不让模型决定是否告知用户。"""
from langchain_core.messages import HumanMessage, ToolMessage


def turn_evidence_gaps(messages, omitted_turns=0) -> list[str]:
    start = max((i for i, m in enumerate(messages) if isinstance(m, HumanMessage) and not m.additional_kwargs.get("internal")), default=-1)
    tools = [m for m in messages[start + 1:] if isinstance(m, ToolMessage)]
    gaps, reads = [], {}
    for message in tools:
        if message.status == "error":
            gaps.append("部分工具未执行或执行失败")
            continue
        artifact = message.artifact if isinstance(message.artifact, dict) else {}
        if artifact.get("failed"):
            gaps.append("部分材料处理失败，未纳入回答依据")
        if artifact.get("dropped"):
            gaps.append("部分提取内容无法核对原文，已丢弃")
        read = artifact.get("result_read")
        if read and not artifact.get("truncation"):
            reads.setdefault(read["result_id"], []).append((read["start"], read["end"]))
    for message in tools:
        artifact = message.artifact if isinstance(message.artifact, dict) else {}
        truncated = artifact.get("truncation")
        if not truncated:
            continue
        end = truncated["preview_chars"]
        for left, right in sorted(reads.get(truncated["result_id"], [])):
            if left > end:
                break
            end = max(end, right)
        if end < truncated["total_chars"]:
            gaps.append("工具结果被截断，尚有内容未读回")
    if omitted_turns:
        gaps.append(f"有 {omitted_turns} 轮历史未纳入上下文")
    return list(dict.fromkeys(gaps))
