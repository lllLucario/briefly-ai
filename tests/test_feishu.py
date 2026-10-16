"""飞书集成：链接识别、客户端（令牌、重试、错误说明）、文档读写、多维表格、导入材料、周报写入。全部用模拟响应。"""
from datetime import date

import pytest

from briefly import config
from briefly.db import create_db_engine
from briefly.feishu.bitable import format_value, read_table
from briefly.feishu.client import FeishuError, FeishuNotConfigured, FeishuClient
from briefly.feishu.docx import MAX_BLOCKS_PER_CALL, blocks_to_markdown, create_document, read_document
from briefly.feishu.links import BITABLE, DOCX, WIKI, find_links, parse_link
from briefly.feishu.service import export_weekly, exports_of, import_link
from briefly.materials import MaterialStore
from briefly.weekly import DONE, WeeklyService
from fakes import FakeFeishu, feishu_error

DOC = "doxcnAbCdEfGhIjKlMnOpQrStUv"  # 27 位
API = "/open-apis"


def text(content, **style):
    run = {"content": content}
    if style:
        run["text_element_style"] = style
    return {"elements": [{"text_run": run}]}


def doc_blocks():
    return [
        {"block_id": DOC, "block_type": 1, "page": text("项目周记"), "children": ["h1", "p1", "b1", "o1", "o2", "t1", "tbl", "img"]},
        {"block_id": "h1", "block_type": 4, "heading2": text("本周进展")},
        {"block_id": "p1", "block_type": 2, "text": {"elements": [
            {"text_run": {"content": "支付回调"}},
            {"text_run": {"content": "降到 200ms", "text_element_style": {"bold": True}}},
            {"text_run": {"content": "文档", "text_element_style": {"link": {"url": "https%3A%2F%2Fexample.com%2Fa"}}}},
        ]}},
        {"block_id": "b1", "block_type": 12, "bullet": text("联调"), "children": ["b2"]},
        {"block_id": "b2", "block_type": 12, "bullet": text("子项")},
        {"block_id": "o1", "block_type": 13, "ordered": text("第一步")},
        {"block_id": "o2", "block_type": 13, "ordered": text("第二步")},
        {"block_id": "t1", "block_type": 17, "todo": {**text("提测"), "style": {"done": True}}},
        {"block_id": "tbl", "block_type": 31, "table": {"cells": ["c1", "c2", "c3", "c4"], "property": {"row_size": 2, "column_size": 2}}, "children": ["c1", "c2", "c3", "c4"]},
        {"block_id": "c1", "block_type": 32, "children": ["x1"]},
        {"block_id": "c2", "block_type": 32, "children": ["x2"]},
        {"block_id": "c3", "block_type": 32, "children": ["x3"]},
        {"block_id": "c4", "block_type": 32, "children": ["x4"]},
        {"block_id": "x1", "block_type": 2, "text": text("事项")},
        {"block_id": "x2", "block_type": 2, "text": text("状态")},
        {"block_id": "x3", "block_type": 2, "text": text("灰度 | 一期")},
        {"block_id": "x4", "block_type": 2, "text": text("完成")},
        {"block_id": "img", "block_type": 27, "image": {"token": "img"}},
    ]  # fmt: skip


@pytest.fixture
def store():
    engine = create_db_engine("sqlite://")
    yield MaterialStore(engine)
    engine.dispose()


# ---- 链接
@pytest.mark.parametrize(
    "url, kind, token, table",
    [
        (f"https://abc.feishu.cn/docx/{DOC}", DOCX, DOC, ""),
        (f"https://abc.feishu.cn/wiki/{DOC}?from=from_copylink", WIKI, DOC, ""),
        ("https://abc.feishu.cn/base/bascnAbCdEfGhIjKl?table=tblXyZ123&view=vewAbc", BITABLE, "bascnAbCdEfGhIjKl", "tblXyZ123"),
        (f"https://abc.larksuite.com/docx/{DOC}", DOCX, DOC, ""),
    ],
)
def test_parse_link(url, kind, token, table):
    link = parse_link(url)
    assert (link.kind, link.token, link.table_id) == (kind, token, table)


@pytest.mark.parametrize(
    "url, message",
    [
        ("https://example.com/docx/abcdefghijkl", "不是飞书链接"),
        ("https://abc.feishu.cn/docs/doccnAbCdEfGhIjKl", "旧版飞书文档"),
        ("https://abc.feishu.cn/sheets/shtcnAbCdEfGhIjKl", "电子表格"),
        ("https://abc.feishu.cn/drive/home", "无法识别"),
    ],
)
def test_unsupported_links_explain_why(url, message):
    with pytest.raises(ValueError, match=message):
        parse_link(url)


def test_find_links_in_chat_text():
    msg = f"帮我总结一下 https://abc.feishu.cn/docx/{DOC}，还有 https://example.com/x 不用管"
    assert find_links(msg) == [f"https://abc.feishu.cn/docx/{DOC}"]


