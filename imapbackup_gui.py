#!/usr/bin/env python3
"""IMAP Backup GUI — Tkinter GUI front-end for imapbackup312 (fork addition)

Design decisions live in docs/adr/ and CONTEXT.md; the issue trail is under
.scratch/tkinter-gui/. Key contracts:
 - Reuses imapbackup312 module-level functions (connect_and_login, get_names,
   scan_folder, scan_file, scan_eml_dir, pending_messages, download_messages);
   no core logic is copied.
 - All IMAP work runs on a worker thread; UI updates flow through a queue polled
   by Tk's after() — widgets are never touched off the main thread.
 - Per-message rows come from the structured report dict; Stop uses the
   cooperative should_stop hook (ADR 0003).
 - The core's Spinner auto-disables and nospinner=True is passed anyway: nothing
   is ever written to stdout.
 - Logs go to %LOCALAPPDATA%\\imapbackup-gui\\log\\run-YYYY-MM-DD.log (daily file;
   the whole directory is the uninstaller's cleanup target).

Original contributors (abridged): Rui Carmo and the upstream imapbackup
contributors listed in imapbackup312.py.
"""
from __future__ import annotations

__version__ = "1.6.0"  # 与 imapbackup312.__version__ 同一个号（项目统一版本，冒烟 version.sync 守卫）
__author__ = "Chen Chong"
__copyright__ = "(C) 2026 Chen Chong. Code under MIT License."

import email.utils
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter.font as tkfont

import imapbackup312 as core

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-10s | %(funcName)s:%(lineno)d | %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
LOG_FORMATTER = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)
LOG_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppDataLocal") / "imapbackup-gui" / "log"
MAX_LOG_LINES = 5000
UPTODATE_STATUS = "已是最新，无待备份邮件"
CONNECT_BTN_LABEL = "⇄ 连接并加载文件夹"
CONNECTED_BTN_LABEL = "✓ 已成功连接"
STALE_HINT_TEXT = "连接信息已更改，请重新连接并加载文件夹"
RUNNING_HINT_TEXT = "备份运行中，账户与服务器信息不可修改，请先停止"
HINT_RED = "red"       # 需要用户操作：连接信息已过期，得重新连接
HINT_GRAY = "gray"     # 备份运行中：配置锁定，先停下才能改
ZEBRA_TAG = "zebra"    # 邮件列表偶数行（第 2、4… 行）的 tag；奇数行走 Treeview 默认白底
ZEBRA_BG = "#f0f0f0"   # 不跟随系统深色模式，与运行日志 Text 的 #ffffff 同一取舍

# 文件夹树的勾选态与箭头符号（字形可用性以本机截图为准，缺字时退用 □/■/▦ 与 [-]/[+]）
CHECK_OFF = "☐"        # U+2610 未勾选
CHECK_ON = "☑"         # U+2611 已勾选
CHECK_PARTIAL = "⊟"    # U+229F 部分勾选
ARROW_OPEN = "▾"       # U+25BE 已展开
ARROW_CLOSED = "▸"     # U+25B8 已折叠
ARROW_SLOT_W = 16      # 箭头占位宽度，保证各级名称左边缘对齐
EMPTY_HINT_FG = "gray"            # 空态提示字色：只说明面板用途，不需要用户操作
FOLDER_PLACEHOLDER_TEXT = "连接成功后，这里会列出该邮箱的所有文件夹"
LIST_PLACEHOLDER_TEXT = "开始备份后，这里会列出本次备份的每封邮件"
ELLIPSIS = "…"                    # 截断省略号，与「正在连接…」同一字符
PROGRESS_MIN_WIDTH = 80           # 进度条地板宽度(px)：计数文字变长先压进度条，触底才开始截断文字
TIP_DELAY_MS = 400                # 悬停多久后弹气泡
TIP_BG = "#ffffe0"                # 气泡底色（经典 tooltip 浅黄）
TIP_WRAP = 600                    # 气泡内容最长宽度(px)，超出自动换行


class QueueLogHandler(logging.Handler):
    """Mirror INFO+ records onto the event queue for the 运行日志 tab (current session only)."""

    def __init__(self, events: queue.Queue[tuple]):
        super().__init__(level=logging.INFO)
        self.events = events
        self.setFormatter(LOG_FORMATTER)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.events.put(("log", self.format(record)))
        except Exception:  # pragma: no cover — logging must never raise into the app
            pass


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def _ellipsize(full: str, budget: int, font: tkfont.Font) -> str:
    """按像素预算截断：放得下返回原文；放不下二分最长前缀，使「前缀+…」刚好 ≤ budget。

    始终从 full 重算，调用方在行宽变大后重调即可自动恢复全文（双向动态）。
    """
    if font.measure(full) <= budget:
        return full
    lo, hi = 0, len(full)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.measure(full[:mid] + ELLIPSIS) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return full[:lo] + ELLIPSIS


def _italic_font(base: tkfont.Font) -> tkfont.Font:
    """同一字体的斜体版：Tk 没有字体克隆 API，按 family/size 重建，其余属性走默认。

    size 取绝对值——系统默认字号可能是负数（DPI 无关），Font 构造要正数。
    """
    return tkfont.Font(family=base.cget("family"), size=abs(int(base.cget("size"))), slant="italic")


def last_folder_segment(display: str, path: tuple[str, ...]) -> str:
    """最后一段显示名。eml_relpath 的段数等于真实层级深度，用它反查真实分隔符。

    不能直接按 "/" 切 display：QQ 用 "/"，Outlook 用 "."，服务器决定分隔符。
    """
    for delim in ("/", "\\", "."):
        if display.count(delim) == len(path) - 1:
            return display.rsplit(delim, 1)[-1]
    return display


