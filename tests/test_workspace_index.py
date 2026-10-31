"""工作区语义索引（一期）：增量更新只重算改动切片、检索前对齐、追踪新版本替换旧版本、知识库专属材料不进索引、
向量未就绪时回落关键词、相关度下限与质量限制标注。假编码器、原创样本，不加载真实模型。"""
import hashlib
import math

import pytest

from briefly import config
from briefly.db import create_db_engine
from briefly.materials import KNOWLEDGE, MaterialStore
from briefly.workspace_index import WorkspaceIndex, attach
from test_knowledge import fake_identity
from test_materials import run_tool
from tracking_util import env  # noqa: F401  追踪夹具


class BagEncoder:
    """按字的哈希做词袋向量：字面相近的文字向量相近，足够检验语义路排序；统计向量化了多少条。"""
    identity = fake_identity(dimension=32)

    def __init__(self):
        self.embedded: list[str] = []

    @staticmethod
    def token_length(text):
        return len(text)

    @staticmethod
    def _vector(text):
        values = [0.0] * 32
        for ch in text:
            if not ch.isspace():
                values[int(hashlib.md5(ch.encode()).hexdigest(), 16) % 32] += 1
        values[0] += 0.01  # 不出现全零向量
        return values

    def embed_documents(self, texts):
        self.embedded.extend(texts)
        return [self._vector(t) for t in texts]

    def embed_query(self, text):
        return self._vector(text)


@pytest.fixture
def setup(tmp_path):
    engine = create_db_engine("sqlite:///" + (tmp_path / "app.db").as_posix())
    store = MaterialStore(engine)
    encoder = BagEncoder()
    index = WorkspaceIndex(engine, tmp_path / "workspace.sqlite", lambda: encoder)
    try:
        yield store, index, encoder
    finally:
        index.wait()
        engine.dispose()


PLAN = ("# 季度方案\n## 背景\n门店扫码点单上线后，午高峰排队时间仍然偏长，需要优化出餐流程。\n"
        "## 目标\n午高峰平均等待时间从十二分钟降到八分钟以内。\n"
        "## 人员\n后厨增加一名打包员，前台由店长兼任调度。\n"
        "## 预算\n本季度设备采购预算三万元，用于加装第二台出餐提示屏。\n")


def vectors(index):
    with index.connect() as db:
        return db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]


def test_incremental_update_only_reembeds_changed_slices(setup):
    store, index, encoder = setup
    old, _ = store.add_text("季度方案", PLAN)
    first = index.sync(encoder)
    assert first["added"] == 1 and first["pending"] == 0
    total = len(encoder.embedded)
    assert total >= 4  # 按章节切成多片
    encoder.embedded.clear()
    # 改稿：只改“预算”一节。旧内容移出工作区，新内容加入（与重新上传、追踪新版本同一情形）
    store.delete(old.id)
    store.add_text("季度方案", PLAN.replace("三万元", "四万五千元"))
    second = index.sync(encoder)
    assert second["removed"] == 1 and second["added"] == 1
    assert len(encoder.embedded) == 1 and "四万五千元" in encoder.embedded[0]
    assert vectors(index) == total  # 旧“预算”切片的向量已清理，其余复用


def test_search_aligns_with_latest_workspace_before_retrieval(setup):
    store, index, _ = setup
    a, _ = store.add_text("季度方案", PLAN)
    index.sync(index.encoder())
    b, _ = store.add_text("巡店记录", "周三巡店发现冷柜温度偏高，已通知维修，下周复查。")
    # 没有再手动同步：检索前先对齐，新材料立刻可检索
    hits = index.search("冷柜温度")
    assert hits and hits[0].material_id == b.id and "冷柜" in hits[0].snippet
    assert hits[0].start is not None and hits[0].snippet == store.get(b.id).content[hits[0].start:hits[0].end].strip()
    store.delete(b.id)
    assert all(h.material_id != b.id for h in index.search("冷柜温度"))
    assert index.search("出餐提示屏")[0].material_id == a.id


def test_tracked_new_version_replaces_old_in_index(env, tmp_path):  # noqa: F811
    encoder = BagEncoder()
    index = WorkspaceIndex(env.store.engine, tmp_path / "ws.sqlite", lambda: encoder)
    env.write("方案.md", "# 方案\n## 进度\n第一版：完成选址。\n## 风险\n供应商报价未定。")
    env.svc.add_paths([str(env.work / "方案.md")])
    index.sync(encoder)
    first = [h.material_id for h in index.search("选址")]
    assert first
    encoder.embedded.clear()
    env.write("方案.md", "# 方案\n## 进度\n第二版：完成选址和装修设计。\n## 风险\n供应商报价未定。")
    env.svc.scan()
    hits = index.search("装修设计")
    assert hits and hits[0].material_id not in first
    with index.connect() as db:
        indexed = {row[0] for row in db.execute("SELECT material_id FROM entries")}
    assert indexed == {hits[0].material_id}  # 旧版本移出工作区索引
    assert not any("供应商报价" in t for t in encoder.embedded)  # 没改的“风险”一节复用向量