# ---- 客户端
def test_missing_credentials():
    with pytest.raises(FeishuNotConfigured, match="FEISHU_APP_ID"):
        FeishuClient("", "")


def test_token_is_cached_and_refreshed_when_invalid():
    fake = FakeFeishu({("GET", f"{API}/x"): [feishu_error(99991663, "token invalid"), {"ok": 1}, {"ok": 2}]})
    with fake.client() as client:
        assert client.get(f"{API}/x") == {"ok": 1}
        assert client.get(f"{API}/x") == {"ok": 2}
    assert fake.token_calls == 2  # 第一次取令牌 + 失效后刷新一次
    assert fake.requests[-1].headers["Authorization"] == "Bearer t-2"


def test_rate_limit_is_retried_with_backoff():
    waits = []
    fake = FakeFeishu({("GET", f"{API}/x"): [feishu_error(99991400, "rate"), feishu_error(0, "", 429), {"ok": 1}]})
    client = FeishuClient("a", "b", transport=fake.transport, sleep=waits.append)
    assert client.get(f"{API}/x") == {"ok": 1}
    assert 1 <= waits[0] <= 1.25 and 2 <= waits[1] <= 2.5  # 指数退避 + 不超过 25% 的随机抖动
    client.close()


def test_rate_limit_reset_header_is_respected():
    import httpx

    waits = []
    limited = httpx.Response(429, json={"code": 0, "msg": ""}, headers={"x-ogw-ratelimit-reset": "3"})
    fake = FakeFeishu({("GET", f"{API}/x"): [limited, {"ok": 1}]})
    client = FeishuClient("a", "b", transport=fake.transport, sleep=waits.append)
    assert client.get(f"{API}/x") == {"ok": 1} and waits == [3.0]
    client.close()


def test_business_error_with_http_200_is_an_error():
    import httpx

    fake = FakeFeishu({("GET", f"{API}/x"): httpx.Response(200, json={"code": 1770002, "msg": "not found"})})
    with fake.client() as client, pytest.raises(FeishuError, match="文档不存在") as e:
        client.get(f"{API}/x")
    assert (e.value.code, e.value.status) == (1770002, 200)


def test_gateway_error_without_json_is_an_error():
    import httpx

    fake = FakeFeishu({("GET", f"{API}/x"): httpx.Response(502, text="<html>Bad Gateway</html>")})
    with fake.client() as client, pytest.raises(FeishuError, match="HTTP 502") as e:
        client.get(f"{API}/x")
    assert len(fake.requests) == 4  # 读取可重试：1 次 + 重试 3 次
    assert e.value.status == 502


def test_unsafe_write_is_not_retried_on_server_error():
    import httpx

    fake = FakeFeishu({("POST", f"{API}/docx/v1/documents"): httpx.Response(503, text="busy")})
    with fake.client() as client, pytest.raises(FeishuError):
        create_document(client, "周报", "")
    assert len(fake.requests) == 1  # 可能已经建好，不重试，避免重复文档


def test_unsafe_write_is_retried_when_rate_limited():
    fake = FakeFeishu({("POST", f"{API}/docx/v1/documents"): [feishu_error(99991400, "rate"), {"document": {"document_id": DOC}}]})
    with fake.client() as client:
        assert create_document(client, "周报", "") == DOC  # 限频时请求没有执行，重试是安全的
    assert len(fake.requests) == 2


def test_network_error_retried_only_for_safe_requests():
    import httpx

    attempts = []

    def flaky(request):
        attempts.append(request.url.path)
        if len(attempts) == 1:
            raise httpx.ConnectError("reset")
        return {"ok": 1}

    fake = FakeFeishu({("GET", f"{API}/x"): flaky, ("POST", f"{API}/y"): flaky})
    with fake.client() as client:
        assert client.get(f"{API}/x") == {"ok": 1}
        attempts.clear()
        with pytest.raises(FeishuError, match="无法连接飞书"):
            client.post(f"{API}/y", {})
        attempts.clear()
        assert client.post(f"{API}/y", {}, idempotent=True) == {"ok": 1}


def test_writes_carry_dedup_keys():
    from briefly.feishu.messages import send_text

    converted = {"first_level_block_ids": ["a"], "blocks": [{"block_id": "a", "block_type": 2, "text": text("x")}]}
    fake = FakeFeishu({
        ("POST", f"{API}/docx/v1/documents"): {"document": {"document_id": DOC}},
        ("POST", f"{API}/docx/v1/documents/blocks/convert"): converted,
        ("POST", f"{API}/docx/v1/documents/{DOC}/blocks/{DOC}/descendant"): {},
        ("POST", f"{API}/im/v1/messages"): {"message_id": "om_1"},
    })  # fmt: skip
    with fake.client() as client:
        create_document(client, "周报", "正文")
        send_text(client, "ou_me", "提醒", "briefly-notification-7")
    insert = fake.calls("POST", f"{API}/docx/v1/documents/{DOC}/blocks/{DOC}/descendant")[0]
    assert len(insert.url.params["client_token"]) == 36
    assert FakeFeishu.body(fake.calls("POST", f"{API}/im/v1/messages")[0])["uuid"] == "briefly-notification-7"


