"""已选知识库的资料提问，助理未检索就作答时由程序补检索（KnowledgeSearchGuard）。"""
from langchain_core.messages import AIMessage, ToolMessage

from briefly.api import visible_messages
from briefly.intent import knowledge_question
from fakes import fake, text_msg, tool_msg
from test_knowledge_index import indexed  # noqa: F401  夹具
from test_knowledge_turn import LEAVE, SICK, base_agent, floor, sent_text  # noqa: F401  夹具

DRAFT = "材料里没有，按通行做法年假一般是10天。"


def searches(history):
    return [m for m in history if isinstance(m, ToolMessage) and m.name == "search_knowledge"]


def test_direct_answer_without_search_is_replaced_by_forced_search(indexed):
    model = fake(text_msg(DRAFT), text_msg("年假每年5天〔依据1〕"))
    agent, kb = base_agent(indexed, model)
    answer = agent.chat("员工年假有几天？", kb_id=kb.id)
    assert answer == "年假每年5天〔依据1〕"
    # 草稿不进会话、不再发给模型；补检索的问题就是用户原话，拿到原文后重新作答
    assert DRAFT not in sent_text(model.calls[1]) and all(DRAFT not in str(m.content) for m in agent.history)
    forced = next(m for m in agent.history if isinstance(m, AIMessage) and m.tool_calls)
    assert forced.response_metadata["forced_search"] is True
    assert forced.tool_calls[0]["args"] == {"questions": ["员工年假有几天？"]}
    assert LEAVE in sent_text(model.calls[1]) and len(searches(agent.history)) == 1
    knowledge = visible_messages(agent.history)[-1].knowledge
    assert [r["label"] for r in knowledge["references"]] == ["依据1"] and knowledge["egress"]["evidence_sent"] > 0


def test_forced_search_happens_at_most_once_per_turn(indexed):
    model = fake(text_msg(DRAFT), text_msg("知识库里没有找到相关规定。"))
    agent, kb = base_agent(indexed, model)
    assert agent.chat("员工年假有几天？", kb_id=kb.id) == "知识库里没有找到相关规定。"
    assert len(model.calls) == 2 and len(searches(agent.history)) == 1


def test_forced_search_still_gets_one_output_check_rewrite(indexed):
    # 补检索不占回答检查的重写次数：拿到依据后的回答含内部块号，仍按约定重写一次
    model = fake(text_msg(DRAFT), text_msg("见第3块：年假5天"), text_msg("年假每年5天〔依据1〕"))
    agent, kb = base_agent(indexed, model)
    assert agent.chat("员工年假有几天？", kb_id=kb.id) == "年假每年5天〔依据1〕"
    assert len(model.calls) == 3 and "【系统检查】" in sent_text(model.calls[2])


def test_follow_up_forced_search_completes_the_question_from_previous_turn(indexed):
    model = fake(tool_msg("search_knowledge", {"questions": ["年假有几天"]}), text_msg("年假5天〔依据1〕"),
                 text_msg("病假一般没有限制。"), text_msg("病假需医院证明〔依据1〕"))
    agent, kb = base_agent(indexed, model, LEAVE, SICK)
    agent.chat("年假怎么规定？", kb_id=kb.id)
    assert agent.chat("那病假呢", kb_id=kb.id) == "病假需医院证明〔依据1〕"
    forced = [m for m in agent.history if isinstance(m, AIMessage) and m.response_metadata.get("forced_search")]
    assert forced[0].tool_calls[0]["args"]["questions"] == ["那病假呢", "年假怎么规定？ 那病假呢"]
    assert SICK in sent_text(model.calls[-1])


def test_no_forced_search_for_non_knowledge_turns(indexed):
    model = fake(text_msg("不客气。"), text_msg("请假条：……"), text_msg("| 项目 | 规定 |"), text_msg("没有附材料。"))
    agent, kb = base_agent(indexed, model)
    agent.chat("好的谢谢", kb_id=kb.id)  # 闲聊
    agent.chat("帮我写个请假条", kb_id=kb.id)  # 写作任务
    agent.chat("换成表格", kb_id=kb.id)  # 调整上一轮（规则层 revise）
    agent.chat("年假有几天？", kb_id=kb.id, skill="summarize")  # 用户指定了能力
    assert len(model.calls) == 4 and not searches(agent.history)


def test_no_forced_search_without_selected_knowledge_base(indexed):
    model = fake(text_msg("请先在输入框上方选择知识库。"))
    agent, _ = base_agent(indexed, model)
    agent.chat("员工年假有几天？")
    assert len(model.calls) == 1 and not searches(agent.history)


def test_knowledge_question_shapes():
    for text in ["ISO9001-2015-FDIS英文版7.5.3.2(b)中，文件储存和保存特别要求保持哪项特性？请附英文原词。",
                 "年假有几天", "报销制度在哪个文件", "那病假呢", "差旅标准？"]:
        assert knowledge_question(text), text
    for text in ["好的谢谢", "帮我写个请假条", "总结一下这份制度", "把这段润色一下", ""]:
        assert not knowledge_question(text), text
