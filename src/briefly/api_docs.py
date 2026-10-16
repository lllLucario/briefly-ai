"""接口文档（/docs）的分组：每个接口用 tags 标明属于哪一组，组的顺序和说明在 OPENAPI_TAGS。"""

CHAT = "对话"
MATERIALS = "材料"
WEEKLY = "周报"
MAIL = "邮件"
FEISHU = "飞书"
TRACKING = "文件追踪：集合与扫描"
SCHEDULE = "文件追踪：定时与提醒"
REVIEW = "文件追踪：周报准备与审核"

OPENAPI_TAGS = [
    {"name": CHAT, "description": "五项能力的对话（多轮记忆，按 thread_id 区分会话），以及会话列表、历史、上下文用量。"},
    {"name": MATERIALS, "description": "材料库：粘贴文本或上传 txt / md / docx / pdf / xlsx / csv，自动识别章节并分块。飞书链接的导入见“飞书”。"},
    {"name": WEEKLY, "description": "周报：选材料、写简述后后台生成（轮询详情看进度），可修改、导出 Markdown。写入飞书见“飞书”，邮件发送见“邮件”。"},
    {"name": MAIL, "description": "用 .env 中配置的 SMTP 邮箱发送周报；失败不自动重试，每次发送都有记录。"},
    {"name": FEISHU, "description": "飞书（可选）：用户授权与云空间浏览、搜索，粘贴文档 / 知识库 / 多维表格 / 妙记链接导入为材料，周报写入飞书文档。"},
    {"name": TRACKING, "description": "文件追踪：集合、署名、追踪源（本地文件、文件夹、飞书文档）、被追踪的文件、手动扫描。"},
    {"name": SCHEDULE, "description": "每周定时准备周报（到点最终扫描并生成候选清单），以及给用户的提醒。"},
    {"name": REVIEW, "description": "周报准备：生成候选工作记录 → 确认 / 排除 / 修改 / 补充 / 合并 / 拆分 → 确认清单 → 用已确认的记录生成周报。"},
]

DESCRIPTION = """办公 AI Agent 后端：对话、材料、周报，以及可选的飞书、邮件和文件追踪。

- 网页在根路径 `/`；用 `http://127.0.0.1:端口` 打开（飞书授权回调按 127.0.0.1 登记）。
- 会调用模型的接口：对话、生成周报、周报准备、用已确认记录生成周报；其余接口不调用模型。
- 错误统一返回 `{"detail": "中文说明"}`：400 输入有误或未配置，401 未连接飞书账号，404 不存在，409 正在扫描，502 模型或飞书接口出错。
"""

# /docs 页面：分组默认折叠（只显示组名和说明），不在页面底部列出全部数据结构（各接口里仍可展开查看）
SWAGGER_UI = {"docExpansion": "none", "defaultModelsExpandDepth": -1}
