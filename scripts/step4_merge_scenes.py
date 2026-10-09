# -*- coding: utf-8 -*-
"""
阶段 4：合并同类 PPT 场景（pHash）

流程：
    1. 读取 output/{视频名}_scene_info.json（step1 的产物：帧文件名 + 时间）
    2. 对每张帧图算 imagehash.phash 指纹
    3. 相邻帧比较：哈希距离 < config.PHASH_THRESHOLD 就并入同一场景
    4. 每个场景保留【第一帧】作为代表图
    5. 从 output/{视频名}_ocr.json 取代表图的 OCR 文字
    6. 解析 output/{视频名}.srt，把时间区间与场景有重叠的文本拼起来作为 transcript
    7. 输出 output/{视频名}_scenes.json

输出格式（每个场景一条）：
    {
      "scene_id": 1,
      "image": "scene_000001.jpg",
      "start_time": 0.0,
      "end_time": 15.3,
      "ocr_text": ["..."],              # 纯文字列表（按规范）
      "ocr_items": [{"text": ..., "x": ..., "y": ...}],   # 额外保留坐标
      "transcript": "..."
    }

为什么多输出一个 ocr_items：
    step3 按约定输出了 (x, y) 坐标，用来还原 Python 代码的缩进——纯文字列表
    会把缩进信息丢掉。ocr_text 保持规范要求的字符串列表不变，坐标另放
    ocr_items，step5 生成笔记时可优先用 ocr_items 还原带缩进的代码。
    两者数据同源，只是结构不同。

关于 start_time：
    step1 只在【画面切换】处抽帧，所以视频开头到第一次切换之间的那一页
    本身没有帧（例如 0~7.5 秒的第一页）。第一个场景的 start_time 取
    config.SCENE_FIRST_START（默认 0.0），让开头的讲解文字不至于整段丢失。

用法：
    python scripts/step4_merge_scenes.py               # 处理 output/ 下所有视频
    python scripts/step4_merge_scenes.py _TEST_demo    # 只处理指定视频（调试用）
"""

import argparse
import json
import logging
import re
import subprocess
import sys
import time
from pathlib import Path

import imagehash
from PIL import Image

# 把项目根目录加进 sys.path，保证在任意工作目录下都能 import config
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402

# 每个子进程都要带这个标志，否则会各自弹出控制台黑框（本脚本由 app.py 用
# pythonw 拉起、自身没有控制台，再开 ffmpeg/Edge 时 Windows 会新建一个控制台窗口）。
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


