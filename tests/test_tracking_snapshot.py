"""文件追踪 Step 1：集合、追踪源、Baseline、增量快照、文件级变化、扫描可靠性。不调用模型。"""
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import update
from sqlalchemy.orm import sessionmaker

from briefly import config
from briefly.tracking import cli as track_cli
from briefly.tracking.models import (
    BASELINE,
    CREATED,
    DELETED,
    DONE,
    FIRST_SEEN,
    INCREMENTAL,
    INTERRUPTED,
    MISSING,
    MODIFIED,
    POSSIBLY_MOVED,
    PRESENT,
    RUNNING,
    UNREACHABLE,
    UNTRACKED,
    ScanLock,
    ScanRun,
)
from briefly.tracking.scanner import ScanBusy, Scanner, Unstable, read_stable
from tracking_util import docx_bytes, env, xlsx_bytes  # noqa: F401


def change_types(env):
    return sorted(ch.change_type for ch in env.svc.changes())


def test_mixed_files_and_folders_get_immediate_baseline(env):
    env.write("proj/方案.md", "# 方案\n第一版")
    env.write("proj/sub/进度.csv", "任务,负责人\n压测,小王\n")
    env.write("proj/node_modules/x.md", "依赖里的文件")
    env.write("proj/~$方案.docx", b"owner file")
    env.write("proj/notes.txt.tmp", "临时文件")
    word = env.write("周报.docx", docx_bytes(["本周完成登录联调"]))
    excel = env.write("数据.xlsx", xlsx_bytes({"Sheet1": [["指标", "值"], ["QPS", 100]]}))
    env.set_owners(["张三"])

    result = env.svc.add_paths([str(env.work / "proj"), str(word), str(excel)])

    assert [s.kind for s in result.added] == ["folder", "file", "file"]
    assert result.scan.status == DONE
    names = sorted(Path(f.path).name for f in env.svc.files())
    assert names == ["周报.docx", "数据.xlsx", "方案.md", "进度.csv"]  # 依赖目录、Office 临时文件、.tmp 被排除
    for f in env.svc.files():
        snaps = env.svc.snapshots(f.id)
        assert [(s.version, s.kind) for s in snaps] == [(1, BASELINE)]
        assert snaps[0].material_id and env.svc.blobs.has(snaps[0].sha256)
    assert env.svc.changes() == []  # 基线不是变化


def test_overlapping_sources_process_each_file_once(env):
    env.write("proj/a.md", "# A")
    env.write("proj/sub/b.md", "# B")
    env.svc.add_paths([str(env.work / "proj")])
    result = env.svc.add_paths([str(env.work / "proj" / "a.md"), str(env.work / "proj" / "sub"), str(env.work / "proj")])

    assert len(result.added) == 2  # 第三个是完全相同的追踪源
    text = "\n".join(result.messages)
    assert "已被文件夹追踪源" in text and "已在追踪" in text
    assert len(env.svc.files()) == 2
    assert all(len(env.svc.snapshots(f.id)) == 1 for f in env.svc.files())
    # 外层文件夹暂停后，文件仍被内层追踪源覆盖
    outer = env.svc.sources()[0]
    env.svc.pause_source(outer.id)
    env.write("proj/sub/b.md", "# B\n更新")
    stats = env.svc.scan().stats
    assert stats["files"] == 2 and stats["modified"] == 1


def test_folder_scan_records_new_file_as_first_seen(env):
    env.write("d/a.md", "# A")
    env.svc.add_paths([str(env.work / "d")])
    env.write("d/new.txt", "后来新建的文件")

    stats = env.svc.scan().stats

    assert stats["created"] == 1
    f = env.file("new.txt")
    assert env.svc.snapshots(f.id)[0].kind == FIRST_SEEN
    change = env.svc.changes()[0]
    assert change.change_type == CREATED and "不代表创建时间" in change.details["basis"]


def test_modified_content_saves_new_version_and_repeat_scans_are_idempotent(env):
    env.write("a.md", "# 标题\n第一版")
    env.svc.add_paths([str(env.work / "a.md")])
    env.write("a.md", "# 标题\n第二版，补充内容")

    first = env.svc.scan().stats
    second = env.svc.scan().stats
    third = env.svc.scan().stats

    assert first["new_snapshots"] == 1 and first["modified"] == 1
    assert second["new_snapshots"] == third["new_snapshots"] == 0
    assert second["stat_skipped"] == 1  # 大小和 mtime 没变，不读文件
    f = env.file("a.md")
    assert [(s.version, s.kind) for s in env.svc.snapshots(f.id)] == [(1, BASELINE), (2, INCREMENTAL)]
    assert change_types(env) == [MODIFIED]


