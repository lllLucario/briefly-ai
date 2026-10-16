"""端到端走查：像用户一样运行安装好的 briefly 命令和 briefly-api 服务（独立进程、真实模型、真实文件）。

    python tests/eval/tracking_e2e.py [--keep-server]

覆盖：追踪文件（混合添加、基线、扫描、各类变化、Diff、暂停恢复）、每日扫描与每周定时开始（API 后台线程实际触发）、
提醒（API 与 inbox）、交互审核（stdin 输入）、生成周报、幂等、run-due、prune，以及对话、材料、周报接口。
全部数据在临时目录，独立数据库，端口 8765，不碰 data/。结果写到 files/eval/tracking-e2e-<时间>.md。
会调用 .env 中配置的模型，产生少量费用。
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "tests")]
from tracking_util import docx_with_revisions, xlsx_bytes  # noqa: E402

SCRIPTS = ROOT / ".venv" / ("Scripts" if os.name == "nt" else "bin")
PORT = 8765
STEPS: list[dict] = []
SERVER: list[subprocess.Popen] = []


def step(name: str, ok: bool, detail: str = "") -> bool:
    STEPS.append({"name": name, "ok": bool(ok), "detail": detail.strip()})
    print(f"[{'通过' if ok else '失败'}] {name}", flush=True)
    if not ok and detail:
        print("    " + detail.strip()[:800].replace("\n", "\n    "), flush=True)
    return ok


class Env:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.work = tmp / "work"
        self.db = tmp / "app.db"
        self.env = {
            **os.environ,
            "DATABASE_URL": f"sqlite:///{self.db.as_posix()}",
            "BRIEFLY_DB_PATH": str(tmp / "briefly.db"),
            "BRIEFLY_TRACKING_DIR": str(tmp / "store"),
            "BRIEFLY_TRACKING_SETTLE_SECONDS": "0",
            "BRIEFLY_TRACKING_DAILY_TIME": "00:00",  # 服务一启动就到点
            "BRIEFLY_TRACKING_WORKDAYS_ONLY": "0",
            "BRIEFLY_TRACKING_TIMEZONE": "",
            "BRIEFLY_PORT": str(PORT),
            "PYTHONIOENCODING": "utf-8",
        }
        self.t = time.time() - 3 * 3600

    def write(self, rel: str, content) -> Path:
        path = self.work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        self.t += 60
        os.utime(path, (self.t, self.t))
        return path

    def run(self, group: str, *args: str, stdin: str | None = None, timeout: int = 300) -> tuple[int, str]:
        """group：track（追踪文件）或 weekly（周报）。"""
        proc = subprocess.run(
            [str(SCRIPTS / "briefly"), group, *args],
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=self.env,
            timeout=timeout,
            cwd=self.tmp,
        )
        return proc.returncode, proc.stdout + proc.stderr

    def sql(self, query: str, *params):
        with sqlite3.connect(self.db) as conn:
            return conn.execute(query, params).fetchall()


def http(method: str, path: str, body: dict | None = None, timeout: int = 120):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        return resp.status, (json.loads(raw) if raw and resp.headers.get_content_type() == "application/json" else raw.decode("utf-8"))


def wait_for(predicate, seconds: int, interval: float = 2):
    deadline = time.time() + seconds
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-server", action="store_true", help="结束后不关闭 API 服务（便于在浏览器里查看）")
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="briefly-e2e-"))
    e = Env(tmp)
    started = time.monotonic()
    try:
        scenario(e)
    except Exception as exc:  # 走查脚本自身出错：记下来，照样出报告、关服务
        step("走查脚本执行完毕", False, f"{type(exc).__name__}: {exc}")
    finally:
        server = SERVER[0] if SERVER else None
        if server and (not args.keep_server or not all(s["ok"] for s in STEPS)):
            server.terminate()
            server.wait(10)
        write_report(e, round(time.monotonic() - started))
        if server and server.poll() is None:
            print(f"API 服务仍在运行：http://127.0.0.1:{PORT}（进程 {server.pid}），数据目录 {tmp}")


def scenario(e: Env):
    # ---- 1. 准备文件：项目文件夹（md、csv、要改名的、要删除的、只改时间的）+ Word + 两个 Excel
    e.write("项目/需求.md", "# 支付改造需求\n## 背景\n现有支付链路为同步调用，高峰期回调超时。\n## 范围\n改造回调链路。")
    e.write("项目/任务.csv", "编号,任务,负责人,状态\n1,接口联调,张三,进行中\n2,压测,赵琳,未开始\n")
    e.write("项目/会议记录.txt", "10月12日 例会：同步本周计划。")
    e.write("项目/旧草稿.md", "# 旧草稿\n不再需要的内容")
    e.write("项目/说明.md", "# 说明\n内容不会变")
    e.write("项目/node_modules/lib.md", "# 依赖目录，应被排除")
    e.write("方案.docx", docx_with_revisions("现有支付链路为同步调用，回调平均耗时 1.8 秒。", []))
    e.write("进度.xlsx", xlsx_bytes({"进度": [["任务", "负责人", "进度"], ["接口联调", "张三", "50%"], ["压测", "赵琳", "0%"]]}))
    e.write("预算.xlsx", xlsx_bytes({"预算": [["项目", "金额"], ["服务器", 20000]]}))

    # ---- 2. 设置你在文档里的名字、混合添加（立即基线）、重叠路径
    code, out = e.run("weekly", "settings", "--owner", "张三")
    step("设置你在文档里的名字", code == 0 and "张三" in out, out)
    code, out = e.run("track", "add", str(e.work / "项目"), str(e.work / "方案.docx"), str(e.work / "进度.xlsx"), str(e.work / "预算.xlsx"))
    step("混合添加文件夹和文件，立即采集基线", code == 0 and "新版本 8" in out, out)  # 5 个项目文件 + 3 个
    code, out = e.run("track", "add", str(e.work / "项目" / "需求.md"))
    step("重叠路径提示、不重复处理", code == 0 and "已被文件夹追踪源" in out and "新版本 0" in out, out)
    files = e.sql("select path from tracked_files")
    step("依赖目录被排除", not any("node_modules" in p for (p,) in files) and len(files) == 8, "\n".join(p for (p,) in files))

    # ---- 3. 周中修改：Word（张三修订）、Excel、Markdown、CSV、新文件、改名、删除、只改时间
    e.write("方案.docx", docx_with_revisions(
        "现有支付链路为同步调用，回调平均耗时 1.8 秒。",
        [("张三", "新增异步队列：回调先入队再处理，预发环境实测平均耗时降到 200 毫秒。")],
    ))  # fmt: skip
    e.write("进度.xlsx", xlsx_bytes({"进度": [["任务", "负责人", "进度"], ["接口联调", "张三", "100%"], ["回调超时排查", "张三", "60%"], ["压测", "赵琳", "0%"]]}))
    e.write("预算.xlsx", xlsx_bytes({"预算": [["项目", "金额"], ["服务器", 20000], ["压测环境", 8000]]}))  # 李四改的
    e.write("项目/需求.md", "# 支付改造需求\n## 背景\n现有支付链路为同步调用，高峰期回调超时。\n## 范围\n改造回调链路。\n## 验收标准\n回调 P99 耗时低于 500 毫秒。")
    e.write("项目/任务.csv", "编号,任务,负责人,状态\n2,压测,赵琳,未开始\n1,接口联调,张三,已完成\n3,回调排查,张三,进行中\n")
    e.write("项目/周三笔记.md", "# 周三笔记\n与运维确认测试库超时原因：连接池配置过小。")
    (e.work / "项目" / "会议记录.txt").rename(e.work / "项目" / "会议记录-10月12日.txt")
    (e.work / "项目" / "旧草稿.md").unlink()
    e.t += 60
    os.utime(e.work / "项目" / "说明.md", (e.t, e.t))
    code, out = e.run("track", "scan")
    expect = ["新版本 7", "修改 5", "新出现 1", "删除 1", "可能移动 1", "只改了修改时间 1"]
    step("手动扫描识别各类变化", code == 0 and all(x in out for x in expect), out)

    ids = {Path(p).name: i for i, p in e.sql("select id, path from tracked_files")}
    code, out = e.run("track", "files")
    step("文件列表：状态与可能移动", code == 0 and "不在了" in out and "可能由文件" in out, out)
    code, out = e.run("track", "history", str(ids["方案.docx"]))
    step("版本历史", code == 0 and "v1 [基线]" in out and "v2 [增量]" in out, out)
    code, out = e.run("track", "diff", str(ids["方案.docx"]))
    step("Word Diff 只含新增的修订内容", code == 0 and "异步队列" in out and "新增 1" in out, out)
    code, out = e.run("track", "diff", str(ids["进度.xlsx"]))
    step("Excel Diff：单元格修改 + 插入行", code == 0 and "C2：50% → 100%" in out and "回调超时排查" in out and "压测" not in out.split("新增")[-1], out)
    code, out = e.run("track", "diff", str(ids["任务.csv"]))
    step("CSV Diff：按主键，忽略顺序", code == 0 and "按主键列「编号」" in out and "进行中 → 已完成" in out and "新增 1" in out, out)
    code, out = e.run("track", "changes")
    step("变化记录", code == 0 and all(x in out for x in ("修改", "删除", "可能移动/改名", "新出现")), out)
    source_id = e.sql("select id from tracking_sources where path like '%进度.xlsx'")[0][0]
    code1, out1 = e.run("track", "pause", str(source_id))
    code2, out2 = e.run("track", "resume", str(source_id))
    step("暂停与恢复追踪源", code1 == 0 and code2 == 0 and "已暂停" in out1 and "已恢复" in out2, out1 + out2)

    # ---- 4. 设置每周定时（1 分钟后），启动 API：后台线程应先做每日扫描，到点再自动开始（检查更新 + 整理审核清单）
    when = datetime.now() + timedelta(minutes=1)
    weekday = str(when.isoweekday())
    code, out = e.run("weekly", "schedule", "--weekday", weekday, "--time", when.strftime("%H:%M"))
    step("设置每周定时", code == 0 and "下次" in out, out)
    e.write("方案.docx", docx_with_revisions(
        "现有支付链路为同步调用，回调平均耗时 1.8 秒。",
        [("张三", "新增异步队列：回调先入队再处理，预发环境实测平均耗时降到 200 毫秒。"), ("张三", "风控调用改为异步，失败时重试 3 次。")],
    ))  # fmt: skip
    log = open(e.tmp / "api.log", "w", encoding="utf-8")
    server = subprocess.Popen([str(SCRIPTS / "briefly-api")], env=e.env, stdout=log, stderr=subprocess.STDOUT, cwd=e.tmp)
    SERVER.append(server)
    up = wait_for(lambda: _alive(), 30, 0.5)
    step("API 服务启动", bool(up))
    daily = wait_for(lambda: e.sql("select status, stats from scan_runs where trigger='daily' and status != 'running'"), 60)
    step("后台线程执行每日扫描", bool(daily) and daily[0][0] == "done", str(daily))
    run = wait_for(lambda: e.sql("select status, report_id, late_minutes, error from scheduled_runs where status != 'running'"), 200, 3)
    step("到点自动开始：整理审核清单（后台线程）", bool(run) and run[0][0] == "done", str(run) + "\n" + (e.tmp / "api.log").read_text(encoding="utf-8")[-1500:])
    if not run or run[0][0] != "done":
        return server
    report_id = run[0][1]
    status = e.sql("select status from weekly_reports where id=?", report_id)[0][0]
    step("自动开始不在审核前生成正文", status == "none", status)
    _, notes = http("GET", "/notifications")
    step("API 返回提醒", bool(notes) and "待你确认" in notes[0]["title"], json.dumps(notes, ensure_ascii=False))
    code, out = e.run("weekly", "inbox")
    step("命令行查看提醒（看后已读）", code == 0 and "待你确认" in out, out)
    code, out = e.run("weekly", "schedule")
    step("定时设置与执行记录", code == 0 and "当前设置：每" in out and "[完成]" in out, out)

    # ---- 5. 审核：确认自己的、排除预算表（李四）、修改一条、补充会议（stdin 输入，真实 input()）
    evidence = e.sql("select id, title, description, attribution, category, source_refs, flags from work_evidence where report_id=? and review_status='PENDING'", report_id)
    lines = [f"E{i}「{t}」{d}（{a}，{c}，{fl}）" for i, t, d, a, c, _, fl in evidence]
    names = {i: {Path(r["path"]).name for r in json.loads(refs)} for i, *_, refs, _ in evidence}
    budget = [i for i in names if "预算.xlsx" in names[i]]
    word = [i for i in names if "方案.docx" in names[i]]
    changed = {"方案.docx", "进度.xlsx", "预算.xlsx", "需求.md", "任务.csv", "周三笔记.md"}
    covered = set().union(*names.values()) if names else set()
    step("候选记录覆盖各个改动文件（改名、只改时间的文件不算）", changed <= covered and "说明.md" not in covered and not any("会议记录" in n for n in covered), "\n".join(lines))
    attr = {i: a for i, _, _, a, *_ in evidence}
    step("Word 修订归属 ATTRIBUTED，Excel 归属 UNKNOWN", all(attr[i] == "ATTRIBUTED" for i in word) and all(attr[i] == "UNKNOWN" for i in budget), "\n".join(lines))
    leaked = [x for x in lines if "1.8 秒" in x or "高峰期回调超时" in x]
    step("已有内容没有被当成本周工作", not leaked, "\n".join(leaked))
    mine = [i for i, *_ in evidence if i not in budget]
    script = "\n".join(
        ["l", f"s {word[0]}", f"y {' '.join(map(str, mine))}", f"n {budget[0]}", f"e {mine[0]}", "", "（人工修改）补充了验收标准", "",
         "a", "参加支付改造评审会", "评审会确认 10 月 20 日上线", "plan", "h", "done", ""]
    )  # fmt: skip
    code, out = e.run("weekly", "review", stdin=script)
    step("交互审核：确认、排除、修改、补充", code == 0 and "生成周报：briefly weekly generate" in out and "已排除 1 条" in out, out[-3000:])
    reviews = [a for (a,) in e.sql("select action from evidence_reviews where report_id=?", report_id)]
    step("审核记录留痕", all(x in reviews for x in ("prepare", "confirm", "exclude", "edit", "add")), str(reviews))

    # ---- 6. 生成周报
    out_file = e.tmp / "周报.md"
    code, out = e.run("weekly", "generate", "-o", str(out_file), timeout=600)
    text = out_file.read_text(encoding="utf-8") if out_file.exists() else ""
    ok = code == 0 and text and "〔" not in text and "8000" not in text and "压测环境" not in text and "评审会" in text
    step("生成周报：只用已确认记录，导出无出处标记", ok, out)
    _, reports = http("GET", "/weekly")
    detail = http("GET", f"/weekly/{reports[0]['id']}")[1] if reports else {}
    sources = {i["source"] for i in detail.get("items", [])}
    step("周报可在网页接口查看，出处为 E 编号；同一周只有一份", len(reports) == 1 and reports[0]["id"] == report_id and bool(sources) and all(s.startswith("E") for s in sources), json.dumps(sorted(sources)))

    # ---- 7. 幂等、run-due、prune
    code, out = e.run("weekly", "prepare")
    step("再次整理：不重复生成、已审核不被覆盖", code == 0 and "文件变化新记录 0 条" in out and "待办新记录 0 条" in out, out)
    code, out = e.run("weekly", "run-due")
    step("run-due：今天的扫描已完成，定时不重复执行", code == 0 and "每日扫描已完成" in out and "每周定时开始完成" not in out, out)
    code, out = e.run("track", "prune", "--keep-days", "30")
    step("清理旧版本", code == 0 and "清理了" in out, out)

    # ---- 8. 之前的功能：材料、对话、上下文用量、网页
    status, created = http("POST", "/materials", {"title": "周会纪要", "content": "王敏：支付改造上线定在10月20日。李强：下周三前提测。"})
    step("材料接口", status in (200, 201) and created["created"], json.dumps(created, ensure_ascii=False))
    status, chat = http("POST", "/chat", {"text": "这份纪要里定了哪些时间？", "material_ids": [created["material"]["id"]]}, timeout=180)
    step("对话接口（真实模型）", status == 200 and "10" in chat["reply"] and "20" in chat["reply"], chat.get("reply", ""))
    status, ctx = http("GET", f"/chat/sessions/{chat['thread_id']}/context")
    step("上下文用量", status == 200 and ctx["usage"] and ctx["usage"]["estimated_tokens"] > 0, json.dumps(ctx, ensure_ascii=False))
    status, page = http("GET", "/")
    step("网页首页", status == 200 and "Briefly" in page and 'id="inbox"' in page)
    return server


def _alive() -> bool:
    try:
        return http("GET", "/skills", timeout=2)[0] == 200
    except Exception:
        return False


def write_report(e: Env, seconds: int) -> None:
    passed = sum(s["ok"] for s in STEPS)
    lines = [f"# 端到端走查（{datetime.now():%Y-%m-%d %H:%M}）", "", f"通过 {passed}/{len(STEPS)}，耗时 {seconds} 秒，数据目录 {e.tmp}", "", "| 步骤 | 结果 |", "|---|---|"]
    lines += [f"| {s['name']} | {'通过' if s['ok'] else '**失败**'} |" for s in STEPS]
    lines += ["", "## 输出", ""]
    for s in STEPS:
        lines += [f"### {s['name']}", "", "```", s["detail"][:3000] or "（无）", "```", ""]
    out = ROOT / "files" / "eval" / f"tracking-e2e-{datetime.now():%Y%m%d-%H%M%S}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n通过 {passed}/{len(STEPS)}，详细结果：{out}")


if __name__ == "__main__":
    main()
