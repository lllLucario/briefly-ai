"""旧版Office（Word .doc、Excel .xls、PowerPoint .ppt）在本地转换成新格式；独立临时目录和配置，不修改原文件。"""
from io import BytesIO
import hashlib
import os
from pathlib import Path
import shutil
import signal
import subprocess
from tempfile import TemporaryDirectory, gettempdir, mkdtemp
from threading import BoundedSemaphore
from zipfile import BadZipFile, ZipFile

from .errors import UserInputError
from . import config

OLE_HEADER = bytes.fromhex("d0cf11e0a1b11ae1")
# 旧格式 → (软件名, LibreOffice 导出筛选器, 新格式后缀, 转换结果里必须有的部件)
LEGACY = {
    "doc": ("Word", "docx:Office Open XML Text", "docx", "word/document.xml"),
    "xls": ("Excel", "xlsx:Calc MS Excel 2007 XML", "xlsx", "xl/workbook.xml"),
    "ppt": ("PowerPoint", "pptx:Impress MS PowerPoint 2007 XML", "pptx", "ppt/presentation.xml"),
}
_slots = BoundedSemaphore(2)
HARDENING = ('<?xml version="1.0" encoding="UTF-8"?>'
             '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
             '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
             '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
             '<prop oor:name="DisableMacrosExecution" oor:op="fuse"><value>true</value></prop>'
             '</item></oor:items>')
TEMPLATE_VERSION = 1
_broken_templates: set[str] = set()


