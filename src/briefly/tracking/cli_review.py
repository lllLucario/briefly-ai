"""命令行：周报准备（prepare）、交互审核（review）、生成周报（report），以及每日扫描（run-due）和清理（prune）。"""
from collections.abc import Callable
from pathlib import Path

from ..summarize import CATEGORY_NAMES
from ..weekly import WeeklyService, strip_source_tags
from .models import CONFIRMED, PENDING, REJECTED
from .pipeline import generate_report, get_prep, prepare
from .review import ReviewService

STATUS = {PENDING: "待确认", CONFIRMED: "已确认", REJECTED: "已排除"}
ATTR = {
    "ATTRIBUTED": "有作者记录",
    "USER_CONFIRMED": "你已确认",
    "INFERRED": "推断（待确认）",
    "UNKNOWN": "无法判断",
    "EXCLUDED": "已排除",
}
FLAG = {
    "first_seen": "本周新发现的文件",
    "possibly_moved": "可能移动/改名",
    "reverted": "中间有过增删",
    "gap": "扫描中断过",
    "parse_failed": "解析失败",
    "format_only": "只改了格式",
    "pdf_text": "PDF 可能有提取误差",
    "layout_noise": "忽略了排版变化",
    "truncated": "变化过多已截断",
    "others": "有他人的修订",
    "mixed": "你和他人都有修订",
    "no_owner": "未设置署名",
    "deleted": "文件被删除",
    "extract_failed": "部分变化未能概括",
    "done_downgraded": "变化里没写“完成”，类别不按已完成算",
}
HELP = """命令（编号写 12 或 E12，多条用空格分隔）：
  l                列出记录            s <编号>         查看来源（文件、版本、位置、原文、变化）
  y <编号...>      确认是你的工作      n <编号...>      排除（不是你的或不写进周报）
  u <编号...>      撤销，回到待确认    e <编号>         修改标题、描述、类别
  a                补充文件没体现的工作                  m <编号...>      合并
  sp <编号>        拆分                r <编号>         重新生成描述（加 ! 强制覆盖你改过的描述）
  h                审核记录            done             确认清单
  q                退出（草稿保留，下次继续）"""


def dispatch(args, svc) -> int | None:
    if args.cmd == "prepare":
        prep, info = prepare(svc, args.collection, args.start, args.end, scan=not args.no_scan)
        if info.scan:
            from .cli import print_scan

            print_scan(info.scan)
        b = info.build
        print(
            f"周报准备 {prep.period_start}~{prep.period_end}：新候选 {len(b.created)} 条，未变化 {len(b.unchanged)} 条，"
            f"替换未审核的 {len(b.superseded)} 条，已审核但有新变化 {len(b.updated_reviewed)} 条（模型调用 {b.calls} 次）"
        )
        for note in info.notes:
            print(f"  注意：{note}")
        print_items(ReviewService(svc).items(prep.id))
        print("下一步：briefly track review " + args.collection + _period_args(args))
    elif args.cmd == "review":
        prep = get_prep(svc, args.collection, args.start, args.end, create=False)
        review_loop(ReviewService(svc), prep.id)
    elif args.cmd == "report":
        prep = get_prep(svc, args.collection, args.start, args.end, create=False)
        weekly = WeeklyService(svc.store)
        report = generate_report(svc, weekly, prep.id, args.brief, args.audience, args.name)
        if report.status != "done":
            print(f"周报生成失败：{report.error}")
            return 1
        print(f"已生成周报草稿 {report.id}（{report.period_start}~{report.period_end}），出处〔E编号〕对应工作记录：\n")
        print(report.content)
        for r in report.generation.get("reminders", []):
            print(f"  提醒：{r}")
        for w in report.generation.get("warnings", []):
            print(f"  注意：{w}")
        if args.output:
            Path(args.output).write_text(strip_source_tags(report.content) + "\n", encoding="utf-8")
            print(f"\n已写入 {args.output}（去掉了出处标记）")
    elif args.cmd == "schedule":
        print_schedule(svc, args)
    elif args.cmd == "inbox":
        from .weekly_schedule import mark_read, notifications

        items = notifications(svc.engine, unread_only=not args.all)
        if not items:
            print("（没有未读提醒）" if not args.all else "（没有提醒）")
        for n in reversed(items):
            state = "" if n.read_at is None else "（已读）"
            print(f"[{n.created_at:%m-%d %H:%M}] {n.title}{state}")
            for line in n.body.splitlines():
                print(f"    {line}")
        mark_read(svc.engine, [n.id for n in items if n.read_at is None])
    elif args.cmd == "run-due":
        from .scheduler import run_due

        for line in run_due(svc):
            print(line)
    elif args.cmd == "prune":
        from .storage import prune

        report = prune(svc.engine, svc.blobs, keep_days=args.keep_days)
        print(f"清理了 {report['snapshots']} 个旧版本、{report['blobs']} 个不再引用的文件，保护了 {report['protected']} 个仍被引用的版本")
    return None


