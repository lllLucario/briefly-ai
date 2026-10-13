# Briefly AI

办公 AI Agent：文档总结、会议纪要拆解、周报生成、待办梳理、文本优化。

单智能体，基于 LangChain / LangGraph，模型走 OpenAI 兼容接口（默认 DeepSeek）。重点放在**内容可靠**：材料中没有的不编造，周报和总结的每一条都能追溯到原文，处理不了的明确说明而不是悄悄略过。

## 功能

- **五项能力**：一份总提示词加五份能力说明，由模型判断任务，也可以指定。支持多轮修改（“再短一点”“改成表格”）。
- **材料管理**：粘贴文本，或上传 txt / md / docx / pdf / xlsx / csv，统一转成带标题的 Markdown，识别章节、分块入库。
  - PDF：清理页眉页脚、水印、页码、乱码，合并排版换行；按字号、书签识别标题；扫描件明确提示。
  - Word：标题、列表、表格、脚注（docling）；Excel：工作表作为章节，合并单元格、百分比按显示处理。
- **长材料**：按大纲、章节、关键词按需读取；需要完整总结时由程序逐块提取（Map-Reduce），每条要点核对原文引用，不会漏读。
- **周报**：勾选素材、写一段简述，后台生成；每条带出处，导出时去掉；数字核对；漏写的要点、简述与材料的矛盾单独提醒，不进入周报正文。
- **上下文管理**：每次调用模型前统一组装与预算检查，早期对话压缩为结构化会话记忆（原始历史不改写），工具结果有单轮预算，过长的输入自动转存为材料。
- **后端**：FastAPI，材料与周报接口。

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

### 文件追踪：从文件变化准备周报

追踪文件和文件夹的版本变化，生成候选工作记录，由你确认后再交给周报生成器。

```bash
briefly track create 项目A --owner 张三      # 创建集合；--owner 是你在 Word 修订里的署名，用于归属判断
briefly track add 项目A D:\项目 方案.docx 进度.xlsx   # 混合添加文件和文件夹，立即保存基线
briefly track scan 项目A                     # 手动扫描（briefly-api 运行时每个工作日自动扫描）
briefly track diff <文件编号>                # 查看两个版本的差异
briefly track prepare 项目A                  # 周报前：最终扫描 + 生成候选工作记录
briefly track review 项目A                   # 审核：确认、排除、修改、补充、合并、拆分，最后 done 确认清单
briefly track report 项目A -o 周报.md        # 只用已确认的记录生成周报草稿
briefly track schedule 项目A --weekday 周五 --time 16:30   # 每周到点自动准备候选清单并提醒你审核
briefly track inbox                          # 查看提醒（网页顶部也会显示）
```

没有常驻 `briefly-api` 时，可以用系统计划任务定时运行 `briefly track run-due`；`briefly track prune` 清理旧版本（被工作记录引用的版本不会删除）。

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
├── weekly.py           周报：后台生成、出处、数字核对、提醒、导出
├── tracking/           文件追踪：快照、Diff、归属、候选工作记录、审核、每日扫描
├── api.py              FastAPI 后端（并提供 web/ 下的网页）
├── cli.py              命令行入口
├── config.py           环境变量配置
├── tools/              工具：日期、材料（列出、大纲、读取、检索、完整提炼）、会话（查原话、读回工具结果）
├── skills/             能力注册表
└── prompts/            base.md + 五个能力说明，以及提取、合成、压缩用的提示词
tests/                  单元测试；eval/ 为真实模型实测用例集
```

新增能力：在 `prompts/` 加 `<name>.md`，在 `skills/__init__.py` 登记一行。新增工具：在 `tools/` 用 `@tool` 写函数并登记，在 `base.md` 的“工具使用”里写明何时调用。
