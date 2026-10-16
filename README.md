# Briefly AI

办公 AI Agent：文档总结、会议纪要拆解、周报生成、待办梳理、文本优化。

单智能体，基于 LangChain / LangGraph，模型走 OpenAI 兼容接口（默认 DeepSeek）。重点放在**内容可靠**：材料中没有的不编造，周报和总结的每一条都能追溯到原文，处理不了的明确说明而不是悄悄略过。

## 功能

- **五项能力**：一份总提示词加五份能力说明，由模型判断任务，也可以指定。支持多轮修改（“再短一点”“改成表格”）。
- **材料管理**：粘贴文本，或上传 txt / md / docx / pdf / xlsx / csv，统一转成带标题的 Markdown，识别章节、分块入库。
  - PDF：清理页眉页脚、水印、页码、乱码，合并排版换行；按字号、书签识别标题；扫描件明确提示。
  - Word：标题、列表、表格、脚注（docling）；Excel：工作表作为章节，合并单元格、百分比按显示处理。
- **长材料**：按大纲、章节、关键词按需读取；需要完整总结时由程序逐块提取（Map-Reduce），每条要点核对原文引用，不会漏读。
- **周报**：一周一份。每周到点自动开始，或随时点“现在生成”：检查追踪文件的更新，把这周的文件变化、待办进展整理成审核清单，你确认后再生成；还可以加入本周材料、写本周说明。每条带出处，导出时去掉；数字核对；漏写的要点、矛盾单独提醒，不进入周报正文。重新生成或修改前的正文存为历史版本。
- **追踪文件**：本机文件、文件夹、飞书文档开启追踪后，每个工作日检查修改，记录版本和差异，变化进入周报的审核清单。
- **待办**：两级待办（父待办 + 子待办，各自有状态和截止日期）、简短的进展说明；关联材料库里的材料（在待办里上传也只存一份）。待办这周的进展和关联文件的修改进入审核清单，你确认后按父待办分组写进周报；改待办状态需要你明确同步，旧周报保留生成时的记录。
- **上下文管理**：每次调用模型前统一组装与预算检查，早期对话压缩为结构化会话记忆（原始历史不改写），工具结果有单轮预算，过长的输入自动转存为材料。
- **飞书**（可选）：粘贴飞书文档、知识库页面或多维表格链接作为材料；周报一键写成飞书文档；飞书文档和文件夹可以追踪；审核清单整理好时机器人发消息给你。
- **邮件**（可选）：周报通过 SMTP 发给导师等收件人，网页上手动发送、发送前确认收件人。
- **后端**：FastAPI，对话、待办、材料与追踪、周报接口。

## 快速开始

```bash
python -m venv .venv
.venv\Scripts\activate          # macOS / Linux：source .venv/bin/activate
pip install -e ".[dev]"
copy .env.example .env          # 填入 BRIEFLY_API_KEY / BRIEFLY_BASE_URL / BRIEFLY_MODEL
```

```bash
briefly                                   # 对话模式：/file /add /materials /context /reset /exit
briefly 会议记录.txt -s minutes           # 单次处理文件；不加 -s 由模型判断任务
briefly -t <会话编号>                     # 恢复之前的会话
briefly-api                               # 启动后端和网页：http://127.0.0.1:8000 ，接口文档：/docs
```

### 周报：追踪文件 → 审核清单 → 生成

网页上在“周报”页操作：右下角“现在生成”先检查更新、整理审核清单，审核完再“确认并生成”；“周报设置”里设每周定时、默认读者与署名、你在文档里的名字。要追踪的文件在“材料”页的“追踪的文件”里添加。命令行如下：

```bash
briefly track add D:\项目 方案.docx 进度.xlsx      # 追踪文件和文件夹（也可以是飞书链接），立即保存初始版本
briefly track scan                               # 检查更新（briefly-api 运行时每个工作日自动检查）
briefly track diff <文件编号>                    # 查看两个版本的差异
briefly weekly settings --owner 张三             # 你在 Word 修订等文档里的名字，用于判断改动是不是你做的
briefly weekly prepare                           # 开始本周周报：检查更新 + 整理审核清单（--week 指定其他周）
briefly weekly review                            # 审核：确认、排除、修改、补充、合并、拆分
briefly weekly generate -o 周报.md               # 确认清单并生成周报
briefly weekly schedule --weekday 周五 --time 16:30   # 每周到点自动开始（整理清单并提醒你审核）
briefly weekly inbox                             # 查看提醒（网页顶部也会显示）
```

