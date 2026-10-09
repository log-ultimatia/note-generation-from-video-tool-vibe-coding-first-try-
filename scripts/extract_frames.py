# -*- coding: utf-8 -*-
"""
第一步：从课程视频里抽取关键帧。

流程：
    1. ffprobe 读元信息，校验视频可读、有画面、有时长
    2. ffmpeg scene 滤镜检测画面切换，输出候选帧 + 时间戳
    3. pHash 感知哈希去掉重复帧（课件视频大量静止画面，这步通常能砍掉一半以上）
    4. 场景检测失效（静态视频检不出切换）时自动退化为定时抽帧
    5. 写出 output/frames_manifest.json，供后续 OCR / 笔记脚本使用

用法：
    python scripts/extract_frames.py                 # 处理 video/ 下所有视频
    python scripts/extract_frames.py lecture1.mp4    # 只处理指定视频
    python scripts/extract_frames.py --force         # 忽略已有结果，重跑
    python scripts/extract_frames.py --keep-rejected # 保留被去重淘汰的帧（调参用）
"""

import argparse
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

import imagehash
from PIL import Image

from common import (
    config,
    ffprobe_json,
    find_videos,
    fmt_time,
    human_size,
    load_json,
    probe_summary,
    run_ffmpeg,
    save_json,
    setup_logger,
)

logger = setup_logger("extract_frames")


# ---------------------------------------------------------------------------
# 场景检测
# ---------------------------------------------------------------------------
def detect_scenes(video: Path, raw_dir: Path, threshold: float):
    """
    跑一遍 ffmpeg，把画面切换帧直接写成 PNG，同时拿到每帧时间戳。

    metadata=print 会把每个通过 select 滤镜的帧信息打到 stdout（file=-），
    格式形如：
        frame:0    pts:0       pts_time:0
        lavfi.scene_score=0.000000

    因为 select 保持原顺序，所以「第 N 个输出文件」必然对应「第 N 段
    pts_time」，我们靠这个顺序把它们配对起来。
    """
    raw_dir.mkdir(parents=True, exist_ok=True)
    for old in raw_dir.glob("scene_*.png"):
        old.unlink()

    pattern = str(raw_dir / "scene_%05d.png")
    stdout = run_ffmpeg(
        [
            "-hide_banner", "-nostdin", "-y",
            "-i", str(video),
            "-an", "-sn",                      # 不要音频和字幕流
            "-vf", f"select='gt(scene,{threshold})',metadata=print:file=-",
            "-fps_mode", "vfr",                # 每个选中帧输出一张，不做帧率补偿
            pattern,
        ],
        logger=logger,
        capture_stdout=True,
    )

    # 解析时间戳和场景分数
    times, scores = [], []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("frame:") and "pts_time:" in line:
            try:
                times.append(float(line.split("pts_time:")[1].split()[0]))
            except (IndexError, ValueError):
                times.append(0.0)
        elif line.startswith("lavfi.scene_score="):
            try:
                scores.append(float(line.split("=", 1)[1]))
            except ValueError:
                scores.append(0.0)

    files = sorted(raw_dir.glob("scene_*.png"))

    # 万一 metadata 输出和文件数对不上（ffmpeg 版本差异），退化为按帧号估算，
    # 保证流程不中断——时间戳精度降级总比整个脚本崩掉好。
    if len(times) != len(files):
        logger.warning(
            "元数据行数(%d)与帧文件数(%d)不一致，时间戳按序号兜底估算",
            len(times), len(files),
        )
        times = times[: len(files)] + [0.0] * max(0, len(files) - len(times))
    if len(scores) != len(files):
        scores = scores[: len(files)] + [0.0] * max(0, len(files) - len(scores))

    return files, times, scores


def detect_fixed_interval(video: Path, raw_dir: Path, interval: float):
    """
    兜底方案：静态课件视频（整节课画面几乎不变）scene 检测可能一帧都抽不出，
    这时按固定间隔抽帧，保证至少有内容可做 OCR。
    时间戳为近似值（第 i 帧约在 (i-1)*interval 秒）。
    """
    raw_dir.mkdir(parents=True, exist_ok=True)
    for old in raw_dir.glob("scene_*.png"):
        old.unlink()

    logger.warning("启用兜底定时抽帧：每 %.0f 秒一帧", interval)
    run_ffmpeg(
        [
            "-hide_banner", "-nostdin", "-y",
            "-i", str(video),
            "-an", "-sn",
            "-vf", f"fps=1/{interval}",
            "-fps_mode", "vfr",
            str(raw_dir / "scene_%05d.png"),
        ],
        logger=logger,
    )
    files = sorted(raw_dir.glob("scene_*.png"))
    times = [(i) * interval for i in range(len(files))]
    scores = [0.0] * len(files)
    return files, times, scores


