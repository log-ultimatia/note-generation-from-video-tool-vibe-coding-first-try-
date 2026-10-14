# -*- coding: utf-8 -*-
"""
阶段 3：RapidOCR 批量识别关键帧文字

流程：
    1. 初始化 RapidOCR 引擎（本地 ONNX，模型随包内置，不联网）
    2. 遍历 frames/ 下每个视频子目录里的所有 .jpg
    3. 逐张 OCR，取文字 + 文字框左上角坐标 (x, y)（宽高和四点坐标不取）
    4. 结果写入 output/{视频名}_ocr.json，格式：
         {"scene_000001.jpg": [{"text": "if n == 0:", "x": 127, "y": 232}, ...], ...}
    5. 日志写 logs/step3.log

为什么要带 x / y 坐标：
    RapidOCR 只返回文字时，Python 代码的【缩进会全部丢失】（实测幻灯片上
    "    if n == 0:" 会变成 "if n == 0:"），而缩进在 Python 里是有语义的，
    丢了之后笔记里的代码是错的、甚至跑不起来。缩进无法从纯文字还原，
    只能靠文字框的水平位置推断，所以保留左边界 x。
    y 用于确定行序：单栏排版下 RapidOCR 的返回顺序通常就是阅读顺序，但双栏
    或图文混排（左边代码、右边讲解）时会乱，必须靠 y 重新分组排序。
    后续 step4 按 y 分行、按 x 聚类即可还原缩进层级。

用法：
    python scripts/step3_ocr.py                  # 处理 frames/ 下所有视频子目录
    python scripts/step3_ocr.py _TEST_demo       # 只处理指定视频（调试用）

说明：
    - 视频子目录名必须与 step1 输出的视频名一致，否则 output 里的 json 名字会对不上。
    - 置信度低于 config.OCR_MIN_CONFIDENCE 的识别结果会被丢弃（课件里多是噪声）。
      想全部保留就把 config.OCR_MIN_CONFIDENCE 设为 0。
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

# 把项目根目录加进 sys.path，保证在任意工作目录下都能 import config
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("step3")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    config.LOGS_PATH.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(config.LOGS_PATH / "step3.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


# ---------------------------------------------------------------------------
# 引擎初始化
# ---------------------------------------------------------------------------
_OPENVINO_USABLE = [None]       # None=未探测, True/False=结果


def _openvino_usable() -> bool:
    """
    判断能不能用 OpenVINO 引擎。

    为什么要探测而不是无脑用：没装 openvino 时指定 OpenVINO 引擎会直接报错。
    另外【千万别】在没装对应包时去开 use_cuda / use_dml —— 实测那会静默退化到
    一条慢路径（23.1s -> 38.8s，比 CPU 还慢 1.7 倍），把提速变成减速。
    """
    mode = str(getattr(config, "OCR_ENGINE", "auto")).lower()
    if mode == "onnxruntime":
        return False
    if _OPENVINO_USABLE[0] is None:
        try:
            import openvino                       # noqa: F401
            _OPENVINO_USABLE[0] = True
        except ImportError:
            _OPENVINO_USABLE[0] = False
    return _OPENVINO_USABLE[0]


def build_engine(logger: logging.Logger):
    """
    初始化 RapidOCR。

    日志静音（实测有效的做法）：
      rapidocr 在模块级执行 `Logger(log_level=INFO, logger_name="RapidOCR")`，把
      "RapidOCR" 这个 logger 的级别设为 INFO。构造 RapidOCR() 时还会再次触发同一段
      代码，把级别【重新设回 INFO】——所以对 logger 调 setLevel(WARNING) 是无效的，
      实测构造后级别又变回 20（INFO）。

      而 Logger.__init__ 只在"当前没有任何 handler"时才会去改 handler 的级别。
      因此正确做法是把【handler 的级别】压到 CRITICAL：已有的 handler 在构造过程中
      不会被重置，INFO 记录会被 handler 直接丢弃。实测 stderr 行数 9 -> 0。

    线程数是这里最重要的参数：默认 -1 会开满逻辑核心，实测在 28 核机器上严重
    超额订阅、反而最慢（9.57s/张）；设成 8 线程最快（5.83s/张，快 1.64 倍）。
    见 config.OCR_NUM_THREADS 里的完整实测数据。

    引擎优先用 OpenVINO（见 config.OCR_ENGINE）：实测 18 张图
      onnxruntime CPU  23.1s（1.28s/张）
      OpenVINO         12.2s（0.68s/张）  快 1.9 倍，识别结果逐行完全一致
    OpenVINO 不可用时自动退回 onnxruntime，不影响正确性。
    """
    from rapidocr import RapidOCR

    rp_logger = logging.getLogger("RapidOCR")
    for handler in rp_logger.handlers:
        handler.setLevel(logging.CRITICAL)

    threads = getattr(config, "OCR_NUM_THREADS", 0)
    params = {}
    engine_name = "onnxruntime"

    if _openvino_usable():
        try:
            from rapidocr.utils.typings import EngineType
            params["Det.engine_type"] = EngineType.OPENVINO
            params["Rec.engine_type"] = EngineType.OPENVINO
            if threads and threads > 0:
                params["EngineConfig.openvino.inference_num_threads"] = threads
            engine_name = "OpenVINO"
        except Exception as exc:
            logger.warning("OpenVINO 引擎配置失败，改用 onnxruntime：%s", exc)
            params = {}

    if engine_name != "OpenVINO" and threads and threads > 0:
        # 参数是扁平的"点号键"格式，不是嵌套字典（嵌套会报 not a valid key）
        params["EngineConfig.onnxruntime.intra_op_num_threads"] = threads

    t0 = time.time()
    try:
        engine = RapidOCR(params=params) if params else RapidOCR()
    except Exception as exc:
        if engine_name == "OpenVINO":
            logger.warning("OpenVINO 初始化失败（%s），退回 onnxruntime", exc)
            _OPENVINO_USABLE[0] = False
            fallback = {}
            if threads and threads > 0:
                fallback["EngineConfig.onnxruntime.intra_op_num_threads"] = threads
            engine = RapidOCR(params=fallback) if fallback else RapidOCR()
            engine_name = "onnxruntime"
        else:
            raise
    logger.info("OCR 引擎：%s", engine_name)

    # 构造完再把 handler 放宽到 WARNING：构造期的噪声已经不打了，
    # 之后若真出问题（WARNING/ERROR）仍能看见，不至于把问题也一起吞掉。
    for handler in rp_logger.handlers:
        handler.setLevel(logging.WARNING)

    logger.info("RapidOCR 引擎初始化完成，耗时 %.1fs（intra_op 线程=%s）",
                time.time() - t0, threads or "默认")
    return engine


def prepare_input(image_path: Path, logger: logging.Logger):
    """
    返回 rapidocr 可以直接接受的对象。

    默认直接传文件路径，不做任何转换——少一次解码/颜色空间转换，也避免通道顺序搞错。
    只有当长边超过 config.OCR_MAX_SIDE 时才缩放：大图 OCR 明显更慢，缩到 1600 基本
    不损失课件文字精度。缩放用 cv2（输出 BGR，与 rapidocr 的 OpenCV 约定一致）。
    """
    max_side = getattr(config, "OCR_MAX_SIDE", 0)
    if not max_side:
        return str(image_path)

    import cv2
    img = cv2.imread(str(image_path))
    if img is None:
        raise RuntimeError(f"cv2 无法读取图片：{image_path}")
    h, w = img.shape[:2]
    long_side = max(h, w)
    if long_side <= max_side:
        return img                      # 本来就够小，但还是走 ndarray 保持一致

    scale = max_side / long_side
    resized = cv2.resize(img, (int(w * scale), int(h * scale)),
                         interpolation=cv2.INTER_AREA)
    logger.debug("缩放 %s: %dx%d -> %dx%d", image_path.name, w, h,
                 resized.shape[1], resized.shape[0])
    return resized


def _box_xy(box):
    """
    取文字框的左上角坐标 (x, y)，单位像素、取整。四点多边形取最小 x / 最小 y。

    为什么要这两个坐标：
      - x 用于还原 Python 代码的缩进层级（缩进 = 该行 x 与同行组最小 x 的差）。
      - y 用于确定行序和分行：RapidOCR 的返回顺序通常在单栏排版下就是阅读顺序，
        但双栏/图文混排（左边代码、右边讲解）时顺序会乱，必须靠 y 重新分组排序。
    宽高和四点坐标不取，保持输出精简。
    """
    try:
        points = list(box)
        return (
            int(round(min(float(p[0]) for p in points))),
            int(round(min(float(p[1]) for p in points))),
        )
    except Exception:
        return None, None


def ocr_image(engine, image_path: Path, min_conf: float, logger: logging.Logger) -> list:
    """
    对单张图做 OCR，返回 [{"text": 文字, "x": 左边界, "y": 上边界}, ...]。

    文字按置信度过滤、去空白；x/y 坐标用于后续还原代码缩进和行序。
    """
    result = engine(prepare_input(image_path, logger))

    # 没检测到任何文字时 txts 为 None
    texts = list(getattr(result, "txts", None) or [])
    scores = list(getattr(result, "scores", None) or [])
    boxes = getattr(result, "boxes", None)

    n = len(texts)
    # 置信度数量对不上就不做过滤，宁可多留也不要因为字段变化丢掉全部文字
    if len(scores) != n:
        scores = [1.0] * n
    # boxes 与文字数量必须严格对齐，否则坐标会错配到别行文字上——那比没有坐标更糟
    if boxes is None or len(boxes) != n:
        if boxes is not None:
            logger.warning("%s：boxes 数量(%d) 与文字数量(%d) 不一致，本次坐标全部置空",
                           image_path.name, len(boxes), n)
        boxes = [None] * n

    kept, dropped = [], 0
    for text, score, box in zip(texts, scores, boxes):
        text = (text or "").strip()
        if not text:
            continue
        if min_conf and float(score) < min_conf:
            dropped += 1
            continue
        x, y = _box_xy(box) if box is not None else (None, None)
        kept.append({"text": text, "x": x, "y": y})

    if dropped:
        logger.debug("%s: 丢弃 %d 条低置信度结果（< %.2f）",
                     image_path.name, dropped, min_conf)
    return kept


# ---------------------------------------------------------------------------
def process_video_dir(engine, video_dir: Path, logger: logging.Logger) -> dict:
    images = sorted(video_dir.glob("*.jpg"))
    if not images:
        logger.warning("%s：目录里没有 .jpg 图片，跳过", video_dir.name)
        return {}

    logger.info("-" * 60)
    logger.info("处理 %s：%d 张图片", video_dir.name, len(images))
    min_conf = getattr(config, "OCR_MIN_CONFIDENCE", 0.0)

    result = {}
    started = time.time()
    empty_count = 0

    for i, image in enumerate(images, start=1):
        # 暂停检查（安全点）：每张图开始前查一次。
        # OCR 没有增量落盘，中途停下这一轮就白做了——但这一步本来就只有几分钟，
        # 而且比"让电脑一直开着"划算。至少不会在暂停后继续烧 CPU。
        if config.pause_requested():
            logger.info("收到暂停请求，停在 %d/%d 张（本轮结果不保存，下次重做）",
                        i - 1, len(images))
            return None
        try:
            texts = ocr_image(engine, image, min_conf, logger)
        except Exception as exc:
            logger.error("  %s OCR 失败：%s", image.name, exc)
            texts = []
        result[image.name] = texts
        if not texts:
            empty_count += 1
        if i % 20 == 0 or i == len(images):
            elapsed = time.time() - started
            logger.info("  进度 %d/%d（%.1f 秒，均 %.2f 秒/张）",
                        i, len(images), elapsed, elapsed / i)

    elapsed = time.time() - started
    total_lines = sum(len(v) for v in result.values())
    logger.info("完成 %s：%d 张图，共 %d 行文字，空图 %d 张，耗时 %.1fs（均 %.2f 秒/张）",
                video_dir.name, len(images), total_lines, empty_count,
                elapsed, elapsed / len(images))
    if empty_count:
        logger.warning("%s：有 %d 张图没识别出任何文字，可能是纯图片/空白帧",
                       video_dir.name, empty_count)

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="阶段3：RapidOCR 批量识别关键帧文字")
    parser.add_argument("video", nargs="?",
                        help="只处理指定视频子目录名（默认处理 frames/ 下全部）")
    args = parser.parse_args()

    logger = setup_logger()
    config.ensure_dirs()

    if not config.FRAMES_PATH.exists():
        logger.error("frames/ 目录不存在：%s", config.FRAMES_PATH)
        return 1

    # 只认真正含 .jpg 的子目录：这样既能跳过 step1 的临时目录（如 _raw），
    # 又不会误伤名字以 "_" 开头的正常视频（例如 _TEST_demo）。
    video_dirs = sorted(
        d for d in config.FRAMES_PATH.iterdir()
        if d.is_dir() and any(d.glob("*.jpg"))
    )
    if args.video:
        video_dirs = [d for d in video_dirs if d.name == args.video]
    if not video_dirs:
        logger.error("frames/ 下没有可处理的视频子目录：%s", config.FRAMES_PATH)
        logger.error("请先运行：python scripts/step1_extract_frames.py")
        return 1

    logger.info("=" * 60)
    logger.info("开始 OCR：%d 个视频子目录（min_confidence=%.2f, max_side=%s）",
                len(video_dirs), getattr(config, "OCR_MIN_CONFIDENCE", 0.0),
                getattr(config, "OCR_MAX_SIDE", 0) or "不限制")

    # 通用视频默认不做 OCR：画面上的文字多半是水印、台标、字幕碎片，是噪声不是信息，
    # 而 OCR 是这一步的主要成本（约 1 秒/帧）。类型判断见 config.PROFILE_PRESETS。
    todo_dirs, skipped = [], []
    for video_dir in video_dirs:
        preset = config.profile_preset(config.load_profile(video_dir.name, logger))
        (todo_dirs if preset.get("ocr", True) else skipped).append((video_dir, preset))
    for video_dir, preset in skipped:
        logger.info("跳过 OCR：%s（内容类型「%s」不做 OCR）", video_dir.name, preset["label"])
        # 仍然写一个空的 ocr.json：step4 要读它，缺文件会被当成出错
        (config.OUTPUT_PATH / f"{video_dir.name}_ocr.json").write_text(
            "{}", encoding="utf-8")
    if not todo_dirs:
        logger.info("所有视频都按内容类型跳过 OCR，本步骤无事可做")
        return 0
    video_dirs = [d for d, _ in todo_dirs]

    engine = build_engine(logger)

    started = time.time()
    ok, total_images, total_lines = 0, 0, 0

    for video_dir in video_dirs:
        try:
            result = process_video_dir(engine, video_dir, logger)
        except Exception as exc:
            logger.error("处理 %s 失败：%s", video_dir.name, exc)
            continue

        if result is None:          # 收到暂停请求
            logger.info("=" * 60)
            logger.info("已暂停：完成 %d/%d 个视频的 OCR", ok, len(video_dirs))
            return 2                # 退出码 2 = 暂停（0 成功 / 1 失败）
        if not result:
            continue

        out_json = config.OUTPUT_PATH / f"{video_dir.name}_ocr.json"
        out_json.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        ok += 1
        total_images += len(result)
        total_lines += sum(len(v) for v in result.values())
        logger.info("已写出 %s", out_json.name)

    logger.info("=" * 60)
    logger.info("完成：%d 个视频，%d 张图，%d 行文字，总耗时 %.1fs",
                ok, total_images, total_lines, time.time() - started)
    logger.info("下一步：python scripts/step4_*.py（用 OCR 文字 + SRT 生成笔记）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
