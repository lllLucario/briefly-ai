"""扫描：枚举追踪范围内的文件 → 大小与 mtime 快速筛选 → SHA-256 确认 → 保存新版本、记录文件级变化。

可靠性约定：
- 幂等：只有字节内容与该文件最新版本不同才保存新版本，失败重试、重复扫描不会产生重复版本。
- 每个文件一个事务：中途中断时已处理的文件版本有效，扫描记录不会被标成完成（下次扫描接手并标为中断）。
- 正在写入（读前读后大小或 mtime 不一致、mtime 太新）的文件本次跳过，不存半截内容。
- 无法访问 ≠ 删除：路径或权限问题只标记 unreachable；所在文件夹能正常列出而文件不在，才记 DELETED。
- 移除追踪源 ≠ 删除：不再被任何启用的追踪源覆盖的文件标记 untracked。
- 不跟随符号链接（包括 Windows 目录联接），不读追踪范围以外的文件。
"""
import logging
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .. import config
from ..loaders import SUPPORTED_TYPES, load_document
from ..materials import MaterialStore
from .attribution import extract_meta
from .blobs import BlobStore, StoreFull, sha256_bytes
from .models import (
    ACTIVE,
    BASELINE,
    CREATED,
    DAILY,
    DELETED,
    DONE,
    FAILED,
    FINAL,
    FIRST_SEEN,
    INCREMENTAL,
    INTERRUPTED,
    MISSING,
    MODIFIED,
    PAUSED,
    POSSIBLY_MOVED,
    PRE_REPORT,
    PRESENT,
    RUNNING,
    UNREACHABLE,
    UNTRACKED,
    FileChange,
    FileSnapshot,
    ScanLock,
    ScanRun,
    TrackedFile,
    TrackingSource,
)

log = logging.getLogger(__name__)

# 开发依赖、版本库、缓存、回收站等目录，不进入追踪
EXCLUDED_DIRS = {
    ".git", ".svn", ".hg", "node_modules", "__pycache__", ".venv", "venv", "env", ".tox", ".idea", ".vscode",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache", "dist", "build", "site-packages",
    "$recycle.bin", "system volume information", ".trash", ".trashes", ".ds_store",
}  # fmt: skip
# 临时文件：Office 的所有者文件 ~$xxx、LibreOffice 锁文件、编辑器交换文件、下载中的文件等
TEMP_PREFIXES = ("~$", ".~lock.", "~")
TEMP_SUFFIXES = (".tmp", ".temp", ".swp", ".swx", ".bak", ".crdownload", ".part", ".partial", ".download", "~")
SYSTEM_FILES = {"thumbs.db", "desktop.ini", ".ds_store"}


class ScanBusy(Exception):
    """同一集合正在扫描。"""


def path_key(path: str | Path) -> str:
    """去重用的规范化路径：绝对路径，Windows 下不区分大小写。"""
    return os.path.normcase(os.path.abspath(str(path)))


def is_supported(name: str) -> bool:
    lower = name.lower()
    if lower in SYSTEM_FILES or lower.startswith(".") or lower.startswith(TEMP_PREFIXES):
        return False
    if lower.endswith(TEMP_SUFFIXES):
        return False
    return Path(lower).suffix.lstrip(".") in SUPPORTED_TYPES


def _is_link(entry: os.DirEntry) -> bool:
    if entry.is_symlink():
        return True
    is_junction = getattr(entry, "is_junction", None)  # Python 3.12+，Windows 目录联接
    return bool(is_junction and is_junction())


def is_within(child_key: str, parent_key: str) -> bool:
    return child_key == parent_key or child_key.startswith(parent_key.rstrip(os.sep) + os.sep)


@dataclass
class Listing:
    """一次枚举的结果。"""

    files: dict[str, Path] = field(default_factory=dict)  # path_key → 路径（多个追踪源覆盖时只出现一次）
    covered_by: dict[str, list[int]] = field(default_factory=dict)  # path_key → 覆盖它的追踪源
    listed_dirs: set[str] = field(default_factory=set)  # 成功列出的目录：里面没有的文件确实不在
    unreachable: list[tuple[str, str]] = field(default_factory=list)  # (路径, 原因)