def test_concurrent_token_refresh_happens_once():
    fake = FakeFeishu()
    with fake.client() as client:
        old = client.token()
        new = client.token(refresh=True, stale=old)
        assert client.token(refresh=True, stale=old) == new  # 另一个线程拿着旧令牌来刷新：直接用新的
    assert fake.token_calls == 2


def test_shared_client_is_reused_and_not_closed_by_with(monkeypatch):
    from briefly.feishu.client import client_from_config, close_shared

    monkeypatch.setattr(config, "FEISHU_APP_ID", "cli_x")
    monkeypatch.setattr(config, "FEISHU_APP_SECRET", "s")
    close_shared()
    first = client_from_config()
    with first:
        pass
    assert client_from_config() is first and not first._http.is_closed  # 令牌缓存、连接池跨请求复用
    monkeypatch.setattr(config, "FEISHU_APP_SECRET", "s2")
    second = client_from_config()
    assert second is not first and first._http.is_closed  # 凭证变了换新实例
    close_shared()
    assert second._http.is_closed


def test_errors_are_explained_in_chinese():
    fake = FakeFeishu({("GET", f"{API}/x"): feishu_error(1770032, "forBidden", 403)})
    with fake.client() as client, pytest.raises(FeishuError, match="添加文档应用") as e:
        client.get(f"{API}/x")
    assert e.value.code == 1770032 and "1770032" in str(e.value)


def test_pagination_merges_pages():
    pages = [{"items": [1, 2], "has_more": True, "page_token": "p2"}, {"items": [3], "has_more": False}]
    fake = FakeFeishu({("GET", f"{API}/list"): pages})
    with fake.client() as client:
        assert client.paginate("GET", f"{API}/list") == [1, 2, 3]
    assert fake.requests[1].url.params["page_token"] == "p2"


# ---- 文档
def test_blocks_to_markdown_keeps_structure():
    title, body, warnings = blocks_to_markdown(doc_blocks())
    assert title == "项目周记"
    assert "## 本周进展" in body
    assert "支付回调**降到 200ms**[文档](https://example.com/a)" in body
    assert "- 联调\n  - 子项" in body
    assert "1. 第一步\n\n2. 第二步" in body
    assert "- [x] 提测" in body
    assert "| 事项 | 状态 |\n| --- | --- |\n| 灰度 \\| 一期 | 完成 |" in body
    assert "[图片]" in body and warnings == ["文档中有 1 处[图片]，只保留了占位说明"]


def test_read_document_pages_through_blocks():
    blocks = doc_blocks()
    fake = FakeFeishu({
        ("GET", f"{API}/docx/v1/documents/{DOC}"): {"document": {"document_id": DOC, "revision_id": 7, "title": "项目周记"}},
        ("GET", f"{API}/docx/v1/documents/{DOC}/blocks"): [
            {"items": blocks[:5], "has_more": True, "page_token": "n"},
            {"items": blocks[5:], "has_more": False},
        ],
    })  # fmt: skip
    with fake.client() as client:
        doc = read_document(client, DOC)
    assert doc.revision_id == 7 and doc.title == "项目周记" and "| 事项 | 状态 |" in doc.markdown


def test_create_document_strips_readonly_fields_and_batches():
    n = MAX_BLOCKS_PER_CALL + 5
    converted = {
        "first_level_block_ids": ["tb"] + [f"p{i}" for i in range(n)],
        "blocks": [{"block_id": "tb", "block_type": 31, "parent_id": "", "children": [],
                    "table": {"property": {"row_size": 1, "column_size": 1, "merge_info": [{"row_span": 1, "col_span": 1}]}}}]
        + [{"block_id": f"p{i}", "block_type": 2, "text": text(str(i))} for i in range(n)],
    }  # fmt: skip
    fake = FakeFeishu({
        ("POST", f"{API}/docx/v1/documents"): {"document": {"document_id": DOC}},
        ("POST", f"{API}/docx/v1/documents/blocks/convert"): converted,
        ("POST", f"{API}/docx/v1/documents/{DOC}/blocks/{DOC}/descendant"): {},
    })  # fmt: skip
    with fake.client() as client:
        assert create_document(client, "周报", "# 标题\n正文", "fldcn1") == DOC
    assert FakeFeishu.body(fake.calls("POST", f"{API}/docx/v1/documents")[0]) == {"title": "周报", "folder_token": "fldcn1"}
    inserts = [FakeFeishu.body(r) for r in fake.calls("POST", f"{API}/docx/v1/documents/{DOC}/blocks/{DOC}/descendant")]
    assert len(inserts) == 2
    assert all(len(b["descendants"]) <= MAX_BLOCKS_PER_CALL for b in inserts)
    assert sum(len(b["children_id"]) for b in inserts) == n + 1
    table = inserts[0]["descendants"][0]
    assert "merge_info" not in table["table"]["property"] and "parent_id" not in table


