"""词法候选的精确排序/范围/同快照验证；只用假向量和隔离SQLite。"""
import importlib
from pathlib import Path
import sqlite3

import pytest


@pytest.fixture
def index(monkeypatch,tmp_path):
    pytest.importorskip("sqlite_vec")
    monkeypatch.syspath_prepend(str(Path(__file__).parent/"eval"))
    support = importlib.import_module("w3_retrieval_support")
    candidate = support.CandidateIndex(tmp_path/"scope.db",dict(model="fake",dimension=2),backend="scalar")
    try:
        yield candidate
    finally:
        candidate.close()


@pytest.fixture
def search(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parent/"eval"))
    return importlib.import_module("w3_scoped_lexical").bounded_lexical


def populate(index):
    index.replace_document("outside",[(1,"审批 报销 111 E403",[1.,0.]),(1000,"报销",[1.,0.])])
    index.replace_document("a",[(20,"报销 111",[1.,0.]),(22,"审批 111 E403",[1.,0.])])
    index.replace_document("b",[(200,"审批 报销",[1.,0.]),(203,"E403",[1.,0.])])
    index.replace_document("gap-outside",[(21,"审批 报销 111 E403",[1.,0.])])


@pytest.mark.parametrize("scope",[["a"],["b"],["a","b"],["a","a","b"],["missing"],[]])
def test_bounded_results_keep_global_scores_and_scoped_topk(index,search,scope):
    populate(index)
    for query in ("审批 报销 111 E403",'" OR 1=1 --',"E403", "不存在ZZ991", ""):
        for k in (1,3,10):
            actual,plan = search(index,query,scope,k)
            assert actual == index.lexical(query,scope,k)
            assert all(index.contents(ident)[0] in scope for ident,_ in actual)
    if scope and "missing" not in scope:
        assert search(index,"审批",scope)[1]["route"] == "bounded"


def test_update_high_ids_delete_restart_and_fallback(index,search):
    populate(index)
    index.replace_document("a",[(1100,"E403 更新",[1.,0.])])
    assert search(index,"E403",["a"])[0] == index.lexical("E403",["a"])
    assert search(index,"E403",["outside"])[1]["route"] == "baseline_wide_span"
    assert search(index,"E403",["a","b"],max_documents=1)[1]["route"] == "baseline_many_documents"
    index.delete_document("a")
    assert search(index,"E403",["a"])[0] == []
    assert not index.db.in_transaction


def test_interval_metadata_and_search_share_read_snapshot(index,search):
    index.db.execute("PRAGMA journal_mode=WAL")
    populate(index)
    second = type(index)(index.path,dict(model="fake",dimension=2),backend="scalar")
    changed = []
    def after_snapshot(sql):
        if "SELECT max(id) FROM chunks" in sql and not changed:
            changed.append(True)
            second.replace_document("a",[(1200,"审批 E403 新版",[1.,0.])])
    index.db.set_trace_callback(after_snapshot)
    try:
        old,plan = search(index,"E403",["a"])
        assert changed and old[0][0] == 22
    finally:
        index.db.set_trace_callback(None)
        second.close()
    assert search(index,"E403",["a"])[0][0][0] == 1200


def test_query_failure_releases_savepoint(index,search):
    populate(index)
    def reject_select(action,*args):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ else sqlite3.SQLITE_OK
    index.db.set_authorizer(reject_select)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            search(index,"审批",["a"])
    finally:
        index.db.set_authorizer(None)
    assert not index.db.in_transaction
    assert search(index,"审批",["a"])[0]
