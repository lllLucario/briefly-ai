"""文件追踪接入飞书文档：添加链接建立 Baseline、按版本号跳过、内容变化记 MODIFIED、无法访问不判删除、归属。"""
import pytest

from briefly import config
from briefly.db import create_db_engine
from briefly.materials import MaterialStore
from briefly.tracking.attribution import judge
from briefly.tracking.blobs import BlobStore
from briefly.tracking.diff import Differ
from briefly.tracking.models import BASELINE, INFERRED, MODIFIED, PRESENT, UNREACHABLE, UNTRACKED
from briefly.tracking.service import TrackingService
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


def only_file(svc):
    files = svc.files()
    assert len(files) == 1
    return files[0]


@pytest.mark.parametrize("track_first", [False, True])
def test_import_and_track_same_feishu_document_share_material(env, track_first):
    from briefly.feishu.service import import_link

    svc, _, fake = env
    if track_first:
        svc.add_paths([URL])
    with fake.client() as client:
        imported, _ = import_link(client, svc.store, URL)
    if not track_first:
        svc.add_paths([URL])
    snap = svc.snapshots(only_file(svc).id)[0]
    assert snap.material_id == imported.id
    assert len(svc.store.list_materials()) == 1
    assert svc.store.get(imported.id).parse_report["feishu"]["token"] == DOC
    with fake.client() as client:
        again, created = import_link(client, svc.store, URL)
    assert again.id == imported.id and not created
    assert not svc.add_paths([URL]).added
    assert len(svc.store.list_materials()) == 1


def test_feishu_dedup_preserves_changed_versions_and_original_import(env):
    from briefly.feishu.service import import_link

    svc, doc, fake = env
    with fake.client() as client:
        original, _ = import_link(client, svc.store, URL)
    original_text = svc.store.get(original.id).content
    svc.add_paths([URL])
    file = only_file(svc)
    first = svc.snapshots(file.id)[0]
    # 标题仍在原始快照中，正文材料不额外拼接它。
    assert svc.blobs.get(first.sha256).decode("utf-8").startswith(f"# {doc.title}\n\n")
    assert first.material_id == original.id
    doc.revision += 1
    doc.paragraphs += ["新增的原始业务事实，不能被当作重复丢掉。"]
    svc.scan()
    second = svc.snapshots(file.id)[-1]
    assert second.material_id != first.material_id
    with fake.client() as client:
        current, _ = import_link(client, svc.store, URL)
    assert current.id == second.material_id
    assert len(svc.store.list_materials()) == 2  # 原始固定副本与最新正文确实不同。
    assert svc.store.get(original.id).content == original_text
    assert Differ(svc).diff(first, second).hunks


def test_add_link_creates_baseline(env):
    svc, doc, _ = env
    result = svc.add_paths([URL])
    assert [s.kind for s in result.added] == ["feishu"] and result.added[0].path == f"feishu://docx/{DOC}"
    assert result.scan.stats["new_snapshots"] == 1
    f = only_file(svc)
    assert f.path.endswith("/周报草稿") and f.status == PRESENT and f.file_type == "feishu"
    snap = svc.snapshots(f.id)[0]
    assert snap.kind == BASELINE and snap.meta["revision_id"] == 1 and snap.meta["last_modified_by"] == "王五"
    material = svc.store.get(snap.material_id)
    assert material.source == "tracked" and "梳理支付回调链路" in material.content
    assert not svc.changes()  # Baseline 不算本周期的变化


def test_deleted_todo_stops_feishu_until_explicit_readd(env):
    from briefly.todos.service import TodoService

    svc, doc, _ = env
    svc.add_paths([URL])
    file = only_file(svc)
    todos = TodoService(svc.engine, svc.store)
    task = todos.create("飞书任务")
    todos.link(task.id, svc.snapshots(file.id)[0].material_id)
    todos.delete_permanently(task.id)
    reads = doc.block_reads
    doc.revision += 1
    doc.paragraphs = ["已停止追踪后的修改"]
    svc.scan()
    assert doc.block_reads == reads and only_file(svc).status == UNTRACKED
    assert DOC not in svc.feishu_tracking()
    svc.add_paths([URL])
    assert doc.block_reads > reads and only_file(svc).status == PRESENT