# ---- 多维表格
@pytest.mark.parametrize(
    "value, ftype, expected",
    [
        ([{"text": "第一行", "type": "text"}, {"text": "接着写", "type": "text"}], 1, "第一行接着写"),
        (3.0, 2, "3"),
        (2.5, 2, "2.5"),
        (["高", "紧急"], 4, "高、紧急"),
        ([{"name": "张三", "id": "ou_1"}, {"name": "李四", "id": "ou_2"}], 11, "张三、李四"),
        (True, 7, "是"),
        ({"text": "设计稿", "link": "https://x.cn"}, 15, "[设计稿](https://x.cn)"),
        ({"type": 1, "value": [{"text": "引用值", "type": "text"}]}, 19, "引用值"),
        ({"link_record_ids": ["r1", "r2"]}, 18, "关联 2 条记录"),
        ({"link_record_ids": None}, 21, ""),  # 实测：没有关联时是 null
        (None, 1, ""),
    ],
)
def test_bitable_values(value, ftype, expected):
    assert format_value(value, ftype) == expected


def test_bitable_date_value():
    from datetime import datetime

    ms = int(datetime(2026, 10, 16).timestamp() * 1000)
    assert format_value(ms, 5) == "2026-10-16"


def bitable_routes(app="bascnAbCdEfGhIjKl"):
    base = f"{API}/bitable/v1/apps/{app}"
    return {
        ("GET", base): {"app": {"name": "项目任务"}},
        ("GET", f"{base}/tables"): {"items": [{"table_id": "tbl1", "name": "任务"}, {"table_id": "tbl2", "name": "风险"}], "has_more": False},
        ("GET", f"{base}/tables/tbl1/fields"): {"items": [{"field_name": "任务", "type": 1}, {"field_name": "负责人", "type": 11}, {"field_name": "完成", "type": 7}], "has_more": False},
        ("POST", f"{base}/tables/tbl1/records/search"): {"items": [
            {"record_id": "r1", "fields": {"任务": [{"text": "支付回调优化", "type": "text"}], "负责人": [{"name": "张三"}], "完成": True}},
            {"record_id": "r2", "fields": {}},
        ], "has_more": False},
        ("GET", f"{base}/tables/tbl2/fields"): {"items": [{"field_name": "风险", "type": 1}], "has_more": False},
        ("POST", f"{base}/tables/tbl2/records/search"): {"items": [], "has_more": False},
    }  # fmt: skip


def test_read_whole_bitable():
    fake = FakeFeishu(bitable_routes())
    with fake.client() as client:
        t = read_table(client, "bascnAbCdEfGhIjKl")
    assert t.title == "项目任务"
    assert "## 任务\n\n| 任务 | 负责人 | 完成 |\n| --- | --- | --- |\n| 支付回调优化 | 张三 | 是 |" in t.markdown
    assert "## 风险\n\n（没有记录）" in t.markdown


def test_read_one_table_with_view():
    fake = FakeFeishu(bitable_routes())
    with fake.client() as client:
        t = read_table(client, "bascnAbCdEfGhIjKl", "tbl1", "vew9")
    assert "## 风险" not in t.markdown
    search = fake.calls("POST", f"{API}/bitable/v1/apps/bascnAbCdEfGhIjKl/tables/tbl1/records/search")[0]
    assert FakeFeishu.body(search) == {"view_id": "vew9"}


# ---- 导入为材料
def doc_routes(token=DOC, blocks=None):
    return {
        ("GET", f"{API}/docx/v1/documents/{token}"): {"document": {"document_id": token, "revision_id": 3, "title": "项目周记"}},
        ("GET", f"{API}/docx/v1/documents/{token}/blocks"): {"items": blocks or doc_blocks(), "has_more": False},
    }


def test_import_doc_link_as_material(store):
    fake = FakeFeishu(doc_routes())
    with fake.client() as client:
        m, created = import_link(client, store, f"https://abc.feishu.cn/docx/{DOC}")
        again, created_again = import_link(client, store, f"https://abc.feishu.cn/docx/{DOC}")
    assert created and not created_again and again.id == m.id
    assert (m.title, m.source, m.file_type) == ("项目周记", "feishu", "feishu")
    assert m.parse_report["feishu"]["revision_id"] == 3
    assert [s.title for s in store.sections(m.id)] == ["本周进展"]


def test_import_wiki_link_resolves_to_document(store):
    wiki = "wikcnAbCdEfGhIjKlMnOp"
    routes = doc_routes() | {("GET", f"{API}/wiki/v2/spaces/get_node"): {"node": {"obj_type": "docx", "obj_token": DOC, "title": "项目周记"}}}
    fake = FakeFeishu(routes)
    with fake.client() as client:
        m, _ = import_link(client, store, f"https://abc.feishu.cn/wiki/{wiki}")
    assert fake.calls("GET", f"{API}/wiki/v2/spaces/get_node")[0].url.params["token"] == wiki
    assert m.parse_report["feishu"]["token"] == DOC


