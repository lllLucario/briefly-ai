"""上下文管理：Token 计数与预算、Context Builder、工具预算（含并行）、会话压缩、输入预检、文件处理、观测。

全部用假模型，不调用真实接口。
"""
import re
import threading
import time
from io import BytesIO

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver

from briefly import cli, config
from briefly.agent import INPUT_TOO_LONG, Agent
from briefly.compaction import MemoryItem, MemoryUpdate, SessionMemory, apply_update, render_memory
from briefly.context_builder import CONTEXT_OVERFLOW, input_limit
from briefly.context_store import ContextStore
from briefly.db import create_db_engine
from briefly.materials import MaterialStore
from briefly.skills import read_prompt
from briefly.summarize import Source, extract_items, synthesize, WorkItem
from briefly.tokens import IMAGE_TOKENS, TokenCounter
from fakes import FunctionModel, structured

OVERFLOW_ERROR = "This model's maximum context length is 65536 tokens. However, you requested 70000 tokens"


@pytest.fixture
def engine():
    engine = create_db_engine("sqlite://")
    yield engine
    engine.dispose()


@pytest.fixture
def store(engine):
    return MaterialStore(engine)


def is_compaction(messages) -> bool:
    return "【当前记忆】" in messages[-1].content


def is_extraction(messages) -> bool:
    return messages[0].content == read_prompt("extract")


def chat_calls(model: FunctionModel) -> list[list]:
    return [m for m in model.calls if not is_compaction(m) and not is_extraction(m)]


def tool_results(messages) -> list[ToolMessage]:
    return [m for m in messages if isinstance(m, ToolMessage)]


def answering(text: str = "好的"):
    def respond(messages):
        return AIMessage(text)

    return respond


# ---- Token 计数


def test_counter_picks_backend_by_model_not_one_tokenizer_for_all(monkeypatch):
    import briefly.tokens as tokens

    monkeypatch.setattr(tokens, "_tiktoken_counter", lambda model: (lambda text: len(text)))
    assert TokenCounter("deepseek-flash").backend == "heuristic"
    assert TokenCounter("gpt-4o").backend == "tiktoken"
    assert TokenCounter("gpt-4o", backend="heuristic").backend == "heuristic"
    assert TokenCounter("gpt-4o").count_text("abcd") == 4


def test_counter_counts_tool_calls_tools_and_images():
    c = TokenCounter("x", backend="heuristic")
    plain = c.count_message(AIMessage("好"))
    with_call = c.count_message(AIMessage("好", tool_calls=[{"name": "read_material", "args": {"chunk": 3}, "id": "1"}]))
    assert with_call > plain
    image = HumanMessage([{"type": "text", "text": "看图"}, {"type": "image_url", "image_url": {"url": "x"}}])
    assert c.count_message(image) >= IMAGE_TOKENS  # 文本 tokenizer 量不了图片，按固定值保守计

    @tool
    def big_tool(x: int) -> str:
        """工具说明。"""
        return ""

    small = c.count_tools([big_tool])
    big_tool.description = "说明" * 500
    c2 = TokenCounter("x", backend="heuristic")
    assert c2.count_tools([big_tool]) > small + 500


def test_calibration_only_scales_up_and_is_capped(monkeypatch):
    monkeypatch.setattr(config, "TOKEN_SCALE_MAX", 2.0)
    c = TokenCounter("x", backend="heuristic")
    c.calibrate(1000, 500)  # 估算偏高：保持 1（偏保守是安全的）
    assert c.scale == 1.0
    c.calibrate(100, 900)  # 样本太小，忽略
    assert c.scale == 1.0
    for _ in range(20):
        c.calibrate(1000, 5000)  # 估算严重偏低：放大，但不超过上限
    assert c.scale == 2.0
    assert c.budgeted(100) == 200


def test_input_limit_follows_model_window(monkeypatch):
    monkeypatch.setattr(config, "MAX_INPUT_TOKENS", 80000)
    monkeypatch.setattr(config, "CONTEXT_LIMIT", 0)
    assert input_limit() == 80000
    monkeypatch.setattr(config, "CONTEXT_LIMIT", 32000)  # 换成小窗口的模型
    monkeypatch.setattr(config, "RESERVED_OUTPUT_TOKENS", 8000)
    monkeypatch.setattr(config, "SAFETY_MARGIN_TOKENS", 4000)
    assert input_limit() == 20000


