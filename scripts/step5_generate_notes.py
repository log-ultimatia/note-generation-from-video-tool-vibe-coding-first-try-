# -*- coding: utf-8 -*-
"""
阶段 5：DeepSeek 生成场景标签 + 中文笔记

流程：
    1. 读取 output/{视频名}_scenes.json
    2. 逐个场景调用 DeepSeek（一次一个场景，避免超 token）
    3. 把返回的 tag / summary / note 合并回 scenes.json
    4. 生成 output/{视频名}_final_notes.md
    5. 日志写 logs/step5.log

喂给模型的 OCR 文字做了两件加工（都在下面有独立函数和注释）：
    A. 用 step3 的 x 坐标还原行首缩进——否则 Python 代码的缩进是丢的，
       模型据此写出的代码块就是错的。
    B. 剔除跨场景重复出现的短文本（页眉/页脚/水印），避免笔记里灌满噪声。

已知的接口坑（都已在代码里处理，别改掉）：
    - 该端点返回体里 reasoning_content 是独立字段，content 常为空。
      max_tokens 给小了会全被推理吃光，finish_reason=length 且 content=""。
      所以这里读 content，并在遇到 length 截断时自动翻倍 max_tokens 重试。
    - 模型是纯文本的，不接受图片输入（图片会被静默换成 [Unsupported Image]）。

用法：
    python scripts/step5_generate_notes.py                # 处理 output/ 下所有视频
    python scripts/step5_generate_notes.py lecture        # 只处理指定视频
    python scripts/step5_generate_notes.py --force        # 已生成过的场景也重新调用

    默认会跳过已经带 tag 的场景（API 调用要花钱花时间，中断后可安全续跑）。
"""

import argparse
import http.client
import json
import logging
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# 把项目根目录加进 sys.path，保证在任意工作目录下都能 import config
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402

PROMPT_TEMPLATE = """你是一位 CS61A 课程的助教。以下是一页 PPT 的 OCR 文字和老师的讲解转录。
请输出 JSON，包含三个字段：
- "tag": 10 字以内的中文标签
- "summary": 30 字以内的中文摘要
- "note": 学术风格的中文笔记，要求：
  1. 保留技术术语准确性（recursion=递归, base case=基本情况, higher-order function=高阶函数, environment diagram=环境图, tail call=尾调用）
  2. 不要逐句翻译，提炼核心概念和逻辑
  3. 如果讲解提到代码，用 markdown 代码块保留
  4. 如果提到注意事项或常见错误，单独用无序列表列出

OCR 文字：
{ocr_text}

讲解转录：
{transcript}
"""

# 图片引用路径。
# 规范里写的是 "frames/{视频名}/{image}"，但笔记文件位于 output/ 目录下，
# 那样写会被解析成 output/frames/... ，导致所有图片裂图。
# 这里用相对 output/ 的正确写法 "../frames/..."。
# 若你要严格照规范写，把下面这行改回 "frames/{video}/{image}" 即可。
IMAGE_REF_TEMPLATE = "../frames/{video}/{image}"

# 缩进还原用：小于这个像素差视为同一层级（抖动/字距），不当作缩进
MIN_INDENT_PX = 8


