from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from briefly.db import Base, create_db_engine, foreign_key_problems
from briefly.knowledge.models import IndexGeneration, KnowledgeDocument, KnowledgePublication, KnowledgeVersion
from briefly.knowledge.service import KnowledgeConflict, KnowledgeNotFound, KnowledgeService
from briefly.materials import MaterialChunk, MaterialSection, MaterialStore


@pytest.fixture
def resources():
    engine = create_db_engine("sqlite://")
    yield engine, MaterialStore(engine), KnowledgeService(engine)
    engine.dispose()


def publish_fake(engine, version):
    """模拟后续构建器的元数据，完全不加载Embedding/索引，不表示真实索引已验收。"""
    with Session(engine) as s, s.begin():
        generation = IndexGeneration(version_id=version.id, embedding_identity={"model":"fake","dimension":2},
                                     dimension=2, chunk_fingerprint=version.chunk_fingerprint, status="ready")
        s.add(generation)
        s.flush()
        publication = s.get(KnowledgePublication, version.document_id)
        if publication is None:
            s.add(KnowledgePublication(document_id=version.document_id, version_id=version.id, generation_id=generation.id))
        else:
            publication.version_id, publication.generation_id = version.id, generation.id
        return generation.id


def test_explicit_membership_multi_library_and_no_copy(resources):
    engine, store, svc = resources
    mat,_ = store.add_text("制度", "上海住宿上限650元。")
    a,b = svc.create("制度库"),svc.create("手册库")
    da,va,created = svc.attach(a.id,mat.id)
    db,vb,_ = svc.attach(b.id,mat.id)
    assert created and va.material_id == vb.material_id == mat.id
    assert va.id != vb.id and da.id != db.id
    assert len(store.list_materials()) == 1
    assert svc.attach(a.id,mat.id)[2] is False
    assert svc.scope([a.id])["status"] == "not_ready"
    publish_fake(engine, va)
    assert svc.scope([a.id])["material_ids"] == [mat.id]
    assert svc.scope([b.id])["material_ids"] == []
    assert svc.scope([])["status"] == "empty_scope"
    with pytest.raises(KnowledgeNotFound):
        svc.scope([a.id,"missing"])


def test_cross_library_and_version_references_rejected(resources):
    _,store,svc=resources
    mat,_=store.add_text("制度","审批在订房前完成。")
    a,b=svc.create("A"),svc.create("B")
    doc,ver,_=svc.attach(a.id,mat.id)
    ref=svc.chunk(a.id,doc.id,ver.id,1)["reference"]
    with pytest.raises(KnowledgeNotFound):
        svc.chunk(b.id,doc.id,ver.id,1)
    with pytest.raises(KnowledgeNotFound):
        svc.resolve_reference(b.id,ref)
    other,_,_=svc.attach(b.id,mat.id)
    with pytest.raises(KnowledgeNotFound):
        svc.chunk(b.id,other.id,ver.id,1)


def test_stage_version_preserves_publication_and_old_reference(resources):
    engine,store,svc=resources
    first,_=store.add_text("制度v1","住宿上限500元。")
    second,_=store.add_text("制度v2","住宿上限650元。")
    kb=svc.create("制度")
    doc,old,_=svc.attach(kb.id,first.id,source_key="travel",business_version="v1")
    publish_fake(engine,old)
    ref=svc.chunk(kb.id,doc.id,old.id,1)["reference"]
    doc,new,created=svc.append_version(kb.id,doc.id,second.id,expected_revision=1,business_version="v2")
    assert created and doc.revision == 2 and new.number == 2
    assert svc.scope([kb.id])["material_ids"] == [first.id]
    generation=svc.plan_index(kb.id,doc.id,new.id,{"model":"fake","dimension":2})
    assert generation.status == "planned"
    assert svc.scope([kb.id])["material_ids"] == [first.id]
    publish_fake(engine,new)
    resolved=svc.resolve_reference(kb.id,ref)
    assert resolved["content"] == "住宿上限500元。" and resolved["status"] == "historical"
    assert svc.scope([kb.id])["material_ids"] == [second.id]


def test_stale_duplicate_and_failed_append_do_not_advance_revision(resources):
    _,store,svc=resources
    mat,_=store.add_text("A","原材料")
    kb=svc.create("库")
    doc,ver,_=svc.attach(kb.id,mat.id)
    duplicate,same,created=svc.append_version(kb.id,doc.id,mat.id,expected_revision=1)
    assert not created and duplicate.revision == 1 and same.id == ver.id
    with pytest.raises(KnowledgeConflict):
        svc.append_version(kb.id,doc.id,mat.id,expected_revision=0)
    with pytest.raises(KnowledgeNotFound):
        svc.append_version(kb.id,doc.id,999999,expected_revision=1)
    assert svc.versions(kb.id,doc.id)[0].revision == 1
    assert len(svc.versions(kb.id,doc.id)[1]) == 1


