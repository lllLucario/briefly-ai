"""联合检索（三期）：选了知识库时同时检索工作区，合并成一个依据包。覆盖合并预算、出处标注、去重、冲突提示、
未授权库不外发、外发分类计数。假编码器（字词袋）、原创样本，不调用模型。"""
from types import SimpleNamespace

import pytest

pytest.importorskip("sqlite_vec", reason="知识索引回归需安装.[rag]")

from briefly import config
from briefly.db import create_db_engine
from briefly.knowledge.assistant import _turn_egress
from briefly.knowledge.index import LocalIndex
from briefly.knowledge.lifecycle import IndexLifecycle
from briefly.knowledge.payload import JOINT_RULES
from briefly.knowledge.qa import KnowledgeQA
from briefly.knowledge.retrieval import RetrievalService
from briefly.knowledge.service import KnowledgeService
from briefly.links import LinkService
from briefly.materials import KNOWLEDGE, MaterialStore
from briefly.workspace_index import WorkspaceIndex
from test_workspace_index import BagEncoder

POLICY = ("# 门店值班制度\n## 值班安排\n每天开店前三十分钟由值班主管完成设备检查并在系统中签到。\n"
          "值班期间发生客诉，值班主管须在十五分钟内到场处理并记录。\n## 节假日\n节假日值班表由店长在前一周的周五之前发布。")
NOTES = "# 我的值班笔记\n## 本周\n周三客诉两起，值班主管都在十分钟左右到场，处理记录已补齐。\n下周节假日排班还没定。"


@pytest.fixture
def world(tmp_path, monkeypatch):
    engine = create_db_engine("sqlite:///" + (tmp_path / "app.db").as_posix())
    encoder = BagEncoder()
    store = MaterialStore(engine)
    store.workspace_index = WorkspaceIndex(engine, tmp_path / "ws.sqlite", lambda: encoder)
    svc, lifecycle = KnowledgeService(engine), IndexLifecycle(engine, LocalIndex(tmp_path / "kb.sqlite"))
    retrieval = RetrievalService(SimpleNamespace(knowledge=svc, lifecycle=lifecycle, encoder_factory=lambda: encoder))
    kb = svc.create("运营制度")

    def publish(title, content):
        material, _ = store.add_text(title, content, source=KNOWLEDGE)
        doc, version, _ = svc.attach(kb.id, material.id)
        lifecycle.build(kb.id, doc.id, version.id, encoder, expected_revision=doc.revision)
        return doc

    try:
        yield SimpleNamespace(engine=engine, store=store, svc=svc, kb=kb, publish=publish,
                              qa=KnowledgeQA(retrieval, lambda: None, workspace=store))
    finally:
        engine.dispose()


def test_both_sides_in_one_bundle_with_origin_labels(world):
    world.publish("门店值班制度", POLICY)
    world.store.add_text("我的值班笔记", NOTES)
    world.svc.set_egress(world.kb.id, True)
    bundle = world.qa.bundle(world.kb.id, ["值班主管客诉多久到场"])
    assert bundle["status"] == "ready"
    origins = {e.get("origin") for e in bundle["evidence"]}
    assert origins == {"knowledge", "workspace"}
    text = bundle["payload_text"]
    assert text.startswith(JOINT_RULES)  # 两类出处怎么用：问制度以知识库为准，处理自己的工作以工作区为准
    assert "出处：知识库·已发布｜运营制度｜第1版" in text
    assert "出处：工作区·你的文件｜我的值班笔记｜更新时间 " in text
    refs = {r["origin"] for r in bundle["references"]}
    assert refs == {"knowledge", "workspace"}
    workspace_ref = next(r for r in bundle["references"] if r["origin"] == "workspace")
    assert workspace_ref["material_id"] and workspace_ref["reference"].startswith("wsref:")
    # 外发分类计数
    kb_count = sum(1 for e in bundle["evidence"] if e.get("origin") == "knowledge")
    assert bundle["egress"]["evidence_sent"] == kb_count and bundle["egress"]["workspace_sent"] == len(bundle["evidence"]) - kb_count


