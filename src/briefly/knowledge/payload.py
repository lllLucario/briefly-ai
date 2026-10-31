"""召回预算与实际问答复用同一依据包，标题/章节/同源标识/问题均计入。"""
from ..materials import wrap_material


def evidence_payload(question, evidence, start=1):
    """start：首条依据的编号。同一轮多次检索时编号接续，回答里的〔依据N〕在本轮内唯一。"""
    blocks, documents = [], {}
    for i, item in enumerate(evidence, start):
        key = (item["document_id"], item["version_id"])
        documents.setdefault(key, f"文档{len(documents) + 1}")
        table = item.get("table_header")
        # 业务版本标签只在有时给出；每条都写“未提供”会引得模型逐题评论版本
        version = f"业务版本标签：{item['business_version']}；" if item["business_version"] else ""
        header = (f"【依据{i}；同源标识：{documents[key]}；标题《{item['source']['title']}》；"
                  f"{version}章节：{item['section'] or '未确定'}"
                  + ("；续表：正文最前面的表格行是原表表头，供对应列名" if table else "") + "】")
        blocks.append(wrap_material(header, (table or "") + item["content"]))
    return "问题：" + question + "\n\n" + "\n\n".join(blocks)
