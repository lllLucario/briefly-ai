"""解析不确定性的引用边界；受限证据只给带限定的摘录，不生成精确归属。"""
from copy import deepcopy
import re

QUALITY_KEYS=("document_quality", "table_structure_status", "restored_source_paragraphs",
              "unverified_table_segments", "object_anchors", "textbox_provenance", "textbox_markdown_range",
              "table_partial_ranges", "ocr_unconfirmed_ranges", "inherited_quality_constraints")


def quality_snapshot(report):
    return {key:deepcopy(report[key]) for key in QUALITY_KEYS if key in report}


def constraints(report,start,end):
    result=dict(report.get("inherited_quality_constraints",{}))
    textbox=report.get("textbox_provenance",{})
    region=report.get("textbox_markdown_range")
    in_textbox=bool(textbox) and (not region or start<region["end"] and end>region["start"])
    if in_textbox and textbox.get("may_infer_section") is False:
        result["section_attribution"]="unverified"
    # 图片和嵌入对象不提取文字，正文里没有图形内容；图形关系限制只落在带图形文字的文本框范围（没有范围时整份）
    graphics=report.get("document_quality",{}).get("facets",{}).get("graphics")
    graphics_unverified=report.get("object_anchors",0)>0 or graphics not in {None,"verified","not_present"}
    if in_textbox and (textbox.get("may_infer_org_chart_relationships") is False or graphics_unverified):
        result["organization_relationships"]="unverified"
    if report.get("table_structure_status")=="partial":
        # 解析器给出了未确认表格的范围（PDF）时只限制这些范围；没有范围时整份受限
        tables=report.get("table_partial_ranges")
        if tables is None or any(start<r["end"] and end>r["start"] for r in tables):
            result["cell_attribution"]="unverified"
    # 图片文字识别置信度低的段落只限制这些范围；置信度够的识别文字在正文里有标记行，不受限
    if any(start<r["end"] and end>r["start"] for r in report.get("ocr_unconfirmed_ranges") or ()):
        result["ocr_text"]="unverified"
    return result


NOTES={
    "section_attribution":"文本框正文锚点未确认，不能赋予原文业务章节。",
    "organization_relationships":"图形连接关系未恢复，不能据此确认组织上下级或汇报关系。",
    "cell_attribution":"表格单元格归属仍待复核，不能据此给出确定的行列对应结论。",
    "ocr_text":"这段文字来自图片识别且置信度低，可能有错字漏字，不能据此给出确定结论。",
}


_OCR_REASONS = dict(time_budget="超过识别时长预算", page_limit="到达识别页数上限", image_limit="到达图片数量上限")
_EMPTY_PAGES = re.compile(r"第 (.+?) 页几乎没有文字")
NOT_FOUND_NOTE = "库里有文件未识别完，没找到不代表文件里没有。"


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _skipped_page_count(pages):
    """pdf._page_list 的写法：“3、5、7”或“3、5 等 382 页”。"""
    if match := re.search(r"等 (\d+)", pages):
        return int(match[1])
    return len(pages.split("、"))


def _disabled_reason(warning):
    if "已关闭" in warning:
        return "图片文字识别已关闭"
    return "未安装本地图片文字识别" if "未安装" in warning else "当时没有做图片文字识别"


