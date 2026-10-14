# -*- coding: utf-8 -*-
"""
公共工具：日志、路径引导、ffmpeg 调用、JSON 读写。

所有脚本第一行都这么写，用来拿到项目根目录并 import config：

    from common import config, setup_logger, run_ffmpeg

common.py 自己负责把项目根目录加进 sys.path，所以其它脚本不需要重复那三行。
"""

import json
import logging
import subprocess
import sys
from pathlib import Path

# 把项目根目录加进 sys.path，让 scripts/ 下的脚本能直接 import config
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import config  # noqa: E402  （必须在 sys.path 调整之后导入）


# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
def setup_logger(name: str) -> logging.Logger:
    """
    同时输出到控制台和 logs/<name>.log。

    强制 UTF-8 写文件：Windows 控制台默认 GBK，中文日志会乱码，
    而项目里到处是中文，所以文件日志必须显式指定编码。
    """
    logger = logging.getLogger(name)
    if logger.handlers:          # 避免重复添加 handler
        return logger
    logger.setLevel(logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    config.LOGS_PATH.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(config.LOGS_PATH / f"{name}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger


# ---------------------------------------------------------------------------
# 时间格式化
# ---------------------------------------------------------------------------
def fmt_time(seconds: float) -> str:
    """秒 -> HH:MM:SS，用于文件名、字幕时间轴和笔记锚点。"""
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def fmt_hms_ms(seconds: float) -> str:
    """秒 -> HH:MM:SS,mmm，SRT 字幕格式用。"""
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3600_000)
    m, s = divmod(rem, 60_000)
    sec, milli = divmod(s, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{milli:03d}"


# ---------------------------------------------------------------------------
# ffmpeg / ffprobe
# ---------------------------------------------------------------------------
def run_ffmpeg(args, logger=None, capture_stdout=False, timeout=None):
    """
    调用 ffmpeg。args 是不含可执行文件路径的参数列表。

    统一用 utf-8 + errors="replace" 解码：ffmpeg 的 banner 和文件路径里
    可能有非 ASCII 字符，默认 GBK 解码会抛 UnicodeDecodeError。
    """
    cmd = [config.FFMPEG_PATH, *args]
    if logger:
        logger.debug("执行: %s", " ".join(str(c) for c in cmd))
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg 失败（exit {proc.returncode}）\n命令: {' '.join(str(c) for c in cmd)}\n"
            f"末尾输出:\n" + "\n".join(stderr.strip().splitlines()[-15:])
        )
    return (proc.stdout or b"").decode("utf-8", errors="replace") if capture_stdout else stderr


def ffprobe_json(video: Path, logger=None) -> dict:
    """读取视频元信息（时长/分辨率/帧率/音轨），用于抽帧前校验。"""
    proc = subprocess.run(
        [
            config.FFPROBE_PATH, "-v", "error",
            "-show_entries",
            "format=duration,size,bit_rate:stream=index,codec_type,codec_name,width,height,"
            "r_frame_rate,sample_rate,channels",
            "-of", "json", str(video),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", errors="replace")
        raise RuntimeError(f"ffprobe 读取失败: {video}\n{err.strip()[-500:]}")
    return json.loads((proc.stdout or b"{}").decode("utf-8", errors="replace"))


def probe_summary(info: dict) -> dict:
    """把 ffprobe 的 JSON 提炼成脚本里好用的扁平字段。"""
    fmt = info.get("format", {}) or {}
    video_stream = next(
        (s for s in info.get("streams", []) if s.get("codec_type") == "video"), {}
    )
    audio_stream = next(
        (s for s in info.get("streams", []) if s.get("codec_type") == "audio"), {}
    )
    fps = 0.0
    raw_fps = video_stream.get("r_frame_rate") or "0/1"
    try:
        num, den = raw_fps.split("/")
        fps = float(num) / float(den) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    return {
        "duration": float(fmt.get("duration") or 0.0),
        "size_bytes": int(fmt.get("size") or 0),
        "width": int(video_stream.get("width") or 0),
        "height": int(video_stream.get("height") or 0),
        "fps": round(fps, 3),
        "video_codec": video_stream.get("codec_name") or "",
        "audio_codec": audio_stream.get("codec_name") or "",
        "has_audio": bool(audio_stream),
    }


# ---------------------------------------------------------------------------
# JSON 读写
# ---------------------------------------------------------------------------
def save_json(path: Path, data, logger=None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if logger:
        logger.info("已写出 %s", path.relative_to(config.PROJECT_ROOT))


def load_json(path: Path):
    if not path.exists():
        raise FileNotFoundError(
            f"缺少中间结果 {path.name}，请先运行生成它的脚本：{path}"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def find_videos() -> list:
    """列出 video/ 下所有受支持的视频文件，按文件名排序。"""
    if not config.VIDEO_PATH.exists():
        return []
    return sorted(
        p for p in config.VIDEO_PATH.iterdir()
        if p.is_file() and p.suffix.lower() in config.VIDEO_EXTENSIONS
    )


def human_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num_bytes) < 1024:
            return f"{num_bytes:.1f}{unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f}TB"
