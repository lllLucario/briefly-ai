"""命令行：briefly track <子命令>。追踪文件：添加、查看版本与变化、检查更新、清理旧版本。周报见 briefly weekly。"""
import argparse
import sys
from datetime import datetime, timedelta

from ..db import create_db_engine
from ..materials import MaterialStore
from .models import ScanRun
from .scanner import ScanBusy, ScanResult
from .service import AddResult, TrackingService

STATUS_TEXT = {"active": "启用", "paused": "已暂停", "removed": "已移除"}
FILE_STATUS = {
    "present": "正常",
    "missing": "不在了",
    "unreachable": "无法访问",
    "untracked": "已不追踪",
}
CHANGE_TEXT = {
    "CREATED": "新出现",
    "MODIFIED": "修改",
    "DELETED": "删除",
    "POSSIBLY_MOVED": "可能移动/改名",
    "UNCHANGED": "未变化",
}
KIND_TEXT = {"baseline": "基线", "first_seen": "首次发现", "incremental": "增量", "final": "整理清单前"}
TRIGGER_TEXT = {"register": "添加时", "manual": "手动", "daily": "每日", "pre_report": "整理清单前"}


def _fmt_time(t: datetime | None) -> str:
    return t.strftime("%m-%d %H:%M") if t else "-"


def print_scan(result: ScanResult | None) -> None:
    if result is None:
        return
    st = result.stats
    head = "扫描完成" if result.status == "done" else f"扫描{result.status}"
    print(
        f"{head}（扫描 {result.run_id}）：{st['files']} 个文件，新版本 {st['new_snapshots']}，未变化 {st['unchanged']}"
        f"（其中只改了修改时间 {st['mtime_only']}），新出现 {st['created']}，修改 {st['modified']}，"
        f"删除 {st['deleted']}，可能移动 {st['possibly_moved']}，无法访问 {st['unreachable']}"
    )
    if st.get("gap"):
        print(f"  注意：{st['gap']['since']} 之后漏了 {len(st['gap']['missed_days'])} 次每日扫描，这期间的变化合并为一次")
    for line in st.get("unstable", []):
        print(f"  跳过（可能正在写入，下次再采集）：{line}")
    for line in st.get("errors", []):
        print(f"  问题：{line}")
    if st.get("parse_errors"):
        print(f"  {st['parse_errors']} 个版本解析失败：原始文件已保存，可在修复解析后重新比较")


def print_add(result: AddResult) -> None:
    for source in result.added:
        kind = {"folder": "文件夹" + ("（含子文件夹）" if source.recursive else ""), "feishu": "飞书文档"}.get(source.kind, "文件")
        print(f"已添加追踪源 {source.id}：{kind} {source.path}")
    for message in result.messages:
        print(f"  {message}")
    print_scan(result.scan)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="briefly track", description="追踪文件：记录文件版本与变化（周报的审核清单见 briefly weekly）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add", help="添加文件或文件夹（可多个），立即采集基线")
    p.add_argument("paths", nargs="+", help="文件、文件夹，或飞书文档链接（需先把应用添加为文档协作者）")
    p.add_argument("--no-recursive", action="store_true", help="文件夹只追踪第一层")
    p = sub.add_parser("sources", help="列出追踪源")
    p.add_argument("--all", action="store_true", help="包括已移除的")
    for name, text in [("pause", "暂停追踪源"), ("resume", "恢复追踪源"), ("remove", "移除追踪源（不删除文件和历史版本）")]:
        p = sub.add_parser(name, help=text)
        p.add_argument("source_id", type=int)

    sub.add_parser("files", help="列出被追踪的文件")
    p = sub.add_parser("history", help="查看一个文件的版本")
    p.add_argument("file_id", type=int)
    sub.add_parser("scan", help="检查更新（手动扫描）")
    sub.add_parser("scans", help="扫描记录")
    p = sub.add_parser("changes", help="文件级变化记录")
    p.add_argument("--days", type=int, default=7, help="最近几天，默认 7")
    p = sub.add_parser("diff", help="比较一个文件的两个版本（默认：上一版本与最新版本）")
    p.add_argument("file_id", type=int)
    p.add_argument("--from", dest="from_version", type=int)
    p.add_argument("--to", dest="to_version", type=int)

    p = sub.add_parser("prune", help="清理旧版本（保护被审核清单引用的版本和每个文件的最新版本）")
    p.add_argument("--keep-days", type=int, default=60)
    return ap


def main(argv: list[str] | None = None, service: TrackingService | None = None) -> int:
    args = build_parser().parse_args(argv)
    if service is None:
        engine = create_db_engine()
        service = TrackingService(engine, MaterialStore(engine))
    try:
        return _dispatch(args, service) or 0
    except (ValueError, ScanBusy) as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1


