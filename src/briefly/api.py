"""FastAPI 后端：对话、待办、材料与追踪、周报，并在根路径提供网页（src/briefly/web）。

接口函数用 def 而不是 async def：解析文件是耗时的同步操作，FastAPI 会放进线程池执行，不阻塞其他请求。
"""
import logging
import re
import threading
from collections import defaultdict
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from pydantic import BaseModel

from . import api_docs as docs
from . import config, mail
from .agent import Agent, new_thread_id, sqlite_checkpointer
from .context_builder import SYSTEM_REPLY
from .db import create_db_engine
from .feishu import FeishuClient, client_from_config
from .llm import create_chat_model
from . import material_library as library
from .materials import TRACKED, Material, MaterialNotFound, MaterialStore
from .sessions import ChatSession, SessionStore
from .skills import SKILLS
from .weekly.service import WeeklyService

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"
USAGE_FIELDS = [  # 上下文用量里给网页看的字段，与 CLI /context 一致
    "estimated_tokens",
    "input_tokens",
    "context_limit",
    "system_tokens",
    "tool_schema_tokens",
    "memory_tokens",
    "history_tokens",
    "history_turns",
    "current_tokens",
    "material_tokens",
    "tool_result_tokens",
    "omitted_turns",
]


class MaterialIn(BaseModel):
    title: str = ""
    content: str


class SectionOut(BaseModel):
    index: int
    level: int
    title: str
    path: str


class RelatedTodo(BaseModel):
    id: int
    title: str
    parent_id: int | None
    parent_title: str | None
    archived: bool
    relation: str  # direct 直接关联；shared 来自父待办的共享材料
    from_todo_id: int  # 关联实际所在的待办（共享时是父待办）


class MaterialOut(BaseModel):
    id: int
    title: str
    source: str
    file_type: str | None
    char_count: int
    chunk_count: int
    section_count: int
    structure: str | None  # 章节来源：font_size / bookmarks / docx_styles / sheets / markdown / rules / none
    warnings: list[str]
    created_at: datetime
    feishu: dict | None = None  # 从飞书导入的材料：{kind, token, url}，用于开启追踪
    tracked: dict | None = None  # 追踪中文件的最新版本 {file_id, key}，随追踪更新
    # 列表与选择器用的只读摘要（见 material_library）
    preview: str = ""  # 正文开头，合并空白后最多 120 字
    source_category: str | None = None  # 来源：file 本机文件 / paste 粘贴文字 / feishu 飞书（与追踪状态无关）
    # 更新方式：fixed 没有在追踪；tracked 追踪中；paused 追踪已暂停；error 原文件缺失或无法访问
    tracking_state: str | None = None
    tracking_note: str = ""  # 暂停、异常的说明
    related_todos: list[RelatedTodo] = []  # 关联的待办：直接关联在前，子待办含父待办名称


class LibraryPage(BaseModel):
    items: list[MaterialOut]
    total: int  # 符合条件的材料总数
    offset: int
    limit: int


class MaterialDetail(MaterialOut):
    content: str
    sections: list[SectionOut]
    parse_report: dict


class CreatedMaterial(BaseModel):
    material: MaterialOut
    created: bool  # False 表示内容相同的材料已存在，返回的是已有的那条


class SkillOut(BaseModel):
    name: str
    description: str


class ChatIn(BaseModel):
    text: str
    thread_id: str | None = None  # 不填则开始新会话
    material_ids: list[int] = []
    skill: str | None = None  # 指定能力；不填由模型判断


class ChatOut(BaseModel):
    thread_id: str
    reply: str


class SessionOut(BaseModel):
    thread_id: str
    title: str
    created_at: datetime
    updated_at: datetime


class MessageOut(BaseModel):
    role: str  # user / assistant
    text: str
    material_ids: list[int] = []
    skill: str | None = None
    tools: list[str] = []  # 给出这条回答之前调用过的工具
    message_id: str | None = None
    saved_todo_ids: list[int] = []
    warnings: list[str] = []
    result_kinds: list[str] = []  # 只用于安排结果操作，不作为业务保存或事实依据
    system_reply: bool = False


class SessionDetail(SessionOut):
    messages: list[MessageOut]


class ContextOut(BaseModel):
    usage: dict | None  # 最近一次对话调用的上下文构成；没有调用记录时为空
    memory_version: int | None
    compacted_turns: int


