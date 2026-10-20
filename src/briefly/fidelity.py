"""有明确保留要求时核对工具已核实的事实；不是通用语义裁判，不调用模型。"""
import re

from langchain_core.messages import HumanMessage, ToolMessage

from .conflicts import value_in
from .summarize import hard_terms


def clauses(text: str) -> list[str]:
    # 表格的一行本身就是事项范围，单元格里的分号不能把负责人/状态拆离该事项。
    return [part for line in text.splitlines()
            for part in ([line] if line.lstrip().startswith("|") else re.split(r"[。；;]",line)) if part.strip()]


def current_instruction(messages) -> str:
    message = next((m for m in reversed(messages) if isinstance(m,HumanMessage) and not m.additional_kwargs.get("internal")),None)
    if message is None:
        return ""
    text = str(message.additional_kwargs.get("display",message.text))
    text = re.sub(r"```.*?```|“[^”]*”|「[^」]*」|\"[^\"\n]*\"", "",text,flags=re.S)
    return re.split(r"(?:原文|材料|正文|待处理内容)\s*[:：]|【材料开始】",text,maxsplit=1)[0]


def fact_problems(answer: str, messages) -> list[str]:
    instruction = current_instruction(messages)
    # 明确筛选、精简不等于要求所有材料事实都出现在正文中。
    if not re.search(r"完整|全部|逐项|逐条|不遗漏|不要遗漏|保留所有",instruction) or re.search(r"只(?:要|保留|提取|总结)|仅(?:要|保留|提取|总结)",instruction):
        return []
    scoped = bool(re.search(r"逐项|逐条|表格|对应关系|对应事项",instruction))
    start = max((n for n,m in enumerate(messages) if isinstance(m,HumanMessage) and not m.additional_kwargs.get("internal")),default=-1)
    problems = []
    for message in messages[start+1:]:
        if not isinstance(message,ToolMessage) or message.name!="summarize_materials" or not isinstance(message.artifact,dict):
            continue
        for item in message.artifact.get("items",[]):
            if item.get("importance")!="high" or item.get("needs_review"):
                continue
            subject = str(item.get("subject") or "").strip()
            value = str(item.get("value") or "").strip()
            details = list(dict.fromkeys([*item.get("key_details",[]),*hard_terms(item.get("point",""))]))
            if value and item.get("attribute"):
                details.insert(0,value)
            if not details:
                continue
            candidates = clauses(answer) if scoped and subject else [answer]
            if subject and scoped:
                candidates = [line for line in candidates if re.sub(r"\s+","",subject) in re.sub(r"\s+","",line)]
            missing = [term for term in details if not any(value_in(line,term) for line in candidates)]
            if missing:
                name = subject or item.get("point","")[:50]
                problems.append(f"{name}：{'、'.join(missing[:6])}未保留"+("在对应事项中" if scoped and subject else ""))
            if len(problems)>=10:
                return problems
    return list(dict.fromkeys(problems))


def outline_problems(answer: str, messages, sources: list[str]) -> list[str]:
    instruction = current_instruction(messages)
    if not re.search(r"保留[^。\n]{0,12}(?:章节|原文)[^。\n]{0,6}编号",instruction):
        return []
    original = []
    for source in sources:
        original += re.findall(r"(?m)^#{1,6}\s+(\d+)[.、]\s*(.+?)\s*$",source)
    # 粗体/行内代码只影响展示，不应把 **4. 标题** 判成缺少编号。
    plain = re.sub(r"\*\*|__|`", "", answer)
    rendered = re.findall(r"(?m)^\s*(?:#{1,6}\s*)?(?:[-*]\s*)?[【\[]?(\d+)[.、)）]\s*(.+?)\s*$",plain)
    out = []
    for number,title in original:
        matching = [(other,line) for other,line in rendered if title in line]
        if not matching:
            out.append(f"缺少带原编号的章节标题“{number}. {title}”；请保留该标题及内容，不用无序列表或仅正文提及代替")
        for other,line in matching:
            if title in line and number!=other:
                out.append(f"原文章节“{number}. {title}”所在行以{other}开头；请直接用“{number}. {title}”作行首，删除外层连续序号，不在正文里再写‘章节{number}’代替行首原编号")
    return list(dict.fromkeys(out))[:10]
