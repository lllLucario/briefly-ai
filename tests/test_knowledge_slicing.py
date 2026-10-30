import hashlib

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from briefly.db import create_db_engine
from briefly.knowledge.models import KnowledgeChunk
from briefly.knowledge.service import KnowledgeConflict, KnowledgeService, index_fingerprint
from briefly.knowledge.slicing import token_slices
from briefly.materials import MaterialChunk, MaterialStore
from test_knowledge import fake_identity


class Encoder:
    identity = fake_identity(dimension=2)

    @staticmethod
    def token_length(text):
        return len(text)

    def embed_documents(self, texts):
        return [[len(text), 1] for text in texts]

    def embed_query(self, text):
        return [len(text), 1]


def test_title_budget_offsets_and_no_punctuation_only_tail():
    text = "# 范围\n" + "业务原文" * 200 + "。"
    config, chunks = token_slices(text, "标题", True, Encoder(), tokens=64, overlap=0)
    assert config["parameters"]["tokens"] == 64
    assert chunks[0].start == 0 and chunks[-1].end == len(text)
    assert all(len(c.embedding_text) <= 64 for c in chunks)
    assert all(c.content == text[c.start:c.end] for c in chunks)
    assert all(any(c.isalnum() for c in row.content) for row in chunks)
    assert all(a.end == b.start for a, b in zip(chunks, chunks[1:]))
    assert "".join(c.content for c in chunks) == text


def test_word_body_numbered_headings_become_slice_sections():
    text = "**第一章 总则**\n" + "总则原文。" * 20 + "\n第三条 本制度适用于全体员工。\n" + "条款原文。" * 20
    _, chunks = token_slices(text, "制度", True, Encoder(), tokens=128, overlap=0)
    assert {c.section for c in chunks} == {"第一章 总则", "第一章 总则 > 第三条"}
    assert all(c.content == text[c.start:c.end] for c in chunks)
    assert any(c.start == text.index("第三条") for c in chunks)  # 章节起点是切片边界


def test_table_rows_are_kept_and_headers_enter_each_continuation():
    text = "| 地点 | 上限 |\n| --- | --- |\n" + "".join(f"| 城市{i} | {i+500} |\n" for i in range(12))
    _, chunks = token_slices(text, "制度", True, Encoder(), tokens=96)
    assert len(chunks) > 1
    assert "".join(c.content for c in chunks) == text
    assert all(c.content.startswith("|") and c.content.endswith("\n") for c in chunks)
    assert all("| 地点 | 上限 |" in c.embedding_text for c in chunks)


def test_wide_row_is_split_by_column_groups_with_row_and_column_labels():
    names = ["职级", "档次"] + [f"项目{i:02d}" for i in range(12)]
    header = "| " + " | ".join(names) + " |\n|" + "---|" * len(names) + "\n"
    rows = "".join(f"| {grade}级 | {band} | " + " | ".join(f"{grade}{band}{i:02d}值" for i in range(12)) + " |\n"
                   for grade in (24, 23) for band in "AB")
    text = "# 薪酬\n\n" + header + rows
    config, chunks = token_slices(text, "表", True, Encoder(), tokens=150)
    assert config["version"] == 5
    assert "".join(c.content for c in chunks) == text  # 各段首尾相接，没有截断或丢字
    assert all(c.content == text[c.start:c.end] and len(c.embedding_text) <= 150 for c in chunks)
    pieces = [c for c in chunks if "第1行" in c.section]
    assert len(pieces) > 1 and all(c.content.startswith("|") for c in pieces)
    assert all("职级：24级；档次：A" in c.section for c in pieces)  # 行标识取到能区分各行为止
    cell = next(c for c in pieces if "24A07值" in c.content)
    columns = cell.section.split("列：")[1].split(" | ")
    values = [v.strip() for v in cell.content.strip().strip("|").split("|")]
    assert columns[values.index("24A07值")] == "项目07"  # 列名与格按顺序一一对应，能还原到原表的行和列
    # 编码文本写成“列名：值”，不再拼整串列名，免得语义向量被稀释
    assert "项目07：24A07值" in cell.embedding_text and "列：" not in cell.embedding_text
    assert any(c.section == "薪酬 > 表头" for c in chunks)


