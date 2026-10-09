# -*- coding: utf-8 -*-
"""
把生成的 Markdown 笔记转成 PDF。

原理：Markdown -> 自包含 HTML（图片以 base64 内嵌）-> Edge 无头模式打印 PDF。

为什么走 Edge：
    Windows 上没有 pandoc / wkhtmltopdf / LibreOffice 时，Edge 是唯一稳定可用的
    渲染器，而且系统自带、零安装。实测输出的 PDF 会内嵌 MicrosoftYaHei / NSimSun /
    Consolas 字体子集，中文正常显示且可搜索，PPT 配图也正确嵌入。

为什么图片要 base64 内嵌而不是引用文件：
    笔记里的图片是相对路径（../frames/xxx.jpg）。转成 HTML 后如果还用相对路径，
    Edge 读取本地文件时容易因为跨目录/同源策略加载失败，PDF 里就会出现空图。
    内嵌成 data URI 后 HTML 完全自包含，放到任何地方都能正确渲染。

用法：
    python scripts/md_to_pdf.py output/lecture_final_notes.md
    python scripts/md_to_pdf.py output/lecture_final_notes.md -o D:/notes.pdf
    python scripts/md_to_pdf.py --all                # 转换 output/ 下所有笔记
"""

import argparse
import base64
import html
import io
import re
import subprocess
import sys
import time
from pathlib import Path

# 把项目根目录加进 sys.path，保证在任意工作目录下都能 import config
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402

# 每个子进程都要带这个标志，否则会各自弹出控制台黑框（本脚本由 app.py 用
# pythonw 拉起、自身没有控制台，再开 ffmpeg/Edge 时 Windows 会新建一个控制台窗口）。
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


# Edge 常见安装位置，找不到时按顺序试
EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
]

MIME_BY_SUFFIX = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".bmp": "image/bmp",
}

CSS = """
@page { size: A4; margin: 16mm 15mm 16mm 15mm; }
* { box-sizing: border-box; }
body {
  font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif;
  font-size: 10.5pt; line-height: 1.75; color: #1a1a1a; margin: 0;
}
h1 {
  font-size: 21pt; margin: 0 0 4px 0; padding-bottom: 8px;
  border-bottom: 2.5px solid #2b4a6f; color: #16324f;
}
h2 {
  font-size: 14pt; margin: 26px 0 8px 0; padding: 2px 0 2px 10px;
  border-left: 4px solid #4a7ab5; color: #1f3c5c;
  page-break-after: avoid;              /* 标题不要和正文被拆到两页 */
}
h3, h4, h5, h6 {
  font-size: 12pt; margin: 16px 0 6px 0; color: #2b4a6f; page-break-after: avoid;
}
p { margin: 7px 0; }
ul, ol { margin: 6px 0; padding-left: 22px; }
li { margin: 3px 0; }
code {
  font-family: Consolas, "Courier New", monospace; font-size: 9.5pt;
  background: #f0f2f5; padding: 1px 4px; border-radius: 3px; color: #b03060;
}
pre {
  background: #f6f8fa; border: 1px solid #d8dee4; border-radius: 4px;
  padding: 9px 11px; margin: 9px 0; overflow-wrap: break-word;
  white-space: pre-wrap; page-break-inside: avoid;
}
pre code {
  background: none; padding: 0; color: #24292e; font-size: 9pt; line-height: 1.55;
}
figure { margin: 10px 0 12px 0; page-break-inside: avoid; }
img { max-width: 100%; border: 1px solid #ccd3da; border-radius: 3px; display: block; }
blockquote {
  margin: 9px 0; padding: 7px 12px; background: #fff8e6;
  border-left: 3px solid #e0b64a; color: #5c4a1a;
}
blockquote p { margin: 0; }
hr { border: none; border-top: 1px solid #e1e4e8; margin: 18px 0; }
a { color: #1a5fb4; text-decoration: none; }
.missing { color: #b00020; font-size: 9pt; }
table {
  border-collapse: collapse; width: 100%; margin: 10px 0 14px 0;
  font-size: 9.5pt; page-break-inside: avoid;
}
th, td {
  border: 1px solid #d3dae2; padding: 5px 9px; text-align: left;
  vertical-align: top; line-height: 1.6;
}
th { background: #eef2f7; color: #1f3c5c; font-weight: bold; white-space: nowrap; }
tbody tr:nth-child(even) { background: #fafbfd; }
td code, th code { font-size: 9pt; }
"""


