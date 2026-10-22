"""会话明确选中或读取过的材料编号；不把可访问范围当成已读依据。"""
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


def conversation_material_ids(messages) -> list[int]:
    ids = []
    for message in messages:
        if isinstance(message, HumanMessage) and not message.additional_kwargs.get("internal"):
            ids += message.additional_kwargs.get("material_ids") or []
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                if call["name"] in {"read_material", "get_outline", "summarize_materials", "search_materials"}:
                    args = call.get("args", {})
                    ids += args.get("material_ids") or []
                    if args.get("material_id"):
                        ids.append(args["material_id"])
        if isinstance(message, ToolMessage) and isinstance(message.artifact, dict):
            ids += message.artifact.get("material_ids") or []
            for item in message.artifact.get("items", []):
                prefix = str(item.get("source", "")).split("-", 1)[0]
                if prefix.isdigit():
                    ids.append(int(prefix))
    return list(dict.fromkeys(ids))
