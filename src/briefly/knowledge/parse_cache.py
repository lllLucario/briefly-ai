"""同库、同原件与同解析器契约复用；切片/Embedding配置不进入解析缓存键。"""
from copy import deepcopy
import hashlib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import shutil

from .. import config
from ..loaders import LoadedDocument, file_type
from ..materials import Material
from .evidence import constraints
from .models import KnowledgeParseCache
from .service import fingerprint


def parser_identity():
    root = Path(__file__).resolve().parents[1]
    sources = ["loaders.py", "direct_word.py", "word_xhtml.py", "legacy_word.py", "pdf.py",
               "tables.py", "chunking.py", "layout_cleaner.py", "materials.py", "parse_process.py",
               "parse_worker.py", "knowledge/parse_cache.py", "java/legacy-doc-parser.jar"]
    packages = {}
    for name in ("docling", "docling-slim", "docling-core", "python-docx", "pypdf", "openpyxl"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    runtimes = {}
    for key in ("JAVA_PATH", "TIKA_APP_JAR", "LIBREOFFICE_PATH"):
        raw = getattr(config, key)
        if not raw and key == "JAVA_PATH":
            raw = shutil.which("java") or ""
        if not raw and key == "LIBREOFFICE_PATH":
            from ..legacy_word import converter_path
            try:
                raw = converter_path()
            except ValueError:
                raw = ""
        path = Path(raw).resolve() if raw else None
        stat = path.stat() if path and path.is_file() else None
        runtimes[key] = dict(path=str(path) if path else None, size=stat.st_size if stat else None,
                             modified=stat.st_mtime_ns if stat else None)
    return dict(version=1, backend=config.DOC_PARSER, conversion_timeout=config.DOC_CONVERSION_TIMEOUT_S,
                packages=packages, runtimes=runtimes,
                sources={name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in sources})


def cache_key(kb_id, filename, data, identity):
    return fingerprint(dict(kb_id=kb_id, kind=file_type(filename),
                            input_sha256=hashlib.sha256(data).hexdigest(), parser=identity))


def read_cached(session, key, kb_id):
    entry = session.get(KnowledgeParseCache, key)
    if entry is None or entry.kb_id != kb_id or entry.material_id is None:
        return None
    material = session.get(Material, entry.material_id)
    if material is None or material.content_hash != entry.content_hash or hashlib.sha256(
            material.content.encode()).hexdigest() != entry.content_hash:
        return None
    report = deepcopy(entry.report_snapshot)
    # 当前材料可能在缓存建立后继承更严格的质量标记，不得靠缓存洗白。
    limits = {**constraints(report, 0, material.char_count),
              **constraints(material.parse_report, 0, material.char_count)}
    if limits:
        report["inherited_quality_constraints"] = limits
    return LoadedDocument(material.content, entry.markdown, report)
