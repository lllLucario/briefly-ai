# Briefly AI

把办公材料整理成清晰的结果，把行动项接到待办，把工作记录汇成周报。

Briefly AI 是基于 **LangChain / LangGraph 的单智能体办公应用**，提供文档总结、会议纪要拆解、周报生成、待办事项梳理和文本优化，并支持基于企业私有文档的**知识库问答（RAG）**。支持网页与命令行，默认使用 DeepSeek，也可配置支持工具调用和结构化输出的 OpenAI 兼容模型服务。

当前面向**单用户、本地运行**：文档、知识库和索引保存在本机，Embedding 与图片文字识别在本机完成，只有生成回答时把必要内容发给所配置的模型服务。飞书和邮件为可选集成，未配置时仍可使用核心办公功能。

## 核心能力

- **文档总结**：提取核心要点、关键数据和待确认事项；支持章节读取、关键词检索与长材料分块提炼。
- **会议纪要拆解**：区分讨论、决议、行动项和遗留问题，保留负责人、期限与依据；没有的信息标为“未提及”或“待定”。
- **周报生成**：汇集本周材料、补充说明、待办进展和追踪文件变化，审核后生成正式周报；支持编辑、历史版本、Markdown 导出与浏览器打印 PDF。
- **待办事项梳理**：提取候选行动项，经结构化预览和勾选确认后保存；支持两级任务、独立状态、截止日期、材料关联与进展记录。
- **文本优化**：按读者、语气、篇幅和格式调整表达，要求保留原意、数字、日期及事实的确定程度。
- **知识库问答**：把制度、项目资料、产品手册等私有文档批量导入知识库，提问时检索原文片段作答，每个结论标出依据并可回到原文位置；资料里没有的明确说明未找到。

支持自动识别或指定能力；同一会话可继续要求“再短一点”“改成表格”或“把行动项整理成待办”。

## 工作流程

网页提供“待办、助理、材料、知识库、周报”五个入口，默认进入待办。可以从任一条路径开始：

1. **临时处理材料**：粘贴内容或选择材料 → 总结、整理纪要、优化文本 → 多轮修改 → 复制使用。
2. **持续推进任务**：创建待办或从助理结果预览保存 → 关联材料 → 记录进展、追踪文件更新 → 纳入本周审核清单。
3. **形成正式周报**：整理本周工作 → 核对纳入或排除 → 确认生成 → 核对正文 → 导出或显式发送。
4. **查询企业资料**：在知识库页建库并批量上传 → 自动解析、切片并建立索引 → 在助理页选择知识库提问 → 得到带依据的回答。

一次提问的处理流程是：**用户提问 → 意图识别 → 知识库检索 → 答案整合 → 结构化输出**。意图识别分三层：规则层直接识别明确的任务，本地语义层判断问题类型和所选知识库的相关度，其余交给助理智能体多步处理；简单的资料问题直接检索作答，复杂或多轮的问题由助理调用知识库检索工具。

**自动周报的边界**：定时任务自动检查更新、整理审核清单并提醒；正式正文由用户核对后触发生成，发送也需要显式操作。服务需保持运行，到点不会绕过审核自动发送。

助理回答是草稿。保存待办会重新核对原始输入与材料；带入周报页的是用户确认的原始信息和材料，模型草稿不会自动成为业务事实。

## 快速开始

需要 Python **3.11 或更高版本**、Git，以及可用的模型服务 API Key。主要验证环境为 Windows。

### 1. 获取项目并安装

Windows PowerShell：

```powershell
git clone https://github.com/lllLucario/briefly-ai.git
cd briefly-ai
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,rag,ocr]"
Copy-Item .env.example .env
```

macOS / Linux：

```bash
git clone https://github.com/lllLucario/briefly-ai.git
cd briefly-ai
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev,rag,ocr]"
cp .env.example .env
```

`rag` 是知识库问答所需的本地 Embedding 与向量索引，`ocr` 是扫描件和图片文字识别；只用五项办公能力时可以只装 `.[dev]`。