def test_budget_split_and_leftover(world, monkeypatch):
    from briefly.knowledge.payload import evidence_payload
    monkeypatch.setattr(config, "KNOWLEDGE_CONTEXT_TOKENS", 3000)
    monkeypatch.setattr(config, "WORKSPACE_CONTEXT_TOKENS", 800)
    rule = "值班主管客诉到场处理记录签到检查。"
    for i in range(12):
        world.publish(f"制度{i}", f"# 制度{i}\n## 条款\n{rule}第{i}条补充说明，适用于{i}号门店。" * 2)
    for i in range(12):
        world.store.add_text(f"笔记{i}", f"# 笔记{i}\n{rule}我的第{i}次值班记录，客诉已处理。" * 2)
    world.svc.set_egress(world.kb.id, True)
    encoder = BagEncoder()

    def size(items):
        return encoder.token_length(evidence_payload("q", items)) - encoder.token_length(evidence_payload("q", []))

    bundle = world.qa.bundle(world.kb.id, ["值班主管客诉到场"])
    ws = [e for e in bundle["evidence"] if e["origin"] == "workspace"]
    kb = [e for e in bundle["evidence"] if e["origin"] == "knowledge"]
    assert ws and kb and encoder.token_length(bundle["payload_text"]) <= 3000
    # 知识库最多用到“总额 − 工作区额度”，工作区拿到了它那一份（片段约两百字一条，装进了三条以上）
    assert size(kb) <= 3000 - 800 and len(ws) >= 3
    # 知识库没有内容时，工作区可以用到总额（余量给另一侧）
    empty_kb = world.svc.create("空库")
    result = world.qa.bundle(empty_kb.id, ["值班主管客诉到场"])
    assert result["status"] == "ready" and all(e["origin"] == "workspace" for e in result["evidence"])
    assert size(result["evidence"]) > 800 or len(result["evidence"]) > len(ws)


def test_linked_identical_passage_keeps_only_knowledge_and_notes_it(world):
    doc = world.publish("门店值班制度", POLICY)
    copy, _ = LinkService(world.engine).copy_to_workspace(world.store, world.kb.id, doc.id)
    world.svc.set_egress(world.kb.id, True)
    bundle = world.qa.bundle(world.kb.id, ["值班主管客诉多久到场"])
    assert all(e.get("origin") != "workspace" or e["material_id"] != copy.id for e in bundle["evidence"])
    assert "工作区版本此处一致" in bundle["payload_text"]


def test_linked_revision_with_different_wording_keeps_both_and_flags_it(world):
    doc = world.publish("门店值班制度", POLICY)
    links = LinkService(world.engine)
    revised, _ = world.store.add_text("门店值班制度-修订稿", POLICY.replace("十五分钟", "十分钟"))
    links.detect()
    links.decide(links.material_links(revised.id)[0]["id"], True)
    world.svc.set_egress(world.kb.id, True)
    bundle = world.qa.bundle(world.kb.id, ["值班主管客诉多久到场"])
    ws = [e for e in bundle["evidence"] if e.get("origin") == "workspace" and e["material_id"] == revised.id]
    assert ws and any("此处写法不同" in e.get("comparison", "") for e in ws)
    assert "与知识库《门店值班制度》第1版此处写法不同" in bundle["payload_text"]
    assert "十分钟" in bundle["payload_text"] and "十五分钟" in bundle["payload_text"]  # 冲突两条都给
    # 关联的工作区文件比发布版新：同时有改稿提醒
    assert any("工作区有更新" in w for w in bundle["warnings"])


def test_unauthorized_library_copy_never_sent(world):
    other = world.svc.create("人事制度（未授权）")
    material, _ = world.store.add_text("考勤制度", "# 考勤制度\n迟到三次扣除全勤奖，值班主管负责登记。", source=KNOWLEDGE)
    doc, version, _ = world.svc.attach(other.id, material.id)
    world.qa.retrieval.imports.lifecycle.build(other.id, doc.id, version.id, BagEncoder(), expected_revision=doc.revision)
    LinkService(world.engine).copy_to_workspace(world.store, other.id, doc.id)
    world.publish("门店值班制度", POLICY)
    world.svc.set_egress(world.kb.id, True)
    bundle = world.qa.bundle(world.kb.id, ["值班主管负责什么"])
    assert "全勤奖" not in bundle.get("payload_text", "")  # 关联到未授权库的工作区副本不外发
    assert any("人事制度（未授权）" in w and "尚未允许外发" in w for w in bundle["warnings"])


