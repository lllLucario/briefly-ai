"""命令行：briefly weekly <子命令>。一周一份周报：整理审核清单（prepare）→ 审核（review）→ 生成（generate）。

周默认为本周，--week 指定那一周里的任意一天（如 --week 2026-10-12）。定时（schedule）到点做的就是 prepare。
"""
import argparse
import sys
from collections.abc import Callable
from datetime import date, datetime, timezone
from pathlib import Path

from ..db import create_db_engine
from ..materials import MaterialStore
from ..summarize import CATEGORY_NAMES
from ..tracking.scanner import ScanBusy
from .engine import strip_source_tags
from .models import CONFIRMED, PENDING, REJECTED
from .review import ReviewService
from .schedule import Schedule, describe, mark_read, notifications, parse_weekday, run_due
from .service import WeeklyService

STATUS = {PENDING: "待确认", CONFIRMED: "已确认", REJECTED: "已排除"}
REPORT_STATUS = {"none": "还没有生成", "pending": "排队中", "running": "生成中", "done": "已生成", "failed": "生成失败"}
RUN_STATUS = {"done": "完成", "failed": "失败", "running": "执行中", "interrupted": "中断"}
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
    "no_owner": "未设置你在文档里的名字",
    "deleted": "文件被删除",
    "extract_failed": "部分变化未能概括",
    "done_downgraded": "变化里没写“完成”，类别不按已完成算",
    "todo": "来自待办",
    "increment": "审核之后的新进展",
}
HELP = """命令（编号写 12 或 E12，多条用空格分隔）：
  l                列出记录            s <编号>         查看来源（文件、版本、位置、原文、变化）
  y <编号...>      确认是你的工作      n <编号...>      排除（不是你的或不写进周报）
  u <编号...>      撤销，回到待确认    e <编号>         修改标题、描述、类别
  a                补充文件和待办体现不了的工作          m <编号...>      合并
  sp <编号>        拆分                r <编号>         重新生成描述（加 ! 强制覆盖你改过的描述）
  h                审核记录            q                退出（随时可以退出，下次继续）"""


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="briefly weekly", description="周报：一周一份，整理审核清单 → 审核 → 生成（追踪文件见 briefly track）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def week(p):
        p.add_argument("--week", type=date.fromisoformat, help="那一周里的任意一天，默认本周")
        return p

    p = week(sub.add_parser("prepare", help="开始：检查更新，把这一周的文件变化和待办进展整理进审核清单"))
    p.add_argument("--no-scan", action="store_true", help="不检查更新（只用已有版本）")
    week(sub.add_parser("review", help="审核清单（交互）"))
    p = week(sub.add_parser("generate", help="确认清单并生成周报（还有待确认的记录时不能生成）"))
    p.add_argument("--audience", help="读者，默认用周报设置里的")
    p.add_argument("--name", help="署名，默认用周报设置里的")
    p.add_argument("-o", "--output", help="周报写入文件（Markdown，去掉出处标记）")
    week(sub.add_parser("show", help="查看这一周：清单概况、本周材料与说明、正文"))
    p = week(sub.add_parser("note", help="本周说明：重点、材料里没有的工作（写空字符串清除）"))
    p.add_argument("text")
    p = week(sub.add_parser("materials", help="本周材料：整份纳入，生成时逐块提取（不带编号表示清空）"))
    p.add_argument("ids", nargs="*", type=int)

    p = sub.add_parser("schedule", help="每周定时：设置、查看或关闭（不带参数时查看）")
    p.add_argument("--weekday", help="星期几：1–7（周一为 1）或“周五”")
    p.add_argument("--time", help="时间 HH:MM，例如 16:30")
    p.add_argument("--off", action="store_true", help="关闭定时")
    p = sub.add_parser("settings", help="默认读者、署名、你在文档里的名字（不带参数时查看）")
    p.add_argument("--audience")
    p.add_argument("--author")
    p.add_argument("--owner", action="append", help="你在文档里的名字（Word 修订作者等），可多次指定")
    p = sub.add_parser("inbox", help="查看提醒（审核清单已整理好、自动开始失败等）")
    p.add_argument("--all", action="store_true", help="包括已读的")
    sub.add_parser("run-due", help="执行到期的每日扫描和每周定时（供系统计划任务调用）")
    return ap


def main(argv: list[str] | None = None, service: WeeklyService | None = None) -> int:
    args = build_parser().parse_args(argv)
    if service is None:
        engine = create_db_engine()
        service = WeeklyService(MaterialStore(engine))
        if args.cmd == "run-due":
            from ..feishu.messages import register_channel

            register_channel()  # 配置了飞书时，提醒同时发到飞书
    try:
        return _dispatch(args, service) or 0
    except (ValueError, ScanBusy) as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1


