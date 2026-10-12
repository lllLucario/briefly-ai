"""FastAPI 后端。目前提供材料管理接口；对话、周报等接口随后续阶段加入。

接口函数用 def 而不是 async def：解析文件是耗时的同步操作，FastAPI 会放进线程池执行，不阻塞其他请求。
"""
from datetime import datetime
from typing import Annotated

from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import config
from .db import create_db_engine
from .materials import Material, MaterialNotFound, MaterialStore


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


def create_app(store: MaterialStore | None = None) -> FastAPI:
    app = FastAPI(title="Briefly AI", description="办公 AI Agent 后端")
    app.state.store = store or MaterialStore(create_db_engine())

    @app.exception_handler(MaterialNotFound)
    def not_found(request: Request, exc: MaterialNotFound):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ValueError)
    def bad_request(request: Request, exc: ValueError):
        # 内容为空、格式不支持、扫描件、文件损坏等，消息本身就是给用户看的中文说明
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    Store = Annotated[MaterialStore, Depends(lambda: app.state.store)]

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

    @app.post("/materials", response_model=CreatedMaterial, status_code=201, summary="粘贴文本创建材料")
    def add_text(body: MaterialIn, store: Store, response: Response):
        material, is_new = store.add_text(body.title, body.content)
        return created(store, material, is_new, response)

    @app.post("/materials/upload", response_model=CreatedMaterial, status_code=201, summary="上传文件创建材料")
    def upload(
        file: Annotated[UploadFile, File(description="txt/md/docx/pdf/xlsx/csv")], store: Store, response: Response
    ):
        if not file.filename:
            raise HTTPException(status_code=400, detail="缺少文件名")
        material, is_new = store.add_file(file.filename, file.file.read())
        return created(store, material, is_new, response)

    @app.get("/materials", response_model=list[MaterialOut], summary="材料列表（不含正文）")
    def list_materials(store: Store):
        chunks, sections = store.chunk_counts(), store.section_counts()
        return [summary(store, m, chunks, sections) for m in store.list_materials()]

    @app.get("/materials/{material_id}", response_model=MaterialDetail, summary="材料详情：正文、大纲、解析报告")
    def get_material(material_id: int, store: Store):
        m = store.get(material_id)
        sections = [SectionOut(index=s.index, level=s.level, title=s.title, path=s.path) for s in store.sections(m.id)]
        return MaterialDetail(
            **summary(store, m).model_dump(), content=m.content, sections=sections, parse_report=m.parse_report
        )

    @app.delete("/materials/{material_id}", status_code=204, summary="删除材料")
    def delete_material(material_id: int, store: Store):
        store.delete(material_id)

    return app


def serve() -> None:
    """命令行入口 briefly-api：启动后端服务。"""
    import uvicorn

    uvicorn.run(create_app(), host=config.HOST, port=config.PORT)