# ---------------------------------------------------------------------------
# pHash 去重
# ---------------------------------------------------------------------------
def dedup(files, times, scores, phash_threshold: float, min_gap: float, keep_rejected: bool):
    """
    感知哈希去重。

    和「上一个保留帧」比而不是「上一帧」：否则画面缓慢漂移时，
    相邻帧差异都很小、帧帧都通过，误差累积到最后会留下大量冗余帧。

    判定规则：
      - 哈希距离 <= phash_threshold          -> 视觉重复，丢弃
      - 间隔 < min_gap 且距离 <= 阈值*2       -> 快速近似重复（打字动画、光标移动），丢弃
    """
    kept, rejected = [], []
    last_hash = None
    last_time = None

    for idx, (path, t, score) in enumerate(zip(files, times, scores)):
        try:
            with Image.open(path) as im:
                h = imagehash.phash(im.convert("RGB"))
        except Exception as exc:                       # 坏帧不该拖垮整批
            logger.warning("跳过无法读取的帧 %s: %s", path.name, exc)
            rejected.append((path, t, score, None))
            continue

        reason = None
        if last_hash is not None:
            dist = h - last_hash
            if dist <= phash_threshold:
                reason = f"视觉重复(距离{dist})"
            elif last_time is not None and (t - last_time) < min_gap and dist <= phash_threshold * 2:
                reason = f"密集近似帧(间隔{t - last_time:.1f}s 距离{dist})"

        if reason is None:
            kept.append({"path": path, "time_sec": t, "score": score, "phash": str(h)})
            last_hash, last_time = h, t
        else:
            rejected.append((path, t, score, reason))
            if idx % 50 == 0:
                logger.debug("第 %d 帧被去重：%s", idx, reason)

    if not keep_rejected:
        for path, _, _, _ in rejected:
            path.unlink(missing_ok=True)

    return kept, rejected


# ---------------------------------------------------------------------------
# 单个视频处理
# ---------------------------------------------------------------------------
def process_video(video: Path, args, ) -> dict:
    logger.info("=" * 64)
    logger.info("处理视频：%s（%s）", video.name, human_size(video.stat().st_size))

    info = probe_summary(ffprobe_json(video, logger))
    if info["duration"] <= 0:
        raise RuntimeError(f"{video.name} 时长读到 0，文件可能损坏")
    if info["width"] == 0:
        raise RuntimeError(f"{video.name} 没有视频流，无法抽帧")

    logger.info(
        "元信息：%d×%d @%.2ffps，时长 %s，音频 %s",
        info["width"], info["height"], info["fps"], fmt_time(info["duration"]),
        info["audio_codec"] or "无",
    )

    # 每个视频单独一个子目录，多视频不会互相覆盖
    video_frames_dir = config.FRAMES_PATH / video.stem
    raw_dir = video_frames_dir / "_raw"
    if video_frames_dir.exists():
        shutil.rmtree(video_frames_dir)
    video_frames_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    files, times, scores = detect_scenes(video, raw_dir, config.SCENE_THRESHOLD)
    used_fallback = False
    logger.info("场景检测到 %d 个候选帧，耗时 %.1fs", len(files), time.time() - t0)

    if len(files) < config.MIN_SCENE_FRAMES:
        logger.warning(
            "候选帧只有 %d 个（低于 MIN_SCENE_FRAMES=%d），可能是静态视频",
            len(files), config.MIN_SCENE_FRAMES,
        )
        used_fallback = True
        files, times, scores = detect_fixed_interval(
            video, raw_dir, config.FALLBACK_INTERVAL_SEC
        )
        logger.info("兜底定时抽帧得到 %d 帧", len(files))

    t0 = time.time()
    kept, rejected = dedup(
        files, times, scores,
        config.PHASH_THRESHOLD, config.MIN_FRAME_GAP_SEC, args.keep_rejected,
    )
    logger.info(
        "去重：保留 %d 帧，丢弃 %d 帧（%.1f%%），耗时 %.1fs",
        len(kept), len(rejected),
        100.0 * len(rejected) / max(1, len(files)),
        time.time() - t0,
    )

    # 重命名为带时间戳的稳定文件名：f0001_000123s.png（序号 + 秒数）
    # 不直接沿用 scene_00001.png，因为去重后序号会断号，不利于人工翻看
    frame_records = []
    for i, item in enumerate(kept, start=1):
        sec = int(round(item["time_sec"]))
        new_path = video_frames_dir / f"f{i:04d}_{sec:06d}s.png"
        item["path"].rename(new_path)
        frame_records.append({
            "index": i,
            "file": str(new_path.relative_to(config.PROJECT_ROOT)).replace("\\", "/"),
            "time_sec": round(item["time_sec"], 3),
            "time": fmt_time(item["time_sec"]),
            "scene_score": round(item["score"], 4),
            "phash": item["phash"],
        })

    # 原始候选帧目录清掉（保留的那批已经改名移走了）
    if raw_dir.exists():
        shutil.rmtree(raw_dir, ignore_errors=True)

    if not frame_records:
        raise RuntimeError(f"{video.name} 抽帧结果为空，请检查视频内容或降低 SCENE_THRESHOLD")

    logger.info("保留帧示例：%s", ", ".join(r["time"] for r in frame_records[:8]))
    logger.info("保留帧范围：%s ~ %s", frame_records[0]["time"], frame_records[-1]["time"])

    return {
        "video": video.name,
        "video_path": str(video.relative_to(config.PROJECT_ROOT)).replace("\\", "/"),
        "frames_dir": str(video_frames_dir.relative_to(config.PROJECT_ROOT)).replace("\\", "/"),
        **info,
        "scene_candidates": len(files),
        "kept": len(frame_records),
        "rejected": len(rejected),
        "used_fallback": used_fallback,
        "frames": frame_records,
    }


# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="从课程视频抽关键帧（scene 检测 + pHash 去重）")
    parser.add_argument("video", nargs="?", help="只处理指定视频文件名（默认处理 video/ 下全部）")
    parser.add_argument("--force", action="store_true", help="忽略已有结果，强制重跑")
    parser.add_argument("--keep-rejected", action="store_true", help="保留被去重淘汰的帧")
    args = parser.parse_args()

    config.check_tools()
    config.ensure_dirs()

    videos = find_videos()
    if not videos:
        logger.error("video/ 目录里没有视频。请把课程视频放进去：%s", config.VIDEO_PATH)
        logger.error("支持格式：%s", " ".join(config.VIDEO_EXTENSIONS))
        return 1

    if args.video:
        target = [v for v in videos if v.name == args.video or v.stem == args.video]
        if not target:
            logger.error("没找到视频：%s。video/ 下现有：%s",
                         args.video, ", ".join(v.name for v in videos))
            return 1
        videos = target

    if not args.force and config.FRAMES_MANIFEST.exists():
        try:
            old = load_json(config.FRAMES_MANIFEST)
            done = {v["video"] for v in old.get("videos", [])}
            skip = [v for v in videos if v.name in done]
            if skip and not args.video:
                logger.info("已有结果，跳过：%s（--force 可强制重跑）",
                            ", ".join(v.name for v in skip))
                videos = [v for v in videos if v.name not in done]
        except Exception:
            pass

    if not videos:
        logger.info("没有需要处理的视频。")
        return 0

    results = []
    failed = []
    for v in videos:
        try:
            results.append(process_video(v, args))
        except Exception as exc:
            logger.error("处理 %s 失败：%s", v.name, exc)
            failed.append({"video": v.name, "error": str(exc)})

    if not results:
        logger.error("全部视频处理失败。")
        return 1

    save_json(config.FRAMES_MANIFEST, {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "params": {
            "scene_threshold": config.SCENE_THRESHOLD,
            "phash_threshold": config.PHASH_THRESHOLD,
            "min_frame_gap_sec": config.MIN_FRAME_GAP_SEC,
            "fallback_interval_sec": config.FALLBACK_INTERVAL_SEC,
        },
        "videos": results,
        "failed": failed,
    }, logger)

    total = sum(r["kept"] for r in results)
    logger.info("=" * 64)
    logger.info("完成：%d 个视频，共保留 %d 帧", len(results), total)
    for r in results:
        logger.info("  %-32s %4d 帧  %s", r["video"][:32], r["kept"],
                    "（兜底抽帧）" if r["used_fallback"] else "")
    logger.info("下一步：python scripts/ocr_frames.py")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