def _dispatch(args, weekly: WeeklyService) -> int | None:
    cmd = args.cmd
    if cmd == "prepare":
        report, info = weekly.prepare(args.week, scan=not args.no_scan)
        if info.scan:
            from ..tracking.cli import print_scan

            print_scan(info.scan)
        b, t = info.build, info.todos
        print(
            f"周报 {report.period_start}~{report.period_end}：文件变化新记录 {len(b.created)} 条（模型调用 {b.calls} 次），"
            f"待办新记录 {len(t.created)} 条，未变化 {len(b.unchanged) + len(t.unchanged)} 条"
        )
        for note in info.notes:
            print(f"  注意：{note}")
        print_items(weekly.review().items(report.id))
        print("下一步：briefly weekly review" + _week_arg(args))
    elif cmd == "review":
        review_loop(weekly.review(), weekly.week(args.week).id)
    elif cmd == "generate":
        report = weekly.generate(weekly.week(args.week).id, args.audience, args.name)
        if report.status != "done":
            print(f"周报生成失败：{report.error}")
            return 1
        print(f"已生成周报（{report.period_start}~{report.period_end}），出处〔E编号〕对应清单里的记录：\n")
        print(report.content)
        for r in report.generation.get("reminders", []):
            print(f"  提醒：{r}")
        for w in report.generation.get("warnings", []):
            print(f"  注意：{w}")
        if args.output:
            Path(args.output).write_text(strip_source_tags(report.content) + "\n", encoding="utf-8")
            print(f"\n已写入 {args.output}（去掉了出处标记）")
    elif cmd == "show":
        report = weekly.week(args.week, create=False)
        if report is None:
            print("这一周还没有开始。开始：briefly weekly prepare" + _week_arg(args))
            return None
        print(f"周报 {report.period_start}~{report.period_end}：{REPORT_STATUS.get(report.status, report.status)}，清单{'已确认' if report.checklist == 'confirmed' else '审核中'}")
        print(f"本周说明：{report.note or '无'}")
        print(f"本周材料：{'、'.join(str(i) for i in report.material_ids) or '无'}")
        print_items(weekly.review().items(report.id))
        if report.content:
            print("\n" + report.content)
    elif cmd == "note":
        report = weekly.set_note(weekly.week(args.week).id, args.text)
        print(f"本周说明：{report.note or '（已清除）'}")
    elif cmd == "materials":
        report = weekly.set_materials(weekly.week(args.week).id, args.ids)
        print(f"本周材料：{'、'.join(str(i) for i in report.material_ids) or '（已清空）'}")
    elif cmd == "schedule":
        print_schedule(weekly, args)
    elif cmd == "settings":
        if args.audience is not None or args.author is not None or args.owner is not None:
            weekly.update_settings(audience=args.audience, author=args.author, owner_names=args.owner)
        s = weekly.settings()
        print(f"默认读者：{s.audience or '直属上级'}；署名：{s.author or '未设置'}；你在文档里的名字：{'、'.join(s.owner_names) or '未设置'}")
    elif cmd == "inbox":
        items = notifications(weekly.engine, unread_only=not args.all)
        if not items:
            print("（没有未读提醒）" if not args.all else "（没有提醒）")
        for n in reversed(items):
            state = "" if n.read_at is None else "（已读）"
            print(f"[{n.created_at:%m-%d %H:%M}] {n.title}{state}")
            for line in n.body.splitlines():
                print(f"    {line}")
        mark_read(weekly.engine, [n.id for n in items if n.read_at is None])
    elif cmd == "run-due":
        for line in run_due(weekly):
            print(line)
    return None


def print_schedule(weekly: WeeklyService, args) -> None:
    from ..tracking.scheduler import tzinfo
    from .schedule import next_time

    schedule = Schedule(weekly)
    if args.off:
        schedule.disable()
        print("已关闭每周定时")
        return
    if args.weekday or args.time:
        if not (args.weekday and args.time):
            raise ValueError("请同时指定 --weekday 和 --time，例如 --weekday 周五 --time 16:30")
        settings = schedule.set(parse_weekday(args.weekday), args.time)
        print(f"每周定时：{describe(settings)}（时区 {tzinfo()}）")
        print("到点会自动检查更新、整理审核清单，然后提醒你审核；需要 briefly-api 在运行，或用系统计划任务定时执行 briefly weekly run-due")
    settings = weekly.settings()
    if not settings.schedule_enabled:
        print("还没有开启每周定时。设置：briefly weekly schedule --weekday 周五 --time 16:30")
    else:
        upcoming = next_time(settings, datetime.now(timezone.utc))
        print(f"当前设置：{describe(settings)}，下次 {upcoming:%Y-%m-%d %H:%M}")
    for r in schedule.runs():
        late = f"，晚 {r.late_minutes} 分钟补做" if r.late_minutes else ""
        error = f"：{r.error[:80]}" if r.error else ""
        print(f"  {r.occurrence:%m-%d %H:%M} 第 {r.attempt} 次 [{RUN_STATUS.get(r.status, r.status)}]{late}{error}")


def _week_arg(args) -> str:
    return f" --week {args.week}" if getattr(args, "week", None) else ""


