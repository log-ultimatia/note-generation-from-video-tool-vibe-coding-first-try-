# -*- coding: utf-8 -*-
"""
阶段 2：Whisper 语音转写，输出 SRT 字幕

流程：
    1. 遍历 video/ 下所有 .mp4
    2. 对每个视频调用 whisper CLI，产出 output/{视频名}.srt
    3. 日志写 logs/step2.log

用法：
    python scripts/step2_transcribe.py                  # 处理 video/ 下所有 mp4
    python scripts/step2_transcribe.py _TEST_demo.mp4   # 只处理指定视频（调试用）
    python scripts/step2_transcribe.py --skip-existing  # 已有 srt 就跳过（长视频省时间）

关于 ffmpeg 路径（重要）：
    规范里写的 `--ffmpeg-location {FFMPEG_BIN_DIR}` 在本题环境的 whisper 版本上
    【不存在这个参数】，实测会被 argparse 拒绝并退出：
        whisper.EXE: error: unrecognized arguments: --ffmpeg-location
    该版本内部固定调用裸 "ffmpeg"（whisper/audio.py 注释：Requires the ffmpeg CLI in PATH）。
    因此这里改为把 config.FFMPEG_PATH 的父目录注入子进程的 PATH 环境变量，
    效果与旧版的 --ffmpeg-location 等价，且同样不依赖全局环境变量配置。
"""

import argparse
import logging
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# 把项目根目录加进 sys.path，保证在任意工作目录下都能 import config
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402

# 每个子进程都要带这个标志，否则会各自弹出控制台黑框（本脚本由 app.py 用
# pythonw 拉起、自身没有控制台，再开 ffmpeg/Edge 时 Windows 会新建一个控制台窗口）。
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0



