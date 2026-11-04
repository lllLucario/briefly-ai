"""W4-02 闭环：一次人为触发的失败，页面看到追踪号，凭它在日志里查到记录和堆栈。"""
import io
import json
import logging
import re

import pytest

from briefly.logs import JsonFormatter

pytestmark = pytest.mark.browser


def test_failed_request_can_be_traced_from_page_to_log(web_page):
    from playwright.sync_api import expect

    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("briefly")
    logger.addHandler(handler)
    try:
        page = web_page
        page.locator('[data-tab="chat"]').click()
        page.locator("#input").fill("模拟失败")
        page.locator("#send").click()
        message = page.locator(".msg.assistant").last
        expect(message).to_contain_text("追踪号")
        request_id = re.search(r"追踪号：([0-9a-f]{16})", message.inner_text())[1]
    finally:
        logger.removeHandler(handler)
    records = [json.loads(line) for line in buffer.getvalue().splitlines()]
    matched = [r for r in records if r["request_id"] == request_id]
    assert any("exception" in r and "模拟服务暂不可用" in r["exception"] for r in matched)
    assert not any("模拟失败" in json.dumps(r, ensure_ascii=False) for r in matched)  # 问题原文不进日志