def _dispatch(args, svc: TrackingService) -> int | None:
    cmd = args.cmd
    if cmd == "add":
        print_add(svc.add_paths(args.paths, recursive=not args.no_recursive))
    elif cmd == "sources":
        for x in svc.sources(include_removed=args.all):
            kind = {"folder": "文件夹", "feishu": "飞书文档"}.get(x.kind, "文件")
            extra = "" if x.kind != "folder" or x.recursive else "（仅第一层）"
            problem = f"  ⚠ {x.last_error}" if x.last_error else ""
            print(f"  {x.id}. [{STATUS_TEXT[x.status]}] {kind}{extra} {x.path}{problem}")
    elif cmd in ("pause", "remove"):
        x = svc.pause_source(args.source_id) if cmd == "pause" else svc.remove_source(args.source_id)
        print(f"追踪源 {x.id} {STATUS_TEXT[x.status]}：{x.path}")
    elif cmd == "resume":
        x, scan = svc.resume_source(args.source_id)
        print(f"追踪源 {x.id} 已恢复：{x.path}")
        print_scan(scan)
    elif cmd == "files":
        counts = svc.snapshot_counts()
        for f in svc.files():
            note = f"  （{f.last_error}）" if f.last_error else ""
            moved = f"  可能由文件 {f.moved_from_id} 移动而来" if f.moved_from_id else ""
            print(f"  {f.id}. [{FILE_STATUS.get(f.status, f.status)}] {f.path}  {counts.get(f.id, 0)} 个版本{moved}{note}")
    elif cmd == "history":
        f = svc.file(args.file_id)
        print(f"文件 {f.id}：{f.path}（{FILE_STATUS.get(f.status, f.status)}）")
        for snap in svc.snapshots(f.id):
            mtime = datetime.fromtimestamp(snap.mtime).strftime("%m-%d %H:%M")
            parse = f"  解析失败：{snap.parse_error}" if snap.parse_error else ""
            gap = "  （与上一版本之间有扫描中断）" if snap.gap_before else ""
            print(
                f"  v{snap.version} [{KIND_TEXT.get(snap.kind, snap.kind)}] 采集 {_fmt_time(snap.captured_at)}，"
                f"文件修改时间 {mtime}，{snap.size} 字节，{snap.sha256[:10]}{gap}{parse}"
            )
    elif cmd == "scan":
        print_scan(svc.scan())
    elif cmd == "scans":
        for r in svc.scans():
            print(_scan_line(r))
    elif cmd == "changes":
        since = datetime.now() - timedelta(days=args.days)
        files = {f.id: f for f in svc.files()}
        for ch in svc.changes(since):
            f = files.get(ch.file_id)
            basis = ch.details.get("basis")
            basis = f"  依据：{'；'.join(basis) if isinstance(basis, list) else basis}" if basis else ""
            print(f"  {_fmt_time(ch.detected_at)} {CHANGE_TEXT.get(ch.change_type, ch.change_type)} 文件 {ch.file_id} {f.path if f else ''}{basis}")
    elif cmd == "diff":
        print_diff(svc, args.file_id, args.from_version, args.to_version)
    elif cmd == "prune":
        from .storage import prune

        report = prune(svc.engine, svc.blobs, keep_days=args.keep_days)
        print(f"清理了 {report['snapshots']} 个旧版本、{report['blobs']} 个不再引用的文件，保护了 {report['protected']} 个仍被引用的版本")
    return None


FLAG_TEXT = {
    "layout_noise": "部分段落只是换行或空白不同，已忽略",
    "format_only": "正文文字没有变化，可能只改了格式",
    "reordered": "只是行顺序变了",
    "pdf_text": "PDF 按提取文字比较，可能有排版误差",
    "parse_failed": "解析失败，无法比较内容",
}


def print_diff(svc: TrackingService, file_id: int, from_version: int | None, to_version: int | None) -> None:
    from .diff import Differ, format_hunk

    snaps = {s.version: s for s in svc.snapshots(file_id)}
    if not snaps:
        print("（这个文件还没有版本）")
        return
    to_v = to_version or max(snaps)
    from_v = from_version if from_version is not None else max((v for v in snaps if v < to_v), default=None)
    if to_v not in snaps or (from_v is not None and from_v not in snaps):
        raise ValueError(f"版本不存在，可选：{', '.join(f'v{v}' for v in sorted(snaps))}")
    result = Differ(svc).diff(snaps[from_v] if from_v else None, snaps[to_v])
    head = f"v{from_v} → v{to_v}" if from_v else f"v{to_v}（没有更早的版本，全部内容视为新出现）"
    st = result.stats()
    print(f"文件 {file_id} {head}：新增 {st['added']}，删除 {st['removed']}，修改 {st['modified']}")
    for flag in result.flags:
        if flag in FLAG_TEXT:
            print(f"  注意：{FLAG_TEXT[flag]}")
    for note in result.notes:
        print(f"  说明：{note}")
    for h in result.hunks:
        print("  " + format_hunk(h, limit=300).replace("\n", "\n  "))


def _scan_line(r: ScanRun) -> str:
    st = r.stats or {}
    detail = f"新版本 {st.get('new_snapshots', 0)}，变化 {st.get('created', 0) + st.get('modified', 0) + st.get('deleted', 0) + st.get('possibly_moved', 0)}" if st else ""
    error = f"  {r.error}" if r.error else ""
    day = f" {r.scheduled_for}" if r.scheduled_for else ""
    return f"  {r.id}. {_fmt_time(r.started_at)} {TRIGGER_TEXT.get(r.trigger, r.trigger)}{day} [{r.status}] {detail}{error}"


if __name__ == "__main__":
    sys.exit(main())