class MailIn(BaseModel):
    to: str  # 收件人，逗号或分号分隔
    cc: str = ""


class MailOut(BaseModel):
    id: int
    report_id: int
    to: str
    cc: str
    subject: str
    status: str  # sent / failed
    error: str
    created_at: datetime


class MailStatus(BaseModel):
    configured: bool
    sender: str  # 发件邮箱
    default_to: str  # 默认收件人


def default_agent(store: MaterialStore) -> Agent:
    return Agent(checkpointer=sqlite_checkpointer(config.DB_PATH), store=store)


def visible_messages(messages: list[AnyMessage]) -> list[MessageOut]:
    """checkpointer 里的完整历史 → 网页显示的消息：用户的原始输入、模型的最终回答，工具交互只记名称。"""
    out: list[MessageOut] = []
    tools: list[str] = []
    skill = None
    for m in messages:
        if isinstance(m, HumanMessage):
            meta = m.additional_kwargs
            if meta.get("internal"):
                continue
            skill = meta.get("skill")
            out.append(
                MessageOut(
                    role="user",
                    text=meta.get("display", m.text),
                    material_ids=meta.get("material_ids") or [],
                    skill=meta.get("skill"),
                )
            )
            tools = []
        elif isinstance(m, AIMessage):
            if m.tool_calls:
                tools += [c["name"] for c in m.tool_calls]
            elif m.text:
                warnings = []
                if m.response_metadata.get("internal_terms"):
                    warnings.append("回答中仍含有未能清除的处理信息，请核对原文；可以重试原任务或补充要求。")
                if m.response_metadata.get("missing_conflicts"):
                    warnings.append("回答可能未完整保留材料中相互冲突的说法，请核对双方原文后再使用。")
                if m.response_metadata.get("fidelity_issues"):
                    warnings.append("回答仍可能遗漏重要事实、混淆事项对应的信息或改变原文章节编号，请核对原材料后再使用。")
                if m.response_metadata.get("date_issues"):
                    warnings.append(f"回答中的日期换算可能有误（{'；'.join(m.response_metadata['date_issues'])}），请按原文核对。")
                if m.response_metadata.get("length_limit"):
                    warnings.append(f"回答仍超过你要求的 {m.response_metadata['length_limit']} 字上限（当前 {m.response_metadata['actual_chars']} 字），可以精简要求后重试。")
                system = bool(m.response_metadata.get(SYSTEM_REPLY))
                # 仅识别栏目以安排界面动作；不从 Markdown 提取任务或保存业务事实。
                markers = {"summarize": "一句话总结", "minutes": "会议信息|行动项", "weekly": "周报标题", "todo": "待办清单", "polish": "优化后"}
                kinds = [k for k, marker in markers.items() if re.search(rf"(?m)^\s*(?:#{{1,6}}\s*)?(?:\*\*)?【(?:{marker})】", m.text)] if not system else []
                out.append(MessageOut(role="assistant", text=m.text, tools=tools, message_id=m.id,
                                      skill=skill, warnings=warnings, result_kinds=kinds, system_reply=system))
                tools = []
    return out


