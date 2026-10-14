# -*- coding: utf-8 -*-
"""
【自测工具，不是主流程】生成一个合成的"课件视频"，用来在没有真实课程视频时
验证整条流水线（抽帧 → OCR → 转写 → 生成笔记）是否正常。

为什么需要它：真实课程视频体积大、不方便反复调试，而合成视频可以精确控制
"画面切换时间点"和"幻灯片文字"，便于检查抽帧和 OCR 的召回率。

视频内容：若干张 PIL 画的幻灯片（含英文、中文、代码），配一段真实语音音轨。
语音默认用 logs/_jfk.flac（11 秒，Whisper 官方测试音频）；没有音轨时会用静音。

用法：
    python scripts/make_test_video.py                          # 默认 8 张幻灯片
    python scripts/make_test_video.py --slide-sec 12 --out video/_TEST_long.mp4
    python scripts/make_test_video.py --audio D:/some.wav
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from common import config, setup_logger

logger = setup_logger("make_test_video")

W, H = 1280, 720


def _font(size: int, kind: str = "cn"):
    """优先用微软雅黑（中文），代码用 Consolas；都取不到就退回内置字体。"""
    candidates = {
        "cn": ["msyh.ttc", "simhei.ttf", "simsun.ttc"],
        "code": ["consola.ttf", "cour.ttf"],
    }[kind]
    for name in candidates:
        try:
            return ImageFont.truetype(f"C:/Windows/Fonts/{name}", size)
        except Exception:
            continue
    return ImageFont.load_default()


SLIDES = [
    ("示例课程：程序设计入门", [
        ("示例讲座 01", 60, "cn"),
        ("Recursion and Higher-Order Functions", 36, "cn"),
        ("", 20, "cn"),
        ("示例大学  |  第一学期", 28, "cn"),
    ], (240, 248, 255)),
    ("What is Recursion?", [
        ("Recursion: a function that calls itself", 40, "cn"),
        ("", 16, "cn"),
        ("Every recursive function needs:", 34, "cn"),
        ("  1. base case  (when to stop)", 32, "cn"),
        ("  2. recursive call (smaller problem)", 32, "cn"),
        ("", 16, "cn"),
        ("递归函数必须要有基准情形，否则会无限递归。", 30, "cn"),
    ], (255, 250, 240)),
    ("Factorial, recursively", [
        ("Factorial in Python", 38, "cn"),
        ("", 14, "cn"),
        ("def fact(n):", 32, "code"),
        ("    if n == 0:", 32, "code"),
        ("        return 1", 32, "code"),
        ("    return n * fact(n - 1)", 32, "code"),
        ("", 14, "cn"),
        ("fact(5) = 5 * 4 * 3 * 2 * 1 = 120", 30, "cn"),
    ], (245, 255, 245)),
    ("The Recursive Process", [
        ("fact(3) evaluation", 40, "cn"),
        ("", 14, "cn"),
        ("fact(3) -> 3 * fact(2)", 32, "code"),
        ("        -> 3 * 2 * fact(1)", 32, "code"),
        ("        -> 3 * 2 * 1 * fact(0)", 32, "code"),
        ("        -> 3 * 2 * 1 * 1", 32, "code"),
        ("        -> 6", 32, "code"),
        ("", 14, "cn"),
        ("计算过程会展开，再逐层回收结果。", 30, "cn"),
    ], (255, 245, 250)),
    ("Fibonacci: naive recursion", [
        ("fib(n) = fib(n-1) + fib(n-2)", 38, "cn"),
        ("", 14, "cn"),
        ("def fib(n):", 32, "code"),
        ("    if n < 2:", 32, "code"),
        ("        return n", 32, "code"),
        ("    return fib(n - 1) + fib(n - 2)", 32, "code"),
        ("", 14, "cn"),
        ("复杂度 O(2^n)，存在大量重复计算。", 30, "cn"),
    ], (255, 255, 235)),
    ("Why it is slow", [
        ("Overlapping subproblems", 40, "cn"),
        ("", 14, "cn"),
        ("fib(5) calls fib(3) twice", 32, "cn"),
        ("fib(4) calls fib(2) three times", 32, "cn"),
        ("", 14, "cn"),
        ("重复子问题导致指数级的时间开销。", 30, "cn"),
        ("Memoization can fix this: 记忆化搜索", 30, "cn"),
    ], (240, 255, 255)),
    ("Common mistakes", [
        ("Watch out for these:", 40, "cn"),
        ("", 14, "cn"),
        ("1. Missing base case  -> RecursionError", 30, "cn"),
        ("2. Not shrinking the problem", 30, "cn"),
        ("3. Using recursion where a loop is fine", 30, "cn"),
        ("4. Forgetting to return the recursive call", 30, "cn"),
        ("", 14, "cn"),
        ("忘记 return 是新手最常见的错误。", 30, "cn"),
    ], (250, 240, 245)),
    ("Summary", [
        ("Key takeaways", 44, "cn"),
        ("", 16, "cn"),
        ("- Recursion = base case + smaller subproblem", 30, "cn"),
        ("- Trust the recursion (recursive leap of faith)", 30, "cn"),
        ("- Tree recursion can be exponential", 30, "cn"),
        ("- Memoization trades space for time", 30, "cn"),
    ], (245, 245, 250)),
]


def draw_slide(title: str, lines, bg) -> Image.Image:
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    # 顶部标题栏
    d.rectangle([0, 0, W, 88], fill=(38, 50, 76))
    d.text((40, 24), title, font=_font(38, "cn"), fill=(255, 255, 255))
    y = 130
    for text, size, kind in lines:
        if text:
            d.text((60, y), text, font=_font(size, kind), fill=(20, 20, 20))
        y += int(size * 1.45)
    # 右下角页码，方便肉眼核对抽帧结果
    d.text((W - 140, H - 50), "示例课程", font=_font(22, "cn"), fill=(150, 150, 150))
    return img


def main():
    ap = argparse.ArgumentParser(description="生成自测用的合成课件视频")
    ap.add_argument("--out", default=str(config.VIDEO_PATH / "_TEST_demo.mp4"))
    ap.add_argument("--slide-sec", type=float, default=7.5, help="每张幻灯片停留秒数")
    ap.add_argument("--audio", default=str(config.LOGS_PATH / "_jfk.flac"),
                    help="音轨文件；不存在则用静音")
    ap.add_argument("--fps", type=int, default=25)
    ap.add_argument("--slides", type=int, default=len(SLIDES), help="使用前 N 张幻灯片")
    args = ap.parse_args()

    config.check_tools()
    config.ensure_dirs()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    slides = SLIDES[: max(1, min(args.slides, len(SLIDES)))]
    total = len(slides) * args.slide_sec

    # 构建目录放在项目内而不是系统 temp：DSH 沙箱只允许写工作区，
    # 写到 %TEMP% 会被拒绝（PermissionError）。用完在 finally 里删掉。
    build = config.PROJECT_ROOT / ".testvid_build"
    if build.exists():
        shutil.rmtree(build, ignore_errors=True)
    build.mkdir(parents=True, exist_ok=True)
    try:
        logger.info("生成 %d 张幻灯片（每张 %.1fs，共 %.1fs）", len(slides), args.slide_sec, total)
        list_lines = []
        for i, (title, lines, bg) in enumerate(slides, start=1):
            p = build / f"slide_{i:02d}.png"
            draw_slide(title, lines, bg).save(p)
            list_lines.append(f"file '{p.as_posix()}'")
            list_lines.append(f"duration {args.slide_sec}")
        # concat demuxer 要求最后一张图重复一次，否则最后一帧时长会被吞掉
        list_lines.append(f"file '{(build / f'slide_{len(slides):02d}.png').as_posix()}'")
        list_file = build / "list.txt"
        list_file.write_text("\n".join(list_lines), encoding="utf-8")

        audio = Path(args.audio)
        use_audio = audio.exists()
        if not use_audio:
            logger.warning("音轨 %s 不存在，改用静音", audio)

        cmd = [config.FFMPEG_PATH, "-hide_banner", "-nostdin", "-y",
               "-f", "concat", "-safe", "0", "-i", str(list_file)]
        if use_audio:
            cmd += ["-stream_loop", "-1", "-i", str(audio)]   # 循环音轨填满视频
        else:
            cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]

        cmd += [
            "-vf", f"fps={args.fps},format=yuv420p",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
            "-c:a", "aac", "-b:a", "96k",
            "-t", f"{total:.3f}",                            # 精确截断到幻灯片总时长
            "-movflags", "+faststart",
            str(out_path),
        ]
        logger.info("调用 ffmpeg 合成视频…")
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", errors="replace")
            logger.error("ffmpeg 合成失败:\n%s", "\n".join(err.strip().splitlines()[-20:]))
            return 1

        size_mb = out_path.stat().st_size / 1024 / 1024
        logger.info("生成成功：%s（%.1f MB，%.1f 秒，%d 张幻灯片）",
                    out_path, size_mb, total, len(slides))
        logger.info("下一步：python scripts/extract_frames.py")
        return 0
    finally:
        shutil.rmtree(build, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
