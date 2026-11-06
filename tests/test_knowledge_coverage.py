"""部分内容没识别成文字时要说清楚（登记表第 99 条）。解析报告是构造的，假编码器、假模型、原创虚构资料。"""
from types import SimpleNamespace

import pytest

pytest.importorskip("sqlite_vec", reason="知识库回归需安装.[rag]")
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from briefly import config
from briefly.api import create_app
from briefly.knowledge.assistant import KnowledgeAssistant, record_knowledge_turn
from briefly.knowledge.evidence import NOT_FOUND_NOTE, coverage
from briefly.knowledge.index import LocalIndex
from briefly.knowledge.ingestion import ImportService
from briefly.knowledge.qa import KnowledgeQA
from briefly.knowledge.retrieval import RetrievalService
from briefly.loaders import LoadedDocument
from briefly.llm_cache import ResponseCache, bind_scope, release_scope
from briefly.materials import KNOWLEDGE, MaterialStore
from briefly import ocr
from db_util import file_engine
from fakes import FunctionModel
from test_workspace_index import BagEncoder

POLICY = "# 差旅制度\n## 住宿\n正式员工在上海住宿上限为每晚650元，按实际发票报销。\n## 交通\n高铁二等座可以报销，飞机需要提前审批。"
OTHER = "# 考勤制度\n## 请假\n病假须在当天上午九点前提交申请，并附医院证明。"
SCAN = dict(parser="pdfplumber", pages=413, warnings=["第 23、25 等 382 页 页几乎没有文字（可能是图片、扫描页或封面），这些页的内容可能缺失"],
            ocr=dict(pages_recognized=18, images_recognized=0, failed=0, skipped=dict(time_budget=382), partial=True))
IMAGE_LIMIT = dict(parser="docling", slides=20, pictures=75,
                   ocr=dict(pages_recognized=0, images_recognized=60, failed=0, skipped=dict(image_limit=15, small=3), partial=True))


# ---- 从报告算覆盖情况
def test_time_budget_on_scanned_pdf():
    found = coverage(SCAN)
    assert (found["unit"], found["total"], found["recognized"], found["unrecognized"]) == ("page", 413, 18, 382)
    assert "413 页中有 382 页未识别" in found["notice"] and "超过识别时长预算" in found["notice"]
    assert "检索不到" in found["notice"]


def test_image_limit_is_not_called_a_scan():
    found = coverage(IMAGE_LIMIT)
    assert (found["unit"], found["total"], found["recognized"], found["unrecognized"]) == ("image", 75, 60, 15)
    assert "75 张图片中有 15 张未识别" in found["notice"] and "到达图片数量上限" in found["notice"]
    assert "扫描" not in found["notice"]


def test_page_limit_and_failures_are_named():
    found = coverage(dict(parser="pdfplumber", pages=100, ocr=dict(pages_recognized=40, failed=2, skipped=dict(page_limit=58))))
    assert found["unrecognized"] == 60 and "到达识别页数上限" in found["notice"] and "识别失败" in found["notice"]


@pytest.mark.parametrize("mode,reason", [("off", "图片文字识别已关闭"), ("auto", "未安装本地图片文字识别")])
def test_ocr_not_available_uses_the_real_warning_text(monkeypatch, mode, reason):
    monkeypatch.setattr(config, "OCR", mode)
    note = ocr.disabled_note()
    slides = dict(parser="docling", pictures=12, warnings=[f"幻灯片中有 12 张图片，未识别图片里的文字（{note}），请核对原文件"])
    found = coverage(slides)
    assert (found["unit"], found["total"], found["recognized"], found["unrecognized"]) == ("image", 12, 0, 12)
    assert reason in found["notice"] and "扫描" not in found["notice"]
    pdf = dict(parser="pdfplumber", pages=9, warnings=["第 2、5 页几乎没有文字（可能是图片、扫描页或封面），这些页的内容可能缺失",
                                                       f"{note}，没有文字层的页未识别"])
    found = coverage(pdf)
    assert (found["unit"], found["total"], found["unrecognized"]) == ("page", 9, 2) and reason in found["notice"]


def test_page_list_format_matches_the_pdf_parser():
    from briefly.pdf import _page_list

    for pages, expected in ((list(range(3)), 3), (list(range(25)), 25)):
        report = dict(parser="pdfplumber", pages=30, warnings=[f"第 {_page_list(pages)} 页几乎没有文字（可能是图片）"])
        assert coverage(report)["unrecognized"] == expected


