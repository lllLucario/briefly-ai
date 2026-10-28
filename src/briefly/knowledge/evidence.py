"""解析不确定性的引用边界；受限证据只给带限定的摘录，不生成精确归属。"""
from copy import deepcopy

QUALITY_KEYS=("document_quality", "table_structure_status", "restored_source_paragraphs",
              "unverified_table_segments", "object_anchors", "textbox_provenance", "textbox_markdown_range", "inherited_quality_constraints")


def quality_snapshot(report):
    return {key:deepcopy(report[key]) for key in QUALITY_KEYS if key in report}


def constraints(report,start,end):
    result=dict(report.get("inherited_quality_constraints",{}))
    textbox=report.get("textbox_provenance",{})
    region=report.get("textbox_markdown_range")
    in_textbox=bool(textbox) and (not region or start<region["end"] and end>region["start"])
    if in_textbox and textbox.get("may_infer_section") is False:
        result["section_attribution"]="unverified"
    if report.get("object_anchors",0)>0 or in_textbox and textbox.get("may_infer_org_chart_relationships") is False:
        result["organization_relationships"]="unverified"
    if report.get("table_structure_status")=="partial":
        result["cell_attribution"]="unverified"
    # partial is not assumed to mean only a table problem.
    for facet,state in report.get("document_quality",{}).get("facets",{}).items():
        if facet=="graphics" and state not in {"verified","not_present"}:
            result["organization_relationships"]="unverified"
    return result


NOTES={
    "section_attribution":"文本框正文锚点未确认，不能赋予原文业务章节。",
    "organization_relationships":"图形连接关系未恢复，不能据此确认组织上下级或汇报关系。",
    "cell_attribution":"表格单元格归属仍待复核，不能据此给出确定的行列对应结论。",
}


def restricted_answer(evidence):
    """程序分支，不依赖模型遵守提示或理解用户问题的同义表达。"""
    blocked=sorted({k for e in evidence for k in e.get("constraints",{})})
    if not blocked:
        return None
    return dict(status="needs_review",answer="这些依据的结构仍有未确认部分，不能给出确定的归属结论。\n"+
        "\n".join(NOTES[k] for k in blocked),blocked_capabilities=blocked,
        # Keep text separate from a generated answer, visibly labelled as disputed extraction.
        observations=[dict(reference=e["reference"],section=e["section"],
            label="尚未确认结构的提取文字，仅供核对" if e.get("constraints") else "原文摘录",
            text=e["content"],constraints=e.get("constraints",{})) for e in evidence],model_called=False)