# ---------------------------------------------------------------------------
def find_edge(explicit: str = "") -> str:
    """定位 msedge.exe。explicit / config.EDGE_PATH 优先，否则按常见路径找。"""
    for candidate in (explicit, getattr(config, "EDGE_PATH", "")):
        if candidate and Path(candidate).exists():
            return str(candidate)
    for candidate in EDGE_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    # 最后退一步：交给 PATH
    import shutil
    found = shutil.which("msedge")
    if found:
        return found
    raise FileNotFoundError(
        "找不到 Microsoft Edge（msedge.exe）。\n"
        "请安装 Edge，或在 config.py 里把 EDGE_PATH 指向它的完整路径。"
    )


# ---------------------------------------------------------------------------
# Markdown -> HTML
# ---------------------------------------------------------------------------
def _inline(text: str) -> str:
    """
    处理行内标记。必须先 html.escape 再插标签：
    反过来会把我们自己生成的 <code> 标签也转义掉。
    """
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    # 站内锚点链接（[文字](#xxx)）在 PDF 里跳不过去（标题没有对应 id），
    # 所以只保留文字，免得印出一串点了没反应的死链。
    text = re.sub(r"\[([^\]]+)\]\(#[^)]*\)", r"\1", text)
    # 其余链接渲染成可点击的 <a>
    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", r'<a href="\2">\1</a>', text)
    return text


def _image_tag(src: str, alt: str, base_dir: Path) -> str:
    """
    把图片转成 base64 内嵌的 <figure>。

    按 config.PDF_IMAGE_MAX_WIDTH 缩放重编码：
    125 张 1280x720 的配图不缩放会让 PDF 到 7MB 量级，缩放后约 1.5MB。
    """
    path = (base_dir / src).resolve()
    if not path.exists():
        return f'<p class="missing">[图片缺失：{html.escape(src)}]</p>'

    mime = MIME_BY_SUFFIX.get(path.suffix.lower(), "image/jpeg")
    max_width = getattr(config, "PDF_IMAGE_MAX_WIDTH", 0)

    if max_width:
        try:
            from PIL import Image
            with Image.open(path) as im:
                im = im.convert("RGB")
                if im.width > max_width:
                    ratio = max_width / im.width
                    im = im.resize(
                        (max_width, max(1, int(im.height * ratio))), Image.LANCZOS
                    )
                buf = io.BytesIO()
                im.save(buf, format="JPEG",
                        quality=getattr(config, "PDF_IMAGE_QUALITY", 82),
                        optimize=True)
                payload = base64.b64encode(buf.getvalue()).decode("ascii")
                mime = "image/jpeg"
        except Exception:
            # 缩放失败就退回原图，不能因为优化把图丢了
            payload = base64.b64encode(path.read_bytes()).decode("ascii")
    else:
        payload = base64.b64encode(path.read_bytes()).decode("ascii")

    return (f'<figure><img alt="{html.escape(alt)}" '
            f'src="data:{mime};base64,{payload}"></figure>')


# 表格分隔行，例如 |---|---| 或 |:--|--:|:--:|
TABLE_SEP_RE = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?$")


def _split_row(line: str) -> list:
    """把 | a | b | c | 拆成 ['a', 'b', 'c']。"""
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [cell.strip() for cell in text.split("|")]


def _table_align(sep_line: str) -> list:
    """从分隔行读出对齐方式（:-- 左对齐、--: 右对齐、:-: 居中）。"""
    aligns = []
    for cell in _split_row(sep_line):
        left, right = cell.startswith(":"), cell.endswith(":")
        aligns.append("center" if left and right else
                      "right" if right else
                      "left" if left else "")
    return aligns


def _render_table(header: list, aligns: list, rows: list) -> str:
    """
    渲染 Markdown 表格。

    为什么要专门支持：README 这类文档几乎全靠表格承载信息，不支持的话
    会退化成一堆散段落，完全没法读。
    """
    def cell(tag: str, text: str, index: int) -> str:
        style = f' style="text-align:{aligns[index]}"' if index < len(aligns) and aligns[index] else ""
        return f"<{tag}{style}>{_inline(text)}</{tag}>"

    parts = ["<table><thead><tr>"]
    parts += [cell("th", c, i) for i, c in enumerate(header)]
    parts.append("</tr></thead><tbody>")
    for row in rows:
        # 列数不足时补空格，避免表格错位
        row = row + [""] * (len(header) - len(row))
        parts.append("<tr>")
        parts += [cell("td", c, i) for i, c in enumerate(row[:len(header)])]
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