# ---- Context Builder 与预算


def fixed_tokens(agent: Agent, messages) -> int:
    """系统提示词 + 工具定义（按第一次调用实际发出的内容算）。"""
    system = messages[0].content
    return agent.counter.count_text(system) + agent.builder.tool_tokens(agent_tools(agent))


def agent_tools(agent: Agent):
    return list(agent.graph.nodes["tools"].bound.tools_by_name.values())


def test_request_over_budget_is_not_sent(engine, monkeypatch):
    model = FunctionModel(respond=answering())
    agent = Agent(model, tools=[], context_store=ContextStore(engine))
    monkeypatch.setattr(config, "MAX_INPUT_TOKENS", 3000)  # 放不下系统提示词 + 工具定义 + 当前消息
    reply = agent.chat("请处理：" + "内容" * 200)
    assert "没有发送给模型" in reply and "上限 3000" in reply
    assert model.calls == []  # 发送前就拦下，不等接口报错
    row = agent.context_store.usage(thread_id=agent.thread_id)[0]
    assert row.context_overflow and row.note == "发送前超出预算"


def test_large_tool_definitions_push_out_history_not_current_turn(engine, monkeypatch):
    @tool
    def huge(x: int) -> str:
        """工具说明。"""
        return ""

    huge.description = "非常长的工具说明。" * 400  # 约 2100 tokens
    model = FunctionModel(respond=answering())
    agent = Agent(model, tools=[huge], context_store=ContextStore(engine))
    for i in range(5):
        agent.chat(f"第{i}轮的问题" * 20)
    first = model.calls[0]
    limit = fixed_tokens(agent, first) + agent.counter.count_messages(first[1:]) * 3  # 只够当前轮再加一两轮
    monkeypatch.setattr(config, "MAX_INPUT_TOKENS", limit)
    agent.chat("最新的问题" * 20)
    last = model.calls[-1]
    assert last[-1].content == "最新的问题" * 20  # 当前轮完整
    assert len(last) < 1 + 2 * 5 + 1  # 历史被挤掉了一部分
    assert "没有放进上下文" in last[0].content  # 并说明
    assert agent.counter.budgeted(agent.counter.count_messages(last) + agent.builder.tool_tokens(agent_tools(agent))) <= limit


def test_history_and_materials_fit_alone_but_not_together(store, monkeypatch):
    material, _ = store.add_text("纪要", "会议决定下周上线支付改造。" * 120)  # 约 840 tokens，小于直接附全文的阈值
    model = FunctionModel(respond=answering())
    agent = Agent(model, tools=[], store=store)
    for i in range(6):
        agent.chat(f"第{i}轮：普通的问题，" * 15)
    base = fixed_tokens(agent, model.calls[0])
    monkeypatch.setattr(config, "MAX_INPUT_TOKENS", base + 1500)  # 材料能放下，历史能放下，加在一起放不下
    agent.chat("总结这份纪要", material_ids=[material.id])
    last = model.calls[-1]
    assert "【材料开始】" in last[-1].content and last[-1].content.count("会议决定") == 120  # 当前轮的材料完整，不截断
    assert sum(isinstance(m, HumanMessage) for m in last) < 7  # 让位的是较早的历史
    row = agent.context_store.usage(thread_id=agent.thread_id)[0]
    assert row.budgeted_tokens <= row.context_limit == base + 1500
    assert row.material_tokens > 800 and row.omitted_turns > 0
    from briefly.api import visible_messages
    assert any("历史未纳入上下文" in w for w in visible_messages(agent.history)[-1].warnings)


def test_api_overflow_rebuilds_smaller_context_and_retries_once(engine, monkeypatch):
    state = {"fail": 0}

    def respond(messages):
        if state["fail"]:
            state["fail"] -= 1
            raise ValueError(OVERFLOW_ERROR)
        return AIMessage("好的")

    model = FunctionModel(respond=respond)
    agent = Agent(model, tools=[], context_store=ContextStore(engine))
    for i in range(3):
        agent.chat(f"第{i}轮的问题" * 10)
    turn = agent.counter.count_messages(agent.history[:2])
    monkeypatch.setattr(config, "HISTORY_TOKENS", turn * 3 + 5)  # 1.0 倍放得下 3 轮，0.6 倍只放得下 1 轮
    state["fail"] = 1
    assert agent.chat("新问题") == "好的"
    first, retry = model.calls[-2], model.calls[-1]
    assert len(retry) < len(first)
    rows = agent.context_store.usage(thread_id=agent.thread_id, limit=2)
    assert rows[1].context_overflow and "接口报超长" in rows[1].note and rows[0].note == "重试"


