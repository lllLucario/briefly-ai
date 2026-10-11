# Briefly AI

办公 AI Agent：文档总结、会议纪要拆解、周报生成、待办梳理、文本优化。

## 架构

```
CLI (cli.py) ──► Agent (agent.py) ──► LLM (llm.py)
   │                 │ 会话历史 + 工具调用循环
   └─ loaders.py     ├─ prompts/：base.md（角色与规则）+ 五个能力说明
      txt/md/docx/pdf └─ tools/：模型可调用的工具（飞书等集成放这里）
```

```
src/briefly/
├── cli.py        命令行入口
├── agent.py      单智能体：chat() 带会话记忆与工具调用循环
├── llm.py        Anthropic 客户端封装
├── loaders.py    文件 → 文本
├── config.py     环境变量配置
├── skills/       能力注册表
├── tools/        工具注册表（目前只有 get_current_date）
└── prompts/      base.md + 五个能力说明（summarize/minutes/weekly/todo/polish）
tests/            用 FakeLLM 测试，不消耗 API
data/             inputs/ 与 outputs/（内容不入库）```

新增技能：在 `prompts/` 加 `<name>.md`，并在 `skills/__init__.py` 登记一行。

## 使用

```bash
python -m venv .venv && .venv\Scripts\activate
pip install -e ".[dev]"
copy .env.example .env   # 填入 BRIEFLY_API_KEY / BRIEFLY_BASE_URL / BRIEFLY_MODEL（任意 OpenAI 兼容服务）

briefly data/inputs/meeting.txt -s minutes
briefly data/inputs/notes.md            # 不指定能力则由模型判断
briefly                                 # 对话模式，支持多轮（/file、/reset、/exit）
pytest
```

## 后续可扩展

- 结构化输出（JSON：行动项等），再导出 docx/飞书/邮件
- 长文档分块 + map-reduce 总结
- 周报接入日历/Git/任务系统等数据源（做成 tools）
- Web/IM 机器人入口，与 CLI 共用 Agent
