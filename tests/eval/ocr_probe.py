"""手动运行：用真实 RapidOCR 识别程序现画的原创文字图片，按标准答案算字符错误率和耗时。

不读真实资料、不调用生成模型；需安装 .[ocr]。默认用产品配置（PP-OCRv6 small，随包分发）；
--model v5-mobile 对照 PP-OCRv5 mobile（首次运行由 rapidocr 从 ModelScope 下载到 --model-dir，只下载模型，不上传任何内容）。

    python tests/eval/ocr_probe.py --output .tmp/ocr-probe.json
    python tests/eval/ocr_probe.py --model v5-mobile --model-dir .tmp/ocr-models --output .tmp/ocr-probe-v5.json
"""
import argparse
from io import BytesIO
import json
from pathlib import Path
import random
import sys
from time import perf_counter

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from PIL import Image, ImageDraw, ImageFilter, ImageFont  # noqa: E402

from briefly import config, ocr  # noqa: E402

# 原创虚构的办公句子（不取自任何真实资料）
SENTENCES = [
    "员工出差须提前三个工作日在系统中提交申请，经部门负责人审批后方可出行。",
    "住宿标准：一线城市每晚不超过600元，其他城市每晚不超过400元。",
    "报销单须在出差结束后30日内提交财务部，逾期不予受理。",
    "采购金额超过50,000元的项目，须由总经理签字并附三家供应商报价。",
    "第七条 会议室预订须提前一天登记，取消预订请在开始前2小时操作。",
    "质量部每月5日前汇总上月不合格品数据，形成纠正与预防措施报告。",
    "新员工试用期为3个月，试用期满经考核合格后办理转正手续。",
    "设备点检记录表由班组长每日填写，异常情况须在1小时内上报。",
    "客户投诉应在24小时内响应，处理结果须回访并记录满意度。",
    "年度预算编制时间为每年11月1日至11月30日，各部门按模板填报。",
    "QC小组活动按PDCA循环开展：计划、实施、检查、处置。",
    "信息安全：禁止将内部文件上传至未经批准的外部网盘或聊天工具。",
]
FONTS = {"宋体": "simsun.ttc", "黑体": "simhei.ttf", "微软雅黑": "msyh.ttc", "楷体": "simkai.ttf"}


def font(name, size):
    return ImageFont.truetype(str(Path("C:/Windows/Fonts") / FONTS[name]), size)