def test_api_overflow_retries_are_limited(engine):
    def respond(messages):
        raise ValueError(OVERFLOW_ERROR)

    model = FunctionModel(respond=respond)
    assert Agent(model, tools=[], context_store=ContextStore(engine)).chat("x") == CONTEXT_OVERFLOW
    assert len(model.calls) == 1 + config.CONTEXT_RETRIES  # 不会无限重试


def test_recent_turn_with_large_inline_material_becomes_reference(store):
    material, _ = store.add_text("纪要", "会议决定下周上线支付改造。" * 120)
    model = FunctionModel(respond=answering())
    agent = Agent(model, tools=[], store=store)
    agent.chat("总结", material_ids=[material.id])
    agent.chat("再短一点")
    previous = model.calls[-1][1]
    assert isinstance(previous, HumanMessage) and "已省略" in previous.content  # 不重复发送同一份材料全文
    assert "会议决定" not in previous.content
    assert agent.history[0].content.count("会议决定") == 120  # 保存的历史不变


# ---- 工具预算


def run_tools(engine, tools, calls_per_step: list[list[dict]], **agent_kwargs):
    """模型按步发起工具调用（每步可并行多个），最后回答“完成”。"""
    def respond(messages):
        steps = sum(isinstance(m, AIMessage) for m in messages[messages.index(_last_human(messages)) :])
        if steps < len(calls_per_step):
            return AIMessage("", tool_calls=[{**c, "id": f"s{steps}c{i}"} for i, c in enumerate(calls_per_step[steps])])
        return AIMessage("完成")

    model = FunctionModel(respond=respond)
    agent = Agent(model, tools=tools, context_store=ContextStore(engine), **agent_kwargs)
    agent.chat("开始")
    return agent, model


def _last_human(messages):
    return [m for m in messages if isinstance(m, HumanMessage)][-1]


def test_parallel_tool_calls_cannot_exceed_turn_budget(engine, monkeypatch):
    monkeypatch.setattr(config, "TURN_TOOL_TOKENS", 1000)
    monkeypatch.setattr(config, "TOOL_RESULT_TOKENS", 400)
    active, peak, lock = [0], [0], threading.Lock()

    @tool
    def fetch(n: int) -> str:
        """读取一段内容。"""
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.1)
        with lock:
            active[0] -= 1
        return "内容" * 170  # 约 200 tokens

    agent, _ = run_tools(engine, [fetch], [[{"name": "fetch", "args": {"n": i}} for i in range(8)]])
    results = tool_results(agent.history)
    executed = [r for r in results if r.status != "error"]
    refused = [r for r in results if r.status == "error"]
    assert peak[0] > 1  # 确实是并行执行的
    assert refused and "达到单轮预算" in refused[0].content
    assert sum(agent.counter.count_text(r.content) for r in executed) <= 1000
    assert len(results) == 8  # 每个调用都有结果，工具调用与结果一一配对


def test_parallel_calls_respect_concurrency_limit(engine, monkeypatch):
    monkeypatch.setattr(config, "TOOL_CONCURRENCY", 2)
    active, peak, lock = [0], [0], threading.Lock()

    @tool
    def slow(n: int) -> str:
        """慢工具。"""
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        with lock:
            active[0] -= 1
        return "ok"

    run_tools(engine, [slow], [[{"name": "slow", "args": {"n": i}} for i in range(6)]])
    assert peak[0] <= 2


