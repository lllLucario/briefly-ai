# Briefly AI

办公 AI Agent：文档总结、会议纪要拆解、周报生成、待办梳理、文本优化。

## 架构

```
CLI (cli.py) ──► Agent (agent.py) ──► LangChain create_agent（LangGraph）
   │                 │                  ├─ llm.py：OpenAI 兼容模型（ChatOpenAI）
   │                 │                  ├─ tools/：@tool 工具（飞书等集成放这里）
   │                 │                  └─ checkpointer：会话记忆存 SQLite
   └─ loaders.py     └─ prompts/：base.md（角色与规则）+ 五个能力说明
      txt/md/docx/pdf
```

```
src/briefly/
├── cli.py        命令行入口
├── agent.py      单智能体：create_agent + 中间件（提示词、历史裁剪、工具轮数上限）
├── llm.py        模型工厂（唯一创建模型的地方）
├── loaders.py    文件 → 文本
├── db.py         业务数据库（SQLAlchemy）
├── materials.py  材料管理：导入、去重、分块、检索
├── context.py    单次调用的运行时参数
├── config.py     环境变量配置
├── skills/       能力注册表
├── tools/        工具：get_current_date；材料工具（列出、分块读取、关键词检索）
└── prompts/      base.md + 五个能力说明（summarize/minutes/weekly/todo/polish）
tests/            用 LangChain 假聊天模型测试，不消耗 API
data/             inputs/、outputs/、会话记忆库 briefly.db、业务库 app.db（内容不入库）
```

新增技能：在 `prompts/` 加 `<name>.md`，并在 `skills/__init__.py` 登记一行。

## 使用

```bash
python -m venv .venv && .venv\Scripts\activate
pip install -e ".[dev]"
copy .env.example .env   # 填入 BRIEFLY_API_KEY / BRIEFLY_BASE_URL / BRIEFLY_MODEL（任意 OpenAI 兼容服务）

briefly data/inputs/meeting.txt -s minutes
briefly data/inputs/notes.md            # 不指定能力则由模型判断
briefly                                 # 对话模式，支持多轮（/file、/add、/materials、/reset、/exit）
briefly -t <会话编号>                    # 恢复之前的会话（记忆存在 data/briefly.db）
pytest
```

## 后续可扩展

- 结构化输出（JSON：行动项等），再导出 docx/飞书/邮件
- 长文档分块 + map-reduce 总结
- 周报接入日历/Git/任务系统等数据源（做成 tools）
- Web/IM 机器人入口，与 CLI 共用 Agent