def test_mtime_only_change_does_not_create_version(env):
    env.write("a.md", "# 内容不变")
    env.svc.add_paths([str(env.work / "a.md")])
    env.touch("a.md")

    stats = env.svc.scan().stats

    assert stats["mtime_only"] == 1 and stats["new_snapshots"] == 0
    assert len(env.svc.snapshots(env.file("a.md").id)) == 1
    assert env.svc.scan().stats["stat_skipped"] == 1  # 记下新的 mtime 后，下次直接跳过


def test_file_being_written_is_skipped_until_stable(env, monkeypatch):
    env.write("a.md", "# v1")
    env.svc.add_paths([str(env.work / "a.md")])
    env.write("a.md", "# v2 写到一半")

    def busy(path):
        raise Unstable("读取过程中文件发生变化，可能正在写入")

    env.svc.scanner.reader = busy
    stats = env.svc.scan().stats
    assert stats["new_snapshots"] == 0 and "正在写入" in stats["unstable"][0]

    env.svc.scanner.reader = read_stable
    assert env.svc.scan().stats["new_snapshots"] == 1


def test_read_stable_detects_fresh_and_changing_files(tmp_path, monkeypatch):
    path = tmp_path / "a.txt"
    path.write_text("abc", encoding="utf-8")
    monkeypatch.setattr(config, "TRACKING_SETTLE_SECONDS", 3600)
    with pytest.raises(Unstable, match="刚被修改"):
        read_stable(path)
    monkeypatch.setattr(config, "TRACKING_SETTLE_SECONDS", 0)
    real_read = Path.read_bytes

    def growing(self):
        data = real_read(self)
        self.write_text("abcdef", encoding="utf-8")  # 读完之后文件又被写入
        return data

    monkeypatch.setattr(Path, "read_bytes", growing)
    with pytest.raises(Unstable, match="读取过程中"):
        read_stable(path)


def test_rename_is_possibly_moved_and_delete_is_recorded(env):
    env.write("d/报告.md", "# 报告\n正文")
    env.write("d/旧.md", "# 旧文件")
    env.svc.add_paths([str(env.work / "d")])
    old = env.file("报告.md")
    os.rename(env.work / "d" / "报告.md", env.work / "d" / "报告-终版.md")
    (env.work / "d" / "旧.md").unlink()

    stats = env.svc.scan().stats

    assert stats["possibly_moved"] == 1 and stats["deleted"] == 1
    moved = env.file("报告-终版.md")
    assert moved.id != old.id and moved.moved_from_id == old.id  # 不合并身份，只标注
    change = next(ch for ch in env.svc.changes() if ch.change_type == POSSIBLY_MOVED)
    assert "内容哈希与消失的文件相同" in change.details["basis"]
    assert env.svc.file(old.id).status == MISSING
    assert env.svc.file(env.file("旧.md").id).status == MISSING
    assert sorted(change_types(env)) == [DELETED, POSSIBLY_MOVED]


def test_same_hash_copy_is_not_treated_as_move(env):
    env.write("d/a.md", "# 相同内容")
    env.svc.add_paths([str(env.work / "d")])
    env.write("d/a-副本.md", "# 相同内容")

    stats = env.svc.scan().stats

    assert stats["created"] == 1 and stats["possibly_moved"] == 0  # 原文件还在，不是移动
    assert env.file("a-副本.md").moved_from_id is None


def test_unreachable_folder_is_not_deleted(env):
    env.write("share/a.md", "# 共享盘文件")
    env.svc.add_paths([str(env.work / "share")])
    os.rename(env.work / "share", env.work / "share-offline")  # 模拟网络盘断开

    stats = env.svc.scan().stats

    assert stats["deleted"] == 0 and stats["unreachable"] == 1
    assert env.file("a.md").status == UNREACHABLE
    assert "无法访问" in env.svc.sources()[0].last_error
    os.rename(env.work / "share-offline", env.work / "share")
    stats = env.svc.scan().stats
    assert env.file("a.md").status == PRESENT and stats["new_snapshots"] == 0  # 恢复后内容没变，不存新版本


def test_missing_single_file_with_accessible_folder_is_deleted(env):
    path = env.write("a.md", "# A")
    env.svc.add_paths([str(path)])
    path.unlink()

    assert env.svc.scan().stats["deleted"] == 1
    env.write("a.md", "# A 恢复了，内容改过")
    stats = env.svc.scan().stats
    assert stats["modified"] == 1 and env.file("a.md").status == PRESENT  # 原路径重新出现：同一文件的新版本