def test_oversized_tool_result_is_stored_and_paged_not_silently_cut(engine, monkeypatch):
    monkeypatch.setattr(config, "TOOL_RESULT_TOKENS", 500)
    full = "".join(f"第{i}行：这是一段很长的输出内容。\n" for i in range(300))

    @tool
    def dump() -> str:
        """输出很多内容。"""
        return full

    holder = {}

    def respond(messages):
        results = tool_results(messages)
        if not results:
            return AIMessage("", tool_calls=[{"name": "dump", "args": {}, "id": "d1"}])
        if len(results) == 1:
            holder["id"] = re.search(r"r_[0-9a-f]+", results[0].content).group(0)
            return AIMessage("", tool_calls=[{"name": "read_tool_result", "args": {"result_id": holder["id"], "page": 2}, "id": "d2"}])
        return AIMessage("完成")

    model = FunctionModel(respond=respond)
    agent = Agent(model, tools=[dump], context_store=ContextStore(engine))
    agent.chat("开始")
    preview, page2 = tool_results(agent.history)
    assert preview.content.startswith("【工具结果过长") and "只是开头部分" in preview.content
    assert agent.counter.count_text(preview.content) <= 520
    assert agent.context_store.tool_result(holder["id"]).content == full  # 完整结果可取回
    body = page2.content.split("\n", 1)[1].rsplit("\n（第", 1)[0]
    assert body in full and "第 2/" in page2.content


def test_tool_with_unpredictable_size_is_bounded_by_its_grant(engine, monkeypatch):
    monkeypatch.setattr(config, "TURN_TOOL_TOKENS", 2000)
    monkeypatch.setattr(config, "TOOL_RESULT_TOKENS", 1500)

    @tool
    def surprise(n: int) -> str:
        """返回大小事先不知道。"""
        return "意外地长的结果。" * (50 * n)

    agent, _ = run_tools(engine, [surprise], [[{"name": "surprise", "args": {"n": 10}}], [{"name": "surprise", "args": {"n": 10}}]])
    first, second = tool_results(agent.history)
    assert agent.counter.count_text(first.content) <= 1500 + 20
    assert agent.counter.count_text(first.content) + agent.counter.count_text(second.content) <= 2000 + 40


def test_failed_tool_releases_its_reservation(engine, monkeypatch):
    monkeypatch.setattr(config, "TURN_TOOL_TOKENS", 1000)

    @tool
    def flaky() -> str:
        """会失败。"""
        raise RuntimeError("boom")

    @tool
    def fetch() -> str:
        """读取。"""
        return "内容" * 100

    agent, _ = run_tools(engine, [flaky, fetch], [[{"name": "flaky", "args": {}}], [{"name": "fetch", "args": {}}]])
    error, ok = tool_results(agent.history)
    assert "工具执行失败" in error.content and ok.content == "内容" * 100


def test_turn_time_budget(engine, monkeypatch):
    monkeypatch.setattr(config, "TURN_TIME_BUDGET_S", 0)

    @tool
    def fetch() -> str:
        """读取。"""
        return "x"

    agent, _ = run_tools(engine, [fetch], [[{"name": "fetch", "args": {}}]])
    assert "超过时间预算" in tool_results(agent.history)[0].content


def test_followup_after_tool_results_were_replaced(engine):
    @tool
    def fetch() -> str:
        """读取。"""
        return "原始结果的关键数字是 4217。" * 40

    def respond(messages):
        current = messages[messages.index(_last_human(messages)) :]
        if _last_human(messages).content == "开始" and not tool_results(current):
            return AIMessage("", tool_calls=[{"name": "fetch", "args": {}, "id": "f1"}])
        if _last_human(messages).content == "刚才的数字是多少" and not tool_results(current):
            stub = tool_results(messages)[0]
            assert stub.content.startswith("[已省略")
            return AIMessage("", tool_calls=[{"name": "read_tool_result", "args": {"result_id": "f1"}, "id": "f2"}])
        return AIMessage("完成")

    model = FunctionModel(respond=respond)
    agent = Agent(model, tools=[fetch], context_store=ContextStore(engine))
    agent.chat("开始")
    agent.chat("刚才的数字是多少")
    assert "4217" in tool_results(agent.history)[-1].content  # 被换成引用后仍能读回原文


# ---- 会话压缩

MEMORY_LINE = re.compile(r"- \[(\w+)\] (.+?)(?:（第 \d+ 轮）)?$", re.M)
TURN = re.compile(r"【第 (\d+) 轮】\n用户：(.*)")