# SRT 时间行：00:00:01,234 --> 00:00:05,678
# 逗号和点号都接受（不同工具导出的 SRT 两种都见得到）
SRT_TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*"
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("step4")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    config.LOGS_PATH.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(config.LOGS_PATH / "step4.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


# ---------------------------------------------------------------------------
# SRT 解析
# ---------------------------------------------------------------------------
def _hms_to_sec(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


def parse_srt(srt_path: Path) -> list:
    """
    解析 SRT，返回 [{"start": float, "end": float, "text": str}, ...]。

    用 utf-8-sig 读取：whisper 写出的 SRT 可能带 BOM，用普通 utf-8 读会让
    第一行的序号变成 "\\ufeff1"，虽然不影响时间行匹配，但读干净点更省事。
    """
    text = srt_path.read_text(encoding="utf-8-sig", errors="replace")
    segments, current = [], None

    for raw in text.splitlines():
        line = raw.strip()
        match = SRT_TIME_RE.search(line)
        if match:
            if current is not None:
                segments.append(current)
            g = match.groups()
            current = {
                "start": _hms_to_sec(g[0], g[1], g[2], g[3]),
                "end": _hms_to_sec(g[4], g[5], g[6], g[7]),
                "lines": [],
            }
            continue
        if not line:
            if current is not None:
                segments.append(current)
                current = None
            continue
        # 序号行（纯数字）出现在时间行之前，此时 current 为 None 会被忽略；
        # 万一文件没有空行分隔，序号会被粘进上一条，这里再兜一层。
        if current is not None:
            if line.isdigit() and not current["lines"]:
                continue
            current["lines"].append(line)

    if current is not None:
        segments.append(current)

    for seg in segments:
        seg["text"] = " ".join(seg.pop("lines")).strip()
    return segments


def srt_text_for_range(segments: list, start: float, end: float) -> str:
    """
    取时间区间 [start, end] 内所有有重叠的字幕并拼接。

    重叠判定用严格不等式：seg.end > start 且 seg.start < end，
    这样正好贴在边界上的字幕不会被重复计入相邻两个场景。
    （跨边界的字幕会同时出现在两个场景里，这是"重叠"语义的必然结果。）
    """
    parts = [
        seg["text"] for seg in segments
        if seg["text"] and seg["end"] > start and seg["start"] < end
    ]
    return " ".join(parts).strip()


# ---------------------------------------------------------------------------
# pHash
# ---------------------------------------------------------------------------
def phash_of(image_path: Path):
    """算感知哈希；单张图失败不该让整批崩掉，返回 None 由调用方处理。"""
    try:
        with Image.open(image_path) as im:
            return imagehash.phash(im.convert("RGB"))
    except Exception:
        return None


def merge_frames(frames: list, threshold: int, logger: logging.Logger):
    """
    相邻帧比较并合并成场景，返回 [[帧, 帧, ...], ...]。

    按规范用【相邻帧】比较（当前帧 vs 上一帧），而不是 vs 场景首帧。
    两种策略的区别：如果画面是缓慢渐变（逐帧都只差一点点），相邻比较会
    一路"接力"下去、把整段并成一个场景；对比首帧则能及时切分。这里遵循
    规范用相邻比较，遇到距离超阈值就开新场景。
    """
    scenes, current = [], []
    hashes = []

    for frame in frames:
        h = phash_of(frame["path"])
        if h is None:
            logger.warning("无法读取帧图，跳过：%s", frame["path"])
            continue

        # 把指纹挂在帧上带出去：写进场景记录后，下次重跑就能用它判断
        # "这张代表图的内容有没有变过"（见 carry_over_notes）
        frame["phash"] = str(h)

        if not current:
            current, hashes = [frame], [h]
            continue

        distance = h - hashes[-1]
        if distance < threshold:
            current.append(frame)
            hashes.append(h)
        else:
            scenes.append(current)
            current, hashes = [frame], [h]

    if current:
        scenes.append(current)
    return scenes


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def video_duration(stem: str) -> float:
    """用 ffprobe 读视频总时长，用于最后一个场景的 end_time；读不到返回 0。"""
    candidates = [
        p for p in config.VIDEO_PATH.glob(f"{stem}.*")
        if p.suffix.lower() in config.VIDEO_EXTENSIONS
    ]
    if not candidates:
        return 0.0
    try:
        proc = subprocess.run(
            [config.FFPROBE_PATH, "-v", "error", "-show_entries",
             "format=duration", "-of", "csv=p=0", str(candidates[0])],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        return float((proc.stdout or "0").strip() or 0)
    except (ValueError, OSError):
        return 0.0


def load_ocr(video: str, logger: logging.Logger) -> dict:
    """读 step3 的 OCR 结果；缺失时返回空字典，让流程继续而不是崩掉。"""
    path = config.OUTPUT_PATH / f"{video}_ocr.json"
    if not path.exists():
        logger.warning("缺少 %s，本次 ocr_text 将全部为空", path.name)
        logger.warning("  请先运行：python scripts/step3_ocr.py")
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.error("读取 %s 失败：%s", path.name, exc)
        return {}


# ---------------------------------------------------------------------------
def process_video(video: str, logger: logging.Logger) -> list:
    scene_info_path = config.OUTPUT_PATH / f"{video}_scene_info.json"
    info = json.loads(scene_info_path.read_text(encoding="utf-8"))
    if not info:
        raise RuntimeError(f"{scene_info_path.name} 是空的")

    frames_dir = config.FRAMES_PATH / video
    frames = []
    for item in info:
        path = frames_dir / item["image"]
        if not path.exists():
            logger.warning("帧图不存在，跳过：%s", path)
            continue
        frames.append({"image": item["image"], "time": float(item["time"]), "path": path})

    if not frames:
        raise RuntimeError(f"{video}：没有任何可用的帧图（检查 {frames_dir}）")
    frames.sort(key=lambda f: f["time"])
    logger.info("读入 %d 帧（%.2fs ~ %.2fs）", len(frames), frames[0]["time"], frames[-1]["time"])

    # 1) pHash 合并
    t0 = time.time()
    merged = merge_frames(frames, config.PHASH_THRESHOLD, logger)
    logger.info("pHash 合并：%d 帧 -> %d 个场景（阈值 %d，耗时 %.1fs）",
                len(frames), len(merged), config.PHASH_THRESHOLD, time.time() - t0)

    # 2) OCR 与 SRT
    ocr = load_ocr(video, logger)
    srt_path = config.OUTPUT_PATH / f"{video}.srt"
    if srt_path.exists():
        segments = parse_srt(srt_path)
        logger.info("解析 SRT：%d 条字幕", len(segments))
    else:
        segments = []
        logger.warning("缺少 %s，本次 transcript 将全部为空", srt_path.name)
        logger.warning("  请先运行：python scripts/step2_transcribe.py")

    # 3) 组装场景；end_time 用下一个场景的起始时间，保证首尾相接不留缝
    duration = video_duration(video)
    if duration <= 0:
        # 视频文件找不到时，退而用最后一条字幕的结束时间兜底
        duration = max((s["end"] for s in segments), default=0.0)
        if duration > 0:
            logger.warning("读不到视频时长，末场景 end_time 用最后一条字幕的结束时间 %.2fs", duration)

    first_start = getattr(config, "SCENE_FIRST_START", 0.0)
    records = []
    for i, group in enumerate(merged):
        rep = group[0]                    # 规范：保留第一帧作为代表图
        start = first_start if i == 0 else rep["time"]
        if i + 1 < len(merged):
            end = merged[i + 1][0]["time"]
        else:
            end = max(duration, rep["time"])
        if end <= start:                  # 防御：时间戳异常时至少保证区间非空
            end = start

        items = ocr.get(rep["image"], [])
        records.append({
            "scene_id": i + 1,
            "image": rep["image"],
            "start_time": round(start, 3),
            "end_time": round(end, 3),
            "ocr_text": [it.get("text", "") for it in items if it.get("text")],
            "ocr_items": items,           # 额外保留 x/y，供 step5 还原代码缩进
            "transcript": srt_text_for_range(segments, start, end),
            "frame_count": len(group),    # 该场景合并了多少帧（便于核对合并效果）
            # 代表图的感知哈希：下次重跑时用它判断"画面内容有没有变过"。
            # 必须写进记录里，否则 carry_over_notes 读到 None、校验会被静默跳过。
            "phash": rep.get("phash"),
        })

    # 沿用上一次 step5 生成的笔记，否则重跑会把 API 全部重烧一遍
    kept = carry_over_notes(video, records, logger)
    if kept:
        logger.info("沿用了上次已生成的 %d/%d 个场景笔记（step5 会自动跳过它们）",
                    kept, len(records))

    logger.info("场景时间轴：%s", ", ".join(
        f"#{r['scene_id']} {r['start_time']:.1f}~{r['end_time']:.1f}s" for r in records[:8]
    ) + (" …" if len(records) > 8 else ""))
    return records


def carry_over_notes(video: str, records: list, logger: logging.Logger) -> int:
    """
    把上一次 step5 生成的 tag/summary/note 沿用到本次重建的场景里。

    为什么必须做这一步：step4 每次都会【完全重建】scenes.json，
    而 step5 的成果（tag/summary/note）恰恰写在这个文件里。不做沿用的话，
    只要再跑一次 run.py 或 app，step5 就得把所有场景重新调一遍 API——
    一节 40 分钟的课就是 125 次调用、20 多分钟、以及对应的费用。

    按【代表图文件名】匹配：抽帧是确定性的（同样的视频 + 同样的阈值必然
    得到同样的帧），所以同一张代表图对应的就是同一页幻灯片。

    但文件名相同【不代表画面内容没变】——step1 做过"转场稳定化"之后，
    同名帧可能已经从"淡变中的黑帧"换成了"稳定画面"。所以还要比对感知哈希，
    内容变过就不沿用，让它重新生成，避免把基于黑帧写出来的笔记留在那里。
    """
    path = config.OUTPUT_PATH / f"{video}_scenes.json"
    if not path.exists():
        return 0
    try:
        previous = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("读取旧的 %s 失败，无法沿用既有笔记：%s", path.name, exc)
        return 0

    by_image = {s.get("image"): s for s in previous if s.get("image")}
    kept, changed = 0, 0
    for record in records:
        old = by_image.get(record["image"])
        if not old:
            continue
        # 画面内容变过就不沿用（用感知哈希判断，容忍轻微的编码差异）
        old_hash, new_hash = old.get("phash"), record.get("phash")
        if old_hash and new_hash and old_hash != new_hash:
            try:
                if imagehash.hex_to_hash(old_hash) - imagehash.hex_to_hash(new_hash) > 4:
                    changed += 1
                    continue
            except Exception:
                pass
        if not old:
            continue
        for field in ("tag", "summary", "note"):
            value = old.get(field)
            if isinstance(value, str) and value.strip():
                record[field] = value
        if (record.get("tag") or "").strip():
            kept += 1
    if changed:
        logger.info("有 %d 个场景的代表图内容变过（step1 转场稳定化的结果），"
                    "不沿用旧笔记，会重新生成", changed)
    return kept


def main() -> int:
    parser = argparse.ArgumentParser(description="阶段4：pHash 合并同类 PPT 场景 + 对齐字幕")
    parser.add_argument("video", nargs="?", help="只处理指定视频名（默认处理 output/ 下全部）")
    args = parser.parse_args()

    logger = setup_logger()
    config.ensure_dirs()

    infos = sorted(config.OUTPUT_PATH.glob("*_scene_info.json"))
    videos = [p.name[: -len("_scene_info.json")] for p in infos]
    if args.video:
        videos = [v for v in videos if v == args.video]

    if not videos:
        logger.error("output/ 下没有 *_scene_info.json，请先运行：python scripts/step1_extract_frames.py")
        return 1

    logger.info("=" * 60)
    logger.info("开始合并场景：%d 个视频（PHASH_THRESHOLD=%d）",
                len(videos), config.PHASH_THRESHOLD)

    started = time.time()
    ok, total_scenes = 0, 0

    for video in videos:
        logger.info("-" * 60)
        logger.info("处理：%s", video)
        try:
            records = process_video(video, logger)
        except Exception as exc:
            logger.error("失败：%s -> %s", video, exc)
            continue

        out_json = config.OUTPUT_PATH / f"{video}_scenes.json"
        out_json.write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        ok += 1
        total_scenes += len(records)
        empty_ocr = sum(1 for r in records if not r["ocr_text"])
        empty_txt = sum(1 for r in records if not r["transcript"])
        logger.info("已写出 %s（%d 个场景，空 OCR %d，空字幕 %d）",
                    out_json.name, len(records), empty_ocr, empty_txt)

    logger.info("=" * 60)
    logger.info("完成：%d 个视频，共 %d 个场景，耗时 %.1fs",
                ok, total_scenes, time.time() - started)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