def test_pause_resume_and_remove_source(env):
    env.write("d/a.md", "# A")
    source = env.svc.add_paths([str(env.work / "d")]).added[0]
    env.svc.pause_source(source.id)
    env.write("d/a.md", "# A 暂停期间修改")
    assert env.svc.scan().stats["files"] == 0

    _, scan = env.svc.resume_source(source.id)
    assert scan.stats["modified"] == 1

    env.svc.remove_source(source.id)
    assert env.file("a.md").status == UNTRACKED
    assert env.svc.scan().stats["deleted"] == 0  # 移除追踪源不等于删除文件
    assert len(env.svc.snapshots(env.file("a.md").id)) == 2  # 历史版本保留
    with pytest.raises(ValueError, match="已移除"):
        env.svc.resume_source(source.id)


def test_symlinks_are_not_followed(env, tmp_path):
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "密码.txt").write_text("不应被读取", encoding="utf-8")
    env.write("d/a.md", "# A")
    try:
        os.symlink(outside, env.work / "d" / "link", target_is_directory=True)
        os.symlink(outside / "密码.txt", env.work / "d" / "file-link.txt")
    except (OSError, NotImplementedError):
        pytest.skip("当前系统不允许创建符号链接")
    env.svc.add_paths([str(env.work / "d")])
    assert [Path(f.path).name for f in env.svc.files()] == ["a.md"]


def test_windows_junctions_are_not_followed(env, tmp_path):
    winapi = pytest.importorskip("_winapi")
    if not hasattr(winapi, "CreateJunction"):
        pytest.skip("不是 Windows")
    outside = tmp_path / "secret"
    outside.mkdir()
    (outside / "机密.md").write_text("# 不应被读取", encoding="utf-8")
    env.write("d/a.md", "# A")
    winapi.CreateJunction(str(outside), str(env.work / "d" / "junction"))
    env.svc.add_paths([str(env.work / "d")])
    assert [Path(f.path).name for f in env.svc.files()] == ["a.md"]


def test_large_files_and_full_store_are_skipped_with_reason(env, monkeypatch):
    env.write("big.txt", "x" * 3000)
    env.write("small.txt", "y" * 100)
    monkeypatch.setattr(config, "TRACKING_MAX_FILE_MB", 2000 / 1024 / 1024)
    result = env.svc.add_paths([str(env.work / "big.txt"), str(env.work / "small.txt")])
    assert "超过" in result.scan.stats["errors"][0]
    assert [Path(f.path).name for f in env.svc.files()] == ["small.txt"]

    env.svc.scanner.blobs.max_bytes = 150
    env.write("small.txt", "z" * 120)
    stats = env.svc.scan().stats
    assert stats["new_snapshots"] == 0 and "版本库已达上限" in stats["errors"][0]


def test_parse_failure_keeps_raw_version(env):
    env.write("坏.docx", b"not a zip file")
    env.svc.add_paths([str(env.work / "坏.docx")])
    snap = env.svc.snapshots(env.file("坏.docx").id)[0]
    assert snap.material_id is None and "无法解析" in snap.parse_error
    assert env.svc.blobs.get(snap.sha256) == b"not a zip file"  # 原始字节保留，可重新解析


def test_material_list_shows_only_latest_tracked_version(env):
    """材料库显示追踪中文件的最新版本（随追踪更新），旧版本和已不追踪的文件不显示。"""
    env.write("a/同.md", "# 同样的内容")
    env.write("b/同.md", "# 同样的内容")
    user_material, _ = env.store.add_text("用户材料", "用户自己粘贴的")
    sources = env.svc.add_paths([str(env.work / "a"), str(env.work / "b")]).added

    ids = {env.svc.snapshots(f.id)[0].material_id for f in env.svc.files()}
    assert len(ids) == 1  # 相同字节只解析一次
    [first] = ids
    assert [m.id for m in env.store.list_materials()] == [user_material.id, first]
    assert [h.material_id for h in env.store.search("同样")] == [first]

    env.write("a/同.md", "# 改过的内容")
    env.write("b/同.md", "# 改过的内容")
    env.svc.scan()
    [latest] = {env.svc.snapshots(f.id)[-1].material_id for f in env.svc.files()}
    assert [m.id for m in env.store.list_materials()] == [user_material.id, latest]  # 旧版本不再显示
    assert env.store.search("同样") == []

    for source in sources:
        env.svc.remove_source(source.id)
    assert [m.id for m in env.store.list_materials()] == [user_material.id]  # 不再追踪：最新版本也不显示
    # 用户上传与某个追踪版本相同的内容：转为用户材料
    m, created = env.store.add_text("我上传的", "# 同样的内容")
    assert created and m.id == first and [x.id for x in env.store.list_materials()] == [user_material.id, m.id]


