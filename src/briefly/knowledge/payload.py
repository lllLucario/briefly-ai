"""召回预算与实际问答复用同一依据包，标题/章节/同源标识/问题均计入。"""
from ..materials import wrap_material


def evidence_payload(question, evidence):
    blocks, documents = [], {}
    for i, item in enumerate(evidence, 1):
        key = (item["document_id"], item["version_id"])
        documents.setdefault(key, f"文档{len(documents) + 1}")
        header = (f"【依据{i}；同源标识：{documents[key]}；标题《{item['source']['title']}》；"
                  f"业务版本标签：{item['business_version'] or '未提供'}；章节：{item['section'] or '未确定'}】")
        blocks.append(wrap_material(header, item["content"]))
    return "问题：" + question + "\n\n" + "\n\n".join(blocks)
