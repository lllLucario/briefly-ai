"""追踪集合与追踪源的管理：创建集合、批量添加文件或文件夹（立即采集 Baseline）、暂停、恢复、移除。"""
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import Engine, delete, func, select, update
from sqlalchemy.orm import sessionmaker

from .. import config
from ..feishu.links import looks_like_feishu
from ..materials import MaterialNotFound, MaterialStore
from .blobs import BlobStore
from .models import (
    ACTIVE,
    DONE,
    MANUAL,
    PAUSED,
    REGISTER,
    REMOVED,
    UNTRACKED,
    EvidenceReview,
    FeishuFolder,
    FeishuFolderMember,
    latest_versions,
    FileChange,
    FileSnapshot,
    Notification,
    PlanSettings,
    ReportPrep,
    ScanLock,
    ScheduledRun,
    ScanRun,
    SnapshotDiff,
    TrackedFile,
    TrackingCollection,
    TrackingSource,
    WeeklySchedule,
    WorkEvidence,
)
from .scanner import (
    FEISHU,
    FEISHU_FOLDER,
    ScanResult,
    Scanner,
    attach_members,
    covers,
    feishu_folder_key,
    feishu_key,
    is_supported,
    is_within,
    path_key,
)


class TrackingNotFound(MaterialNotFound):
    """集合、追踪源或文件不存在。复用 MaterialNotFound，接口层统一按 404 处理。"""


@dataclass
class AddResult:
    added: list[TrackingSource] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)  # 重复、重叠、不支持、不存在等说明
    scan: ScanResult | None = None


def latest_materials(engine: Engine) -> dict[int, list[dict]]:
    """材料库里随追踪更新的材料：{material_id: [{plan_id, plan, file_id, key}]}（同一篇可能在多个计划里）。"""
    out: dict[int, list[dict]] = {}
    with sessionmaker(engine)() as s:
        plans = {c.id: c.name for c in s.scalars(select(TrackingCollection))}
        for material_id, file_id, plan_id, key in s.execute(latest_versions()):
            out.setdefault(material_id, []).append({"plan_id": plan_id, "plan": plans.get(plan_id, ""), "file_id": file_id, "key": key})
    return out


