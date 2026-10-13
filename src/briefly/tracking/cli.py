"""命令行：briefly track <子命令>。集合可以用编号或名称指定。"""
import argparse
import sys
from datetime import date, datetime, timedelta

from ..db import create_db_engine
from ..materials import MaterialStore
from .collection import AddResult, TrackingService
from .models import ScanRun
from .scanner import ScanBusy, ScanResult

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
KIND_TEXT = {"baseline": "基线", "first_seen": "首次发现", "incremental": "增量", "final": "周报前"}
TRIGGER_TEXT = {"register": "添加时", "manual": "手动", "daily": "每日", "pre_report": "周报前"}


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
        kind = "文件夹" + ("（含子文件夹）" if source.recursive else "") if source.kind == "folder" else "文件"
        print(f"已添加追踪源 {source.id}：{kind} {source.path}")
    for message in result.messages:
        print(f"  {message}")
    print_scan(result.scan)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="briefly track", description="文件追踪：记录文件版本与变化，生成候选工作记录")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create", help="创建追踪集合")
    p.add_argument("name")
    p.add_argument("--owner", action="append", default=[], help="你在文档中的署名（Word 修订作者等），可多次指定")
    sub.add_parser("list", help="列出集合")
    p = sub.add_parser("delete", help="删除集合（不影响原文件）")
    p.add_argument("collection")
    p.add_argument("-y", "--yes", action="store_true", help="不再确认")
    p = sub.add_parser("owner", help="设置你在文档中的署名")
    p.add_argument("collection")
    p.add_argument("names", nargs="+")

    p = sub.add_parser("add", help="添加文件或文件夹（可多个），立即采集基线")
    p.add_argument("collection")
    p.add_argument("paths", nargs="+")
    p.add_argument("--no-recursive", action="store_true", help="文件夹只追踪第一层")
    p = sub.add_parser("sources", help="列出追踪源")
    p.add_argument("collection")
    p.add_argument("--all", action="store_true", help="包括已移除的")
    for name, text in [("pause", "暂停追踪源"), ("resume", "恢复追踪源"), ("remove", "移除追踪源（不删除文件和历史版本）")]:
        p = sub.add_parser(name, help=text)
        p.add_argument("source_id", type=int)

    p = sub.add_parser("files", help="列出被追踪的文件")
    p.add_argument("collection")
    p = sub.add_parser("history", help="查看一个文件的版本")
    p.add_argument("file_id", type=int)
    p = sub.add_parser("scan", help="手动扫描")
    p.add_argument("collection")
    p = sub.add_parser("scans", help="扫描记录")
    p.add_argument("collection")
    p = sub.add_parser("changes", help="文件级变化记录")
    p.add_argument("collection")
    p.add_argument("--days", type=int, default=7, help="最近几天，默认 7")
    p = sub.add_parser("diff", help="比较一个文件的两个版本（默认：上一版本与最新版本）")
    p.add_argument("file_id", type=int)
    p.add_argument("--from", dest="from_version", type=int)
    p.add_argument("--to", dest="to_version", type=int)

    p = sub.add_parser("prepare", help="周报准备：最终扫描 + 生成候选工作记录")
    p.add_argument("collection")
    p.add_argument("--start", type=date.fromisoformat, help="周期开始（默认本周一）")
    p.add_argument("--end", type=date.fromisoformat, help="周期结束（默认本周日）")
    p.add_argument("--no-scan", action="store_true", help="不做最终扫描（只用已有版本）")
    p = sub.add_parser("review", help="审核候选工作记录（交互）")
    p.add_argument("collection")
    p.add_argument("--start", type=date.fromisoformat)
    p.add_argument("--end", type=date.fromisoformat)
    p = sub.add_parser("report", help="用已确认的工作记录生成周报草稿")
    p.add_argument("collection")
    p.add_argument("--start", type=date.fromisoformat)
    p.add_argument("--end", type=date.fromisoformat)
    p.add_argument("--brief", default="", help="补充说明（可选）")
    p.add_argument("--audience", help="读者，默认直属上级")
    p.add_argument("--name", help="姓名")
    p.add_argument("-o", "--output", help="周报写入文件（Markdown，去掉出处标记）")

    p = sub.add_parser("schedule", help="每周定时准备周报：设置、查看或关闭（不带参数时查看）")
    p.add_argument("collection")
    p.add_argument("--weekday", help="星期几：1–7（周一为 1）或“周五”")
    p.add_argument("--time", help="时间 HH:MM，例如 16:30")
    p.add_argument("--off", action="store_true", help="关闭定时准备")
    p = sub.add_parser("inbox", help="查看提醒（周报已准备好、准备失败等）")
    p.add_argument("--all", action="store_true", help="包括已读的")
    sub.add_parser("run-due", help="执行到期的每日扫描和每周定时准备（供系统计划任务调用）")
    p = sub.add_parser("prune", help="清理旧版本（保护被证据、周报引用的版本和每个文件的最新版本）")
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
    if cmd == "create":
        c = svc.create_collection(args.name, args.owner)
        owners = f"，署名：{'、'.join(c.owner_names)}" if c.owner_names else "（未设置署名，归属需要你逐条确认）"
        print(f"已创建集合 {c.id}「{c.name}」{owners}")
    elif cmd == "list":
        collections = svc.list_collections()
        if not collections:
            print("（还没有集合，用 briefly track create <名称> 创建）")
        from .weekly_schedule import ScheduleService, describe, notifications

        schedules = ScheduleService(svc)
        for c in collections:
            n_sources, n_files = len(svc.sources(c.id)), len(svc.files(c.id))
            schedule = schedules.get(c.id)
            when = f"，定时准备：{describe(schedule)}" if schedule else ""
            print(f"  {c.id}. {c.name}：{n_sources} 个追踪源，{n_files} 个文件，署名 {('、'.join(c.owner_names)) or '未设置'}{when}")
        unread = notifications(svc.engine)
        if unread:
            print(f"有 {len(unread)} 条未读提醒：briefly track inbox")
    elif cmd == "delete":
        c = svc.collection(args.collection)
        if not args.yes and input(f"删除集合「{c.name}」及其全部版本和候选记录？原文件不受影响。(y/N) ").strip().lower() != "y":
            print("已取消")
            return None
        svc.delete_collection(c.id)
        print(f"已删除集合「{c.name}」")
    elif cmd == "owner":
        c = svc.set_owners(args.collection, args.names)
        print(f"集合「{c.name}」的署名：{'、'.join(c.owner_names)}")
    elif cmd == "add":
        print_add(svc.add_paths(args.collection, args.paths, recursive=not args.no_recursive))
    elif cmd == "sources":
        for x in svc.sources(args.collection, include_removed=args.all):
            kind = "文件夹" if x.kind == "folder" else "文件"
            extra = "" if x.kind == "file" or x.recursive else "（仅第一层）"
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
        counts = svc.snapshot_counts(args.collection)
        for f in svc.files(args.collection):
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
        print_scan(svc.scan(args.collection))
    elif cmd == "scans":
        for r in svc.scans(args.collection):
            print(_scan_line(r))
    elif cmd == "changes":
        since = datetime.now() - timedelta(days=args.days)
        files = {f.id: f for f in svc.files(args.collection)}
        for ch in svc.changes(args.collection, since):
            f = files.get(ch.file_id)
            basis = ch.details.get("basis")
            basis = f"  依据：{'；'.join(basis) if isinstance(basis, list) else basis}" if basis else ""
            print(f"  {_fmt_time(ch.detected_at)} {CHANGE_TEXT.get(ch.change_type, ch.change_type)} 文件 {ch.file_id} {f.path if f else ''}{basis}")
    elif cmd == "diff":
        print_diff(svc, args.file_id, args.from_version, args.to_version)
    else:
        from . import cli_review

        return cli_review.dispatch(args, svc)
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