def test_import_wiki_sheet_is_rejected(store):
    fake = FakeFeishu({("GET", f"{API}/wiki/v2/spaces/get_node"): {"node": {"obj_type": "sheet", "obj_token": "x"}}})
    with fake.client() as client, pytest.raises(ValueError, match="电子表格"):
        import_link(client, store, f"https://abc.feishu.cn/wiki/{DOC}")


def test_import_bitable_link(store):
    fake = FakeFeishu(bitable_routes())
    with fake.client() as client:
        m, _ = import_link(client, store, "https://abc.feishu.cn/base/bascnAbCdEfGhIjKl?table=tbl1")
    assert m.file_type == "bitable" and "支付回调优化" in m.content


# ---- 周报写入飞书
def done_report(store) -> int:
    weekly = WeeklyService(store, model_factory=lambda: None)
    r = weekly.create([], "支付回调优化", date(2026, 10, 12), date(2026, 10, 18), "", "王五")
    weekly._update(r.id, status=DONE, content="【本周完成】\n- 支付回调降到 200ms〔1-1〕\n【下周计划】\n- 上线")
    return weekly, r.id


def write_routes(captured):
    def convert(request):
        captured.append(FakeFeishu.body(request)["content"])
        return {"first_level_block_ids": ["a"], "blocks": [{"block_id": "a", "block_type": 2, "text": text("x")}]}

    return {
        ("POST", f"{API}/docx/v1/documents"): {"document": {"document_id": DOC}},
        ("POST", f"{API}/docx/v1/documents/blocks/convert"): convert,
        ("POST", f"{API}/docx/v1/documents/{DOC}/blocks/{DOC}/descendant"): {},
        ("POST", f"{API}/drive/v1/permissions/{DOC}/members"): {"member": {}},
        ("POST", f"{API}/drive/v1/metas/batch_query"): {"metas": [{"doc_token": DOC, "url": f"https://abc.feishu.cn/docx/{DOC}"}]},
    }


def test_export_weekly_creates_doc_and_shares(store, monkeypatch):
    monkeypatch.setattr(config, "FEISHU_NOTIFY_USER", "me@example.com")
    monkeypatch.setattr(config, "FEISHU_FOLDER_TOKEN", "")
    weekly, report_id = done_report(store)
    captured = []
    fake = FakeFeishu(write_routes(captured))
    with fake.client() as client:
        export = export_weekly(client, weekly, report_id)
    assert export.url == f"https://abc.feishu.cn/docx/{DOC}" and export.title == "王五 周报 10.12–10.18"
    assert "〔" not in captured[0]  # 出处标记不写进飞书
    assert captured[0].startswith("## 本周完成\n")
    share = fake.calls("POST", f"{API}/drive/v1/permissions/{DOC}/members")[0]
    assert FakeFeishu.body(share) == {"member_type": "email", "member_id": "me@example.com", "perm": "full_access", "type": "user"}
    assert share.url.params["type"] == "docx"
    assert [e.id for e in exports_of(store.engine, report_id)] == [export.id]


def test_export_weekly_warns_when_nobody_can_see_it(store, monkeypatch):
    monkeypatch.setattr(config, "FEISHU_NOTIFY_USER", "")
    monkeypatch.setattr(config, "FEISHU_FOLDER_TOKEN", "")
    weekly, report_id = done_report(store)
    fake = FakeFeishu(write_routes([]))
    with fake.client() as client:
        export = export_weekly(client, weekly, report_id)
    assert "看不到" in export.share_error and not fake.calls("POST", f"{API}/drive/v1/permissions/{DOC}/members")


def test_export_unfinished_report_is_rejected(store):
    weekly = WeeklyService(store, model_factory=lambda: None)
    r = weekly.create([], "支付回调优化", date(2026, 10, 12), date(2026, 10, 18), "", "")
    with FakeFeishu().client() as client, pytest.raises(ValueError, match="还没有生成完成"):
        export_weekly(client, weekly, r.id)


# ---- 对话中粘贴链接
def test_agent_reads_pasted_link_and_can_use_it_within_selected_scope(store):
    from langchain_core.messages import ToolMessage

    from briefly.agent import Agent
    from fakes import fake, text_msg, tool_msg

    other, _ = store.add_text("别的材料", "无关内容")
    fake_feishu = FakeFeishu(doc_routes())
    model = fake(
        tool_msg("read_feishu_link", {"url": f"https://abc.feishu.cn/docx/{DOC}"}, id="t1"),
        tool_msg("get_outline", {"material_id": other.id + 1}, id="t2"),
        text_msg("已总结"),
    )
    agent = Agent(model, store=store, feishu_factory=fake_feishu.client)
    assert agent.chat(f"总结 https://abc.feishu.cn/docx/{DOC}", material_ids=[other.id]) == "已总结"
    results = [m for m in model.calls[-1] if isinstance(m, ToolMessage)]
    assert "支付回调" in results[0].content and "【材料开始】" in results[0].content
    assert "本周进展" in results[1].content  # 刚读取的材料已纳入本次可访问范围


