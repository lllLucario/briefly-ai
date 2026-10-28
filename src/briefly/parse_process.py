"""知识入库的整文件解析边界：独立进程、总预算、有限并发与进程树清理。"""
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import BoundedSemaphore
from time import monotonic

from . import config
from .loaders import LoadedDocument, file_type

_slots = BoundedSemaphore(2)
MAX_RESULT_BYTES = 50 * 1024 * 1024


class ParseFailure(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class _WindowsJob:
    """启动门打开前将worker加入job；关闭job也清理Java/LibreOffice后代。"""
    def __init__(self, pid):
        from ctypes import wintypes as w

        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_longlong), ("job_time", ctypes.c_longlong),
                        ("flags", w.DWORD), ("min_working", ctypes.c_size_t), ("max_working", ctypes.c_size_t),
                        ("active", w.DWORD), ("affinity", ctypes.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", ctypes.c_ulonglong * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, arguments, result in (
            ("CreateJobObjectW", [ctypes.c_void_p, w.LPCWSTR], w.HANDLE),
            ("SetInformationJobObject", [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD], w.BOOL),
            ("OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            ("AssignProcessToJobObject", [w.HANDLE, w.HANDLE], w.BOOL),
            ("CloseHandle", [w.HANDLE], w.BOOL),
        ):
            fn = getattr(api, name)
            fn.argtypes, fn.restype = arguments, result
        self.api, self.handle = api, api.CreateJobObjectW(None, None)
        process = None
        try:
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
            limits = Extended()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not api.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            process = api.OpenProcess(0x0100 | 0x0001, False, pid)  # SET_QUOTA | TERMINATE
            if not process or not api.AssignProcessToJobObject(self.handle, process):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise
        finally:
            if process:
                api.CloseHandle(process)

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def _worker_command(folder):
    return [sys.executable, "-m", "briefly.parse_worker", str(folder)]


def load_isolated(filename, data, *, timeout=None):
    """总预算包含排队、启动、DOC直读/回退、Docling和结果校验；不写业务库。"""
    file_type(filename)
    if not data or len(data) > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise ParseFailure("invalid_size", f"文件为空或超过{config.MAX_UPLOAD_MB}MB上限")
    timeout = config.PARSE_TIMEOUT_S if timeout is None else timeout
    if timeout <= 0:
        raise ValueError("解析总预算必须大于0")
    deadline = monotonic() + timeout
    if not _slots.acquire(timeout=timeout):
        raise ParseFailure("parse_timeout", "解析等待超过总预算，请稍后重试")
    process, job = None, None
    try:
        with TemporaryDirectory(prefix="briefly-parse-") as temporary:
            folder = Path(temporary)
            (folder / "input").write_bytes(data)
            settings = {key: getattr(config, key) for key in (
                "DOC_PARSER", "JAVA_PATH", "TIKA_APP_JAR", "LIBREOFFICE_PATH", "DOC_CONVERSION_TIMEOUT_S")}
            (folder / "request.json").write_text(json.dumps(dict(filename=Path(filename).name, settings=settings)), encoding="utf-8")
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
            try:
                process = subprocess.Popen(_worker_command(folder), stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)
                if os.name == "nt":
                    job = _WindowsJob(process.pid)
                (folder / "start").touch()  # 只有进程树保护就绪才允许解析器启动
                process.wait(timeout=max(0, deadline - monotonic()))
                result = folder / "result.json"
                if process.returncode != 0 or not result.is_file() or result.stat().st_size > MAX_RESULT_BYTES:
                    raise ParseFailure("worker_failed", "解析进程异常或结果超过上限，文件未入库")
                value = json.loads(result.read_text(encoding="utf-8"))
                if monotonic() > deadline:
                    raise subprocess.TimeoutExpired(process.args, timeout)
                if value.get("error"):
                    raise ParseFailure("parse_failed", value["error"])
                if not isinstance(value.get("text"), str) or not value["text"].strip():
                    raise ParseFailure("empty_document", "未从文件提取到正文，文件未入库")
                return LoadedDocument(value["text"], value["markdown"], value["report"])
            except subprocess.TimeoutExpired as exc:
                raise ParseFailure("parse_timeout", "整文件解析超过总预算，文件未入库；可单独重试") from exc
            except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
                raise ParseFailure("worker_failed", "解析隔离或结果校验失败，文件未入库") from exc
            finally:
                if job:
                    job.close()
                elif process and os.name != "nt":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if process:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=10)
    finally:
        _slots.release()
