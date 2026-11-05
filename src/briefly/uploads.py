"""请求体大小限制：在框架解析请求体、写临时文件之前按路径拦住，读文件时再按文件大小精确判断。"""
import re

from fastapi import HTTPException, UploadFile
from starlette.responses import JSONResponse

from . import config

BLOCK = 1024 * 1024
MB = 1024 * 1024
FORM_OVERHEAD = 64 * 1024  # 表单的边界和字段头所占的余量；精确的文件大小由 read_upload 判断
_FILE_PATHS = re.compile(r"/(?:materials/upload|todos/\d+/materials/upload)")
_IMPORT_PATHS = re.compile(r"/knowledge-bases/[^/]+/imports")


def _too_large() -> str:
    return f"文件超过 {config.MAX_UPLOAD_MB}MB 上限"


def json_limit_mb() -> int:
    """其他请求体的总上限，只防滥用。JSON 转义最多让中文变成 2 倍（\\uXXXX），留 3 倍，所以调大上传上限时不用跟着改。"""
    return max(config.MAX_JSON_MB, config.MAX_UPLOAD_MB * 3)


def _limit_of(path: str) -> tuple[int, str]:
    """(请求体字节上限, 超限时给用户看的说明)。"""
    if _FILE_PATHS.fullmatch(path):
        return config.MAX_UPLOAD_MB * MB + FORM_OVERHEAD, _too_large()
    if _IMPORT_PATHS.fullmatch(path):
        return config.MAX_IMPORT_BATCH_MB * MB + FORM_OVERHEAD, f"批次超过 {config.MAX_IMPORT_BATCH_MB}MB，请分批提交"
    return json_limit_mb() * MB, f"提交的内容超过 {json_limit_mb()}MB 上限，请拆分后再提交"


def read_upload(file: UploadFile) -> bytes:
    """分块读取，超过上限立刻返回 413，不把整个文件读进内存。"""
    limit = config.MAX_UPLOAD_MB * MB
    if file.size is not None and file.size > limit:
        raise HTTPException(status_code=413, detail=_too_large())
    data = bytearray()
    while block := file.file.read(BLOCK):
        data.extend(block)
        if len(data) > limit:
            raise HTTPException(status_code=413, detail=_too_large())
    return bytes(data)


class UploadLimitMiddleware:
    """框架在进入接口之前就读完请求体（表单写到临时文件），所以要在这之前限制。

    有 Content-Length 且超限时不读请求体直接拒绝（只是优化）；真正的保证是读请求体时计数，分块传输也管得住。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit, message = _limit_of(scope["path"])
        declared = next((value for key, value in scope["headers"] if key == b"content-length"), b"")
        if declared.isdigit() and len(declared) <= 20 and int(declared) > limit:
            await JSONResponse({"detail": message}, status_code=413)(scope, receive, send)
            return
        received = 0

        async def counted_receive():
            nonlocal received
            event = await receive()
            received += len(event.get("body", b""))
            if received > limit:
                raise HTTPException(status_code=413, detail=message)
            return event

        await self.app(scope, counted_receive, send)
