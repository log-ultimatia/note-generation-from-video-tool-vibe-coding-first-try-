# -*- coding: utf-8 -*-
"""
阶段 1：FFmpeg 场景检测抽帧 + 记录每帧时间戳

流程：
    1. 遍历 video/ 下所有 .mp4
    2. 用 select='gt(scene,SCENE_THRESHOLD)',showinfo 抽出画面切换帧，
       写到 frames/{视频名}/scene_%06d.jpg
    3. 从 ffmpeg 的 stderr 里正则提取每个 showinfo 帧的 pts_time
    4. 按顺序与帧文件名一一对应，写入 output/{视频名}_scene_info.json
    5. 额外补抽视频第 0 秒的帧（config.EXTRACT_FIRST_FRAME），
       已有帧整体后移一位，首帧占 scene_000001.jpg

    第 5 步的必要性：select 只挑"画面切换"的帧，而视频第一帧的 scene 分数
    恒为 0，所以开头那一页永远抽不到。实测 8 页的测试视频只抽到第 2~8 页，
    标题页完全丢失，而 step4 会把首场景 start_time 记成 0.0，导致开头那段
    被贴上第二页的文字。补帧后首场景才有真正的代表图。

输出 JSON 格式（首帧 time 为 0.0）：
    [{"image": "scene_000001.jpg", "time": 0.0},
     {"image": "scene_000002.jpg", "time": 7.52}, ...]

用法：
    python scripts/step1_extract_frames.py                # 处理 video/ 下所有 mp4
    python scripts/step1_extract_frames.py _TEST_demo.mp4 # 只处理指定视频（调试用）

日志：logs/step1.log
"""

import argparse
import json
import logging
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# 把项目根目录加进 sys.path，保证在任意工作目录下都能 import config
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402

# 每个子进程都必须带这个标志，否则会各自弹出控制台黑框。
# 本脚本由 app.py 用 pythonw 拉起（自身没有控制台），它再去开 ffmpeg 时，
# 那个新进程会被 Windows 分配一个全新的控制台窗口——step1 要调很多次 ffmpeg
# （探测 + 抽帧 + 逐帧重抽），于是黑框一个接一个地闪。
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# showinfo 的行长这样（ffmpeg 9.0.2 实测）：
#   [Parsed_showinfo_1 @ 000001f6f2e49280] n:   0 pts:  96256 pts_time:7.52
#   duration: 512 duration_time:0.04 fmt:yuv420p ...
# 注意 pts_time 可能是整数（如 15）也可能是小数（如 7.52），负数也要能匹配。
PTS_TIME_RE = re.compile(r"pts_time:([-+]?[0-9]*\.?[0-9]+)")