def test_source_key_cannot_silently_replace_document(resources):
    _,store,svc=resources
    a,_=store.add_text("A","甲材料")
    b,_=store.add_text("B","乙材料")
    kb=svc.create("库")
    doc,_,_=svc.attach(kb.id,a.id,source_key="travel")
    with pytest.raises(KnowledgeConflict):
        svc.attach(kb.id,b.id,source_key="travel")
    assert len(svc.versions(kb.id,doc.id)[1]) == 1


def test_deleted_material_and_reused_id_do_not_rebind_reference(resources):
    engine,store,svc=resources
    mat,_=store.add_text("旧制度","原住宿500元")
    kb=svc.create("库")
    doc,ver,_=svc.attach(kb.id,mat.id)
    publish_fake(engine,ver)
    ref=svc.chunk(kb.id,doc.id,ver.id,1)["reference"]
    store.delete(mat.id)
    replacement,_=store.add_text("其他制度","新住宿999元")
    assert replacement.id == mat.id  # SQLite可重用末尾整数主键
    source=svc.resolve_reference(kb.id,ref)
    assert source["status"] == "source_deleted" and source["content"] is None
    assert source["source"]["title"] == "旧制度"
    assert svc.scope([kb.id])["unavailable"][0]["reason"] == "source_deleted"
    assert not foreign_key_problems(engine)


def test_soft_remove_keeps_source_and_rejects_stale_write(resources):
    _,store,svc=resources
    mat,_=store.add_text("A","材料保留")
    kb=svc.create("库")
    doc,ver,_=svc.attach(kb.id,mat.id)
    with pytest.raises(KnowledgeConflict):
        svc.remove_document(kb.id,doc.id,expected_revision=0)
    svc.remove_document(kb.id,doc.id,expected_revision=1)
    assert svc.documents(kb.id) == []
    assert svc.chunk(kb.id,doc.id,ver.id,1)["status"] == "document_removed"
    assert store.get(mat.id).content == "材料保留"
    with pytest.raises(KnowledgeNotFound):
        svc.append_version(kb.id,doc.id,mat.id,expected_revision=2)


def test_modified_chunk_or_forged_reference_is_visible(resources):
    engine,store,svc=resources
    mat,_=store.add_text("A","原文切片")
    kb=svc.create("库")
    doc,ver,_=svc.attach(kb.id,mat.id)
    ref=svc.chunk(kb.id,doc.id,ver.id,1)["reference"]
    with pytest.raises(KnowledgeNotFound):
        svc.resolve_reference(kb.id,ref[:-1]+("1" if ref[-1]!="1" else "2"))
    with Session(engine) as s,s.begin():
        s.scalar(select(MaterialChunk).where(MaterialChunk.material_id==mat.id)).content="新文本"
    assert svc.resolve_reference(kb.id,ref)["status"] == "source_changed"
    assert svc.resolve_reference(kb.id,ref)["content"] is None


def test_publication_composite_foreign_keys(resources):
    engine,store,svc=resources
    a,_=store.add_text("A","甲")
    b,_=store.add_text("B","乙")
    kb=svc.create("库")
    da,va,_=svc.attach(kb.id,a.id)
    db,vb,_=svc.attach(kb.id,b.id)
    generation=svc.plan_index(kb.id,db.id,vb.id,{"model":"fake","dimension":2})
    with pytest.raises(IntegrityError),Session(engine) as s,s.begin():
        s.add(KnowledgePublication(document_id=da.id,version_id=va.id,generation_id=generation.id))
        s.flush()
    assert not foreign_key_problems(engine)


def test_not_ready_failed_or_partial_scope(resources):
    engine,store,svc=resources
    kb=svc.create("库")
    assert svc.scope([kb.id])["status"]=="empty_library"
    a,_=store.add_text("A","甲")
    b,_=store.add_text("B","乙")
    da,va,_=svc.attach(kb.id,a.id)
    db,vb,_=svc.attach(kb.id,b.id)
    ga=publish_fake(engine,va)
    assert svc.scope([kb.id])["status"]=="partial"
    with Session(engine) as s,s.begin():
        s.get(IndexGeneration,ga).status="failed"
    assert svc.scope([kb.id])["status"]=="not_ready"
    assert svc.scope([kb.id])["material_ids"]==[]