def test_selected_library_not_authorized_sends_only_workspace(world):
    world.publish("门店值班制度", POLICY)
    plain = KnowledgeQA(world.qa.retrieval, lambda: None, workspace=world.store)
    assert plain.bundle(world.kb.id, ["值班主管客诉多久到场"])["status"] == "external_authorization_required"  # 工作区没东西：照旧提示授权
    world.store.add_text("我的值班笔记", NOTES)
    bundle = world.qa.bundle(world.kb.id, ["值班主管客诉多久到场"])
    # 所选库未授权：只发工作区片段，知识库片段不发并说明
    assert bundle["status"] == "ready" and bundle["evidence"]
    assert all(e["origin"] == "workspace" for e in bundle["evidence"])
    assert "十五分钟" not in bundle["payload_text"] and "出处：知识库·已发布" not in bundle["payload_text"]
    assert bundle["egress"]["evidence_sent"] == 0 and bundle["egress"]["workspace_sent"] >= 1
    assert any("尚未允许外发" in w and "只依据你的工作区文件" in w for w in bundle["warnings"])
    empty = world.svc.create("空库")
    only = world.qa.bundle(empty.id, ["值班主管客诉多久到场"])
    assert only["status"] == "ready" and only["egress"]["evidence_sent"] == 0 and only["egress"]["workspace_sent"] >= 1


def test_workspace_quality_constraints_are_withheld(world):
    world.publish("门店值班制度", POLICY)
    world.store.add_text("排班表", "| 门店 | 值班主管 |\n|---|---|\n| 一号店 | 小李 |\n", report={"table_structure_status": "partial"})
    world.svc.set_egress(world.kb.id, True)
    bundle = world.qa.bundle(world.kb.id, ["一号店值班主管是谁"])
    assert "小李" not in bundle.get("payload_text", "")
    assert any("结构仍有未确认部分" in w and "排班表" in w for w in bundle["warnings"])


def test_without_workspace_index_bundle_is_unchanged(world):
    world.publish("门店值班制度", POLICY)
    world.store.add_text("我的值班笔记", NOTES)
    world.svc.set_egress(world.kb.id, True)
    plain = KnowledgeQA(world.qa.retrieval, lambda: None).bundle(world.kb.id, ["值班主管客诉多久到场"])
    assert all("origin" not in e for e in plain["evidence"])
    assert plain["payload_text"].startswith("问题：") and "出处：" not in plain["payload_text"]
    assert "workspace_sent" in plain["egress"] and plain["egress"]["workspace_sent"] == 0


def test_assistant_egress_counts_workspace_separately():
    from langchain_core.messages import AIMessage
    searches = [dict(references=[dict(label="依据1", origin="knowledge"), dict(label="依据2", origin="workspace"),
                                 dict(label="依据3", origin="workspace")], egress={"redactions": {}})]
    turn = [AIMessage("", response_metadata={"egress_sent": {"evidence": ["依据1", "依据2", "依据3"], "history_evidence": 0, "materials": 0}})]
    egress = _turn_egress(turn, searches)
    assert egress["evidence_sent"] == 1 and egress["workspace_sent"] == 2


SLIDES = ("# 第9页 协商解除的特点\n\n- 1、不受法律解除限制性规定的调整；\n- 2、不用提前30日书面通知；\n"
          "- 3、需要支付经济补偿金，但最多支付\n\n12个月；（用人单位提出解除的）\n\n- 4、权利义务可以一次性处理。\n\n"
          "# 第10页 补偿计算\n\n个别补偿根据员工在本单位工作年限决定，每满一年支付一个月工资。\n")


def _revision(world, title, content):
    links = LinkService(world.engine)
    revised, _ = world.store.add_text(title, content)
    links.detect()
    links.decide(links.material_links(revised.id)[0]["id"], True)
    world.svc.set_egress(world.kb.id, True)
    return revised


def test_short_number_line_change_is_not_marked_identical(world):
    # 幻灯片里数字常单独成句：“14个月；”去掉标点只有4个字，原先不参与对照，改稿被当成一致删掉并错标知识库片段
    world.publish("离职管理课件", SLIDES)
    revised = _revision(world, "离职管理课件（部门修订稿）", SLIDES.replace("12个月", "14个月"))
    bundle = world.qa.bundle(world.kb.id, ["协商解除用人单位提出时补偿最多支付几个月"])
    ws = [e for e in bundle["evidence"] if e.get("origin") == "workspace" and e["material_id"] == revised.id]
    assert ws and any("“14个月”" in e.get("comparison", "") for e in ws)
    kb = [e for e in bundle["evidence"] if e.get("origin") == "knowledge" and "12个月" in e["content"]]
    assert kb and not any("此处一致" in e.get("comparison", "") for e in kb)
    assert any("“14个月”" in e.get("comparison", "") for e in kb)  # 不同之处也写在知识库这条上
    assert "14个月" in bundle["payload_text"] and "12个月" in bundle["payload_text"]


