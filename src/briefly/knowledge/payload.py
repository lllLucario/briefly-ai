"""召回预算与实际问答复用同一依据包，标题/章节/同源标识/问题均计入。"""
from ..materials import wrap_material

# 联合检索（工作区＋知识库）时放在依据前面：两类出处怎么用。事实性规则仍以回答提示为准，这里不放宽
JOINT_RULES = ("依据分两类：“知识库·已发布”是企业正式发布的资料，“工作区·你的文件”是用户自己的工作文件。"
               "问制度、规定时以知识库发布版为准，工作区写法不同时指出不同之处；处理用户自己的工作时以工作区为准，"
               "并提醒与知识库不一致的地方。两类依据说法冲突时并列说明，不替用户裁决，也不编造两边都没有的内容。")


def origin_label(item):
    """依据头里的出处：知识库写库名与技术版本号，工作区写文件名与更新时间；只有知识库依据的旧调用没有出处字段。"""
    origin = item.get("origin")
    if origin == "workspace":
        updated = item.get("updated_at")
        return f"工作区·你的文件｜{item['source']['title']}｜更新时间 {updated:%Y-%m-%d %H:%M}" if updated else f"工作区·你的文件｜{item['source']['title']}"
    if origin == "knowledge":
        return f"知识库·已发布｜{item.get('kb_name') or '知识库'}｜第{item['number']}版"
    return ""


def evidence_payload(question, evidence, start=1):
    """start：首条依据的编号。同一轮多次检索时编号接续，回答里的〔依据N〕在本轮内唯一。"""
    blocks, documents = [], {}
    for i, item in enumerate(evidence, start):
        key = (item["document_id"], item["version_id"])
        documents.setdefault(key, f"文档{len(documents) + 1}")
        table = item.get("table_header")
        # 业务版本标签只在有时给出；每条都写“未提供”会引得模型逐题评论版本
        version = f"业务版本标签：{item['business_version']}；" if item["business_version"] else ""
        origin = f"出处：{label}；" if (label := origin_label(item)) else ""
        note = f"；{item['comparison']}" if item.get("comparison") else ""  # 与关联文档对照的结果（一致／写法不同）
        header = (f"【依据{i}；同源标识：{documents[key]}；{origin}标题《{item['source']['title']}》；"
                  f"{version}章节：{item['section'] or '未确定'}"
                  + ("；续表：正文最前面的表格行是原表表头，供对应列名" if table else "") + note + "】")
        blocks.append(wrap_material(header, (table or "") + item["content"]))
    preamble = JOINT_RULES + "\n\n" if any(item.get("origin") == "workspace" for item in evidence) else ""
    return preamble + "问题：" + question + "\n\n" + "\n\n".join(blocks)
