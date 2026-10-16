"""FastAPI 后端：对话、材料管理、周报，并在根路径提供网页（src/briefly/web）。

接口函数用 def 而不是 async def：解析文件是耗时的同步操作，FastAPI 会放进线程池执行，不阻塞其他请求。
"""
import logging
import threading
from collections import defaultdict
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from pydantic import BaseModel

from . import api_docs as docs
from . import config, mail
from .agent import Agent, new_thread_id, sqlite_checkpointer
from .db import create_db_engine
from .feishu import FeishuClient, client_from_config
from .llm import create_chat_model
from .materials import Material, MaterialNotFound, MaterialStore
from .sessions import ChatSession, SessionStore
from .skills import SKILLS
from .weekly import WeeklyReport, WeeklyService

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


class MaterialDetail(MaterialOut):
    content: str
    sections: list[SectionOut]
    parse_report: dict


class CreatedMaterial(BaseModel):
    material: MaterialOut
    created: bool  # False 表示内容相同的材料已存在，返回的是已有的那条


class WeeklyIn(BaseModel):
    material_ids: list[int] = []
    brief: str = ""  # 用户写的“这周大概做了什么”，既是筛选条件也是来源
    period_start: date | None = None  # 不填为本周一
    period_end: date | None = None  # 不填为本周日
    audience: str | None = None  # 读者，默认直属上级
    name: str | None = None


class WeeklyContentIn(BaseModel):
    content: str


class WeeklyOut(BaseModel):
    id: int
    status: str  # pending / running / done / failed
    period_start: date
    period_end: date
    brief: str
    material_ids: list[int]
    options: dict
    error: str
    created_at: datetime
    updated_at: datetime


class WeeklyDetail(WeeklyOut):
    content: str  # 带出处标记〔1-3〕〔简述〕，导出时去掉
    items: list[dict]  # 工作项及原文，用于查看出处
    generation: dict  # 生成报告：处理块数、失败、丢弃、未核实数字、调用次数、耗时、警告（warnings）、给用户的提醒（reminders，不进入周报）


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


class SessionDetail(SessionOut):
    messages: list[MessageOut]


class ContextOut(BaseModel):
    usage: dict | None  # 最近一次对话调用的上下文构成；没有调用记录时为空
    memory_version: int | None
    compacted_turns: int


class NotificationOut(BaseModel):
    id: int
    kind: str
    title: str
    body: str
    collection_id: int | None
    prep_id: int | None
    created_at: datetime
    read_at: datetime | None