def test_deleted_sentence_is_reported_from_the_knowledge_side(world):
    # 工作区删掉一句：剩下的句子都在知识库里，但知识库那段有工作区没有的句子，不能标“此处一致”
    world.publish("离职管理课件", SLIDES)
    deleted = "个别补偿根据员工在本单位工作年限决定，每满一年支付一个月工资。"
    revised = _revision(world, "离职管理课件（部门修订稿）", SLIDES.replace(deleted, "整体补偿另行规定。"))
    bundle = world.qa.bundle(world.kb.id, ["个别补偿根据什么决定"])
    kb = [e for e in bundle["evidence"] if e.get("origin") == "knowledge" and "个别补偿" in e["content"]]
    assert kb and not any("此处一致" in e.get("comparison", "") for e in kb)
    ws = [e for e in bundle["evidence"] if e.get("origin") == "workspace" and e["material_id"] == revised.id]
    assert ws and any("在工作区文件中没有" in e.get("comparison", "") and "个别补偿" in e["comparison"] for e in ws)


def test_identical_copy_note_names_the_workspace_file(world):
    doc = world.publish("门店值班制度", POLICY)
    LinkService(world.engine).copy_to_workspace(world.store, world.kb.id, doc.id)
    world.svc.set_egress(world.kb.id, True)
    bundle = world.qa.bundle(world.kb.id, ["值班主管客诉多久到场"])
    assert "工作区版本此处一致（你的文件《门店值班制度" in bundle["payload_text"] and "已逐句核对" in bundle["payload_text"]
    assert "可以直接据此说明两份一致" in bundle["payload_text"]  # JOINT_RULES 说明注记用法


def test_kept_revision_is_not_squeezed_out_by_knowledge_notes(world, monkeypatch):
    # 素材库第二轮复跑 MC03：对照注记让知识库片段变长、超出知识库那一份额度，对照后保留的工作区片段反而进不了依据包。
    # 工作区保底额度内的片段先留位置，挤不下时去掉知识库末尾的片段
    monkeypatch.setattr(config, "KNOWLEDGE_CONTEXT_TOKENS", 3000)
    monkeypatch.setattr(config, "WORKSPACE_CONTEXT_TOKENS", 800)
    rule = "值班主管客诉到场处理记录签到检查。"
    for i in range(12):
        world.publish(f"制度{i}", f"# 制度{i}\n## 条款\n{rule}第{i}条补充说明，适用于{i}号门店。" * 2)
    for i in range(3):
        world.store.add_text(f"笔记{i}", f"# 笔记{i}\n{rule}我的第{i}次值班记录，客诉已处理。" * 2)
    world.svc.set_egress(world.kb.id, True)
    compare = world.qa._compare

    def noted(knowledge, kb_items, ws_items):
        kept = compare(knowledge, kb_items, ws_items)
        for e in kb_items:
            e["comparison"] = "工作区版本此处一致（你的文件《一份标题很长的部门修订稿》，已逐句核对）" * 2
        return kept

    monkeypatch.setattr(world.qa, "_compare", noted)
    bundle = world.qa.bundle(world.kb.id, ["值班主管客诉到场"])
    assert sum(e["origin"] == "workspace" for e in bundle["evidence"]) >= 2
    assert BagEncoder().token_length(bundle["payload_text"]) <= 3000


def test_joint_rules_state_priority_by_scene():
    # 2026-11-02 用户确定：问制度以知识库为准、处理自己的稿子以工作区为准、问哪版优先直接回答；不替用户裁决只留给同类依据
    assert "现行以知识库已发布版为准" in JOINT_RULES and "以工作区为准" in JOINT_RULES
    assert "未发布的改动不能当作现行规定" in JOINT_RULES and "要生效需走发布" in JOINT_RULES
    assert "同为知识库或同为工作区" in JOINT_RULES and "不替用户裁决" in JOINT_RULES
