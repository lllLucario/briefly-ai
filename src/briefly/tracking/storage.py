"""版本库的存储管理：清理不再被引用的原始文件、按保留期清理旧版本（保护仍被引用的版本）。

总量上限在写入时检查（BlobStore.put，超出时不保存新版本并在扫描记录里说明），这里负责腾出空间。
"""
from datetime import datetime, timedelta

from sqlalchemy import Engine, delete, select, update
from sqlalchemy.orm import sessionmaker

from ..materials import TRACKED, Material
from ..weekly.models import WorkEvidence
from .blobs import BlobStore
from .models import FileChange, FileSnapshot, SnapshotDiff


def collect_garbage(engine: Engine, blobs: BlobStore) -> int:
    """删除版本库中没有任何快照引用的内容和中断写入留下的临时文件，返回删除数。"""
    with sessionmaker(engine)() as s:
        referenced = set(s.scalars(select(FileSnapshot.sha256).distinct()))
    removed = blobs.clean_temp()
    for digest in blobs.all_digests() - referenced:
        blobs.delete(digest)
        removed += 1
    return removed


def protected_snapshots(engine: Engine, cutoff: datetime) -> set[int]:
    """不能删的版本：被工作记录（任何状态，含已用于周报的）引用的；每个文件的最新版本；
    每个文件在保留期之前的最后一个版本（作为之后周期比较的基线）。"""
    with sessionmaker(engine)() as s:
        keep: set[int] = set()
        for refs in s.scalars(select(WorkEvidence.source_refs)):
            for ref in refs or []:
                keep.update(x for x in (ref.get("from_snapshot_id"), ref.get("to_snapshot_id")) if x)
        latest: dict[int, FileSnapshot] = {}
        last_before: dict[int, FileSnapshot] = {}
        for snap in s.scalars(select(FileSnapshot).order_by(FileSnapshot.file_id, FileSnapshot.version)):
            latest[snap.file_id] = snap
            if snap.captured_at < cutoff:
                last_before[snap.file_id] = snap
        keep.update(x.id for x in latest.values())
        keep.update(x.id for x in last_before.values())
    return keep


def prune(engine: Engine, blobs: BlobStore, keep_days: int = 60) -> dict:
    """删除保留期之前、且不受保护的版本；随后删除不再被引用的原始文件和解析出的追踪材料。"""
    if keep_days < 1:
        raise ValueError("保留天数至少为 1")
    cutoff = datetime.now() - timedelta(days=keep_days)
    keep = protected_snapshots(engine, cutoff)
    with sessionmaker(engine)() as s:
        old = list(s.scalars(select(FileSnapshot).where(FileSnapshot.captured_at < cutoff)))
        doomed = [x for x in old if x.id not in keep]
        ids = [x.id for x in doomed]
        protected = len(old) - len(doomed)
        material_ids = {x.material_id for x in doomed if x.material_id}
        if ids:
            s.execute(delete(SnapshotDiff).where(SnapshotDiff.from_snapshot_id.in_(ids) | SnapshotDiff.to_snapshot_id.in_(ids)))
            s.execute(update(FileChange).where(FileChange.from_snapshot_id.in_(ids)).values(from_snapshot_id=None))
            s.execute(update(FileChange).where(FileChange.to_snapshot_id.in_(ids)).values(to_snapshot_id=None))
            s.execute(delete(FileSnapshot).where(FileSnapshot.id.in_(ids)))
        still_used = set(s.scalars(select(FileSnapshot.material_id).where(FileSnapshot.material_id.in_(material_ids))))
        orphan = [m for m in material_ids - still_used]
        removed_materials = 0
        for m in s.scalars(select(Material).where(Material.id.in_(orphan), Material.source == TRACKED)):
            s.delete(m)  # 用户自己的材料（source 不是 tracked）不删
            removed_materials += 1
        s.commit()
    return {
        "snapshots": len(ids),
        "materials": removed_materials,
        "blobs": collect_garbage(engine, blobs),
        "protected": protected,
    }
