"""事实保留反例：不以散落关键词代替事项关系，回查分页可到达末尾。"""
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from briefly import config
from briefly.api import visible_messages
from briefly.conflicts import value_in
from briefly.context_store import ContextStore
from briefly.db import create_db_engine
from briefly.diagnostics import Fact, Stages, diagnose, has_fact, stages_from_history
from briefly.fidelity import fact_problems, outline_problems
from briefly.output_check import InternalTermCheck, missing_conflict_values
from briefly.tools.context import context_tools
from briefly.agent import Agent, sqlite_checkpointer
from fakes import FunctionModel, structured


@pytest.fixture
def recall(monkeypatch):
    engine = create_db_engine("sqlite://")
    import briefly.tools.context as module
    monkeypatch.setattr(module,"_page_chars",lambda:180)
    yield context_tools(ContextStore(engine))[0].func
    engine.dispose()


def runtime(messages):
    return SimpleNamespace(state={"messages":messages})


def test_recall_all_pages_reaches_tail_and_does_not_count_internal_turns(recall):
    text = "开头"+"填充段落。"*150+"末尾约束：禁止代发邮件。"
    messages = [HumanMessage("缩略后的模型输入",additional_kwargs={"display":text}),
                AIMessage("首次答复"),HumanMessage("内部核对",additional_kwargs={"internal":True}),
                AIMessage("重写答复"),HumanMessage("下一轮")]
    pages = []
    for p in range(1,30):
        out = recall(runtime(messages),turn=1,page=p)
        pages.append(out)
        if "已读完" in out: break
    assert "末尾约束：禁止代发邮件" in "".join(pages)
    assert "内部核对" not in "".join(pages) and "缩略后的模型输入" not in "".join(pages)
    assert "下一轮" in recall(runtime(messages),turn=2)
    with pytest.raises(ValueError,match="页码超出"):
        recall(runtime(messages),turn=1,page=999)


def test_keyword_search_includes_later_hits_in_same_long_message(recall):
    messages=[HumanMessage("预算：100元。"+"背景段落。"*120+"预算最终改为200元。")]
    output=[]
    for page in range(1,12):
        text=recall(runtime(messages),keywords="预算",page=page)
        output.append(text)
        if "已读完" in text:break
    assert "100元" in "".join(output) and "200元" in "".join(output)
    assert "turn=" not in "".join(output)  # 搜索页提示使用相同关键词分页，不能误读成单轮首屏


def test_recall_has_no_other_session_or_attached_material(recall):
    text=recall(runtime([HumanMessage("原话+程序附加的秘密材料",additional_kwargs={"display":"仅用户输入"})]),turn=1)
    assert "仅用户输入" in text and "秘密材料" not in text
    assert "没有找到" in recall(runtime([HumanMessage("另一个独立会话")]),keywords="仅用户输入")
    with pytest.raises(ValueError):recall(runtime([]),keywords="无命中",page=2)


@pytest.mark.parametrize("value,text,expected",[
    ("10月20日","10次成功、20条用例",False),
    ("2026-10-20","2026年10月20日",True),
    ("2026-10-20","2025-10-20",False),
    ("未完成3条","已完成3条",False),
    ("8000元","8000美元",False),
    ("1.8秒","1.8 秒",True),
    ("4. 文件追踪","3. 文件追踪",False),
])
def test_literal_claim_cannot_be_proven_by_unrelated_numbers(value,text,expected):
    assert value_in(text,value)==expected


def test_scoped_facts_reject_swapped_people_dates_and_status():
    facts=[Fact("账单负责人",["王敏"],subject="账单核对"),
           Fact("账单期限",["2026-10-23"],subject="账单核对"),
           Fact("账单未完成",["未完成"],subject="账单核对")]
    wrong="账单核对由赵琳负责，2026-10-26已完成；接口回归由王敏负责，2026-10-23未完成。"
    assert not any(has_fact(wrong,f) for f in facts)
    good="|账单核对|王敏|2026-10-23|未完成|"
    assert all(has_fact(good,f) for f in facts)


def test_table_row_keeps_scope_across_cell_semicolon_and_negative_alias_stays_negative():
    fact=Fact("灰度",["不代表正式上线"],subject="灰度验证",
              alternatives={"不代表正式上线":["不等同于正式上线"]})
    assert has_fact("|灰度验证|待进行；不代表正式上线|",fact)
    assert has_fact("灰度验证不等同于正式上线。",fact)
    assert not has_fact("灰度验证等同于正式上线。",fact)
    assert not has_fact("|正式上线|不代表正式上线|\n|灰度验证|已完成|",fact)


def test_four_stage_diagnosis_distinguishes_extraction_and_synthesis():
    facts=[Fact("提取丢失",["王敏"],subject="账单核对"),Fact("合成丢失",["赵琳"],subject="接口回归")]
    st=Stages(map_reduce="无",tool_message="无",answer="无",extraction="接口回归由赵琳负责")
    d=diagnose(facts,[],st,{})
    assert d.lost_at=={"逐块提取":["提取丢失"],"合成汇总":["合成丢失"]}
    assert "extraction" in d.retention