def memory_model(seen_turns: list[int] | None = None, fail: bool = False):
    """假模型：对话回复“好的”；压缩时按规则更新记忆：
    【要求】X → 加一条要求；【改要求】旧:A 新:B → 删掉含 A 的要求、加 B；其他轮 → 加一条事实。"""

    def respond(messages):
        if not is_compaction(messages):
            return AIMessage("好的")
        if fail:
            raise RuntimeError("压缩接口出错")
        text = messages[-1].content
        current = text.split("【新的对话】")[0]
        adds, removes = [], []
        for n, said in TURN.findall(text):
            n = int(n)
            if seen_turns is not None:
                seen_turns.append(n)
            if said.startswith("【要求】"):
                adds.append({"field": "requirements", "text": said[4:], "turn": n})
            elif said.startswith("【改要求】"):
                old, new = re.match(r"【改要求】旧:(\S+) 新:(.+)", said).groups()
                removes += [{"id": i, "reason": "用户修改"} for i, t in MEMORY_LINE.findall(current) if old in t]
                adds.append({"field": "requirements", "text": new, "turn": n})
            else:
                adds.append({"field": "facts", "text": f"第{n}轮讨论了普通事项", "turn": n})
        return structured("MemoryUpdate", current_goal="", add=adds, remove=removes)

    return FunctionModel(respond=respond)


def say(n: int) -> str:
    special = {
        1: "【要求】周报控制在300字以内",
        50: "【要求】读者是部门总监",
        80: "【改要求】旧:300字 新:周报控制在500字以内",
    }
    return special.get(n, f"第{n}轮：讨论一些普通的事情，没有新的要求。")


@pytest.fixture
def small_windows(monkeypatch):
    monkeypatch.setattr(config, "COMPACT_TRIGGER_TOKENS", 300)
    monkeypatch.setattr(config, "COMPACT_KEEP_TURNS", 3)
    monkeypatch.setattr(config, "COMPACT_INPUT_TOKENS", 200)
    monkeypatch.setattr(config, "HISTORY_TOKENS", 400)
    monkeypatch.setattr(config, "MEMORY_TOKENS", 300)


def test_long_conversation_keeps_constraints_through_many_compactions(engine, small_windows):
    seen: list[int] = []
    model = memory_model(seen)
    agent = Agent(model, tools=[], context_store=ContextStore(engine))
    for n in range(1, 121):
        agent.chat(say(n))
    agent.chat("现在按要求写周报")

    system = chat_calls(model)[-1][0].content
    assert "周报控制在500字以内" in system and "读者是部门总监" in system  # 第 1、50 轮的约束跨多次压缩仍在
    assert "300字以内" not in system  # 被第 80 轮修改的旧要求已删除
    assert "因篇幅省略" in system  # 低优先级的事实按预算省略，并注明
    versions = agent.context_store.memory_versions(agent.thread_id)
    assert len(versions) >= 10
    assert any(r["reason"] == "用户修改" for v in versions for r in v.changes.get("removed", []))
    assert len(seen) == len(set(seen))  # 每轮只被压缩一次，不对已压缩的内容反复摘要
    assert sorted(seen) == list(range(1, max(seen) + 1))  # 没有漏掉任何一轮
    assert len(agent.history) == 2 * 121  # 原始历史完整
    rows = [r for r in agent.context_store.usage(thread_id=agent.thread_id, limit=1000) if r.task_type == "chat"]
    assert max(r.history_tokens for r in rows) <= 400  # 单次上下文可预测，不随对话增长
    assert max(r.estimated_tokens for r in rows) - min(r.estimated_tokens for r in rows) < 1000
    assert any(r.compaction_triggered for r in rows)


def test_session_resumes_with_memory_after_restart(tmp_path, small_windows):
    from briefly.agent import sqlite_checkpointer

    opened = []  # 测试结束时关闭，避免连接留到被回收时才关

    def connections():
        saver = sqlite_checkpointer(tmp_path / "m.db")
        engine = create_db_engine(f"sqlite:///{tmp_path / 'app.db'}")
        opened.append((saver, engine))
        return saver, ContextStore(engine)

    try:
        saver, store = connections()
        first = Agent(memory_model(), tools=[], checkpointer=saver, context_store=store)
        for n in range(1, 30):
            first.chat(say(n))
        thread = first.thread_id

        model = memory_model()  # 模拟重启：新进程、新的连接
        saver, store = connections()
        second = Agent(model, tools=[], checkpointer=saver, thread_id=thread, context_store=store)
        second.chat("继续")
        sent = chat_calls(model)[-1]
        assert "周报控制在300字以内" in sent[0].content
        humans = [m.content for m in sent if isinstance(m, HumanMessage)]
        assert humans[-1] == "继续" and say(1) not in humans  # 已压缩的早期轮次不再以原文重发
        assert len(second.history) == 2 * 30
    finally:
        for saver, engine in opened:
            saver.conn.close()
            engine.dispose()