def test_real_ocr_session_report_is_read_back():
    session = ocr.OcrSession()
    session.skipped.update(image_limit=4)
    session.images = 60
    _, report = session.finish("正文", dict(parser="docling", pictures=64, warnings=[]))
    assert coverage(report)["unrecognized"] == 4


def test_docx_warning_from_the_real_loader(monkeypatch):
    from briefly.loaders import load_document
    from test_ocr import docx_with, picture

    monkeypatch.setattr(config, "OCR", "off")
    found = coverage(load_document("a.docx", docx_with([picture((600, 200))])).report)
    assert found and found["unrecognized"] == 1 and "图片文字识别已关闭" in found["notice"]


@pytest.mark.parametrize("report", [
    dict(parser="pdfplumber", pages=5, warnings=[]),
    dict(parser="pdfplumber", pages=5, ocr=dict(pages_recognized=5, images_recognized=0, failed=0, skipped={}, partial=False)),
    dict(parser="docling", pictures=9, ocr=dict(images_recognized=4, skipped=dict(small=3, repeated=2), failed=0)),
    dict(parser="pdfplumber", pages=5, warnings=["第 2 页几乎没有文字（封面）"], ocr=dict(pages_recognized=1, skipped={}, failed=0)),
], ids=["no-ocr-needed", "all-recognized", "only-decorative-skips", "blank-page-recognized"])
def test_complete_recognition_shows_nothing(report):
    assert coverage(report) is None


@pytest.mark.parametrize("report", [
    None, {}, [], dict(parser="docling"), dict(warnings=None), dict(ocr=None), dict(ocr="坏"), dict(ocr=dict(skipped="坏", failed="x")),
    dict(warnings=[None, 1]), dict(parser="pdfplumber", ocr=dict(skipped=dict(time_budget=-1))),
], ids=str)
def test_old_or_odd_reports_do_not_fail(report):
    assert coverage(report) is None


# ---- 依据包：两条路径
@pytest.fixture
def world(tmp_path):
    engine = file_engine()
    encoder = BagEncoder()
    store = MaterialStore(engine)
    imports = ImportService(engine, tmp_path / "knowledge", LocalIndex(tmp_path / "kb.sqlite"), lambda: encoder)
    kb = imports.knowledge.create("制度库")
    imports.knowledge.set_egress(kb.id, True)

    def publish(title, content, report=None):
        material, _ = store.add_text(title, content, source=KNOWLEDGE, report=report)
        doc, version, _ = imports.knowledge.attach(kb.id, material.id)
        imports.lifecycle.build(kb.id, doc.id, version.id, encoder, expected_revision=doc.revision)
        return doc

    retrieval = RetrievalService(imports)
    yield SimpleNamespace(engine=engine, kb=kb, publish=publish, retrieval=retrieval, svc=imports.knowledge,
                          qa=lambda model=None: KnowledgeQA(retrieval, lambda: model), encoder=encoder,
                          assistant=KnowledgeAssistant(retrieval, lambda: None))


QUESTION = "上海住宿上限是多少"


def test_bundle_warns_when_evidence_comes_from_partial_file(world):
    world.publish("差旅制度", POLICY, SCAN)
    bundle = world.qa().bundle(world.kb.id, [QUESTION])
    assert bundle["status"] == "ready"
    assert any("《差旅制度》" in w and "413 页中有 382 页未识别" in w and "回答可能不完整" in w for w in bundle["warnings"])
    assert bundle["not_found_note"] == NOT_FOUND_NOTE


def test_bundle_is_quiet_when_nothing_is_missing_or_only_other_files_are(world):
    world.publish("差旅制度", POLICY)
    bundle = world.qa().bundle(world.kb.id, [QUESTION])
    assert bundle["warnings"] == [] and bundle["not_found_note"] is None
    world.publish("考勤制度", OTHER, IMAGE_LIMIT)
    bundle = world.qa().bundle(world.kb.id, [QUESTION], document_ids=[d.id for d in world.svc.documents(world.kb.id) if d.title == "差旅制度"])
    assert bundle["not_found_note"] is None  # 限定了文档范围，范围外的不算
    bundle = world.qa().bundle(world.kb.id, [QUESTION])
    assert bundle["not_found_note"] == NOT_FOUND_NOTE


def test_nothing_found_says_absence_is_not_proof(world, monkeypatch):
    world.publish("差旅制度", POLICY, SCAN)
    monkeypatch.setattr("briefly.knowledge.qa.relevant", lambda evidence, floor: False)
    low = world.qa().bundle(world.kb.id, [QUESTION])
    assert low["status"] == "low_relevance" and NOT_FOUND_NOTE in low["answer"]


