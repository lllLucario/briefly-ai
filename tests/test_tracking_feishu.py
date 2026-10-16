"""文件追踪接入飞书文档：添加链接建立 Baseline、按版本号跳过、内容变化记 MODIFIED、无法访问不判删除、归属。"""
import pytest

from briefly import config
from briefly.db import create_db_engine
from briefly.materials import MaterialStore
from briefly.tracking.attribution import judge
from briefly.tracking.blobs import BlobStore
from briefly.tracking.collection import TrackingService
from briefly.tracking.diff import Differ
from briefly.tracking.models import BASELINE, INFERRED, MODIFIED, PRESENT, UNREACHABLE, UNTRACKED
from fakes import FakeFeishu, feishu_error

DOC = "doxcnAbCdEfGhIjKlMnOpQrStUv"
URL = f"https://abc.feishu.cn/docx/{DOC}"
API = "/open-apis"


class Doc:
    """可变的飞书文档：改 paragraphs / revision 模拟用户在飞书里编辑。"""

    def __init__(self) -> None:
        self.title = "周报草稿"
        self.revision = 1
        self.paragraphs = ["周一：梳理支付回调链路"]
        self.error = None  # 设为 httpx.Response 模拟无权限等错误
        self.block_reads = 0

    def info(self, request):
        return self.error or {"document": {"document_id": DOC, "revision_id": self.revision, "title": self.title}}

    def blocks(self, request):
        self.block_reads += 1
        ids = [f"p{i}" for i in range(len(self.paragraphs))]
        items = [{"block_id": DOC, "block_type": 1, "page": {"elements": [{"text_run": {"content": self.title}}]}, "children": ids}]
        items += [{"block_id": i, "block_type": 2, "text": {"elements": [{"text_run": {"content": p}}]}} for i, p in zip(ids, self.paragraphs)]
        return {"items": items, "has_more": False}


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TRACKING_DIR", str(tmp_path / "store"))
    doc = Doc()
    fake = FakeFeishu({
        ("GET", f"{API}/docx/v1/documents/{DOC}"): doc.info,
        ("GET", f"{API}/docx/v1/documents/{DOC}/blocks"): doc.blocks,
        ("POST", f"{API}/drive/v1/metas/batch_query"): {"metas": [{"doc_token": DOC, "latest_modify_user": "ou_me", "owner_id": "ou_me"}]},
        ("GET", f"{API}/contact/v3/users/ou_me"): {"user": {"name": "王五"}},
        ("GET", f"{API}/wiki/v2/spaces/get_node"): {"node": {"obj_type": "docx", "obj_token": DOC}},
    })  # fmt: skip
    engine = create_db_engine("sqlite://")
    store = MaterialStore(engine)
    svc = TrackingService(engine, store, BlobStore(tmp_path / "store"), feishu_factory=fake.client)
    yield svc, doc, fake
    engine.dispose()


def only_file(svc, c):
    files = svc.files(c.id)
    assert len(files) == 1
    return files[0]


def test_add_link_creates_baseline(env):
    svc, doc, _ = env
    c = svc.create_collection("项目A", ["王五"])
    result = svc.add_paths(c.id, [URL])
    assert [s.kind for s in result.added] == ["feishu"] and result.added[0].path == f"feishu://docx/{DOC}"
    assert result.scan.stats["new_snapshots"] == 1
    f = only_file(svc, c)
    assert f.path.endswith("/周报草稿") and f.status == PRESENT and f.file_type == "feishu"
    snap = svc.snapshots(f.id)[0]
    assert snap.kind == BASELINE and snap.meta["revision_id"] == 1 and snap.meta["last_modified_by"] == "王五"
    material = svc.store.get(snap.material_id)
    assert material.source == "tracked" and "梳理支付回调链路" in material.content
    assert not svc.changes(c.id)  # Baseline 不算本周期的变化


def test_unchanged_revision_is_not_read_again(env):
    svc, doc, _ = env
    c = svc.create_collection("项目A")
    svc.add_paths(c.id, [URL])
    reads = doc.block_reads
    stats = svc.scan(c.id).stats
    assert stats["stat_skipped"] == 1 and doc.block_reads == reads


