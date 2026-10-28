"""隔离选型原型的范围、事务与向量身份边界；全部使用确定性假向量。"""
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def support():
    pytest.importorskip("sqlite_vec")
    path=Path(__file__).parent/"eval"/"w3_retrieval_support.py"
    spec=importlib.util.spec_from_file_location("w3_retrieval_checks",path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(params=["scalar","vec0"])
def index(request,support,tmp_path):
    index=support.CandidateIndex(tmp_path/"index.db",{"model":"fake","dimension":2},backend=request.param)
    try:
        yield index
    finally:
        index.close()


def test_scope_before_topk(index):
    index.replace_document("outside",[(1,"报销",[1.,0.])])
    index.replace_document("inside",[(2,"报销",[.1,.9])])
    assert index.dense([1.,0.],["inside"],1)[0][0]==2
    assert index.lexical("报销",["inside"],1)[0][0]==2
    assert index.dense([1.,0.],[],1)==[]
    assert index.dense([1.,0.],["missing"],1)==[]


def test_mutations_and_rollback(index):
    import sqlite3
    index.replace_document("a",[(1,"E413 超过20MB",[1.,0.])])
    index.replace_document("a",[(2,"E201 会话过期",[0.,1.])])
    assert index.contents(1) is None
    assert not index.lexical("E413",["a"])
    assert index.lexical("E201",["a"])[0][0]==2
    index.replace_document("b",[(3,"其他",[1.,0.])])
    with pytest.raises(sqlite3.IntegrityError):
        index.replace_document("a",[(3,"冲突",[1.,0.])])
    assert index.contents(2) is not None
    assert index.lexical("E201",["a"])[0][0]==2
    index.delete_document("a")
    assert not index.dense([0.,1.],["a"])
    assert not index.lexical("E201",["a"])


@pytest.mark.parametrize("vector",[[1.],[float("nan"),0.],[float("inf"),1.],[0.,0.]])
def test_invalid_vectors_leave_existing_content(index,vector):
    index.replace_document("a",[(1,"旧内容",[1.,0.])])
    with pytest.raises(ValueError):
        index.replace_document("a",[(2,"新内容",vector)])
    assert index.contents(1)[1]=="旧内容"


def test_restart_and_model_dimension_guard(support,tmp_path):
    path=tmp_path/"index.db"
    identity={"model":"fake","dimension":2,"revision":"one"}
    index=support.CandidateIndex(path,identity)
    try:
        index.replace_document("a",[(1,"元",[1.,0.])])
    finally:
        index.close()
    index=support.CandidateIndex(path,identity)
    try:
        assert index.dense([1.,0.],["a"],1)[0][0]==1
        assert index.lexical("元",["a"],1)[0][0]==1
    finally:
        index.close()
    for changed in ({**identity,"dimension":3},{**identity,"revision":"two"}):
        with pytest.raises(ValueError,match="新建索引"):
            support.CandidateIndex(path,changed)


def test_special_tokens_and_rrf(support,index):
    index.replace_document("a",[(1,'橙叶111元 E403 20MB C++ "引号"',[1.,0.])])
    for query in ("111","E403","20MB","C++",'"引号"',"元"):
        assert index.lexical(query,["a"]), query
    assert support.rrf([[(1,99.),(2,.1)],[(2,-500.),(1,1000.)]])==[(1,1/61+1/62),(2,1/61+1/62)]
