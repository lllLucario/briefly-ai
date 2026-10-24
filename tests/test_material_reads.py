"""按需读取：验证原文、翻页语义、实际加载范围及旧库索引补建。"""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import event, inspect
from sqlalchemy.exc import InvalidRequestError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import DetachedInstanceError

from briefly import config
from briefly.context import Context
from briefly.db import create_db_engine
from briefly.materials import Material, MaterialChunk, MaterialNotFound, MaterialStore, wrap_material
from briefly.tools.materials import material_tools


@pytest.fixture
def store():
    engine = create_db_engine("sqlite://")
    try:
        yield MaterialStore(engine)
    finally:
        engine.dispose()


def invoke(store, name, material_ids=None, **kwargs):
    tool = next(tool for tool in material_tools(store) if tool.name == name)
    return tool.func(runtime=SimpleNamespace(context=Context(material_ids=material_ids)), **kwargs)


@contextmanager
def loaded_rows():
    rows = []

    def record(session, instance):
        if isinstance(instance, (Material, MaterialChunk)):
            rows.append((type(instance), instance.id, "content" not in inspect(instance).unloaded))

    event.listen(Session, "loaded_as_persistent", record)
    try:
        yield rows
    finally:
        event.remove(Session, "loaded_as_persistent", record)


def test_metadata_and_sections_never_load_material_body(store):
    material, _ = store.add_text("材料", "# 第一章\n" + "正文。" * 1000)
    with loaded_rows() as rows:
        metadata = store.get(material.id, content=False)
        assert metadata.title == "材料"
        with pytest.raises((InvalidRequestError, DetachedInstanceError)):
            _ = metadata.content
        assert store.sections(material.id)
        invoke(store, "list_materials")
        invoke(store, "get_outline", material_id=material.id)
    assert rows and all(not has_body for _, _, has_body in rows)
    assert not any(kind is MaterialChunk for kind, _, _ in rows)


