"""追踪的管理：添加文件、文件夹或飞书文档（立即采集 Baseline）、暂停、恢复、移除，以及查询文件与版本。

追踪是材料（文件）自己的属性：全局只有一份，同一路径或同一篇飞书文档只追踪一次。
"""
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import Engine, func, select, update
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
    FeishuFolder,
    FeishuFolderMember,
    FileChange,
    FileSnapshot,
    ScanRun,
    TrackedFile,
    TrackingSource,
    latest_versions,
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
    """追踪源、文件或版本不存在。复用 MaterialNotFound，接口层统一按 404 处理。"""


@dataclass
class AddResult:
    added: list[TrackingSource] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)  # 重复、重叠、不支持、不存在等说明
    scan: ScanResult | None = None


def latest_materials(engine: Engine) -> dict[int, dict]:
    """材料库里随追踪更新的材料：{material_id: {file_id, key}}。"""
    with sessionmaker(engine)() as s:
        return {material_id: {"file_id": file_id, "key": key} for material_id, file_id, key in s.execute(latest_versions())}


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
        self, paths: list[str], recursive: bool = True, scan: bool = True, names: dict[str, str] | None = None
    ) -> AddResult:
        """批量添加文件、文件夹或飞书文档链接，并立即扫描建立 Baseline。"""
        result = AddResult()
        tracking_root = path_key(Path(config.TRACKING_DIR).resolve())
        with self._session() as s:
            existing = list(s.scalars(select(TrackingSource)))
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
                source = self._save_source(same, kind, key, kind == FEISHU_FOLDER, existing)
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
                if other.status == REMOVED or other is same or other.kind in (FEISHU, FEISHU_FOLDER):
                    continue
                if kind == "file" and other.kind == "folder" and covers(other, key):
                    result.messages.append(f"{raw}：已被文件夹追踪源 {other.id} 覆盖，同一文件只处理一次")
                elif kind == "folder" and covers(
                    TrackingSource(kind="folder", path=str(p), recursive=recursive), path_key(other.path)
                ):
                    result.messages.append(f"{raw}：包含已有的追踪源 {other.id}（{other.path}），重叠的文件只处理一次")
            result.added.append(self._save_source(same, kind, str(p), recursive, existing))
        if scan and result.added:
            result.scan = self.scanner.scan(REGISTER, baseline_sources={x.id for x in result.added})
        return result

    def _save_source(self, same: TrackingSource | None, kind: str, path: str, recursive: bool, existing: list) -> TrackingSource:
        with self._session() as s:
            if same:  # 之前移除过：恢复为启用
                s.execute(
                    update(TrackingSource)
                    .where(TrackingSource.id == same.id)
                    .values(status=ACTIVE, recursive=recursive, last_error="")
                )
                source = s.get(TrackingSource, same.id)
            else:
                source = TrackingSource(kind=kind, path=path, recursive=recursive)
                s.add(source)
            s.commit()
        existing.append(source)
        return source

    def sources(self, include_removed: bool = False) -> list[TrackingSource]:
        query = select(TrackingSource).order_by(TrackingSource.id)
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
        return source, (self.scanner.scan(MANUAL) if scan else None)

    def remove_source(self, source_id: int) -> TrackingSource:
        """移除：不再追踪。不在其他启用追踪源范围内的文件标为 untracked（不是删除），历史版本保留。"""
        source = self._set_source_status(source_id, REMOVED)
        with self._session() as s:
            active = list(s.scalars(select(TrackingSource).where(TrackingSource.status != REMOVED)))
            attach_members(s, [source, *active])  # 飞书文件夹按成员名单判断覆盖
            for f in s.scalars(select(TrackedFile)):
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

    # ---- 飞书文档的追踪开关（材料页）
    def feishu_tracking(self) -> dict[str, dict]:
        """飞书文档的追踪情况：{document_id: {source_id, status, folder}}。

        单独添加的文档 folder 为空；随文件夹追踪的，folder 是文件夹名（不能单独停止，只能移除整个文件夹）。
        同一篇文档既单独追踪、又在追踪的文件夹里时，以单独追踪的为准。
        """
        out: dict[str, dict] = {}
        with self._session() as s:
            sources = list(
                s.scalars(select(TrackingSource).where(TrackingSource.kind.in_([FEISHU, FEISHU_FOLDER]), TrackingSource.status != REMOVED))
            )
            folders = {x.source_id: x.name for x in s.scalars(select(FeishuFolder))}
            members = s.execute(
                select(FeishuFolderMember.source_id, FeishuFolderMember.file_key).where(
                    FeishuFolderMember.source_id.in_([x.id for x in sources if x.kind == FEISHU_FOLDER])
                )
            ).all()
        by_id = {x.id: x for x in sources}
        for sid, key in members:
            x = by_id[sid]
            out[key.rsplit("/", 1)[-1]] = {"source_id": x.id, "status": x.status, "folder": folders.get(sid) or "飞书文件夹"}
        for x in sources:
            if x.kind == FEISHU:
                out[x.path.rsplit("/", 1)[-1]] = {"source_id": x.id, "status": x.status, "folder": None}
        return out

    def track_material(self, material_id: int) -> AddResult:
        """开启追踪：材料是导入的飞书文档时，追踪原文档。上传、粘贴的副本没法回去检查原文件。"""
        material = self.store.get(material_id)
        feishu = (material.parse_report or {}).get("feishu") or {}
        if feishu.get("kind") != "docx" or not feishu.get("url"):
            raise ValueError("只有导入的飞书文档可以在这里开启追踪；本机文件请在“追踪的文件”里添加路径")
        return self.add_paths([feishu["url"]])

    # ---- 查询
    def last_changes(self) -> dict[int, datetime]:
        """每个文件最近一次发现变化的时间。"""
        with self._session() as s:
            rows = s.execute(select(FileChange.file_id, func.max(FileChange.detected_at)).group_by(FileChange.file_id)).all()
        return {file_id: at for file_id, at in rows}

    def last_scan(self) -> datetime | None:
        """最近一次完成的扫描时间；从没扫描过时为 None。"""
        with self._session() as s:
            return s.scalar(select(func.max(ScanRun.finished_at)).where(ScanRun.status == DONE))

    def feishu_folders(self) -> dict[int, FeishuFolder]:
        """飞书文件夹追踪源的名称与统计：{追踪源编号: FeishuFolder}。"""
        with self._session() as s:
            return {x.source_id: x for x in s.scalars(select(FeishuFolder))}

    def folder_members(self) -> dict[str, list[tuple[int, str]]]:
        """飞书文档属于哪些文件夹追踪源：{feishu://docx/<id>: [(追踪源编号, 相对路径)]}。"""
        with self._session() as s:
            rows = s.execute(select(FeishuFolderMember.file_key, FeishuFolderMember.source_id, FeishuFolderMember.rel_path)).all()
        out: dict[str, list[tuple[int, str]]] = {}
        for key, sid, rel in rows:
            out.setdefault(key, []).append((sid, rel))
        return out

    def files(self) -> list[TrackedFile]:
        with self._session() as s:
            return list(s.scalars(select(TrackedFile).order_by(TrackedFile.id)))

    def file(self, file_id: int) -> TrackedFile:
        with self._session() as s:
            f = s.get(TrackedFile, file_id)
        if f is None:
            raise TrackingNotFound(f"文件 {file_id} 不存在")
        return f

    def snapshots(self, file_id: int) -> list[FileSnapshot]:
        self.file(file_id)
        with self._session() as s:
            return list(s.scalars(select(FileSnapshot).where(FileSnapshot.file_id == file_id).order_by(FileSnapshot.version)))

    def snapshot(self, snapshot_id: int) -> FileSnapshot:
        with self._session() as s:
            snap = s.get(FileSnapshot, snapshot_id)
        if snap is None:
            raise TrackingNotFound(f"版本 {snapshot_id} 不存在")
        return snap

    def snapshot_counts(self) -> dict[int, int]:
        with self._session() as s:
            return dict(s.execute(select(FileSnapshot.file_id, func.count()).group_by(FileSnapshot.file_id)).all())

    def changes(self, since: datetime | None = None, limit: int = 200) -> list[FileChange]:
        query = select(FileChange)
        if since:
            query = query.where(FileChange.detected_at >= since)
        with self._session() as s:
            return list(s.scalars(query.order_by(FileChange.id.desc()).limit(limit)))

    def scans(self, limit: int = 20) -> list[ScanRun]:
        with self._session() as s:
            return list(s.scalars(select(ScanRun).order_by(ScanRun.id.desc()).limit(limit)))

    def scan(self, trigger: str = MANUAL) -> ScanResult:
        return self.scanner.scan(trigger)