没有常驻 `briefly-api` 时，可以用系统计划任务定时运行 `briefly weekly run-due`；`briefly track prune` 清理旧版本（被审核清单引用的版本不会删除）。

### 飞书（可选）

在飞书开发者后台创建企业自建应用，开通文档、知识库、多维表格、云空间、机器人发消息等权限并发布版本，然后在 `.env` 填 `FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_NOTIFY_USER`（你的飞书 open_id，`ou_` 开头；普通邮箱不行）。应用以自己的身份访问，需要在文档右上角“…→ 更多 → 添加文档应用”把它加进来。

```bash
briefly https://xxx.feishu.cn/docx/... -s summary   # 读取飞书文档并总结（对话中 /add、/file 也可以粘贴链接）
briefly track add https://xxx.feishu.cn/wiki/...          # 追踪飞书文档的变化
```

网页上：材料页“读取飞书内容”“飞书云空间”（导入或追踪），周报页“写入飞书文档”。未配置时这些入口不显示，其余功能不受影响。

运行数据（会话记忆、材料库、文件版本）默认存在 `data/` 下的 SQLite 文件和 `data/tracking/` 目录中，不进 Git；`DATABASE_URL` 可换成 Postgres。

## 测试

```bash
pytest -W error                           # 单元测试，用假模型，不调用真实 API
python tests/eval/run_eval.py -n 3        # 实测用例集：真实模型，每个用例跑 3 次统计通过率
```

实测用例集（`tests/eval/`）覆盖周报、五项能力、异常输入（乱码、超出范围、提示词注入、超长）、多轮对话和长材料，用例材料均为虚构。

## 结构

```
src/briefly/
├── agent.py            单智能体：create_agent + 中间件
├── context_builder.py  每次调用前组装上下文、检查预算
├── compaction.py       会话压缩为结构化记忆
├── tool_budget.py      工具结果的单轮预算
├── context_store.py    会话记忆、超大工具结果、用量记录、提取缓存
├── tokens.py           token 计数（中文按实测系数，可用接口用量校准）
├── llm.py              模型工厂（唯一创建模型的地方）
├── loaders.py          文件 → 带标题的 Markdown + 解析报告
├── pdf.py              PDF 解析
├── layout_cleaner.py   PDF 页眉页脚、水印、页码、乱码清理，合并排版换行
├── tables.py           xlsx / csv → Markdown 表格
├── chunking.py         章节识别与分块
├── materials.py        材料库：导入、去重、检索
├── summarize.py        Map-Reduce：逐块提取并核对原文、分批合并、漏写核对
├── mail.py             邮件发送周报（SMTP）
├── tracking/           追踪文件：快照、Diff、归属、每日扫描、清理
├── weekly/             周报：一周一份，审核清单、写作引擎、生成、定时与提醒
├── todos/              待办：两级结构、材料关联、活动记录，以及与周报审核清单的联动
├── feishu/             飞书：读取文档 / 多维表格、写入文档、机器人提醒（httpx 薄封装）
├── api.py              FastAPI 后端（并提供 web/ 下的网页）
├── cli.py              命令行入口
├── config.py           环境变量配置
├── tools/              工具：日期、材料（列出、大纲、读取、检索、完整提炼）、会话（查原话、读回工具结果）、读取飞书链接
├── skills/             能力注册表
└── prompts/            base.md + 五个能力说明，以及提取、合成、压缩用的提示词
tests/                  单元测试；eval/ 为真实模型实测用例集
```

新增能力：在 `prompts/` 加 `<name>.md`，在 `skills/__init__.py` 登记一行。新增工具：在 `tools/` 用 `@tool` 写函数并登记，在 `base.md` 的“工具使用”里写明何时调用。