class TrackingService:
    def __init__(
        self,
        engine: Engine,
        store: MaterialStore,
        blobs: BlobStore | None = None,
        scanner: Scanner | None = None,
        feishu_factory: Callable | None = None,
    ):
        """feishu_factory 创建飞书客户端（追踪飞书文档用）；不传时按配置创建，测试时注入模拟响应。"""
        self.engine = engine
        self.store = store
        self.blobs = blobs or BlobStore()
        self.feishu_factory = feishu_factory
        self.scanner = scanner or Scanner(engine, store, self.blobs, feishu_factory=feishu_factory)
        self._session = sessionmaker(engine, expire_on_commit=False)

    # ---- 集合
    def create_collection(self, name: str, owner_names: list[str] | None = None) -> TrackingCollection:
        name = name.strip()
        if not name:
            raise ValueError("集合名称不能为空")
        if name.isdigit():
            raise ValueError("集合名称不能是纯数字（数字用来表示编号）")
        with self._session() as s:
            if s.scalar(select(TrackingCollection).where(TrackingCollection.name == name)):
                raise ValueError(f"集合“{name}”已存在")
            c = TrackingCollection(name=name, owner_names=[n.strip() for n in owner_names or [] if n.strip()])
            s.add(c)
            s.commit()
            return c

    def set_owners(self, ref: str | int, owner_names: list[str]) -> TrackingCollection:
        c = self.collection(ref)
        names = [n.strip() for n in owner_names if n.strip()]
        with self._session() as s:
            s.execute(update(TrackingCollection).where(TrackingCollection.id == c.id).values(owner_names=names))
            s.commit()
        return self.collection(c.id)

    def collection(self, ref: str | int) -> TrackingCollection:
        """按编号或名称查找。"""
        with self._session() as s:
            if isinstance(ref, int) or str(ref).isdigit():
                c = s.get(TrackingCollection, int(ref))
            else:
                c = s.scalar(select(TrackingCollection).where(TrackingCollection.name == str(ref).strip()))
        if c is None:
            raise TrackingNotFound(f"集合 {ref} 不存在")
        return c

    def list_collections(self) -> list[TrackingCollection]:
        with self._session() as s:
            return list(s.scalars(select(TrackingCollection).order_by(TrackingCollection.id)))

    def delete_collection(self, ref: str | int) -> None:
        """删除集合及其全部追踪记录、版本、证据（原文件不受影响）。版本库中不再被引用的内容随后清理。"""
        c = self.collection(ref)
        with self._session() as s:
            file_ids = select(TrackedFile.id).where(TrackedFile.collection_id == c.id)
            snap_ids = select(FileSnapshot.id).where(FileSnapshot.file_id.in_(file_ids))
            prep_ids = select(ReportPrep.id).where(ReportPrep.collection_id == c.id)
            s.execute(delete(ScheduledRun).where(ScheduledRun.collection_id == c.id))
            s.execute(delete(WeeklySchedule).where(WeeklySchedule.collection_id == c.id))
            s.execute(delete(Notification).where(Notification.collection_id == c.id))
            s.execute(delete(EvidenceReview).where(EvidenceReview.prep_id.in_(prep_ids)))
            s.execute(delete(WorkEvidence).where(WorkEvidence.prep_id.in_(prep_ids)))
            s.execute(delete(ReportPrep).where(ReportPrep.collection_id == c.id))
            s.execute(delete(FileChange).where(FileChange.collection_id == c.id))
            s.execute(
                delete(SnapshotDiff).where(
                    SnapshotDiff.to_snapshot_id.in_(snap_ids) | SnapshotDiff.from_snapshot_id.in_(snap_ids)
                )
            )
            s.execute(delete(FileSnapshot).where(FileSnapshot.file_id.in_(file_ids)))
            s.execute(update(TrackedFile).where(TrackedFile.collection_id == c.id).values(moved_from_id=None))
            s.execute(delete(TrackedFile).where(TrackedFile.collection_id == c.id))
            s.execute(delete(ScanRun).where(ScanRun.collection_id == c.id))
            s.execute(delete(ScanLock).where(ScanLock.collection_id == c.id))
            source_ids = select(TrackingSource.id).where(TrackingSource.collection_id == c.id)
            s.execute(delete(FeishuFolderMember).where(FeishuFolderMember.source_id.in_(source_ids)))
            s.execute(delete(FeishuFolder).where(FeishuFolder.source_id.in_(source_ids)))
            s.execute(delete(TrackingSource).where(TrackingSource.collection_id == c.id))
            s.execute(delete(PlanSettings).where(PlanSettings.collection_id == c.id))
            s.execute(delete(TrackingCollection).where(TrackingCollection.id == c.id))
            s.commit()
        from .storage import collect_garbage

        collect_garbage(self.engine, self.blobs)

    # ---- 追踪源
    def _feishu_path(self, raw: str, known_name: str = "") -> tuple[str, str, str]:
        """飞书链接 → (追踪源类型, 路径, 文件夹名)。

        文档：feishu://docx/<id>（知识库链接换成文档本身），确认能读取；
        文件夹：feishu://folder/<token>，确认能列出（通常要连接飞书账号，应用身份看不到个人云空间）。
        """
        from ..feishu import FeishuError, parse_link
        from ..feishu.client import default_client
        from ..feishu.docx import document_revision
        from ..feishu.drive import folder_name, list_folder
        from ..feishu.links import BITABLE, FOLDER, MINUTES
        from ..feishu.service import resolve

        link = parse_link(raw)
        try:
            with (self.feishu_factory or default_client)() as client:
                if link.kind == FOLDER:
                    list_folder(client, link.token)
                    name = known_name
                    if not name:  # 网页会带上名称；命令行添加时才查（需要额外权限，查不到就不显示名称）
                        try:
                            name = folder_name(client, link.token)
                        except FeishuError:
                            name = ""
                    return FEISHU_FOLDER, feishu_folder_key(link.token), name
                link = resolve(client, link)
                if link.kind in (BITABLE, MINUTES):
                    raise ValueError("多维表格、妙记暂不支持追踪，可以在“材料”页导入当前内容")
                document_revision(client, link.token)
        except FeishuError as e:
            raise ValueError(str(e)) from e
        return FEISHU, feishu_key(link.token), ""

    def add_paths(
        self, ref: str | int, paths: list[str], recursive: bool = True, scan: bool = True, names: dict[str, str] | None = None
    ) -> AddResult:
        """批量添加文件、文件夹或飞书文档链接，并立即扫描建立 Baseline。"""
        c = self.collection(ref)
        result = AddResult()
        tracking_root = path_key(Path(config.TRACKING_DIR).resolve())
        with self._session() as s:
            existing = list(s.scalars(select(TrackingSource).where(TrackingSource.collection_id == c.id)))
        for raw in paths:
            if looks_like_feishu(raw):
                try:
                    kind, key, name = self._feishu_path(raw, (names or {}).get(raw, ""))
                except ValueError as e:
                    result.messages.append(f"{raw}：{e}，未添加")
                    continue
                same = next((x for x in existing if x.kind == kind and x.path == key), None)
                if same and same.status != REMOVED:
                    result.messages.append(f"{raw}：已在追踪（追踪源 {same.id}）")
                    continue
                source = self._save_source(c.id, same, kind, key, kind == FEISHU_FOLDER, existing)
                if kind == FEISHU_FOLDER:
                    with self._session() as s:
                        info = s.get(FeishuFolder, source.id) or FeishuFolder(source_id=source.id)
                        info.name = name or info.name
                        s.merge(info)
                        s.commit()
                result.added.append(source)
                continue
            try:
                p = Path(raw).expanduser().resolve(strict=True)
            except (OSError, RuntimeError):
                result.messages.append(f"{raw}：路径不存在或无法访问，未添加")
                continue
            key = path_key(p)
            if is_within(key, tracking_root) or is_within(tracking_root, key):
                result.messages.append(f"{raw}：包含 Briefly 自己的版本库目录，未添加")
                continue
            if p.is_dir():
                kind = "folder"
            elif p.is_file():
                if not is_supported(p.name):
                    result.messages.append(f"{raw}：不支持的文件类型或临时文件，未添加")
                    continue
                kind = "file"
            else:
                result.messages.append(f"{raw}：不是普通文件或文件夹，未添加")
                continue
            same = next((x for x in existing if x.kind == kind and x.kind != FEISHU and path_key(x.path) == key), None)
            if same and same.status != REMOVED:
                result.messages.append(f"{raw}：已在追踪（追踪源 {same.id}）")
                continue
            for other in existing:
                if other.status == REMOVED or other is same or other.kind == FEISHU:
                    continue
                if kind == "file" and other.kind == "folder" and covers(other, key):
                    result.messages.append(f"{raw}：已被文件夹追踪源 {other.id} 覆盖，同一文件只处理一次")
                elif kind == "folder" and covers(
                    TrackingSource(kind="folder", path=str(p), recursive=recursive), path_key(other.path)
                ):
                    result.messages.append(f"{raw}：包含已有的追踪源 {other.id}（{other.path}），重叠的文件只处理一次")
            result.added.append(self._save_source(c.id, same, kind, str(p), recursive, existing))
        if scan and result.added:
            result.scan = self.scanner.scan(c.id, REGISTER, baseline_sources={x.id for x in result.added})
        return result

    def _save_source(
        self, collection_id: int, same: TrackingSource | None, kind: str, path: str, recursive: bool, existing: list
    ) -> TrackingSource:
        with self._session() as s:
            if same:  # 之前移除过：恢复为启用
                s.execute(
                    update(TrackingSource)
                    .where(TrackingSource.id == same.id)
                    .values(status=ACTIVE, recursive=recursive, last_error="")
                )
                source = s.get(TrackingSource, same.id)
            else:
                source = TrackingSource(collection_id=collection_id, kind=kind, path=path, recursive=recursive)
                s.add(source)
            s.commit()
        existing.append(source)
        return source

    def sources(self, ref: str | int, include_removed: bool = False) -> list[TrackingSource]:
        c = self.collection(ref)
        query = select(TrackingSource).where(TrackingSource.collection_id == c.id).order_by(TrackingSource.id)
        if not include_removed:
            query = query.where(TrackingSource.status != REMOVED)
        with self._session() as s:
            return list(s.scalars(query))

    def source(self, source_id: int) -> TrackingSource:
        with self._session() as s:
            source = s.get(TrackingSource, source_id)
        if source is None:
            raise TrackingNotFound(f"追踪源 {source_id} 不存在")
        return source

    def pause_source(self, source_id: int) -> TrackingSource:
        """暂停：不再扫描，文件和版本保留；恢复后的第一次扫描会把暂停期间的变化合并成一次。"""
        return self._set_source_status(source_id, PAUSED)

    def resume_source(self, source_id: int, scan: bool = True) -> tuple[TrackingSource, ScanResult | None]:
        source = self._set_source_status(source_id, ACTIVE)
        return source, (self.scanner.scan(source.collection_id, MANUAL) if scan else None)

    def remove_source(self, source_id: int) -> TrackingSource:
        """移除：不再追踪。不在其他启用追踪源范围内的文件标为 untracked（不是删除），历史版本保留。"""
        source = self._set_source_status(source_id, REMOVED)
        with self._session() as s:
            active = list(
                s.scalars(
                    select(TrackingSource).where(
                        TrackingSource.collection_id == source.collection_id, TrackingSource.status != REMOVED
                    )
                )
            )
            attach_members(s, [source, *active])  # 飞书文件夹按成员名单判断覆盖
            for f in s.scalars(select(TrackedFile).where(TrackedFile.collection_id == source.collection_id)):
                if covers(source, f.path_key) and not any(covers(x, f.path_key) for x in active):
                    f.status, f.last_error = UNTRACKED, "所属追踪源已移除"
            s.commit()
        return source

    def _set_source_status(self, source_id: int, status: str) -> TrackingSource:
        source = self.source(source_id)
        if source.status == REMOVED and status != REMOVED:
            raise ValueError(f"追踪源 {source_id} 已移除；如需重新追踪，请再次添加该路径")
        with self._session() as s:
            s.execute(update(TrackingSource).where(TrackingSource.id == source_id).values(status=status))
            s.commit()
        return self.source(source_id)

    # ---- 查询
    # ---- 周报计划的默认读者、署名（网页上把集合叫“周报计划”）
    def plan_settings(self, ref: str | int) -> PlanSettings:
        c = self.collection(ref)
        with self._session() as s:
            return s.get(PlanSettings, c.id) or PlanSettings(collection_id=c.id, audience="", author="")

    def set_plan_settings(self, ref: str | int, audience: str = "", author: str = "") -> PlanSettings:
        c = self.collection(ref)
        with self._session() as s:
            row = s.get(PlanSettings, c.id)
            if row is None:
                row = PlanSettings(collection_id=c.id)
                s.add(row)
            row.audience, row.author = audience.strip()[:100], author.strip()[:100]
            s.commit()
            return row

    def last_changes(self, ref: str | int) -> dict[int, datetime]:
        """每个文件最近一次发现变化的时间。"""
        c = self.collection(ref)
        with self._session() as s:
            rows = s.execute(
                select(FileChange.file_id, func.max(FileChange.detected_at))
                .where(FileChange.collection_id == c.id)
                .group_by(FileChange.file_id)
            ).all()
        return {file_id: at for file_id, at in rows}

    def last_scan(self, ref: str | int):
        """最近一次完成的扫描时间；从没扫描过时为 None。"""
        c = self.collection(ref)
        with self._session() as s:
            return s.scalar(select(func.max(ScanRun.finished_at)).where(ScanRun.collection_id == c.id, ScanRun.status == DONE))

    def feishu_tracking(self) -> dict[str, list[dict]]:
        """所有周报计划里飞书文档的追踪情况：{document_id: [{plan_id, plan, source_id, status, folder}]}。

        单独添加的文档 folder 为空；随文件夹追踪的，folder 是文件夹名（不能单独停止，只能移除整个文件夹）。
        """
        out: dict[str, list[dict]] = {}
        with self._session() as s:
            plans = {c.id: c.name for c in s.scalars(select(TrackingCollection))}
            sources = list(
                s.scalars(
                    select(TrackingSource).where(
                        TrackingSource.kind.in_([FEISHU, FEISHU_FOLDER]), TrackingSource.status != REMOVED
                    )
                )
            )
            folders = {x.source_id: x.name for x in s.scalars(select(FeishuFolder))}
            members = s.execute(
                select(FeishuFolderMember.source_id, FeishuFolderMember.file_key).where(
                    FeishuFolderMember.source_id.in_([x.id for x in sources if x.kind == FEISHU_FOLDER])
                )
            ).all()
        by_id = {x.id: x for x in sources}

        def add(key: str, source: TrackingSource, folder: str | None) -> None:
            out.setdefault(key.rsplit("/", 1)[-1], []).append(
                {"plan_id": source.collection_id, "plan": plans.get(source.collection_id, ""),
                 "source_id": source.id, "status": source.status, "folder": folder}
            )  # fmt: skip

        for x in sources:
            if x.kind == FEISHU:
                add(x.path, x, None)
        for sid, key in members:
            add(key, by_id[sid], folders.get(sid) or "飞书文件夹")
        return out

    def feishu_folders(self, ref: str | int) -> dict[int, FeishuFolder]:
        """飞书文件夹追踪源的名称与统计：{追踪源编号: FeishuFolder}。"""
        c = self.collection(ref)
        with self._session() as s:
            rows = s.scalars(
                select(FeishuFolder).join(TrackingSource, TrackingSource.id == FeishuFolder.source_id)
                .where(TrackingSource.collection_id == c.id)
            )
            return {x.source_id: x for x in rows}

    def folder_members(self, ref: str | int) -> dict[str, list[tuple[int, str]]]:
        """飞书文档属于哪些文件夹追踪源：{feishu://docx/<id>: [(追踪源编号, 相对路径)]}。"""
        c = self.collection(ref)
        with self._session() as s:
            rows = s.execute(
                select(FeishuFolderMember.file_key, FeishuFolderMember.source_id, FeishuFolderMember.rel_path)
                .join(TrackingSource, TrackingSource.id == FeishuFolderMember.source_id)
                .where(TrackingSource.collection_id == c.id)
            ).all()
        out: dict[str, list[tuple[int, str]]] = {}
        for key, sid, rel in rows:
            out.setdefault(key, []).append((sid, rel))
        return out

    def files(self, ref: str | int) -> list[TrackedFile]:
        c = self.collection(ref)
        with self._session() as s:
            return list(s.scalars(select(TrackedFile).where(TrackedFile.collection_id == c.id).order_by(TrackedFile.id)))

    def file(self, file_id: int) -> TrackedFile:
        with self._session() as s:
            f = s.get(TrackedFile, file_id)
        if f is None:
            raise TrackingNotFound(f"文件 {file_id} 不存在")
        return f

    def snapshots(self, file_id: int) -> list[FileSnapshot]:
        self.file(file_id)
        with self._session() as s:
            return list(
                s.scalars(select(FileSnapshot).where(FileSnapshot.file_id == file_id).order_by(FileSnapshot.version))
            )

    def snapshot(self, snapshot_id: int) -> FileSnapshot:
        with self._session() as s:
            snap = s.get(FileSnapshot, snapshot_id)
        if snap is None:
            raise TrackingNotFound(f"版本 {snapshot_id} 不存在")
        return snap

    def snapshot_counts(self, ref: str | int) -> dict[int, int]:
        c = self.collection(ref)
        with self._session() as s:
            rows = s.execute(
                select(FileSnapshot.file_id, func.count())
                .join(TrackedFile, TrackedFile.id == FileSnapshot.file_id)
                .where(TrackedFile.collection_id == c.id)
                .group_by(FileSnapshot.file_id)
            ).all()
        return dict(rows)

    def changes(self, ref: str | int, since: datetime | None = None, limit: int = 200) -> list[FileChange]:
        c = self.collection(ref)
        query = select(FileChange).where(FileChange.collection_id == c.id)
        if since:
            query = query.where(FileChange.detected_at >= since)
        with self._session() as s:
            return list(s.scalars(query.order_by(FileChange.id.desc()).limit(limit)))

    def scans(self, ref: str | int, limit: int = 20) -> list[ScanRun]:
        c = self.collection(ref)
        with self._session() as s:
            return list(
                s.scalars(select(ScanRun).where(ScanRun.collection_id == c.id).order_by(ScanRun.id.desc()).limit(limit))
            )

    def scan(self, ref: str | int, trigger: str = MANUAL) -> ScanResult:
        return self.scanner.scan(self.collection(ref).id, trigger)