# ---------------------------------------------------------------------------
# 日志：同时写 logs/step1.log 和打印到控制台
# ---------------------------------------------------------------------------
def setup_logger() -> logging.Logger:
    logger = logging.getLogger("step1")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    config.LOGS_PATH.mkdir(parents=True, exist_ok=True)
    # 必须显式指定 utf-8：中文 Windows 默认 GBK，中文日志和视频名会乱码/报错
    fh = logging.FileHandler(config.LOGS_PATH / "step1.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


# ---------------------------------------------------------------------------
# fps_mode 兼容处理
#
# 规范里写的是 -vsync vfr，但 -vsync 在 ffmpeg 9 里已被【完全移除】，
# 实测报错：Error splitting the argument list: Option not found
# 它从 ffmpeg 5.1 起改名为 -fps_mode。为了让脚本在新旧版本上都能跑，
# 这里探测一次 ffmpeg 帮助，选可用的那个写法。
# ---------------------------------------------------------------------------
_FPS_MODE_ARGS = None
_HW_DECODER_CACHE = {}       # 视频路径 -> (decoder 或 None, 原因)，避免重复探测


def run_ffmpeg(cmd: list, logger: logging.Logger):
    """
    跑 ffmpeg：边跑边收集输出、盯着进度、并且不弹控制台黑框。

    为什么要盯着进度：硬件解码器（av1_cuvid）会【随机】死锁——实测同一条命令、
    同一个视频，一次 24 秒正常跑完，下一次就卡在 85% 处再不前进（时间戳不动、
    elapsed 一直涨），整个流程就此挂住。用 subprocess.run() 一步到底的话，
    这里会永远等下去。

    做法：把输出读成流。ffmpeg 正常工作时每秒会刷好几行进度，
    超过 FFMPEG_STALL_TIMEOUT 秒一行都没有，就判定卡死并杀掉。

    顺带的好处：抽帧期间不再是一片空白，每 30 秒往日志报一次进度。

    返回 (是否成功, 全部输出文本)。输出文本仍要用于解析 showinfo 的 pts_time。
    """
    stall_timeout = getattr(config, "FFMPEG_STALL_TIMEOUT", 30)
    started = time.time()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,   # ffmpeg 的日志和进度都在 stderr
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        creationflags=CREATE_NO_WINDOW,
    )

    state = {"last": time.time(), "killed": False, "last_report": time.time()}
    lines = []
    stop = threading.Event()

    def watchdog():
        while not stop.wait(1.0):
            if time.time() - state["last"] > stall_timeout:
                state["killed"] = True
                logger.warning("ffmpeg 已有 %d 秒没有任何输出，判定卡死，终止它",
                               stall_timeout)
                try:
                    proc.kill()
                except OSError:
                    pass
                return

    threading.Thread(target=watchdog, daemon=True).start()

    for chunk in proc.stdout:
        # 进度行用 \r 反复刷新，会挤在同一行里，先按 \r 和 \n 都拆开
        for piece in chunk.replace("\r", "\n").split("\n"):
            piece = piece.strip()
            if not piece:
                continue
            lines.append(piece)
            state["last"] = time.time()
            if ("time=" in piece or "frame=" in piece) \
                    and time.time() - state["last_report"] >= 30:
                logger.info("抽帧中… %s", piece[:110])
                state["last_report"] = time.time()

    proc.wait()
    stop.set()
    elapsed = time.time() - started

    if state["killed"]:
        logger.warning("ffmpeg 被判定卡死并终止（已运行 %.1f 秒）", elapsed)
        return False, "\n".join(lines)
    logger.info("ffmpeg 结束：退出码 %d，耗时 %.1f 秒", proc.returncode, elapsed)
    return proc.returncode == 0, "\n".join(lines)


def fps_mode_args() -> list:
    global _FPS_MODE_ARGS
    if _FPS_MODE_ARGS is None:
        try:
            probe = subprocess.run(
                [config.FFMPEG_PATH, "-hide_banner", "-h", "full"],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                creationflags=CREATE_NO_WINDOW,
            )
            if "fps_mode" in (probe.stdout or ""):
                _FPS_MODE_ARGS = ["-fps_mode", "vfr"]
            else:
                _FPS_MODE_ARGS = ["-vsync", "vfr"]
        except Exception:
            _FPS_MODE_ARGS = ["-fps_mode", "vfr"]
    return list(_FPS_MODE_ARGS)


# 视频编码 → 对应的 NVIDIA 硬件解码器
HW_DECODER_BY_CODEC = {
    "av1": "av1_cuvid",
    "h264": "h264_cuvid",
    "hevc": "hevc_cuvid",
    "vp9": "vp9_cuvid",
    "mpeg2video": "mpeg2_cuvid",
    "vc1": "vc1_cuvid",
}