class ReadIn(BaseModel):
    ids: list[int] = []


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
    for m in messages:
        if isinstance(m, HumanMessage):
            meta = m.additional_kwargs
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
                out.append(MessageOut(role="assistant", text=m.text, tools=tools))
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

    tracking_scheduler：是否在本进程内启动文件追踪的每日扫描线程（briefly-api 按配置开启，测试默认关闭）；
    开启时若配置了飞书，提醒同时由飞书机器人发送。feishu_factory 创建应用身份的飞书客户端，
    feishu_tokens 是飞书用户授权的令牌存储（默认 data/feishu_user_token.json），测试时注入模拟响应。
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        scheduler = None
        if tracking_scheduler:
            from .tracking.collection import TrackingService
            from .tracking.scheduler import DailyScheduler

            from .feishu.messages import register_channel

            register_channel()
            scheduler = DailyScheduler(
                TrackingService(app.state.store.engine, app.state.store, feishu_factory=app.state.feishu_reader)
            )
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
    app.state.weekly = WeeklyService(app.state.store, model_factory)
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
        return SessionDetail(**s.model_dump(), messages=visible_messages(agent.history_of(thread_id)))

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

    def summary(store: MaterialStore, m: Material, chunks: dict | None = None, sections: dict | None = None):
        """chunks / sections 为批量查好的计数；不传时单独查这一份。"""
        chunks = store.chunk_counts([m.id]) if chunks is None else chunks
        sections = store.section_counts([m.id]) if sections is None else sections
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
        chunks, sections = store.chunk_counts(), store.section_counts()
        return [summary(store, m, chunks, sections) for m in store.list_materials()]

    @app.get("/materials/{material_id}", tags=[docs.MATERIALS], response_model=MaterialDetail, summary="材料详情：正文、大纲、解析报告")
    def get_material(material_id: int, store: Store):
        m = store.get(material_id)
        sections = [SectionOut(index=s.index, level=s.level, title=s.title, path=s.path) for s in store.sections(m.id)]
        return MaterialDetail(
            **summary(store, m).model_dump(), content=m.content, sections=sections, parse_report=m.parse_report
        )

    @app.delete("/materials/{material_id}", tags=[docs.MATERIALS], status_code=204, summary="删除材料")
    def delete_material(material_id: int, store: Store):
        store.delete(material_id)

    def weekly_out(r: WeeklyReport) -> WeeklyOut:
        return WeeklyOut.model_validate(r, from_attributes=True)

    @app.post("/weekly-reports", tags=[docs.WEEKLY], response_model=WeeklyOut, status_code=202, summary="提交周报生成任务（后台生成）")
    def create_weekly(body: WeeklyIn, weekly: Weekly, background: BackgroundTasks):
        report = weekly.create(
            body.material_ids, body.brief, body.period_start, body.period_end, body.audience, body.name
        )
        background.add_task(weekly.generate, report.id)
        return weekly_out(report)

    @app.get("/weekly-reports", tags=[docs.WEEKLY], response_model=list[WeeklyOut], summary="周报历史")
    def list_weekly(weekly: Weekly):
        return [weekly_out(r) for r in weekly.list_reports()]

    @app.get("/weekly-reports/{report_id}", tags=[docs.WEEKLY], response_model=WeeklyDetail, summary="周报详情（轮询看进度）")
    def get_weekly(report_id: int, weekly: Weekly):
        return WeeklyDetail.model_validate(weekly.get(report_id), from_attributes=True)

    @app.put("/weekly-reports/{report_id}", tags=[docs.WEEKLY], response_model=WeeklyDetail, summary="保存修改后的周报正文")
    def update_weekly(report_id: int, body: WeeklyContentIn, weekly: Weekly):
        return WeeklyDetail.model_validate(weekly.update_content(report_id, body.content), from_attributes=True)

    @app.delete("/weekly-reports/{report_id}", tags=[docs.WEEKLY], status_code=204, summary="删除周报")
    def delete_weekly(report_id: int, weekly: Weekly):
        weekly.delete(report_id)

    @app.get("/weekly-reports/{report_id}/export.md", tags=[docs.WEEKLY], response_class=PlainTextResponse, summary="导出 Markdown")
    def export_weekly(report_id: int, weekly: Weekly):
        filename, text = weekly.export_markdown(report_id)
        return PlainTextResponse(
            text,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
        )

    app.state.mail_connect = mail._connect  # 测试时替换成模拟的 SMTP

    @app.get("/mail/status", tags=[docs.MAIL], response_model=MailStatus, summary="邮件是否已配置")
    def mail_status():
        return MailStatus(configured=mail.configured(), sender=config.SMTP_USER, default_to=config.MAIL_DEFAULT_TO)

    @app.post("/weekly-reports/{report_id}/email", tags=[docs.MAIL], response_model=MailOut, status_code=201, summary="把周报通过邮件发给收件人（失败不自动重试）")
    def email_weekly(report_id: int, body: MailIn, weekly: Weekly):
        record = mail.send_weekly(weekly, report_id, body.to, body.cc, connect=app.state.mail_connect)
        return MailOut.model_validate(record, from_attributes=True)

    @app.get("/weekly-reports/{report_id}/email", tags=[docs.MAIL], response_model=list[MailOut], summary="周报的邮件发送记录")
    def list_emails(report_id: int, weekly: Weekly):
        weekly.get(report_id)
        return [MailOut.model_validate(x, from_attributes=True) for x in mail.sends_of(app.state.store.engine, report_id)]

    @app.get("/notifications", tags=[docs.SCHEDULE], response_model=list[NotificationOut], summary="提醒（文件追踪：周报已准备好、准备失败等）")
    def list_notifications(unread_only: bool = True):
        from .tracking.weekly_schedule import notifications

        return [NotificationOut.model_validate(n, from_attributes=True) for n in notifications(app.state.store.engine, unread_only)]

    @app.post("/notifications/read", tags=[docs.SCHEDULE], summary="标为已读（不传 ids 表示全部）")
    def read_notifications(body: ReadIn):
        from .tracking.weekly_schedule import mark_read

        return {"marked": mark_read(app.state.store.engine, body.ids or None)}

    @app.middleware("http")
    async def revalidate_web(request: Request, call_next):
        """网页文件每次按 ETag 校验，更新后刷新即可生效，不用清浏览器缓存。"""
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/web/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    from .feishu.api import register as register_feishu
    from .tracking.api import register as register_tracking

    register_feishu(app, feishu_factory, feishu_tokens)  # 飞书：用户授权、云空间、导入链接、周报写入飞书文档
    # 文件追踪：集合、扫描、定时、周报准备与审核；飞书文档连接了账号时先用用户身份读取
    register_tracking(app, model_factory, app.state.feishu_reader)

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/web", StaticFiles(directory=WEB_DIR), name="web")
    return app


def serve() -> None:
    """命令行入口 briefly-api：启动后端服务。"""
    import uvicorn

    uvicorn.run(create_app(tracking_scheduler=config.TRACKING_SCHEDULER), host=config.HOST, port=config.PORT)