def coverage(report):
    """文件里没有识别成文字的内容：{unit, total, recognized, unrecognized, reasons, notice}；都识别了或看不出来时返回 None。
    只读解析报告，旧数据不用重新解析。没开识别时报告里只有警告文字，所以从警告里取。"""
    report = report if isinstance(report, dict) else {}
    warnings = [w for w in report.get("warnings") or () if isinstance(w, str)]
    ocr = report.get("ocr") if isinstance(report.get("ocr"), dict) else None
    pages = report.get("parser") in {"pdfplumber", "rapidocr"}
    if ocr is not None:
        skipped = ocr.get("skipped") if isinstance(ocr.get("skipped"), dict) else {}
        reasons = [text for code, text in _OCR_REASONS.items() if _count(skipped.get(code))]
        failed = _count(ocr.get("failed"))
        if failed:
            reasons.append("识别失败")
        unrecognized = failed + sum(_count(skipped.get(code)) for code in _OCR_REASONS)
        pages = pages or (_count(ocr.get("pages_recognized")) > 0 and not _count(ocr.get("images_recognized")))
        recognized = _count(ocr.get("pages_recognized" if pages else "images_recognized"))
    else:
        reasons, unrecognized, recognized = [], 0, 0
        for warning in warnings:
            if pages and (match := _EMPTY_PAGES.search(warning)):
                unrecognized = _skipped_page_count(match[1])
            elif "未识别图片里的文字" in warning:
                unrecognized, reasons = _count(report.get("pictures")) or unrecognized, [_disabled_reason(warning)]
        if unrecognized and not reasons:
            note = next((w for w in warnings if "没有文字层的页未识别" in w), "")
            reasons = [_disabled_reason(note)]
    if not unrecognized:
        return None
    if pages:
        total = _count(report.get("pages")) or _count(report.get("frames"))
    else:
        total = _count(report.get("pictures"))
    total = max(total, recognized + unrecognized)
    why = "、".join(reasons)
    head = f"{total} 页中有 {unrecognized} 页" if pages else f"{total} 张图片中有 {unrecognized} 张"
    return dict(unit="page" if pages else "image", total=total, recognized=recognized, unrecognized=unrecognized,
                reasons=reasons, notice=f"{head}未识别（{why}），这些内容检索不到。")


def material_quality_notice(store, material_ids):
    """旧入口共用质量能力边界；只查已选/实际读过的来源，不扩大至全库。"""
    blocked = set()
    for material_id in dict.fromkeys(material_ids):
        try:
            material = store.get(material_id, content=False, workspace=False)
        except ValueError:
            continue
        blocked.update(constraints(material.parse_report, 0, material.char_count))
        # 旧材料工具也必须继承知识版本快照的更严限制，不能清空当前报告绕过。
        from sqlalchemy import select
        from sqlalchemy.orm import Session
        from .models import KnowledgeVersion
        with Session(store.engine) as session:
            for version in session.scalars(select(KnowledgeVersion).where(KnowledgeVersion.material_id == material_id)):
                blocked.update(constraints(version.source_snapshot.get("parse_quality", {}), 0, material.char_count))
                for chunk in version.chunk_manifest:
                    blocked.update(chunk.get("constraints", {}))
    if not blocked:
        return None
    return "本次关联材料存在尚未确认的结构，需核对原件后再继续。\n" + "\n".join(NOTES[key] for key in sorted(blocked))


def material_reference_artifact(store, material_id, start, end):
    """只从实际读取范围关联已存在的知识版本；未入库材料不伪造知识库身份。"""
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from .models import KnowledgeDocument, KnowledgeVersion
    material = store.get(material_id, content=False, workspace=False)
    references = []
    with Session(store.engine) as session:
        rows = session.execute(select(KnowledgeVersion, KnowledgeDocument)
            .join(KnowledgeDocument, KnowledgeDocument.id == KnowledgeVersion.document_id)
            .where(KnowledgeVersion.material_id == material_id,
                   KnowledgeVersion.content_hash == material.content_hash)).all()
        for version, document in rows:
            for chunk in version.chunk_manifest:
                if chunk["start"] < end and chunk["end"] > start:
                    references.append(dict(kb_id=document.kb_id, document_id=document.id,
                        version_id=version.id, reference=f"kbref:{version.id}:{chunk['index']}:{chunk['sha256']}"))
    return dict(material_ids=[material_id], read_range=dict(start=start, end=end),
        content_hash=material.content_hash, knowledge_references=references,
        quality_notice=material_quality_notice(store, [material_id]))


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