def setup_logger() -> logging.Logger:
    logger = logging.getLogger("step2")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    config.LOGS_PATH.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(config.LOGS_PATH / "step2.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


# ---------------------------------------------------------------------------
# 定位 whisper 可执行文件
# ---------------------------------------------------------------------------
def find_whisper_cli() -> list:
    """
    返回调用 whisper 的命令前缀（列表）。

    优先用 pip 装出来的 whisper.exe；找不到就退回 `python -m whisper`。
    都不可用时由调用方给出可读的报错，而不是让 subprocess 抛 WinError 2。
    """
    # 首选 config.WHISPER_PYTHON（D 盘 venv，装了 CUDA 版 torch）。
    # 用 `python -m whisper` 调用：whisper 包自带 __main__.py，实测可用，
    # 这样 venv 只要装 torch 就能用上 GPU，不用在 venv 里重复安装 whisper 本体。
    venv_python = Path(config.WHISPER_PYTHON) if getattr(config, "WHISPER_PYTHON", "") else None
    if venv_python and venv_python.exists():
        return [str(venv_python), "-m", "whisper"]
    exe = shutil.which("whisper")
    if exe:
        return [exe]
    # 与当前解释器同目录的 Scripts 里通常有 whisper.exe
    candidate = Path(sys.executable).parent / "Scripts" / "whisper.exe"
    if candidate.exists():
        return [str(candidate)]
    return [sys.executable, "-m", "whisper"]


def _interpreter_of(whisper_cmd: list) -> str:
    """推断 whisper 实际跑在哪个解释器上，用来探测对应的 torch 是否有 CUDA。"""
    if len(whisper_cmd) >= 2 and whisper_cmd[1] == "-m":
        return whisper_cmd[0]
    return sys.executable


_CUDA_CACHE = {}


def cuda_available(python_exe: str) -> bool:
    """用指定解释器探测 CUDA。torch 导入较慢，每个解释器只探一次并缓存。"""
    if python_exe not in _CUDA_CACHE:
        try:
            proc = subprocess.run(
                [python_exe, "-c", "import torch;print(torch.cuda.is_available())"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=300,
                creationflags=CREATE_NO_WINDOW,
            )
            _CUDA_CACHE[python_exe] = (proc.stdout or "").strip().endswith("True")
        except Exception:
            _CUDA_CACHE[python_exe] = False
    return _CUDA_CACHE[python_exe]


def resolve_device(whisper_cmd: list, logger: logging.Logger) -> str:
    """把 config 里的 'auto' 解析成 cuda / cpu。"""
    if config.WHISPER_DEVICE != "auto":
        return config.WHISPER_DEVICE
    interp = _interpreter_of(whisper_cmd)
    ok = cuda_available(interp)
    device = "cuda" if ok else "cpu"
    logger.info("自动探测推理设备：%s -> 使用 %s（解释器 %s）",
                "CUDA 可用" if ok else "CUDA 不可用", device, interp)
    return device


def build_env() -> dict:
    """
    构造子进程环境变量：
      - 把 ffmpeg 所在目录放到 PATH 最前面（替代不存在的 --ffmpeg-location）
      - 强制子进程 UTF-8 输出，避免中文 Windows 下 GBK 解码炸掉
      - 给 faster-whisper 指定模型镜像与缓存目录
    """
    env = os.environ.copy()
    ffmpeg_bin_dir = str(Path(config.FFMPEG_PATH).parent)
    env["PATH"] = ffmpeg_bin_dir + os.pathsep + env.get("PATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    endpoint = getattr(config, "WHISPER_HF_ENDPOINT", "")
    if endpoint:
        env["HF_ENDPOINT"] = endpoint
        # 必须关掉 Xet 加速下载通道：新版 huggingface_hub 会先走
        # cas-bridge.xethub.hf.co，而镜像站【不代理这个域名】，于是卡在
        # "Error while downloading ... WinError 10054 / Trying to resume download"
        # 无限重试。关掉后才会回到镜像站能代理的普通下载地址。
        env["HF_HUB_DISABLE_XET"] = "1"
    model_dir = getattr(config, "WHISPER_MODEL_DIR", "")
    if model_dir:
        env["HF_HOME"] = model_dir
    # 关掉 huggingface_hub 在 Windows 上"无法创建符号链接"的告警（无害但很吵）
    env["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
    return env


# ---------------------------------------------------------------------------
# faster-whisper 转写
# ---------------------------------------------------------------------------
def _add_cuda_dll_dirs(logger: logging.Logger) -> None:
    """
    CTranslate2 运行时需要 cuBLAS / cuDNN，而它们随 torch 一起躺在
    site-packages/torch/lib 下、默认不在 DLL 搜索路径里。不处理就会报：
        RuntimeError: Library cublas64_12.dll is not found or cannot be loaded

    【只调 os.add_dll_directory 是不够的】——这一点实测踩过：
    早先代码里为了探测 CUDA 而 import 了 torch，于是 CUDA 库被连带加载进进程，
    一切正常；后来为了省那 5 秒把 import torch 去掉，改成只加搜索路径，
    结果 CTranslate2 立刻报 cublas 找不到（cublas 自己的依赖链解析不到）。
    所以这里在加完路径后，再用 ctypes 主动把关键库预加载一遍。

    优先直接按路径找，避免为了拿路径而 import torch（那要好几秒）。
    """
    import ctypes
    import sysconfig

    candidates = []
    try:
        candidates.append(Path(sysconfig.get_paths()["purelib"]) / "torch" / "lib")
    except Exception:
        pass
    if not any(p.is_dir() for p in candidates):
        try:
            import torch                            # noqa: PLC0415
            candidates.append(Path(torch.__file__).parent / "lib")
        except Exception:
            return

    directory = next((p for p in candidates if p.is_dir()), None)
    if directory is None:
        return
    try:
        os.add_dll_directory(str(directory))
    except (OSError, AttributeError):
        pass

    loaded, missing = [], []
    for name in ("cudart64_12.dll", "cublasLt64_12.dll", "cublas64_12.dll",
                 "cudnn64_9.dll"):
        try:
            ctypes.WinDLL(name)
            loaded.append(name)
        except OSError:
            missing.append(name)
    logger.info("CUDA 库搜索路径：%s（预加载 %d 个%s）",
                directory, len(loaded),
                "，缺失 " + ",".join(missing) if missing else "")


def _load_audio(video: Path, sample_rate: int = 16000):
    """
    用 ffmpeg 把音轨解成 16kHz 单声道 float32 数组。

    为什么不用 faster-whisper 自带的解码：它走 PyAV，而当前 PyAV 19 删掉了
    faster-whisper 仍在传的 metadata_errors 参数，会直接 TypeError。我们本来
    就依赖 ffmpeg，自己解码既绕开这个版本坑，又能精确控制采样率。
    实测 40 分钟视频解码只要 2.2 秒。
    """
    import numpy as np
    # -vn 必须加：不加的话 ffmpeg 会连着视频流一起解一遍（实测多花约 1 秒/视频）
    proc = subprocess.run(
        [config.FFMPEG_PATH, "-nostdin", "-threads", "0", "-i", str(video),
         "-vn", "-f", "f32le", "-ac", "1", "-ar", str(sample_rate), "-"],
        capture_output=True,
        creationflags=CREATE_NO_WINDOW,
    )
    if proc.returncode != 0 or not proc.stdout:
        detail = (proc.stderr or b"").decode("utf-8", "replace")[-400:]
        raise RuntimeError(f"ffmpeg 解码音频失败：{detail}")
    return np.frombuffer(proc.stdout, dtype=np.float32)


def _fmt_srt_time(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    ms = int(round(seconds * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(segments: list, path: Path) -> int:
    """把 faster-whisper 的分段写成标准 SRT（下游 step4 按这个格式解析）。"""
    blocks, index = [], 0
    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        index += 1
        blocks.append(
            f"{index}\n{_fmt_srt_time(seg.start)} --> {_fmt_srt_time(seg.end)}\n{text}\n"
        )
    path.write_text("\n".join(blocks), encoding="utf-8")
    return index


def run_faster_whisper_worker(video: Path, srt_path: Path) -> int:
    """
    faster-whisper 的实际执行体。

    这个函数运行在【装了 faster-whisper 的解释器】里，由主进程以子进程方式调用
    （主流程用的是系统 Python，里面没有 torch/faster-whisper）。
    """
    logger = setup_logger()
    _add_cuda_dll_dirs(logger)

    import numpy as np                              # noqa: F401
    from faster_whisper import WhisperModel

    device = config.WHISPER_DEVICE
    if device == "auto":
        # 用 ctranslate2 自己判断有没有 CUDA，【不要 import torch】：
        # 只为查一个布尔值就加载 torch 要花约 5 秒，而 step2 每次转写都要
        # 重新起一个 worker 进程，这 5 秒是白花的固定开销。
        try:
            import ctranslate2                      # noqa: PLC0415
            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            device = "cpu"
    compute = getattr(config, "WHISPER_COMPUTE_TYPE", "float16")
    if device == "cpu":
        compute = "int8"      # CPU 上用 int8 明显更快

    logger.info("faster-whisper：加载模型 %s（device=%s, compute=%s）",
                config.WHISPER_MODEL, device, compute)
    started = time.time()
    model = WhisperModel(config.WHISPER_MODEL, device=device, compute_type=compute)
    logger.info("模型就绪，耗时 %.1f 秒", time.time() - started)

    started = time.time()
    audio = _load_audio(video)
    logger.info("音频解码完成：%.1f 分钟，耗时 %.1f 秒",
                len(audio) / 16000 / 60, time.time() - started)

    started = time.time()
    segments, info = model.transcribe(
        audio,
        language=config.WHISPER_LANGUAGE,
        beam_size=1,                       # 贪心解码：笔记场景不需要 beam search，快很多
        vad_filter=True,                   # 滤掉长静音，减少幻听和无效分段
        condition_on_previous_text=False,  # 避免长视频里错误逐段累积传播
    )

    collected, last = [], time.time()
    for seg in segments:                   # 这是生成器，边转写边产出，可以报进度
        collected.append(seg)
        now = time.time()
        if now - last >= 20:
            logger.info("…已转写到 %s（%.1f 分钟处），累计 %.1f 秒",
                        _fmt_srt_time(seg.end), seg.end / 60, now - started)
            last = now

    elapsed = time.time() - started
    count = write_srt(collected, srt_path)
    duration = len(audio) / 16000
    if elapsed > 0:
        logger.info("转写完成：媒体 %.1f 秒，耗时 %.1f 秒，实时倍率 %.2fx（%d 条字幕）",
                    duration, elapsed, duration / elapsed, count)
    return 0 if count else 1


def faster_whisper_available(python_exe: str, logger: logging.Logger) -> bool:
    """探测指定解释器里有没有 faster-whisper（也顺带确认 ctranslate2 能导入）。"""
    try:
        proc = subprocess.run(
            [python_exe, "-c", "import faster_whisper, ctranslate2"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120, env=build_env(),
            creationflags=CREATE_NO_WINDOW,
        )
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("探测 faster-whisper 失败：%s", exc)
        return False


def transcribe_faster_whisper(video: Path, srt_path: Path,
                              logger: logging.Logger) -> Path:
    """用 D 盘 venv 里的 faster-whisper 转写（以子进程方式调用 worker）。"""
    python_exe = config.WHISPER_PYTHON or sys.executable
    cmd = [python_exe, str(Path(__file__).resolve()),
           "--fw-worker", str(video), "--srt-out", str(srt_path)]
    logger.info("转写命令: %s", " ".join(cmd))

    started = time.time()
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        env=build_env(), bufsize=1,
        creationflags=CREATE_NO_WINDOW,
    )
    for raw in proc.stdout:
        line = raw.replace("\r", "").rstrip()
        if line:
            logger.info("[faster-whisper] %s", line)
    proc.wait()
    elapsed = time.time() - started

    if proc.returncode != 0 or not srt_path.exists():
        raise RuntimeError(f"faster-whisper 退出码 {proc.returncode}，耗时 {elapsed:.1f}s")

    duration = media_duration(video)
    if duration > 0 and elapsed > 0:
        logger.info("转写完成：媒体 %.1f 秒，总耗时 %.1f 秒，实时倍率 %.2fx",
                    duration, elapsed, duration / elapsed)
    return srt_path


def media_duration(path: Path) -> float:
    """用 ffprobe 读媒体时长（秒），用于算实时倍率；读不到返回 0。"""
    try:
        proc = subprocess.run(
            [config.FFPROBE_PATH, "-v", "error", "-show_entries",
             "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        return float((proc.stdout or "0").strip() or 0)
    except (ValueError, OSError):
        return 0.0


# ---------------------------------------------------------------------------
# 单个视频转写
# ---------------------------------------------------------------------------
def transcribe_one(video: Path, whisper_cmd: list, logger: logging.Logger) -> Path:
    """
    转写入口：按 config.WHISPER_ENGINE 决定用哪个引擎。

    "auto" 的行为是"能用快的就用快的"：探测 D 盘 venv 里有没有 faster-whisper，
    有就走它（快 4~6 倍），没有就退回原来的 openai-whisper 命令行并给出提示。
    """
    srt_path = config.OUTPUT_PATH / f"{video.stem}.srt"
    engine = str(getattr(config, "WHISPER_ENGINE", "auto")).lower()

    if engine == "openai":
        return transcribe_openai_cli(video, srt_path, whisper_cmd, logger)

    python_exe = config.WHISPER_PYTHON or sys.executable
    available = faster_whisper_available(python_exe, logger)
    if available:
        return transcribe_faster_whisper(video, srt_path, logger)

    if engine == "faster":
        raise RuntimeError(
            f"config.WHISPER_ENGINE='faster'，但 {python_exe} 里没有 faster-whisper。"
            f"请先安装：{python_exe} -m pip install faster-whisper"
        )
    logger.warning("没检测到 faster-whisper，退回 openai-whisper 命令行"
                   "（实测慢 4~6 倍）。想提速就装一下：")
    logger.warning("  %s -m pip install faster-whisper", python_exe)
    return transcribe_openai_cli(video, srt_path, whisper_cmd, logger)


def transcribe_openai_cli(video: Path, srt_path: Path, whisper_cmd: list,
                          logger: logging.Logger) -> Path:
    duration = media_duration(video)
    device = resolve_device(whisper_cmd, logger)

    cmd = [
        *whisper_cmd,
        str(video),
        "--model", config.WHISPER_MODEL,          # 从 config 读，不硬编码
        "--language", config.WHISPER_LANGUAGE,
        "--device", device,
        "--output_format", "srt",
        "--output_dir", str(config.OUTPUT_PATH),
    ]
    logger.info("转写命令: %s", " ".join(cmd))

    # 用 Popen 逐行流式读取，而不是 subprocess.run(capture_output=True)：
    # CPU 上 small 模型跑 50 分钟视频要 20~35 分钟，如果全部缓冲到最后才输出，
    # 整个过程完全看不到进度，无法判断是卡住还是在正常工作。
    started = time.time()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,          # whisper 的进度和日志都在 stderr，合并后一起读
        text=True,
        encoding="utf-8",
        errors="replace",
        env=build_env(),
        bufsize=1,
        creationflags=CREATE_NO_WINDOW,
    )

    last_report = started
    for raw in proc.stdout:
        line = raw.replace("\r", "").rstrip()
        if not line:
            continue
        # tqdm 进度条会刷出大量重复行，只打到控制台、不写日志文件，避免日志爆炸
        if "%|" in line or "it/s]" in line or "s/it]" in line:
            print(line, flush=True)
            continue
        logger.info("[whisper] %s", line)
        # 长视频时每 60 秒给一条心跳，方便确认进程还活着
        now = time.time()
        if now - last_report >= 60:
            logger.info("…仍在转写中，已耗时 %.1f 分钟", (now - started) / 60)
            last_report = now

    proc.wait()
    elapsed = time.time() - started

    if proc.returncode != 0:
        raise RuntimeError(f"whisper 退出码 {proc.returncode}，耗时 {elapsed:.1f}s")

    if not srt_path.exists():
        raise RuntimeError(
            f"whisper 正常退出但没有生成 {srt_path.name}，请检查上方日志"
        )

    # 实时倍率 = 媒体时长 / 实际耗时，> 1 表示比实时快，用来预估长视频要跑多久
    if duration > 0 and elapsed > 0:
        logger.info("转写完成：媒体 %.1f 秒，耗时 %.1f 秒，实时倍率 %.2fx",
                    duration, elapsed, duration / elapsed)
    else:
        logger.info("转写完成，耗时 %.1f 秒", elapsed)
    return srt_path


def srt_segment_count(srt_path: Path) -> int:
    """数一下 SRT 里有多少条字幕，用来快速判断转写是否有效（0 条说明音轨有问题）。"""
    try:
        text = srt_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return -1
    return sum(1 for line in text.splitlines() if "-->" in line)


# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="阶段2：Whisper 语音转写输出 SRT")
    parser.add_argument("video", nargs="?", help="只处理指定视频文件名")
    parser.add_argument("--skip-existing", action="store_true",
                        help="output/ 里已有同名 srt 就跳过（长视频省时间）")
    parser.add_argument("--fw-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--srt-out", help=argparse.SUPPRESS)
    args = parser.parse_args()

    # worker 模式：由主流程用装了 faster-whisper 的解释器拉起本脚本，
    # 在这里执行真正的转写。必须放在所有前置校验之前（主流程那套检查在 worker 里没意义）。
    if args.fw_worker:
        config.ensure_dirs()
        return run_faster_whisper_worker(Path(args.video), Path(args.srt_out))

    logger = setup_logger()
    config.ensure_dirs()

    # 前置校验，失败要给出人能看懂的原因
    if not Path(config.FFMPEG_PATH).exists():
        logger.error("FFMPEG_PATH 不存在：%s", config.FFMPEG_PATH)
        return 1

    whisper_cmd = find_whisper_cli()
    logger.info("whisper 调用方式: %s", " ".join(whisper_cmd))

    model_file = Path.home() / ".cache" / "whisper" / f"{config.WHISPER_MODEL}.pt"
    if model_file.exists():
        logger.info("模型已缓存：%s（%.1f MB），无需下载",
                    model_file.name, model_file.stat().st_size / 1024 / 1024)
    else:
        logger.warning("模型 %s.pt 未缓存，whisper 会自动下载（约 500MB），请保持网络通畅",
                       config.WHISPER_MODEL)

    # 同 step1：必须用 config.VIDEO_EXTENSIONS，不能写死 .mp4
    # （写死会导致从 YouTube 下到的 .webm 被漏掉）
    videos = sorted(
        (p for p in config.VIDEO_PATH.iterdir()
         if p.is_file() and p.suffix.lower() in config.VIDEO_EXTENSIONS),
        key=lambda p: p.name,
    ) if config.VIDEO_PATH.exists() else []

    if args.video:
        videos = [v for v in videos if v.name == args.video or v.stem == args.video]

    if not videos:
        logger.error("video/ 下没有找到视频文件：%s", config.VIDEO_PATH)
        logger.error("  支持的格式：%s", " ".join(config.VIDEO_EXTENSIONS))
        return 1

    logger.info("=" * 60)
    logger.info("开始转写 %d 个视频（model=%s, language=%s, device=%s）",
                len(videos), config.WHISPER_MODEL,
                config.WHISPER_LANGUAGE, config.WHISPER_DEVICE)
    started = datetime.now()
    ok = 0

    for video in videos:
        logger.info("-" * 60)
        logger.info("处理：%s", video.name)

        srt_path = config.OUTPUT_PATH / f"{video.stem}.srt"
        if args.skip_existing and srt_path.exists():
            logger.info("已存在 %s，跳过", srt_path.name)
            ok += 1
            continue

        try:
            srt = transcribe_one(video, whisper_cmd, logger)
        except Exception as exc:
            logger.error("失败：%s -> %s", video.name, exc)
            continue

        n = srt_segment_count(srt)
        ok += 1
        logger.info("输出：%s（%d 条字幕）", srt.name, n)
        if n == 0:
            logger.warning("字幕条数为 0：该视频可能没有音轨，或语音无法识别")

    logger.info("=" * 60)
    logger.info("完成：成功 %d/%d 个视频，总耗时 %.1f 分钟",
                ok, len(videos), (datetime.now() - started).total_seconds() / 60)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