def line_image(text, face, size, *, background="white", color="black"):
    f = font(face, size)
    width = int(f.getlength(text)) + size * 2
    image = Image.new("RGB", (width, int(size * 2.2)), background)
    ImageDraw.Draw(image).text((size, size // 2), text, fill=color, font=f)
    return image


def jpeg(image, quality):
    out = BytesIO()
    image.save(out, "JPEG", quality=quality)
    return Image.open(BytesIO(out.getvalue())).convert("RGB")


def noisy(image, seed):
    rng = random.Random(seed)
    pixels = image.load()
    for _ in range(image.width * image.height // 25):
        x, y = rng.randrange(image.width), rng.randrange(image.height)
        pixels[x, y] = (rng.randrange(256),) * 3
    return image


def scan_page(lines, seed):
    """A4 150dpi 的模拟扫描页：宋体正文，灰底、轻微旋转、模糊和噪点。"""
    page = Image.new("RGB", (1240, 1754), (246, 244, 238))
    draw, f = ImageDraw.Draw(page), font("宋体", 30)
    y = 140
    for text in lines:
        draw.text((110, y), text, fill=(30, 30, 30), font=f)
        y += 64
    page = page.rotate(0.8, expand=False, fillcolor=(246, 244, 238)).filter(ImageFilter.GaussianBlur(0.6))
    return jpeg(noisy(page, seed), 60)


def cases():
    """（名称, 图片, 标准答案）"""
    for i, text in enumerate(SENTENCES):
        face = list(FONTS)[i % len(FONTS)]
        yield f"clean/{face}/24px", line_image(text, face, 24), text
        yield f"small/{face}/14px", line_image(text, face, 14), text
        yield f"jpeg30/{face}/20px", jpeg(line_image(text, face, 20), 30), text
        yield f"blur/{face}/20px", line_image(text, face, 20).filter(ImageFilter.GaussianBlur(1.0)), text
        yield f"lowcontrast/{face}/22px", line_image(text, face, 22, background=(235, 235, 235), color=(140, 140, 140)), text
        yield f"rotate2/{face}/24px", line_image(text, face, 24).rotate(2, expand=True, fillcolor="white"), text
    for n in range(3):
        lines = SENTENCES[n * 4:n * 4 + 4] * 2
        yield f"scan-page/{n + 1}", scan_page(lines, n), "".join(lines)


def distance(a, b):
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def normalize(text):
    """比较时去掉空白和识别标记行，全角数字逗号统一成半角。"""
    lines = [l for l in text.splitlines() if not l.startswith(ocr.OPEN)]
    table = str.maketrans("０１２３４５６７８９，：（）", "0123456789,:()")
    return "".join("".join(lines).split()).translate(table)


def engine(model, model_dir):
    if model == "v6-small":
        return None  # 产品配置
    from rapidocr import EngineType, LangDet, LangRec, ModelType, OCRVersion, RapidOCR

    return RapidOCR(params={
        "Global.log_level": "error", "Global.model_root_dir": model_dir,
        "EngineConfig.onnxruntime.intra_op_num_threads": config.OCR_THREADS,
        "Det.engine_type": EngineType.ONNXRUNTIME, "Det.lang_type": LangDet.CH,
        "Det.model_type": ModelType.MOBILE, "Det.ocr_version": OCRVersion.PPOCRV5,
        "Rec.engine_type": EngineType.ONNXRUNTIME, "Rec.lang_type": LangRec.CH,
        "Rec.model_type": ModelType.MOBILE, "Rec.ocr_version": OCRVersion.PPOCRV5,
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", required=True)
    parser.add_argument("--model", choices=["v6-small", "v5-mobile"], default="v6-small")
    parser.add_argument("--model-dir", default=".tmp/ocr-models")
    args = parser.parse_args()
    config.OCR = "auto"
    tick = perf_counter()
    ocr._engine = engine(args.model, str(Path(args.model_dir).resolve()))
    ocr._get_engine()
    load_seconds = perf_counter() - tick
    rows, groups = [], {}
    for name, image, truth in cases():
        session = ocr.OcrSession(budget=10**6)
        tick = perf_counter()
        block = session.page(image, name)
        seconds = perf_counter() - tick
        got = normalize(block.text) if block else ""
        want = normalize(truth)
        errors = distance(got, want)
        rows.append(dict(case=name, chars=len(want), errors=errors, cer=round(errors / len(want), 4),
                         confidence=block.confidence if block else 0.0, seconds=round(seconds, 3)))
        group = groups.setdefault(name.split("/")[0], dict(chars=0, errors=0, seconds=0.0, n=0, conf=0.0, exact=0))
        group["chars"] += len(want)
        group["errors"] += errors
        group["seconds"] += seconds
        group["conf"] += rows[-1]["confidence"]
        group["exact"] += errors == 0
        group["n"] += 1
    summary = {name: dict(cases=g["n"], cer=round(g["errors"] / g["chars"], 4), exact=g["exact"],
                          mean_confidence=round(g["conf"] / g["n"], 3), mean_seconds=round(g["seconds"] / g["n"], 3))
               for name, g in groups.items()}
    total_chars = sum(r["chars"] for r in rows)
    result = dict(model=args.model, engine_load_seconds=round(load_seconds, 2), threads=config.OCR_THREADS,
                  overall_cer=round(sum(r["errors"] for r in rows) / total_chars, 4), summary=summary, cases=rows)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("model", "engine_load_seconds", "overall_cer", "summary")}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