def test_knowledge_only_material_is_not_indexed(setup):
    store, index, encoder = setup
    store.add_text("差旅制度", "一线城市住宿上限六百元。", source=KNOWLEDGE)
    store.add_text("周报", "本周完成门店盘点。")
    index.sync(encoder)
    with index.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 1
    assert all("住宿" not in h.snippet for h in index.search("住宿上限"))
    assert not any("住宿" in t for t in encoder.embedded)


def test_knowledge_only_material_explicitly_requested_is_refused_by_tool(setup):
    store, index, _ = setup
    store.workspace_index = index
    secret, _ = store.add_text("差旅制度", "一线城市住宿上限六百元。", source=KNOWLEDGE)
    result = run_tool(store, "search_materials", {"keywords": "住宿", "material_ids": [secret.id]})
    assert "属于知识库" in result.content and "六百" not in result.content


def test_pending_vectors_fall_back_to_keywords_and_say_so(setup):
    store, _, encoder = setup
    index = WorkspaceIndex(store.engine, setup[1].path, lambda: encoder, inline_embed=0)
    store.workspace_index = index
    store.add_text("巡店记录", "周三巡店发现冷柜温度偏高，已通知维修。")
    hits = index.search("冷柜")
    assert hits and hits[0].keyword_only and hits[0].similarity is None
    text = run_tool(store, "search_materials", {"keywords": "冷柜"}).content
    assert "语义索引更新中，此条仅按关键词命中" in text
    index.sync(encoder)  # 后台把向量算完
    hits = index.search("冷柜")
    assert not hits[0].keyword_only and hits[0].similarity is not None


def test_relevance_floor_drops_unrelated_semantic_neighbours(setup, monkeypatch):
    store, index, _ = setup
    store.add_text("季度方案", PLAN)
    store.add_text("巡店记录", "周三巡店发现冷柜温度偏高，已通知维修，下周复查。")
    monkeypatch.setattr(config, "INTENT_KB_THRESHOLD", 0.99)
    # 语义上谁都够不到 0.99：只剩关键词命中的那条
    hits = index.search("冷柜")
    assert [h.title for h in hits] == ["巡店记录"]
    assert index.search("火星探测") == []


def test_quality_constraints_are_labelled_on_hits(setup):
    store, index, _ = setup
    store.workspace_index = index
    report = {"table_structure_status": "partial"}
    store.add_text("排班表", "| 门店 | 早班 |\n|---|---|\n| 一号店 | 小李 |\n", report=report)
    hits = index.search("一号店 早班")
    assert hits and "单元格归属" in hits[0].notes[0]
    # 助理路径上，工具结果的 quality_notice 仍照旧让回答检查拦下（不因建索引放宽），这里只看检索结果本身


def test_tool_uses_hybrid_results_and_keeps_output_shape(setup):
    store, index, _ = setup
    store.workspace_index = index
    material, _ = store.add_text("季度方案", PLAN)
    result = run_tool(store, "search_materials", {"keywords": "加装第二台出餐提示屏要花多少钱"})
    assert f"【材料 {material.id}《季度方案》第 1 块（季度方案 > 预算）】" in result.content
    assert "三万元" in result.content and "命中" not in result.content
    assert result.artifact["material_ids"] == [material.id]


def test_without_encoder_tool_keeps_keyword_search(setup):
    store, _, _ = setup
    def missing():
        raise ValueError("尚未配置本地Embedding目录")
    store.workspace_index = WorkspaceIndex(store.engine, setup[1].path, missing)
    store.add_text("t", "接口联调完成")
    assert "第 1 块，命中 1 次" in run_tool(store, "search_materials", {"keywords": "联调"}).content


def test_explicit_old_tracked_version_uses_keyword_search(env, tmp_path):  # noqa: F811
    env.write("方案.md", "# 方案\n第一版：完成选址。")
    env.svc.add_paths([str(env.work / "方案.md")])
    old = env.store.list_materials()[0].id
    env.write("方案.md", "# 方案\n第二版：完成装修。")
    env.svc.scan()
    encoder = BagEncoder()
    attach(env.store, lambda: encoder)
    assert env.store.workspace_index.search("选址", [old]) is None  # 不在索引里：交给原关键词检索
    text = run_tool(env.store, "search_materials", {"keywords": "选址", "material_ids": [old]}).content
    assert "命中 1 次" in text
    env.store.workspace_index.wait()


def test_material_changes_schedule_background_sync(setup):
    store, index, encoder = setup
    store.listeners.append(index.schedule)
    store.add_text("巡店记录", "周三巡店发现冷柜温度偏高。")
    index.wait()
    with index.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] >= 1
    assert math.isclose(len(encoder.embedded), 1)
