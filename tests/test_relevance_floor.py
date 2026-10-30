import logging

import pytest

from briefly import config, relevance_floor
from briefly.intent import IntentFunnel, TurnState
from briefly.knowledge.service import fingerprint
from briefly.relevance_floor import check_indexes, kb_floor
from test_knowledge_index import Encoder, indexed  # noqa: F401  夹具

IDENTITY = {"model": "fake/registered", "revision": "r1", "dimension": 2}
OTHER = {"model": "fake/new-model", "revision": "r1", "dimension": 2}
STATE = TurnState(kb_id="kb")


class IdentityEncoder:
    def __init__(self, identity):
        self.identity = identity

    def embed_documents(self, texts):
        return [[1.0, 0.0] for _ in texts]


@pytest.fixture(autouse=True)
def registry(monkeypatch):
    monkeypatch.setattr(config, "INTENT_KB_THRESHOLD", None)
    monkeypatch.setattr(relevance_floor, "REGISTERED", {fingerprint(IDENTITY): 0.42})


def funnel(identity, score, calls=None):
    def probe(kb_id, text):
        if calls is not None:
            calls.append(text)
        return score
    return IntentFunnel(lambda: IdentityEncoder(identity), {}, kb_probe=probe)


def test_registered_fingerprint_uses_its_floor():
    assert kb_floor(IDENTITY) == 0.42
    assert kb_floor(fingerprint(IDENTITY)) == 0.42  # 也可直接传指纹
    # 索引代次里存的身份带来源契约，不影响模型指纹
    assert kb_floor({**IDENTITY, "index_contract": {"embedding_fingerprint": fingerprint(IDENTITY)}}) == 0.42
    assert funnel(IDENTITY, 0.45).classify("年假有几天", STATE).intent == "knowledge"
    assert funnel(IDENTITY, 0.40).classify("年假有几天", STATE).intent == "agent"


def test_unregistered_model_skips_kb_routing_and_warns(caplog):
    assert kb_floor(OTHER) is None and kb_floor(None) is None
    calls = []
    f = funnel(OTHER, 0.99, calls)
    with caplog.at_level(logging.WARNING, logger="briefly.intent"):
        assert f.classify("年假有几天", STATE).intent == "agent"
        assert f.classify("报销流程在哪个文件里", STATE).intent == "agent"
    assert calls == []  # 跳过路由时不做检索
    warnings = [r.getMessage() for r in caplog.records]
    assert len(warnings) == 1 and "fake/new-model" in warnings[0] and "--sweep" in warnings[0]


def test_config_override_wins(monkeypatch):
    monkeypatch.setattr(config, "INTENT_KB_THRESHOLD", 0.3)
    assert kb_floor(IDENTITY) == 0.3 and kb_floor(OTHER) == 0.3 and kb_floor(None) == 0.3
    assert funnel(OTHER, 0.35).classify("年假有几天", STATE).intent == "knowledge"
    assert funnel(IDENTITY, 0.35).classify("年假有几天", STATE).intent == "knowledge"  # 低于登记值0.42，仍用覆盖值


def test_startup_check_reports_ready_indexes_without_floor(indexed, caplog, monkeypatch):
    engine, store, svc, lifecycle = indexed
    mat, _ = store.add_text("制度", "住宿上限650元。")
    kb = svc.create("制度库")
    doc, version, _ = svc.attach(kb.id, mat.id)
    lifecycle.build(kb.id, doc.id, version.id, Encoder(), expected_revision=1)
    with caplog.at_level(logging.WARNING, logger="briefly.relevance_floor"):
        messages = check_indexes(engine)
    assert len(messages) == 1 and Encoder.identity["model"] in messages[0] and caplog.records
    monkeypatch.setattr(relevance_floor, "REGISTERED", {fingerprint(Encoder.identity): 0.5})
    assert check_indexes(engine) == []