def test_no_candidates_says_absence_is_not_proof(world, monkeypatch):
    world.publish("差旅制度", POLICY, SCAN)
    real = world.retrieval.search
    monkeypatch.setattr(world.retrieval, "search", lambda *a, **k: {**real(*a, **k), "evidence": [], "status": "no_candidates"})
    bundle = world.qa().bundle(world.kb.id, [QUESTION])
    assert bundle["status"] == "no_candidates" and NOT_FOUND_NOTE in bundle["answer"]


def answering(text):
    return FunctionModel(respond=lambda _: AIMessage(text), calls=[])


def test_shortcut_answer_from_partial_file_needs_review_and_is_not_cached(world):
    world.publish("差旅制度", POLICY, SCAN)
    token = bind_scope(ResponseCache(world.engine), "local")
    original, config.LLM_CACHE = config.LLM_CACHE, True
    try:
        model = answering("【可答性：可回答】每晚650元〔依据1〕")
        first = world.qa(model).answer(world.kb.id, QUESTION)
        calls = len(model.calls)
        second = world.qa(model).answer(world.kb.id, QUESTION)
    finally:
        config.LLM_CACHE = original
        release_scope(token)
    assert first["status"] == "needs_review" and any("未识别" in w for w in first["warnings"])
    assert not second.get("from_cache") and len(model.calls) > calls  # 这种回答不写缓存


def test_shortcut_from_complete_files_stays_cached_in_a_library_with_a_partial_file(world):
    """缓存影响收窄：库里另一份文件没识别完，这份完整文件的正常回答照常缓存。"""
    world.publish("差旅制度", POLICY)
    partial = world.publish("考勤制度", OTHER, IMAGE_LIMIT)
    model = answering("【可答性：可回答】每晚650元〔依据1〕")
    documents = [d.id for d in world.svc.documents(world.kb.id) if d.id != partial.id]
    token = bind_scope(ResponseCache(world.engine), "local")
    original, config.LLM_CACHE = config.LLM_CACHE, True
    real = world.retrieval.search
    world.retrieval.search = lambda kb, q, **k: {**(found := real(kb, q, **k)), "evidence": [e for e in found["evidence"] if e["document_id"] in documents]}
    try:
        first = world.qa(model).answer(world.kb.id, QUESTION)
        second = world.qa(model).answer(world.kb.id, QUESTION)
    finally:
        config.LLM_CACHE = original
        release_scope(token)
    assert first["status"] == "answered" and first["warnings"] == []
    assert second["from_cache"] is True


def test_shortcut_insufficient_evidence_gets_the_note(world):
    world.publish("差旅制度", POLICY)
    partial = world.publish("考勤制度", OTHER, IMAGE_LIMIT)
    real = world.retrieval.search
    world.retrieval.search = lambda kb, q, **k: {**(found := real(kb, q, **k)), "evidence": [e for e in found["evidence"] if e["document_id"] != partial.id]}
    result = world.qa(answering("【可答性：依据不足】未提及")).answer(world.kb.id, QUESTION)
    assert result["answer"].endswith("未提及")  # 回答本身仍是未提及，只在提示里加限定
    assert NOT_FOUND_NOTE in result["warnings"] and result["status"] == "needs_review"


def test_prompt_keeps_the_fact_rules_and_gets_no_hint_to_guess(world):
    world.publish("差旅制度", POLICY, SCAN)
    model = answering("【可答性：依据不足】未提及")
    world.qa(model).answer(world.kb.id, QUESTION)
    sent = str(model.calls)
    assert "依据不足" in sent and "未提及" in sent and "只依据下列材料回答" in sent


def test_assistant_tool_tells_the_model_the_rule(world):
    from briefly.knowledge.assistant import knowledge_tools

    world.publish("差旅制度", POLICY, SCAN)
    [tool] = knowledge_tools(world.assistant)
    context = SimpleNamespace(kb_id=world.kb.id)
    result = tool.func(questions=[QUESTION], runtime=SimpleNamespace(context=context, state={"messages": [HumanMessage(QUESTION)]}))
    text, artifact = result
    assert NOT_FOUND_NOTE in text and "按未提及回答" in text and "413 页中有 382 页未识别" in text
    assert artifact["knowledge"]["not_found_note"] == NOT_FOUND_NOTE


