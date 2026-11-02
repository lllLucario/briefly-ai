"""召回预算与实际问答复用同一依据包，标题/章节/同源标识/问题均计入。"""
from ..materials import wrap_material

# 联合检索（工作区＋知识库）时放在依据前面：两类出处怎么用。事实性规则仍以回答提示为准，这里不放宽。
# 知识库与工作区说法不同时按场景表态（2026-11-02 用户确定）：系统里只有“已发布”和“未发布”，没有“已批准”，
# 未发布的改动不能当作现行规定；“不替用户裁决”只留给没有发布状态可判断的情况（同为知识库或同为工作区）。
JOINT_RULES = ("依据分两类：“知识库·已发布”是企业正式发布的资料，“工作区·你的文件”是用户自己的工作文件，尚未发布。"
               "两类说法不同时，先写清两边各自怎么写，再按场景表态："
               "问制度、规定怎么执行时，写明现行以知识库已发布版为准，并指出工作区哪里不同；"
               "用户在处理自己的稿子（核对、修改、整理自己的文件）时，以工作区为准，同时列出与知识库已发布版的不同；"
               "用户问哪一版优先、以哪版为准时，直接回答：知识库是已发布的正式版，工作区是未发布的文件，"
               "未发布的改动不能当作现行规定，要生效需走发布（更新知识库）。"
               "同为知识库或同为工作区的依据互相矛盾、又无法判断哪份是现行版本时，并列说明，不替用户裁决。不编造两边都没有的内容。"
               "依据头注明“工作区版本此处一致”的，表示工作区那份文件此段已逐句核对、与知识库逐字相同，可以直接据此说明两份一致；"
               "注明“此处写法不同”的，以注明的不同之处为准，不要说工作区没有改动。")


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
    # 工作区副本与知识库逐字相同时只留知识库片段（带对照注记），依据里没有工作区条目也要说明注记的用法
    preamble = JOINT_RULES + "\n\n" if any(item.get("origin") == "workspace" or item.get("comparison") for item in evidence) else ""
    return preamble + "问题：" + question + "\n\n" + "\n\n".join(blocks)
