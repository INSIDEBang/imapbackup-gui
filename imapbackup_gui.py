#!/usr/bin/env python3
"""邮箱备份工具 — Tkinter GUI front-end for imapbackup312 (fork addition)

Design decisions live in docs/adr/ and CONTEXT.md; the issue trail is under
.scratch/tkinter-gui/. Key contracts:
 - Reuses imapbackup312 module-level functions (connect_and_login, get_names,
   scan_folder, scan_file, scan_eml_dir, download_messages); no core logic is copied.
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

__version__ = "0.1.0"
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

import imapbackup312 as core

LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-10s | %(funcName)s:%(lineno)d | %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
LOG_FORMATTER = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)
LOG_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppDataLocal") / "imapbackup-gui" / "log"
MAX_LOG_LINES = 5000
UPTODATE_STATUS = "已是最新，无待备份邮件"


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


class BackupApp:
    """Single-window layout: config zones on top, Notebook (邮件列表/运行日志) below, status bar at the bottom."""

    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title("邮箱备份工具")
        self.root.geometry("960x680")
        self.root.minsize(800, 600)

        self.events: queue.Queue[tuple] = queue.Queue()
        self.stop_flag = threading.Event()
        self.log = logging.getLogger("gui")
        self._setup_logging()

        # ---- state ----
        self.connect_state = "idle"  # idle | connecting | connected | stale
        self._snapshot: tuple[str, str, str] | None = None  # (server, user, password) at connect time
        self.running = False
        self.folder_rows: list[dict] = []
        self._ever_loaded = False
        self._port_touched = False
        self._config_widgets: list[tuple[tk.Widget, str]] = []
        self.server_total = 0
        self.pending_total = 0
        self.backed_count = 0

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
        left.grid(row=0, column=0, sticky="n", padx=(0, 8))
        self._build_conn_group(left)
        self._build_output_group(left)
        self._build_folder_group(outer)

    def _build_conn_group(self, parent: tk.Frame) -> None:
        group = tk.LabelFrame(parent, text="连接")
        group.pack(fill="x", pady=(0, 6))
        group.columnconfigure(1, weight=1)

        tk.Label(group, text="邮箱类型：").grid(row=0, column=0, sticky="e", padx=(8, 0), pady=3)
        tk.Label(group, text="IMAP（仅接收，不发送）", anchor="w").grid(row=0, column=1, columnspan=3, sticky="w", pady=3)

        tk.Label(group, text="帐号：").grid(row=1, column=0, sticky="e", padx=(8, 0))
        self.user_var = tk.StringVar()
        self._register(tk.Entry(group, textvariable=self.user_var)).grid(row=1, column=1, columnspan=3, sticky="we", padx=4, pady=3)

        tk.Label(group, text="收件服务器：").grid(row=2, column=0, sticky="e", padx=(8, 0))
        self.server_var = tk.StringVar()
        self._register(tk.Entry(group, textvariable=self.server_var)).grid(row=2, column=1, sticky="we", padx=4, pady=3)
        self.ssl_var = tk.BooleanVar(value=True)
        self._register(tk.Checkbutton(group, text="SSL", variable=self.ssl_var, command=self._on_ssl_toggle)).grid(row=2, column=2, sticky="w")
        port_box = tk.Frame(group)
        port_box.grid(row=2, column=3, sticky="w", padx=(0, 8))
        tk.Label(port_box, text="端口：").pack(side="left")
        self.port_var = tk.StringVar(value="993")
        port_entry = self._register(tk.Entry(port_box, textvariable=self.port_var, width=6))
        port_entry.pack(side="left")
        port_entry.bind("<Key>", self._on_port_key, add="+")

        tk.Label(group, text="密码：").grid(row=3, column=0, sticky="e", padx=(8, 0))
        self.password_var = tk.StringVar()
        self.password_entry = self._register(tk.Entry(group, textvariable=self.password_var, show="*"))
        self.password_entry.grid(row=3, column=1, columnspan=3, sticky="we", padx=4, pady=3)
        self.show_var = tk.BooleanVar(value=False)
        self._register(tk.Checkbutton(group, text="显示", variable=self.show_var, command=self._on_show_toggle)).grid(row=3, column=4, sticky="w", padx=(0, 8))

        self.connect_btn = self._register(tk.Button(group, text="连接并加载文件夹", command=self._on_connect))
        self.connect_btn.grid(row=4, column=0, columnspan=2, sticky="we", padx=8, pady=(4, 8))
        self.stale_hint = tk.Label(group, text="连接信息已更改，请重新连接并加载文件夹", foreground="red")
        self.stale_hint.grid(row=4, column=2, columnspan=3, sticky="w", padx=(4, 8))
        self.stale_hint.grid_remove()

        for var in (self.server_var, self.user_var, self.password_var):
            var.trace_add("write", self._check_stale)

    def _build_output_group(self, parent: tk.Frame) -> None:
        group = tk.LabelFrame(parent, text="输出")
        group.pack(fill="x")
        group.columnconfigure(1, weight=1)

        tk.Label(group, text="备份保存位置：").grid(row=0, column=0, sticky="e", padx=(8, 0), pady=3)
        self.outdir_var = tk.StringVar(value=str(Path.home() / "邮箱备份"))
        self._register(tk.Entry(group, textvariable=self.outdir_var)).grid(row=0, column=1, sticky="we", padx=4, pady=3)
        self._register(tk.Button(group, text="浏览…", command=self._on_browse)).grid(row=0, column=2, sticky="w", padx=(0, 8))

        fmt_row = tk.Frame(group)
        fmt_row.grid(row=1, column=0, columnspan=3, sticky="w", padx=8, pady=(2, 8))
        self.mbox_var = tk.BooleanVar(value=False)
        self.eml_var = tk.BooleanVar(value=True)
        self._register(tk.Checkbutton(fmt_row, text="mbox 输出", variable=self.mbox_var, command=self._update_start_state)).pack(side="left")
        self._register(tk.Checkbutton(fmt_row, text="eml 输出", variable=self.eml_var, command=self._update_start_state)).pack(side="left", padx=(12, 0))

    def _build_folder_group(self, parent: tk.Frame) -> None:
        group = tk.LabelFrame(parent, text="邮箱文件夹")
        group.grid(row=0, column=1, sticky="nsew", padx=(0, 8))
        group.columnconfigure(0, weight=1)
        group.rowconfigure(1, weight=1)

        toolbar = tk.Frame(group)
        toolbar.grid(row=0, column=0, columnspan=2, sticky="we", padx=6, pady=(2, 0))
        self._register(tk.Button(toolbar, text="全选", command=lambda: self._set_all_folders(True))).pack(side="left")
        self._register(tk.Button(toolbar, text="清空", command=lambda: self._set_all_folders(False))).pack(side="left", padx=(6, 0))
        self._register(tk.Button(toolbar, text="仅收件箱", command=self._set_inbox_only)).pack(side="left", padx=(6, 0))

        self.folder_canvas = tk.Canvas(group, highlightthickness=0, height=200)
        self.folder_canvas.grid(row=1, column=0, sticky="nsew", padx=(6, 0), pady=4)
        folder_scroll = ttk.Scrollbar(group, orient="vertical", command=self.folder_canvas.yview)
        folder_scroll.grid(row=1, column=1, sticky="ns", padx=(0, 6), pady=4)
        self.folder_inner = tk.Frame(self.folder_canvas)
        self._folder_win = self.folder_canvas.create_window((0, 0), window=self.folder_inner, anchor="nw")
        self.folder_inner.bind("<Configure>", lambda _e: self.folder_canvas.configure(scrollregion=self.folder_canvas.bbox("all")))
        self.folder_canvas.bind("<Configure>", lambda e: self.folder_canvas.itemconfigure(self._folder_win, width=e.width))
        self.folder_canvas.configure(yscrollcommand=folder_scroll.set)
        self.folder_canvas.bind("<Enter>", lambda _e: self.folder_canvas.bind_all("<MouseWheel>", self._on_folder_wheel))
        self.folder_canvas.bind("<Leave>", lambda _e: self.folder_canvas.unbind_all("<MouseWheel>"))

        placeholder = tk.Label(self.folder_inner, text="连接成功后，这里会列出该邮箱的所有文件夹", foreground="gray")
        placeholder.pack(anchor="w", padx=8, pady=8)

    # --------------------------------------------------------------- run area

    def _build_run_area(self) -> None:
        run = tk.Frame(self.root)
        run.pack(fill="both", expand=True, padx=8, pady=(2, 8))

        # status bar packs first (side=bottom) so the Notebook's expand can never clip it
        bar = tk.Frame(run)
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
        tree_scroll = ttk.Scrollbar(list_tab, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        tree_scroll.grid(row=0, column=1, sticky="ns")

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

        self.count_var = tk.StringVar(value="尚未开始备份")
        tk.Label(bar, textvariable=self.count_var, anchor="w").pack(side="left")
        self.status_var = tk.StringVar(value="")
        self.status_label = tk.Label(bar, textvariable=self.status_var, anchor="w")
        self.status_label.pack(side="left", padx=(16, 8), fill="x", expand=True)
        self.progress = ttk.Progressbar(bar, orient="horizontal", length=220, mode="determinate", maximum=1)
        self.progress.pack(side="left", padx=(0, 8))
        self.start_btn = tk.Button(bar, text="开始备份", width=10, command=self._on_start)
        self.start_btn.pack(side="left")
        self.stop_btn = tk.Button(bar, text="停止", width=6, state="disabled", command=self._on_stop)
        self.stop_btn.pack(side="left", padx=(6, 0))

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
        if self._ever_loaded:
            checked = {r["foldername"] for r in self.folder_rows if r["var"].get()}
        else:
            checked = {"INBOX"}
        for child in self.folder_inner.winfo_children():
            child.destroy()
        self.folder_rows = []
        ordered = sorted(names, key=lambda n: tuple(n[2].split("/")))
        for foldername, filename, eml_relpath, display in ordered:
            depth = eml_relpath.count("/")
            var = tk.BooleanVar(value=foldername in checked)
            var.trace_add("write", self._update_start_state)
            cb = self._register(tk.Checkbutton(self.folder_inner, text="    " * depth + display, variable=var))
            cb.pack(anchor="w", fill="x", padx=4)
            self.folder_rows.append({"foldername": foldername, "filename": filename,
                                     "eml_relpath": eml_relpath, "display": display, "var": var})
        self._ever_loaded = True

    # ------------------------------------------------------- stale state rules

    def _check_stale(self, *_args) -> None:
        if self.running or self._snapshot is None or self.connect_state not in ("connected", "stale"):
            return
        cur = (self.server_var.get(), self.user_var.get(), self.password_var.get())
        self.connect_state = "connected" if cur == self._snapshot else "stale"
        self._apply_connect_state()
        self._update_start_state()

    def _apply_connect_state(self) -> None:
        if self.connect_state == "connecting":
            self.connect_btn.configure(text="正在连接…", state="disabled")
            self.stale_hint.grid_remove()
        elif self.connect_state == "connected":
            self.connect_btn.configure(text="已成功连接", state="disabled")
            self.stale_hint.grid_remove()
        else:  # idle / stale — same control, stale adds the hint
            self.connect_btn.configure(text="连接并加载文件夹", state="normal")
            if self.connect_state == "stale":
                self.stale_hint.grid()
            else:
                self.stale_hint.grid_remove()

    # --------------------------------------------------------- misc handlers

    def _on_ssl_toggle(self) -> None:
        if not self._port_touched:
            self.port_var.set("993" if self.ssl_var.get() else "143")

    def _on_port_key(self, _event) -> str:
        self._port_touched = True
        return None  # continue normal binding

    def _on_show_toggle(self) -> None:
        self.password_entry.configure(show="" if self.show_var.get() else "*")

    def _on_browse(self) -> None:
        initial = self.outdir_var.get().strip() or str(Path.home())
        chosen = filedialog.askdirectory(parent=self.root, title="选择备份保存位置", initialdir=initial)
        if chosen:
            self.outdir_var.set(Path(chosen).as_posix())

    def _set_all_folders(self, value: bool) -> None:
        for row in self.folder_rows:
            row["var"].set(value)

    def _set_inbox_only(self) -> None:
        for row in self.folder_rows:
            row["var"].set(row["foldername"] == "INBOX")

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
        if children := self.tree.get_children():
            self.tree.delete(*children)
        self.progress.configure(maximum=1, value=0)
        self.count_var.set("正在统计…")
        self._set_status("正在连接…", error=False)
        self._set_config_enabled(False)
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.notebook.select(0)
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
                local: dict[str, str] = {}
                if basedir is not None:
                    local.update(core.scan_file(f"{account}/{filename}", False, True, basedir, quiet=False, log=log))
                if eml_root is not None:
                    local.update(core.scan_eml_dir(eml_root / account / eml_relpath, False, True, quiet=False, log=log))
                new = {mid: remote[mid] for mid in remote if mid not in local}
                server_total += len(remote)
                pending_total += len(new)
                plans.append((n, new))
            eq.put(("totals", server_total, pending_total))

            # pass 2: download; folders were re-selectable because scan_folder moved the selection each time
            if pending_total > 0 and not self.stop_flag.is_set():
                if basedir is not None:
                    core.ensure_basedir(basedir)
                    core.create_folder_structure([p[0] for p in plans], basedir, account)
                for n, new in plans:
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
                                           eml_dir=eml_folder_dir, foldername=display,
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
        self.status_var.set(text)
        self.status_label.configure(foreground="red" if error else "black")

    def _refresh_counts(self) -> None:
        self.count_var.set(f"服务器共 {self.server_total} ｜ 待备份 {self.pending_total} ｜ 已备份 {self.backed_count} / {self.pending_total}")
        self.progress.configure(value=self.backed_count)

    def _add_message_row(self, d: dict) -> None:
        dt: datetime = d["timestamp"]
        ts_fmt = "%m-%d %H:%M" if dt.year == datetime.now().year else "%Y-%m-%d %H:%M"
        sender_raw = core.decode_mime_words(d["from"]) if d["from"] else ""
        name, addr = email.utils.parseaddr(sender_raw)
        sender = name or addr or "?"
        subject = core.decode_mime_words(d["subject"]) if d["subject"] else ""
        iid = self.tree.insert("", "end", values=(sender, subject or "(no subject)",
                                                  dt.strftime(ts_fmt), d["folder"],
                                                  core.pretty_byte_count(d["size"])))
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
