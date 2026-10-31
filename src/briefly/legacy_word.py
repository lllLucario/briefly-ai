"""旧版Office（Word .doc、Excel .xls、PowerPoint .ppt）在本地转换成新格式；独立临时目录和配置，不修改原文件。"""
from io import BytesIO
import os
from pathlib import Path
import shutil
import signal
import subprocess
from tempfile import TemporaryDirectory
from threading import BoundedSemaphore
from zipfile import BadZipFile, ZipFile

from . import config

OLE_HEADER = bytes.fromhex("d0cf11e0a1b11ae1")
# 旧格式 → (软件名, LibreOffice 导出筛选器, 新格式后缀, 转换结果里必须有的部件)
LEGACY = {
    "doc": ("Word", "docx:Office Open XML Text", "docx", "word/document.xml"),
    "xls": ("Excel", "xlsx:Calc MS Excel 2007 XML", "xlsx", "xl/workbook.xml"),
    "ppt": ("PowerPoint", "pptx:Impress MS PowerPoint 2007 XML", "pptx", "ppt/presentation.xml"),
}
_slots = BoundedSemaphore(2)


def converter_path():
    """显式配置优先，否则查PATH和常见安装位置；不下载、不安装。"""
    configured = config.LIBREOFFICE_PATH.strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file():
            raise ValueError("BRIEFLY_LIBREOFFICE 指定的转换程序不存在，请核对服务端配置")
        return str(path.resolve())
    for name in ("soffice.com", "soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
        if base:
            path = Path(base)/"LibreOffice"/"program"/"soffice.com"
            if path.is_file():
                return str(path)
    raise ValueError("解析旧版 .doc/.xls/.ppt 需要服务端安装 LibreOffice，或配置 BRIEFLY_LIBREOFFICE 指向 soffice；"
                     ".docx/.xlsx/.pptx 可直接解析")


def _run(command, *, action="转换", label="旧版 .doc"):
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, **options)
    try:
        process.communicate(timeout=config.DOC_CONVERSION_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        if os.name == "nt":
            # 只结束本次独立配置启动的进程树，避免遗留soffice.bin占用临时文件。
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW, check=False, timeout=10)
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
        raise ValueError(f"{label} {action}超时；请检查文件是否损坏、加密或过于复杂") from exc
    if process.returncode != 0:
        raise ValueError(f"{label} {action}失败；请检查文件是否损坏或受密码保护")


def convert_doc(data: bytes) -> bytes:
    return convert_legacy(data, "doc")


def convert_legacy(data: bytes, kind: str) -> bytes:
    app, export, suffix, part = LEGACY[kind]
    # 旧版 Office 都是 OLE 复合文档；.doc 另接受 RTF（常见后缀为 .doc 的 RTF 文件）
    if not (data.startswith(OLE_HEADER) or kind == "doc" and data.lstrip().startswith(b"{\\rtf")):
        raise ValueError(f"文件不是可识别的旧版 {app} .{kind} 文档；仅更改后缀不能转换文件格式")
    executable = converter_path()
    if not _slots.acquire(timeout=5):
        raise ValueError(f"旧版 {app} 转换任务繁忙，请稍后重试")
    try:
        with TemporaryDirectory(prefix="briefly-office-") as temporary:
            folder = Path(temporary)
            source, target = folder/f"source.{kind}", folder/"output"
            source.write_bytes(data)
            target.mkdir()
            profile = folder/"profile"
            profile.mkdir()
            (profile/"user").mkdir()
            # 独立配置禁用宏执行，不读取用户桌面Office配置。
            (profile/"user"/"registrymodifications.xcu").write_text(
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
                '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
                '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
                '<prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>'
                '</item></oor:items>', encoding="utf-8")
            _run([executable, "-env:UserInstallation="+profile.as_uri(), "--headless", "--nologo",
                  "--nodefault", "--norestore", "--convert-to", export,
                  "--outdir", str(target), str(source)], label=f"旧版 .{kind}")
            converted = target/f"source.{suffix}"
            if not converted.is_file() or not 0 < converted.stat().st_size <= 50*1024*1024:
                raise ValueError("未获得有效转换结果，文件可能损坏、加密或转换结果超过50MB")
            payload = converted.read_bytes()
            try:
                with ZipFile(BytesIO(payload)) as archive:
                    if part not in archive.namelist():
                        raise ValueError(f"转换结果不是有效的{app}文档")
            except BadZipFile as exc:
                raise ValueError(f"转换结果不是有效的{suffix}文件") from exc
            return payload
    finally:
        _slots.release()