def test_long_cell_is_split_inside_the_cell_and_keeps_row_and_column():
    remark = "".join(f"第{i}条说明内容较长需要拆分。" for i in range(20))
    text = "| 指标 | 定义 |\n|---|---|\n| 招聘达成率 | " + remark + " |\n| 存活率 | 短 |\n"
    _, chunks = token_slices(text, "KPI", True, Encoder(), tokens=96)
    assert "".join(c.content for c in chunks) == text
    inside = [c for c in chunks if c.section == "第1行（指标：招聘达成率） > 列：定义"]
    assert len(inside) > 1
    assert all(c.content.endswith("。") for c in inside[:-1])  # 在句末切开
    assert chunks[-1].content == "| 存活率 | 短 |\n" and chunks[-1].section == ""  # 装得下的行照旧带表头
    with pytest.raises(ValueError, match="行标识或列名"):
        token_slices("| " + "长列名" * 30 + " |\n|---|\n| " + "x" * 200 + " |\n", "表", True, Encoder(), tokens=64)


def test_prose_default_overlap_keeps_offsets_and_total_budget():
    text = "# 范围\n\n" + "不同业务条件须结合适用对象判断。" * 100
    config, chunks = token_slices(text, "制度", True, Encoder(), context="适用版本待核对")
    assert config["version"] == 5 and config["parameters"]["tokens"] == 512
    assert config["parameters"]["overlap"] == 50
    assert chunks[0].start == 0 and chunks[-1].end == len(text)
    assert all(c.content == text[c.start:c.end] and len(c.embedding_text) <= 512 for c in chunks)
    assert all(a.start < b.start <= a.end for a, b in zip(chunks, chunks[1:]))
    assert all(0 < a.end - b.start <= 50 for a, b in zip(chunks, chunks[1:]))
    assert all("适用版本待核对" in c.embedding_text for c in chunks)
    with pytest.raises(ValueError, match="上下文"):
        token_slices(text, "制度", True, Encoder(), context="太长" * 300)
    with pytest.raises(ValueError, match="Embedding输入上限"):
        token_slices(text, "制度", True, Encoder(), tokens=1024)


def test_paragraphs_preferred_and_overlap_stops_at_structure_boundaries():
    before = "第一段完整业务说明。" * 6 + "\n\n" + "第二段完整业务说明。" * 6 + "\n\n"
    table = "| 条件 | 处理 |\n|---|---|\n" + "| a | b |\n" * 12
    image = "![图形标签](local.png)\n"
    text = before + table + image + "末尾的原文。" * 20
    _, chunks = token_slices(text, "资料", True, Encoder(), tokens=100, overlap=15)
    table_start, table_end = len(before), len(before) + len(table)
    for c in chunks:
        if c.start < table_end and c.end > table_start:
            assert table_start <= c.start < c.end <= table_end
            assert c.content.startswith("|") and c.content.endswith("\n")
    table_chunks = [c for c in chunks if table_start <= c.start < table_end]
    assert all(a.end == b.start for a, b in zip(table_chunks, table_chunks[1:]))
    assert any(c.content == image for c in chunks)
    assert chunks[0].end == before.index("\n\n") + 2


def test_textbox_range_is_atomic_and_not_given_a_business_section():
    text = "# 正文\n\n一般条款。\n\n文本框甲→文本框乙"
    start = text.index("文本框")
    report = {"textbox_markdown_range": {"start": start, "end": len(text)}}
    _, chunks = token_slices(text, "资料", True, Encoder(), tokens=64, report=report)
    assert chunks[-1].content == text[start:] and chunks[-1].section == ""
    with pytest.raises(ValueError, match="未套用正文滑窗"):
        token_slices("形状" * 80, "图", True, Encoder(), tokens=64,
                     report={"textbox_markdown_range": {"start": 0, "end": 160}})