def turn(reply, note):
    search = ToolMessage("结果", name="search_knowledge", tool_call_id="c1", artifact=dict(knowledge=dict(
        kb_id="k", status="ready", questions=["q"], warnings=[], egress=None, not_found_note=note,
        references=[dict(label="依据1", reference="r", title="t")])))
    state = dict(messages=[HumanMessage("q", id="h"), AIMessage("", id="a", tool_calls=[dict(name="search_knowledge", args={}, id="c1", type="tool_call")]),
                           search, AIMessage(reply, id="b")])
    update = record_knowledge_turn.after_agent(state, SimpleNamespace(context=SimpleNamespace(kb_id="k")))
    return update["messages"][0].response_metadata["knowledge"]["warnings"]


@pytest.mark.parametrize("reply,shown", [
    ("资料里未提及", True),
    ("没有找到相关规定", True),
    ("每晚650元〔依据1〕，交通未提及〔依据1〕", True),
    ("大概是这样", True),
    ("每晚650元〔依据1〕", False),
])
def test_assistant_turn_adds_the_note_only_when_nothing_was_found(reply, shown):
    assert (NOT_FOUND_NOTE in turn(reply, NOT_FOUND_NOTE)) is shown
    assert NOT_FOUND_NOTE not in turn(reply, None)


def test_locate_covers_both_cases(world, monkeypatch):
    world.publish("差旅制度", POLICY, SCAN)
    found = world.assistant.locate(world.kb.id, QUESTION)
    assert found["status"] == "located" and any("382 页未识别" in w for w in found["warnings"])
    monkeypatch.setattr("briefly.knowledge.assistant.relevant", lambda evidence, floor: False)
    monkeypatch.setattr("briefly.knowledge.assistant.relevance_floor", lambda identity: 0.5)
    missing = world.assistant.locate(world.kb.id, QUESTION)
    assert missing["evidence"] == [] and NOT_FOUND_NOTE in missing["answer"]


# ---- 接口
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "KNOWLEDGE_DIR", str(tmp_path / "knowledge"))
    engine = file_engine()
    app = create_app(MaterialStore(engine))
    imports = app.state.knowledge_imports
    imports.encoder_factory = BagEncoder
    reports = dict(scan=SCAN, image=IMAGE_LIMIT, full=dict(parser="docling", warnings=[]))
    imports.parser = lambda filename, data: LoadedDocument(data.decode(), True, dict(reports[filename.split(".")[0]]))
    with TestClient(app) as c:
        yield c
    engine.dispose()


def upload(client, kb, name, content):
    assert client.post(f"/knowledge-bases/{kb}/imports", data={"request_key": name, "tokens": "64"},
                       files=[("files", (name, content.encode()))]).status_code == 202


def test_documents_and_jobs_report_coverage(client):
    kb = client.post("/knowledge-bases", json={"name": "库"}).json()["id"]
    upload(client, kb, "scan.md", POLICY)
    upload(client, kb, "image.md", OTHER)
    upload(client, kb, "full.md", "# 完整\n这份文件都识别了，内容是报销流程说明。")
    documents = {d["title"]: d["coverage"] for d in client.get(f"/knowledge-bases/{kb}/documents").json()}
    assert documents["full.md"] is None
    assert documents["scan.md"]["total"] == 413 and documents["scan.md"]["unrecognized"] == 382 and documents["scan.md"]["recognized"] == 18
    assert documents["image.md"]["unit"] == "image"
    jobs = {j["filename"]: j["coverage"] for j in client.get(f"/knowledge-bases/{kb}/imports").json()}
    assert jobs["full.md"] is None and jobs["scan.md"]["unrecognized"] == 382
    # 状态仍是已完成：覆盖情况是额外说明
    assert {j["status"] for j in client.get(f"/knowledge-bases/{kb}/imports").json()} == {"ready"}


def test_old_material_without_report_fields(client):
    kb = client.post("/knowledge-bases", json={"name": "库"}).json()["id"]
    upload(client, kb, "full.md", "# 旧资料\n这是早先入库的虚构内容。")
    from sqlalchemy import update
    from sqlalchemy.orm import Session

    from briefly.materials import Material

    with Session(client.app.state.store.engine) as s, s.begin():
        s.execute(update(Material).values(parse_report={}))
    assert client.get(f"/knowledge-bases/{kb}/documents").json()[0]["coverage"] is None
    assert client.get(f"/knowledge-bases/{kb}/imports").json()[0]["coverage"] is None