def messages(instruction="请完整逐项列出所有事实"):
    item={"importance":"high","subject":"账单核对","attribute":"owner","value":"王敏",
          "point":"王敏负责账单核对","key_details":["王敏","账单核对"]}
    return [HumanMessage(instruction),ToolMessage("账单核对：王敏负责",tool_call_id="a",name="summarize_materials",artifact={"items":[item],"content_full":"账单核对：王敏负责"})]


def test_runtime_check_respects_partial_scope_and_rejects_swapped_owner():
    assert fact_problems("账单核对：赵琳；接口回归：王敏",messages())
    assert not fact_problems("账单核对：王敏",messages())
    assert not fact_problems("只看接口回归",messages("只总结接口回归"))
    assert not fact_problems("一句话概括",messages("再简短一点"))


def test_outline_check_only_applies_to_explicit_original_numbering():
    source=["## 1. 账单核对\n内容\n## 4. 文件追踪\n内容"]
    assert outline_problems("1. 账单核对\n2. 文件追踪",messages("完整列出并保留原文章节编号"),source)
    assert not outline_problems("1. 账单核对\n2. 文件追踪",messages("按你自己的列表总结"),source)
    assert not outline_problems("1. 账单核对\n4. 文件追踪",messages("保留原文章节编号"),source)
    assert not outline_problems("**1. 账单核对**\n**4. 文件追踪**",messages("保留原文章节编号"),source)
    assert not outline_problems("【1. 账单核对】正文\n【4. 文件追踪】正文",messages("保留原文章节编号"),source)
    assert outline_problems("- 账单核对\n4. 文件追踪",messages("保留原文章节编号"),source)
    assert outline_problems("1. 账单核对\n2. 4. 文件追踪\n4. 文件追踪",messages("保留原文章节编号"),source)


def test_one_retry_and_persisted_warning_for_remaining_relation_error():
    check=InternalTermCheck(lambda request:[])
    request=SimpleNamespace(messages=messages())
    request.override=lambda **changes:SimpleNamespace(**{**vars(request),**changes})
    calls=[]
    def respond(req):
        calls.append(req)
        return AIMessage("账单核对由赵琳负责；接口回归由王敏负责")
    answer=check.wrap_model_call(request,respond)
    assert len(calls)==2 and answer.response_metadata["fidelity_issues"]
    output=visible_messages([answer])[0]
    assert any("重要事实" in w for w in output.warnings)
    assert not any("王敏" in w for w in output.warnings)  # 网页检查提醒不复制私人原话


def test_conflict_value_cannot_be_completed_with_scattered_digits():
    assert missing_conflict_values("10次检查，20条用例",[{"subject":"项目A","attribute":"date","values":["10月20日"]}])


def test_internal_retry_does_not_hide_prior_tool_diagnosis():
    history=messages()+[HumanMessage("内部重写",additional_kwargs={"internal":True}),AIMessage("账单核对：王敏")]
    st=stages_from_history(history)
    assert st.tool_called and st.extraction=="王敏负责账单核对"


def test_sqlite_restart_after_compaction_can_page_to_original_tail(tmp_path,monkeypatch):
    import re
    import briefly.tools.context as module
    monkeypatch.setattr(config,"COMPACT_KEEP_TURNS",0)
    monkeypatch.setattr(module,"_page_chars",lambda:700)
    engine=create_db_engine(f"sqlite:///{(tmp_path/'app.db').as_posix()}")
    saver=sqlite_checkpointer(tmp_path/'chat.db')
    def record(messages):
        if "【当前记忆】" in messages[-1].text:
            return structured("MemoryUpdate",current_goal="",add=[],remove=[])
        return AIMessage("已记录")
    try:
        agent=Agent(FunctionModel(respond=record,calls=[]),context_store=ContextStore(engine),checkpointer=saver)
        agent.chat("背景记录。"*600+"末尾审计词：松柏；禁止代发邮件。")
        thread=agent.thread_id
        assert agent.compactor.maybe_compact(thread,agent.history,force=True)
        saver.conn.close()
        saver=sqlite_checkpointer(tmp_path/'chat.db')
        def recall_reply(messages):
            if isinstance(messages[-1],ToolMessage):
                if "松柏" in messages[-1].text:
                    return AIMessage("末尾审计词是松柏；禁止代发邮件。")
                page=int(re.search(r"第 (\d+)/",messages[-1].text)[1])+1
            else:
                page=1
            return AIMessage("",tool_calls=[{"name":"recall_conversation","args":{"turn":1,"page":page},"id":f"r{page}"}])
        restarted=Agent(FunctionModel(respond=recall_reply,calls=[]),context_store=ContextStore(engine),checkpointer=saver)
        assert "松柏" in restarted.chat("回查第一轮末尾原话",thread_id=thread)
        results=[m for m in restarted.history_of(thread) if isinstance(m,ToolMessage)]
        assert len(results)>1 and "已读完" in results[-1].text
        assert not any("松柏" in x.text for x in restarted.history_of("other-thread"))
    finally:
        saver.conn.close()
        engine.dispose()
