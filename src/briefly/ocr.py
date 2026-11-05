"""本地图片文字识别：RapidOCR（ONNX Runtime，CPU）＋ rapidocr 包自带的 PP-OCRv6 small 中文模型。

推理全在本机进行，不联网下载模型、不外发图片。解析器把待识别的整页（扫描页、图片文件）和嵌入图片交给
OcrSession：会话按每份文件的页数/图片数上限和总时长预算决定识别多少，超出的部分跳过并写进报告；
太小的图片（图标、装饰）不识别；同一张图片只识别一次，多数页面都重复出现的同一张图片（版式背景、徽标）跳过。

识别结果成段放进正文，前后各有一行标记写明来源（第几页、第几张图）和平均置信度，读者和模型都能看出
哪段来自图片识别。平均置信度低于门槛的段落记入 ocr_unconfirmed_ranges，质量限制只落在这些范围上。
"""
from collections import Counter
from dataclasses import dataclass, field
import hashlib
from importlib.metadata import PackageNotFoundError, version
from importlib.util import find_spec
from io import BytesIO
from pathlib import Path
import re
from time import monotonic

from .errors import UserInputError
from . import config

ENGINE = "rapidocr"
MODEL = dict(det="PP-OCRv6 small", rec="PP-OCRv6 small", cls="ch_ppocr_mobile_v2.0", lang="ch")
# rapidocr 3.10 wheel 自带的模型文件（rapidocr/models/），指纹只看这三个
MODEL_FILES = ("PP-OCRv6_det_small.onnx", "PP-OCRv6_rec_small.onnx", "ch_ppocr_mobile_v2.0_cls_mobile.onnx")
MIN_SIDE = 24  # 图片短边不到这么多像素不识别（图标、分隔线）
MIN_PIXELS = 20_000  # 图片面积不到这么多像素不识别（约 140×140，小图标、徽标）
MAX_SIDE = 3000  # 送入识别前把长边缩到这个像素以内（检测模型内部还会缩到 2000）
PAGE_DPI = 200  # 扫描页渲染分辨率
PAGE_MAX_SIDE = 2400  # 扫描页渲染后长边不超过这个像素
MIN_TEXT_CHARS = 5  # 嵌入图片识别出的字母数字/汉字少于这么多（LOGO、装饰字母、照片里的零星笔画）不收录
REPEATED_MIN = 3  # 同一张图片出现在至少这么多页、且不少于一半页面，视为版式装饰
OPEN = "〔图片文字识别"
CLOSE = "〔图片文字识别结束〕"
_TOKEN = re.compile("\ue000OCR(\\d+)\ue001")


@dataclass
class OcrBlock:
    location: str  # 第3页 / 第2页 图1 / 工作表“汇总” 图1 / 图片
    text: str  # 识别出的文字，已按行距合并成段
    confidence: float  # 按字数加权的平均置信度
    lines: int
    page: int | None = None  # 从 1 起
    image: int | None = None  # 页内第几张图，从 1 起；整页识别为 None
    seconds: float = 0.0

    @property
    def unconfirmed(self) -> bool:
        return self.confidence < config.OCR_MIN_CONFIDENCE

    def rendered(self) -> str:
        """放进正文的样子：标记行 + 文字 + 结束行。"""
        note = f"平均置信度 {self.confidence:.2f}"
        if self.unconfirmed:
            note += f"，低于 {config.OCR_MIN_CONFIDENCE:.2f}，未确认"
        return f"{OPEN}｜{self.location}｜{note}〕\n{self.text}\n{CLOSE}"


def enabled() -> bool:
    """BRIEFLY_OCR 不是 off 且已安装 rapidocr（或测试注入了引擎）。"""
    if config.OCR not in {"auto", "off"}:
        raise UserInputError("BRIEFLY_OCR须为auto或off")
    return config.OCR == "auto" and (_engine is not None or find_spec("rapidocr") is not None)