def test_old_database_upgrade_is_additive_and_idempotent(tmp_path):
    from datetime import date
    from briefly.todos.service import TodoService
    from briefly.tracking.models import FileSnapshot, TrackedFile
    from briefly.weekly.service import WeeklyService
    from fakes import fake
    path=tmp_path/"old.db"
    url="sqlite:///"+path.as_posix()
    engine=create_db_engine(url)
    store=MaterialStore(engine)
    mat,_=store.add_text("原材料","# 标题\n"+"旧内容"*1000)
    todos=TodoService(engine,store)
    todo=todos.create("保留待办")
    todos.link(todo.id,mat.id)
    weekly=WeeklyService(store,lambda:fake())
    report=weekly.week(date(2026,10,26))
    weekly.set_materials(report.id,[mat.id])
    with Session(engine) as s,s.begin():
        tracked=TrackedFile(path="virtual://old.txt",path_key="virtual://old.txt",file_type="txt")
        s.add(tracked)
        s.flush()
        snap=FileSnapshot(file_id=tracked.id,version=1,kind="baseline",sha256="fixture",size=3000,mtime=0,path=tracked.path,material_id=mat.id)
        s.add(snap)
        s.flush()
        snapshot_id=snap.id
    with Session(engine) as s:
        before=[(c.id,c.index,c.start,c.end,c.content) for c in s.scalars(select(MaterialChunk).order_by(MaterialChunk.id))]
        sections=[(c.id,c.index,c.path,c.start,c.end) for c in s.scalars(select(MaterialSection).order_by(MaterialSection.id))]
    # 回退到不含W3表的旧schema；不触碰原材料表。
    tables=[t for t in Base.metadata.sorted_tables if t.name.startswith("knowledge_")]
    Base.metadata.drop_all(engine,tables=list(reversed(tables)))
    engine.dispose()
    for _ in range(2):
        upgraded=create_db_engine(url)
        try:
            with Session(upgraded) as s:
                assert before==[(c.id,c.index,c.start,c.end,c.content) for c in s.scalars(select(MaterialChunk).order_by(MaterialChunk.id))]
                assert sections==[(c.id,c.index,c.path,c.start,c.end) for c in s.scalars(select(MaterialSection).order_by(MaterialSection.id))]
            assert KnowledgeService(upgraded).list_bases()==[]
            assert MaterialStore(upgraded).get(mat.id).title=="原材料"
            linked=TodoService(upgraded,MaterialStore(upgraded)).direct_links([todo.id])
            assert len(linked)==1 and linked[0].material_id==mat.id
            assert WeeklyService(MaterialStore(upgraded),lambda:fake()).report(report.id).material_ids==[mat.id]
            with Session(upgraded) as s:
                assert s.get(FileSnapshot,snapshot_id).material_id==mat.id
            assert not foreign_key_problems(upgraded)
        finally:
            upgraded.dispose()


def test_concurrent_append_has_one_winner(tmp_path):
    engine=create_db_engine("sqlite:///"+(tmp_path/"parallel.db").as_posix())
    try:
        store,svc=MaterialStore(engine),KnowledgeService(engine)
        old,_=store.add_text("旧","旧版本")
        new,_=store.add_text("新","新版本")
        kb=svc.create("库")
        doc,_,_=svc.attach(kb.id,old.id)
        barrier=Barrier(2)
        def worker():
            barrier.wait(timeout=5)
            try:
                svc.append_version(kb.id,doc.id,new.id,expected_revision=1)
                return "ok"
            except KnowledgeConflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(lambda _:worker(),range(2)))==["conflict","ok"]
        assert svc.versions(kb.id,doc.id)[0].revision==2
        assert len(svc.versions(kb.id,doc.id)[1])==2
    finally:
        engine.dispose()


def test_business_label_dedup_and_invalid_generation_identity(resources):
    _,store,svc=resources
    mat,_=store.add_text("A","资料")
    kb=svc.create("库")
    doc,ver,_=svc.attach(kb.id,mat.id,business_version="v1")
    with pytest.raises(KnowledgeConflict):
        svc.attach(kb.id,mat.id,business_version="v2")
    with pytest.raises(KnowledgeConflict):
        svc.append_version(kb.id,doc.id,mat.id,expected_revision=1,business_version="v2")
    for identity in ({"model":"fake","dimension":0},{"dimension":2},{"model":"fake","dimension":True}):
        with pytest.raises(ValueError):
            svc.plan_index(kb.id,doc.id,ver.id,identity)
    planned=svc.plan_index(kb.id,doc.id,ver.id,{"model":"fake","dimension":2})
    assert svc.generations(kb.id,doc.id,ver.id)[0].id==planned.id


def test_reimport_identical_content_creates_new_version_without_resurrecting_old_ref(resources):
    _,store,svc=resources
    mat,_=store.add_text("原文","同样的制度原文")
    kb=svc.create("库")
    doc,old,_=svc.attach(kb.id,mat.id)
    ref=svc.chunk(kb.id,doc.id,old.id,1)["reference"]
    store.delete(mat.id)
    again,_=store.add_text("重新上传","同样的制度原文")
    doc,new,created=svc.append_version(kb.id,doc.id,again.id,expected_revision=1)
    assert created and new.id != old.id and new.number==2
    assert svc.chunk(kb.id,doc.id,new.id,1)["content"]=="同样的制度原文"
    assert svc.resolve_reference(kb.id,ref)["status"]=="source_deleted"