def list_source(source: TrackingSource, listing: Listing) -> str:
    """枚举一个追踪源，结果并入 listing；返回访问问题（空串表示正常）。"""
    root = Path(source.path)
    if source.kind == "file":
        parent = root.parent
        try:
            exists = root.is_file()
        except OSError as e:
            listing.unreachable.append((str(root), str(e)))
            return f"无法访问：{e}"
        if parent.is_dir():
            try:
                os.listdir(parent)
                listing.listed_dirs.add(path_key(parent))
            except OSError as e:
                listing.unreachable.append((str(parent), str(e)))
                return f"所在文件夹无法访问：{e}"
        else:
            listing.unreachable.append((str(parent), "文件夹不存在或无法访问"))
            return "所在文件夹不存在或无法访问"
        if exists:
            _add(listing, root, source.id)
            return ""
        return "文件不在原位置"

    if not root.is_dir():
        listing.unreachable.append((str(root), "文件夹不存在或无法访问"))
        return "文件夹不存在或无法访问"
    problems = []
    stack = [root]
    while stack:
        folder = stack.pop()
        try:
            with os.scandir(folder) as entries:
                entries = list(entries)
        except OSError as e:
            listing.unreachable.append((str(folder), str(e)))
            problems.append(f"{folder}：{e.strerror or e}")
            continue
        listing.listed_dirs.add(path_key(folder))
        for entry in entries:
            try:
                if _is_link(entry):
                    continue  # 不跟随符号链接，避免循环和读到范围外的文件
                if entry.is_dir(follow_symlinks=False):
                    if source.recursive and entry.name.lower() not in EXCLUDED_DIRS and not entry.name.startswith("."):
                        stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False) and is_supported(entry.name):
                    _add(listing, Path(entry.path), source.id)
            except OSError as e:
                listing.unreachable.append((entry.path, str(e)))
    return "；".join(problems[:3]) + (f" 等 {len(problems)} 处" if len(problems) > 3 else "") if problems else ""


def _add(listing: Listing, path: Path, source_id: int) -> None:
    key = path_key(path)
    listing.files.setdefault(key, path)
    listing.covered_by.setdefault(key, []).append(source_id)


def covers(source: TrackingSource, key: str) -> bool:
    """追踪源是否覆盖这个路径（不看文件是否存在）。"""
    src = path_key(source.path)
    if source.kind == "file":
        return key == src
    if source.recursive:
        return is_within(key, src) and key != src
    return path_key(Path(key).parent) == src


def expected_daily_dates(after: date, until: date) -> list[date]:
    """after（不含）到 until（不含）之间应当执行每日扫描的日期。"""
    days, d = [], after + timedelta(days=1)
    while d < until:
        if not config.TRACKING_WORKDAYS_ONLY or d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


class Unstable(Exception):
    """文件在读取过程中发生变化，或刚被修改（可能正在写入）。"""


def read_stable(path: Path) -> tuple[bytes, os.stat_result]:
    """读取文件；读前读后大小或 mtime 不一致、或 mtime 距现在太近时视为正在写入。"""
    before = os.stat(path)
    if time.time() - before.st_mtime < config.TRACKING_SETTLE_SECONDS:
        raise Unstable("刚被修改，可能正在写入")
    data = path.read_bytes()
    after = os.stat(path)
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or len(data) != after.st_size:
        raise Unstable("读取过程中文件发生变化，可能正在写入")
    return data, after


@dataclass
class ScanResult:
    run_id: int
    status: str
    stats: dict


