# -*- coding: utf-8 -*-
"""
生成应用图标 assets/icon.ico。

为什么要单独写一个脚本：图标二进制不方便直接写进仓库，
而生成逻辑只有十几行，需要时重跑一次即可。

用法：
    python scripts/make_icon.py
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUT = PROJECT_ROOT / "assets" / "icon.ico"

# 画一个"文档 + 播放三角"的图标：一眼能看出是"视频转文档"
BG = (43, 74, 111)          # 深蓝，和界面主题同色
PAPER = (247, 249, 252)
ACCENT = (233, 108, 79)     # 橙色播放键


def render(size: int) -> Image.Image:
    # 用 4 倍超采样再缩小，得到平滑边缘（PIL 不做抗锯齿绘图）
    scale = 4
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    pad = s * 0.10
    # 圆角背景
    d.rounded_rectangle([pad, pad, s - pad, s - pad],
                        radius=s * 0.18, fill=BG)

    # 纸张
    px0, py0 = s * 0.27, s * 0.22
    px1, py1 = s * 0.73, s * 0.78
    d.rounded_rectangle([px0, py0, px1, py1], radius=s * 0.05, fill=PAPER)

    # 纸上的文字行
    line_x0, line_x1 = s * 0.33, s * 0.67
    for i, ratio in enumerate((0.34, 0.42, 0.50)):
        y = s * ratio
        d.rounded_rectangle([line_x0, y, line_x1 if i != 2 else s * 0.55, y + s * 0.035],
                            radius=s * 0.018, fill=(150, 165, 185))

    # 播放三角（表示"视频"）
    cx, cy, r = s * 0.60, s * 0.66, s * 0.16
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=ACCENT)
    tri = [(cx - r * 0.30, cy - r * 0.48),
           (cx - r * 0.30, cy + r * 0.48),
           (cx + r * 0.52, cy)]
    d.polygon(tri, fill=PAPER)

    return img.resize((size, size), Image.LANCZOS)


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)

    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]

    # 关键：bitmap_format="bmp" 强制所有帧用 BMP/DIB 编码。
    #
    # 默认情况下这个 Pillow 会把【每一帧都写成 PNG】（实测 7 帧全是 PNG，连 16x16 也是），
    # 而 Windows 对图标的兼容性要求是：<= 48x48 的帧必须是 BMP/DIB，只有 256x256
    # 才允许用 PNG。小尺寸用 PNG 会让资源管理器渲染失败，回退成默认的"白色文档"图标
    # —— 实测就踩了这个坑，桌面快捷方式显示成一张白纸。
    #
    # 代价是体积变大（256x256 的 BMP 帧约 262 KB），但图标本身只存一份，可以接受；
    # 换来确定能显示，值得。
    render(256).save(OUT, format="ICO", sizes=sizes, bitmap_format="bmp")

    # 存完自查：确认小帧真的是 BMP，避免又悄悄退回白纸图标
    import struct
    raw = OUT.read_bytes()
    count = struct.unpack_from("<HHH", raw, 0)[2]
    encoded = []
    for i in range(count):
        off = 6 + i * 16
        _w, _h, _c, _r, _p, _b, _size, offset = struct.unpack_from("<BBBBHHII", raw, off)
        encoded.append("PNG" if raw[offset:offset + 4] == b"\x89PNG" else "BMP")

    print(f"已生成 {OUT}（{OUT.stat().st_size / 1024:.1f} KB，{count} 帧）")
    print(f"  各帧编码: {', '.join(encoded)}")

    # 同时输出一份同内容的 notes.ico。
    #
    # 为什么需要第二个名字：桌面快捷方式曾经一直显示白页，根因是图标缓存里存在一条
    # 以【图标文件路径 + 尺寸】为键的损坏条目（当时 icon.ico 还是 PNG 帧的坏文件，
    # 失败结果被缓存了下来；后来即使修好了文件内容，路径没变就仍然命中旧条目）。
    # 换一个新文件名就能拿到全新的缓存键。两个文件内容保持一致，
    # 免得以后只重新生成了其中一个、另一个变成过期文件。
    twin = OUT.parent / "notes.ico"
    twin.write_bytes(OUT.read_bytes())
    print(f"  同步输出 {twin.name}（内容相同，供快捷方式使用）")

    bad = [i for i, e in enumerate(encoded) if e == "PNG" and sizes[i][0] <= 48]
    if bad:
        print(f"  [警告] 第 {bad} 帧是小尺寸却用了 PNG，Windows 可能显示成白纸图标！")
        return 1
    print("  小尺寸帧均为 BMP，资源管理器可正常显示")
    return 0


if __name__ == "__main__":
    sys.exit(main())