RUN_STATUS = {"done": "完成", "failed": "失败", "running": "执行中", "interrupted": "中断"}


def print_schedule(svc, args) -> None:
    from datetime import datetime, timezone

    from .scheduler import tzinfo
    from .weekly_schedule import ScheduleService, describe, parse_weekday

    schedules = ScheduleService(svc)
    collection = svc.collection(args.collection)
    if args.off:
        schedules.disable(collection.id)
        print(f"「{collection.name}」已关闭每周定时准备")
        return
    if args.weekday or args.time:
        if not (args.weekday and args.time):
            raise ValueError("请同时指定 --weekday 和 --time，例如 --weekday 周五 --time 16:30")
        schedule = schedules.set(collection.id, parse_weekday(args.weekday), args.time)
        print(f"「{collection.name}」每周定时准备：{describe(schedule)}（时区 {tzinfo()}）")
        print("到点会自动最终扫描并生成候选清单，然后提醒你审核；需要 briefly-api 在运行，或用系统计划任务定时执行 briefly track run-due")
    schedule = schedules.get(collection.id)
    if schedule is None:
        print(f"「{collection.name}」还没有设置每周定时准备。设置：briefly track schedule {collection.name} --weekday 周五 --time 16:30")
        return
    if schedule.enabled:
        nxt = schedules.next_time(schedule, datetime.now(timezone.utc).astimezone(tzinfo()))
        print(f"当前设置：{describe(schedule)}，下次 {nxt:%Y-%m-%d %H:%M}")
    else:
        print(f"当前设置：{describe(schedule)}")
    for r in schedules.runs(collection.id):
        late = f"，晚 {r.late_minutes} 分钟补做" if r.late_minutes else ""
        error = f"：{r.error[:80]}" if r.error else ""
        print(f"  {r.occurrence:%m-%d %H:%M} 第 {r.attempt} 次 [{RUN_STATUS.get(r.status, r.status)}]{late}{error}")


def _period_args(args) -> str:
    parts = []
    if getattr(args, "start", None):
        parts.append(f" --start {args.start}")
    if getattr(args, "end", None):
        parts.append(f" --end {args.end}")
    return "".join(parts)


def format_item(e) -> str:
    files = "、".join(dict.fromkeys(r["path"].replace("\\", "/").rsplit("/", 1)[-1] for r in e.source_refs if r.get("path")))
    versions = "，".join(f"v{r.get('from_version') or '-'}→v{r.get('to_version') or '-'}" for r in e.source_refs if r.get("to_version"))
    flags = "、".join(FLAG[f] for f in e.flags if f in FLAG)
    lines = [
        f"E{e.id} [{STATUS.get(e.review_status, e.review_status)}] {e.title}"
        + ("（你改过）" if e.user_edited and e.origin != "manual" else "")
        + ("（有新变化，见新的待确认项）" if e.has_update else ""),
        f"    {e.description}",
        f"    类别：{CATEGORY_NAMES.get(e.category, e.category)}  归属：{ATTR.get(e.attribution, e.attribution)}"
        + (f"  来源：{files} {versions}" if files else "  来源：你补充的")
        + (f"  标记：{flags}" if flags else ""),
    ]
    if e.attribution_basis and e.origin != "manual":
        lines.append(f"    依据：{e.attribution_basis[0]}" + (f" 等 {len(e.attribution_basis)} 条" if len(e.attribution_basis) > 1 else ""))
    return "\n".join(lines)


def print_items(items, out: Callable[[str], None] = print) -> None:
    if not items:
        out("（没有候选记录）")
        return
    counts = {s: sum(1 for e in items if e.review_status == s) for s in STATUS}
    out(f"共 {len(items)} 条：待确认 {counts[PENDING]}，已确认 {counts[CONFIRMED]}，已排除 {counts[REJECTED]}")
    for e in items:
        out(format_item(e))


def _ids(args: list[str]) -> list[int]:
    try:
        return [int(a.upper().removeprefix("E")) for a in args]
    except ValueError:
        raise ValueError("编号格式不对，例如 12 或 E12") from None