def setup_logger() -> logging.Logger:
    logger = logging.getLogger("step5")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    config.LOGS_PATH.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(config.LOGS_PATH / "step5.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


def fmt_time(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# OCR 文字加工
# ---------------------------------------------------------------------------
def find_repeated_texts(scenes: list, ratio: float, max_len: int, logger) -> set:
    """
    找出跨场景重复出现的短文本（页眉、页脚、水印、页码）。

    判定：在 >= ratio 比例的场景里都出现、且长度 <= max_len 的文本。
    实测课件右下角固定有 "CS61A" 页脚，每页都被 OCR 识别出来，
    不剔除的话每段笔记都会带一行噪声。
    """
    counter = Counter()
    for scene in scenes:
        # 同一场景内重复出现只算一次
        seen = {
            (item.get("text") or "").strip()
            for item in (scene.get("ocr_items") or [])
            if (item.get("text") or "").strip()
        }
        for text in seen:
            if len(text) <= max_len:
                counter[text] += 1

    if not scenes:
        return set()
    threshold = max(2, int(len(scenes) * ratio))
    dropped = {text for text, count in counter.items() if count >= threshold}
    if dropped:
        preview = "; ".join(sorted(dropped)[:8])
        logger.info("剔除跨场景重复文本 %d 条（出现于 >=%d/%d 个场景）：%s",
                    len(dropped), threshold, len(scenes), preview)
    return dropped


def render_ocr_lines(items: list, max_level: int) -> str:
    """
    把 ocr_items 渲染成保留缩进的文本块（按 y 排行序，按 x 推缩进层级）。

    缩进基准怎么定（这里踩过坑，别随手改）：
      - 基准 x 取【出现次数最多】的那个值（按 4 像素网格聚合以吸收抖动），
        而不是取最小 x。因为标题栏文字往往在最左边（如 x=34），
        用最小 x 当基准会把正文整体算成深缩进。
      - 缩进单位取"所有 >= MIN_INDENT_PX 的正向差值里最小的那个"。
      - 小于 MIN_INDENT_PX 的差值当作抖动，视为同一层级。
      - 找不到有效单位时（整页文字 x 基本一致，比如纯列表页）就不缩进。
    """
    entries = []
    for item in items:
        text = (item.get("text") or "").strip()
        if not text:
            continue
        x, y = item.get("x"), item.get("y")
        entries.append((text, x if isinstance(x, (int, float)) else None,
                        y if isinstance(y, (int, float)) else 0))
    if not entries:
        return ""

    xs = [e[1] for e in entries if e[1] is not None]
    levels = [0] * len(entries)

    if xs:
        grid = 4
        buckets = Counter(int(round(x / grid)) for x in xs)
        # 出现最多的一档作基准；并列时取更靠左的那个
        base = max(buckets.items(), key=lambda kv: (kv[1], -kv[0]))[0] * grid

        deltas = [(x - base) if x is not None else None for x in xs]  # 与 xs 同序
        # 把 deltas 映射回 entries（entries 中 x 为 None 的跳过）
        positive = [d for d in deltas if d is not None and d >= MIN_INDENT_PX]
        unit = min(positive) if positive else 0

        if unit:
            di = 0
            for i, entry in enumerate(entries):
                if entry[1] is None:
                    continue
                d = deltas[di]
                di += 1
                if d is not None and d >= MIN_INDENT_PX:
                    levels[i] = min(max_level, max(1, int(round(d / unit))))

    order = sorted(range(len(entries)), key=lambda i: entries[i][2])
    return "\n".join("  " * levels[i] + entries[i][0] for i in order)


def build_ocr_block(scene: dict, dropped: set, max_level: int) -> str:
    """优先生成带缩进的文本块；没有坐标时退回纯文字列表。"""
    items = scene.get("ocr_items") or []
    if items:
        kept = [it for it in items
                if (it.get("text") or "").strip() not in dropped]
        rendered = render_ocr_lines(kept, max_level)
        if rendered:
            return rendered
    texts = [t for t in (scene.get("ocr_text") or []) if t.strip() not in dropped]
    return "\n".join(texts)


# ---------------------------------------------------------------------------
# DeepSeek 调用
# ---------------------------------------------------------------------------
# 记录服务端是否支持 response_format={"type":"json_object"}。
# step5 现在会并发调用，所以这个开关要在锁里读写：否则多个线程同时探测、
# 判断和写入会互相干扰（最坏情况是少数请求白跑一次 400）。
_JSON_FORMAT_OK = True
_JSON_FORMAT_LOCK = threading.Lock()


def _json_format_enabled() -> bool:
    with _JSON_FORMAT_LOCK:
        return _JSON_FORMAT_OK


def _disable_json_format() -> None:
    global _JSON_FORMAT_OK
    with _JSON_FORMAT_LOCK:
        _JSON_FORMAT_OK = False


def is_conn_drop(exc: BaseException) -> bool:
    """
    判断是不是"连接层被服务端掐断"这类错误。

    为什么单独区分它：实测这个接口在高频调用下会偶发
        RemoteDisconnected: Remote end closed connection without response
    这类错误代表连接直接断了，**服务端并没有在处理我们的请求**，
    所以等 2/4/8 秒退避毫无意义——立刻重试往往就成功。
    而 429（限流）和 5xx（服务端忙）才需要指数退避，让服务端缓过来。

    urllib 会把底层异常包在 URLError.reason 里，所以要递归检查。
    """
    if isinstance(exc, (http.client.RemoteDisconnected,
                        http.client.BadStatusLine,
                        ConnectionResetError,
                        ConnectionAbortedError,
                        BrokenPipeError,
                        ConnectionRefusedError)):
        return True
    if isinstance(exc, urllib.error.URLError):
        return is_conn_drop(exc.reason)
    return False


def extract_json(text: str) -> dict:
    """
    从模型输出里抠出 JSON 对象。

    实测模型返回过这些形态（所以不能直接用 json.loads 硬解）：
      - 被 ```json 围栏包起来
      - 在 JSON 后面又补一段说明文字 -> json.loads 报 "Extra data"
      - 把 response_format 的 type 字段一起回显进返回体
        （形如 {"type": "json_object", "tag": ...}）
      - max_tokens 不够时 JSON 被截断 -> 任何解析都会失败（由调用方翻倍重试）

    这里改用 JSONDecoder.raw_decode：从第一个 { 开始，只解析【恰好一个】
    JSON 值，后面的多余内容自动忽略，比手写括号切片可靠。
    （之前用 find("{") + rfind("}") 切片，遇到"两个对象"会把两段一起框进去，
     反而制造出 Extra data 错误。）
    """
    stripped = (text or "").strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[A-Za-z]*\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped).strip()

    start = stripped.find("{")
    if start == -1:
        raise ValueError(f"返回内容里没有 JSON 对象：{stripped[:200]}")

    try:
        data, _ = json.JSONDecoder().raw_decode(stripped[start:])
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 不完整或非法（{exc}）：{stripped[:200]}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"解析结果不是对象：{type(data).__name__}")
    data.pop("type", None)     # 去掉 response_format 的回显字段
    return data