def test_single_chunk_loads_only_requested_body_and_preserves_reply(store, monkeypatch):
    monkeypatch.setattr(config, "CHUNK_SIZE", 50)
    monkeypatch.setattr(config, "CHUNK_OVERLAP", 0)
    material, _ = store.add_text("长材料", "\n".join(f"第{i}条原始记录，不得删改。" for i in range(100)))
    chunks = store.chunks(material.id)
    target = chunks[len(chunks) // 2]
    statements = []

    def record(connection, cursor, statement, parameters, context, many):
        statements.append(statement)

    event.listen(store.engine, "before_cursor_execute", record)
    try:
        with loaded_rows() as rows:
            actual = invoke(store, "read_material", material_id=material.id, chunk=target.index)
    finally:
        event.remove(store.engine, "before_cursor_execute", record)
    where = f"（{target.section}）" if target.section else ""
    assert actual == wrap_material(f"【材料 {material.id}《长材料》第 {target.index}/{len(chunks)} 块{where}】", target.content)
    assert [(rid, body) for kind, rid, body in rows if kind is MaterialChunk] == [(target.id, True)]
    assert all(not body for kind, _, body in rows if kind is Material)
    assert len(statements) == 1


@pytest.mark.parametrize("chunk", [None, 0, -1, 9999])
def test_default_and_invalid_chunk_behaviour(store, chunk):
    material, _ = store.add_text("材料", "唯一一块正文。")
    if chunk in (None, 0):
        assert "第 1/1 块" in invoke(store, "read_material", material_id=material.id, chunk=chunk)
    else:
        with pytest.raises(ValueError, match="块号超出范围"):
            invoke(store, "read_material", material_id=material.id, chunk=chunk)


def test_scope_denial_happens_before_any_material_is_loaded(store):
    material, _ = store.add_text("不允许读取", "保密原始文字")
    with loaded_rows() as rows, pytest.raises(ValueError, match="不在用户本次选择"):
        invoke(store, "read_material", material_ids=[], material_id=material.id)
    assert rows == []


def test_missing_material_stays_not_found(store):
    for read in (lambda: store.get(999, content=False), lambda: store.chunks(999), lambda: store.sections(999), lambda: store.read_chunk(999, 1)):
        with pytest.raises(MaterialNotFound):
            read()


@pytest.mark.parametrize("body", [
    "普通正文。",
    "\u3000\u00a0" * 5000 + "正文😀\x1c" + "\u2003\n" * 5000,
    "\u3000\t\n" * 4000,
    "正文\x00后续不得丢失😀。",
    "长章节内容。" * 3000,
], ids=["short", "unicode-whitespace", "whitespace-only", "nul", "long"])
@pytest.mark.parametrize("limit", [20, 50000])
def test_section_slice_preserves_python_strip_and_unicode(store, body, limit):
    content = "前文不应读入。" + body + "另一章不应读入。"
    material, _ = store.add_text("材料", content)
    start, end = len("前文不应读入。"), len("前文不应读入。") + len(body)
    actual, length = store.section_text(material.id, start, end, limit)
    expected = content[start:end].strip()
    assert length == len(expected)
    assert actual == (expected if len(expected) <= limit else None)


def test_long_section_preserves_table_headers_budget_and_continuation(store, monkeypatch):
    monkeypatch.setattr(config, "CHUNK_SIZE", 75)
    monkeypatch.setattr(config, "CHUNK_OVERLAP", 0)
    monkeypatch.setattr(config, "SECTION_READ_CHARS", 100)
    body = "# 表格\n| 任务 | 状态 |\n| --- | --- |\n" + "\n".join(f"| 事项{i} | 保留原始进展 |" for i in range(40))
    material, _ = store.add_text("表格材料", body)
    chunks = store.chunks(material.id)
    target = store.sections(material.id)[0]
    first, last = store.chunk_range(material.id, target.start, target.end)
    selected, size = [], 0
    for chunk in chunks[first - 1:last]:
        if selected and size + chunk.char_count > 100:
            break
        selected.append(chunk)
        size += chunk.char_count
    with loaded_rows() as rows:
        actual = invoke(store, "read_material", material_id=material.id, section=1)
    expected_body = "\n".join(chunk.content for chunk in selected)
    assert f"【材料开始】\n{expected_body}\n【材料结束】" in actual
    assert f"共 {len(material.content[target.start:target.end].strip())} 字" in actual
    assert f"chunk={selected[-1].index + 1}" in actual
    assert all(not has_body for kind, _, has_body in rows if kind is Material)
    assert {rid for kind, rid, _ in rows if kind is MaterialChunk} == {chunk.id for chunk in selected}
    # 第一块超过预算时也保留，避免改变已有的翻页规则。
    assert store.chunk_prefix(material.id, first, last, 1)[0].content == chunks[0].content


def test_short_section_uses_exact_original_span_even_when_block_crosses_sections(store):
    material, _ = store.add_text("材料", "# 第一章\n第一章正文。\n# 第二章\n第二章正文。")
    assert len(store.chunks(material.id)) == 1
    with loaded_rows() as rows:
        actual = invoke(store, "read_material", material_id=material.id, section=2)
    assert "# 第二章\n第二章正文。" in actual
    assert "第一章正文" not in actual
    assert not any(kind is MaterialChunk for kind, _, _ in rows)


def test_long_section_queries_only_bounded_spans_and_selected_chunk_bodies(store):
    material, _ = store.add_text("长材料", "# 正文\n" + "原文必须保留。" * 10000)
    statements = []

    def record(connection, cursor, statement, parameters, context, many):
        statements.append(" ".join(statement.lower().split()))

    event.listen(store.engine, "before_cursor_execute", record)
    try:
        result = invoke(store, "read_material", material_id=material.id, section=1)
    finally:
        event.remove(store.engine, "before_cursor_execute", record)
    assert "较长，本次返回" in result
    assert any("select substr(materials.content" in statement for statement in statements)
    assert not any("select materials.content from" in statement for statement in statements)


def test_invalid_long_section_offsets_fail_instead_of_looping(store):
    material, _ = store.add_text("材料", "短正文。")
    with pytest.raises(ValueError, match="章节位置与材料正文不一致"):
        store.section_text(material.id, 1000, 2000, 20)


def test_existing_database_gets_index_without_rechunking(tmp_path):
    path = tmp_path / "existing.db"
    url = "sqlite:///" + path.as_posix()
    engine = create_db_engine(url)
    try:
        store = MaterialStore(engine)
        material, _ = store.add_text("原材料", "原始文字，不重新分块。")
        before = [(chunk.id, chunk.index, chunk.content) for chunk in store.chunks(material.id)]
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP INDEX ix_material_chunks_material_index")
    finally:
        engine.dispose()
    for _ in range(2):
        engine = create_db_engine(url)
        try:
            indexes = inspect(engine).get_indexes("material_chunks")
            matching = [index for index in indexes if index["name"] == "ix_material_chunks_material_index"]
            assert len(matching) == 1 and matching[0]["column_names"] == ["material_id", "index"]
            assert [(chunk.id, chunk.index, chunk.content) for chunk in MaterialStore(engine).chunks(material.id)] == before
        finally:
            engine.dispose()