def test_new_slice_versions_keep_old_material_and_reference(tmp_path):
    engine = create_db_engine("sqlite:///" + (tmp_path / "slices.db").as_posix())
    try:
        store, svc = MaterialStore(engine), KnowledgeService(engine)
        text = "原文依据" * 100
        material, _ = store.add_text("资料", text)
        kb = svc.create("库")
        doc, old, _ = svc.attach(kb.id, material.id, source_key="fixture")
        ref = svc.chunk(kb.id, doc.id, old.id, 1)["reference"]
        with Session(engine) as s:
            before = [(c.id, c.index, c.content) for c in s.scalars(select(MaterialChunk))]
        config, chunks = token_slices(text, "资料", False, Encoder(), tokens=64)
        _, new, created = svc.stage_slices(kb.id, material.id, "fixture", config, chunks, expected_revision=1)
        assert created and new.number == 2 and new.id != old.id
        generation = svc.plan_index(kb.id, doc.id, new.id, Encoder.identity)
        assert generation.status == "planned"
        newref = svc.chunk(kb.id, doc.id, new.id, 1)
        assert newref["content"] == text[:chunks[0].end] and newref["reference"] != ref
        assert svc.resolve_reference(kb.id, ref)["content"] == text
        # 同样切片重复确认不重复创建版本；配置改变必须创建新身份。
        _, same, created = svc.stage_slices(kb.id, material.id, "fixture", config, chunks, expected_revision=2)
        assert not created and same.id == new.id
        other_config, other_chunks = token_slices(text, "资料", False, Encoder(), tokens=96)
        _, other, created = svc.stage_slices(kb.id, material.id, "fixture", other_config, other_chunks, expected_revision=2)
        assert created and other.number == 3
        other_generation = svc.plan_index(kb.id, doc.id, other.id, Encoder.identity)
        assert index_fingerprint(generation.embedding_identity) != index_fingerprint(other_generation.embedding_identity)
        with Session(engine) as s:
            assert before == [(c.id, c.index, c.content) for c in s.scalars(select(MaterialChunk))]
        with pytest.raises(KnowledgeConflict):
            svc.stage_slices(kb.id, material.id, "fixture", config, chunks, expected_revision=1)
        with Session(engine) as s, s.begin():
            s.get(KnowledgeChunk, (new.id, 1)).embedding_text = "被改写"
        with pytest.raises(KnowledgeConflict):
            svc.plan_index(kb.id, doc.id, new.id, Encoder.identity)
        assert hashlib.sha256(chunks[0].content.encode()).hexdigest() == new.chunk_manifest[0]["sha256"]
    finally:
        engine.dispose()


def test_continuation_chunks_give_the_answer_model_their_table_header(tmp_path):
    from briefly.knowledge.payload import evidence_payload
    engine = create_db_engine("sqlite:///" + (tmp_path / "header.db").as_posix())
    try:
        store, svc = MaterialStore(engine), KnowledgeService(engine)
        text = "| 地点 | 上限 |\n|---|---|\n" + "".join(f"| 城市{i} | {i + 500} |\n" for i in range(12))
        material, _ = store.add_text("差旅", text, markdown=True)
        config, chunks = token_slices(material.content, "差旅", True, Encoder(), tokens=96)
        kb = svc.create("库")
        doc, version, _ = svc.stage_slices(kb.id, material.id, "fixture", config, chunks)
        first, second = (svc.chunk(kb.id, doc.id, version.id, n) for n in (1, 2))
        assert first["table_header"] is None  # 首块原文已含表头
        assert second["table_header"] == "| 地点 | 上限 |\n|---|---|\n" and not second["content"].startswith("| 地点")
        payload = evidence_payload("城市11的上限？", [second])
        assert "续表" in payload and payload.index("| 地点 | 上限 |") < payload.index(second["content"].splitlines()[0])
    finally:
        engine.dispose()
