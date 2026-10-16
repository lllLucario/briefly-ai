"""文件追踪的接口（/tracking/...）：追踪源、追踪的文件、版本历史与差异、检查更新；材料页的追踪开关。

追踪是材料（文件）自己的属性，全局一份。接口函数用 def：扫描要读文件，FastAPI 放进线程池执行。
"""
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import api_docs as docs
from .diff import Differ, format_hunk
from .scanner import ScanBusy
from .service import TrackingNotFound, TrackingService


class PathsIn(BaseModel):
    paths: list[str]
    recursive: bool = True
    names: dict[str, str] = {}  # 链接 → 显示名（网页从云空间列表里已知，如飞书文件夹的名称）


def scan_out(result) -> dict | None:
    return None if result is None else {"run_id": result.run_id, "status": result.status, "stats": result.stats}


def source_out(x, folders: dict | None = None) -> dict:
    out = {"id": x.id, "kind": x.kind, "path": x.path, "recursive": x.recursive, "status": x.status, "last_error": x.last_error}
    info = (folders or {}).get(x.id)
    if info is not None:  # 飞书文件夹：名称、追踪的文档数、跳过的非文档项
        out["folder"] = {"name": info.name, "docs": info.docs, "skipped": info.skipped, "listed_at": info.listed_at}
    return out


def register(app: FastAPI) -> None:
    def svc() -> TrackingService:
        return app.state.tracking

    @app.exception_handler(ScanBusy)
    def busy(request, exc: ScanBusy):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.get("/tracking/summary", tags=[docs.TRACKING], summary="追踪概况：追踪源数、文件数、最近一次检查更新")
    def summary():
        s = svc()
        return {"sources": len(s.sources()), "files": len(s.files()), "last_scan": s.last_scan()}

    @app.get("/tracking/sources", tags=[docs.TRACKING], summary="追踪源列表")
    def sources():
        folders = svc().feishu_folders()
        return [source_out(x, folders) for x in svc().sources()]

    @app.post("/tracking/sources", tags=[docs.TRACKING], summary="添加文件、文件夹或飞书链接（可多个），立即采集初始版本")
    def add_sources(body: PathsIn):
        result = svc().add_paths(body.paths, recursive=body.recursive, names=body.names)
        folders = svc().feishu_folders()
        return {"added": [source_out(x, folders) for x in result.added], "messages": result.messages, "scan": scan_out(result.scan)}

    @app.post("/tracking/sources/{source_id}/{action}", tags=[docs.TRACKING], summary="暂停 / 恢复 / 移除追踪源")
    def source_action(source_id: int, action: str):
        s = svc()
        if action == "pause":
            return {"source": source_out(s.pause_source(source_id)), "scan": None}
        if action == "resume":
            source, scan = s.resume_source(source_id)
            return {"source": source_out(source), "scan": scan_out(scan)}
        if action == "remove":
            return {"source": source_out(s.remove_source(source_id)), "scan": None}
        raise ValueError("操作只能是 pause / resume / remove")

    @app.get("/tracking/feishu-docs", tags=[docs.TRACKING], summary="飞书文档的追踪状态（材料页“追踪”一栏）")
    def feishu_docs():
        return svc().feishu_tracking()

    @app.post("/materials/{material_id}/track", tags=[docs.TRACKING], summary="开启追踪：导入的飞书文档追踪原文档的修改")
    def track_material(material_id: int):
        result = svc().track_material(material_id)
        return {"added": [source_out(x) for x in result.added], "messages": result.messages, "scan": scan_out(result.scan)}

    @app.get("/tracking/files", tags=[docs.TRACKING], summary="被追踪的文件")
    def files():
        s = svc()
        counts, changes, members = s.snapshot_counts(), s.last_changes(), s.folder_members()
        return [
            {"id": f.id, "path": f.path, "file_type": f.file_type, "status": f.status, "versions": counts.get(f.id, 0),
             "last_change": changes.get(f.id), "last_error": f.last_error, "moved_from_id": f.moved_from_id,
             "folders": [{"source_id": sid, "rel_path": rel} for sid, rel in members.get(f.path_key, [])]}
            for f in s.files()
        ]  # fmt: skip

    @app.get("/tracking/files/{file_id}/versions", tags=[docs.TRACKING], summary="一个文件的版本历史（新的在前）")
    def file_versions(file_id: int):
        snaps = svc().snapshots(file_id)
        kinds = {"baseline": "初始版本", "first_seen": "首次发现", "incremental": "检查更新", "final": "整理清单前检查"}
        return [
            {"id": x.id, "version": x.version, "kind": kinds.get(x.kind, x.kind), "captured_at": x.captured_at, "size": x.size,
             "parse_error": x.parse_error, "gap_before": x.gap_before}
            for x in reversed(snaps)
        ]  # fmt: skip

    @app.get("/tracking/files/{file_id}/diff", tags=[docs.TRACKING], summary="某个版本与上一个版本的差异")
    def file_diff(file_id: int, version: int):
        snaps = svc().snapshots(file_id)
        current = next((x for x in snaps if x.version == version), None)
        if current is None:
            raise TrackingNotFound(f"文件 {file_id} 没有第 {version} 个版本")
        previous = next((x for x in reversed(snaps) if x.version < version), None)
        result = Differ(svc()).diff(previous, current)
        return {
            "from_version": previous.version if previous else None,
            "to_version": current.version,
            "hunks": [format_hunk(h, 600) for h in result.hunks],
            "notes": result.notes,
            "truncated": result.truncated,
        }

    @app.post("/tracking/scan", tags=[docs.TRACKING], summary="检查更新（手动扫描）")
    def scan():
        return scan_out(svc().scan())