def format_item(e) -> str:
    files = "、".join(dict.fromkeys(r["path"].replace("\\", "/").rsplit("/", 1)[-1] for r in e.source_refs if r.get("path")))
    versions = "，".join(f"v{r.get('from_version') or '-'}→v{r.get('to_version') or '-'}" for r in e.source_refs if r.get("to_version"))
    flags = "、".join(FLAG[f] for f in e.flags if f in FLAG)
    source = f"{files} {versions}" if files else {"todo": "你的待办记录", "manual": "你补充的"}.get(e.origin, "你补充的")
    lines = [
        f"E{e.id} [{STATUS.get(e.review_status, e.review_status)}] {e.title}"
        + ("（你改过）" if e.user_edited and e.origin != "manual" else "")
        + ("（有新变化，见新的待确认项）" if e.has_update else ""),
        f"    {e.description}",
        f"    类别：{CATEGORY_NAMES.get(e.category, e.category)}  归属：{ATTR.get(e.attribution, e.attribution)}  来源：{source}"
        + (f"  标记：{flags}" if flags else ""),
    ]
    if e.attribution_basis and e.origin not in ("manual", "todo"):
        lines.append(f"    依据：{e.attribution_basis[0]}" + (f" 等 {len(e.attribution_basis)} 条" if len(e.attribution_basis) > 1 else ""))
    return "\n".join(lines)


def print_items(items, out: Callable[[str], None] = print) -> None:
    if not items:
        out("（清单里还没有记录）")
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


def review_loop(review: ReviewService, report_id: int, ask: Callable[[str], str] = input, out: Callable[[str], None] = print) -> None:
    """交互审核。随时可以退出，审核结果已保存；全部确认或排除后用 generate 生成周报。"""
    out("审核这一周的候选工作记录。只有你确认的记录会写进周报。")
    out(HELP)
    print_items(review.items(report_id), out)
    while True:
        try:
            line = ask("审核> ").strip()
        except (EOFError, KeyboardInterrupt):
            out("\n已保存，下次运行 review 继续")
            return
        if not line:
            continue
        cmd, *rest = line.split()
        cmd = cmd.lower()
        try:
            if cmd in ("q", "quit", "exit", "done"):
                pending = sum(1 for e in review.items(report_id) if e.review_status == PENDING)
                out(f"已保存。还有 {pending} 条待确认" if pending else "已保存。生成周报：briefly weekly generate")
                return
            if cmd in ("l", "list"):
                print_items(review.items(report_id), out)
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
                review.confirm(report_id, _ids(rest))
                out(f"已确认 {len(rest)} 条")
            elif cmd in ("n", "no", "exclude"):
                review.exclude(report_id, _ids(rest))
                out(f"已排除 {len(rest)} 条")
            elif cmd in ("u", "undo"):
                review.reset(report_id, _ids(rest))
                out("已改回待确认")
            elif cmd in ("e", "edit"):
                e = review.get(_ids(rest)[0], report_id)
                out(f"当前标题：{e.title}\n当前描述：{e.description}（直接回车表示不改）")
                title = ask("新标题> ")
                description = ask("新描述> ")
                category = ask(f"类别（{'/'.join(CATEGORY_NAMES)}，回车不改）> ").strip() or None
                review.edit(report_id, e.id, title, description, category)
                out("已修改（还需要确认）")
            elif cmd in ("a", "add"):
                title = ask("标题> ")
                description = ask("描述> ")
                category = ask(f"类别（{'/'.join(CATEGORY_NAMES)}，默认 other）> ").strip() or "other"
                e = review.add(report_id, title, description, category)
                out(f"已补充 E{e.id}（你补充的记录直接算确认）")
            elif cmd in ("m", "merge"):
                title = ask("合并后的标题（回车用第一条的）> ").strip() or None
                description = ask("合并后的描述（回车把各条描述拼起来）> ").strip() or None
                e = review.merge(report_id, _ids(rest), title, description)
                out(f"已合并为 E{e.id}（待确认）")
            elif cmd in ("sp", "split"):
                parts = []
                out("逐条输入拆分后的记录，标题留空结束")
                while True:
                    title = ask(f"第 {len(parts) + 1} 条标题> ").strip()
                    if not title:
                        break
                    parts.append((title, ask("描述> ")))
                news = review.split(report_id, _ids(rest)[0], parts)
                out(f"已拆分为 {', '.join(f'E{n.id}' for n in news)}（待确认）")
            elif cmd in ("r", "regen", "r!"):
                force = cmd == "r!" or (rest and rest[-1] == "!")
                ids = _ids([x for x in rest if x != "!"])
                e = review.regenerate(report_id, ids[0], force=force)
                out(format_item(e))
            elif cmd in ("h", "history"):
                for r in review.history(report_id):
                    target = f"E{r.evidence_id} " if r.evidence_id else ""
                    out(f"  {r.created_at:%m-%d %H:%M} {r.action} {target}{r.note}")
            else:
                out("不认识的命令，输入 ? 查看帮助")
        except ValueError as e:
            out(f"错误：{e}")


if __name__ == "__main__":
    sys.exit(main())
