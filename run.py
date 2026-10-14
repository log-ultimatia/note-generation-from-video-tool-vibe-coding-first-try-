# -*- coding: utf-8 -*-
"""
总入口：一条命令跑完整条流水线（抽帧 → 转写 → OCR → 合并 → 生成笔记）。

    把课程视频丢进 video/ 目录，然后在项目根目录执行：
        python run.py

依次执行：
    step1  抽取关键帧 + 时间戳          -> output/{视频}_scene_info.json
    step2  Whisper 语音转写 SRT         -> output/{视频}.srt
    step3  RapidOCR 识别帧文字（带坐标） -> output/{视频}_ocr.json
    step4  pHash 合并场景 + 对齐字幕    -> output/{视频}_scenes.json
    step5  DeepSeek 生成标签与中文笔记   -> output/{视频}_final_notes.md

任一步骤返回非 0 退出码就立即停止并提示。

可重复执行（不必每次从头跑）：
    - step2 带 --skip-existing：已有 srt 就跳过，不会重复花几十分钟转写
    - step5 默认跳过已生成 tag 的场景，中断后可安全续跑
    - 想彻底重跑某一步，单独执行那个脚本并加对应参数（如 --force）

常用参数：
    python run.py              # 完整跑一遍
    python run.py --from 3     # 从 step3 开始（前两步跳过）
    python run.py --only 5     # 只跑 step5
"""

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# 项目根目录 = 本文件所在目录
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402

# (步骤号, 脚本名, 说明, 附加参数)
STEPS = [
    (1, "step1_extract_frames.py", "抽取关键帧 + 记录时间戳", []),
    (2, "step2_transcribe.py", "Whisper 语音转写 SRT", ["--skip-existing"]),
    (3, "step3_ocr.py", "RapidOCR 识别帧文字（带 x/y 坐标）", []),
    (4, "step4_merge_scenes.py", "pHash 合并同类场景 + 对齐字幕", []),
    (5, "step5_generate_notes.py", "DeepSeek 生成场景标签与中文笔记", []),
]


def build_env() -> dict:
    """
    子进程环境：强制 UTF-8。

    Windows 控制台默认 GBK，而这些脚本的输出全是中文，不统一编码容易出现乱码。
    （脚本自身写日志文件时已经显式指定 utf-8，这里只管控制台显示。）
    """
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def preflight() -> bool:
    """跑之前先做基本检查，避免跑了十分钟才因为 ffmpeg 路径写错而失败。"""
    print("=" * 64)
    print("课程笔记流水线预检")
    print("=" * 64)

    try:
        config.check_tools()
    except Exception as exc:
        print(f"[错误] {exc}")
        return False
    print(f"[ok] ffmpeg  : {config.FFMPEG_PATH}")

    videos = sorted(
        p for p in config.VIDEO_PATH.iterdir()
        if p.is_file() and p.suffix.lower() in config.VIDEO_EXTENSIONS
    ) if config.VIDEO_PATH.exists() else []
    if not videos:
        print(f"[错误] video/ 里没有视频：{config.VIDEO_PATH}")
        print(f"       支持格式：{' '.join(config.VIDEO_EXTENSIONS)}")
        return False
    print(f"[ok] 待处理视频 {len(videos)} 个：")
    for v in videos:
        print(f"       {v.name}  ({v.stat().st_size / 1024 / 1024:.1f} MB)")

    # API Key 只被 step5 用到，缺了不阻断前四步，但必须提前提醒
    try:
        config.check_api_key()
        print("[ok] DeepSeek API Key 已配置")
    except Exception as exc:
        print(f"[警告] {exc}")
        print("       step1~step4 不受影响，但 step5 会失败。")

    print()
    return True


def run_step(number: int, script: str, desc: str, extra_args: list, env: dict) -> bool:
    """执行单个步骤。返回是否成功。"""
    # 用 sys.executable 而不是裸 "python"：保证跑的是当前这个解释器，
    # 不受 PATH 里其它 python（conda/venv/py launcher）影响。
    cmd = [sys.executable, str(PROJECT_ROOT / "scripts" / script), *extra_args]

    print(f"=== 正在执行 step{number} ===")
    print(f"    {desc}")
    print(f"    {' '.join(cmd)}")

    started = time.time()
    try:
        # check=True：非 0 退出码直接抛 CalledProcessError，由下面统一处理
        subprocess.run(cmd, check=True, cwd=str(PROJECT_ROOT), env=env)
    except subprocess.CalledProcessError as exc:
        elapsed = time.time() - started
        print()
        print("=" * 64)
        print(f"!!! step{number} 失败（退出码 {exc.returncode}），流水线已停止")
        print("=" * 64)
        print(f"  失败步骤：step{number} - {desc}")
        print(f"  出错脚本：scripts/{script}")
        print(f"  已耗时  ：{elapsed:.1f} 秒")
        print(f"  排查日志：logs/step{number}.log")
        print()
        print(f"  修好后可从这一步继续：python run.py --from {number}")
        return False
    except FileNotFoundError:
        print(f"!!! 找不到脚本：{PROJECT_ROOT / 'scripts' / script}")
        return False

    print(f"=== step{number} 完成 ===")
    print(f"    耗时 {time.time() - started:.1f} 秒\n")
    return True


def report_notes() -> None:
    """完成后列出所有最终笔记文件。"""
    notes = sorted(config.OUTPUT_PATH.glob("*_final_notes.md"))
    print("=" * 64)
    if not notes:
        print("没有找到任何 *_final_notes.md —— 请检查上面各步骤的日志。")
        return
    print(f"全部完成，共生成 {len(notes)} 份笔记：")
    print("=" * 64)
    for path in notes:
        size_kb = path.stat().st_size / 1024
        # 数一下里面有多少段笔记（"## 时间 - 标签" 标题数）
        try:
            sections = sum(
                1 for line in path.read_text(encoding="utf-8").splitlines()
                if line.startswith("## ")
            )
        except OSError:
            sections = 0
        print(f"  {path}")
        print(f"      {sections} 段，{size_kb:.1f} KB")
    print()
    print("提示：笔记里的图片引用是相对 output/ 的 ../frames/... 路径，")
    print("      用支持 markdown 预览的编辑器（如 VS Code）打开即可看到 PPT 配图。")


def main() -> int:
    parser = argparse.ArgumentParser(description="课程笔记流水线总入口")
    parser.add_argument("--from", dest="from_step", type=int, default=1,
                        metavar="N", help="从第 N 步开始执行（1~5，默认 1）")
    parser.add_argument("--only", dest="only_step", type=int, default=None,
                        metavar="N", help="只执行第 N 步")
    args = parser.parse_args()

    steps = STEPS
    if args.only_step is not None:
        steps = [s for s in STEPS if s[0] == args.only_step]
        if not steps:
            print(f"[错误] --only 只能是 1~5，收到 {args.only_step}")
            return 2
    elif args.from_step > 1:
        steps = [s for s in STEPS if s[0] >= args.from_step]
        if not steps:
            print(f"[错误] --from 只能是 1~5，收到 {args.from_step}")
            return 2

    if not preflight():
        return 1

    config.ensure_dirs()
    env = build_env()
    overall_start = datetime.now()

    for number, script, desc, extra in steps:
        if not run_step(number, script, desc, extra, env):
            return 1

    report_notes()
    total = (datetime.now() - overall_start).total_seconds()
    print(f"总耗时 {total / 60:.1f} 分钟")
    return 0


if __name__ == "__main__":
    sys.exit(main())
