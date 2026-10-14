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
        ("https://abc.feishu.cn/base/bascnAbCdEfGhIjKl", "多维表格暂不支持追踪"),
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