def review_loop(review: ReviewService, prep_id: int, ask: Callable[[str], str] = input, out: Callable[[str], None] = print) -> str:
    """交互审核。返回退出方式：confirmed（清单已确认）/ draft（保存草稿退出）。"""
    prep = review.prep(prep_id)
    out(f"审核候选工作记录（{prep.period_start}~{prep.period_end}）。只有你确认的记录会写进周报。")
    out(HELP)
    print_items(review.items(prep_id), out)
    while True:
        try:
            line = ask("审核> ").strip()
        except (EOFError, KeyboardInterrupt):
            out("\n已保存草稿，下次运行 review 继续")
            return "draft"
        if not line:
            continue
        cmd, *rest = line.split()
        cmd = cmd.lower()
        try:
            if cmd in ("q", "quit", "exit"):
                out("已保存草稿，下次运行 review 继续")
                return "draft"
            if cmd in ("l", "list"):
                print_items(review.items(prep_id), out)
            elif cmd in ("?", "help"):
                out(HELP)
            elif cmd in ("s", "show"):
                for view in review.sources(_ids(rest)[0]):
                    out(f"  文件：{view.path}  版本 {view.versions}  采集 {view.captured}")
                    for note in view.notes:
                        out(f"    说明：{note}")
                    if view.locations:
                        out(f"    位置：{'；'.join(view.locations[:8])}")
                    for q in view.quotes[:8]:
                        out(f"    要点：{q['point']}\n      原文：{q['quote'][:200]}")
                    for h in view.hunks:
                        out("    " + h.replace("\n", "\n    "))
            elif cmd in ("y", "ok", "confirm"):
                review.confirm(prep_id, _ids(rest))
                out(f"已确认 {len(rest)} 条")
            elif cmd in ("n", "no", "exclude"):
                review.exclude(prep_id, _ids(rest))
                out(f"已排除 {len(rest)} 条")
            elif cmd in ("u", "undo"):
                review.reset(prep_id, _ids(rest))
                out("已改回待确认")
            elif cmd in ("e", "edit"):
                e = review.get(_ids(rest)[0], prep_id)
                out(f"当前标题：{e.title}\n当前描述：{e.description}（直接回车表示不改）")
                title = ask("新标题> ")
                description = ask("新描述> ")
                category = ask(f"类别（{'/'.join(CATEGORY_NAMES)}，回车不改）> ").strip() or None
                review.edit(prep_id, e.id, title, description, category)
                out("已修改（还需要确认）")
            elif cmd in ("a", "add"):
                title = ask("标题> ")
                description = ask("描述> ")
                category = ask(f"类别（{'/'.join(CATEGORY_NAMES)}，默认 other）> ").strip() or "other"
                e = review.add(prep_id, title, description, category)
                out(f"已补充 E{e.id}（你补充的记录直接算确认）")
            elif cmd in ("m", "merge"):
                title = ask("合并后的标题（回车用第一条的）> ").strip() or None
                description = ask("合并后的描述（回车把各条描述拼起来）> ").strip() or None
                e = review.merge(prep_id, _ids(rest), title, description)
                out(f"已合并为 E{e.id}（待确认）")
            elif cmd in ("sp", "split"):
                parts = []
                out("逐条输入拆分后的记录，标题留空结束")
                while True:
                    title = ask(f"第 {len(parts) + 1} 条标题> ").strip()
                    if not title:
                        break
                    parts.append((title, ask("描述> ")))
                news = review.split(prep_id, _ids(rest)[0], parts)
                out(f"已拆分为 {', '.join(f'E{n.id}' for n in news)}（待确认）")
            elif cmd in ("r", "regen", "r!"):
                force = cmd == "r!" or (rest and rest[-1] == "!")
                ids = _ids([x for x in rest if x != "!"])
                e = review.regenerate(prep_id, ids[0], force=force)
                out(format_item(e))
            elif cmd in ("h", "history"):
                for r in review.history(prep_id):
                    target = f"E{r.evidence_id} " if r.evidence_id else ""
                    out(f"  {r.created_at:%m-%d %H:%M} {r.action} {target}{r.note}")
            elif cmd == "done":
                review.confirm_checklist(prep_id)
                out("清单已确认。生成周报：briefly track report <集合>")
                return "confirmed"
            else:
                out("不认识的命令，输入 ? 查看帮助")
        except ValueError as e:
            out(f"错误：{e}")