def test_agent_reports_feishu_errors_to_model(store):
    from langchain_core.messages import ToolMessage

    from briefly.agent import Agent
    from fakes import fake, text_msg, tool_msg

    fake_feishu = FakeFeishu({("GET", f"{API}/docx/v1/documents/{DOC}"): feishu_error(1770032, "forBidden", 403)})
    model = fake(tool_msg("read_feishu_link", {"url": f"https://abc.feishu.cn/docx/{DOC}"}), text_msg("没有权限"))
    agent = Agent(model, store=store, feishu_factory=fake_feishu.client)
    agent.chat("看看这个文档")
    result = [m for m in model.calls[-1] if isinstance(m, ToolMessage)][0]
    assert "添加文档应用" in result.content


def test_agent_has_no_feishu_tool_without_config(store, monkeypatch):
    from briefly.agent import Agent
    from fakes import fake

    monkeypatch.setattr(config, "FEISHU_APP_ID", "")
    agent = Agent(fake(), store=store)
    names = {t.name for t in agent.graph.nodes["tools"].bound.tools_by_name.values()}
    assert "read_feishu_link" not in names and "read_material" in names


# ---- 接口
@pytest.fixture
def api(store, monkeypatch):
    from fastapi.testclient import TestClient

    from briefly.api import create_app

    monkeypatch.setattr(config, "FEISHU_NOTIFY_USER", "ou_me")
    routes = doc_routes() | write_routes([])
    fake_feishu = FakeFeishu(routes)
    app = create_app(store=store, model_factory=lambda: None, feishu_factory=fake_feishu.client)
    with TestClient(app) as client:
        yield client, fake_feishu, app


def test_api_status(api):
    client, _, _ = api
    status = client.get("/feishu/status").json()
    assert (status["configured"], status["notify_user"], status["folder"], status["user"]) == (True, True, False, None)
    assert status["redirect_uri"].endswith("/feishu/oauth/callback")


def test_api_import_link(api):
    client, _, _ = api
    r = client.post("/materials/feishu", json={"url": f"https://abc.feishu.cn/docx/{DOC}"})
    assert r.status_code == 201 and r.json()["material"]["title"] == "项目周记"
    assert r.json()["material"]["source"] == "feishu"
    feishu = r.json()["material"]["feishu"]  # 材料页据此开启追踪
    assert (feishu["kind"], feishu["token"]) == ("docx", DOC) and feishu["url"].endswith(DOC)
    assert client.get("/tracking/feishu-docs").json() == {}
    again = client.post("/materials/feishu", json={"url": f"https://abc.feishu.cn/docx/{DOC}"})
    assert again.status_code == 200 and again.json()["created"] is False


def test_api_import_errors(api):
    client, fake_feishu, _ = api
    assert client.post("/materials/feishu", json={"url": "https://example.com/x"}).status_code == 400
    fake_feishu.routes[("GET", f"{API}/docx/v1/documents/{DOC}")] = feishu_error(1770032, "forBidden", 403)
    r = client.post("/materials/feishu", json={"url": f"https://abc.feishu.cn/docx/{DOC}"})
    assert r.status_code == 502 and "添加文档应用" in r.json()["detail"]


def test_api_not_configured(store, monkeypatch):
    from fastapi.testclient import TestClient

    from briefly.api import create_app

    monkeypatch.setattr(config, "FEISHU_APP_ID", "")
    with TestClient(create_app(store=store, model_factory=lambda: None)) as client:
        assert client.get("/feishu/status").json()["configured"] is False
        r = client.post("/materials/feishu", json={"url": f"https://abc.feishu.cn/docx/{DOC}"})
        assert r.status_code == 400 and "FEISHU_APP_ID" in r.json()["detail"]


def test_api_export_weekly(api, store):
    client, fake_feishu, app = api
    weekly = app.state.weekly
    r = weekly.create([], "支付回调优化", date(2026, 10, 12), date(2026, 10, 18), "", "")
    assert client.post(f"/weekly-reports/{r.id}/feishu").status_code == 400  # 还没生成完
    assert not fake_feishu.calls("POST", f"{API}/docx/v1/documents")
    weekly._update(r.id, status=DONE, content="【本周完成】\n- 完成〔简述〕")
    out = client.post(f"/weekly-reports/{r.id}/feishu")
    assert out.status_code == 201 and out.json()["shared_with"] == "ou_me"
    share = fake_feishu.calls("POST", f"{API}/drive/v1/permissions/{DOC}/members")[0]
    assert FakeFeishu.body(share)["member_type"] == "openid"
    assert [e["url"] for e in client.get(f"/weekly-reports/{r.id}/feishu").json()] == [out.json()["url"]]
    assert client.post("/weekly-reports/999/feishu").status_code == 404


