# -*- coding: utf-8 -*-
"""
cs61a_notes 图形界面：填入视频网址或本地路径，自动生成 PDF 笔记。

启动方式：
    双击  启动笔记工具.vbs     <- 推荐，完全不出现控制台窗口
    双击  启动笔记工具.bat     <- 等价，会闪一下命令行窗口
    或    pythonw app.py

设计要点（都是踩过坑才这么写的，别随手改）：

1. 所有子进程都带 CREATE_NO_WINDOW
   pythonw 本身没有控制台，而 Windows 上 subprocess 默认会给"控制台程序"新建
   一个控制台窗口。流水线有 5 个子步骤，不加这个标志就会连续闪 5 次黑框。

2. 子线程干活 + 队列回传日志
   tkinter 控件只能在主线程操作；子线程把日志丢进 queue，主线程用 after 轮询消费。
   否则长时间跑流水线界面会卡死不动。

3. 图片/字体等资源缺失都不致命
   图标找不到就用默认图标，PDF 列表为空就显示提示，不让界面启动失败。
"""

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import config  # noqa: E402

# 拖拽文件支持是【可选依赖】：tkinterdnd2 不是标准库。
# 没有它时整个功能自动关闭，程序照常能用（输入框 + 选择文件对话框），
# 绝不能因为一个可选功能让工具起不来。
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DND_AVAILABLE = True
except ImportError:
    DND_AVAILABLE = False

# 让子进程不弹控制台窗口
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

MAX_LOG_LINES = 1000

# 「② 生成的笔记」列表固定显示多少行。固定而不是随数量伸缩，
# 是为了让下面的日志区高度稳定、不被列表长高挤掉。
OUTPUT_LIST_ROWS = 4

# 配色（和图标同色系）
C_BRAND = "#2b4a6f"
C_BRAND_DARK = "#1f3c5c"
C_BG = "#f4f6f9"
C_CARD = "#ffffff"
C_TEXT = "#1a1a1a"
C_MUTED = "#6b7684"