class BackupApp:
    """Single-window layout: config zones on top, Notebook (邮件列表/运行日志) below, status bar at the bottom."""

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("IMAP Backup GUI")
        self.root.geometry("960x736")
        self.root.minsize(800, 620)

        self.events: queue.Queue[tuple] = queue.Queue()
        self.stop_flag = threading.Event()
        self.log = logging.getLogger("gui")
        self._setup_logging()

        # ---- state ----
        self.connect_state = "idle"  # idle | connecting | connected | stale
        self._snapshot: tuple[str, str, str] | None = None  # (server, user, password) at connect time
        self.running = False
        self.folder_rows: list[dict] = []
        self._folder_nodes: dict[tuple[str, ...], dict] = {}
        self._folder_clickables: list[tuple[tk.Widget, str]] = []
        self._port_touched = False
        self._spinner_job: int | None = None  # root.after id while the button shows a spinner
        self._config_widgets: list[tuple[tk.Widget, str]] = []
        self.server_total = 0
        self.pending_total = 0
        self.backed_count = 0
        self._row_index = 0  # 邮件列表插入序号；清表归零，斑马纹按它的奇偶决定
        self._status_full = ""              # 状态行全文；status_var 里可能是它的截断形
        self._count_full = "尚未开始备份"    # 计数行全文；count_var 同理
        self._fit_font: tkfont.Font | None = None  # 量字宽的缓存，两行标签同一默认字体
        self._tip: tk.Toplevel | None = None
        self._tip_after: str | None = None  # 待发的气泡 after id，leave 时取消

        self._build_config_area()
        self._build_run_area()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._apply_connect_state()
        self._update_start_state()

    # ------------------------------------------------------------------ logging

    def _setup_logging(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        debug = os.environ.get("IMAPBACKUP_GUI_DEBUG") == "1"
        file_handler = logging.FileHandler(LOG_DIR / f"run-{datetime.now():%Y-%m-%d}.log", encoding="utf-8")
        file_handler.setLevel(logging.DEBUG if debug else logging.INFO)
        file_handler.setFormatter(LOG_FORMATTER)
        root = logging.getLogger()
        root.handlers[:] = [file_handler, QueueLogHandler(self.events)]
        root.setLevel(logging.DEBUG if debug else logging.INFO)
        self.log.info("程序启动 v%s", __version__)

    # ------------------------------------------------------------- config area

    def _register(self, widget: tk.Widget) -> tk.Widget:
        self._config_widgets.append((widget, str(widget.cget("state"))))
        return widget

    def _build_config_area(self) -> None:
        outer = tk.Frame(self.root)
        outer.pack(fill="x", padx=8, pady=(8, 2))
        outer.columnconfigure(0, weight=1)
        outer.columnconfigure(1, weight=1)
        left = tk.Frame(outer)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self._build_conn_group(left)
        self._build_output_group(left)
        self._build_folder_group(outer)

    def _build_conn_group(self, parent: tk.Frame) -> None:
        group = tk.LabelFrame(parent, text="连接")
        group.pack(fill="x", pady=(0, 6))
        group.columnconfigure(1, weight=1)

        tk.Label(group, text="邮箱类型：").grid(row=0, column=0, sticky="e", padx=(8, 0), pady=3)
        tk.Label(group, text="IMAP（仅接收，不发送）", anchor="w").grid(row=0, column=1, columnspan=4, sticky="w", pady=3)

        tk.Label(group, text="帐号：").grid(row=1, column=0, sticky="e", padx=(8, 0))
        self.user_var = tk.StringVar()
        self.user_entry = self._register(tk.Entry(group, textvariable=self.user_var))
        self.user_entry.grid(row=1, column=1, columnspan=4, sticky="we", padx=4, pady=3)

        tk.Label(group, text="收件服务器：").grid(row=2, column=0, sticky="e", padx=(8, 0))
        self.server_var = tk.StringVar()
        self.server_entry = self._register(tk.Entry(group, textvariable=self.server_var))
        self.server_entry.grid(row=2, column=1, sticky="we", padx=4, pady=3)
        self.ssl_var = tk.BooleanVar(value=True)
        self._register(tk.Checkbutton(group, text="SSL", variable=self.ssl_var, command=self._on_ssl_toggle)).grid(row=2, column=2, sticky="w")
        tk.Label(group, text="端口：").grid(row=2, column=3, sticky="e", padx=(8, 0))
        self.port_var = tk.StringVar(value="993")
        self.port_entry = self._register(tk.Entry(group, textvariable=self.port_var, width=7))
        self.port_entry.grid(row=2, column=4, sticky="e", padx=(4, 4), pady=3)
        self.port_entry.bind("<Key>", self._on_port_key, add="+")

        tk.Label(group, text="密码：").grid(row=3, column=0, sticky="e", padx=(8, 0))
        self.password_var = tk.StringVar()
        self.password_entry = self._register(tk.Entry(group, textvariable=self.password_var, show="*"))
        self.password_entry.grid(row=3, column=1, columnspan=4, sticky="we", padx=4, pady=3)
        self.show_var = tk.BooleanVar(value=False)
        self._register(tk.Checkbutton(group, text="显示", variable=self.show_var, command=self._on_show_toggle)).grid(row=3, column=5, sticky="e", padx=(0, 8))

        self.connect_btn = self._register(tk.Button(group, text=CONNECT_BTN_LABEL, width=26, command=self._on_connect))
        self.connect_btn.grid(row=4, column=0, columnspan=6, pady=(10, 10))
        # 按需挂出：无提示时 grid_remove 让出这一行，按钮位置不变，让出的高度由下方邮件列表吸收
        self.conn_hint = tk.Label(group, text="")
        self.conn_hint.grid(row=5, column=0, columnspan=6, pady=(0, 8))
        self.conn_hint.grid_remove()

        self._bind_enter(self.user_entry, self.server_entry, self.port_entry, self.password_entry)

        for var in (self.server_var, self.user_var, self.password_var):
            var.trace_add("write", self._check_stale)

    def _build_output_group(self, parent: tk.Frame) -> None:
        group = tk.LabelFrame(parent, text="输出")
        group.pack(fill="x")
        group.columnconfigure(1, weight=1)

        tk.Label(group, text="备份保存位置：").grid(row=0, column=0, sticky="e", padx=(8, 0), pady=3)
        self.outdir_var = tk.StringVar(value=str(Path.home() / "邮箱备份"))
        self.outdir_entry = self._register(tk.Entry(group, textvariable=self.outdir_var))
        self.outdir_entry.grid(row=0, column=1, sticky="we", padx=4, pady=3)
        self._register(tk.Button(group, text="浏览…", command=self._on_browse)).grid(row=0, column=2, sticky="w", padx=(0, 8))

        fmt_row = tk.Frame(group)
        fmt_row.grid(row=1, column=0, columnspan=3, sticky="w", padx=8, pady=(2, 8))
        self.mbox_var = tk.BooleanVar(value=False)
        self.eml_var = tk.BooleanVar(value=True)
        self._register(tk.Checkbutton(fmt_row, text="mbox 输出", variable=self.mbox_var, command=self._update_start_state)).pack(side="left")
        self._register(tk.Checkbutton(fmt_row, text="eml 输出", variable=self.eml_var, command=self._update_start_state)).pack(side="left", padx=(12, 0))

        self._bind_enter(self.outdir_entry)

    def _build_folder_group(self, parent: tk.Frame) -> None:
        group = tk.LabelFrame(parent, text="邮箱文件夹")
        group.grid(row=0, column=1, sticky="nsew", padx=(0, 8))
        group.columnconfigure(0, weight=1)
        group.rowconfigure(1, weight=1)

        toolbar = tk.Frame(group)
        toolbar.grid(row=0, column=0, columnspan=2, sticky="we", padx=6, pady=(2, 0))
        self._register(tk.Button(toolbar, text="全选", width=7, command=lambda: self._set_all_folders(True))).pack(side="left")
        self._register(tk.Button(toolbar, text="清空", width=7, command=lambda: self._set_all_folders(False))).pack(side="left", padx=(6, 0))
        self._register(tk.Button(toolbar, text="仅收件箱", width=7, command=self._set_inbox_only)).pack(side="left", padx=(6, 0))

        # 用嵌套 Frame 手搭树形，不用 ttk.Treeview：Treeview 的原生 <Button-1> 把整行都当展开/折叠
        # 目标，单行放不下"点箭头=展开、点名字=勾选"两个动作。这里箭头和名称都是真控件，命中区明确。
        self.folder_canvas = tk.Canvas(group, highlightthickness=0, height=200)
        self.folder_canvas.grid(row=1, column=0, sticky="nsew", padx=(6, 0), pady=4)
        folder_scroll = ttk.Scrollbar(group, orient="vertical", command=self.folder_canvas.yview)
        folder_scroll.grid(row=1, column=1, sticky="ns", padx=(0, 6), pady=4)
        self.folder_canvas.configure(yscrollcommand=folder_scroll.set)
        self.folder_inner = tk.Frame(self.folder_canvas)
        self._folder_win = self.folder_canvas.create_window((0, 0), window=self.folder_inner, anchor="nw")
        self.folder_inner.bind("<Configure>", lambda _e: self.folder_canvas.configure(scrollregion=self.folder_canvas.bbox("all")))
        self.folder_canvas.bind("<Configure>", lambda e: self.folder_canvas.itemconfigure(self._folder_win, width=e.width))
        self.folder_canvas.bind("<MouseWheel>", self._on_folder_wheel)
        self.folder_ph = self._empty_hint(group, FOLDER_PLACEHOLDER_TEXT, 1, 0)

    # --------------------------------------------------------------- run area

    def _build_run_area(self) -> None:
        run = tk.Frame(self.root)
        run.pack(fill="both", expand=True, padx=8, pady=(2, 8))

        # 两行状态条都先于 Notebook 以 side=bottom 打包，Notebook 的 expand 才不会挤掉它们。
        # msg_bar 先打包所以落在最底。初始只显示一行（未开始备份时没有进度与状态可显示），
        # 首次开始备份时 _ensure_status_rows 放出进度条与第二行，之后不再收回。
        self._msg_bar = msg_bar = tk.Frame(run)
        msg_bar.pack(side="bottom", fill="x", pady=(4, 0))
        self._bar = bar = tk.Frame(run)
        bar.pack(side="bottom", fill="x", pady=(6, 0))
        self.notebook = ttk.Notebook(run)
        self.notebook.pack(fill="both", expand=True)

        list_tab = tk.Frame(self.notebook)
        self.notebook.add(list_tab, text="邮件列表")
        list_tab.columnconfigure(0, weight=1)
        list_tab.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(list_tab, columns=("from", "subject", "when", "folder", "size"), show="headings")
        self.tree.heading("from", text="发件人")
        self.tree.heading("subject", text="主题")
        self.tree.heading("when", text="时间")
        self.tree.heading("folder", text="邮箱文件夹")
        self.tree.heading("size", text="大小")
        self.tree.column("from", width=200, stretch=False)
        self.tree.column("subject", width=320, stretch=True)
        self.tree.column("when", width=110, stretch=False)
        self.tree.column("folder", width=140, stretch=False)
        self.tree.column("size", width=90, anchor="e", stretch=False)
        self.tree.tag_configure(ZEBRA_TAG, background=ZEBRA_BG)
        tree_scroll = ttk.Scrollbar(list_tab, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        tree_scroll.grid(row=0, column=1, sticky="ns")
        self.list_ph = self._empty_hint(list_tab, LIST_PLACEHOLDER_TEXT, 0, 0)

        log_tab = tk.Frame(self.notebook)
        self.notebook.add(log_tab, text="运行日志")
        log_tab.columnconfigure(0, weight=1)
        log_tab.rowconfigure(1, weight=1)
        log_toolbar = tk.Frame(log_tab)
        log_toolbar.grid(row=0, column=0, columnspan=2, sticky="we", padx=4, pady=2)
        tk.Button(log_toolbar, text="打开日志文件夹", command=self._on_open_log_dir).pack(side="left")
        self.log_text = tk.Text(log_tab, state="disabled", wrap="none", background="#ffffff")
        self.log_text.grid(row=1, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_tab, orient="vertical", command=self.log_text.yview)
        log_scroll.grid(row=1, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scroll.set)

        # 按钮 side=right 钉在右端：进度条隐藏时它们也在右边，放出后位置纹丝不动
        self.count_var = tk.StringVar(value=self._count_full)
        self.count_label = tk.Label(bar, textvariable=self.count_var, anchor="w")
        self.count_label.pack(side="left")
        self.stop_btn = tk.Button(bar, text="停止", width=6, state="disabled", command=self._on_stop)
        self.stop_btn.pack(side="right")
        self.start_btn = tk.Button(bar, text="开始备份", width=10, command=self._on_start)
        self.start_btn.pack(side="right", padx=(0, 6))
        # 进度条是这一行唯一可伸缩的控件：文字变长它先缩水，触及地板宽度后文字才截断
        self.progress = ttk.Progressbar(bar, orient="horizontal", length=220, mode="determinate", maximum=1)
        self.progress.pack(side="left", fill="x", expand=True, padx=(16, 8))

        # 版本号先打包占住右端一条：pack 从不动先打包的自然宽度控件，状态文本只能自己截断
        # 斜体 + 灰字，跟正文区分开但不抢注意力
        self.status_var = tk.StringVar(value="")
        self.version_label = tk.Label(msg_bar, text=f"v{__version__}", foreground=EMPTY_HINT_FG,
                                      font=_italic_font(tkfont.nametofont("TkDefaultFont")))
        self.version_label.pack(side="right")
        self.status_label = tk.Label(msg_bar, textvariable=self.status_var, anchor="w")
        self.status_label.pack(side="left", fill="x", expand=True)

        # 计数标签不 expand、宽度不跟窗口走，<Configure> 得绑行容器；状态标签 expand，绑它自身
        bar.bind("<Configure>", lambda _e: self._fit_counts())
        self.status_label.bind("<Configure>", lambda _e: self._fit_status())
        self._bind_tip(self.count_label, self.count_var, lambda: self._count_full)
        self._bind_tip(self.status_label, self.status_var, lambda: self._status_full)

        # 初始单行：进度条与第二行在首次开始备份时才出现
        self.progress.pack_forget()
        msg_bar.pack_forget()

    # ------------------------------------------------------- connect handlers

    def _conn_values(self) -> dict | None:
        server = self.server_var.get().strip()
        user = self.user_var.get().strip()
        password = self.password_var.get()
        if not server:
            messagebox.showwarning("信息不完整", "请填写收件服务器。", parent=self.root)
            return None
        if not user:
            messagebox.showwarning("信息不完整", "请填写帐号。", parent=self.root)
            return None
        if not password:
            messagebox.showwarning("信息不完整", "请填写密码。", parent=self.root)
            return None
        try:
            port = int(self.port_var.get().strip())
            if not 0 < port < 65536:
                raise ValueError
        except ValueError:
            messagebox.showwarning("端口无效", "端口必须是 1-65535 的整数。", parent=self.root)
            return None
        return {"server": server, "user": user, "password": password,
                "ssl": bool(self.ssl_var.get()), "port": port,
                "mbox": bool(self.mbox_var.get()), "eml": bool(self.eml_var.get()),
                "outdir": self.outdir_var.get().strip()}

    def _on_connect(self) -> None:
        if str(self.connect_btn.cget("state")) != "normal":
            # 连接中/已连接/备份运行时按钮被禁用，输入框在连接中并未禁用——
            # 门控不挡住的话，连接中再按一次 Enter 会起第二条连接线程。
            return
        v = self._conn_values()
        if v is None:
            return
        self.connect_state = "connecting"
        self._apply_connect_state()
        self._update_start_state()
        threading.Thread(target=self._connect_worker, args=(v,), daemon=True).start()

    def _core_config(self, v: dict, basedir: Path | None = None, eml_dir: Path | None = None) -> core.Config:
        return core.Config(overwrite=False, usessl=v["ssl"], thunderbird=False, nospinner=True,
                           basedir=basedir, icloud=False, quiet=False, user=v["user"], server=v["server"],
                           password=v["password"], timeout=60, port=v["port"], eml_dir=eml_dir)

    def _connect_worker(self, v: dict) -> None:
        log = logging.getLogger("imapbackup")
        cfg = self._core_config(v)
        try:
            server = core.connect_and_login(cfg, log)
            try:
                names = core.get_names(server, False, True, quiet=False, log=log)
            finally:
                try:
                    server.logout()
                except Exception:
                    pass
            self.events.put(("folders_loaded", (names, v)))
        except SystemExit as e:
            self.events.put(("connect_failed", str(e)))
        except Exception as e:
            log.exception("连接失败")
            self.events.put(("connect_failed", f"{type(e).__name__}: {e}"))

    def _on_folders_loaded(self, names: list[tuple], v: dict) -> None:
        self._rebuild_folders(names)
        self._snapshot = (v["server"], v["user"], v["password"])
        self.connect_state = "connected"
        self.log.info("已加载 %d 个邮箱文件夹", len(names))
        self._check_stale()  # user may have edited fields while connecting
        self._update_start_state()

    def _rebuild_folders(self, names: list[tuple]) -> None:
        checked = ({r["foldername"] for r in self.folder_rows if r["var"].get()}
                   if self.folder_rows else {"INBOX"})
        open_keys = None
        if self._folder_nodes:
            open_keys = {key for key, node in self._folder_nodes.items() if node["open"]}
        for child in self.folder_inner.winfo_children():
            child.destroy()
        self.folder_rows, self._folder_nodes, self._folder_clickables = [], {}, []

        rows: list[dict] = []
        for foldername, filename, eml_relpath, display in names:
            path = tuple(eml_relpath.split("/"))
            rows.append({"foldername": foldername, "filename": filename,
                         "eml_relpath": eml_relpath, "display": display, "path": path,
                         "label": last_folder_segment(display, path),
                         "var": tk.BooleanVar(value=foldername in checked),
                         "check_btn": None})

        real: dict[tuple, list[dict]] = {}
        for row in rows:
            real.setdefault(row["path"], []).append(row)

        keys = sorted({row["path"][:i] for row in rows for i in range(1, len(row["path"]) + 1)},
                      key=lambda k: (len(k), k))
        with_kids = {k for k in keys if any(o != k and o[:len(k)] == k for o in keys)}
        for key in keys:  # 父节点必然先于子节点建成
            self._make_folder_node(key, real.get(key), with_kids, open_keys)

        self.folder_rows.sort(key=lambda r: r["path"])
        self._render_folder_checks()
        # 重建会铺满树区，空态提示得跟着收掉；一个文件夹都没解析出来时重新挂回
        self._toggle_hint(self.folder_ph, not self.folder_rows)

    def _make_folder_node(self, key: tuple[str, ...], rows: list[dict] | None,
                          with_kids: set[tuple], open_keys: set[tuple] | None) -> None:
        open_now = True if open_keys is None else key in open_keys
        # rows 可能多于一条：两个不同文件夹名消毒后落到同一路径时共享一行，
        # 但仍按 foldername 各自进备份清单（输出路径本就相同）。
        node = {"key": key, "label": key[-1], "open": open_now,
                "rows": rows or [], "arrow": None}
        self._folder_nodes[key] = node
        parent = self._folder_nodes.get(key[:-1])
        host = parent["kids"] if parent is not None else self.folder_inner

        frame = tk.Frame(host)
        frame.pack(anchor="w", fill="x", pady=1)
        node["frame"] = frame

        row = tk.Frame(frame)
        row.pack(anchor="w")
        slot = tk.Frame(row)  # 固定宽度的箭头槽，让各级名称左边缘对齐
        slot.pack(side="left")
        slot.pack_propagate(False)
        slot.configure(width=ARROW_SLOT_W)
        if key in with_kids:
            arrow = tk.Label(slot, text=ARROW_OPEN if open_now else ARROW_CLOSED, cursor="hand2")
            arrow.pack(side="left")
            arrow.bind("<Button-1>", lambda _e, _k=key: self._toggle_folder_open(_k))
            node["arrow"] = arrow
            self._folder_clickables.append((arrow, str(arrow.cget("foreground"))))

        if rows:
            btn = tk.Label(row, text=key[-1], cursor="hand2")
            btn.pack(side="left")
            btn.bind("<Button-1>", lambda _e, _k=key: self._on_folder_check(_k))
            self._folder_clickables.append((btn, str(btn.cget("foreground"))))
            for r in rows:
                self.folder_rows.append(r)
                r["check_btn"] = btn
        else:
            tk.Label(row, text=key[-1]).pack(side="left")

        node["kids"] = tk.Frame(frame)
        self._pack_kids(node)

    def _pack_kids(self, node: dict) -> None:
        """展开挂出子文件夹框、折叠收起；控件不销毁，勾选与展开状态都留在内存里。"""
        if node["open"]:
            node["kids"].pack(anchor="w", fill="x", padx=(ARROW_SLOT_W, 0))
        else:
            node["kids"].pack_forget()

    def _empty_hint(self, parent: tk.Frame, text: str, row: int, column: int) -> tk.Label:
        """空态提示：与内容控件占同一格居中叠放，内容出现后用 _toggle_hint 收掉。"""
        label = tk.Label(parent, text=text, foreground=EMPTY_HINT_FG)
        label.grid(row=row, column=column, padx=8, pady=6)
        return label

    def _toggle_hint(self, hint: tk.Label, show: bool) -> None:
        hint.grid_remove()
        if show:
            hint.grid()
            hint.tkraise()

    def _descendant_rows(self, key: tuple[str, ...]) -> list[dict]:
        """严格后代的行（不含该节点自己）；合成父节点没有可备份的文件夹。"""
        return [r for k, node in self._folder_nodes.items()
                if len(k) > len(key) and k[:len(key)] == key for r in node["rows"]]

    def _folder_subtree(self, key: tuple[str, ...]) -> list[dict]:
        """该节点自身及其全部后代的行。"""
        node = self._folder_nodes.get(key)
        own = node["rows"] if node is not None else []
        return own + self._descendant_rows(key)

    def _on_folder_check(self, key: tuple[str, ...]) -> None:
        if self.running:
            return  # 运行中配置区锁定；标签只能置灰，点击要靠这里挡住
        subtree = self._folder_subtree(key)
        turning_on = not any(r["var"].get() for r in subtree)
        for r in subtree:
            r["var"].set(turning_on)
        # 有子文件夹的节点自身也是真实文件夹。勾它下面的文件夹时它本身一并进备份清单，
        # 否则父级会一直停在 ⊟，看着像"没勾上"；取消时下面还有勾选就保留它，
        # 这样父级符号只反映子文件夹：全勾 ☑、全不勾 ☐、混合 ⊟。
        for i in range(1, len(key)):
            anc = self._folder_nodes.get(key[:i])
            if anc is None or not anc["rows"]:
                continue
            keep = any(r["var"].get() for r in self._descendant_rows(key[:i]))
            for r in anc["rows"]:
                r["var"].set(turning_on or keep)
        self._render_folder_checks()
        self._update_start_state()

    def _toggle_folder_open(self, key: tuple[str, ...]) -> None:
        if self.running:
            return
        node = self._folder_nodes[key]
        node["open"] = not node["open"]
        self._pack_kids(node)
        node["arrow"].configure(text=ARROW_OPEN if node["open"] else ARROW_CLOSED)

    def _render_folder_checks(self) -> None:
        for node in self._folder_nodes.values():
            if not node["rows"]:
                continue
            states = [r["var"].get() for r in self._folder_subtree(node["key"])]
            if all(states):
                glyph = CHECK_ON
            elif any(states):
                glyph = CHECK_PARTIAL
            else:
                glyph = CHECK_OFF
            text = f"{glyph} {node['label']}"
            for r in node["rows"]:
                r["check_btn"].configure(text=text)

    # ------------------------------------------------------- stale state rules

    def _check_stale(self, *_args) -> None:
        if self.running or self._snapshot is None or self.connect_state not in ("connected", "stale"):
            return
        cur = (self.server_var.get(), self.user_var.get(), self.password_var.get())
        self.connect_state = "connected" if cur == self._snapshot else "stale"
        self._apply_connect_state()
        self._update_start_state()

    def _show_conn_hint(self, text: str, color: str) -> None:
        """显示提示并上色；空文案时整行让出空间，避免无提示时留一条空带。

        用 winfo_manager 判断是否已挂出，不用 winfo_ismapped：后者反映显示端映射态，
        比布局管理器滞后，刚 grid_remove 后仍报 True，会让该显示的提示被跳过。
        """
        if text:
            self.conn_hint.configure(text=text, foreground=color)
            if self.conn_hint.winfo_manager() == "":
                self.conn_hint.grid()
        else:
            # 不可见时把文本清掉：标签只是不挂出，内容仍在，将来误挂出会闪出旧提示
            self.conn_hint.configure(text="")
            self.conn_hint.grid_remove()

    def _start_button_spinner(self) -> None:
        """连接中的按钮图标用盲文点阵转圈（U+2800 段，Windows 字体普遍有字形）。"""
        frames = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

        def tick(i: int = 0) -> None:
            if self.connect_state != "connecting":
                self._spinner_job = None
                return
            self.connect_btn.configure(text=f"{frames[i % len(frames)]} 正在连接…")
            self._spinner_job = self.root.after(150, tick, i + 1)

        self.connect_btn.configure(text=f"{frames[0]} 正在连接…", state="disabled")
        self._spinner_job = self.root.after(150, tick, 1)

    def _apply_connect_state(self) -> None:
        if self.connect_state == "connecting":
            if self._spinner_job is None:
                self._start_button_spinner()
        else:
            self._spinner_job = None
            if self.running:
                # 运行中配置区整体锁定，connect_btn 已被 _set_config_enabled(False) 禁用
                self.connect_btn.configure(text=CONNECT_BTN_LABEL)
                self._show_conn_hint(RUNNING_HINT_TEXT, HINT_GRAY)
            elif self.connect_state == "connected":
                self.connect_btn.configure(text=CONNECTED_BTN_LABEL, state="disabled")
                self._show_conn_hint("", HINT_GRAY)
            else:  # idle / stale — 同一控件，stale 额外加一行提示
                # 必须恢复 normal：运行中 _set_config_enabled(False) 禁用的按钮在结束后要能重新点击
                self.connect_btn.configure(text=CONNECT_BTN_LABEL, state="normal")
                if self.connect_state == "stale":
                    self._show_conn_hint(STALE_HINT_TEXT, HINT_RED)
                else:
                    self._show_conn_hint("", HINT_GRAY)

    # --------------------------------------------------------- misc handlers

    def _on_ssl_toggle(self) -> None:
        if not self._port_touched:
            self.port_var.set("993" if self.ssl_var.get() else "143")

    def _on_port_key(self, _event) -> str:
        self._port_touched = True
        return None  # continue normal binding

    def _on_show_toggle(self) -> None:
        self.password_entry.configure(show="" if self.show_var.get() else "*")

    def _bind_enter(self, *entries: tk.Entry) -> None:
        """连接相关的输入框里 Enter = 点连接按钮；小键盘的 Enter 是独立事件名 KP_Enter。"""
        for entry in entries:
            for key in ("<Return>", "<KP_Enter>"):
                entry.bind(key, self._on_return)

    def _on_return(self, _event) -> None:
        self._on_connect()

    def _on_browse(self) -> None:
        initial = self.outdir_var.get().strip() or str(Path.home())
        chosen = filedialog.askdirectory(parent=self.root, title="选择备份保存位置", initialdir=initial)
        if chosen:
            self.outdir_var.set(Path(chosen).as_posix())

    def _set_all_folders(self, value: bool) -> None:
        for row in self.folder_rows:
            row["var"].set(value)
        if value:
            for node in self._folder_nodes.values():
                if not node["open"]:
                    self._toggle_folder_open(node["key"])
        self._render_folder_checks()
        self._update_start_state()

    def _set_inbox_only(self) -> None:
        for row in self.folder_rows:
            row["var"].set(row["foldername"] == "INBOX")
        self._render_folder_checks()
        self._update_start_state()

    def _on_folder_wheel(self, event) -> None:
        self.folder_canvas.yview_scroll(int(-event.delta / 120), "units")

    def _on_open_log_dir(self) -> None:
        try:
            os.startfile(str(LOG_DIR))  # type: ignore[attr-defined]
        except Exception as e:
            messagebox.showerror("无法打开", f"日志文件夹打开失败：{e}", parent=self.root)

    # ------------------------------------------------------------- run control

    def _update_start_state(self, *_args) -> None:
        ok = (self.connect_state == "connected"
              and any(r["var"].get() for r in self.folder_rows)
              and (self.mbox_var.get() or self.eml_var.get())
              and not self.running)
        self.start_btn.configure(state="normal" if ok else "disabled")

    def _set_config_enabled(self, enabled: bool) -> None:
        for widget, default_state in self._config_widgets:
            try:
                widget.configure(state=default_state if enabled else "disabled")
            except tk.TclError:
                pass
        # 树里是 tk.Label：它有 -state 选项，但置灰和阻断绑定都不生效，
        # 所以靠前景色 + 光标提示不可点，真正的拦截在两个 handler 的 running 判定里。
        # 默认前景色是 Tk 的系统色名（如 SystemButtonText），"" 不是合法颜色名，须按控件记录还原。
        for w, default_fg in self._folder_clickables:
            w.configure(foreground=default_fg if enabled else "gray",
                        cursor="hand2" if enabled else "arrow")

    def _ensure_status_rows(self) -> None:
        """首次开始备份时放出进度条和第二行状态条；放出后不再收回。

        结束/停止后保持两行：完成与停止消息需要第二行显示。before= 保持
        msg_bar 居最底、进度条排在计数与按钮之间，与初始打包顺序一致。
        """
        if self._msg_bar.winfo_manager():
            return
        self.progress.pack(side="left", fill="x", expand=True, padx=(16, 8), before=self.start_btn)
        self._msg_bar.pack(side="bottom", fill="x", pady=(4, 0), before=self._bar)
        self._fit_counts()
        self._fit_status()

    def _begin_run_ui(self) -> None:
        """开始备份的界面侧（调用前 self.running 已置真）：锁配置区、亮出"先停止才能改"提示、切到列表。

        必须调 _apply_connect_state：运行中那条提示只在连接状态机里输出，
        只锁控件不刷状态机的话，用户会以为可以照改配置。
        """
        self._ensure_status_rows()
        self._set_config_enabled(False)
        self._apply_connect_state()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.notebook.select(0)

    def _on_start(self) -> None:
        v = self._conn_values()
        if v is None:
            return
        if not v["outdir"]:
            messagebox.showwarning("信息不完整", "请选择备份保存位置。", parent=self.root)
            return
        selected = [(r["foldername"], r["filename"], r["eml_relpath"], r["display"])
                    for r in self.folder_rows if r["var"].get()]
        if not selected:
            return
        self.running = True
        self.stop_flag.clear()
        self.server_total = self.pending_total = self.backed_count = 0
        self._clear_message_rows()
        self.progress.configure(maximum=1, value=0)
        self._set_counts("正在统计…")
        self._set_status("正在连接…", error=False)
        self._begin_run_ui()
        self.log.info("开始备份运行：%d 个邮箱文件夹，输出 %s（mbox=%s eml=%s）",
                      len(selected), v["outdir"], v["mbox"], v["eml"])
        threading.Thread(target=self._backup_worker, args=(v, selected), daemon=True).start()

    def _backup_worker(self, v: dict, selected: list[tuple]) -> None:
        log = logging.getLogger("imapbackup")
        t0 = time.monotonic()
        eq = self.events
        backed = 0

        def report(d: dict) -> None:
            nonlocal backed
            backed += 1
            self.log.debug("已备份 %s：%s", d["folder"], d["msg_id"])
            eq.put(("message", d))

        try:
            root_dir = Path(v["outdir"])
            basedir = root_dir if v["mbox"] else None
            eml_root = root_dir if v["eml"] else None
            cfg = self._core_config(v, basedir, eml_root)
            server = core.connect_and_login(cfg, log)
            account = core.sanitize_segment(v["user"], core.EML_SEGMENT_MAX)

            # pass 1: enumerate + incremental scan, so 待备份 total is known before any download
            plans: list[tuple] = []
            server_total = 0
            pending_total = 0
            for i, n in enumerate(selected, 1):
                if self.stop_flag.is_set():
                    break
                foldername, filename, eml_relpath, display = n
                eq.put(("phase", f"扫描 {i}/{len(selected)}：{display}"))
                try:
                    remote = core.scan_folder(server, foldername, True, quiet=False, log=log, display=display)
                except core.SkipFolderException as e:
                    self.log.warning("%s：跳过（%s）", display, e)
                    continue
                # 两种格式各自判重：任一已选格式缺这封就算待备份，写盘时只补缺的那一份。
                local_mbox: dict[str, str] | None = None
                if basedir is not None:
                    local_mbox = core.scan_file(f"{account}/{filename}", False, True, basedir, quiet=False, log=log)
                local_eml: dict[str, str] | None = None
                if eml_root is not None:
                    local_eml = core.scan_eml_dir(eml_root / account / eml_relpath, False, True, quiet=False, log=log)
                new = core.pending_messages(remote, local_mbox, local_eml)
                server_total += len(remote)
                pending_total += len(new)
                plans.append((n, new, local_mbox, local_eml))
            eq.put(("totals", server_total, pending_total))

            # pass 2: download; folders were re-selectable because scan_folder moved the selection each time
            if pending_total > 0 and not self.stop_flag.is_set():
                if basedir is not None:
                    core.ensure_basedir(basedir)
                    core.create_folder_structure([p[0] for p in plans], basedir, account)
                for n, new, local_mbox, local_eml in plans:
                    if self.stop_flag.is_set():
                        break
                    foldername, filename, eml_relpath, display = n
                    if not new:
                        continue
                    eq.put(("phase", f"备份 {display}（{len(new)} 封）"))
                    typ, data = server.select(f'"{foldername}"', readonly=True)
                    if typ != "OK":
                        self.log.warning("%s：SELECT 失败 %s", display, data)
                        continue
                    label = f"{account}/{filename}" if basedir is not None else f"{account}/{eml_relpath}"
                    eml_folder_dir = eml_root / account / eml_relpath if eml_root is not None else None
                    core.download_messages(server, label, new, False, True, False,
                                           basedir, False, quiet=False, log=log,
                                           eml_dir=eml_folder_dir, skip_mbox=local_mbox, skip_eml=local_eml,
                                           foldername=display,
                                           report=report, should_stop=self.stop_flag.is_set)
            try:
                server.logout()
            except Exception:
                pass
            seconds = time.monotonic() - t0
            eq.put(("run_done", backed, server_total - pending_total, seconds,
                    self.stop_flag.is_set(), pending_total))
        except SystemExit as e:
            eq.put(("run_failed", str(e)))
        except Exception as e:
            self.log.exception("备份运行失败")
            eq.put(("run_failed", f"{type(e).__name__}: {e}"))

    def _on_stop(self) -> None:
        self.stop_flag.set()
        self.stop_btn.configure(state="disabled")
        self._set_status("正在停止…", error=False)

    # ------------------------------------------------------------ event pump

    def _pump(self) -> None:
        try:
            while True:
                ev = self.events.get_nowait()
                self._handle_event(ev)
        except queue.Empty:
            pass
        self.root.after(100, self._pump)

    def _handle_event(self, ev: tuple) -> None:
        kind = ev[0]
        if kind == "log":
            self._append_log(ev[1])
        elif kind == "folders_loaded":
            self._on_folders_loaded(*ev[1])
        elif kind == "connect_failed":
            self.connect_state = "idle"
            self._apply_connect_state()
            self._update_start_state()
            messagebox.showerror("连接失败", ev[1], parent=self.root)
        elif kind == "phase":
            self._set_status(ev[1], error=False)
        elif kind == "totals":
            self.server_total, self.pending_total = ev[1], ev[2]
            self.progress.configure(maximum=max(self.pending_total, 1), value=0)
            self._refresh_counts()
            if self.pending_total == 0:
                self._set_status(UPTODATE_STATUS, error=False)
        elif kind == "message":
            self._add_message_row(ev[1])
        elif kind == "run_done":
            self._finish_run(*ev[1:])
        elif kind == "run_failed":
            self._end_run_controls()
            self._set_status("备份失败，详见运行日志", error=True)
            messagebox.showerror("备份失败", ev[1], parent=self.root)

    def _end_run_controls(self) -> None:
        self.running = False
        # 先解锁配置区再走连接状态机：状态机可能重新禁用连接按钮（过期连接），顺序反了会被解锁覆盖回去
        self._set_config_enabled(True)
        self._apply_connect_state()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="disabled")

    def _finish_run(self, backed: int, skipped: int, seconds: float, stopped: bool, pending: int) -> None:
        self._end_run_controls()
        if stopped:
            self._set_status(f"已停止：本次备份 {backed} 封", error=False)
        elif pending == 0:
            self._set_status(UPTODATE_STATUS, error=False)
        else:
            self.progress.configure(value=self.progress["maximum"])
            self._set_status(f"完成：本次备份 {backed} 封，跳过 {skipped} 封（本地已存在），用时 {_fmt_duration(seconds)}",
                             error=False)
        self.log.info("备份运行结束：备份 %d 封，跳过 %d 封，用时 %s%s", backed, skipped, _fmt_duration(seconds),
                      "（用户停止）" if stopped else "")
        self._update_start_state()

    def _set_status(self, text: str, error: bool) -> None:
        self._status_full = text
        self.status_label.configure(foreground="red" if error else "black")
        self._fit_status()

    def _set_counts(self, text: str) -> None:
        self._count_full = text
        self._fit_counts()

    def _fit_counts(self) -> None:
        """计数文案与进度条的退让：文字变长先缩进度条，触底 PROGRESS_MIN_WIDTH 后文字才截断。

        不能靠 pack 的亏空裁剪让进度条变小——控件总请求超过行宽时 expand 控件收缩、
        后打包的按钮会叠进它的残 parcel 里（实测叠影）。所以进度条用 length 主动改请求，
        保证整行请求宽度始终 ≤ 行宽：pack 无亏空，按钮永不受损。
        """
        width = self._bar.winfo_width()
        if width < 20:  # 未映射时宽度是 1，等真实 <Configure> 再算
            self.count_var.set(self._count_full)
            return
        fixed = self.start_btn.winfo_reqwidth() + self.stop_btn.winfo_reqwidth() + 30  # 进度条 padx 24 + 按钮间距 6
        # 先放全文拿标签的真实请求宽（font.measure 与 reqwidth 差一个固定边距，估算不可靠）
        self.count_var.set(self._count_full)
        natural = self.count_label.winfo_reqwidth()
        spare = width - fixed - natural  # 全文显示时进度条能分到的宽度
        if spare >= 220:
            self.progress.configure(length=220)
        elif spare >= PROGRESS_MIN_WIDTH:
            self.progress.configure(length=max(spare, 1))  # 缩水阶段：文字照旧全文
        else:
            self.progress.configure(length=PROGRESS_MIN_WIDTH)  # 触底：文字开始截断
            self._shrink_to_fit(self.count_label, self.count_var, self._count_full,
                                width - fixed - PROGRESS_MIN_WIDTH)
        self._hide_tip()

    def _fit_status(self) -> None:
        """状态文案按标签实得宽度动态截断；行变宽后从全文重算，省略号自动消失。"""
        width = self.status_label.winfo_width()
        if width < 20:
            self.status_var.set(self._status_full)
            return
        self.status_var.set(self._status_full)  # 先放全文拿真实请求宽；同一回调内改回，不上屏
        if self.status_label.winfo_reqwidth() > width:
            self._shrink_to_fit(self.status_label, self.status_var, self._status_full, width)
        self._hide_tip()

    def _shrink_to_fit(self, label: tk.Label, var: tk.StringVar, full: str, budget: int) -> None:
        """把 full 截到标签请求宽 ≤ budget（像素）。font.measure 与标签请求宽差一个固定
        边距（本机约 6px），先留 8px 余量砍一刀，再按真实请求宽补一刀收口。"""
        font = self._text_font()
        var.set(_ellipsize(full, budget - 8, font))
        over = label.winfo_reqwidth() - budget
        if over > 0:
            var.set(_ellipsize(full, budget - 8 - over, font))

    def _text_font(self) -> tkfont.Font:
        """两行标签同为默认字体，缓存一份用来量像素宽。"""
        if self._fit_font is None:
            self._fit_font = tkfont.nametofont(str(self.status_label.cget("font")) or "TkDefaultFont")
        return self._fit_font

    # ------------------------------------------------------------- tooltip

    def _bind_tip(self, label: tk.Label, var: tk.StringVar, get_full) -> None:
        label.bind("<Enter>", lambda e: self._on_tip_enter(e, var, get_full))
        label.bind("<Leave>", lambda _e: self._on_tip_leave())

    def _on_tip_enter(self, event, var: tk.StringVar, get_full) -> None:
        """只在文本被截断时调度气泡：全文本就完整显示，悬停不弹。"""
        if var.get() == get_full():
            return
        x, y = event.x_root, event.y_root
        self._tip_after = self.root.after(TIP_DELAY_MS, lambda: self._show_tip(x, y, get_full()))

    def _on_tip_leave(self) -> None:
        if self._tip_after is not None:
            self.root.after_cancel(self._tip_after)
            self._tip_after = None
        self._hide_tip()

    def _show_tip(self, x: int, y: int, text: str) -> None:
        """无边框气泡显示被截断的全文；x 贴屏幕右缘收拢，避免超出屏幕。"""
        self._tip_after = None
        self._hide_tip()
        tip = tk.Toplevel(self.root)
        tip.wm_overrideredirect(True)
        tip.attributes("-topmost", True)
        tk.Label(tip, text=text, background=TIP_BG, relief="solid", borderwidth=1,
                 padx=6, pady=3, justify="left", wraplength=TIP_WRAP).pack()
        tip.update_idletasks()
        x = min(x + 8, tip.winfo_screenwidth() - tip.winfo_reqwidth() - 8)
        tip.wm_geometry(f"+{max(x, 0)}+{y + 14}")
        self._tip = tip

    def _hide_tip(self) -> None:
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None

    def _refresh_counts(self) -> None:
        self._set_counts(f"服务器共 {self.server_total} ｜ 待备份 {self.pending_total} ｜ 已备份 {self.backed_count} / {self.pending_total}")
        self.progress.configure(value=self.backed_count)

    def _clear_message_rows(self) -> None:
        """清掉上一轮的邮件行；序号归零，斑马纹从第 1 行重新起算，空态提示重新挂出。"""
        if children := self.tree.get_children():
            self.tree.delete(*children)
        self._row_index = 0
        self._toggle_hint(self.list_ph, True)

    def _add_message_row(self, d: dict) -> None:
        dt: datetime = d["timestamp"]
        ts_fmt = "%m-%d %H:%M" if dt.year == datetime.now().year else "%Y-%m-%d %H:%M"
        sender_raw = core.decode_mime_words(d["from"]) if d["from"] else ""
        name, addr = email.utils.parseaddr(sender_raw)
        sender = name or addr or "?"
        subject = core.decode_mime_words(d["subject"]) if d["subject"] else ""
        # 行都是 append 到 "end"，插入顺序即显示顺序，按序号奇偶就是按视觉行号
        self._row_index += 1
        if self._row_index == 1:
            self._toggle_hint(self.list_ph, False)
        tags = (ZEBRA_TAG,) if self._row_index % 2 == 0 else ()
        iid = self.tree.insert("", "end", values=(sender, subject or "(no subject)",
                                                  dt.strftime(ts_fmt), d["folder"],
                                                  core.pretty_byte_count(d["size"])), tags=tags)
        self.tree.see(iid)
        self.backed_count += 1
        self._refresh_counts()

    def _append_log(self, line: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line + "\n")
        total = int(self.log_text.index("end-1c").split(".")[0])
        if total > MAX_LOG_LINES:
            self.log_text.delete("1.0", f"{total - MAX_LOG_LINES + 1000}.0")
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    # ------------------------------------------------------------------ close

    def _on_close(self) -> None:
        if self.running and not messagebox.askokcancel("退出", "备份正在进行中，确定要退出吗？", parent=self.root):
            return
        self.root.destroy()

    def run(self) -> None:
        self.root.after(100, self._pump)
        self.root.mainloop()


def main() -> int:
    app = BackupApp()
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