def test_api_track_feishu_link(api, tmp_path, monkeypatch):
    client, _, _ = api
    monkeypatch.setattr(config, "TRACKING_DIR", str(tmp_path / "store"))
    c = client.post("/tracking/collections", json={"name": "项目A"}).json()
    r = client.post(f"/tracking/collections/{c['id']}/sources", json={"paths": [f"https://abc.feishu.cn/docx/{DOC}"]})
    assert r.status_code in (200, 201), r.text
    assert r.json()["added"][0]["kind"] == "feishu"
    files = client.get(f"/tracking/collections/{c['id']}/files").json()
    assert files[0]["path"].endswith("/项目周记")


# ---- 机器人提醒
def test_notification_is_sent_to_feishu(store, monkeypatch):
    import json

    from briefly.feishu.messages import notification_channel
    from briefly.tracking import weekly_schedule

    fake_feishu = FakeFeishu({("POST", f"{API}/im/v1/messages"): {"message_id": "om_1"}})
    channel = notification_channel(fake_feishu.client, user="ou_me")
    monkeypatch.setattr(weekly_schedule, "CHANNELS", [channel])
    n = weekly_schedule.notify(store.engine, "weekly_ready", "「项目A」本周周报已准备好", "3 条候选记录待确认")
    sent = fake_feishu.calls("POST", f"{API}/im/v1/messages")[0]
    assert sent.url.params["receive_id_type"] == "open_id"
    body = FakeFeishu.body(sent)
    assert body["receive_id"] == "ou_me" and body["msg_type"] == "text"
    assert json.loads(body["content"]) == {"text": "「项目A」本周周报已准备好\n3 条候选记录待确认"}
    assert n.id  # 网页、命令行提醒照常保存


def test_failed_feishu_send_does_not_break_notification(store, monkeypatch):
    from briefly.feishu.messages import notification_channel
    from briefly.tracking import weekly_schedule

    fake_feishu = FakeFeishu({("POST", f"{API}/im/v1/messages"): feishu_error(230013, "no permission")})
    monkeypatch.setattr(weekly_schedule, "CHANNELS", [notification_channel(fake_feishu.client, user="me@example.com")])
    n = weekly_schedule.notify(store.engine, "weekly_failed", "准备失败")
    assert n.id and weekly_schedule.notifications(store.engine)[0].title == "准备失败"


def test_channel_registered_once_only_when_configured(monkeypatch):
    from briefly.feishu.messages import register_channel
    from briefly.tracking import weekly_schedule

    monkeypatch.setattr(weekly_schedule, "CHANNELS", [])
    monkeypatch.setattr(config, "FEISHU_NOTIFY_USER", "")
    assert register_channel() is False and weekly_schedule.CHANNELS == []
    monkeypatch.setattr(config, "FEISHU_NOTIFY_USER", "me@example.com")
    monkeypatch.setattr(config, "FEISHU_APP_ID", "cli_x")
    monkeypatch.setattr(config, "FEISHU_APP_SECRET", "s")
    assert register_channel() and register_channel()
    assert len(weekly_schedule.CHANNELS) == 1


def test_export_explains_unknown_collaborator(store, monkeypatch):
    monkeypatch.setattr(config, "FEISHU_NOTIFY_USER", "someone@qq.com")
    weekly, report_id = done_report(store)
    routes = write_routes([]) | {("POST", f"{API}/drive/v1/permissions/{DOC}/members"): feishu_error(1063001, "Invalid parameter")}
    with FakeFeishu(routes).client() as client:
        export = export_weekly(client, weekly, report_id)
    assert export.document_id == DOC and not export.shared_with
    assert "open_id" in export.share_error and "普通邮箱" in export.share_error