def test_unchanged_revision_is_not_read_again(env):
    svc, doc, _ = env
    svc.add_paths([URL])
    reads = doc.block_reads
    stats = svc.scan().stats
    assert stats["stat_skipped"] == 1 and doc.block_reads == reads


def test_edit_is_recorded_as_modified_with_diff(env):
    svc, doc, _ = env
    svc.add_paths([URL])
    doc.revision, doc.paragraphs = 5, doc.paragraphs + ["周三：回调耗时从 1.8 秒降到 200 毫秒"]
    stats = svc.scan().stats
    assert stats["modified"] == 1
    change = svc.changes()[0]
    assert change.change_type == MODIFIED and change.details["revision_id"] == 5
    old, new = svc.snapshots(only_file(svc).id)
    result = Differ(svc).diff(old, new)
    assert [h.op for h in result.hunks] == ["added"] and "200 毫秒" in result.hunks[0].new


def test_revision_change_without_text_change_keeps_one_version(env):
    svc, doc, _ = env
    svc.add_paths([URL])
    doc.revision = 2  # 只改了格式或评论
    stats = svc.scan().stats
    assert stats["mtime_only"] == 1 and len(svc.snapshots(only_file(svc).id)) == 1
    assert svc.scan().stats["stat_skipped"] == 1  # 新版本号已记下，下次不再读取


def test_lost_access_is_unreachable_not_deleted(env):
    svc, doc, _ = env
    svc.add_paths([URL])
    doc.error = feishu_error(1770032, "forBidden", 403)
    stats = svc.scan().stats
    f = only_file(svc)
    assert stats["unreachable"] == 1 and stats["deleted"] == 0
    assert f.status == UNREACHABLE and "添加文档应用" in f.last_error
    assert "添加文档应用" in svc.sources()[0].last_error
    doc.error = None
    doc.revision, doc.paragraphs = 3, ["改过的内容"]
    svc.scan()
    assert only_file(svc).status == PRESENT and svc.sources()[0].last_error == ""


def test_wiki_link_and_duplicates(env):
    svc, _, fake = env
    svc.add_paths([f"https://abc.feishu.cn/wiki/wikcnAbCdEfGhIjKlMn"])
    again = svc.add_paths([URL])
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
    result = svc.add_paths([url])
    assert not result.added and message in result.messages[0]


def test_inaccessible_link_is_not_added(env):
    svc, doc, _ = env
    doc.error = feishu_error(1770032, "forBidden", 403)
    result = svc.add_paths([URL])
    assert not result.added and "添加文档应用" in result.messages[0]


def test_removed_source_marks_file_untracked(env):
    svc, _, _ = env
    source = svc.add_paths([URL]).added[0]
    svc.remove_source(source.id)
    svc.scan()
    assert only_file(svc).status == UNTRACKED


def test_missing_credentials_marks_unreachable(env):
    svc, doc, _ = env
    svc.add_paths([URL])
    from briefly.feishu import FeishuNotConfigured

    def no_client():
        raise FeishuNotConfigured("未配置飞书应用")

    svc.scanner.feishu_factory = no_client
    svc.scan()
    assert only_file(svc).status == UNREACHABLE


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


def files_by_doc(svc):
    return {f.path_key.rsplit("/", 1)[-1]: f for f in svc.files()}