**锁定依赖（可复现安装，CI 用的就是这种方式）**：`uv.lock` 锁定了全部依赖和可选依赖 `dev`、`rag`、`ocr`、`browser`，覆盖 Python 3.14 的 Linux 和 Windows，其他平台或 Python 版本用上面的 pip 方式。需要先[安装 uv](https://docs.astral.sh/uv/getting-started/installation/)：

```bash
uv sync --locked --all-extras   # 按锁文件创建 .venv，锁文件过期时报错
uv lock --check                 # 只检查 uv.lock 是否和 pyproject.toml 一致
uv lock                         # 改过 pyproject.toml 的依赖后更新锁文件，已锁定的版本不变
uv lock --upgrade-package httpx # 只升级某个包
```

### 2. 配置模型

编辑项目根目录的 `.env`：

```dotenv
BRIEFLY_API_KEY=your-key-here
BRIEFLY_BASE_URL=https://api.deepseek.com
BRIEFLY_MODEL=deepseek-flash
```

接入其他兼容服务时，按服务商要求修改地址与模型名；若服务商不支持 DeepSeek 的思考模式参数，再设置 `BRIEFLY_THINKING=provider`。不同模型的工具兼容性和输出质量需要分别验证。需要完全私有化时，可把地址换成内网部署的 OpenAI 兼容模型服务。

使用知识库前，先准备本地 Embedding 模型（只下载公开模型，不上传任何文档），并在 `.env` 指向它：

```bash
python tests/eval/w3_prepare_models.py
```

```dotenv
BRIEFLY_EMBEDDING_DIR=.tmp/w3-models/bge-small-zh-v1.5
```

离线服务器可整体复制准备好的模型目录。

### 3. 启动网页

```bash
briefly-api
```

打开 [本地工作台](http://127.0.0.1:8000)，接口文档位于 [Swagger UI](http://127.0.0.1:8000/docs)。在助理页输入一段会议记录即可开始，也可以先在待办页创建任务。

模型请求会产生服务商 API 费用。相关输入、历史上下文及材料内容会按任务需要发送到所配置的模型服务。

**访问控制：** 在 `.env` 设置 `BRIEFLY_ACCESS_TOKEN` 后，打开网页要先登录，脚本调用接口要带 `Authorization: Bearer <令牌>`。

| 配置 | 行为 |
|---|---|
| 设了令牌 | 始终要求登录 |
| 没设令牌，绑定本机地址（默认） | 不要求登录，启动日志会提示 |
| 没设令牌，绑定其他地址 | 拒绝启动 |

不管有没有令牌，都只接受 `BRIEFLY_ALLOWED_HOSTS` 里的主机名（默认 `localhost,127.0.0.1,[::1]`），并拒绝来自其他网站的写操作。用其他地址访问时，要把主机名加进去。

就绪检查：`GET /healthz` 说明进程活着，不需要登录；`GET /readyz` 检查业务库和模型配置，启用令牌时需要登录。

## 材料与长文处理

- 支持文本粘贴及上传 `txt`、`md`、`doc`、`docx`、`pdf`、`xlsx`、`xls`、`csv`、`pptx`、`ppt` 和常见图片（`png`、`jpg` 等，需安装 `ocr`），普通材料默认单次上传上限 10 MB，知识库默认单个文件上限 50 MB（`BRIEFLY_MAX_KNOWLEDGE_FILE_MB`，单批合计也不超过 50 MB）。旧版 `.doc`/`.xls`/`.ppt` 优先直接读取，读不了时用本机 LibreOffice 转换。
- 统一解析成文本或 Markdown，保留可识别的标题、列表和表格，并提供解析提示；支持搜索、筛选、关联待办和内容去重。
- 短材料直接进入上下文；长材料通过大纲、检索和分块工具读取。完整提炼使用 Map-Reduce，逐块提取、核对原文摘录并合并结果。
- 本机文件、文件夹及支持的飞书来源可开启追踪，记录版本和差异；上传文件是副本，要监测原文件后续修改需另行开启追踪。

文件变化是待核对的信息，不直接等同于用户完成了工作。长材料提炼也可能遗漏或误关联事实，重要结果仍需对照原文。

材料库是个人的**工作区**，放正在处理的文件；知识库是企业的**受管控参考资料**。两者分开管理：知识库文档不会出现在材料列表里，只通过知识库检索读取。

## 知识库问答

**导入与切片**（参考 Coze／火山知识库的导入规则）：

- 在知识库页建库，批量上传 Word、PDF、Excel、PPT 和扫描件；每个文件在独立进程中解析，互不影响，逐文件显示状态。
- 解析、切片后自动建立索引并发布到检索，无需用户审批；文件详情只展示章节与片段标题骨架，不展开长正文。切片按标题和段落优先，表格按完整行切并在每片写明行列，幻灯片能装下时整页一片；可设置切片目标长度，正文相邻片段有少量重叠。
- 文档有版本：更新时导入新版本并自动建立索引，旧版本的引用仍能回到原文；删除文档同时撤下索引。

**检索与溯源**：

- 本地 BGE 向量检索与 SQLite FTS5 关键词检索两路召回，用 RRF 融合排序；低于相关度下限的片段不作为依据，避免拿不相关的内容作答。
- 回答中每个结论标注〔依据N〕，可以展开查看原文片段、所在文档、章节和版本。
- 解析质量没有确认的范围（例如复杂表格的局部、低置信度的图片识别文字）不作为确定依据，回答会提示核对原件。

**外发与工作区**：

- 每个知识库默认不允许外发，首次提问时提示开启；开启后每次回答显示本次发送给模型的片段数，可随时关闭。
- 选了知识库提问时，同时检索个人工作区里的文件，回答标明出处。两边说法不同时会指出差异：问现行规定以知识库已发布版为准，处理自己的稿子以工作区为准。
- 所选知识库明显不是最相关的库时，提示可能在另一个库里，并可一键切换重问。

相关配置见 [配置示例](.env.example)：`BRIEFLY_EMBEDDING_DIR`、`BRIEFLY_KNOWLEDGE_DIR`、`BRIEFLY_PARSE_TIMEOUT_S`，以及 `BRIEFLY_OCR*` 系列。

## 命令行使用

在已激活虚拟环境的项目目录下运行：

```bash
briefly                                           # 多轮对话
briefly "会议记录.txt" -s minutes                  # 会议纪要
briefly "报告.pdf" -s summarize -e "控制在200字内"  # 总结与额外要求
briefly "工作记录.md" -s weekly -o "周报草稿.md"    # 聊天周报草稿
briefly -t SESSION_ID                             # 恢复历史会话
```

能力参数：`summarize`、`minutes`、`weekly`、`todo`、`polish`。不指定时由模型判断。对话命令包括 `/file`、`/add`、`/materials`、`/context`、`/reset` 和 `/exit`。

追踪与正式周报：

```bash
briefly track add "方案.docx" "进度.xlsx"
briefly track scan
briefly weekly prepare
briefly weekly review
briefly weekly generate -o "正式周报.md"
briefly weekly schedule --weekday 周五 --time 16:30
```

更多参数见 `briefly -h`、`briefly track -h` 和 `briefly weekly -h`。

定时由后台线程与 `run-due` 实现。建议常驻一个 `briefly-api` 进程；不运行 API 时，可由系统计划任务调用 `briefly weekly run-due`。时区可通过 `BRIEFLY_TRACKING_TIMEZONE` 配置，例如 `Asia/Shanghai`；未配置时使用系统本地时区。

## 可选集成

### 飞书

通过飞书开放平台官方接口接入文档、知识库、多维表格、云空间与妙记文字稿；支持材料导入、追踪支持的文档、将周报写入新文档及机器人提醒。文本提炼使用自定义 LangChain 工具，文件格式转换使用本地解析库。

1. 在飞书开发者后台创建企业自建应用，按需要开通文档、知识库、多维表格、云空间、妙记及消息权限，并发布应用版本。
2. 在 `.env` 填写 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET`。以应用身份访问时，还需给应用授予对应文档或文件夹权限。
3. 在材料页粘贴飞书链接；使用个人云空间时，可通过网页完成 OAuth 用户授权。回调地址需与开发者后台一致，可配置 `FEISHU_REDIRECT_URI`。
4. 如需提醒或导出后的协作权限，配置 `FEISHU_NOTIFY_USER`；导出目录等参数见 [配置示例](.env.example)。

具体可用内容取决于应用权限、用户授权和文档访问权限，并非所有飞书文档类型均受支持。

### 邮件

配置 `SMTP_HOST`、`SMTP_PORT`、`SMTP_USER`、`SMTP_PASSWORD` 后，可在周报页确认收件人并发送。部分邮箱需要 SMTP 授权码。可选设置 `MAIL_FROM_NAME` 和 `MAIL_DEFAULT_TO`。

外发失败或结果未知会保留记录；发送阶段断线时，邮件可能已经发出，应先核实记录再决定是否重试。

## 可靠性与使用边界

- **事实与来源**：要求不编造、缺失信息保持待定、冲突保留来源。程序辅助检查字数、日期、数字依据、部分事实遗漏及虚报操作成功；这些检查不等于完整语义验证。
- **回答恢复**：同一轮最多进行一次检查重写，仍有问题显示提醒；模型或工具失败保留失败说明，用户可修改后重试。
- **多轮上下文**：LangGraph checkpoint 持久化历史，早期对话压缩成结构化记忆，支持原话回查。上下文和工具有预算限制；压缩记忆、自然指代和复杂多任务仍可能出现遗漏。
- **业务保存**：待办保存保留预览、来源快照核对、幂等回执及事务检查；周报整理和生成有租约及写入身份检查。助理查询工具只读，修改与外发通过显式业务入口完成。
- **文件解析**：扫描 PDF、图片文件以及 Word / PowerPoint / Excel 里图片中的文字可用本地 OCR 识别（可选安装 `python -m pip install -e ".[ocr]"`，RapidOCR + PP-OCRv6 small，模型随包分发，本机 CPU 推理、不联网；`BRIEFLY_OCR=off` 关闭）。识别文字在正文中以“〔图片文字识别｜来源｜平均置信度〕”标出，置信度低的段落不作为确定依据；每份文件有识别页数、图片数和时长上限，超出部分在解析提示里写明。本地录音直接转写尚未实现；复杂 PDF 多栏、表格及部分 Word 内容可能无法完整还原，请检查解析提示和原文件。
- **部署范围**：当前以单用户、单 API 进程为主，尚未完成多用户隔离和生产部署验证。应用没有完整的登录与权限体系，默认仅监听 `127.0.0.1`，不宜直接暴露到公网。Postgres 地址可配置，但不代表已完成实库部署验证。

运行数据默认保存在 `data/` 下，包括业务数据库 `app.db`、会话数据库 `briefly.db`、追踪快照及可选的飞书用户令牌。请妥善备份并保护本机数据；`.env`、数据库、令牌和追踪数据不应提交到 Git。

## 测试

默认回归使用假模型、模拟飞书响应及 SMTP 替身，不调用真实外部服务：

```bash
python -m pytest -W error
```

实际浏览器回归需单独安装并显式运行：

```bash
python -m pip install -e ".[dev,browser]"
python -m playwright install chromium
python -m pytest tests/browser --run-browser -W error
```

也可设置 `BRIEFLY_BROWSER_CHANNEL=msedge` 使用本机 Edge。默认回归跳过浏览器用例，跳过不代表网页已验证。

真实模型评测需配置可用 Key，**会发送测试内容并产生 API 费用**，仅在手动执行时运行：

```bash
python tests/eval/run_eval.py -n 3
python tests/eval/run_eval.py -n 3 -k M1 M2 M3 M4 M5
python tests/eval/journey_browser.py --real-model -n 2
```

用例覆盖五项能力、异常输入、提示词注入、多轮偏好和指令切换、长材料、周报规则及助理到业务的交接。通过率与模型、配置和用例范围有关，不能将某批次结果视为任意输入的质量保证。

## 技术结构

技术栈：Python、LangChain / LangGraph、OpenAI 兼容模型接口、FastAPI、SQLAlchemy、SQLite、原生 HTML / JavaScript / CSS；解析使用 Docling、pdfplumber、pypdf、openpyxl、xlrd 和 Apache POI / Tika（旧版 Office 直读），图片文字识别使用 RapidOCR；知识库使用 fastembed（BGE-small 中文）、sqlite-vec 与 SQLite FTS5；飞书通过 httpx 调用 REST API。

```text
src/briefly/
├── agent.py              单智能体与工具调用、中间件
├── llm.py                统一模型工厂
├── skills/               五项能力注册
├── prompts/              角色、能力、提取与合成提示词
├── tools/                日期、材料、历史回查、飞书及业务只读工具
├── context_builder.py    上下文组装与预算
├── compaction.py         会话记忆压缩
├── output_check.py       回答检查与一次纠正
├── intent.py             三层意图漏斗（规则、本地语义与知识库路由、助理兜底）
├── knowledge/            知识库：入库、切片、索引、检索、依据包与问答
├── workspace_index.py    工作区语义索引
├── links.py              工作区与知识库的文档关联
├── loaders.py            文件解析入口
├── pdf.py / ocr.py       PDF 解析与本地图片文字识别
├── materials.py          材料存储、分块与检索
├── summarize.py          长材料分块提炼与合成
├── handoffs.py           助理结果到业务的预览和确认
├── todos/                两级待办、材料关联与进展
├── tracking/             文件快照、差异与定时扫描
├── weekly/               周报整理、审核、生成与版本
├── feishu/               飞书集成与 OAuth
├── mail.py               SMTP 周报发送
├── api.py                FastAPI 与网页服务
├── cli.py                命令行入口
└── web/                  五页工作台
tests/
├── test_*.py             单元与接口回归
├── browser/              实际浏览器、假模型回归
└── eval/                 手动真实模型评测与虚构素材
```

新增能力时，在 `prompts/` 添加能力说明，并在 `skills/__init__.py` 注册；新增工具时，在 `tools/` 实现并挂载，同时说明调用条件。模型实例统一由 `llm.py` 创建。
