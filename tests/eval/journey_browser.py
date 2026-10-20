"""完整网页主线走查。默认假模型；显式 --real-model 才使用真实API及费用。

python tests/eval/journey_browser.py --real-model -n 2 --channel msedge
仅用虚构会议材料和隔离数据库，不连接真实飞书、不发送邮件。
"""
import argparse
import json
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langchain_core.callbacks import BaseCallbackHandler  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from browser.flows import run_journey  # noqa: E402
from browser.support import fake_model, live_app  # noqa: E402
from briefly.llm import create_chat_model  # noqa: E402


class CallBudget(BaseCallbackHandler):
    """限制逻辑模型调用数；SDK重试关闭，统计本轮全部模型消息的实际token。"""
    raise_error = True

    def __init__(self, limit):
        self.limit = limit
        self.calls = self.input_tokens = self.output_tokens = 0
        self.lock = threading.Lock()

    def on_chat_model_start(self, serialized, messages, **kwargs):
        with self.lock:
            if self.calls >= self.limit:
                raise RuntimeError("本轮真实模型调用数已到上限，停止额外调用")
            self.calls += 1

    def on_llm_end(self, response, **kwargs):
        for group in response.generations:
            for generation in group:
                usage = getattr(getattr(generation, "message", None), "usage_metadata", None) or {}
                with self.lock:
                    self.input_tokens += usage.get("input_tokens", 0)
                    self.output_tokens += usage.get("output_tokens", 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-model", action="store_true")
    parser.add_argument("-n", type=int, default=1)
    parser.add_argument("--max-calls", type=int, default=12, help="每轮逻辑模型调用上限")
    parser.add_argument("--channel", default=None, help="如msedge；默认使用已安装的Playwright Chromium")
    parser.add_argument("--out", type=Path, default=Path("files/eval/journey-browser"))
    args = parser.parse_args()
    if args.n < 1 or args.max_calls < 1:
        parser.error("运行次数与调用上限必须为正数")
    out = args.out/datetime.now(ZoneInfo("Australia/Sydney")).strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True)
    results = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=args.channel)
        try:
            for index in range(1, args.n+1):
                budget = CallBudget(args.max_calls)
                def factory():
                    if not args.real_model:
                        return fake_model()
                    model = create_chat_model(max_retries=0, timeout=60)
                    model.callbacks = [budget]
                    return model
                started = time.monotonic()
                record = {"run":index,"real_model":args.real_model,"ok":False}
                with live_app(out/f"run-{index}", factory) as url:
                    context = browser.new_context(viewport={"width":1440,"height":1000}, timezone_id="Australia/Sydney")
                    context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(url+"/") else route.abort())
                    page = context.new_page()
                    page.set_default_timeout(90000)
                    errors = []
                    page.on("pageerror", lambda error: errors.append(error.stack or str(error)))
                    try:
                        page.goto(url)
                        record.update(run_journey(page, screenshot=out/f"run-{index}.png"))
                        assert not errors, errors
                        record["ok"] = True
                    except Exception as error:
                        record["error"] = str(error)
                        page.screenshot(path=str(out/f"run-{index}-failed.png"), full_page=True)
                    finally:
                        record.update(calls=budget.calls, input_tokens=budget.input_tokens,
                                      output_tokens=budget.output_tokens, seconds=round(time.monotonic()-started,1),
                                      browser_errors=errors)
                        results.append(record)
                        (out/"results.json").write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding="utf-8")
                        context.close()
                print(f"第{index}轮 {'通过' if record['ok'] else '失败'}，{budget.calls}次模型调用，{record['seconds']}秒",flush=True)
                if not record["ok"]:
                    break  # 失败先诊断，不继续浪费API
        finally:
            browser.close()
    print(f"结果：{out}")
    if not all(r["ok"] for r in results) or len(results)!=args.n:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