def test_compaction_failure_keeps_previous_state_and_degrades_safely(engine, small_windows):
    model = memory_model(fail=True)
    agent = Agent(model, tools=[], context_store=ContextStore(engine))
    for n in range(1, 40):
        assert agent.chat(say(n)) == "好的"  # 压缩失败不影响对话
    assert agent.context_store.latest_memory(agent.thread_id) is None
    system = chat_calls(model)[-1][0].content
    assert "没有放进上下文" in system and "recall_conversation" in system  # 降级时明确告知，可查原文


def test_interrupted_compaction_resumes_from_checkpoint(engine, small_windows, monkeypatch):
    seen: list[int] = []
    model = memory_model(seen)
    agent = Agent(model, tools=[], context_store=ContextStore(engine))
    monkeypatch.setattr(config, "COMPACT_TRIGGER_TOKENS", 10**9)  # 先不自动压缩，攒够历史
    for n in range(1, 20):
        agent.chat(say(n))

    original = ContextStore.save_memory
    calls = {"n": 0}

    def crash_on_second(self, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("进程在保存第二批时退出")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(ContextStore, "save_memory", crash_on_second)
    assert agent.compactor.maybe_compact(agent.thread_id, agent.history, force=True)
    versions = agent.context_store.memory_versions(agent.thread_id)
    assert len(versions) == 1  # 第一批已保存，第二批作废，没有半写入的状态
    first_batch_turns = set(range(1, versions[0].data["compacted_turns"] + 1))
    assert first_batch_turns

    monkeypatch.setattr(ContextStore, "save_memory", original)
    seen.clear()
    agent.compactor.maybe_compact(agent.thread_id, agent.history, force=True)
    assert seen and not (set(seen) & first_batch_turns)  # 从检查点继续，不重复压缩第一批
    assert agent.context_store.memory_versions(agent.thread_id)[-1].data["compacted_turns"] == 19 - 3


def test_memory_out_of_sync_with_history_is_not_trusted_for_position(engine):
    model = FunctionModel(respond=answering())
    agent = Agent(model, tools=[], context_store=ContextStore(engine))
    agent.chat("第一个问题")
    memory = SessionMemory(requirements=[MemoryItem(id="R1", text="用英文回答")], seq=1)
    agent.context_store.save_memory(agent.thread_id, memory.model_dump(), "不存在的消息", 99, {})
    agent.chat("第二个问题")
    sent = model.calls[-1]
    assert "用英文回答" in sent[0].content
    assert sent[1].content == "第一个问题"  # 检查点对不上时宁可重复，不丢历史


def test_apply_update_is_incremental():
    memory = SessionMemory()
    memory, _ = apply_update(memory, MemoryUpdate(add=[{"field": "requirements", "text": "用表格", "turn": 1}]))
    memory, changes = apply_update(
        memory,
        MemoryUpdate(
            add=[{"field": "requirements", "text": "用表格", "turn": 3}, {"field": "pending", "text": "补数据", "turn": 3}],
            remove=[{"id": "X9", "reason": "不存在"}],
        ),
    )
    assert [i.text for i in memory.requirements] == ["用表格"]  # 重复的不加
    assert memory.requirements[0].id == "R1" and memory.pending[0].id == "P2"
    assert changes["ignored"] == [{"remove": "X9"}]
    memory, _ = apply_update(memory, MemoryUpdate(remove=[{"id": "P2", "reason": "完成"}], add=[{"field": "completed", "text": "补数据"}]))
    assert not memory.pending and memory.completed[0].id == "C3"  # 编号不复用


def test_render_memory_keeps_requirements_even_over_budget():
    c = TokenCounter("x", backend="heuristic")
    memory = SessionMemory(
        requirements=[MemoryItem(id=f"R{i}", text=f"第{i}条很重要的要求") for i in range(30)],
        facts=[MemoryItem(id=f"F{i}", text=f"第{i}条事实") for i in range(30)],
    )
    text, omitted = render_memory(memory, 50, c)
    assert all(f"第{i}条很重要的要求" in text for i in range(30))
    assert omitted == 30 and "30 条较早的记录因篇幅省略" in text


# ---- 输入预检与文件


def test_long_pasted_text_is_stored_as_material_with_instruction_kept(store):
    model = FunctionModel(respond=answering())
    agent = Agent(model, tools=[], store=store)
    body = "".join(f"第{i}条记录：今天讨论了项目进度和风险。\n" for i in range(800))
    agent.chat("请把下面的会议记录整理成纪要，只保留决定事项：\n" + body + "以上，谢谢。")
    sent = model.calls[0][-1].content
    assert "已存为材料" in sent and "请把下面的会议记录整理成纪要，只保留决定事项" in sent and "以上，谢谢。" in sent
    assert agent.counter.count_text(sent) < config.INPUT_INLINE_TOKENS
    stored = store.list_materials()[-1]
    assert stored.content.count("今天讨论了") == 800  # 完整内容可取回
    assert f"材料 {stored.id}" in sent


def test_long_input_without_material_store_is_refused_clearly(engine):
    model = FunctionModel(respond=answering())
    reply = Agent(model, tools=[], context_store=ContextStore(engine)).chat("长" * 20000)
    assert reply.startswith("这段输入约") and model.calls == []
    assert INPUT_TOO_LONG.split("{")[0] in reply


def run_repl(agent: Agent, lines: list[str], monkeypatch, capsys) -> str:
    feed = iter([*lines, "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(feed))
    cli.repl(agent, None)
    return capsys.readouterr().out


def test_file_command_stores_large_file_and_sends_listing_only(store, tmp_path, monkeypatch, capsys):
    big = tmp_path / "日志.txt"
    big.write_text("".join(f"第{i}天：完成了第{i}项任务，进展顺利。\n" for i in range(2000)), encoding="utf-8")
    model = FunctionModel(respond=answering())
    agent = Agent(model, tools=[], store=store)
    run_repl(agent, [f"/file {big} 总结一下", f"/file {big} 再看看风险"], monkeypatch, capsys)
    assert len(store.list_materials()) == 1  # 同一文件只解析入库一次
    for call in model.calls:
        assert "【已选材料】" in call[-1].content and "第1999天" not in call[-1].content  # 不把大文件放进对话
        assert agent.counter.count_messages(call[1:]) < 2000


def test_file_command_inlines_small_file_within_budget(store, tmp_path, monkeypatch, capsys):
    small = tmp_path / "纪要.md"
    small.write_text("# 周会\n决定：下周三上线。", encoding="utf-8")
    model = FunctionModel(respond=answering())
    agent = Agent(model, tools=[], store=store)
    out = run_repl(agent, [f"/file {small}", "/context"], monkeypatch, capsys)
    assert "决定：下周三上线" in model.calls[0][-1].content
    assert "（用户只提供了文件" in model.calls[0][-1].content
    assert "上次调用：估算" in out


def test_large_docx_and_pdf_go_through_materials(store):
    from docx import Document
    from test_pdf import make_pdf

    words = "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa".split()

    def line(p: int, k: int) -> str:  # 每页每行文字不同，免得被当成页眉页脚删掉
        n = len(words)
        return " ".join([words[p % n], words[k % n], words[k // n], *words[:9], words[(p + k) % n]])

    doc = Document()
    for i in range(600):
        doc.add_paragraph(f"第{i}段：这是一份很长的文档中的普通段落，记录了项目的进展。")
    buf = BytesIO()
    doc.save(buf)
    docx, _ = store.add_file("长文档.docx", buf.getvalue())
    pdf, _ = store.add_file("长报告.pdf", make_pdf([[(10, 700 - 14 * k, line(p, k)) for k in range(40)] for p in range(30)]))
    assert store.chunk_counts([docx.id])[docx.id] > 10 and store.chunk_counts([pdf.id])[pdf.id] > 10

    model = FunctionModel(respond=answering())
    agent = Agent(model, tools=[], store=store)
    agent.chat("总结", material_ids=[docx.id])
    sent = model.calls[0][-1].content
    assert "【已选材料】" in sent and "第599段" not in sent
    agent.reset()
    calls = len(model.calls)
    agent.chat("总结", material_ids=[docx.id, pdf.id])
    assert len(model.calls) > calls  # 没有表格的文字版 PDF 不再整份受限，旧聊天入口正常交给模型


# ---- Map-Reduce 隔离、缓存、恢复


def extraction_model(fail_chunks: set[int] = frozenset()):
    def respond(messages):
        text = messages[-1].content
        n = int(re.search(r"第 (\d+) 块】", text).group(1))
        if n in fail_chunks:
            raise RuntimeError("接口超时")
        body = text.split("【材料开始】\n")[1].split("\n【材料结束】")[0]
        return structured("Extraction", items=[{"category": "done", "point": body, "quote": body}])

    return FunctionModel(respond=respond)


def test_map_calls_stay_small_with_hundreds_of_chunks_and_results_are_cached(engine):
    cache = ContextStore(engine)
    sources = [Source(1, i, "长材料", f"第{i}项：完成了模块{i}的开发。") for i in range(1, 301)]
    model = extraction_model()
    result = extract_items(model, sources, cache=cache)
    assert result.processed == 300 and len(result.items) == 300
    c = TokenCounter("x", backend="heuristic")
    sizes = [c.count_messages(call) for call in model.calls]
    assert max(sizes) - min(sizes) < 20  # 每块独立调用，单次输入不随块数增长
    assert all("第2项" not in call[-1].content for call in model.calls if "第 1 块】" in call[-1].content)

    again = extraction_model()
    second = extract_items(again, sources, cache=cache)
    assert again.calls == [] and second.cached == 300 and len(second.items) == 300  # 同一材料再次提炼不重算


def test_map_failure_midway_resumes_only_failed_chunks(engine, monkeypatch):
    monkeypatch.setattr(config, "MAP_RETRIES", 0)
    cache = ContextStore(engine)
    sources = [Source(1, i, "材料", f"第{i}项进展。") for i in range(1, 21)]
    first = extract_items(extraction_model(fail_chunks={5, 6, 7}), sources, cache=cache)
    assert [f["source"] for f in first.failed] == ["1-5", "1-6", "1-7"]  # 失败的块明确列出
    model = extraction_model()
    second = extract_items(model, sources, cache=cache)
    assert len(model.calls) == 3 and second.processed == 20


def test_synthesize_refuses_to_send_when_items_cannot_fit(monkeypatch):
    monkeypatch.setattr(config, "MAX_INPUT_TOKENS", 500)
    items = [WorkItem("done", f"完成第{i}项", f"原文{i}", f"1-{i}") for i in range(200)]
    model = FunctionModel(respond=lambda messages: AIMessage(messages[-1].content))  # 合并不会变短
    with pytest.raises(ValueError, match="超出单次输入上限"):
        synthesize(model, "写总结", "请总结", items, budget=200)


# ---- 观测


def test_usage_is_recorded_per_call_with_breakdown_and_map_calls_grouped_by_run(store):
    material, _ = store.add_text("日志", "".join(f"第{i}天完成任务{i}。\n" for i in range(3000)))
    digest = read_prompt("digest")

    def respond(messages):
        if is_extraction(messages):
            return structured("Extraction", items=[])
        if messages[0].content == digest:
            return AIMessage("- 无")
        if not tool_results(messages):
            return AIMessage("", tool_calls=[{"name": "summarize_materials", "args": {}, "id": "s1"}])
        return AIMessage("完成")

    model = FunctionModel(respond=respond)
    agent = Agent(model, tools=[], store=store)
    agent.chat("总结全部", material_ids=[material.id])
    rows = agent.context_store.usage(thread_id=agent.thread_id, limit=500)
    run_ids = {r.run_id for r in rows}
    assert len(run_ids) == 1  # 一次对话里的所有模型调用归到同一个 run_id
    kinds = [r.task_type for r in rows]
    assert kinds.count("chat") == 2 and kinds.count("map") == store.chunk_counts([material.id])[material.id]
    chat = [r for r in rows if r.task_type == "chat"]
    last, first = chat[0], chat[1]
    assert first.system_tokens > 1000 and first.tool_schema_tokens > 100 and first.current_tokens > 0
    assert last.tool_result_tokens > 0 and last.tool_call_count == 1
    assert first.input_tokens == 100 and first.output_tokens == 10  # 接口返回的实际用量与估算分开记
    assert first.estimated_tokens > 0 and first.context_limit == input_limit()