def test_edit_is_recorded_as_modified_with_diff(env):
    svc, doc, _ = env
    c = svc.create_collection("项目A")
    svc.add_paths(c.id, [URL])
    doc.revision, doc.paragraphs = 5, doc.paragraphs + ["周三：回调耗时从 1.8 秒降到 200 毫秒"]
    stats = svc.scan(c.id).stats
    assert stats["modified"] == 1
    change = svc.changes(c.id)[0]
    assert change.change_type == MODIFIED and change.details["revision_id"] == 5
    old, new = svc.snapshots(only_file(svc, c).id)
    result = Differ(svc).diff(old, new)
    assert [h.op for h in result.hunks] == ["added"] and "200 毫秒" in result.hunks[0].new


def test_revision_change_without_text_change_keeps_one_version(env):
    svc, doc, _ = env
    c = svc.create_collection("项目A")
    svc.add_paths(c.id, [URL])
    doc.revision = 2  # 只改了格式或评论
    stats = svc.scan(c.id).stats
    assert stats["mtime_only"] == 1 and len(svc.snapshots(only_file(svc, c).id)) == 1
    assert svc.scan(c.id).stats["stat_skipped"] == 1  # 新版本号已记下，下次不再读取


def test_lost_access_is_unreachable_not_deleted(env):
    svc, doc, _ = env
    c = svc.create_collection("项目A")
    svc.add_paths(c.id, [URL])
    doc.error = feishu_error(1770032, "forBidden", 403)
    stats = svc.scan(c.id).stats
    f = only_file(svc, c)
    assert stats["unreachable"] == 1 and stats["deleted"] == 0
    assert f.status == UNREACHABLE and "添加文档应用" in f.last_error
    assert "添加文档应用" in svc.sources(c.id)[0].last_error
    doc.error = None
    doc.revision, doc.paragraphs = 3, ["改过的内容"]
    svc.scan(c.id)
    assert only_file(svc, c).status == PRESENT and svc.sources(c.id)[0].last_error == ""


def test_wiki_link_and_duplicates(env):
    svc, _, fake = env
    c = svc.create_collection("项目A")
    svc.add_paths(c.id, [f"https://abc.feishu.cn/wiki/wikcnAbCdEfGhIjKlMn"])
    again = svc.add_paths(c.id, [URL])
    assert not again.added and "已在追踪" in again.messages[0]


@pytest.mark.parametrize(
    "url, message",
    [
        ("https://abc.feishu.cn/base/bascnAbCdEfGhIjKl", "暂不支持追踪"),
        ("https://abc.feishu.cn/sheets/shtcnAbCdEfGhIjKl", "电子表格"),
    ],
)
def test_unsupported_links_are_not_added(env, url, message):
    svc, _, fake = env
    fake.routes[("GET", f"{API}/bitable/v1/apps/bascnAbCdEfGhIjKl")] = {"app": {"name": "x"}}
    c = svc.create_collection("项目A")
    result = svc.add_paths(c.id, [url])
    assert not result.added and message in result.messages[0]


def test_inaccessible_link_is_not_added(env):
    svc, doc, _ = env
    doc.error = feishu_error(1770032, "forBidden", 403)
    c = svc.create_collection("项目A")
    result = svc.add_paths(c.id, [URL])
    assert not result.added and "添加文档应用" in result.messages[0]


def test_removed_source_marks_file_untracked(env):
    svc, _, _ = env
    c = svc.create_collection("项目A")
    source = svc.add_paths(c.id, [URL]).added[0]
    svc.remove_source(source.id)
    svc.scan(c.id)
    assert only_file(svc, c).status == UNTRACKED


def test_missing_credentials_marks_unreachable(env):
    svc, doc, _ = env
    c = svc.create_collection("项目A")
    svc.add_paths(c.id, [URL])
    from briefly.feishu import FeishuNotConfigured

    def no_client():
        raise FeishuNotConfigured("未配置飞书应用")

    svc.scanner.feishu_factory = no_client
    svc.scan(c.id)
    assert only_file(svc, c).status == UNREACHABLE