def test_bitable_links_show_linked_titles():
    base = f"{API}/bitable/v1/apps/bascnAbCdEfGhIjKl"
    routes = {
        ("GET", base): {"app": {"name": "客户跟进"}},
        ("GET", f"{base}/tables"): {"items": [{"table_id": "tA", "name": "跟进"}, {"table_id": "tB", "name": "问题"}], "has_more": False},
        ("GET", f"{base}/tables/tA/fields"): {"items": [{"field_name": "主题", "type": 1, "is_primary": True}, {"field_name": "问题", "type": 21}], "has_more": False},
        ("POST", f"{base}/tables/tA/records/search"): {"items": [
            {"record_id": "ra", "fields": {"主题": [{"text": "需求澄清", "type": "text"}], "问题": {"link_record_ids": ["rb"]}}},
            {"record_id": "ra2", "fields": {"主题": [{"text": "报价", "type": "text"}], "问题": {"link_record_ids": None}}},
        ], "has_more": False},
        ("GET", f"{base}/tables/tB/fields"): {"items": [{"field_name": "标题", "type": 1, "is_primary": True}, {"field_name": "关联跟进", "type": 21}], "has_more": False},
        ("POST", f"{base}/tables/tB/records/search"): {"items": [
            {"record_id": "rb", "fields": {"标题": "决策链缺失", "关联跟进": {"link_record_ids": ["ra"]}}},
        ], "has_more": False},
    }  # fmt: skip
    with FakeFeishu(routes).client() as client:
        whole = read_table(client, "bascnAbCdEfGhIjKl")
        single = read_table(client, "bascnAbCdEfGhIjKl", "tA")
    assert "| 需求澄清 | 决策链缺失 |" in whole.markdown and "| 决策链缺失 | 需求澄清 |" in whole.markdown
    assert "| 报价 |  |" in whole.markdown
    assert "| 需求澄清 | 关联 1 条记录 |" in single.markdown  # 只读一张表时不知道被关联记录的标题


# ---- 妙记
MINUTE = "obcnq3b9jl72l83w4f149w9c"
TRANSCRIPT = "2026-10-15 10:00:00 CST|1分钟 56秒\n\n说话人 1 00:00:01\n支付回调部分按原计划十月二十日上线。\n\n说话人 2 00:00:15\n李四负责在十月十八日之前确认签名算法。\n"


def minutes_routes(transcript=TRANSCRIPT):
    import httpx

    return {
        ("GET", f"{API}/minutes/v1/minutes/{MINUTE}"): {"minute": {"token": MINUTE, "title": "支付回调周会", "duration": "116000", "create_time": "1791031017000", "url": f"https://abc.feishu.cn/minutes/{MINUTE}"}},
        ("GET", f"{API}/minutes/v1/minutes/{MINUTE}/transcript"): httpx.Response(200, content=transcript.encode("utf-8"), headers={"content-type": "application/octet-stream"}),
    }  # fmt: skip


def test_parse_minutes_link():
    link = parse_link(f"https://abc.feishu.cn/minutes/{MINUTE}?from=from_copylink")
    assert (link.kind, link.token) == ("minutes", MINUTE)


def test_import_minutes_transcript(store):
    fake = FakeFeishu(minutes_routes())
    with fake.client() as client:
        m, created = import_link(client, store, f"https://abc.feishu.cn/minutes/{MINUTE}")
    assert created and (m.title, m.file_type, m.source) == ("支付回调周会", "minutes", "feishu")
    assert m.content.split("\n")[0] == "妙记《支付回调周会》（时长 1 分 56 秒）"  # 不写录制时间，避免和文字稿的北京时间看起来矛盾
    assert "说话人 2 00:00:15\n李四负责" in m.content
    req = fake.calls("GET", f"{API}/minutes/v1/minutes/{MINUTE}/transcript")[0]
    assert (req.url.params["need_speaker"], req.url.params["file_format"]) == ("true", "txt")
    assert m.parse_report["structure"] in ("rules", "none")  # 纯文本，不按 Markdown 解析


def test_minutes_without_transcript_is_rejected(store):
    fake = FakeFeishu(minutes_routes(transcript=""))
    with fake.client() as client, pytest.raises(ValueError, match="转写"):
        import_link(client, store, f"https://abc.feishu.cn/minutes/{MINUTE}")


def test_minutes_permission_error_is_explained(store):
    routes = minutes_routes()
    routes[("GET", f"{API}/minutes/v1/minutes/{MINUTE}/transcript")] = feishu_error(99991672, "scope required: minutes:minutes.transcript:export")
    with FakeFeishu(routes).client() as client, pytest.raises(FeishuError, match="缺少接口权限"):
        import_link(client, store, f"https://abc.feishu.cn/minutes/{MINUTE}")


def test_download_retries_rate_limit_and_refreshes_token():
    import httpx

    fake = FakeFeishu({("GET", f"{API}/f"): [feishu_error(99991663, "token invalid"), feishu_error(99991400, "rate"),
                                              httpx.Response(200, content=b"abc", headers={"content-type": "text/plain"})]})  # fmt: skip
    with fake.client() as client:
        assert client.download(f"{API}/f") == b"abc"
    assert fake.token_calls == 2


def test_zero_quota_is_not_retried():
    import httpx

    waits = []
    zero = httpx.Response(400, json={"code": 99991400, "msg": "request trigger frequency limit"},
                          headers={"x-ogw-ratelimit-limit": "0", "x-ogw-ratelimit-reset": "1"})  # 实测：飞书 OCR 对个人版
    fake = FakeFeishu({("POST", f"{API}/ocr"): zero})
    client = FeishuClient("a", "b", transport=fake.transport, sleep=waits.append)
    with pytest.raises(FeishuError, match="额度为 0"):
        client.post(f"{API}/ocr", {}, idempotent=True)
    assert len(fake.requests) == 1 and waits == []
    client.close()
