"""本地Tika/POI直接读旧Word；解析结果按文件隔离，模型和网络不参与。"""
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from threading import BoundedSemaphore

from . import config
from .legacy_word import OLE_HEADER, _run
from .word_xhtml import xhtml_document, xhtml_structure_report

_slots = BoundedSemaphore(2)
BRIDGE = Path(__file__).parent / "java" / "legacy-doc-parser.jar"


class DirectWordUnsupported(ValueError):
    """已知结构边界；auto模式可显式记录后转用兼容路径。"""


def _paths():
    java = config.JAVA_PATH.strip() or shutil.which("java")
    jar = config.TIKA_APP_JAR.strip()
    if not java or not Path(java).is_file() or not jar or not Path(jar).is_file() or not BRIDGE.is_file():
        raise ValueError("直接解析.doc需要本地Java 17+和Tika 3.3.3；请配置BRIEFLY_JAVA、BRIEFLY_TIKA_APP_JAR，或选择convert路径")
    return str(Path(java).resolve()), str(Path(jar).resolve())


def direct_available():
    try:
        _paths()
        return True
    except ValueError:
        return False


def parse_docs(payloads: list[bytes]):
    """一次启动处理至多16份/合计50MB；逐文件返回(text,report)或ValueError。

    此接口仅解析，不写材料库。业务批量确认、重试与索引仍由入库层负责。
    """
    if not payloads:
        return []
    if len(payloads) > 16 or sum(map(len, payloads)) > 50*1024*1024:
        raise ValueError("单批直接解析限16份、合计50MB，请分批处理")
    java, jar = _paths()
    if not _slots.acquire(timeout=5):
        raise ValueError("旧版Word直接解析任务繁忙，请稍后重试")
    try:
        results = [None] * len(payloads)
        with TemporaryDirectory(prefix="briefly-doc-direct-") as temporary:
            folder = Path(temporary)
            inputs, positions = [], []
            for position, payload in enumerate(payloads):
                if payload.lstrip().startswith(b"{\\rtf"):
                    results[position] = DirectWordUnsupported("此.doc实际为RTF内容，请使用本地转换路径")
                elif not payload.startswith(OLE_HEADER):
                    results[position] = ValueError("文件不是可识别的旧版Word.doc；仅更改后缀不能转换文件格式")
                else:
                    source = folder / f"input-{position}.doc"
                    source.write_bytes(payload)
                    inputs.append(str(source))
                    positions.append(position)
            if inputs:
                import os
                _run([java, "-Xmx256m", "-XX:ActiveProcessorCount=4", "-Dfile.encoding=UTF-8",
                      "-Djava.awt.headless=true", "-cp", jar+os.pathsep+str(BRIDGE),
                      "LegacyDocParser", str(folder/"output"), *inputs], action="直接解析")
            for offset, position in enumerate(positions):
                target = folder/"output"/str(offset)
                error = target/"error.code"
                if error.is_file():
                    if error.read_text(encoding="utf-8").strip() == "unsupported_structure":
                        results[position] = DirectWordUnsupported("文档含批注或当前无法可靠处理的结构，请核对并使用转换路径")
                    else:
                        results[position] = ValueError("旧版Word直接解析失败，文件可能损坏或受密码保护")
                    continue
                output = target/"0.xhtml"
                if not output.is_file() or not 0 < output.stat().st_size <= 50*1024*1024:
                    results[position] = ValueError("旧版Word直接解析没有有效结果，或结果超过50MB")
                    continue
                try:
                    text, footnotes = xhtml_document(output.read_bytes())
                    if not text.strip():
                        raise ValueError("未从旧版Word中提取到正文")
                    results[position] = (text, dict(parser="tika-poi", structure="word_styles", original_format="doc",
                                                   conversion=None, footnotes=footnotes, adapter_version=4,
                                                   **xhtml_structure_report(output.read_bytes())))
                except ValueError as exc:
                    results[position] = exc
            return results
    finally:
        _slots.release()


def parse_doc(data: bytes):
    result = parse_docs([data])[0]
    if isinstance(result, ValueError):
        raise result
    return result
