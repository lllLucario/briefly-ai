"""普通文件上传的大小限制：解析表单前先按请求体大小拦住，读文件时再按文件大小精确判断。知识库批量入口有自己的限制。"""
import re

from fastapi import HTTPException, UploadFile
from starlette.responses import JSONResponse

from . import config

BLOCK = 1024 * 1024
FORM_OVERHEAD = 64 * 1024  # 表单的边界和字段头所占的余量；精确的文件大小由 read_upload 判断
_PATHS = re.compile(r"/(?:materials/upload|todos/\d+/materials/upload)")


def _limit() -> int:
    return config.MAX_UPLOAD_MB * 1024 * 1024


def _too_large() -> str:
    return f"文件超过 {config.MAX_UPLOAD_MB}MB 上限"


def read_upload(file: UploadFile) -> bytes:
    """分块读取，超过上限立刻返回 413，不把整个文件读进内存。"""
    if file.size is not None and file.size > _limit():
        raise HTTPException(status_code=413, detail=_too_large())
    data = bytearray()
    while block := file.file.read(BLOCK):
        data.extend(block)
        if len(data) > _limit():
            raise HTTPException(status_code=413, detail=_too_large())
    return bytes(data)


class UploadLimitMiddleware:
    """框架在进入接口之前就把整个表单写到临时文件，所以要在这之前限制。

    有 Content-Length 且超限时不读请求体直接拒绝（只是优化）；真正的保证是读请求体时计数，分块传输也管得住。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST" or not _PATHS.fullmatch(scope["path"]):
            await self.app(scope, receive, send)
            return
        limit = _limit() + FORM_OVERHEAD
        declared = next((value for key, value in scope["headers"] if key == b"content-length"), b"")
        if declared.isdigit() and len(declared) <= 20 and int(declared) > limit:
            await JSONResponse({"detail": _too_large()}, status_code=413)(scope, receive, send)
            return
        received = 0

        async def counted_receive():
            nonlocal received
            message = await receive()
            received += len(message.get("body", b""))
            if received > limit:
                raise HTTPException(status_code=413, detail=_too_large())
            return message

        await self.app(scope, counted_receive, send)
