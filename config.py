# -*- coding: utf-8 -*-
"""
cs61a_notes 项目全局配置。

所有脚本都必须从这里读取常量，禁止在脚本里硬编码路径、阈值或 API Key。

用法（脚本放在 scripts/ 目录下时，在文件顶部加这几行）：

    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import config
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent

# 所有子进程都要带这个标志，否则各自的控制台黑框会一个个弹出来
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


# ---------------------------------------------------------------------------
# 本地密钥 / 配置的读取
#
# API Key【绝不写进代码】——写进去再传到 GitHub 就等于公开泄露（而且密钥扫描
# 爬虫几分钟内就会找上门）。这里按下面的顺序找，.env 已在 .gitignore 里：
#     1. 环境变量 DEEPSEEK_API_KEY
#     2. 项目根目录的 .env 文件（一行一个 KEY=VALUE，# 开头是注释）
# ---------------------------------------------------------------------------
def _read_dotenv(path: Path) -> dict:
    """读一个极简的 .env（只支持 KEY=VALUE 和 # 注释，够用且不引入依赖）。"""
    data = {}
    if not path.exists():
        return data
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            data[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return data


_ENV_FILE = _read_dotenv(PROJECT_ROOT / ".env")


def env_or_dotenv(name: str, default: str = "") -> str:
    """取配置：环境变量优先，其次 .env 文件，最后用默认值。"""
    return (os.environ.get(name) or _ENV_FILE.get(name) or default).strip()


def _first_existing(*candidates: str) -> str:
    """返回第一个真实存在的路径；都不存在时返回最后一个（便于给出可读的报错）。"""
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return candidates[-1] if candidates else ""


# ---------------------------------------------------------------------------
# 外部工具
#
# ffmpeg 一定要选【带 libdav1d 的构建】。dav1d 是最快的 AV1 软件解码器，
# 而 step1 的耗时几乎全在解码整片视频上，换解码器就是直接换速度。
# 实测同一个 13.8 分钟 1080p AV1 视频（抽帧扫描）：
#     旧构建（只有 libaom）        51.8s
#     本构建（带 libdav1d）        13.4s    快 3.9 倍，产出帧像素完全一致
# 注意"essentials"精简版通常不带 dav1d，要下 "full"/"gpl" 构建。
# ---------------------------------------------------------------------------
FFMPEG_KNOWN_GOOD = r"D:\ffmpeg-dav1d\ffmpeg-master-latest-win64-gpl\bin\ffmpeg.exe"
FFMPEG_OLD_BUILD = r"D:\ffmpeg\ffmpeg-9.0.2-essentials_build\bin\ffmpeg.exe"

# 按顺序找：环境变量/.env → 本机已知可用路径 → 系统 PATH。
# 这样别人克隆下来、只要 PATH 里有 ffmpeg 就能直接用，不必改代码。
FFMPEG_PATH = _first_existing(
    env_or_dotenv("FFMPEG_PATH"),
    FFMPEG_KNOWN_GOOD,
    FFMPEG_OLD_BUILD,
    shutil.which("ffmpeg") or "",
)

# ffprobe 与 ffmpeg 同目录，由 FFMPEG_PATH 推导，避免重复硬编码。
# 用途：读取视频时长 / 分辨率 / 帧率，抽帧前做校验。
FFPROBE_PATH = str(Path(FFMPEG_PATH).with_name("ffprobe.exe"))

# ---------------------------------------------------------------------------
# DeepSeek API
#
# 重要：该端点是【纯文本】模型，不接受图片输入。图片会被静默替换成
# "[Unsupported Image]" 并返回 HTTP 200 + 空 content，不会报错。
# 所以图片内容必须先经本地 OCR 转成文字，再交给本接口。
#
# 可用模型只有两个（deepseek-chat 是别名，实际路由到 deepseek-flash）：
#     deepseek-flash    —— 快、便宜，适合批量逐段整理
#     deepseek-v4-pro   —— 更强，适合整体串联润色
# 注意：返回体里 reasoning_content 是独立字段，content 常为空。
# max_tokens 给小了会被推理吃光（finish_reason=length）导致 content 为空，
# 因此下面给了较宽裕的 4096。读取时必须取 content，不能取 reasoning_content。
# ---------------------------------------------------------------------------
DEEPSEEK_API_KEY = env_or_dotenv("DEEPSEEK_API_KEY")

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-flash"
DEEPSEEK_MODEL_PRO = "deepseek-v4-pro"
DEEPSEEK_MAX_TOKENS = 4096
DEEPSEEK_TEMPERATURE = 0.3
DEEPSEEK_TIMEOUT = 180      # 单次请求超时（秒），长文本整理会比较慢

# 失败重试次数。实测这个接口高频调用时会偶发 RemoteDisconnected（服务端直接断连）。
# 该类"连接层"错误在 step5 里是【立即重试】而不是指数退避（等退避没意义，
# 因为服务端根本没处理请求），所以把次数给到 5 次，代价很小。
# 429/5xx 这类仍走指数退避，避免打爆服务端。
DEEPSEEK_MAX_RETRY = 5

# 并发调用数。这是整条流水线提速最大的一处，实测数据（同一批场景）：
#     串行       8 个场景 43.8s   1.00x
#     4 线程     8 个场景 16.1s   2.72x
#     8 线程     8 个场景  6.5s   6.78x   （0 失败）
# 单次调用本身只要 3~5 秒，串行时墙钟时间几乎全耗在"等接口回话"上，所以并发
# 接近线性提速。125 个场景：串行约 11 分钟，8 线程约 2 分钟。
# 设成 1 即退回原来的逐个串行（万一接口开始限流可以这样退）。
DEEPSEEK_CONCURRENCY = 8

# 思考（reasoning）强度。这个接口的模型默认会先"想"一大段再回答，
# 而思考 token 同样计费，还会挤占 max_tokens 预算。
#
# 实测三种档位（真实场景，同一批输入）：
#   档位    延迟            推理字数      内容质量
#   默认    7.6~11.5s      3900~5200 字  正确
#   "low"   3.9~ 9.1s      1100~3200 字  正确（与默认一致）
#   "none"  1.2~ 1.6s           0 字    【会编造概念】
#
# 注意 "none" 有实质质量风险：实测两个场景里模型都凭空说出了"递归"
# （而这两页根本没有递归内容），而 "low" 和默认都没有这个毛病。
# 所以默认用 "low"：质量与默认持平，速度快 1.3~2 倍，思考 token 少一半以上。
#
# 取值："" = 用模型默认；"low" / "medium" / "high"；"none" = 完全不思考（最快）。
DEEPSEEK_REASONING_EFFORT = "low"

# ---------------------------------------------------------------------------
# step5：生成笔记时的 OCR 文字加工
# ---------------------------------------------------------------------------
# 跨场景重复出现的短文本视为页眉/页脚/水印并剔除。
# 实测课件每页右下角都有 "CS61A" 页脚，OCR 逐页识别出来，不剔除的话
# 每段笔记都会混进这一行噪声。
STEP5_DROP_REPEATED_LINES = True
STEP5_REPEAT_SCENE_RATIO = 0.6   # 在 >=60% 的场景里都出现就判定为页眉页脚
STEP5_REPEAT_MAX_LEN = 20        # 只剔除短文本，避免误删正文

# 用 x 坐标还原缩进时的最大层级（防止异常坐标产生几十层缩进）
STEP5_MAX_INDENT_LEVEL = 6

# 生成 Markdown 时把笔记正文里的标题降几级。
# 场景标题是二级（"## 时间 - 标签"），而模型返回的 note 里常自带 "# / ##" 标题，
# 实测出现过 note 带一级标题的情况，会让全文出现多个一级标题、大纲错乱。
# 设为 0 可关闭降级（保持模型原始输出）。
STEP5_DEMOTE_NOTE_HEADINGS = 2

# ---------------------------------------------------------------------------
# Whisper 语音转写
#
# 实测环境：openai-whisper 20250625，small.pt 已缓存在 ~/.cache/whisper。
#
# 性能实测（重要）：系统 Python 里的 torch 是 CPU 版，实测【比实时还慢】——
#   60 秒音频耗时 106.8 秒，实时倍率仅 0.56x，
#   推算 50 分钟一节课要 89 分钟，40 讲全课程约 59 小时。
#
# 因此 CUDA 版 torch 装在 D 盘的独立虚拟环境里，不污染系统 Python：
#   D:\cs61a_venv 由 python -m venv --system-site-packages 创建，
#   它继承系统里的 whisper / numpy 等包，但优先使用自己的 CUDA 版 torch。
# WHISPER_PYTHON 指向该解释器。留空或路径不存在时会自动退回系统 whisper 命令
# （能跑，但慢很多，不会报错）。别人克隆项目时这里一般是空的，用环境变量指定即可：
#     $env:WHISPER_PYTHON = "D:\my-venv\Scripts\python.exe"
# ---------------------------------------------------------------------------
WHISPER_PYTHON = _first_existing(
    env_or_dotenv("WHISPER_PYTHON"),
    r"D:\cs61a_venv\Scripts\python.exe",
    "",                       # 都不存在就留空 → 走系统 whisper 命令行
)

WHISPER_MODEL = "small"
WHISPER_LANGUAGE = "en"     # CS61A 是英文课；想自动检测改成 None
WHISPER_DEVICE = "auto"     # auto = 探测到 CUDA 就用 cuda，否则 cpu；也可写死 cuda / cpu

# ---------------------------------------------------------------------------
# 转写引擎
#
#   "auto"   = 能用 faster-whisper 就用，否则退回 openai-whisper 命令行
#   "faster" = 强制 faster-whisper
#   "openai" = 强制原来的 openai-whisper 命令行
#
# 为什么换 faster-whisper —— 实测同一个 40 分钟视频（RTX 4070）：
#     openai-whisper  small             250s     9.7x 实时
#     faster-whisper  small  float16     44s    55.4x 实时
#     faster-whisper  small  int8        61s    40.0x 实时
#     faster-whisper  large-v3 float16  ~187s    ~13x 实时（含约 60s 冷启动预热）
# 两个反直觉的实测结论：
#   1. float16 比 int8 更快——4070 有原生 FP16 张量核心，而 small 模型能整个放进
#      显存，量化只会多出反量化开销。显存吃紧（large 系）才该考虑 int8_float16。
#   2. 大模型有一次性的"首次推理预热"成本，约 60 秒：large-v3 冷启动测出来只有
#      4.0x 实时，预热后是 19.2x。所以别用"跑一小段"的结果去估算大模型的速度。
#
# 默认选 small 是为了速度；想更准可以改成 "large-v3"（约 3 分钟/40 分钟视频，
# 仍比原来的 openai-whisper small 快）。
# ---------------------------------------------------------------------------
WHISPER_ENGINE = "auto"
WHISPER_COMPUTE_TYPE = "float16"

# 模型下载源。huggingface.co 国内直连不通（实测 WinError 10054 连接被重置），
# faster-whisper 默认从那里拉模型，所以必须走镜像。实测 hf-mirror.com 可用。
WHISPER_HF_ENDPOINT = "https://hf-mirror.com"

# 模型缓存目录（faster-whisper 的模型有几个 GB）。留空则用系统默认的
# C:\Users\<你>\.cache\huggingface。指到别的盘可以避免占满系统盘。
WHISPER_MODEL_DIR = r"D:\cs61a_models"

# 注意：whisper 这个版本【没有 --fp16 命令行参数】，FP16 由它依据 device 自动决定
# （在 CPU 上会自动降级为 FP32 并打警告）。所以不要传 --fp16，否则 CLI 会直接报
# unrecognized arguments 退出——和 --ffmpeg-location 是同一类坑。

# ---------------------------------------------------------------------------
# RapidOCR（本地 ONNX，模型随包内置，无需下载）
# ---------------------------------------------------------------------------
OCR_MIN_CONFIDENCE = 0.5    # 低于此置信度的识别结果丢弃

# 线程数：这个参数对速度影响非常大，务必不要用默认值。
# onnxruntime 默认(-1)会开满逻辑核心数，实测 28 核机器上严重超额订阅反而最慢。
# 实测同一批 1280x720 课件帧（取 3 张最快值平均）：
#     -1(28线程) 9.57s | 1线程 25.76s | 2线程 13.84s
#     4线程 7.43s | 8线程 5.83s | 16线程 9.51s
# 8 线程最快，比默认快 1.64 倍。换机器建议在 4~8 之间试。
OCR_NUM_THREADS = 8

# ---------------------------------------------------------------------------
# OCR 推理引擎
#
#   "auto"        = 装了 openvino 就用它，否则用 onnxruntime（推荐）
#   "openvino"    = 强制 OpenVINO
#   "onnxruntime" = 强制 onnxruntime
#
# 实测 18 张 1080p 课件帧：
#     onnxruntime CPU   23.1s（1.28s/张）
#     OpenVINO + 8线程  12.2s（0.68s/张）   快 1.9 倍，识别结果逐行完全一致
# OpenVINO 是 Intel 的推理框架，AMD/Intel CPU 也能用，不影响识别结果。
#
# 安装：pip install openvino
#
# 【别踩这个坑】不要在没有装 onnxruntime-gpu / onnxruntime-directml 的情况下把
# EngineConfig.onnxruntime.use_cuda / use_dml 打开：实测它会静默退化到一条慢路径，
# 23.1s 变成 38.8s——比 CPU 还慢 1.7 倍，把提速变成减速。代码里已经不会这么做。
# ---------------------------------------------------------------------------
OCR_ENGINE = "auto"

# 送 OCR 前是否缩放长边。实测【缩放没有收益】：
# 同一张图长边 1280 -> 8.04s，960 -> 10.49s，640 -> 8.72s，480 -> 8.71s，
# 识别行数不变。因为检测阶段内部本来就会把图缩放到自己的目标尺寸，
# 外部再缩是重复劳动。所以默认 0 = 不缩放，直接把文件路径交给 RapidOCR。
# 若以后要处理超大图（如 4K）可设成 1600 之类的值。
OCR_MAX_SIDE = 0

# ---------------------------------------------------------------------------
# 目录（相对于项目根目录）
# ---------------------------------------------------------------------------
VIDEO_DIR = "video"
FRAMES_DIR = "frames"
OUTPUT_DIR = "output"
LOGS_DIR = "logs"
SCRIPTS_DIR = "scripts"

# ---------------------------------------------------------------------------
# 场景合并（step4）
# ---------------------------------------------------------------------------
# 第一个场景的起始时间。step1 只在【画面切换】处抽帧，所以视频开头到第一次
# 切换之间的那一页本身没有帧（实测测试视频里 0~7.5 秒的第一页就没有被抽到）。
# 把首场景 start_time 定为 0.0，可以让开头的讲解字幕进入笔记，不至于整段丢失。
SCENE_FIRST_START = 0.0

# ---------------------------------------------------------------------------
# 抽帧 / 去重阈值
#
# SCENE_THRESHOLD 的取值有实测依据，不要照抄网上"电影场景检测"的 0.3：
# 实测一个 8 张幻灯片的课件视频（1280x720，每 7.5 秒硬切一次），
# 7 次切换的 ffmpeg scene 分数只有 0.063 ~ 0.125，最高 0.125。
# 原因：课件是浅色背景 + 大量留白 + 每页相同的深色标题栏，像素差异被稀释。
# 用 0.3 会导致【一帧都抓不到】，全部退化成兜底定时抽帧、严重漏帧。
# 所以这里取 0.05（约为实测最低切换分的 80%），宁可多抓一点交给 pHash 去重。
# ---------------------------------------------------------------------------
SCENE_THRESHOLD = 0.05      # FFmpeg scene 检测阈值，越小越敏感
PHASH_THRESHOLD = 8         # 感知哈希去重距离阈值，越小越严格

MIN_FRAME_GAP_SEC = 2.0     # 两帧最小时间间隔，防止同一画面刷出一堆帧
NMS_WINDOW_SEC = 0.6        # 一次画面切换只保留窗口内分数最高的那一帧
SCENE_RETRY_FLOOR = 0.01    # 候选太少时自动降到此阈值重扫一遍
FALLBACK_INTERVAL_SEC = 15  # 场景检测彻底失效（静态视频）时的兜底定时抽帧间隔
MIN_SCENE_FRAMES = 5        # 候选帧少于这个数就触发降阈值重扫 / 兜底抽帧

# ---------------------------------------------------------------------------
# 转场帧修正（这个坑很隐蔽，别删）
#
# select='gt(scene,TH)' 在【淡入淡出】过程中会连续命中大量帧：淡变时每一帧的
# 像素都在变化，逐帧差异都超过阈值。实测一个 1.1 秒的淡出淡入被抽成了 10 帧，
# 平均亮度从 240 一路掉到 2.6（全黑）再回升到 245——其中一半是黑帧/灰帧。
# 这些黑帧会成为"场景代表图"，用户在笔记里看到的就是发黑的幻灯片。
#
# 而且提高 SCENE_THRESHOLD 解决不了：淡变逐帧分数与真实换页分数重叠
# （换页实测 0.063~0.125），调高会把正常换页一起漏掉。
#
# 办法：对亮度异常的帧，改从 T+SCENE_SETTLE_OFFSET 重新抽一帧（转场已结束）。
# 只处理亮度异常的帧，正常画面不受影响。
# ---------------------------------------------------------------------------
SCENE_SETTLE_OFFSET = 0.5      # 每帧都改从 T±此偏移重抽，避开淡入淡出中间的黑帧

# ---------------------------------------------------------------------------
# 用显卡做硬件解码（只影响 step1 的抽帧扫描）
#
# 【默认关闭，因为实测它会随机卡死】
# 硬件解码确实快很多（9 分钟 1080p AV1：软件 50s → 硬件 24s），但 av1_cuvid 会
# 偶发死锁：同一条命令、同一个视频，一次 24 秒正常跑完，下一次就卡在 85% 处
# 再不前进（时间戳不动、elapsed 一直涨），整个流程就此挂住。
# 因为它【随机】发生，靠"先解一帧探测"根本拦不住（探测是通过的）。
#
# 想用的话设成 "auto"：程序会解密一帧探测可用性，并且带停滞看门狗——
# 30 秒没有进度就判定卡死、杀掉并自动改用软件解码重试，不会一直挂住。
#
# "off"  = 只用软件解码（默认，可靠）
# "auto" = 探测可用就用硬件解码，带卡死回退
# ---------------------------------------------------------------------------
FFMPEG_HW_DECODE = "off"

# 解码停滞多少秒判定卡死（仅硬件解码时启用）。
# 正常解码时 ffmpeg 每秒钟会刷好几行进度，30 秒毫无进度必然是卡死了。
FFMPEG_STALL_TIMEOUT = 30

# 是否额外补抽【视频第 0 秒】的帧，并放在帧序列最前面（scene_000001.jpg）。
#
# 为什么需要：select 滤镜只挑"画面切换"的帧，而视频第一帧的 scene 分数恒为 0
# （没有前一帧可比），所以视频开头那一页永远抽不到。实测 8 页幻灯片的测试视频
# 只抽到第 2~8 页，标题页完全没有代表图；而 step4 会把首场景 start_time 记为 0.0，
# 于是 0~7.5 秒那段被贴上了第二页的文字——标题页内容彻底丢失。
# 打开后首场景才有真正的代表图，start_time=0.0 也才名副其实。
EXTRACT_FIRST_FRAME = True

VIDEO_EXTENSIONS = (".mp4", ".mkv", ".flv", ".avi", ".mov", ".webm", ".ts", ".m4v")

# ---------------------------------------------------------------------------
# 生成 PDF（md_to_pdf.py / app.py）
# ---------------------------------------------------------------------------
# 用 Microsoft Edge 无头模式把 HTML 打印成 PDF。这是 Windows 上最省事的方案：
# 零额外依赖（Edge 系统自带），中文和代码字体都能正确内嵌。
# 留空 = 自动在常见安装路径里找；也可以写死完整路径。
# 实测生成的 PDF 会内嵌 MicrosoftYaHei / NSimSun / Consolas 字体子集，
# 中文正常显示且可搜索。
EDGE_PATH = ""

# 图片在 PDF 里的最大宽度（像素）。笔记配图原图 1280x720，
# 按 900px 重编码后单张约从 40KB 降到 8KB，
# 125 张图的 PDF 从 7MB 量级降到 1.5MB 量级，渲染也快很多。
# 设为 0 = 不缩放，直接用原图。
PDF_IMAGE_MAX_WIDTH = 900
PDF_IMAGE_QUALITY = 82

# ---------------------------------------------------------------------------
# 项目根目录（由本文件位置推导，避免依赖当前工作目录）
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent

VIDEO_PATH = PROJECT_ROOT / VIDEO_DIR
FRAMES_PATH = PROJECT_ROOT / FRAMES_DIR
OUTPUT_PATH = PROJECT_ROOT / OUTPUT_DIR
LOGS_PATH = PROJECT_ROOT / LOGS_DIR

# 中间产物统一放这里，方便脚本之间传递
FRAMES_MANIFEST = OUTPUT_PATH / "frames_manifest.json"
OCR_RESULT = OUTPUT_PATH / "ocr_result.json"
TRANSCRIPT_RESULT = OUTPUT_PATH / "transcript.json"


def ensure_dirs() -> None:
    """确保所有输出目录存在，脚本启动时调用一次即可。"""
    for path in (VIDEO_PATH, FRAMES_PATH, OUTPUT_PATH, LOGS_PATH):
        path.mkdir(parents=True, exist_ok=True)


def check_api_key() -> str:
    """返回可用的 API Key；没配置就报错并给出三种配置方式，避免跑到一半才失败。"""
    key = (DEEPSEEK_API_KEY or "").strip()
    if not key or key.startswith("你的") or key == "sk-你的新key":
        raise RuntimeError(
            "DEEPSEEK_API_KEY 未配置。三种配置方式，任选一种：\n"
            "  1) 在项目根目录建 .env 文件，写一行：DEEPSEEK_API_KEY=sk-xxxx\n"
            "  2) 设置环境变量：$env:DEEPSEEK_API_KEY=\"sk-xxxx\"（当前窗口有效）\n"
            "  3) 永久设置：setx DEEPSEEK_API_KEY \"sk-xxxx\"（重开终端生效）\n"
            "  密钥在 https://platform.deepseek.com/api_keys 申请。\n"
            "  注意：.env 已在 .gitignore 里，不会被提交到 Git。"
        )
    return key


def check_tools() -> None:
    """启动时校验外部可执行文件存在，给出明确报错而不是晦涩的 WinError。"""
    for name, path in (("ffmpeg", FFMPEG_PATH), ("ffprobe", FFPROBE_PATH)):
        if not Path(path).exists():
            raise FileNotFoundError(
                f"{name} 不存在：{path}\n"
                "  请安装 ffmpeg 并确保它在 PATH 里，或设置环境变量 FFMPEG_PATH，\n"
                "  或修改 config.py 顶部的 FFMPEG_PATH。"
            )
    if ffmpeg_lacks_dav1d():
        import logging
        logging.getLogger("config").warning(
            "当前 ffmpeg（%s）没有 libdav1d，抽帧会慢约 4 倍。\n"
            "  建议换一个带 libdav1d 的构建（如 BtbN 的 win64-gpl 或 gyan.dev 的 full 版）。",
            FFMPEG_PATH)


_DAV1D_CACHE = []


def ffmpeg_lacks_dav1d() -> bool:
    """探测当前 ffmpeg 是不是缺 libdav1d（缺了抽帧会慢好几倍，值得提醒一次）。"""
    if _DAV1D_CACHE:
        return _DAV1D_CACHE[0]
    missing = False
    try:
        proc = subprocess.run(
            [FFMPEG_PATH, "-hide_banner", "-buildconf"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, creationflags=CREATE_NO_WINDOW,
        )
        conf = proc.stdout or ""
        # 有 --enable-libdav1d 才算真支持
        missing = "--enable-libdav1d" not in conf
    except Exception:
        missing = False        # 探测不了就不打扰用户
    _DAV1D_CACHE.append(missing)
    return missing