def test_attribution_uses_last_editor():
    meta = {"revision_id": 2, "last_modified_by": "王五", "last_modified_by_id": "ou_me"}
    status, basis, _ = judge(["王五"], None, meta)
    assert status == INFERRED and "飞书文档的“最后编辑人”" in basis[0]
    status, _, _ = judge(["ou_me"], None, {**meta, "last_modified_by": "ou_me"})
    assert status == INFERRED
    status, _, flags = judge(["张三"], None, meta)
    assert status != INFERRED and "others" in flags


# ---- 飞书文件夹：整个文件夹一起追踪
class Drive:
    """可变的飞书云空间：文件夹 → 条目；文档 → 段落。"""

    def __init__(self) -> None:
        self.folders = {
            "fldcnProject0001": [("docA1234567890", "docx", "方案"), ("fldcnSubdir00002", "folder", "子目录"), ("bascnX1234567", "bitable", "任务表")],
            "fldcnSubdir00002": [("docB1234567890", "docx", "纪要")],
        }
        self.docs = {"docA1234567890": ["方案初稿"], "docB1234567890": ["周会纪要"]}
        self.revisions = {k: 1 for k in self.docs}
        self.list_error = None

    def routes(self) -> dict:
        def files(request):
            if self.list_error:
                return self.list_error
            token = request.url.params.get("folder_token", "")
            return {"files": [{"token": t, "type": k, "name": n} for t, k, n in self.folders.get(token, [])], "has_more": False}

        def doc(request):
            token = request.url.path.split("/")[5]
            return {"document": {"document_id": token, "revision_id": self.revisions[token], "title": token}}

        def blocks(request):
            token = request.url.path.split("/")[5]
            ids = [f"p{i}" for i in range(len(self.docs[token]))]
            items = [{"block_id": token, "block_type": 1, "page": {"elements": [{"text_run": {"content": token}}]}, "children": ids}]
            items += [{"block_id": i, "block_type": 2, "text": {"elements": [{"text_run": {"content": p}}]}} for i, p in zip(ids, self.docs[token])]
            return {"items": items, "has_more": False}

        routes = {
            ("GET", f"{API}/drive/v1/files"): files,
            ("GET", f"{API}/drive/explorer/v2/folder/fldcnProject0001/meta"): {"name": "项目资料"},
            ("POST", f"{API}/drive/v1/metas/batch_query"): {"metas": []},
        }
        for token in ["docA1234567890", "docB1234567890", "docC1234567890"]:
            routes[("GET", f"{API}/docx/v1/documents/{token}")] = doc
            routes[("GET", f"{API}/docx/v1/documents/{token}/blocks")] = blocks
        return routes


@pytest.fixture
def folder_env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TRACKING_DIR", str(tmp_path / "store"))
    drive = Drive()
    fake = FakeFeishu(drive.routes())
    engine = create_db_engine("sqlite://")
    svc = TrackingService(engine, MaterialStore(engine), BlobStore(tmp_path / "store"), feishu_factory=fake.client)
    yield svc, drive
    engine.dispose()


FOLDER_URL = "https://abc.feishu.cn/drive/folder/fldcnProject0001"


def files_by_doc(svc, c):
    return {f.path_key.rsplit("/", 1)[-1]: f for f in svc.files(c.id)}


def test_track_whole_folder(folder_env):
    svc, drive = folder_env
    c = svc.create_collection("项目A")
    r = svc.add_paths(c.id, [FOLDER_URL])
    [source] = r.added
    assert (source.kind, source.path, source.recursive) == ("feishu_folder", "feishu://folder/fldcnProject0001", True)
    files = files_by_doc(svc, c)
    assert set(files) == {"docA1234567890", "docB1234567890"}  # 子文件夹里的也算；多维表格跳过
    assert all(svc.snapshots(f.id)[0].kind == "baseline" for f in files.values())
    info = svc.feishu_folders(c.id)[source.id]
    assert (info.name, info.docs, info.skipped) == ("项目资料", 2, 1)
    members = svc.folder_members(c.id)
    assert members["feishu://docx/docB1234567890"] == [(source.id, "子目录/纪要")]
    assert svc.add_paths(c.id, [FOLDER_URL]).messages[0].endswith("已在追踪（追踪源 1）")