@pytest.mark.parametrize("track_first", [False, True])
def test_upload_and_track_local_file_reuse_material(env, track_first):
    content = "原始说明\n保留完整内容。"
    path = env.write("重复上传.txt", content)
    if track_first:
        env.svc.add_paths([str(path)])
    uploaded, _ = env.store.add_file(path.name, content.encode("utf-8"))
    again, created = env.store.add_file("改名后.txt", content.replace("\n", "\r\n").encode("utf-8"))
    assert not created and again.id == uploaded.id
    if not track_first:
        env.svc.add_paths([str(path)])
    assert env.svc.snapshots(env.file(path.name).id)[0].material_id == uploaded.id
    assert len(env.store.list_materials()) == 1
    assert not env.svc.add_paths([str(path)]).added


def test_docx_metadata_is_captured(env):
    env.write("a.docx", docx_bytes(["正文"]))
    env.svc.add_paths([str(env.work / "a.docx")])
    meta = env.svc.snapshots(env.file("a.docx").id)[0].meta
    assert "revisions" in meta and meta["revisions"] == []


def test_interrupted_scan_is_marked_and_resumed_without_duplicates(env, monkeypatch):
    for i in range(4):
        env.write(f"d/{i}.md", f"# 文件 {i}")
    env.svc.add_paths([str(env.work / "d")])
    for i in range(4):
        env.write(f"d/{i}.md", f"# 文件 {i}\n第二版")

    with pytest.raises(KeyboardInterrupt):
        env.svc.scanner.scan("manual", interrupt_after=2)  # 处理两个文件后进程被杀
    runs = env.svc.scans()
    assert runs[0].status == RUNNING  # 没有被错误地标成完成
    with pytest.raises(ScanBusy):
        env.svc.scan()  # 锁还在租约期内

    with sessionmaker(env.svc.engine)() as s:  # 租约到期
        s.execute(update(ScanLock).values(expires_at=datetime.now() - timedelta(seconds=1)))
        s.commit()
    other = Scanner(env.svc.engine, env.store, env.svc.blobs)  # 另一个进程接手
    result = other.scan("manual")
    assert result.status == DONE and result.stats["new_snapshots"] == 2  # 只补上没处理的两个
    assert env.svc.scans()[1].status == INTERRUPTED
    assert all(len(env.svc.snapshots(f.id)) == 2 for f in env.svc.files())
    assert len([ch for ch in env.svc.changes() if ch.change_type == MODIFIED]) == 4


def test_failed_scan_is_recorded(env, monkeypatch):
    env.write("a.md", "# A")
    env.svc.add_paths([str(env.work / "a.md")])
    env.write("a.md", "# A2")
    monkeypatch.setattr(env.svc.scanner, "_scan_existing", lambda *a, **k: 1 / 0)
    result = env.svc.scan()
    assert result.status == "failed" and "ZeroDivisionError" in env.svc.scans()[0].error
    monkeypatch.undo()


def test_offline_gap_is_recorded_not_hidden(env):
    env.write("a.md", "# A")
    env.svc.add_paths([str(env.work / "a.md")])
    with sessionmaker(env.svc.engine)() as s:  # 假装上次成功扫描在 10 天前
        s.execute(update(ScanRun).values(finished_at=datetime.now() - timedelta(days=10)))
        s.commit()
    env.write("a.md", "# A 离线期间改过")

    stats = env.svc.scan().stats

    assert stats["gap"]["missed_days"]
    assert env.svc.snapshots(env.file("a.md").id)[-1].gap_before is True


def test_bad_paths_are_reported(env):
    result = env.svc.add_paths([str(env.work / "不存在.md"), str(env.svc.blobs.root.parent)])
    assert result.added == [] and len(result.messages) == 2


def test_cli_basic_flow(env, capsys):
    env.write("d/a.md", "# A")
    run = lambda *argv: track_cli.main(list(argv), service=env.svc)  # noqa: E731
    assert run("add", str(env.work / "d")) == 0
    env.write("d/a.md", "# A 改过")
    assert run("scan") == 0
    assert run("files") == 0
    assert run("history", "1") == 0
    assert run("changes") == 0
    assert run("sources") == 0
    assert run("scans") == 0
    out = capsys.readouterr().out
    assert "已添加追踪源 1" in out and "新版本 1" in out
    assert "v2 [增量]" in out and "修改" in out
    assert run("history", "99") == 1
    assert "文件 99 不存在" in capsys.readouterr().err
