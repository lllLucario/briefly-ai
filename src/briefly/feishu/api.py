"""飞书相关接口：配置状态、粘贴链接导入材料、周报写入飞书文档。"""
from collections.abc import Callable
from datetime import datetime

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import config
from .client import FeishuClient, FeishuError, FeishuNotConfigured, client_from_config, configured
from .service import export_weekly, exports_of, import_link


class FeishuStatus(BaseModel):
    configured: bool  # 已填写 App ID / Secret
    notify_user: bool  # 已设置接收提醒、写入文档后加为协作者的账号
    folder: bool  # 已设置写入文档的文件夹


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


def register(app: FastAPI, client_factory: Callable[[], FeishuClient] = client_from_config) -> None:
    """client_factory 可注入，测试时用模拟响应。"""
    from ..api import CreatedMaterial

    @app.exception_handler(FeishuNotConfigured)
    def not_configured(request: Request, exc: FeishuNotConfigured):
        return JSONResponse(status_code=400, content={"detail": str(exc)})

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
        )

    @app.post("/materials/feishu", response_model=CreatedMaterial, status_code=201, summary="粘贴飞书文档或多维表格链接，读取当前内容存为材料")
    def add_feishu(body: LinkIn, response: Response):
        store = app.state.store
        with client_factory() as client:
            material, is_new = import_link(client, store, body.url)
        response.status_code = status.HTTP_201_CREATED if is_new else status.HTTP_200_OK
        return app.state.created_material(material, is_new)

    @app.post("/weekly-reports/{report_id}/feishu", response_model=ExportOut, status_code=201, summary="把周报写成一篇新的飞书文档")
    def export_to_feishu(report_id: int):
        app.state.weekly.export_markdown(report_id)  # 不存在、未完成时先报错，不新建空文档
        with client_factory() as client:
            export = export_weekly(client, app.state.weekly, report_id)
        return ExportOut.model_validate(export, from_attributes=True)

    @app.get("/weekly-reports/{report_id}/feishu", response_model=list[ExportOut], summary="周报写入飞书的记录")
    def list_exports(report_id: int):
        app.state.weekly.get(report_id)
        return [ExportOut.model_validate(e, from_attributes=True) for e in exports_of(app.state.store.engine, report_id)]