def deepseek_json(prompt: str, logger: logging.Logger) -> dict:
    """
    调 DeepSeek，返回解析后的 dict。内含两层容错：
      1. response_format 若不被支持 -> 关掉它重试（只关一次，之后不再带）
      2. content 为空且 finish_reason=length -> 推理把 token 吃光了，
         把 max_tokens 翻倍重试
    """
    global _JSON_FORMAT_OK

    key = config.check_api_key()
    url = config.DEEPSEEK_BASE_URL.rstrip("/") + "/v1/chat/completions"
    max_tokens = config.DEEPSEEK_MAX_TOKENS
    last_error = None
    dropped = False          # 本轮是否属于"连接被掐断"

    for attempt in range(1, config.DEEPSEEK_MAX_RETRY + 1):
        dropped = False
        payload = {
            "model": config.DEEPSEEK_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": config.DEEPSEEK_TEMPERATURE,
        }
        if _json_format_enabled():
            payload["response_format"] = {"type": "json_object"}
        # 限制"思考"强度：模型默认会先想一大段再回答，思考 token 既计费又挤占
        # max_tokens 预算（实测有场景把 4096 全花在思考上、content 为空，
        # 被迫翻倍重试两次）。默认 "low" 的笔记质量与不限制时一致，但快一倍以上。
        effort = getattr(config, "DEEPSEEK_REASONING_EFFORT", "")
        if effort:
            payload["reasoning_effort"] = effort

        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + key,
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=config.DEEPSEEK_TIMEOUT) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = (exc.read() or b"").decode("utf-8", "replace")
            last_error = f"HTTP {exc.code}: {detail[:300]}"
            # response_format 不被支持时关掉它，立即重试
            if _json_format_enabled() and ("response_format" in detail or exc.code == 400):
                logger.warning("接口不支持 response_format，改为提示词约束 JSON 后重试")
                _disable_json_format()
                continue
            logger.warning("第 %d/%d 次调用失败：%s", attempt, config.DEEPSEEK_MAX_RETRY, last_error)
        except Exception as exc:
            dropped = is_conn_drop(exc)
            prefix = "连接被服务端掐断" if dropped else "调用异常"
            last_error = f"{prefix}：{type(exc).__name__}: {exc}"
            logger.warning("第 %d/%d 次调用失败：%s",
                           attempt, config.DEEPSEEK_MAX_RETRY, last_error)
        else:
            choice = (body.get("choices") or [{}])[0]
            message = choice.get("message") or {}
            content = (message.get("content") or "").strip()
            finish = choice.get("finish_reason")

            if not content:
                # 关键坑：reasoning_content 是独立字段，max_tokens 小时它会吃光预算
                reasoning = (message.get("reasoning_content") or "")
                if finish == "length" or reasoning:
                    max_tokens = min(max_tokens * 2, 32768)
                    last_error = (f"content 为空（finish_reason={finish}，"
                                  f"推理内容 {len(reasoning)} 字），"
                                  f"max_tokens 提到 {max_tokens} 重试")
                    logger.warning("  %s", last_error)
                    continue
                last_error = "模型返回了空 content"
                logger.warning("  %s", last_error)
            else:
                try:
                    data = extract_json(content)
                except Exception as exc:
                    last_error = f"JSON 解析失败：{exc}"
                    logger.warning("  %s", last_error)
                    # 解析失败最常见的原因是 JSON 被 max_tokens 截断，
                    # 所以这里也翻倍预算再试一次
                    max_tokens = min(max_tokens * 2, 32768)
                else:
                    # 内容非空校验：实测偶发 HTTP 200、JSON 也合法，但三个字段全为空。
                    # 这种情况绝不能当成功——否则该场景被静默标记为已完成，
                    # 笔记里直接缺一整页，而且下一轮不会再补（除非 tag 为空才会重试）。
                    if not (data.get("tag") or "").strip() \
                            and not (data.get("note") or "").strip():
                        last_error = "模型返回了空内容（tag 与 note 均为空）"
                        logger.warning("  %s", last_error)
                    else:
                        return data

        if attempt < config.DEEPSEEK_MAX_RETRY:
            if dropped:
                # 连接断了、服务端压根没处理请求，等退避纯属浪费。
                # 留 0.5 秒只是避免极端情况下的死循环式直冲。
                time.sleep(0.5)
            else:
                time.sleep(2 ** attempt)      # 限流/5xx：指数退避

    raise RuntimeError(f"DeepSeek 调用最终失败：{last_error}")