def create_app(
    store: MaterialStore | None = None,
    model_factory: Callable[[], BaseChatModel] = create_chat_model,
    agent_factory: Callable[[MaterialStore], Agent] = default_agent,
    tracking_scheduler: bool = False,
    feishu_factory: Callable[[], FeishuClient] = client_from_config,
    feishu_tokens=None,
) -> FastAPI:
    """store、model_factory、agent_factory 可注入，便于测试用内存库和假模型。智能体在第一次用到时才创建。

    tracking_scheduler：是否在本进程内启动定时线程（每日扫描、每周自动整理；briefly-api 按配置开启，测试默认关闭）；
    开启时若配置了飞书，提醒同时由飞书机器人发送。feishu_factory 创建应用身份的飞书客户端，
    feishu_tokens 是飞书用户授权的令牌存储（默认 data/feishu_user_token.json），测试时注入模拟响应。
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        scheduler = None
        if tracking_scheduler:
            from .feishu.messages import register_channel
            from .weekly.schedule import BackgroundScheduler

            register_channel()
            scheduler = BackgroundScheduler(app.state.weekly)
            scheduler.start()
        app.state.scheduler = scheduler
        yield
        if scheduler is not None:
            scheduler.stop()
        from .feishu.client import close_shared

        close_shared()  # 关闭共用飞书客户端的连接池

    app = FastAPI(
        title="Briefly AI",
        description=docs.DESCRIPTION,
        openapi_tags=docs.OPENAPI_TAGS,
        swagger_ui_parameters=docs.SWAGGER_UI,
        lifespan=lifespan,
    )
    app.state.store = store or MaterialStore(create_db_engine())
    app.state.sessions = SessionStore(app.state.store.engine)
    app.state.agent = None
    agent_lock = threading.Lock()
    thread_locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)  # 同一会话的提问依次处理

    def get_agent() -> Agent:
        with agent_lock:
            if app.state.agent is None:
                app.state.agent = agent_factory(app.state.store)
            return app.state.agent

    @app.exception_handler(MaterialNotFound)
    def not_found(request: Request, exc: MaterialNotFound):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    def bad_request(request: Request, exc: ValueError):
        # 内容为空、格式不支持、扫描件、文件损坏等，消息本身就是给用户看的中文说明
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    Store = Annotated[MaterialStore, Depends(lambda: app.state.store)]
    Weekly = Annotated[WeeklyService, Depends(lambda: app.state.weekly)]
    Sessions = Annotated[SessionStore, Depends(lambda: app.state.sessions)]
    AgentDep = Annotated[Agent, Depends(get_agent)]

    @app.get("/skills", tags=[docs.CHAT], response_model=list[SkillOut], summary="可指定的能力")
    def list_skills():
        return [SkillOut(name=s.name, description=s.description) for s in SKILLS.values()]

    def session_out(s: ChatSession) -> SessionOut:
        return SessionOut.model_validate(s, from_attributes=True)

    @app.post("/chat", tags=[docs.CHAT], response_model=ChatOut, summary="对话（等回答完成后返回；不传 thread_id 则开始新会话）")
    def chat(body: ChatIn, agent: AgentDep, sessions: Sessions):
        if not body.text.strip():
            raise ValueError("输入不能为空")
        if body.skill and body.skill not in SKILLS:
            raise ValueError(f"未知能力: {body.skill}，可选: {', '.join(SKILLS)}")
        if body.thread_id:
            sessions.get(body.thread_id)  # 不存在时 404
        thread_id = body.thread_id or new_thread_id()
        with thread_locks[thread_id]:
            try:
                reply = agent.chat(body.text, body.skill, material_ids=body.material_ids or None, thread_id=thread_id)
            except (ValueError, MaterialNotFound):
                raise
            except Exception as e:  # 模型接口出错（网络、密钥、限流等）
                log.exception("对话失败")
                raise HTTPException(status_code=502, detail=f"模型调用失败：{e}") from e
            sessions.touch(thread_id, body.text)
        return ChatOut(thread_id=thread_id, reply=reply)

    @app.get("/chat/sessions", tags=[docs.CHAT], response_model=list[SessionOut], summary="会话列表（最近的在前）")
    def list_sessions(sessions: Sessions):
        return [session_out(s) for s in sessions.list_sessions()]

    @app.get("/chat/sessions/{thread_id}", tags=[docs.CHAT], response_model=SessionDetail, summary="会话历史")
    def get_session(thread_id: str, sessions: Sessions, agent: AgentDep):
        s = session_out(sessions.get(thread_id))
        messages = visible_messages(agent.history_of(thread_id))
        from sqlalchemy import select
        from sqlalchemy.orm import Session
        from .handoffs import TodoImportDraft
        with Session(app.state.store.engine) as db:
            drafts = list(db.scalars(select(TodoImportDraft).where(TodoImportDraft.thread_id == thread_id, TodoImportDraft.receipt.is_not(None))))
            for message in messages:
                message.saved_todo_ids = list(dict.fromkeys(i for d in drafts if d.data["message_id"] == message.message_id and d.receipt for i in d.receipt["todo_ids"]))
        return SessionDetail(**s.model_dump(), messages=messages)

    @app.get("/chat/sessions/{thread_id}/context", tags=[docs.CHAT], response_model=ContextOut, summary="上下文用量")
    def get_context(thread_id: str, sessions: Sessions, agent: AgentDep):
        sessions.get(thread_id)
        rows = [r for r in agent.context_store.usage(thread_id=thread_id, limit=20) if r.task_type == "chat"]
        memory = agent.context_store.latest_memory(thread_id)
        return ContextOut(
            usage={k: getattr(rows[0], k) for k in USAGE_FIELDS} if rows else None,
            memory_version=memory.version if memory else None,
            compacted_turns=memory.data.get("compacted_turns", 0) if memory else 0,
        )

    @app.delete("/chat/sessions/{thread_id}", tags=[docs.CHAT], status_code=204, summary="删除会话")
    def delete_session(thread_id: str, sessions: Sessions, agent: AgentDep):
        sessions.delete(thread_id)
        agent.graph.checkpointer.delete_thread(thread_id)

    def tracked_versions() -> dict[int, dict]:
        from .tracking.service import latest_materials

        return latest_materials(app.state.store.engine)

    def summary(
        store: MaterialStore,
        m: Material,
        chunks: dict | None = None,
        sections: dict | None = None,
        tracked: dict | None = None,
        described: tuple[dict, dict] | None = None,
    ):
        """chunks / sections / tracked / described（library.describe 的结果）为批量查好的；不传时单独查这一份。"""
        chunks = store.chunk_counts([m.id]) if chunks is None else chunks
        sections = store.section_counts([m.id]) if sections is None else sections
        tracked = tracked_versions() if tracked is None else tracked
        info, previews = library.describe(store.engine, [m.id], include_archived=True) if described is None else described
        x = info.get(m.id)
        return MaterialOut(
            id=m.id,
            title=m.title,
            source=m.source,
            file_type=m.file_type,
            char_count=m.char_count,
            chunk_count=chunks.get(m.id, 0),
            section_count=sections.get(m.id, 0),
            structure=m.parse_report.get("structure"),
            warnings=m.parse_report.get("warnings", []),
            created_at=m.created_at,
            feishu={k: v for k, v in feishu.items() if k in ("kind", "token", "url")}
            if (feishu := m.parse_report.get("feishu"))
            else None,
            tracked=tracked.get(m.id),
            preview=previews.get(m.id, ""),
            source_category=x.source_category if x else None,
            tracking_state=x.tracking_state if x else None,
            tracking_note=x.tracking_note if x else "",
            related_todos=x.related_todos if x else [],
        )

    def created(store: MaterialStore, m: Material, is_new: bool, response: Response) -> CreatedMaterial:
        response.status_code = status.HTTP_201_CREATED if is_new else status.HTTP_200_OK
        return CreatedMaterial(material=summary(store, m), created=is_new)

    app.state.created_material = lambda m, is_new: CreatedMaterial(material=summary(app.state.store, m), created=is_new)

    @app.post("/materials", tags=[docs.MATERIALS], response_model=CreatedMaterial, status_code=201, summary="粘贴文本创建材料")
    def add_text(body: MaterialIn, store: Store, response: Response):
        material, is_new = store.add_text(body.title, body.content)
        return created(store, material, is_new, response)

    @app.post("/materials/upload", tags=[docs.MATERIALS], response_model=CreatedMaterial, status_code=201, summary="上传文件创建材料")
    def upload(
        file: Annotated[UploadFile, File(description="txt/md/docx/pdf/xlsx/csv")], store: Store, response: Response
    ):
        if not file.filename:
            raise HTTPException(status_code=400, detail="缺少文件名")
        material, is_new = store.add_file(file.filename, file.file.read())
        return created(store, material, is_new, response)

    @app.get("/materials", tags=[docs.MATERIALS], response_model=list[MaterialOut], summary="材料列表（不含正文）")
    def list_materials(store: Store):
        chunks, sections, tracked = store.chunk_counts(), store.section_counts(), tracked_versions()
        materials = store.list_materials(content=False)
        described = library.describe(store.engine, [m.id for m in materials])
        return [summary(store, m, chunks, sections, tracked, described) for m in materials]

    # 静态路径要注册在 /materials/{material_id} 之前
    @app.get("/materials/library", tags=[docs.MATERIALS], response_model=LibraryPage, summary="材料库浏览：标题搜索、筛选、排序、分页")
    def library_page(
        store: Store,
        q: Annotated[str, Query(max_length=200, description="标题子串，不区分英文大小写")] = "",
        source: Literal["all", "file", "paste", "feishu"] = "all",
        tracking: Literal["all", "tracked", "fixed"] = "all",
        relation: Literal["all", "unlinked", "todo"] = "all",
        todo_id: int | None = None,
        include_archived: bool = False,
        sort: Literal["newest", "oldest", "title"] = "newest",
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        material_ids: Annotated[list[int] | None, Query(description=f"限定材料编号，最多 {library.MAX_IDS} 个")] = None,
    ):
        ids, total, info, previews = library.browse(
            store.engine, q=q, source=source, tracking=tracking, relation=relation, todo_id=todo_id,
            include_archived=include_archived, sort=sort, offset=offset, limit=limit, material_ids=material_ids,
        )  # fmt: skip
        materials = {m.id: m for m in store.list_materials(ids, content=False)}
        chunks, sections, tracked = store.chunk_counts(ids), store.section_counts(ids), tracked_versions()
        items = [summary(store, materials[i], chunks, sections, tracked, (info, previews)) for i in ids]
        return LibraryPage(items=items, total=total, offset=offset, limit=limit)

    @app.get("/materials/{material_id}", tags=[docs.MATERIALS], response_model=MaterialDetail, summary="材料详情：正文、大纲、解析报告")
    def get_material(material_id: int, store: Store):
        m = store.get(material_id)
        sections = [SectionOut(index=s.index, level=s.level, title=s.title, path=s.path) for s in store.sections(m.id)]
        return MaterialDetail(
            **summary(store, m).model_dump(), content=m.content, sections=sections, parse_report=m.parse_report
        )

    @app.delete("/materials/{material_id}", tags=[docs.MATERIALS], status_code=204, summary="删除材料（被待办关联时需 force）")
    def delete_material(material_id: int, store: Store, force: bool = False):
        if store.get(material_id).source == TRACKED:
            raise ValueError("这是追踪中文档的最新版本，随追踪自动更新；不需要了请停止追踪")
        store.delete(material_id, force=force)  # 被待办关联时返回 409（confirm），清理规则见 MaterialStore.delete

    app.state.mail_connect = mail._connect  # 测试时替换成模拟的 SMTP

    @app.get("/mail/status", tags=[docs.MAIL], response_model=MailStatus, summary="邮件是否已配置")
    def mail_status():
        return MailStatus(configured=mail.configured(), sender=config.SMTP_USER, default_to=config.MAIL_DEFAULT_TO)

    @app.post("/weekly/{report_id}/email", tags=[docs.MAIL], response_model=MailOut, status_code=201, summary="把周报通过邮件发给收件人（失败不自动重试）")
    def email_weekly(report_id: int, body: MailIn, weekly: Weekly):
        record = mail.send_weekly(weekly, report_id, body.to, body.cc, connect=app.state.mail_connect)
        return MailOut.model_validate(record, from_attributes=True)

    @app.get("/weekly/{report_id}/email", tags=[docs.MAIL], response_model=list[MailOut], summary="周报的邮件发送记录")
    def list_emails(report_id: int, weekly: Weekly):
        weekly.report(report_id)
        return [MailOut.model_validate(x, from_attributes=True) for x in mail.sends_of(app.state.store.engine, report_id)]

    @app.middleware("http")
    async def revalidate_web(request: Request, call_next):
        """网页文件每次按 ETag 校验，更新后刷新即可生效，不用清浏览器缓存。"""
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/web/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    from .feishu.api import register as register_feishu
    from .todos.api import register as register_todos
    from .tracking.api import register as register_tracking
    from .tracking.service import TrackingService
    from .weekly.api import register as register_weekly

    register_feishu(app, feishu_factory, feishu_tokens)  # 飞书：用户授权、云空间、导入链接、周报写入飞书文档
    # 追踪与周报共用一个追踪服务；飞书文档连接了账号时先用用户身份读取
    app.state.tracking = TrackingService(app.state.store.engine, app.state.store, feishu_factory=app.state.feishu_reader)
    app.state.weekly = WeeklyService(app.state.store, model_factory, tracking=app.state.tracking)
    register_tracking(app)  # 追踪：追踪源、文件、版本与差异、检查更新、材料的追踪开关
    register_weekly(app)  # 周报：一周一份，审核清单 → 生成；定时、设置、提醒
    register_todos(app)  # 待办：父子结构、材料关联、活动；进展进入周报的审核清单
    from .handoffs import register as register_handoffs
    register_handoffs(app, get_agent, thread_locks, model_factory)

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")
    return app


def serve() -> None:
    """命令行入口 briefly-api：启动后端服务。"""
    import uvicorn

    uvicorn.run(create_app(tracking_scheduler=config.TRACKING_SCHEDULER), host=config.HOST, port=config.PORT)
