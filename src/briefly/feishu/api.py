"""飞书相关接口：配置状态、用户授权（OAuth）、云空间浏览与搜索、粘贴链接导入材料、周报写入飞书文档。"""
from collections.abc import Callable
from datetime import datetime

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from .. import config
from urllib.parse import quote

from .client import FeishuClient, FeishuError, FeishuNotConfigured, client_from_config, configured, with_user
from .drive import list_folder, search
from .oauth import FeishuNotAuthorized, UserTokens, authorize_url, fetch_user_info, redirect_uri
from .service import export_weekly, exports_of, import_link


class FeishuStatus(BaseModel):
    configured: bool  # 已填写 App ID / Secret
    notify_user: bool  # 已设置接收提醒、写入文档后加为协作者的账号
    folder: bool  # 已设置写入文档的文件夹
    user: dict | None = None  # 已连接的飞书账号 {name, open_id}；未连接为空
    redirect_uri: str = ""  # 需要在开发者后台“安全设置”里登记的回调地址


class LinkIn(BaseModel):
    url: str


class ExportOut(BaseModel):
    id: int
    report_id: int
    document_id: str
    url: str
    title: str
    shared_with: str
    share_error: str  # 加协作者失败等提示；文档已写入
    created_at: datetime


def register(
    app: FastAPI, client_factory: Callable[[], FeishuClient] = client_from_config, tokens: UserTokens | None = None
) -> None:
    """client_factory 创建应用身份的客户端，tokens 是用户授权的令牌存储；都可注入，测试时用模拟响应。"""
    from ..api import CreatedMaterial

    tokens = tokens or UserTokens()

    def reader():
        """连接了飞书账号时先用用户身份，没权限再用应用身份。"""
        return with_user(client_factory(), tokens)

    def user_client() -> FeishuClient:
        if tokens.connected() is None:
            raise FeishuNotAuthorized("还没有连接飞书账号：请先点击“连接飞书”")
        return client_factory().as_user(tokens)

    app.state.feishu_reader = reader  # 追踪、对话工具也用同一个

    @app.exception_handler(FeishuNotConfigured)
    def not_configured(request: Request, exc: FeishuNotConfigured):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(FeishuNotAuthorized)
    def not_authorized(request: Request, exc: FeishuNotAuthorized):
        return JSONResponse(status_code=401, content={"detail": str(exc)})

    @app.exception_handler(FeishuError)
    def feishu_error(request: Request, exc: FeishuError):
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    @app.get("/feishu/status", response_model=FeishuStatus, summary="飞书是否已配置")
    def feishu_status():
        is_default = client_factory is client_from_config
        return FeishuStatus(
            configured=configured() if is_default else True,
            notify_user=bool(config.FEISHU_NOTIFY_USER),
            folder=bool(config.FEISHU_FOLDER_TOKEN),
            user=tokens.connected(),
            redirect_uri=redirect_uri(),
        )

    # ---- 用户授权
    @app.get("/feishu/oauth/login", summary="跳转到飞书授权页（连接飞书账号）", include_in_schema=False)
    def oauth_login():
        if client_factory is client_from_config and not configured():
            raise FeishuNotConfigured("未配置飞书应用：请在 .env 中填写 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
        return RedirectResponse(authorize_url())

    @app.get("/feishu/oauth/callback", summary="飞书授权回调", include_in_schema=False)
    def oauth_callback(code: str = "", state: str = "", error: str = ""):
        if error or not code:
            return RedirectResponse(f"/?feishu_error={quote(error or '没有拿到授权码')}#materials")
        try:
            with client_factory() as client:
                info = tokens.exchange(code, state, lambda token: fetch_user_info(client, token))
        except FeishuError as e:
            return RedirectResponse(f"/?feishu_error={quote(str(e)[:300])}#materials")
        return RedirectResponse(f"/?feishu=connected&name={quote(info.get('name', ''))}#materials")

    @app.post("/feishu/oauth/logout", summary="断开飞书账号（删除本机保存的用户令牌）")
    def oauth_logout():
        tokens.clear()
        return {"ok": True}

    # ---- 云空间（用户身份）
    @app.get("/feishu/drive/files", summary="列出我的云空间文件夹内容（不传 folder 为根目录）")
    def drive_files(folder: str = ""):
        with user_client() as client:
            return [x.to_dict() for x in list_folder(client, folder)]

    @app.get("/feishu/drive/search", summary="搜索我能看到的云文档和知识库")
    def drive_search(q: str):
        with user_client() as client:
            return [x.to_dict() for x in search(client, q)]

    @app.post("/materials/feishu", response_model=CreatedMaterial, status_code=201, summary="粘贴飞书文档或多维表格链接，读取当前内容存为材料")
    def add_feishu(body: LinkIn, response: Response):
        store = app.state.store
        with reader() as client:
            material, is_new = import_link(client, store, body.url)
        response.status_code = status.HTTP_201_CREATED if is_new else status.HTTP_200_OK
        return app.state.created_material(material, is_new)

    @app.post("/weekly-reports/{report_id}/feishu", response_model=ExportOut, status_code=201, summary="把周报写成一篇新的飞书文档")
    def export_to_feishu(report_id: int):
        app.state.weekly.export_markdown(report_id)  # 不存在、未完成时先报错，不新建空文档
        with reader() as client:
            export = export_weekly(client, app.state.weekly, report_id, me=(tokens.connected() or {}).get("open_id", ""))
        return ExportOut.model_validate(export, from_attributes=True)

    @app.get("/weekly-reports/{report_id}/feishu", response_model=list[ExportOut], summary="周报写入飞书的记录")
    def list_exports(report_id: int):
        app.state.weekly.get(report_id)
        return [ExportOut.model_validate(e, from_attributes=True) for e in exports_of(app.state.store.engine, report_id)]