class Scanner:
    def __init__(
        self,
        engine: Engine,
        store: MaterialStore,
        blobs: BlobStore | None = None,
        reader: Callable[[Path], tuple[bytes, os.stat_result]] = read_stable,
    ) -> None:
        self.engine = engine
        self.store = store
        self.blobs = blobs or BlobStore()
        self.reader = reader  # 可替换，便于测试“正在写入”
        self._session = sessionmaker(engine, expire_on_commit=False)
        self.owner = uuid.uuid4().hex

    # ---- 扫描锁（跨进程）
    def _acquire(self, collection_id: int) -> bool:
        now = datetime.now()
        with self._session() as s:
            s.execute(delete(ScanLock).where(ScanLock.collection_id == collection_id, ScanLock.expires_at < now))
            s.add(
                ScanLock(
                    collection_id=collection_id,
                    owner=self.owner,
                    expires_at=now + timedelta(seconds=config.TRACKING_LOCK_SECONDS),
                )
            )
            try:
                s.commit()
            except IntegrityError:
                return False
            # 持有锁时不可能有别的扫描在进行：残留的 running 记录来自中断的进程
            s.execute(
                update(ScanRun)
                .where(ScanRun.collection_id == collection_id, ScanRun.status == RUNNING)
                .values(status=INTERRUPTED, error="扫描进程中断，未完成", finished_at=now)
            )
            s.commit()
        return True

    def _renew(self, collection_id: int) -> None:
        with self._session() as s:
            s.execute(
                update(ScanLock)
                .where(ScanLock.collection_id == collection_id, ScanLock.owner == self.owner)
                .values(expires_at=datetime.now() + timedelta(seconds=config.TRACKING_LOCK_SECONDS))
            )
            s.commit()

    def _release(self, collection_id: int) -> None:
        with self._session() as s:
            s.execute(delete(ScanLock).where(ScanLock.collection_id == collection_id, ScanLock.owner == self.owner))
            s.commit()

    # ---- 扫描
    def scan(
        self,
        collection_id: int,
        trigger: str,
        *,
        baseline_sources: set[int] | None = None,
        scheduled_for: date | None = None,
        attempt: int = 1,
        interrupt_after: int | None = None,
    ) -> ScanResult:
        """扫描整个集合。baseline_sources：刚注册的追踪源，其中新发现的文件保存为 baseline。

        interrupt_after 只用于测试：处理这么多个文件后模拟进程中断（抛出 KeyboardInterrupt，不收尾）。
        """
        if not self._acquire(collection_id):
            raise ScanBusy("该集合正在扫描，请稍后再试")
        run = ScanRun(collection_id=collection_id, trigger=trigger, scheduled_for=scheduled_for, attempt=attempt)
        with self._session() as s:
            s.add(run)
            s.commit()
        stats = {
            "files": 0, "new_snapshots": 0, "unchanged": 0, "stat_skipped": 0, "mtime_only": 0, "created": 0,
            "modified": 0, "deleted": 0, "possibly_moved": 0, "unreachable": 0, "untracked": 0, "unstable": [],
            "errors": [], "parse_errors": 0,
        }  # fmt: skip
        try:
            stats["gap"] = self._coverage_gap(collection_id, run)
            self._run(collection_id, run, trigger, baseline_sources or set(), stats, interrupt_after)
        except KeyboardInterrupt:
            raise  # 模拟进程被杀：不收尾，记录保持 running，锁留到过期
        except Exception as e:
            log.exception("扫描失败")
            self._finish(run.id, FAILED, stats, f"{type(e).__name__}: {e}")
            self._release(collection_id)
            return ScanResult(run.id, FAILED, stats)
        self._finish(run.id, DONE, stats, "")
        self._release(collection_id)
        return ScanResult(run.id, DONE, stats)

    def _finish(self, run_id: int, status: str, stats: dict, error: str) -> None:
        with self._session() as s:
            s.execute(
                update(ScanRun)
                .where(ScanRun.id == run_id)
                .values(status=status, stats=stats, error=error[:1000], finished_at=datetime.now())
            )
            s.commit()

    def _coverage_gap(self, collection_id: int, run: ScanRun) -> dict | None:
        """上次成功扫描到现在之间漏掉的每日扫描日期：这段时间的变化只能合并成一次，如实记下来。"""
        with self._session() as s:
            last = s.scalar(
                select(func.max(ScanRun.finished_at)).where(
                    ScanRun.collection_id == collection_id, ScanRun.status == DONE
                )
            )
        if last is None:
            return None
        missed = expected_daily_dates(last.date(), run.started_at.date())
        if not missed:
            return None
        return {"since": last.isoformat(timespec="minutes"), "missed_days": [d.isoformat() for d in missed]}

    def _run(
        self,
        collection_id: int,
        run: ScanRun,
        trigger: str,
        baseline_sources: set[int],
        stats: dict,
        interrupt_after: int | None,
    ) -> None:
        with self._session() as s:
            sources = list(s.scalars(select(TrackingSource).where(TrackingSource.collection_id == collection_id)))
            files = list(s.scalars(select(TrackedFile).where(TrackedFile.collection_id == collection_id)))
        active = [x for x in sources if x.status == ACTIVE]
        paused = [x for x in sources if x.status == PAUSED]

        listing = Listing()
        for source in active:
            problem = list_source(source, listing)
            if problem != source.last_error:
                with self._session() as s:
                    s.execute(update(TrackingSource).where(TrackingSource.id == source.id).values(last_error=problem))
                    s.commit()

        # 同一路径只对应一个文件记录：文件不在后又在原路径出现，按同一文件处理（与最新版本比较）
        by_key = {f.path_key: f for f in files}
        # 1. 原有文件中这次没列出来的：判断是不在了、无法访问、还是已不在追踪范围
        vanished: list[TrackedFile] = []
        for f in files:
            if f.path_key in listing.files:
                continue
            covering = [x for x in active if covers(x, f.path_key)]
            if not covering:
                if any(covers(x, f.path_key) for x in paused):
                    continue  # 只被暂停的追踪源覆盖：保持原状态，不扫描
                if f.status != UNTRACKED:
                    self._set_status(f.id, UNTRACKED, "已不在任何启用的追踪源范围内")
                    stats["untracked"] += 1
                continue
            parent = path_key(Path(f.path).parent)
            if parent in listing.listed_dirs:
                if f.status == PRESENT:
                    vanished.append(f)
                elif f.status == UNREACHABLE:
                    vanished.append(f)  # 之前无法访问、现在能访问且确实不在
            elif f.status not in (MISSING, UNTRACKED):
                self._set_status(f.id, UNREACHABLE, "路径或权限暂时无法访问")
                stats["unreachable"] += 1

        # 2. 列出来的文件：新文件或已有文件
        new_paths: list[tuple[str, Path]] = []
        processed = 0
        last_renew = time.monotonic()
        for key, path in listing.files.items():
            if interrupt_after is not None and processed >= interrupt_after:
                raise KeyboardInterrupt
            stats["files"] += 1
            existing = by_key.get(key)
            if existing is None:
                new_paths.append((key, path))
                continue
            self._scan_existing(existing, path, run, trigger, stats)
            processed += 1
            if time.monotonic() - last_renew > 60:
                self._renew(run.collection_id)
                last_renew = time.monotonic()

        # 3. 新文件：先算哈希，与本次消失的文件比对，推断可能的移动或改名
        vanished_hash = {f.id: self._latest(f.id) for f in vanished}
        claimed: set[int] = set()
        for key, path in new_paths:
            if interrupt_after is not None and processed >= interrupt_after:
                raise KeyboardInterrupt
            covering = listing.covered_by.get(key, [])
            kind = BASELINE if baseline_sources & set(covering) else FIRST_SEEN
            self._scan_new(collection_id, key, path, kind, run, stats, vanished, vanished_hash, claimed)
            processed += 1

        # 4. 确实不在了的文件（没有被认作移动）记 DELETED
        for f in vanished:
            if f.id in claimed:
                continue
            latest = vanished_hash.get(f.id)
            with self._session() as s:
                s.execute(
                    update(TrackedFile)
                    .where(TrackedFile.id == f.id)
                    .values(status=MISSING, missing_since=datetime.now(), last_error="")
                )
                s.add(
                    FileChange(
                        collection_id=collection_id,
                        file_id=f.id,
                        scan_run_id=run.id,
                        change_type=DELETED,
                        from_snapshot_id=latest.id if latest else None,
                        details={"path": f.path, "basis": "所在文件夹可以正常访问，但文件已不在"},
                    )
                )
                s.commit()
            stats["deleted"] += 1

    def _set_status(self, file_id: int, status: str, error: str = "") -> None:
        with self._session() as s:
            s.execute(update(TrackedFile).where(TrackedFile.id == file_id).values(status=status, last_error=error))
            s.commit()

    def _latest(self, file_id: int, s: Session | None = None) -> FileSnapshot | None:
        query = select(FileSnapshot).where(FileSnapshot.file_id == file_id).order_by(FileSnapshot.version.desc())
        if s is not None:
            return s.scalars(query.limit(1)).first()
        with self._session() as s2:
            return s2.scalars(query.limit(1)).first()

    def _read(self, path: Path, stats: dict) -> tuple[bytes, os.stat_result] | None:
        """读取文件；正在写入时等一会重试一次，仍不稳定则跳过。"""
        for attempt in range(2):
            try:
                return self.reader(path)
            except Unstable as e:
                if attempt == 0 and config.TRACKING_SETTLE_SECONDS > 0:
                    time.sleep(config.TRACKING_SETTLE_SECONDS)
                    continue
                stats["unstable"].append(f"{path}：{e}")
                return None
        return None

    def _too_large(self, path: Path, size: int, stats: dict) -> bool:
        if size > config.TRACKING_MAX_FILE_MB * 1024 * 1024:
            stats["errors"].append(f"{path}：超过 {config.TRACKING_MAX_FILE_MB:g}MB，未采集")
            return True
        return False

    def _scan_existing(self, f: TrackedFile, path: Path, run: ScanRun, trigger: str, stats: dict) -> None:
        try:
            st = os.stat(path)
        except OSError as e:
            self._set_status(f.id, UNREACHABLE, str(e))
            stats["unreachable"] += 1
            return
        latest = self._latest(f.id)
        if latest and f.status == PRESENT and st.st_size == f.size and st.st_mtime == f.mtime:
            stats["stat_skipped"] += 1  # 大小和 mtime 都没变：不读文件
            stats["unchanged"] += 1
            return
        if self._too_large(path, st.st_size, stats):
            self._set_status(f.id, PRESENT, f"超过 {config.TRACKING_MAX_FILE_MB:g}MB，未采集")
            return
        try:
            read = self._read(path, stats)
        except OSError as e:
            self._set_status(f.id, UNREACHABLE, str(e))
            stats["unreachable"] += 1
            return
        if read is None:
            return
        data, st = read
        digest = sha256_bytes(data)
        if latest and latest.sha256 == digest:
            with self._session() as s:  # 只有 mtime 变了：更新筛选用的大小和 mtime，不存新版本
                s.execute(
                    update(TrackedFile)
                    .where(TrackedFile.id == f.id)
                    .values(
                        size=st.st_size, mtime=st.st_mtime, status=PRESENT, last_error="", last_seen_at=datetime.now()
                    )
                )
                s.commit()
            stats["unchanged"] += 1
            stats["mtime_only"] += 1
            return
        kind = FINAL if trigger == PRE_REPORT else INCREMENTAL
        snapshot = self._save_snapshot(f, path, data, digest, st, kind, run, stats, gap=bool(stats.get("gap")))
        if snapshot is None:
            return
        change = MODIFIED if latest else CREATED
        with self._session() as s:
            s.add(
                FileChange(
                    collection_id=f.collection_id,
                    file_id=f.id,
                    scan_run_id=run.id,
                    change_type=change,
                    from_snapshot_id=latest.id if latest else None,
                    to_snapshot_id=snapshot.id,
                    details={"path": str(path), "reappeared": f.status in (MISSING, UNREACHABLE)},
                )
            )
            s.commit()
        stats["modified" if change == MODIFIED else "created"] += 1

    def _scan_new(
        self,
        collection_id: int,
        key: str,
        path: Path,
        kind: str,
        run: ScanRun,
        stats: dict,
        vanished: list[TrackedFile],
        vanished_hash: dict[int, FileSnapshot | None],
        claimed: set[int],
    ) -> None:
        try:
            st = os.stat(path)
        except OSError as e:
            stats["errors"].append(f"{path}：{e}")
            return
        if self._too_large(path, st.st_size, stats):
            return
        try:
            read = self._read(path, stats)
        except OSError as e:
            stats["errors"].append(f"{path}：{e}")
            return
        if read is None:
            return
        data, st = read
        digest = sha256_bytes(data)
        # Hash 相同、且原文件这次消失了：推断为移动或改名（同名优先）。只作标注，不合并身份
        candidates = [f for f in vanished if f.id not in claimed and (h := vanished_hash.get(f.id)) and h.sha256 == digest]
        candidates.sort(key=lambda f: Path(f.path).name.lower() != path.name.lower())
        moved_from = candidates[0] if candidates else None
        f = TrackedFile(
            collection_id=collection_id,
            path=str(path),
            path_key=key,
            file_type=path.suffix.lower().lstrip("."),
            status=PRESENT,
            first_seen_at=datetime.now(),
            moved_from_id=moved_from.id if moved_from else None,
        )
        with self._session() as s:
            s.add(f)
            s.commit()
        snapshot = self._save_snapshot(f, path, data, digest, st, kind, run, stats, gap=False)
        if snapshot is None:
            return
        if moved_from:
            claimed.add(moved_from.id)
            basis = ["内容哈希与消失的文件相同", "原文件在本次扫描中消失"]
            if Path(moved_from.path).name.lower() == path.name.lower():
                basis.append("文件名相同")
            with self._session() as s:
                s.execute(
                    update(TrackedFile)
                    .where(TrackedFile.id == moved_from.id)
                    .values(status=MISSING, missing_since=datetime.now(), last_error=f"可能已移动到 {path}")
                )
                s.add(
                    FileChange(
                        collection_id=collection_id,
                        file_id=f.id,
                        scan_run_id=run.id,
                        change_type=POSSIBLY_MOVED,
                        from_snapshot_id=vanished_hash[moved_from.id].id,
                        to_snapshot_id=snapshot.id,
                        details={"old_file_id": moved_from.id, "old_path": moved_from.path, "new_path": str(path), "basis": basis},
                    )
                )
                s.commit()
            stats["possibly_moved"] += 1
        elif kind == FIRST_SEEN:
            with self._session() as s:
                s.add(
                    FileChange(
                        collection_id=collection_id,
                        file_id=f.id,
                        scan_run_id=run.id,
                        change_type=CREATED,
                        to_snapshot_id=snapshot.id,
                        details={"path": str(path), "basis": "扫描时首次发现，首次发现时间不代表创建时间"},
                    )
                )
                s.commit()
            stats["created"] += 1

    def _save_snapshot(
        self,
        f: TrackedFile,
        path: Path,
        data: bytes,
        digest: str,
        st: os.stat_result,
        kind: str,
        run: ScanRun,
        stats: dict,
        gap: bool,
    ) -> FileSnapshot | None:
        """保存一个版本：原始字节进版本库、解析文本存为材料、版本记录与文件状态同一事务提交。"""
        try:
            self.blobs.put(data, digest)
        except StoreFull as e:
            stats["errors"].append(f"{path}：{e}")
            self._set_status(f.id, PRESENT, str(e))
            return None
        material_id, parse_error = self._parse(f.file_type, path.name, data, digest)
        if parse_error:
            stats["parse_errors"] += 1
        try:
            meta = extract_meta(f.file_type, data)
        except Exception as e:  # 元数据只用于归属判断，读不出不影响版本保存
            meta = {"error": f"{type(e).__name__}: {e}"[:200]}
        with self._session() as s:
            latest = self._latest(f.id, s)
            if latest and latest.sha256 == digest:  # 并发或重试时已经保存过
                return latest
            snapshot = FileSnapshot(
                file_id=f.id,
                version=(latest.version + 1) if latest else 1,
                kind=kind,
                sha256=digest,
                size=len(data),
                mtime=st.st_mtime,
                path=str(path),
                material_id=material_id,
                parse_error=parse_error,
                meta=meta,
                scan_run_id=run.id,
                gap_before=gap and latest is not None,
            )
            s.add(snapshot)
            s.execute(
                update(TrackedFile)
                .where(TrackedFile.id == f.id)
                .values(
                    size=st.st_size,
                    mtime=st.st_mtime,
                    status=PRESENT,
                    last_error="",
                    last_seen_at=datetime.now(),
                    missing_since=None,
                )
            )
            s.commit()
        stats["new_snapshots"] += 1
        return snapshot

    def _parse(self, kind: str, filename: str, data: bytes, digest: str) -> tuple[int | None, str]:
        """复用解析结果：相同字节内容已解析过的直接用那份材料；否则用现有解析器解析并存为材料。"""
        with self._session() as s:
            reused = s.scalar(
                select(FileSnapshot.material_id)
                .where(FileSnapshot.sha256 == digest, FileSnapshot.material_id.is_not(None))
                .limit(1)
            )
        if reused is not None:
            try:
                self.store.get(reused)
                return reused, ""
            except ValueError:
                pass  # 材料被删了，重新解析
        try:
            doc = load_document(filename, data)
        except ValueError as e:
            return None, str(e)
        if not doc.text.strip():
            return None, "未从文件中提取到文字" + ("，可能是扫描版 PDF" if kind == "pdf" else "")
        material, _ = self.store.add_text(
            filename, doc.text, source="tracked", kind=kind, markdown=doc.markdown, report=doc.report
        )
        return material.id, ""

    def daily_runs(self, collection_id: int) -> list[ScanRun]:
        with self._session() as s:
            return list(
                s.scalars(
                    select(ScanRun)
                    .where(ScanRun.collection_id == collection_id, ScanRun.trigger == DAILY)
                    .order_by(ScanRun.id)
                )
            )