def identity() -> dict:
    """进入解析缓存指纹：开关、引擎与模型文件、上限和门槛变化时旧缓存自然失效。不导入 rapidocr（慢）。"""
    value = dict(mode=config.OCR, version=1)
    if config.OCR != "auto":
        return value
    packages = {}
    for name in ("rapidocr", "onnxruntime", "opencv-python-headless"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    spec = find_spec("rapidocr")
    models = {}
    if spec and spec.origin:
        for name in MODEL_FILES:
            path = Path(spec.origin).parent / "models" / name
            models[name] = _file_sha256(path) if path.is_file() else None
    return dict(value, engine=ENGINE, model=MODEL, packages=packages, models=models,
                limits=dict(pages=config.OCR_MAX_PAGES, images=config.OCR_MAX_IMAGES,
                            seconds=config.OCR_TIME_BUDGET_S, min_confidence=config.OCR_MIN_CONFIDENCE,
                            min_side=MIN_SIDE, min_pixels=MIN_PIXELS, page_dpi=PAGE_DPI))


_hashes: dict = {}


def _file_sha256(path: Path) -> str:
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if key not in _hashes:
        _hashes[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return _hashes[key]


_engine = None  # 进程内只建一次；测试替换成替身
DEADLINE: float | None = None  # 隔离解析子进程里识别必须结束的时刻（monotonic），从子进程开始解析算起


def start_deadline() -> None:
    """解析子进程开始解析时调用：识别时长预算扣除识别之前的解析耗时（大 PDF 光提取就要几十秒）。"""
    global DEADLINE
    DEADLINE = monotonic() + config.OCR_TIME_BUDGET_S


def _get_engine():
    global _engine
    if _engine is None:
        from rapidocr import EngineType, LangDet, LangRec, ModelType, OCRVersion, RapidOCR

        _engine = RapidOCR(params={
            "Global.log_level": "error",
            "EngineConfig.onnxruntime.intra_op_num_threads": config.OCR_THREADS,
            "Det.engine_type": EngineType.ONNXRUNTIME, "Det.lang_type": LangDet.CH,
            "Det.model_type": ModelType.SMALL, "Det.ocr_version": OCRVersion.PPOCRV6,
            "Rec.engine_type": EngineType.ONNXRUNTIME, "Rec.lang_type": LangRec.CH,
            "Rec.model_type": ModelType.SMALL, "Rec.ocr_version": OCRVersion.PPOCRV6,
        })
    return _engine


@dataclass
class _Line:
    text: str
    score: float
    x0: float
    y0: float
    x1: float
    y1: float


def _lines(image) -> list[_Line]:
    """调用引擎；引擎返回 txts/scores/boxes（四点框）。"""
    import numpy as np

    result = _get_engine()(np.asarray(image))
    texts, scores, boxes = getattr(result, "txts", None), getattr(result, "scores", None), getattr(result, "boxes", None)
    if not texts:
        return []
    out = []
    for text, score, box in zip(texts, scores, boxes):
        text = " ".join(str(text).split())
        if not text:
            continue
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        out.append(_Line(text, float(score), min(xs), min(ys), max(xs), max(ys)))
    return out


_SENTENCE_END = tuple("。！？；：.!?;:")


def _paragraphs(lines: list[_Line]) -> str:
    """识别行 → 正文：纵向重叠的框是同一行（左到右，用两个空格隔开）。至少三行时，单框行写满整段宽度、
    下一行紧挨着且左端对齐、本行不以句末标点结尾，视为排版换行，接成一段；短的几行（标签、列表）不接。"""
    from .layout_cleaner import join_text

    rows: list[list[_Line]] = []
    for line in sorted(lines, key=lambda l: (l.y0 + l.y1) / 2):
        middle = (line.y0 + line.y1) / 2
        if rows and rows[-1][-1].y0 <= middle <= rows[-1][-1].y1:
            rows[-1].append(line)
        else:
            rows.append([line])
    left = min(l.x0 for l in lines)
    edges = sorted(max(l.x1 for l in row) for row in rows)
    full = edges[int(len(edges) * 0.8)]  # 正文行宽：大多数行到达的右端
    out: list[str] = []
    previous = None
    for row in rows:
        row.sort(key=lambda l: l.x0)
        text = "  ".join(l.text for l in row)
        height = max(l.y1 - l.y0 for l in row)
        if (previous and len(rows) >= 3 and len(row) == 1 and len(previous) == 1
                and previous[0].x1 >= left + (full - left) * 0.92
                and abs(previous[0].x0 - row[0].x0) <= height * 0.5
                and row[0].y0 - previous[0].y1 <= height * 0.8
                and not out[-1].endswith(_SENTENCE_END)):
            out[-1] = join_text(out[-1], text)
        else:
            out.append(text)
        previous = row
    return "\n".join(_escape(line) for line in out)


def _escape(line: str) -> str:
    """识别出的行不能被当成 Markdown 标题、表格行或我们自己的标记行。"""
    stripped = line.lstrip()
    if stripped.startswith(("#", "|", OPEN)):
        return "\\" + line
    return line


def _rgb(image):
    """PIL 图片 → RGB；透明底按白底合成（否则透明处变黑，黑字看不见）；过大的先缩小。"""
    from PIL import Image

    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        image = image.convert("RGBA")
        canvas = Image.new("RGB", image.size, "white")
        canvas.paste(image, mask=image.getchannel("A"))
        image = canvas
    elif image.mode != "RGB":
        image = image.convert("RGB")
    if max(image.size) > MAX_SIDE:
        image = image.copy()
        image.thumbnail((MAX_SIDE, MAX_SIDE))
    return image


@dataclass
class OcrSession:
    """一份文件的识别过程：上限、预算、去重和报告。"""
    budget: float = field(default_factory=lambda: config.OCR_TIME_BUDGET_S)
    blocks: list[OcrBlock] = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)
    pages: int = 0
    images: int = 0
    failed: int = 0
    started: float = field(default_factory=monotonic)
    _seen: dict = field(default_factory=dict)  # 图片 sha256 → 识别结果（None 表示没识别出文字）
    _repeated: set = field(default_factory=set)

    def ignore_repeated(self, hashes: list[list[str]]) -> None:
        """每页的图片 sha256 列表；多数页面都出现的同一张图片是版式装饰，之后遇到直接跳过。"""
        if len(hashes) < REPEATED_MIN:
            return
        seen = Counter(h for page in hashes for h in set(page))
        self._repeated = {h for h, n in seen.items() if n >= max(REPEATED_MIN, len(hashes) / 2)}

    def _over(self, kind: str) -> str | None:
        if kind == "page" and self.pages >= config.OCR_MAX_PAGES:
            return "page_limit"
        if kind == "image" and self.images >= config.OCR_MAX_IMAGES:
            return "image_limit"
        if monotonic() - self.started >= self.budget or DEADLINE is not None and monotonic() >= DEADLINE:
            return "time_budget"
        return None

    def page(self, image, location: str, page: int | None = None) -> OcrBlock | None:
        """整页识别（扫描页、图片文件），不设尺寸门槛。"""
        if reason := self._over("page"):
            self.skipped[reason] += 1
            return None
        self.pages += 1
        return self._run(image, location, page, None)

    def picture(self, data: bytes | None, location: str, page: int | None = None,
                image: int | None = None, picture=None) -> OcrBlock | None:
        """文档里嵌入的图片：原始字节或已打开的 PIL 图片；太小、重复、超限的跳过。"""
        digest = hashlib.sha256(data if data is not None else picture.tobytes()).hexdigest()
        if digest in self._repeated:
            self.skipped["repeated"] += 1
            return None
        if digest in self._seen:
            cached = self._seen[digest]
            if cached is None:
                return None
            block = OcrBlock(location, cached.text, cached.confidence, cached.lines, page, image)
            self.blocks.append(block)
            return block
        try:
            if picture is None:
                from PIL import Image

                picture = Image.open(BytesIO(data))
                picture.load()
        except Exception:  # 不认识的格式（EMF/WMF 等矢量图）、损坏、超大
            self.skipped["unreadable"] += 1
            self._seen[digest] = None
            return None
        width, height = picture.size
        if min(width, height) < MIN_SIDE or width * height < MIN_PIXELS:
            self.skipped["small"] += 1
            self._seen[digest] = None
            return None
        if reason := self._over("image"):
            self.skipped[reason] += 1
            return None
        self.images += 1
        block = self._run(picture, location, page, image, MIN_TEXT_CHARS)
        self._seen[digest] = block
        return block

    def _run(self, image, location, page, number, min_chars=1) -> OcrBlock | None:
        tick = monotonic()
        try:
            lines = _lines(_rgb(image))
        except Exception:  # 单张图片识别失败不拖垮整份解析
            self.failed += 1
            return None
        if sum(c.isalnum() for l in lines for c in l.text) < min_chars:
            if lines:
                self.skipped["little_text"] += 1
            return None
        chars = sum(len(l.text) for l in lines)
        confidence = sum(l.score * len(l.text) for l in lines) / chars
        block = OcrBlock(location, _paragraphs(lines), round(confidence, 4), len(lines), page, number,
                         round(monotonic() - tick, 3))
        self.blocks.append(block)
        return block

    def token(self, block: OcrBlock) -> str:
        """Docling 导出前放进文档的占位符；finish 时换成识别文字。"""
        return f"\ue000OCR{next(i for i, b in enumerate(self.blocks) if b is block)}\ue001"

    def finish(self, text: str, report: dict) -> tuple[str, dict]:
        """把占位符换成识别文字，按最终正文计算各段范围，并进报告。正文需已是最终形态（之后不再改动或 strip）。"""
        if not (self.blocks or self.skipped or self.failed or self.pages or self.images):
            return text, report  # 没有要识别的图片，报告不变
        text = _TOKEN.sub(lambda m: self.blocks[int(m.group(1))].rendered(), text)
        placed, cursor = [], 0
        for block in self.blocks:
            rendered = block.rendered()
            start = text.find(rendered, cursor)
            if start < 0:  # 被去重或清理掉（如重复页脚规则），不计范围
                continue
            placed.append((block, start, start + len(rendered)))
            cursor = start + len(rendered)
        return text, merge_report(report, self.report(placed))

    def report(self, placed) -> dict:
        warnings = []
        reasons = dict(page_limit=f"超过每份 {config.OCR_MAX_PAGES} 页的识别上限",
                       image_limit=f"超过每份 {config.OCR_MAX_IMAGES} 张图片的识别上限",
                       time_budget=f"超过识别时长预算 {self.budget:.0f} 秒")
        for reason, text in reasons.items():
            if self.skipped[reason]:
                warnings.append(f"{text}，有 {self.skipped[reason]} {'页' if reason == 'page_limit' else '处'}图片未识别文字，内容可能缺失")
        if self.failed:
            warnings.append(f"有 {self.failed} 张图片识别失败，内容可能缺失")
        low = [b for b, _, _ in placed if b.unconfirmed]
        if placed:
            warnings.append(f"有 {len(placed)} 段文字来自图片识别（正文中以“{OPEN}”标出），可能有错字漏字，请核对原件"
                            + (f"；其中 {len(low)} 段置信度低，未作为确定依据" if low else ""))
        info = dict(engine=ENGINE, model=MODEL, min_confidence=config.OCR_MIN_CONFIDENCE,
                    pages_recognized=self.pages, images_recognized=self.images, failed=self.failed,
                    skipped=dict(self.skipped), seconds=round(monotonic() - self.started, 3),
                    partial=any(self.skipped[r] for r in reasons) or bool(self.failed),
                    blocks=[dict(location=b.location, page=b.page, image=b.image, start=start, end=end,
                                 chars=len(b.text), lines=b.lines, confidence=b.confidence,
                                 unconfirmed=b.unconfirmed, seconds=b.seconds) for b, start, end in placed])
        result = dict(ocr=info)
        if placed:
            result["ocr_ranges"] = [dict(start=s, end=e) for _, s, e in placed]
            result["ocr_unconfirmed_ranges"] = [dict(start=s, end=e) for b, s, e in placed if b.unconfirmed]
        if warnings:
            result["ocr_warnings"] = warnings
        return result


def merge_report(report: dict, extra: dict) -> dict:
    """把 finish 的结果并进解析报告：警告追加到 warnings。"""
    warnings = extra.pop("ocr_warnings", [])
    merged = {**report, **extra}
    if warnings:
        merged["warnings"] = [*report.get("warnings", []), *warnings]
    return merged


def disabled_note() -> str:
    if config.OCR == "off":
        return "图片文字识别已关闭（BRIEFLY_OCR=off）"
    return "未安装本地图片文字识别（pip install -e \".[ocr]\"）"


def render_page(page, *, dpi: int = PAGE_DPI):
    """pypdfium2 页面 → PIL 图片，长边不超过 PAGE_MAX_SIDE。"""
    width, height = page.get_size()
    scale = min(dpi / 72, PAGE_MAX_SIDE / max(width, height, 1))
    bitmap = page.render(scale=scale)
    try:
        return bitmap.to_pil().copy()
    finally:
        bitmap.close()