def test_folder_picks_up_new_docs_and_drops_removed_ones(folder_env):
    svc, drive = folder_env
    c = svc.create_collection("项目A")
    svc.add_paths(c.id, [FOLDER_URL])
    drive.folders["fldcnProject0001"].append(("docC1234567890", "docx", "新文档"))
    drive.docs["docC1234567890"], drive.revisions["docC1234567890"] = ["新写的内容"], 1
    drive.folders["fldcnProject0001"] = [x for x in drive.folders["fldcnProject0001"] if x[0] != "docA1234567890"]  # 移出文件夹
    drive.docs["docB1234567890"].append("补充结论")
    drive.revisions["docB1234567890"] = 2
    stats = svc.scan(c.id).stats
    files = files_by_doc(svc, c)
    assert files["docC1234567890"].status == "present" and files["docA1234567890"].status == "untracked"
    changes = {(ch.file_id, ch.change_type) for ch in svc.changes(c.id)}
    assert (files["docC1234567890"].id, "CREATED") in changes and (files["docB1234567890"].id, "MODIFIED") in changes
    assert stats["untracked"] == 1


def test_folder_listing_failure_keeps_members(folder_env):
    svc, drive = folder_env
    c = svc.create_collection("项目A")
    svc.add_paths(c.id, [FOLDER_URL])
    drive.list_error = feishu_error(1061004, "forbidden", 403)
    svc.scan(c.id)
    assert all(f.status == "present" for f in svc.files(c.id))  # 没有因为列不出而误标“已不追踪”
    assert "沿用上次的文档名单" in svc.sources(c.id)[0].last_error
    drive.list_error = None
    svc.scan(c.id)
    assert svc.sources(c.id)[0].last_error == ""


def test_removing_folder_untracks_its_docs_and_delete_cleans_up(folder_env):
    svc, drive = folder_env
    c = svc.create_collection("项目A")
    source = svc.add_paths(c.id, [FOLDER_URL]).added[0]
    svc.remove_source(source.id)
    assert {f.status for f in svc.files(c.id)} == {"untracked"}
    svc.delete_collection(c.id)  # 两张文件夹表一起清理，不留孤儿记录
    assert svc.list_collections() == []


def test_folder_cannot_be_imported_as_material(folder_env):
    from briefly.feishu.service import import_link

    svc, drive = folder_env
    with svc.feishu_factory() as client, pytest.raises(ValueError, match="文件夹不能直接导入"):
        import_link(client, svc.store, FOLDER_URL)


def test_folder_name_from_web_is_used(folder_env):
    """网页从云空间列表里已知文件夹名，随添加请求带上；不用再调需要额外权限的文件夹元数据接口。"""
    svc, drive = folder_env
    c = svc.create_collection("项目A")
    source = svc.add_paths(c.id, [FOLDER_URL], names={FOLDER_URL: "网页带来的名字"}).added[0]
    assert svc.feishu_folders(c.id)[source.id].name == "网页带来的名字"


def test_tracking_status_by_document(folder_env):
    """材料页的“追踪”一栏：每篇飞书文档被哪些周报计划追踪，单独添加的可以停止，随文件夹的标出文件夹名。"""
    svc, drive = folder_env
    a, b = svc.create_collection("项目A"), svc.create_collection("项目B")
    folder = svc.add_paths(a.id, [FOLDER_URL]).added[0]
    single = svc.add_paths(b.id, ["https://abc.feishu.cn/docx/docA1234567890"]).added[0]
    status = svc.feishu_tracking()
    assert status["docB1234567890"] == [
        {"plan_id": a.id, "plan": "项目A", "source_id": folder.id, "status": "active", "folder": "项目资料"}
    ]
    assert {(x["plan"], x["folder"]) for x in status["docA1234567890"]} == {("项目A", "项目资料"), ("项目B", None)}
    svc.remove_source(single.id)
    assert [x["plan"] for x in svc.feishu_tracking()["docA1234567890"]] == ["项目A"]