def video_codec(video: Path) -> str:
    """用 ffprobe 读视频编码名，失败返回空串。"""
    try:
        proc = subprocess.run(
            [config.FFPROBE_PATH, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(video)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        return (proc.stdout or "").strip().splitlines()[0].strip() if proc.stdout else ""
    except Exception:
        return ""


def pick_hw_decoder(video: Path, logger: logging.Logger):
    """
    探测能否用显卡硬件解码这个视频，返回 (decoder, 说明)；不能用就返回 (None, 原因)。

    为什么要试：实测 9 分钟 1080p AV1 视频，软件 libaom 解码整片要 32 秒，
    av1_cuvid 只要 14.6 秒（2.2 倍），而 step1 的耗时几乎全在解码整片上。
    硬件解码还更准——同一视频软件解码漏了一帧、并把片尾一帧解成全黑。

    为什么要探测而不是直接用：不是所有显卡都支持（AV1 硬解要 RTX 40 系及以上，
    H.264 老卡也行），显卡驱动缺失或格式不支持时 ffmpeg 会直接失败。
    所以先拿这个视频真实解一帧试试，失败就静默退回软件解码。
    """
    mode = str(getattr(config, "FFMPEG_HW_DECODE", "auto")).lower()
    if mode == "off":
        return None, "配置为 off"
    global _HW_DECODER_CACHE
    key = str(video)
    if key in _HW_DECODER_CACHE:
        return _HW_DECODER_CACHE[key]

    codec = video_codec(video)
    decoder = HW_DECODER_BY_CODEC.get(codec)
    if not decoder:
        _HW_DECODER_CACHE[key] = (None, f"编码 {codec or '未知'} 无对应硬件解码器")
        return _HW_DECODER_CACHE[key]

    try:
        proc = subprocess.run(
            [config.FFMPEG_PATH, "-hide_banner", "-loglevel", "error",
             "-nostdin", "-y", "-c:v", decoder, "-i", str(video),
             "-frames:v", "1", "-f", "null", "-"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=120,
            creationflags=CREATE_NO_WINDOW,
        )
        if proc.returncode == 0:
            _HW_DECODER_CACHE[key] = (decoder, "可用")
        else:
            detail = (proc.stderr or "").strip().splitlines()
            _HW_DECODER_CACHE[key] = (
                None, f"{decoder} 不可用：{detail[-1][:90] if detail else '未知原因'}")
    except Exception as exc:
        _HW_DECODER_CACHE[key] = (None, f"{decoder} 探测异常：{exc}")
    return _HW_DECODER_CACHE[key]


# ---------------------------------------------------------------------------
# 单个视频抽帧
# ---------------------------------------------------------------------------
def extract_first_frame(video: Path, frames_dir: Path, logger: logging.Logger):
    """
    补抽视频第 0 秒的画面，先写成临时文件 _first_frame.jpg 并返回其路径。

    为什么需要它：select 滤镜只挑"画面切换"的帧，而视频第一帧的 scene 分数
    恒为 0（没有前一帧可比），所以视频开头那一页永远抽不到。实测 8 页幻灯片的
    测试视频只抽到第 2~8 页、标题页完全丢失；而 step4 会把首场景 start_time
    记为 0.0，于是开头那段被贴上了第二页的文字。补一帧，首场景才有真代表图。

    这里不用 -ss：直接解码并从第一帧开始输出一帧即可，比 -ss 0 更明确。
    文件名故意不匹配 scene_*.jpg，避免被后面的重编号循环误处理。
    """
    tmp = frames_dir / "_first_frame.jpg"
    cmd = [
        config.FFMPEG_PATH, "-hide_banner", "-nostdin", "-y",
        "-i", str(video),
        "-frames:v", "1",          # 只输出第一帧
        "-an", "-sn",
        str(tmp),
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
    creationflags=CREATE_NO_WINDOW
    )
    if proc.returncode != 0 or not tmp.exists():
        logger.warning("补抽第 0 秒首帧失败，本次不含首帧")
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        for line in tail:
            logger.warning("  ffmpeg: %s", line)
        return None
    return tmp


# ---------------------------------------------------------------------------
def extract_one(video: Path, logger: logging.Logger) -> list:
    stem = video.stem
    frames_dir = config.FRAMES_PATH / stem

    # 先清空目录：否则重跑时若本次帧数变少，上次残留的高序号文件会让
    # "文件数" 和 "时间戳数" 对不上，破坏一一对应关系。
    if frames_dir.exists():
        shutil.rmtree(frames_dir)
    frames_dir.mkdir(parents=True, exist_ok=True)

    vf = f"select='gt(scene,{config.SCENE_THRESHOLD})',showinfo"
    hw_decoder, hw_reason = pick_hw_decoder(video, logger)
    if hw_decoder:
        logger.info("硬件解码：%s（%s）", hw_decoder, hw_reason)
    else:
        logger.info("软件解码（%s）", hw_reason)

    base_cmd = [
        config.FFMPEG_PATH,          # 绝对路径，不依赖 PATH / 环境变量
        "-hide_banner", "-nostdin", "-y",
    ]
    tail_args = [
        "-i", str(video),
        "-vf", vf,
        *fps_mode_args(),            # 不做帧率补偿，选中几帧就输出几帧
        str(frames_dir / "scene_%06d.jpg"),
    ]

    cmd = [*base_cmd, *(["-c:v", hw_decoder] if hw_decoder else []), *tail_args]
    logger.info("抽帧命令: %s", " ".join(cmd))

    ok, stderr = run_ffmpeg(cmd, logger)

    # 硬件解码器会【随机】死锁（实测同一条命令一次正常、一次卡在 85% 处不再前进）。
    # 所以硬件解码这条路一旦失败或停滞，就地退回软件解码重跑一遍，不让整个流程挂住。
    if not ok and hw_decoder:
        logger.warning("硬件解码失败或停滞，改用软件解码重试")
        _HW_DECODER_CACHE[str(video)] = (None, "上次解码卡死")
        cmd = [*base_cmd, *tail_args]
        logger.info("抽帧命令: %s", " ".join(cmd))
        ok, stderr = run_ffmpeg(cmd, logger)

    if not ok:
        tail = "\n".join(stderr.strip().splitlines()[-15:])
        raise RuntimeError(f"ffmpeg 失败，末尾输出：\n{tail}")

    # 从输出里提取所有 showinfo 的 pts_time（showinfo 走 stderr，已合并进 stderr）
    times = [float(m) for m in PTS_TIME_RE.findall(stderr)]
    files = sorted(frames_dir.glob("scene_*.jpg"))

    # 一一对应校验
    if len(times) != len(files):
        logger.warning(
            "%s：时间戳数量(%d) 与 帧文件数量(%d) 不一致，按较小值配对",
            video.name, len(times), len(files),
        )
        n = min(len(times), len(files))
        times, files = times[:n], files[:n]

    if not files:
        logger.warning("%s：没有检测到任何场景切换帧", video.name)
        logger.warning("  可能是 SCENE_THRESHOLD=%.3f 偏高，或视频画面几乎不变",
                       config.SCENE_THRESHOLD)

    # 补一帧第 0 秒的画面。若首个切换帧本身就在 0 秒附近，说明已经覆盖开头，
    # 就不重复补了。
    if getattr(config, "EXTRACT_FIRST_FRAME", True) and (not times or times[0] > 0.05):
        first = extract_first_frame(video, frames_dir, logger)
        if first is not None:
            # 已有的 scene_*.jpg 整体后移一位，给首帧腾出 scene_000001.jpg。
            # 必须【从大到小】改名，否则会互相覆盖。
            for old in sorted(frames_dir.glob("scene_*.jpg"), reverse=True):
                idx = int(old.stem.split("_")[1])
                old.rename(frames_dir / f"scene_{idx + 1:06d}.jpg")
            first.rename(frames_dir / "scene_000001.jpg")
            # 改名后重新收集，保证文件名与时间戳严格按顺序一一对应
            files = sorted(frames_dir.glob("scene_*.jpg"))
            times = [0.0] + times
            logger.info("已补入第 0 秒首帧，共 %d 帧", len(files))

    if not files:
        return []

    # 一一对应校验（补帧后可能仍有偏差，例如首帧写入失败）
    if len(times) != len(files):
        logger.warning("%s：补帧后时间戳(%d)与文件(%d)数量仍不一致，按较小值配对",
                       video.name, len(times), len(files))
        n = min(len(times), len(files))
        times, files = times[:n], files[:n]

    records = [
        {"image": f.name, "time": round(t, 3)}
        for f, t in zip(files, times)
    ]

    # 修正落在转场淡变中间的帧（见 config.SCENE_SETTLE_OFFSET 的说明）
    fix_transition_frames(video, frames_dir, records, logger)

    return records


def fix_transition_frames(video: Path, frames_dir: Path, records: list,
                          logger: logging.Logger) -> int:
    """
    把每帧从"检测到画面变化的那一刻"改抽到"变化稳定之后"。

    问题：select=gt(scene,TH) 命中的时刻 T 往往正处在【淡入淡出】中间——因为淡变时
    每一帧的像素都在变，逐帧差异都超过阈值，于是整段转场被当成一串"场景切换"。
    实测一个 1.1 秒的转场被抽成 10 帧，平均亮度从 240 掉到 2.6（全黑）再回升，
    其中一半是黑帧/灰帧，成为场景代表图，笔记配图就发黑。

    做法很直接：所有帧一律改从 T + SCENE_SETTLE_OFFSET 重抽。
      · 幻灯片是静止画面，偏移 0.5 秒内容完全一样，所以不会误伤正常帧
      · 淡入时 T+0.5 正好是转场结束后的稳定画面
      · 视频结尾的淡出，往后已超出片尾取不到帧，就回退到 T-offset（往前是亮画面）

    为什么不做"亮度异常才重抽"的启发式：试过固定阈值（会漏掉均值 174/144 这种
    "不够黑但仍在淡变中"的帧），也试过跟全片中位数比（中位数会被暗帧自己拉低，
    要反复跑才收敛）。一律偏移没有阈值要调，也就没有调错的空间。
    """
    offset = getattr(config, "SCENE_SETTLE_OFFSET", 0.5)
    if offset <= 0:
        return 0

    replaced, kept = 0, 0
    for record in records:
        target = frames_dir / record["image"]
        if not target.exists():
            continue

        original = target.read_bytes()
        done = False
        for delta in (offset, -offset):        # 先往后（淡入），再往前（淡出）
            settle_time = record["time"] + delta
            if settle_time < 0:
                continue
            tmp = frames_dir / f"_settle_{record['image']}"
            result = subprocess.run(
                [config.FFMPEG_PATH, "-hide_banner", "-nostdin", "-y",
                 "-ss", f"{settle_time:.3f}", "-i", str(video),
                 "-frames:v", "1", "-an", "-sn", str(tmp)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                creationflags=CREATE_NO_WINDOW,
            )
            if result.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
                continue                       # 例如淡出发生在片尾，往后取不到帧
            # 抽到的内容和原来字节完全相同就没必要替换（避免无谓地改写文件）
            if tmp.read_bytes() == original:
                tmp.unlink(missing_ok=True)
                done = True
                break
            tmp.replace(target)
            replaced += 1
            done = True
            logger.debug("%s: 偏移 %+.1fs 重抽", record["image"], delta)
            break

        if not done:
            kept += 1

    if replaced:
        logger.info("转场稳定化：%d 帧改从 T%+.1fs 重抽（避开淡入淡出中间的黑帧），"
                    "%d 帧无法偏移、保留原样", replaced, offset, kept)
    return replaced


# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="阶段1：FFmpeg 场景检测抽帧 + 记录时间戳")
    parser.add_argument("video", nargs="?",
                        help="只处理指定视频文件名（默认处理 video/ 下全部 mp4）")
    args = parser.parse_args()

    logger = setup_logger()
    config.ensure_dirs()

    if not Path(config.FFMPEG_PATH).exists():
        logger.error("FFMPEG_PATH 不存在：%s", config.FFMPEG_PATH)
        return 1

    # 必须用 config.VIDEO_EXTENSIONS，不能写死 .mp4。
    # 曾经这里硬编码成 .mp4，而界面/文档都宣称支持 8 种格式，结果从 YouTube
    # 下到的是 .webm：文件明明在 video/ 里，这里却一个都找不到、直接报错退出。
    videos = sorted(
        p for p in config.VIDEO_PATH.iterdir()
        if p.is_file() and p.suffix.lower() in config.VIDEO_EXTENSIONS
    ) if config.VIDEO_PATH.exists() else []

    if args.video:
        videos = [v for v in videos if v.name == args.video or v.stem == args.video]

    if not videos:
        logger.error("video/ 下没有找到视频文件：%s", config.VIDEO_PATH)
        logger.error("  支持的格式：%s", " ".join(config.VIDEO_EXTENSIONS))
        return 1

    # 提醒那个经典的踩坑阈值
    if config.SCENE_THRESHOLD >= 0.2:
        logger.warning(
            "SCENE_THRESHOLD=%.3f 偏大：课件类视频的切换分数实测只有 0.06~0.13，"
            "很可能一帧都抽不到。建议 0.05 左右。", config.SCENE_THRESHOLD,
        )

    logger.info("=" * 60)
    logger.info("开始处理 %d 个视频，SCENE_THRESHOLD=%.3f，fps 模式参数=%s",
                len(videos), config.SCENE_THRESHOLD, " ".join(fps_mode_args()))
    started = datetime.now()
    ok, total_frames = 0, 0

    for video in videos:
        logger.info("-" * 60)
        logger.info("处理：%s", video.name)
        try:
            records = extract_one(video, logger)
        except Exception as exc:
            logger.error("失败：%s -> %s", video.name, exc)
            continue

        out_json = config.OUTPUT_PATH / f"{video.stem}_scene_info.json"
        out_json.write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        ok += 1
        total_frames += len(records)
        logger.info("抽取 %d 帧，已写出 %s", len(records), out_json.name)
        if records:
            logger.info("时间范围：%.2fs ~ %.2fs",
                        records[0]["time"], records[-1]["time"])

    logger.info("=" * 60)
    logger.info("完成：成功 %d/%d 个视频，共 %d 帧，耗时 %.1fs",
                ok, len(videos), total_frames, (datetime.now() - started).total_seconds())
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