def _render_blocks(lines: list, base_dir: Path) -> list:
    """
    把 Markdown 行渲染成 HTML 片段列表。

    单独抽成函数是为了支持【引用块递归】：引用块内部同样可以包含代码块、
    列表、甚至嵌套引用，直接当普通段落处理会把 ``` 原样打出来。
    """
    body, para = [], []
    in_code, code_lines = False, []
    fence_char, fence_len = "`", 3

    def flush_para():
        if para:
            body.append("<p>" + _inline(" ".join(para)) + "</p>")
            para.clear()

    def flush_code():
        if code_lines:
            code = html.escape("\n".join(code_lines), quote=False)
            body.append(f"<pre><code>{code}</code></pre>")
        code_lines.clear()

    idx = 0
    while idx < len(lines):
        raw = lines[idx]
        line = raw.rstrip()

        # 代码围栏：内容必须用原始行（保留缩进，缩进就是代码语义）
        #
        # 按 CommonMark 的规则处理【围栏长度】：关闭围栏的反引号数量必须 >= 开启它的。
        # 这不是吹毛求疵——文档里写"示例里再套一段代码"很常见（README 的笔记格式示例
        # 就是 ```markdown 里套 ```python）。只判断 startswith("```") 的话，
        # 内层的 ``` 会提前把外层关掉，后面的内容全部错乱。
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[0]
            length = len(stripped) - len(stripped.lstrip(marker))
            rest = stripped[length:].strip()
            if in_code:
                # 关闭围栏：符号要一致、长度不少于开启时、且后面不能再有别的内容
                if marker == fence_char and length >= fence_len and not rest:
                    flush_code()
                    in_code = False
            else:
                # 开启围栏：后面【可以】跟语言名（```python），别要求它是空的
                flush_para()
                in_code, fence_char, fence_len = True, marker, length
            idx += 1
            continue
        if in_code:
            code_lines.append(raw)
            idx += 1
            continue

        if not line.strip():
            flush_para()
            idx += 1
            continue

        # 表格：当前行是 |...|，且下一行是 |---|---| 分隔行。
        # 必须向前看一行才能确认，否则会把正文里单独出现的 | 当成表格。
        if (line.lstrip().startswith("|") and idx + 1 < len(lines)
                and TABLE_SEP_RE.match(lines[idx + 1].strip())):
            flush_para()
            header = _split_row(line)
            aligns = _table_align(lines[idx + 1])
            idx += 2
            rows = []
            while idx < len(lines) and lines[idx].strip().startswith("|"):
                rows.append(_split_row(lines[idx]))
                idx += 1
            body.append(_render_table(header, aligns, rows))
            continue

        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            flush_para()
            level = len(heading.group(1))
            body.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            idx += 1
            continue

        if line.startswith("!["):
            flush_para()
            match = re.match(r"^!\[(.*?)\]\((.*?)\)\s*$", line)
            if match:
                body.append(_image_tag(match.group(2), match.group(1), base_dir))
            idx += 1
            continue

        if line.strip() in ("---", "***", "___"):
            flush_para()
            body.append("<hr>")
            idx += 1
            continue

        if line.lstrip().startswith(">"):
            flush_para()
            # 连续的 > 行合并成【一个】引用块（标准 Markdown 行为），
            # 再递归解析里面的内容——这样引用块里的代码块/列表才能正确渲染。
            quote_lines = []
            while idx < len(lines) and lines[idx].lstrip().startswith(">"):
                inner_line = lines[idx].lstrip()[1:]
                quote_lines.append(inner_line[1:] if inner_line.startswith(" ") else inner_line)
                idx += 1
            inner = "".join(_render_blocks(quote_lines, base_dir))
            body.append(f"<blockquote>{inner}</blockquote>")
            continue

        # 列表：按前导空格数还原嵌套层级（模型输出的子项就是这么缩进的）
        bullet = re.match(r"^(\s*)[-*+]\s+(.*)$", line)
        ordered = re.match(r"^(\s*)(\d+[.)])\s+(.*)$", line)
        if bullet or ordered:
            flush_para()
            indent = len((bullet or ordered).group(1))
            level = min(4, indent // 2)
            if bullet:
                content = bullet.group(2)
                marker = "•"
            else:
                content = ordered.group(3)
                marker = ordered.group(2)
            body.append(
                f'<p style="margin:3px 0 3px {level * 20}px">'
                f'{marker}&nbsp;{_inline(content)}</p>'
            )
            idx += 1
            continue

        para.append(line.strip())
        idx += 1

    flush_para()
    if in_code:          # 围栏没闭合也要兜住，别把内容吞了
        flush_code()

    return body


def md_to_html(md_text: str, base_dir: Path, title: str = "") -> str:
    """把笔记 Markdown 转成自包含的 HTML 文档。"""
    body = _render_blocks(md_text.split("\n"), base_dir)
    safe_title = html.escape(title or "课程笔记")
    return (
        '<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        f"<title>{safe_title}</title>\n<style>{CSS}</style>\n</head>\n<body>\n"
        + "\n".join(body)
        + "\n</body>\n</html>\n"
    )


# ---------------------------------------------------------------------------
# HTML -> PDF
# ---------------------------------------------------------------------------
def html_to_pdf(html_path: Path, pdf_path: Path, edge: str, timeout: int = 300) -> None:
    """
    用 Edge 无头模式打印 PDF。

    --user-data-dir 指向项目内的独立目录：否则 Edge 会尝试用用户正在使用的
    默认配置文件，可能因为被占用而失败或弹窗。
    --no-pdf-header-footer 去掉页眉页脚上的网址和日期。
    """
    profile_dir = config.PROJECT_ROOT / ".edge_pdf_profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    # 必须传【绝对路径】：Edge 是按自己的进程工作目录解析 --print-to-pdf 的，
    # 传相对路径会报 "系统找不到指定的路径 (0x3)" 且退出码仍是 0（很难排查）。
    pdf_abs = pdf_path.resolve()

    cmd = [
        edge,
        "--headless",
        "--disable-gpu",
        "--no-pdf-header-footer",
        f"--user-data-dir={profile_dir}",
        f"--print-to-pdf={pdf_abs}",
        html_path.resolve().as_uri(),
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )
    if not pdf_path.exists() or pdf_path.stat().st_size == 0:
        tail = "\n".join(((proc.stderr or "") + (proc.stdout or "")).strip().splitlines()[-14:])
        raise RuntimeError(
            f"Edge 没有生成 PDF（退出码 {proc.returncode}，目标 {pdf_path}）。\n"
            f"两种常见原因：\n"
            f"  1. 在受限环境（如 DSH 沙箱）里运行。Edge 是多进程架构、依赖命名管道通信，\n"
            f"     被沙箱禁止时会直接启动失败。请在沙箱外的普通终端里运行。\n"
            f"  2. 输出路径不可写，或磁盘空间不足。\n"
            f"末尾输出：\n{tail}"
        )


def convert(md_path: Path, pdf_path: Path = None, keep_html: bool = False,
            log=print) -> Path:
    """Markdown 文件 -> PDF，返回 PDF 路径。"""
    md_path = Path(md_path)
    if not md_path.exists():
        raise FileNotFoundError(f"找不到 Markdown 文件：{md_path}")
    pdf_path = Path(pdf_path) if pdf_path else md_path.with_suffix(".pdf")

    log(f"读取 {md_path.name}（{md_path.stat().st_size / 1024:.1f} KB）")
    md_text = md_path.read_text(encoding="utf-8")
    log(f"转换 Markdown -> HTML（图片内嵌，共 {md_text.count('![')} 张）")
    html_text = md_to_html(md_text, md_path.parent, title=md_path.stem)

    html_path = md_path.with_suffix(".html")
    html_path.write_text(html_text, encoding="utf-8")
    size_kb = html_path.stat().st_size / 1024
    log(f"HTML 大小 {size_kb / 1024:.1f} MB" if size_kb > 1024
        else f"HTML 大小 {size_kb:.0f} KB")

    edge = find_edge()
    log(f"调用 Edge 打印 PDF：{Path(edge).name}")
    started = time.time()
    html_to_pdf(html_path, pdf_path, edge)
    log(f"PDF 生成成功：{pdf_path.name}（{pdf_path.stat().st_size / 1024:.1f} KB，"
        f"耗时 {time.time() - started:.1f} 秒）")

    if not keep_html:
        html_path.unlink(missing_ok=True)
    return pdf_path


# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="把 Markdown 笔记转成 PDF")
    parser.add_argument("md", nargs="?", help="Markdown 文件路径")
    parser.add_argument("-o", "--output", help="输出 PDF 路径（默认与 md 同名同目录）")
    parser.add_argument("--all", action="store_true",
                        help="转换 output/ 下所有 *_final_notes.md")
    parser.add_argument("--keep-html", action="store_true",
                        help="保留中间 HTML（排查排版问题时有用）")
    args = parser.parse_args()

    try:
        find_edge()
    except FileNotFoundError as exc:
        print(f"[错误] {exc}")
        return 1

    if args.all:
        targets = sorted(config.OUTPUT_PATH.glob("*_final_notes.md"))
        if not targets:
            print(f"[错误] {config.OUTPUT_PATH} 下没有 *_final_notes.md")
            return 1
    elif args.md:
        targets = [Path(args.md)]
    else:
        parser.print_help()
        return 2

    ok = 0
    for md in targets:
        print(f"\n=== {md.name} ===")
        try:
            convert(md, args.output, keep_html=args.keep_html)
            ok += 1
        except Exception as exc:
            print(f"[失败] {md.name} -> {exc}")
    print(f"\n完成 {ok}/{len(targets)} 个")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