def test_track_whole_folder(folder_env):
    svc, drive = folder_env
    r = svc.add_paths([FOLDER_URL])
    [source] = r.added
    assert (source.kind, source.path, source.recursive) == ("feishu_folder", "feishu://folder/fldcnProject0001", True)
    files = files_by_doc(svc)
    assert set(files) == {"docA1234567890", "docB1234567890"}  # 子文件夹里的也算；多维表格跳过
    assert all(svc.snapshots(f.id)[0].kind == "baseline" for f in files.values())
    info = svc.feishu_folders()[source.id]
    assert (info.name, info.docs, info.skipped) == ("项目资料", 2, 1)
    members = svc.folder_members()
    assert members["feishu://docx/docB1234567890"] == [(source.id, "子目录/纪要")]
    assert svc.add_paths([FOLDER_URL]).messages[0].endswith("已在追踪（追踪源 1）")


def test_folder_picks_up_new_docs_and_drops_removed_ones(folder_env):
    svc, drive = folder_env
    svc.add_paths([FOLDER_URL])
    drive.folders["fldcnProject0001"].append(("docC1234567890", "docx", "新文档"))
    drive.docs["docC1234567890"], drive.revisions["docC1234567890"] = ["新写的内容"], 1
    drive.folders["fldcnProject0001"] = [x for x in drive.folders["fldcnProject0001"] if x[0] != "docA1234567890"]  # 移出文件夹
    drive.docs["docB1234567890"].append("补充结论")
    drive.revisions["docB1234567890"] = 2
    stats = svc.scan().stats
    files = files_by_doc(svc)
    assert files["docC1234567890"].status == "present" and files["docA1234567890"].status == "untracked"
    changes = {(ch.file_id, ch.change_type) for ch in svc.changes()}
    assert (files["docC1234567890"].id, "CREATED") in changes and (files["docB1234567890"].id, "MODIFIED") in changes
    assert stats["untracked"] == 1


def test_folder_listing_failure_keeps_members(folder_env):
    svc, drive = folder_env
    svc.add_paths([FOLDER_URL])
    drive.list_error = feishu_error(1061004, "forbidden", 403)
    svc.scan()
    assert all(f.status == "present" for f in svc.files())  # 没有因为列不出而误标“已不追踪”
    assert "沿用上次的文档名单" in svc.sources()[0].last_error
    drive.list_error = None
    svc.scan()
    assert svc.sources()[0].last_error == ""


def test_removing_folder_untracks_its_docs(folder_env):
    svc, drive = folder_env
    source = svc.add_paths([FOLDER_URL]).added[0]
    svc.remove_source(source.id)
    assert {f.status for f in svc.files()} == {"untracked"}  # 历史版本保留，不是删除


def test_folder_cannot_be_imported_as_material(folder_env):
    from briefly.feishu.service import import_link

    svc, drive = folder_env
    with svc.feishu_factory() as client, pytest.raises(ValueError, match="文件夹不能直接导入"):
        import_link(client, svc.store, FOLDER_URL)


def test_folder_name_from_web_is_used(folder_env):
    """网页从云空间列表里已知文件夹名，随添加请求带上；不用再调需要额外权限的文件夹元数据接口。"""
    svc, drive = folder_env
    source = svc.add_paths([FOLDER_URL], names={FOLDER_URL: "网页带来的名字"}).added[0]
    assert svc.feishu_folders()[source.id].name == "网页带来的名字"


def test_tracking_status_by_document(folder_env):
    """材料页的“追踪”一栏：单独追踪的文档可以停止；随文件夹追踪的标出文件夹名。两者都有时以单独追踪的为准。"""
    svc, drive = folder_env
    folder = svc.add_paths([FOLDER_URL]).added[0]
    single = svc.add_paths(["https://abc.feishu.cn/docx/docA1234567890"]).added[0]
    status = svc.feishu_tracking()
    assert status["docB1234567890"] == {"source_id": folder.id, "status": "active", "folder": "项目资料"}
    assert status["docA1234567890"] == {"source_id": single.id, "status": "active", "folder": None}
    assert len(svc.files()) == 2  # 同一篇文档只追踪一次
    svc.remove_source(single.id)
    assert svc.feishu_tracking()["docA1234567890"]["folder"] == "项目资料"