def converter_path():
    """显式配置优先，否则查PATH和常见安装位置；不下载、不安装。"""
    configured = config.LIBREOFFICE_PATH.strip()
    if configured:
        path = Path(configured).expanduser()
        if not path.is_file():
            raise UserInputError("BRIEFLY_LIBREOFFICE 指定的转换程序不存在，请核对服务端配置")
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
    raise UserInputError("解析旧版 .doc/.xls/.ppt 需要服务端安装 LibreOffice，或配置 BRIEFLY_LIBREOFFICE 指向 soffice；"
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
        raise UserInputError(f"{label} {action}超时；请检查文件是否损坏、加密或过于复杂") from exc
    if process.returncode != 0:
        raise UserInputError(f"{label} {action}失败；请检查文件是否损坏或受密码保护")


def _hardened_profile(profile: Path) -> None:
    """独立配置禁用宏执行，不读取用户桌面Office配置。"""
    (profile/"user").mkdir(parents=True)
    (profile/"user"/"registrymodifications.xcu").write_text(HARDENING, encoding="utf-8")


def _is_hardened(profile: Path) -> bool:
    """配置里的宏设置仍是禁用：LibreOffice 退出时会重写 registrymodifications.xcu，复用前逐项核对。"""
    from xml.etree import ElementTree

    try:
        root = ElementTree.parse(profile/"user"/"registrymodifications.xcu").getroot()
    except (OSError, ElementTree.ParseError):
        return False
    path, name = "{http://openoffice.org/2001/registry}path", "{http://openoffice.org/2001/registry}name"
    found = {}
    for item in root.iter("item"):
        if item.get(path) == "/org.openoffice.Office.Common/Security/Scripting":
            for prop in item.iter("prop"):
                found[prop.get(name)] = (prop.findtext("value") or "").strip()
    return found.get("MacroSecurityLevel") == "3" and found.get("DisableMacrosExecution") == "true"


def _template_root() -> Path:
    """模板放在当前用户的临时目录；共享临时目录（POSIX 的 /tmp）下要求目录归本人独占，防止他人预置配置或扩展。"""
    root = Path(gettempdir())/"briefly-office-profile"
    root.mkdir(mode=0o700, exist_ok=True)
    if os.name != "nt":
        info = root.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise OSError("LibreOffice 配置模板目录不归当前用户独占")
    return root


def _template(executable: str) -> Path | None:
    """同一 LibreOffice 程序共用一份预先初始化的加固配置；没有就现建一份，建不成返回 None（改用全新配置）。

    冷启动的主要开销（内置扩展注册、配置与公式缓存）只在真正转换时发生，--terminate_after_init 不触发，
    所以模板用加固配置转换一份内置的两行 CSV 来预热，从未打开过用户文件；按程序路径、大小、修改时间和加固内容区分。
    """
    stat = Path(executable).stat()
    key = hashlib.sha256(f"{TEMPLATE_VERSION}|{Path(executable).resolve()}|{stat.st_size}|{stat.st_mtime_ns}|"
                         f"{HARDENING}".encode()).hexdigest()[:16]
    if key in _broken_templates:
        return None
    try:
        target = _template_root()/key
        if not (target/"ready").is_file():
            building = Path(mkdtemp(prefix=f"{key}-", dir=target.parent))
            try:
                _hardened_profile(building)
                with TemporaryDirectory(prefix="briefly-office-warm-") as warm:
                    sample = Path(warm)/"warm.csv"
                    sample.write_text("项目,数量\n示例,1\n", encoding="utf-8")
                    _run([executable, "-env:UserInstallation="+building.as_uri(), "--headless", "--nologo",
                          "--nodefault", "--norestore", "--convert-to", LEGACY["xls"][1], "--outdir",
                          str(Path(warm)/"output"), str(sample)], action="初始化", label="LibreOffice 配置模板")
                if not _is_hardened(building):
                    raise ValueError("初始化后的配置未保持禁用宏")
                (building/"ready").touch()
                try:
                    building.rename(target)
                except OSError:  # 并发进程已建好同一份，用已有的
                    pass
            finally:
                shutil.rmtree(building, ignore_errors=True)
        if (target/"ready").is_file() and _is_hardened(target):
            return target
    except Exception:  # 模板只是提速手段，任何失败都退回每次全新配置
        pass
    _broken_templates.add(key)  # 本进程内不再反复尝试
    return None


def _new_profile(profile: Path, executable: str) -> None:
    """每次转换一个独立配置目录：优先复制预初始化模板（省去冷启动初始化），否则从零建加固配置。"""
    template = _template(executable) if config.OFFICE_PROFILE_TEMPLATE else None
    if template is not None:
        try:
            shutil.copytree(template, profile, ignore=shutil.ignore_patterns("ready"))
            if _is_hardened(profile):
                return
        except OSError:
            pass
        shutil.rmtree(profile, ignore_errors=True)
    _hardened_profile(profile)


def convert_doc(data: bytes) -> bytes:
    return convert_legacy(data, "doc")


def convert_legacy(data: bytes, kind: str) -> bytes:
    app, export, suffix, part = LEGACY[kind]
    # 旧版 Office 都是 OLE 复合文档；.doc 另接受 RTF（常见后缀为 .doc 的 RTF 文件）
    if not (data.startswith(OLE_HEADER) or kind == "doc" and data.lstrip().startswith(b"{\\rtf")):
        raise UserInputError(f"文件不是可识别的旧版 {app} .{kind} 文档；仅更改后缀不能转换文件格式")
    executable = converter_path()
    if not _slots.acquire(timeout=5):
        raise UserInputError(f"旧版 {app} 转换任务繁忙，请稍后重试")
    try:
        with TemporaryDirectory(prefix="briefly-office-") as temporary:
            folder = Path(temporary)
            source, target = folder/f"source.{kind}", folder/"output"
            source.write_bytes(data)
            target.mkdir()
            profile = folder/"profile"
            _new_profile(profile, executable)
            _run([executable, "-env:UserInstallation="+profile.as_uri(), "--headless", "--nologo",
                  "--nodefault", "--norestore", "--convert-to", export,
                  "--outdir", str(target), str(source)], label=f"旧版 .{kind}")
            converted = target/f"source.{suffix}"
            if not converted.is_file() or not 0 < converted.stat().st_size <= 50*1024*1024:
                raise UserInputError("未获得有效转换结果，文件可能损坏、加密或转换结果超过50MB")
            payload = converted.read_bytes()
            try:
                with ZipFile(BytesIO(payload)) as archive:
                    if part not in archive.namelist():
                        raise UserInputError(f"转换结果不是有效的{app}文档")
            except BadZipFile as exc:
                raise UserInputError(f"转换结果不是有效的{suffix}文件") from exc
            return payload
    finally:
        _slots.release()