def demote_headings(text: str, levels: int) -> str:
    """
    把笔记正文里的 markdown 标题整体降级。

    原因：场景标题用的是 "## HH:MM:SS - tag"（二级），而模型返回的 note 里
    常自带 "# xxx" / "## xxx" 标题。实测出现过 note 里带一级标题的情况
    （"# 递归过程（The Recursive Process）"），直接拼进文档会让全文出现多个
    一级标题、大纲结构错乱。统一降 levels 级（默认 2： # -> ###，## -> ####）。

    必须跳过代码块：Python 代码里的行首注释（如 "# 基本情况"）长得和标题
    一模一样，不做围栏判断就会被改坏。
    """
    if levels <= 0:
        return text

    out, in_fence = [], False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        if not in_fence:
            match = re.match(r"^(#{1,6})(\s+)(.*)$", line)
            if match:
                level = min(6, len(match.group(1)) + levels)
                line = "#" * level + match.group(2) + match.group(3)
        out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------------------
def process_video(video: str, force: bool, logger: logging.Logger) -> Path:
    scenes_path = config.OUTPUT_PATH / f"{video}_scenes.json"
    scenes = json.loads(scenes_path.read_text(encoding="utf-8"))
    if not scenes:
        raise RuntimeError(f"{scenes_path.name} 是空的")

    logger.info("读入 %d 个场景", len(scenes))

    dropped = set()
    if getattr(config, "STEP5_DROP_REPEATED_LINES", True):
        dropped = find_repeated_texts(
            scenes,
            getattr(config, "STEP5_REPEAT_SCENE_RATIO", 0.6),
            getattr(config, "STEP5_REPEAT_MAX_LEN", 20),
            logger,
        )
    max_level = getattr(config, "STEP5_MAX_INDENT_LEVEL", 6)

    todo = [s for s in scenes if force or not (s.get("tag") or "").strip()]
    if len(todo) < len(scenes):
        logger.info("跳过已标注的 %d 个场景（--force 可全部重做）",
                    len(scenes) - len(todo))

    workers = max(1, int(getattr(config, "DEEPSEEK_CONCURRENCY", 1)))
    if workers > 1 and len(todo) > 1:
        logger.info("并发调用：%d 个线程（单次调用只要 3~5 秒，串行时墙钟时间"
                    "几乎全耗在等接口回话上）", workers)

    started = time.time()
    write_lock = threading.Lock()
    done_counter = [0]

    def handle(scene) -> bool:
        """处理单个场景。返回是否成功。会在多线程里并发执行。"""
        ocr_block = build_ocr_block(scene, dropped, max_level)
        prompt = PROMPT_TEMPLATE.format(
            ocr_text=ocr_block or "(本页没有识别到文字)",
            transcript=(scene.get("transcript") or "").strip() or "(本段没有转录文字)",
        )

        label = f"场景 {scene['scene_id']}/{len(scenes)} @{fmt_time(scene['start_time'])}"
        try:
            result = deepseek_json(prompt, logger)
        except Exception as exc:
            # 单个场景失败不该中断整批：记下来，最后统一汇报
            logger.error("%s 生成失败：%s", label, exc)
            scene["note_error"] = str(exc)
            return False

        scene["tag"] = str(result.get("tag") or "").strip()
        scene["summary"] = str(result.get("summary") or "").strip()
        scene["note"] = str(result.get("note") or "").strip()
        scene.pop("note_error", None)
        done_counter[0] += 1

        # 每完成一个场景就落盘：40 分钟的视频场景很多，
        # 中途失败/中断后可安全续跑，不用重花 API 费用。
        # 并发时必须加锁，否则多个线程同时写会把文件写坏。
        with write_lock:
            scenes_path.write_text(
                json.dumps(scenes, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        logger.info("%s -> [%s] %s", label, scene["tag"], scene["summary"][:42])
        return True

    if workers > 1 and len(todo) > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(handle, todo))
    else:
        results = [handle(scene) for scene in todo]
    done = sum(1 for r in results if r)

    elapsed = time.time() - started
    logger.info("DeepSeek 调用完成：成功 %d/%d 个场景，耗时 %.1f 分钟",
                done, len(todo), elapsed / 60)

    # ---- 生成 Markdown ----
    demote = getattr(config, "STEP5_DEMOTE_NOTE_HEADINGS", 2)
    lines = [f"# {video} 课程笔记", ""]
    for scene in scenes:
        note = (scene.get("note") or "").strip()
        if not note:
            continue
        tag = (scene.get("tag") or "").strip() or "未命名"
        image_ref = IMAGE_REF_TEMPLATE.format(video=video, image=scene["image"])
        lines.append(f"## {fmt_time(scene['start_time'])} - {tag}")
        lines.append(f"![PPT]({image_ref})")
        lines.append("")
        lines.append(demote_headings(note, demote))
        lines.append("")

    md_path = config.OUTPUT_PATH / f"{video}_final_notes.md"
    md_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    tagged = sum(1 for s in scenes if (s.get("note") or "").strip())
    logger.info("已写出 %s（%d 段笔记，%d 个场景）",
                md_path.name, tagged, len(scenes))
    return md_path


def main() -> int:
    parser = argparse.ArgumentParser(description="阶段5：DeepSeek 生成场景标签与中文笔记")
    parser.add_argument("video", nargs="?", help="只处理指定视频名（默认处理全部）")
    parser.add_argument("--force", action="store_true",
                        help="已带 tag 的场景也重新调用（默认跳过，省钱省时间）")
    args = parser.parse_args()

    logger = setup_logger()
    config.ensure_dirs()

    try:
        config.check_api_key()
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 1

    videos = sorted(
        p.name[: -len("_scenes.json")]
        for p in config.OUTPUT_PATH.glob("*_scenes.json")
    )
    if args.video:
        videos = [v for v in videos if v == args.video]
    if not videos:
        logger.error("output/ 下没有 *_scenes.json，请先运行：python scripts/step4_merge_scenes.py")
        return 1

    logger.info("=" * 60)
    logger.info("开始生成笔记：%d 个视频（model=%s）", len(videos), config.DEEPSEEK_MODEL)

    ok = 0
    for video in videos:
        logger.info("-" * 60)
        logger.info("处理：%s", video)
        try:
            process_video(video, args.force, logger)
            ok += 1
        except Exception as exc:
            logger.error("失败：%s -> %s", video, exc)

    logger.info("=" * 60)
    logger.info("完成：%d/%d 个视频", ok, len(videos))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