def enable_dpi_awareness():
    """
    声明进程 DPI 感知。必须在创建 Tk 窗口之前调用。

    为什么必须做：这台机器是 2560x1600 物理分辨率 + Windows 175% 缩放。
    不声明 DPI 感知的话，Windows 会把整个窗口当位图拉伸 1.75 倍显示，
    结果就是【字体和图标全是模糊的】——很掉档次，小字号尤其明显。
    声明之后 Tk 按真实像素绘制，界面清晰锐利。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # 1 = SYSTEM_DPI_AWARE
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()    # 老系统没有 shcore
        except Exception:
            pass


def real_dpi() -> int:
    """取主显示器真实 DPI（175% 缩放下是 168，即 96 的 1.75 倍）。"""
    try:
        import ctypes
        hdc = ctypes.windll.user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)   # 88 = LOGPIXELSX
        ctypes.windll.user32.ReleaseDC(0, hdc)
        return int(dpi) or 96
    except Exception:
        return 96


def parse_drop_list(data: str) -> list:
    """
    解析拖放事件里的文件列表。

    【为什么不能用 root.tk.splitlist()】
    那是绝大多数 tkinterdnd2 教程推荐的做法，但它按 Tcl 列表规则解析，而 Tcl 在
    【没有花括号包裹】的元素里会处理反斜杠转义。Windows 路径全是反斜杠，于是：
        C:\\Users\\you\\video\\lecture.mp4
          -> C:Usersyou\\x0bideolecture.mp4    （\\U 被吃掉、\\v 变成垂直制表符）
        C:\\a\\1.mp4  ->  C:\\x07\\x01.mp4          （\\a 变响铃、\\1 变 \\x01）
    路径直接废掉，而且是静默的——只会表现为"拖进去没反应"。

    所以这里自己按拖放数据的格式解析，全程不做转义处理：
      - 以 { 开头：读到配对的 } 为止，内容原样保留（这样带空格的路径才对）
      - 否则：读到下一个空白为止
    两种形式都能正确处理，不依赖 tkdnd 到底加不加花括号。
    """
    items, i, n = [], 0, len(data or "")
    while i < n:
        while i < n and data[i] in " \t\r\n":
            i += 1
        if i >= n:
            break

        if data[i] == "{":
            depth, j, buf = 1, i + 1, []
            while j < n:
                ch = data[j]
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        break
                buf.append(ch)
                j += 1
            items.append("".join(buf))
            i = j + 1
        else:
            j = i
            while j < n and data[j] not in " \t\r\n":
                j += 1
            items.append(data[i:j])
            i = j

    return [item for item in items if item]


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.queue = queue.Queue()
        self.running = False
        self.worker = None
        self.pdf_path = None
        self._refresh_job = None

        root.title("课程视频 → PDF 笔记")
        root.configure(bg=C_BG)
        self._sync_tk_scaling()
        self._set_icon()
        self._setup_style()

        self._build_ui()
        # 先注册拖拽（它决定那行提示文字显不显示），再算窗口尺寸；
        # 反过来的话尺寸算完又插进一行提示，底部日志区会被挤出屏幕
        self._setup_dnd()
        self._fit_window()
        self.refresh_outputs()
        self._set_progress(False)
        self.root.after(120, self._drain_queue)

    def _sync_tk_scaling(self):
        """
        让 Tk 的"点"单位按真实 DPI 换算。

        声明 DPI 感知之后，Tk 仍以为屏幕是 96 DPI，于是 10 号字在 175% 的屏幕上
        会小得看不清。把 tk scaling 设成 dpi/72，字号就回到正确的视觉大小。
        """
        try:
            self.root.tk.call("tk", "scaling", real_dpi() / 72.0)
        except Exception:
            pass

    def _fit_window(self):
        """
        计算窗口尺寸。三个约束，缺一个都会出问题：

        1. 【按 DPI 缩放】窗口尺寸必须以 96 DPI 为基准再乘缩放因子。
           这台机器是 175% 缩放，若写死 960 宽，在实际屏幕上只占约 1/3 宽，
           字放大后每行只放得下 40 来个字符，非常局促。
        2. 【不低于布局需要的高度】否则底部的运行日志会被挤出可视区域——
           用户看不到进度和报错，等于白做。
        3. 【不超过屏幕】否则窗口底部跑到屏幕外面，同样会看不到日志。
        """
        self.root.update_idletasks()
        scale = real_dpi() / 96.0
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()

        width = int(min(920 * scale, screen_w - 60))
        # 取"布局需要的高度"和"基准高度×缩放"里更大的那个
        height = int(min(max(self.root.winfo_reqheight(), 660 * scale),
                         screen_h - 90))
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(int(700 * scale), int(480 * scale))

    def _set_progress(self, active: bool):
        """
        空闲时切成 determinate 且值为 0，进度条才是干净的空槽。
        一直挂着 indeterminate 的话，空闲状态下也会显示一块绿色填充，很难看。
        """
        if active:
            self.progress.configure(mode="indeterminate")
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self.progress["value"] = 0

    # ==================================================================
    # 外观
    # ==================================================================
    def _set_icon(self):
        icon = PROJECT_ROOT / "assets" / "icon.ico"
        try:
            if icon.exists():
                self.root.iconbitmap(default=str(icon))
            # 让任务栏用我们自己的图标，而不是 Python 的默认图标
            if sys.platform == "win32":
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                    "cs61a.notes.tool"
                )
        except Exception:
            pass

    def _setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass

        style.configure("TFrame", background=C_BG)
        style.configure("Card.TFrame", background=C_CARD, relief="flat")
        style.configure("TLabel", background=C_BG, foreground=C_TEXT,
                        font=("Microsoft YaHei UI", 10))
        style.configure("Card.TLabel", background=C_CARD, foreground=C_TEXT,
                        font=("Microsoft YaHei UI", 10))
        style.configure("Muted.TLabel", background=C_BG, foreground=C_MUTED,
                        font=("Microsoft YaHei UI", 9))
        style.configure("Section.TLabel", background=C_BG, foreground=C_BRAND_DARK,
                        font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Status.TLabel", background=C_BG, foreground=C_BRAND_DARK,
                        font=("Microsoft YaHei UI", 9))

        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 11, "bold"),
                        padding=(18, 9))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(10, 5))
        style.configure("Treeview", rowheight=26, font=("Microsoft YaHei UI", 9),
                        fieldbackground=C_CARD, background=C_CARD)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))

    # ==================================================================
    # 布局
    # ==================================================================
    def _build_ui(self):
        self._build_header()

        body = ttk.Frame(self.root, style="TFrame")
        body.pack(fill="both", expand=True, padx=16, pady=(12, 14))

        self._build_input(body)
        self._build_actions(body)
        self._build_outputs(body)
        self._build_log(body)

    def _build_header(self):
        # 不要给 header 写死像素高度！字体在 175% 缩放下会放大 2.33 倍，
        # 写死高度会把副标题裁掉。让容器按内容自动撑高即可。
        header = tk.Frame(self.root, bg=C_BRAND)
        header.pack(fill="x")

        tk.Label(header, text="课程视频 → PDF 笔记", bg=C_BRAND, fg="white",
                 font=("Microsoft YaHei UI", 15, "bold")).pack(
            anchor="w", padx=20, pady=(12, 0))
        tk.Label(header, text="填入视频网址或本地路径，自动生成带 PPT 配图、代码块与注意事项的中文笔记",
                 bg=C_BRAND, fg="#c3d3e6", font=("Microsoft YaHei UI", 9)).pack(
            anchor="w", padx=20, pady=(2, 12))

    def _build_input(self, parent):
        ttk.Label(parent, text="① 视频来源", style="Section.TLabel").pack(anchor="w")

        row = ttk.Frame(parent, style="TFrame")
        row.pack(fill="x", pady=(6, 0))

        self.source_var = tk.StringVar()
        self.entry = ttk.Entry(row, textvariable=self.source_var,
                               font=("Microsoft YaHei UI", 10))
        self.entry.pack(side="left", fill="x", expand=True, ipady=4)
        self.entry.bind("<Return>", lambda _e: self.start())

        # 占位提示（tkinter 没有原生 placeholder，用灰字模拟）
        self._placeholder = "例如  https://www.bilibili.com/video/BV...   或   D:\\视频\\lecture.mp4"
        self._show_placeholder()
        self.entry.bind("<FocusIn>", self._clear_placeholder)
        self.entry.bind("<FocusOut>", self._show_placeholder)

        # 右键菜单 + Ctrl+V。粘贴走自定义处理：占位符还在时先清掉，
        # 否则链接会被插进"例如 https://..."这行提示文字中间，变成一坨乱码。
        self._attach_menu(self.entry, editable=True, on_paste=self._paste_into_source)
        self._bind_paste_keys(self.entry, self._paste_into_source)

        self.browse_btn = ttk.Button(row, text="选择本地文件…", command=self._browse)
        self.browse_btn.pack(side="left", padx=(8, 0))

        # 拖拽提示；只有 tkinterdnd2 可用时才会填上文字（见 _setup_dnd）
        self.dnd_hint = tk.Label(parent, text="", bg=C_BG, fg=C_MUTED,
                                 font=("Microsoft YaHei UI", 8))
        self.dnd_hint.pack(anchor="w", pady=(3, 0))

    # ==================================================================
    # 右键菜单 / 剪贴板
    #
    # 为什么需要自己写：tkinter 的 ttk.Entry 和 tk.Text 【都没有内置右键菜单】。
    # Windows 上右键点输入框什么都没发生，只有 Ctrl+V 能用——而从浏览器复制
    # 视频链接后，大多数人的习惯就是右键→粘贴。这里把标准菜单补齐。
    #
    # 顺便补 Ctrl+A：Tk 在 Windows 上默认【没有】给 Entry 绑全选。
    # ==================================================================
    def _attach_menu(self, widget, editable=True, on_paste=None, extra=None):
        menu = tk.Menu(widget, tearoff=0, font=("Microsoft YaHei UI", 9))

        def gen(event):
            widget.focus_set()
            widget.event_generate(event)

        if editable:
            menu.add_command(label="剪切", accelerator="Ctrl+X",
                             command=lambda: gen("<<Cut>>"))
        menu.add_command(label="复制", accelerator="Ctrl+C",
                         command=lambda: gen("<<Copy>>"))
        if editable:
            menu.add_command(label="粘贴", accelerator="Ctrl+V",
                             command=on_paste or (lambda: gen("<<Paste>>")))
        menu.add_separator()
        menu.add_command(label="全选", accelerator="Ctrl+A",
                         command=lambda: gen("<<SelectAll>>"))
        for label, func in (extra or ()):
            menu.add_separator()
            menu.add_command(label=label, command=func)

        def popup(event):
            widget.focus_set()
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()

        widget.bind("<Button-3>", popup, add="+")

        def select_all(_event):
            widget.event_generate("<<SelectAll>>")
            return "break"

        widget.bind("<Control-a>", select_all, add="+")
        widget.bind("<Control-A>", select_all, add="+")
        # 菜单对象要保引用，否则会被 GC 掉、右键弹不出来
        widget._context_menu = menu
        return menu

    def _bind_paste_keys(self, widget, handler):
        """把 Ctrl+V 也接到自定义粘贴上，保证和右键菜单行为一致。"""
        def wrapper(_event):
            handler()
            return "break"      # 返回 break 阻止 Tk 再粘一次

        for seq in ("<Control-v>", "<Control-V>", "<Shift-Insert>"):
            widget.bind(seq, wrapper, add="+")

    def _paste_into_source(self):
        """粘到"视频来源"框：占位符还在就先清掉，并把光标放到末尾。"""
        self.entry.focus_set()
        if self.source_var.get() == self._placeholder:
            self.source_var.set("")
            self.entry.configure(foreground=C_TEXT)
        self.entry.icursor("end")
        self.entry.event_generate("<<Paste>>")

    # ==================================================================
    # 拖拽文件（可选功能，依赖 tkinterdnd2）
    # ==================================================================
    def _setup_dnd(self):
        """
        把窗口里所有控件都注册成拖放目标，做到"拖到哪儿都能放"。

        为什么要递归注册每个控件：tkdnd 只把拖放事件发给【光标正下方那个控件】，
        它没注册事件就丢了，不会往父控件冒泡。所以得整棵控件树遍历一遍。
        个别控件注册失败不影响其它。
        """
        self.dnd_enabled = False
        if not DND_AVAILABLE or not hasattr(self.root, "drop_target_register"):
            return

        registered = 0

        def walk(widget):
            nonlocal registered
            try:
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self._on_drop)
                widget.dnd_bind("<<DropEnter>>", self._on_drag_enter)
                widget.dnd_bind("<<DropLeave>>", self._on_drag_leave)
                registered += 1
            except Exception:
                pass
            for child in widget.winfo_children():
                walk(child)

        walk(self.root)
        self.dnd_enabled = registered > 0
        if self.dnd_enabled:
            self.dnd_hint.configure(text="提示：也可以把视频文件直接拖进这个窗口")

    def _on_drag_enter(self, event):
        if not self.running:
            self.status_var.set("松开鼠标即可接收该文件")
        return event.action

    def _on_drag_leave(self, event):
        if not self.running:
            self.status_var.set("就绪")
        return event.action

    def _on_drop(self, event):
        """
        处理放下文件。

        event.data 的解析见 parse_drop_list —— 千万【不要】改用
        root.tk.splitlist()，它会把 Windows 路径里的反斜杠当转义符吃掉。
        """
        raw_paths = parse_drop_list(getattr(event, "data", ""))

        files = [Path(p) for p in raw_paths if Path(p).is_file()]
        if not files:
            self.log_line("拖入的内容里没有可用的文件，已忽略。", "err")
            if raw_paths:
                self.log_line(f"  解析出的路径：{raw_paths[:3]}")
            return event.action

        videos = [p for p in files if p.suffix.lower() in config.VIDEO_EXTENSIONS]
        if not videos:
            names = "、".join(p.name for p in files[:3])
            self.log_line(f"拖入的不是支持的视频格式：{names}", "err")
            self.log_line(f"  支持的格式：{' '.join(config.VIDEO_EXTENSIONS)}")
            self.set_status("拖入的文件不是视频")
            return event.action

        chosen = videos[0]
        self.entry.configure(foreground=C_TEXT)
        self.source_var.set(str(chosen))
        if len(videos) > 1:
            self.log_line(f"一次拖入 {len(videos)} 个视频，只取第一个：{chosen.name}")
        self.log_line(f"已接收拖入的文件：{chosen}", "ok")
        self.set_status(f"已接收：{chosen.name} —— 点「开始生成」")
        return event.action        # 返回 action 表示接受这次拖放

    def _build_actions(self, parent):
        bar = ttk.Frame(parent, style="TFrame")
        bar.pack(fill="x", pady=(14, 0))

        self.start_btn = ttk.Button(bar, text="▶  开始生成", style="Primary.TButton",
                                    command=self.start)
        self.start_btn.pack(side="left")

        self.md_btn = ttk.Button(bar, text="仅把 Markdown 转 PDF",
                                 command=self.convert_existing_md)
        self.md_btn.pack(side="left", padx=(8, 0))

        ttk.Button(bar, text="打开输出目录",
                   command=lambda: self._open_path(config.OUTPUT_PATH)).pack(side="right")
        self.clear_btn = ttk.Button(bar, text="清空日志", command=self._clear_log)
        self.clear_btn.pack(side="right", padx=(0, 8))

        prog = ttk.Frame(parent, style="TFrame")
        prog.pack(fill="x", pady=(10, 0))
        self.progress = ttk.Progressbar(prog, mode="indeterminate")
        self.progress.pack(fill="x")
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(prog, textvariable=self.status_var, style="Status.TLabel").pack(
            anchor="w", pady=(4, 0))

    def _build_outputs(self, parent):
        ttk.Label(parent, text="② 生成的笔记（PDF）", style="Section.TLabel").pack(
            anchor="w", pady=(16, 0))

        card = tk.Frame(parent, bg=C_CARD, highlightbackground="#dbe2ea",
                        highlightthickness=1)
        card.pack(fill="x", pady=(6, 0))

        # 结果路径框（只读）
        path_row = tk.Frame(card, bg=C_CARD)
        path_row.pack(fill="x", padx=10, pady=(10, 6))
        tk.Label(path_row, text="PDF 路径：", bg=C_CARD, fg=C_MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(side="left")
        self.pdf_path_var = tk.StringVar(value="（还没有生成笔记）")
        self.pdf_entry = tk.Entry(path_row, textvariable=self.pdf_path_var,
                                  state="readonly", font=("Consolas", 9),
                                  readonlybackground=C_CARD, bd=0, fg=C_BRAND_DARK)
        self.pdf_entry.pack(side="left", fill="x", expand=True)
        # 只读框也要能右键复制路径（这是用户最常想做的操作）
        self._attach_menu(self.pdf_entry, editable=False,
                          extra=[("复制路径", self._copy_pdf_path)])

        self.open_pdf_btn = ttk.Button(path_row, text="打开 PDF",
                                       command=self._open_selected, state="disabled")
        self.open_pdf_btn.pack(side="left", padx=(8, 0))

        # 笔记列表
        #
        # 高度【固定】为 OUTPUT_LIST_ROWS 行，不随笔记数量变化：
        # 之前是按数量在 2~5 行之间伸缩，笔记一多列表就长高，把下面的「③ 运行日志」
        # 往上挤（日志区是 fill="both" 的，被挤的正是它）。页数超过可见行数时靠
        # 滚动条查看，布局始终稳定。
        #
        # 注意顺序：Treeview 必须在【创建时】就以 tree_row 为父容器。
        # tkinter 的父容器是创建时定的，pack(side=...) 只决定它在本容器里怎么摆、
        # 不能把它挪到别的容器里去。写成 ttk.Treeview(card, ...) 再 pack 进 tree_row
        # 是无效的——tree_row 会是空的，而列表以 side="left" 占满 card 的整个高度，
        # 后面的提示文字就只能被挤到列表右边。
        tree_row = ttk.Frame(card, style="Card.TFrame")
        tree_row.pack(fill="x", padx=10, pady=(0, 6))

        cols = ("file", "size", "pages", "time")
        self.tree = ttk.Treeview(tree_row, columns=cols, show="headings",
                                 height=OUTPUT_LIST_ROWS)
        for col, text, width, anchor in (
            ("file", "文件名", 380, "w"),
            ("size", "大小", 90, "e"),
            ("pages", "页数", 70, "e"),
            ("time", "生成时间", 150, "w"),
        ):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor)

        self.tree.pack(side="left", fill="x", expand=True)
        tree_scroll = ttk.Scrollbar(tree_row, orient="vertical",
                                    command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.pack(side="right", fill="y")

        self.tree.bind("<<TreeviewSelect>>", self._on_select_output)
        self.tree.bind("<Double-1>", lambda _e: self._open_selected())

        self.tree_tip = tk.Label(
            card, text="双击一行即可打开对应 PDF；列表按生成时间倒序",
            bg=C_CARD, fg=C_MUTED, font=("Microsoft YaHei UI", 8))
        self.tree_tip.pack(anchor="w", padx=10, pady=(0, 10))

    def _build_log(self, parent):
        ttk.Label(parent, text="③ 运行日志", style="Section.TLabel").pack(
            anchor="w", pady=(16, 0))

        wrap = tk.Frame(parent, bg=C_CARD, highlightbackground="#dbe2ea",
                        highlightthickness=1)
        wrap.pack(fill="both", expand=True, pady=(6, 0))

        self.log = tk.Text(wrap, wrap="word", height=10,
                           font=("Consolas", 9), bd=0,
                           background="#1e1e1e", foreground="#d4d4d4",
                           insertbackground="#d4d4d4", state="disabled")
        scroll = ttk.Scrollbar(wrap, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        scroll.pack(side="right", fill="y", pady=8)

        # 日志区也要能右键复制（出错时用户往往需要把日志发出来）
        self._attach_menu(self.log, editable=False,
                          extra=[("复制全部日志", self._copy_all_log),
                                 ("清空日志", self._clear_log)])

        self.log.tag_configure("err", foreground="#f48771")
        self.log.tag_configure("ok", foreground="#89d185")
        self.log.tag_configure("step", foreground="#569cd6")
        self.log.tag_configure("dim", foreground="#8a8a8a")

        self.log_line("准备好了。填入视频网址或本地路径，点「开始生成」。", "dim")
        self.log_line("一节 40 分钟的课大约需要 6~7 分钟，主要花在抽帧、转写和 OCR 上。",
                      "dim")

    # ==================================================================
    # 占位提示
    # ==================================================================
    def _show_placeholder(self, _event=None):
        if not self.source_var.get().strip():
            self.entry.configure(foreground="#9aa4b0")
            self.source_var.set(self._placeholder)

    def _clear_placeholder(self, _event=None):
        if self.source_var.get() == self._placeholder:
            self.source_var.set("")
            self.entry.configure(foreground=C_TEXT)

    def _current_source(self) -> str:
        value = self.source_var.get().strip().strip('"')
        return "" if value == self._placeholder else value

    def _copy_pdf_path(self):
        """把 PDF 路径复制到剪贴板，并把路径框里的文字选中作为视觉反馈。"""
        path = self.pdf_path_var.get()
        if not path or path.startswith("（"):
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(path)
        self.pdf_entry.selection_range(0, "end")
        self.set_status("已复制 PDF 路径到剪贴板")

    # ==================================================================
    # 日志 / 状态
    # ==================================================================
    def log_line(self, text: str, tag: str = ""):
        self.queue.put(("log", text, tag))

    def set_status(self, text: str):
        self.queue.put(("status", text, ""))

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _copy_all_log(self):
        """复制全部日志。Text 是 disabled 状态，用 get 取内容不受影响。"""
        content = self.log.get("1.0", "end-1c")
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        self.set_status(f"已复制全部日志（{len(content)} 个字符）")

    def _drain_queue(self):
        try:
            while True:
                kind, a, b = self.queue.get_nowait()
                if kind == "log":
                    self.log.configure(state="normal")
                    self.log.insert("end", a + "\n", b)
                    lines = int(self.log.index("end-1c").split(".")[0])
                    if lines > MAX_LOG_LINES:
                        self.log.delete("1.0", f"{lines - MAX_LOG_LINES}.0")
                    self.log.see("end")
                    self.log.configure(state="disabled")
                elif kind == "status":
                    self.status_var.set(a)
                elif kind == "done":
                    self._on_done(a)
                elif kind == "refresh_outputs":
                    self.refresh_outputs()
        except queue.Empty:
            pass
        self.root.after(120, self._drain_queue)

    def _on_done(self, ok: bool):
        self.running = False
        for widget, state in ((self.start_btn, "normal"),
                              (self.browse_btn, "normal"),
                              (self.md_btn, "normal")):
            widget.configure(state=state)
        self._set_progress(False)
        if ok:
            self.refresh_outputs()
            self.set_status("完成")
        else:
            self.set_status("已停止（原因见日志）")

    # ==================================================================
    # PDF 列表
    # ==================================================================
    @staticmethod
    def _pdf_pages(path: Path) -> str:
        """数 PDF 页数（数 /Type /Page 出现次数，够用且不用装 PDF 库）。"""
        try:
            import re
            raw = path.read_bytes()
            return str(len(re.findall(rb"/Type\s*/Page[^s]", raw)))
        except Exception:
            return "—"

    def refresh_outputs(self):
        for item in self.tree.get_children():
            self.tree.delete(item)

        pdfs = sorted(config.OUTPUT_PATH.glob("*_final_notes.pdf"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        for pdf in pdfs:
            stat = pdf.stat()
            self.tree.insert(
                "", "end", iid=str(pdf),
                values=(pdf.name,
                        f"{stat.st_size / 1024 / 1024:.1f} MB" if stat.st_size > 1024 * 1024
                        else f"{stat.st_size / 1024:.0f} KB",
                        self._pdf_pages(pdf),
                        __import__("datetime").datetime.fromtimestamp(
                            stat.st_mtime).strftime("%m-%d %H:%M")),
            )

        if pdfs:
            first = self.tree.get_children()[0]
            self.tree.selection_set(first)
            # 必须显式调用：selection_set() 不会同步触发 <<TreeviewSelect>>，
            # 只挂事件处理器的话路径框和「打开 PDF」按钮会一直是初始状态
            self._on_select_output()
        else:
            self.pdf_path_var.set("（还没有生成笔记）")
            self.open_pdf_btn.configure(state="disabled")
        # 高度固定，不随笔记数量变化（否则笔记一多就会把下面的日志区挤上去）。
        # 超出可见行数时用滚动条查看。
        count = len(self.tree.get_children())
        if hasattr(self, "tree_tip"):
            if count > OUTPUT_LIST_ROWS:
                self.tree_tip.configure(
                    text=f"共 {count} 份笔记，列表可滚动；双击一行即可打开对应 PDF")
            else:
                self.tree_tip.configure(
                    text="双击一行即可打开对应 PDF；列表按生成时间倒序")

    def _selected_pdf(self):
        selection = self.tree.selection()
        if not selection:
            return None
        path = Path(selection[0])
        return path if path.exists() else None

    def _on_select_output(self, _event=None):
        pdf = self._selected_pdf()
        if pdf:
            self.pdf_path_var.set(str(pdf))
            self.open_pdf_btn.configure(state="normal")

    def _open_selected(self):
        pdf = self._selected_pdf()
        if pdf:
            try:
                os.startfile(str(pdf))
            except OSError as exc:
                messagebox.showerror("打不开", f"{pdf}\n\n{exc}")

    # ==================================================================
    # 动作
    # ==================================================================
    def _browse(self):
        path = filedialog.askopenfilename(
            title="选择课程视频",
            initialdir=str(config.VIDEO_PATH if config.VIDEO_PATH.exists() else PROJECT_ROOT),
            filetypes=[("视频文件", "*.mp4 *.mkv *.flv *.avi *.mov *.webm *.ts *.m4v"),
                       ("所有文件", "*.*")])
        if path:
            self.entry.configure(foreground=C_TEXT)
            self.source_var.set(path)

    def _open_path(self, path):
        path = Path(path)
        if not path.exists():
            messagebox.showwarning("找不到", f"路径不存在：\n{path}")
            return
        try:
            os.startfile(str(path))
        except OSError as exc:
            messagebox.showerror("打不开", f"{path}\n\n{exc}")

    def convert_existing_md(self):
        path = filedialog.askopenfilename(
            title="选择要转换的 Markdown 笔记",
            initialdir=str(config.OUTPUT_PATH),
            filetypes=[("Markdown", "*.md"), ("所有文件", "*.*")])
        if not path:
            return

        def work():
            try:
                import md_to_pdf
                self.log_line("", "")
                self.log_line("=== 仅转换 Markdown → PDF ===", "step")
                pdf = md_to_pdf.convert(Path(path), log=self.log_line)
                self.pdf_path = pdf
                self.log_line(f"完成：{pdf}", "ok")
                self.queue.put(("done", True, ""))
            except Exception as exc:
                self.log_line(f"失败：{exc}", "err")
                self.queue.put(("done", False, ""))

        self._launch(work, "正在转换…")

    def start(self):
        source = self._current_source()
        if not source:
            messagebox.showwarning("还没填", "请先填视频网址或本地路径。")
            self.entry.focus_set()
            return
        self._launch(lambda: self._run_pipeline(source), "启动中…")

    def _begin(self):
        self.running = True
        self.start_btn.configure(state="disabled")
        self.browse_btn.configure(state="disabled")
        self.md_btn.configure(state="disabled")
        self.open_pdf_btn.configure(state="disabled")
        self._set_progress(True)

    def _launch(self, work, status: str):
        if self.running:
            messagebox.showinfo("正在运行", "已经有任务在跑了，请等它结束。")
            return
        self._begin()
        self.set_status(status)
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    # ==================================================================
    # 流水线（子线程）
    # ==================================================================
    def _run_pipeline(self, source: str):
        try:
            video = self._resolve_source(source)
            if video is None:
                self.queue.put(("done", False, ""))
                return

            stem = video.stem
            self.log_line(f"视频：{video.name}", "ok")

            steps = [
                (1, "step1_extract_frames.py", [video.name], "抽取关键帧"),
                (2, "step2_transcribe.py", [video.name, "--skip-existing"], "语音转写"),
                (3, "step3_ocr.py", [stem], "识别 PPT 文字"),
                (4, "step4_merge_scenes.py", [stem], "合并场景并对齐字幕"),
                (5, "step5_generate_notes.py", [stem], "生成中文笔记"),
            ]

            for number, script, args, desc in steps:
                self.set_status(f"正在执行 step{number}：{desc}…")
                self.log_line("", "")
                self.log_line(f"=== step{number}  {desc} ===", "step")
                if not self._run_script(script, args):
                    self.log_line(f"step{number} 失败，流程停止。", "err")
                    self.queue.put(("done", False, ""))
                    return

            md_path = config.OUTPUT_PATH / f"{stem}_final_notes.md"
            self.set_status("正在把 Markdown 转成 PDF…")
            self.log_line("", "")
            self.log_line("=== 生成 PDF ===", "step")
            import md_to_pdf
            pdf = md_to_pdf.convert(md_path, log=self.log_line)
            self.pdf_path = pdf
            self.log_line("", "")
            self.log_line(f"全部完成！PDF：{pdf}", "ok")
            self.queue.put(("done", True, ""))

        except Exception as exc:
            self.log_line(f"出错：{type(exc).__name__}: {exc}", "err")
            self.queue.put(("done", False, ""))

    def _env(self):
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def _run_cmd(self, cmd, collect=False):
        """
        跑一个子进程，输出逐行推到日志区。

        collect=True 时返回 (是否成功, 所有输出行)，否则只返回是否成功。
        下载视频时需要 collect：要从 yt-dlp 的输出里抠出最终文件路径。
        """
        lines = []
        # creationflags 是关键：不加的话每个子进程都会弹一个控制台黑框
        proc = subprocess.Popen(
            cmd, cwd=str(PROJECT_ROOT), env=self._env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=CREATE_NO_WINDOW)
        for raw in proc.stdout:
            line = raw.replace("\r", "").rstrip()
            if line:
                self.log_line(line)
                if collect:
                    lines.append(line)
        proc.wait()
        return (proc.returncode == 0, lines) if collect else proc.returncode == 0

    def _run_script(self, script: str, args: list) -> bool:
        return self._run_cmd([sys.executable, str(SCRIPTS_DIR / script), *args])

    # ------------------------------------------------------------------
    def _resolve_source(self, source: str):
        if source.lower().startswith(("http://", "https://")):
            return self._download(source)

        path = Path(source)
        if not path.exists():
            self.log_line(f"找不到文件：{path}", "err")
            return None
        config.VIDEO_PATH.mkdir(parents=True, exist_ok=True)
        target = config.VIDEO_PATH / path.name
        if path.resolve() != target.resolve():
            self.log_line(f"复制到项目 video/ 目录：{target.name}")
            import shutil
            shutil.copy2(path, target)
        return target

    def _download(self, url: str):
        import shutil
        if not shutil.which("yt-dlp"):
            self.log_line("没找到 yt-dlp。请先安装：pip install yt-dlp", "err")
            return None

        config.VIDEO_PATH.mkdir(parents=True, exist_ok=True)
        before = {p.name for p in config.VIDEO_PATH.iterdir() if p.is_file()}

        self.set_status("正在下载视频…")
        self.log_line(f"下载：{url}")

        # --no-simulate 必须加：--print 会隐含 --simulate，只打印不下载。
        # --print after_move:filepath 让 yt-dlp 把【最终文件路径】打到 stdout。
        #   为什么用它而不是"对比下载前后目录里多了哪个文件"：
        #   文件已经存在时 yt-dlp 会跳过下载（"has already been downloaded"），
        #   目录里不会出现新文件，旧逻辑就误判成"下载失败"并中断整个流程。
        #   实测这种跳过的情况 --print 依然会打印路径，所以这条路是可靠的。
        ok, lines = self._run_cmd([
            sys.executable, "-m", "yt_dlp",
            "-o", str(config.VIDEO_PATH / "%(title).80s.%(ext)s"),
            "--no-playlist", "--newline", "--progress",
            "--no-simulate", "--print", "after_move:filepath",
            url], collect=True)
        if not ok:
            self.log_line("下载失败。若是需要登录的站点内容，可先手动下载再用本地路径。", "err")
            return None

        video = self._pick_downloaded_video(lines, before)
        if video is None:
            self.log_line("下载命令成功了，但没能确定下载到哪个文件。", "err")
            self.log_line("  请改用本地文件路径，或手动确认 video/ 目录里的文件。", "err")
            return None
        self.log_line(f"下载完成：{video.name}", "ok")
        return video

    def _pick_downloaded_video(self, lines, before):
        """从 yt-dlp 输出里确定最终文件，三级兜底。"""
        # 1) yt-dlp 打印的路径（最可靠；文件已存在而跳过下载时也会打印）
        for line in reversed(lines):
            candidate = Path(line.strip().strip('"'))
            if candidate.is_file() and candidate.suffix.lower() in config.VIDEO_EXTENSIONS:
                return candidate

        videos = [p for p in config.VIDEO_PATH.iterdir()
                  if p.is_file() and p.suffix.lower() in config.VIDEO_EXTENSIONS]

        # 2) 目录里新增的文件
        new = [p for p in videos if p.name not in before]
        if new:
            return max(new, key=lambda p: p.stat().st_mtime)

        # 3) 最后兜底：只有当目录里【只有一个】视频时才敢猜。
        #    多个视频时猜错等于给用户跑错课程，宁可报错让他用本地路径。
        if len(videos) == 1:
            self.log_line(f"（没能从下载输出里确认文件名，按目录里唯一的视频处理：{videos[0].name}）")
            return videos[0]
        return None


def main():
    enable_dpi_awareness()        # 必须在 tk.Tk() 之前

    # 拖拽支持需要 TkinterDnD.Tk() 这个特化的 root；
    # 没装 tkinterdnd2 就退回标准 tk.Tk()，功能少一个但一切照常
    if DND_AVAILABLE:
        try:
            root = TkinterDnD.Tk()
        except Exception:
            root = tk.Tk()
    else:
        root = tk.Tk()

    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
