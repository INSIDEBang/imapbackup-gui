#!/usr/bin/env python3
"""IMAP Backup GUI — Tkinter GUI front-end for imapbackup312 (fork addition)

Design decisions live in docs/adr/ and CONTEXT.md; the issue trail is under
.scratch/tkinter-gui/. Key contracts:
 - Reuses imapbackup312 module-level functions (connect_and_login, get_names,
   scan_folder, scan_file, scan_eml_dir, pending_messages, download_messages);
   no core logic is copied.
 - Accounts (name/address/server/SSL/port/DPAPI-encrypted password, folder and
   format choices) live in the settings page; the backup page shows the selected
   account's detail read-only and runs one account at a time, or all of them in
   list order (ADR 0010). The account directory name is always the email address.
 - All IMAP work runs on a worker thread; UI updates flow through a queue polled
   by Tk's after() — widgets are never touched off the main thread.
 - Per-message rows come from the structured report dict; Stop uses the
   cooperative should_stop hook (ADR 0003).
 - The core's Spinner auto-disables and nospinner=True is passed anyway: nothing
   is ever written to stdout.
 - Logs go to %LOCALAPPDATA%\\imapbackup-gui\\log\\run-YYYY-MM-DD.log (daily file);
   UI settings (column widths, window state, accounts and the shared backup root)
   go to the same app-data directory and are independently removable by the
   uninstaller.

Original contributors (abridged): Rui Carmo and the upstream imapbackup
contributors listed in imapbackup312.py.
"""
from __future__ import annotations

__version__ = "1.6.0"  # 与 imapbackup312.__version__ 同一个号（项目统一版本，冒烟 version.sync 守卫）
__author__ = "Chen Chong"
__copyright__ = "(C) 2026 Chen Chong. Code under MIT License."

import base64
import contextlib
import ctypes
import email.utils
import json
import logging
import os
import queue
import sys
import tempfile
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
import uuid
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import imapbackup312 as core
import imapbackup_tray

# 日志目录、文件名与格式由 core 模块统一定义：命令行与 GUI 写入同一日文件（见 ADR 0005）
LOG_FORMAT = core.LOG_FORMAT
LOG_DATEFMT = core.LOG_DATEFMT
LOG_FORMATTER = core.LOG_FORMATTER
LOG_DIR = core.LOG_DIR
MAX_LOG_LINES = 5000

# 单实例互斥量名：必须与 packaging/imapbackup-gui.iss 的 AppMutex 完全一致（卸载时靠它探测运行中的实例）。
# 不加 Global\ 前缀——每用户安装（PrivilegesRequired=lowest）没有创建全局对象的权利。
APP_MUTEX_NAME = "INSIDEBang.imapbackup-gui.single-instance"
ERROR_ALREADY_EXISTS = 183
ICON_FILENAME = "imapbackup-gui.ico"
_instance_mutex_handle: int | None = None  # 进程级持有，不 CloseHandle：进程活着互斥量就该活着
UPTODATE_STATUS = "已是最新，无待备份邮件"
CONNECT_BTN_LABEL = "⇄ 连接并加载文件夹"
CONNECTING_BTN_LABEL = "正在连接…"
CONNECTED_BTN_LABEL = "✓ 已成功连接"
CONNECT_BTN_CHARS = 26          # 连接按钮的固定宽度（字符数）：各状态文案长短不一，定宽才不会抖
START_BTN_LABEL = "开始备份"
RUNNING_BTN_LABEL = "正在备份…"
STOP_BTN_LABEL = "停止"
STOPPING_BTN_LABEL = "正在停止…"  # 停止中：转圈挪到停止按钮上，开始按钮退回静止外观
STALE_HINT_TEXT = "连接信息已更改，请重新连接并加载文件夹"
RUNNING_HINT_TEXT = "备份运行中，文件夹选择与输出格式不可修改，请先停止"
STOPPING_HINT_TEXT = "备份正在停止，完成后即可修改"  # 已按过停止，再说「请先停止」就是过期信息

# 加载动画：连接与备份共用同一套帧与节奏——用户要求两处的加载图标看起来一样。
# 帧是盲文点阵（U+2800 段，Windows 字体普遍有字形），画在按钮文字前缀上，不需要图片资产；
# 按钮处在禁用态时 Tk 会用系统灰降级渲染，两处观感因此天然一致。
SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
SPINNER_INTERVAL_MS = 150

# 按钮图标：彩色图形只能由代码逐像素画出来——文字字形只有一种颜色，且按钮一进禁用态
# 就会被系统灰覆盖。不引图片资产：PhotoImage 运行时生成，打包零成本、没有图标路径问题。
PLAY_ICON_COLOR = "#1f9d55"     # 开始：绿色
STOP_ICON_COLOR = "#d13438"     # 停止：红色
ICON_MIN_SIZE = 9               # 图标边长下限(px)
ICON_LINESPACE_GAP = 5          # 图标边长 = 默认字体行高 − 这个值：行高变大图标同比例变大，不设上限
STOP_ICON_INSET_RATIO = 0.125   # 红方块相对外接框的内缩比例：方块视觉比同框三角重，缩一圈才平衡
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
FOLDER_ROW_PAD_Y = 1   # 文件夹行的上下 pady(px)；行距 = 行高 + 2×它，滚轮的按行对齐按行距算
WHEEL_DELTA = 120      # Windows 一格滚轮的标准 delta；高分辨率鼠标/触摸板给更小的值，须累加
FOLDER_WHEEL_ROWS = 1  # 一格滚轮滚几行：与下方「邮件列表」的 1 行/格（Tk Treeview 默认）一致
EMPTY_HINT_FG = "gray"            # 空态提示字色：只说明面板用途，不需要用户操作
FOLDER_PLACEHOLDER_TEXT = "连接成功后，这里会列出该邮箱的所有文件夹"
LIST_PLACEHOLDER_TEXT = "开始备份后，这里会列出本次备份的每封邮件"
APP_DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppDataLocal") / "imapbackup-gui"
COLUMN_CONFIG_PATH = APP_DATA_DIR / "treeview-columns.json"
MAIL_COLUMNS = ("from", "subject", "when", "folder", "size")
UI_SETTINGS_VERSION = 2
DEFAULT_WINDOW_WIDTH = 960
DEFAULT_WINDOW_HEIGHT = 736
MIN_WINDOW_WIDTH = 800
MIN_WINDOW_HEIGHT = 620
ELLIPSIS = "…"                    # 截断省略号，与「正在连接…」同一字符
PROGRESS_MIN_WIDTH = 80           # 进度条地板宽度(px)：计数文字变长先压进度条，触底才开始截断文字
TIP_DELAY_MS = 400                # 悬停多久后弹气泡
TIP_BG = "#ffffe0"                # 气泡底色（经典 tooltip 浅黄）
TIP_WRAP = 600                    # 气泡内容最长宽度(px)，超出自动换行
# 右键菜单：输入框 / 邮件列表 / 运行日志三类各一张 tk.Menu，启动时各建一次复用。
# 三项编辑动作全部走与键盘快捷键相同的类绑定路径；不含「全选」（见 issues/13）。
MENU_CUT_LABEL = "剪切"
MENU_COPY_LABEL = "复制"
MENU_PASTE_LABEL = "粘贴"
MENU_COPY_ALL_LABEL = "复制全部"

# 关闭行为偏好：点窗口关闭按钮时的处理方式，与其它界面设置同存一份 JSON（键 close_action）。
# 不加版本号（bump 会让老设置整份被判不兼容而丢弃），新键按字段独立校验、非法即回退默认值。
CLOSE_ACTION_KEY = "close_action"
CLOSE_ACTION_ASK = "ask"
CLOSE_ACTION_TRAY = "tray"
CLOSE_ACTION_EXIT = "exit"
DEFAULT_CLOSE_ACTION = CLOSE_ACTION_ASK
CLOSE_ACTION_LABELS = ((CLOSE_ACTION_ASK, "关闭窗口时询问我"),
                       (CLOSE_ACTION_TRAY, "关闭窗口时隐藏到托盘，继续在后台运行"),
                       (CLOSE_ACTION_EXIT, "关闭窗口时退出程序"))
SETTINGS_WINDOW_SECTION = "window"
SETTINGS_ACCOUNTS_SECTION = "accounts"
SETTINGS_BACKUP_SECTION = "backup"
# 设置页左栏分类表：(表键, 分类名)。加分类＝加一行 + 建一个详情 Frame（见 _build_settings_page）。
SETTINGS_SECTIONS: tuple[tuple[str, str], ...] = ((SETTINGS_ACCOUNTS_SECTION, "账户"),
                                                  (SETTINGS_BACKUP_SECTION, "备份"),
                                                  (SETTINGS_WINDOW_SECTION, "窗口与后台"))
SETTINGS_SECTION_LIST_WIDTH = 160   # 左分类栏固定宽度(px)：右栏吃掉剩余宽度
PAGE_BACKUP = "backup"
PAGE_SETTINGS = "settings"
PAGE_LABELS = ((PAGE_BACKUP, "备份"), (PAGE_SETTINGS, "设置"))

# 账户（多账户配置）：连接设置在设置页「账户」分类维护；备份页左列表 + 右只读详情。
# 账户名称是纯显示名，不参与 <备份根>/<账号>/ 目录名生成（账号目录取邮箱地址，见 ADR 0002/0010）。
ACCOUNT_LIST_WIDTH = 160            # 备份页左侧账户列表宽度(px)：与设置页左分类栏同宽
ACCOUNT_LIST_TITLE = "账户"          # 备份页左侧列表的带标题边框（与「账户信息」卡同款）
ACCOUNT_NAME_FALLBACK = "未命名账户"
DEFAULT_ACCOUNT_SSL = True
DEFAULT_ACCOUNT_PORT = "993"
DEFAULT_BACKUP_ROOT = str(Path.home() / "邮箱备份")
CARD_EMPTY_TEXT = "—"               # 未选中账户时只读信息卡的占位
PASSWORD_SAVED_TEXT = "已保存"       # 密码行不回显任何明文或掩码位数
PASSWORD_MISSING_TEXT = "未设置"
# 账户行配色：底色取对话框灰（行是自绘控件，不再有 Listbox 的白底），选中行沿用系统高亮色，
# 与原 Listbox 的选中蓝完全一致，且跟随系统主题。
ACCOUNT_ROW_BG = "SystemButtonFace"
ACCOUNT_ROW_FG = "SystemButtonText"
ACCOUNT_ROW_SELECTED_BG = "SystemHighlight"
ACCOUNT_ROW_SELECTED_FG = "SystemHighlightText"
# 账户列表底部的批量入口按钮（勾选工具三格见 _build_accounts_pane）
BACKUP_SELECTED_BTN_LABEL = "备份所选"
QUEUED_STATUS_TEXT = "排队中…"
CONNECTING_STATUS_TEXT = "正在连接…"
ACCOUNTS_HINT_TEXT = ("在这里维护每个账户的连接信息；密码经系统加密后只保存在本机。\n"
                      "账户名称只用于界面区分，备份目录始终按邮箱地址命名。")
BACKUP_ROOT_HINT_TEXT = ("所有账户共用这一个备份位置；每个账户的备份分别保存在该位置下、"
                         "以邮箱地址命名的目录里。")
BACKUP_ROOT_SETTINGS_HINT = "（在设置页「备份」中修改）"

# 托盘：图标只在窗口隐藏期间存在；tooltip 随备份是否运行切换
TRAY_TOOLTIP_IDLE = "IMAP Backup GUI"
TRAY_TOOLTIP_RUNNING = "IMAP Backup GUI — 备份运行中"
TRAY_TITLE_DONE = "备份完成"
TRAY_TITLE_STOPPED = "备份已停止"
TRAY_TITLE_FAILED = "备份失败"
TRAY_BODY_STOPPED = "备份已停止，可随时重新开始"
TRAY_BODY_FAILED = "备份失败，详见运行日志"

# 关闭询问对话框
CLOSE_DLG_TITLE = "关闭窗口"
CLOSE_DLG_TEXT_RUNNING = ("备份正在进行中。\n\n"
                          "隐藏到后台：窗口收进托盘，备份继续运行，图标在任务栏右下角通知区域。\n"
                          "退出程序：中断本次备份并结束程序。")
CLOSE_DLG_TEXT_IDLE = ("隐藏到后台：窗口收进托盘，程序继续运行，图标在任务栏右下角通知区域。\n"
                       "退出程序：结束程序。")
CLOSE_DLG_REMEMBER = "记住我的选择，以后不再询问"
CLOSE_DLG_HIDE = "隐藏到后台"
CLOSE_DLG_EXIT = "退出程序"
QUIT_CONFIRM_TITLE = "退出"
QUIT_CONFIRM_TEXT = "备份正在进行中，退出会中断本次备份。确定要退出吗？"
SETTINGS_HINT_TEXT = ("托盘图标（任务栏右下角通知区域）从程序启动起常驻，直到退出：左键单击它恢复窗口，右键菜单可停止备份或退出。\n"
                      "Windows 11 默认把新托盘图标收进溢出区（任务栏的 ^ 小箭头里），找不到时点开那里看看。\n"
                      "在关闭对话框里勾选「记住我的选择」与在这里选定等价。")


class QueueLogHandler(logging.Handler):
    """Mirror INFO+ records onto the event queue for the 运行日志 tab (current session only)."""

    def __init__(self, events: queue.Queue[tuple]):
        super().__init__(level=logging.INFO)
        self.events = events
        self.setFormatter(LOG_FORMATTER)

    def emit(self, record: logging.LogRecord) -> None:
        # pragma: no cover — logging must never raise into the app
        with contextlib.suppress(Exception):
            self.events.put(("log", self.format(record)))


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 60}:{s % 60:02d}"


def _ellipsize(full: str, budget: int, font: tkfont.Font) -> str:
    """按像素预算截断出「前缀 + …」。

    Args:
        full: 完整文本，截断源。
        budget: 像素预算上限。
        font: 量字宽用的字体。

    Returns:
        放得下时原样返回全文；放不下时返回二分出的最长前缀加省略号，宽度刚好 ≤ budget。

    Note:
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
    """返回同一字体的斜体版。

    Args:
        base: 作为模板的字体。

    Returns:
        按 family/size 重建的斜体字体（Tk 没有字体克隆 API，其余属性走默认）。

    Note:
        size 取绝对值——系统默认字号可能是负数（DPI 无关），Font 构造要正数。
    """
    return tkfont.Font(family=base.cget("family"), size=abs(int(base.cget("size"))), slant="italic")


def _bold_font(base: tkfont.Font) -> tkfont.Font:
    """返回同一字体的加粗版（设置页分组标题用，口径与 _italic_font 一致）。"""
    return tkfont.Font(family=base.cget("family"), size=abs(int(base.cget("size"))), weight="bold")


def last_folder_segment(display: str, path: tuple[str, ...]) -> str:
    """返回显示名的最后一段。

    Args:
        display: 文件夹显示名。
        path: eml_relpath 拆出的层级段；段数等于真实层级深度，用它反查真实分隔符。

    Returns:
        显示名的最后一段；匹配不出分隔符时原样返回 display。

    Note:
        不能直接按 "/" 切 display：QQ 用 "/"，Outlook 用 "."，服务器决定分隔符。
    """
    for delim in ("/", "\\", "."):
        if display.count(delim) == len(path) - 1:
            return display.rsplit(delim, 1)[-1]
    return display


def parse_close_action(raw: object) -> str:
    """校验关闭行为偏好：只认三个已知值，其余（含缺失、类型不对）一律回退默认值。"""
    if raw in (CLOSE_ACTION_ASK, CLOSE_ACTION_TRAY, CLOSE_ACTION_EXIT):
        return str(raw)
    return DEFAULT_CLOSE_ACTION


def decide_close(preference: str, *, hidden: bool) -> str:
    """关闭窗口的决策点：偏好 × 当前窗口状态 → 动作。

    Args:
        preference: 关闭行为偏好（`parse_close_action` 的产物）。
        hidden: 窗口当前是否已隐藏（`root.state() == "withdrawn"`）。

    Returns:
        `"force_exit"`（窗口已隐藏时收到的关闭请求来自外部程序，一律真退出，不能把程序卡在
        后台让安装器/卸载器关不掉它）、`"hide"`、`"exit"` 或 `"ask"`。

    Note:
        备份是否运行不影响这里的判定：它只改变询问对话框的文案与托盘「退出」的确认，
        隐藏与退出两条路径本身与运行状态无关。
    """
    if hidden:
        return "force_exit"
    if preference == CLOSE_ACTION_TRAY:
        return "hide"
    if preference == CLOSE_ACTION_EXIT:
        return "exit"
    return "ask"


def new_account() -> dict:
    """新建一个账户记录：除稳定 id 外都是默认值（还没有地址、服务器与密码）。"""
    return {"id": uuid.uuid4().hex, "name": "", "user": "", "server": "",
            "ssl": DEFAULT_ACCOUNT_SSL, "port": DEFAULT_ACCOUNT_PORT,
            "folders": [], "mbox": False, "eml": True}


def parse_account(raw: object) -> dict | None:
    """校验并规范化一条持久化的账户记录。

    Args:
        raw: 设置文件 `accounts` 列表里的一项。

    Returns:
        规范化后的账户字典；缺少稳定 id（或根本不是字典）时返回 None，调用方跳过该账户。

    Note:
        id 之外按「单字段非法只跳过该字段」降级（ADR 0008 的容错风格）：类型不符回落默认值，
        一条脏字段不该毁掉整个账户；`id` 是列表勾选、批量运行与事件分发的引用键，缺了就只能丢弃。
    """
    if not isinstance(raw, dict):
        return None
    account_id = raw.get("id")
    if not isinstance(account_id, str) or not account_id:
        return None
    account = {"id": account_id,
               "name": raw.get("name") if isinstance(raw.get("name"), str) else "",
               "user": raw.get("user") if isinstance(raw.get("user"), str) else "",
               "server": raw.get("server") if isinstance(raw.get("server"), str) else "",
               "port": raw.get("port") if isinstance(raw.get("port"), str) else DEFAULT_ACCOUNT_PORT,
               "ssl": raw.get("ssl") if isinstance(raw.get("ssl"), bool) else DEFAULT_ACCOUNT_SSL,
               "mbox": raw.get("mbox") if isinstance(raw.get("mbox"), bool) else False,
               "eml": raw.get("eml") if isinstance(raw.get("eml"), bool) else True,
               "folders": []}
    cipher = raw.get("password_dpapi")
    if isinstance(cipher, str) and cipher:
        account["password_dpapi"] = cipher
    folders = raw.get("folders")
    if isinstance(folders, list):
        account["folders"] = [name for name in folders if isinstance(name, str) and name]
    return account


def account_record(account: dict) -> dict:
    """账户 → 落盘记录：只保留已知字段，空密码连键都不写（与旧 config 节同一口径）。"""
    record = {"id": account["id"],
              "name": account.get("name", ""),
              "user": account.get("user", ""),
              "server": account.get("server", ""),
              "ssl": bool(account.get("ssl", DEFAULT_ACCOUNT_SSL)),
              "port": str(account.get("port", DEFAULT_ACCOUNT_PORT)),
              "folders": [name for name in account.get("folders", []) if isinstance(name, str) and name],
              "mbox": bool(account.get("mbox", False)),
              "eml": bool(account.get("eml", True))}
    cipher = account.get("password_dpapi")
    if isinstance(cipher, str) and cipher:
        record["password_dpapi"] = cipher
    return record


def account_display(account: dict) -> str:
    """账户在界面上的显示名：自定义名称优先，其次邮箱地址，都空则用占位名。"""
    for key in ("name", "user"):
        value = account.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ACCOUNT_NAME_FALLBACK


def _make_tray(events: queue.Queue[tuple], log: logging.Logger) -> imapbackup_tray.TrayHost | None:
    """托盘宿主的唯一构造点：测试替换这里即可注入假托盘。

    Args:
        events: GUI 事件队列（托盘线程只往里投元组，不碰 widget）。
        log: 诊断日志。

    Returns:
        可用的托盘宿主；非 Windows 或创建失败时 None（调用方退化为最小化到任务栏）。
    """
    tray = imapbackup_tray.create_tray(
        icon_path=_window_icon_path(),
        tooltip=TRAY_TOOLTIP_IDLE,
        on_show=lambda: events.put(("tray_show",)),
        on_stop=lambda: events.put(("tray_stop",)),
        on_quit=lambda: events.put(("tray_quit",)),
        log=log)
    if tray is None:
        log.info("托盘不可用：关闭窗口选「隐藏到后台」时将退化为最小化到任务栏")
    return tray


def _window_icon_path() -> str | None:
    """窗口/文件图标路径：打包后在 PyInstaller 数据目录（onedir 的 _internal/），源码运行时在 packaging/。"""
    base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) if getattr(sys, "frozen", False) \
        else Path(__file__).resolve().parent
    for cand in (base / ICON_FILENAME, base / "packaging" / ICON_FILENAME):
        if cand.is_file():
            return str(cand)
    return None


def _icon_size() -> int:
    """返回按钮图标的边长(px)。

    Returns:
        图标边长，跟默认字体行高走——系统缩放或字号变大，图标同比例变大。

    Note:
        不设上限（封顶会让高 DPI 下的图标相对文字偏小）；比行高矮几像素，按钮高度因此仍由
        文字决定，只留一个下限兜底。
    """
    linespace = tkfont.nametofont("TkDefaultFont").metrics("linespace")
    return max(ICON_MIN_SIZE, linespace - ICON_LINESPACE_GAP)


def _put(img: tk.PhotoImage, color: str, x0: int, y0: int, x1: int, y1: int) -> None:
    """画一个矩形色块。坐标先夹进画布：put 越界会抛 TclError。"""
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(img.width(), x1), min(img.height(), y1)
    if x1 > x0 and y1 > y0:
        img.put(color, to=(x0, y0, x1, y1))


def _play_icon(color: str, size: int) -> tk.PhotoImage:
    """实心右向三角（开始/播放）：左边缘竖直，尖端落在中线。"""
    img = tk.PhotoImage(width=size, height=size)
    mid = (size - 1) / 2
    for y in range(size):
        width = round(size * (1 - abs(y - mid) / (mid + 0.5)))
        _put(img, color, 0, y, width, y + 1)
    return img


def _stop_icon(color: str, size: int) -> tk.PhotoImage:
    """实心方块（停止）。缩到 3/4 边长：同外接框的方块比三角视觉更重，缩一点才平衡。"""
    img = tk.PhotoImage(width=size, height=size)
    inset = max(1, round(size * STOP_ICON_INSET_RATIO))
    _put(img, color, inset, inset, size - inset, size - inset)
    return img


def _blank_icon(height: int) -> tk.PhotoImage:
    """生成高与彩色图标相同、宽 1px 的全透明位图（一个像素都不画）。

    Args:
        height: 位图高度(px)，与彩色图标一致。

    Returns:
        全透明的占位图。

    Note:
        加载态不能把 image 撤成 ""：Tk 的按钮请求高度取决于「有没有 image」——实测同一按钮
        带图 28px、撤图 30px，与文案是中文还是盲文帧无关。保留一枚 1px 宽的占位只借它的高度：
        按钮高度在加载/静止之间不动，而图标槽不再占宽——转圈符贴近左边框，左右留白与静止态
        一样对称。
    """
    return tk.PhotoImage(width=1, height=height)


def _another_instance_running() -> bool:
    """抢不到同名互斥量就说明已有实例在跑。非 Windows 不创建互斥量，直接放行。"""
    global _instance_mutex_handle
    if sys.platform != "win32":
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _instance_mutex_handle = kernel32.CreateMutexW(None, False, APP_MUTEX_NAME)
    return ctypes.get_last_error() == ERROR_ALREADY_EXISTS


class _DATA_BLOB(ctypes.Structure):  # wincrypt 的 DATA_BLOB
    _fields_ = [("cbData", ctypes.c_ulong),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


_CRYPTPROTECT_UI_FORBIDDEN = 0x1  # 凭据场景不许弹 UI


def _dpapi_run(func_name: str, raw: bytes) -> bytes | None:
    """调一次 crypt32 的加/解密入口。

    Args:
        func_name: "CryptProtectData" 或 "CryptUnprotectData"。
        raw: 输入字节串（明文或密文）。

    Returns:
        成功时的输出字节串；非 win32 或任何失败返回 None。
    """
    if sys.platform != "win32":
        return None
    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        buf = ctypes.create_string_buffer(raw, len(raw))
        blob_in = _DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = _DATA_BLOB()
        if not getattr(crypt32, func_name)(ctypes.byref(blob_in), None, None, None, None,
                                           _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out)):
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(blob_out.pbData)
    except (OSError, AttributeError):
        return None


def _dpapi_encrypt(text: str) -> str | None:
    """明文 → DPAPI 密文的 base64（仅当前用户+当前机器可解）。失败返回 None。"""
    cipher = _dpapi_run("CryptProtectData", text.encode("utf-8"))
    return None if cipher is None else base64.b64encode(cipher).decode("ascii")


def _dpapi_decrypt(cipher: str) -> str | None:
    """base64 密文 → 明文。解码/解密/utf-8 任一失败返回 None，调用方按没存过处理。"""
    try:
        raw = base64.b64decode(cipher, validate=True)
    except (TypeError, ValueError):
        return None
    plain = _dpapi_run("CryptUnprotectData", raw)
    if plain is None:
        return None
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError:
        return None


class BackupApp:
    """Single-window layout: the backup page is an account list plus the selected account's
    detail (read-only card, folder tree, Notebook 邮件列表/运行日志, status bar); the settings
    page holds the accounts and the shared backup root."""

    def __init__(self) -> None:
        self.root = tk.Tk()
        icon = _window_icon_path()
        if icon:
            # 非 Windows 平台没有 iconbitmap，或图标不可读时退回 Tk 默认图标
            with contextlib.suppress(tk.TclError):
                self.root.iconbitmap(icon)
        self.root.title("IMAP Backup GUI")
        self.root.geometry(f"{DEFAULT_WINDOW_WIDTH}x{DEFAULT_WINDOW_HEIGHT}")
        self.root.minsize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)

        self.events: queue.Queue[tuple] = queue.Queue()
        self.stop_flag = threading.Event()
        self.log = logging.getLogger("gui")
        self._setup_logging()
        self._ui_settings = self._read_ui_settings()
        self._normal_geometry = self._restore_window_settings()

        # ---- accounts（多账户配置；accounts 与设置文件里的列表是同一个对象，改动即内存态）----
        self.accounts: list[dict] = self._ui_settings.get("accounts") or []
        self._ui_settings["accounts"] = self.accounts
        selected = self._ui_settings.get("selected_account")
        self._selected_account_id: str | None = selected if selected in {a["id"] for a in self.accounts} \
            else (self.accounts[0]["id"] if self.accounts else None)
        self._ui_settings["selected_account"] = self._selected_account_id
        self.backup_root_var = tk.StringVar(value=self._ui_settings.get("backup_root") or DEFAULT_BACKUP_ROOT)
        self._states: dict[str, dict] = {}      # 每账户独立状态（连接、文件夹树、邮件行、计数、进度）
        self._editing_account_id: str | None = None
        self._run_account_ids: list[str] = []   # 本次批量运行参与的账户（按运行顺序）
        self._run_t0 = 0.0
        # 左栏勾选（「备份所选」跑哪些账户）：纯界面态，不落盘；新建/读入的账户默认勾上
        self._checked_accounts: set[str] = {account["id"] for account in self.accounts}
        self._account_rows: dict[str, dict] = {}

        # ---- state ----
        self.connect_state = "idle"  # idle | connecting | connected | stale（当前可见账户的口径）
        self.running = False
        self.stopping = False  # 运行中的子阶段：已按停止、工作线程还在收尾（按钮与提示都跟着换口径）
        self.folder_rows: list[dict] = []
        self._folder_nodes: dict[tuple[str, ...], dict] = {}
        self._folder_clickables: list[tuple[tk.Label, str]] = []
        self._spinner_job: str | None = None  # root.after id while the button shows a spinner
        self._run_spinner_job: str | None = None  # 同上，备份运行中的开始按钮
        self._config_widgets: list[tuple[tk.Widget, str]] = []
        self.server_total = 0
        self.pending_total = 0
        self.backed_count = 0
        self._row_index = 0  # 邮件列表插入序号；清表归零，斑马纹按它的奇偶决定
        self._status_full = ""              # 状态行全文；status_var 里可能是它的截断形
        self._status_error = False          # 状态行当前是否错误色（切账户时要跟着状态一起还原）
        self._count_full = "尚未开始备份"    # 计数行全文；count_var 同理
        self._fit_font: tkfont.Font | None = None  # 量字宽的缓存，两行标签同一默认字体
        self._tip: tk.Toplevel | None = None
        self._tip_after: str | None = None  # 待发的气泡 after id，leave 时取消
        self._wheel_accum = 0               # 面板内滚轮没攒满一格的余量（小 delta 的设备要用）
        self._entry_menu_target: tk.Entry | None = None  # 输入框菜单弹窗当刻的目标，动作命令读它
        self._tree_menu_target: tuple[str, int] | None = None  # 邮件菜单的复制目标：(行 id, 列号 1 起)
        self._column_config_loaded = False
        self._column_config_fitted = False
        self._column_reflowing = False

        # 页面外壳：备份页与设置页同格叠放，tkraise 切页；菜单栏两项直接切页（不开下拉）
        self.page_host = tk.Frame(self.root)
        self.page_host.pack(fill="both", expand=True)
        self.page_host.columnconfigure(0, weight=1)
        self.page_host.rowconfigure(0, weight=1)
        self.backup_page = tk.Frame(self.page_host)
        self.backup_page.grid(row=0, column=0, sticky="nsew")
        self.settings_page = tk.Frame(self.page_host)
        self.settings_page.grid(row=0, column=0, sticky="nsew")
        self._build_menubar()
        # 托盘宿主：非 Windows 或创建失败时为 None，关闭窗口选「隐藏到后台」会退化为最小化
        self._tray = _make_tray(self.events, self.log)

        self._build_backup_page()
        self._build_settings_page()
        self._build_context_menus()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Configure>", self._on_root_configure, add="+")
        self._refresh_account_list()
        self._refresh_edit_list(self.accounts[0]["id"] if self.accounts else None)
        self._render_account_detail()
        self._apply_connect_state()
        self._update_start_state()
        self._show_page(PAGE_BACKUP)
        # 窗口真正落地后再摆一次几何：菜单栏在落地前会把客户区高度吃掉一截（真机实测默认 736
        # 变成 716），落地后设置才生效、所见即所得。最大化时不许再动——ADR 0008 要求先恢复
        # 正常几何再最大化，zoomed 状态下再设几何会碰正常几何。
        self.root.update()
        if self.root.state() != "zoomed":
            self.root.geometry(self._target_geometry)
        # 托盘图标随程序启动常驻（窗口可见时也在），直到 _quit() 才收掉；挂不出来不致命，
        # 记一条 warning，隐藏窗口前还会重试一次——那次失败才退化为最小化
        if self._tray is not None and not self._tray.show(self._tray_tooltip()):
            self.log.warning("托盘图标挂出失败：关闭窗口选「隐藏到后台」将退化为最小化到任务栏")
        self._sync_tray()

    # ------------------------------------------------------------------ logging

    def _setup_logging(self) -> None:
        debug = os.environ.get("IMAPBACKUP_GUI_DEBUG") == "1"
        level = logging.DEBUG if debug else logging.INFO
        root = logging.getLogger()
        handlers: list = [QueueLogHandler(self.events)]
        file_handler = core.make_log_file_handler()
        if file_handler is not None:
            file_handler.setLevel(level)
            file_handler.setFormatter(LOG_FORMATTER)
            handlers.append(file_handler)
        root.handlers[:] = handlers
        root.setLevel(level)
        # 日志文件建不出来不该让程序起不来：降级为只写运行日志面板
        if file_handler is None:
            self.log.warning("无法创建日志文件：%s", LOG_DIR)
        self.log.info("程序启动 v%s", __version__)

    # ------------------------------------------------------------- config area

    def _register[T: tk.Widget](self, widget: T) -> T:
        """登记配置区控件并原样返回：加入运行中的启停名单，构造时可链式书写。"""
        self._config_widgets.append((widget, str(widget.cget("state"))))
        return widget

    def _build_backup_page(self) -> None:
        """备份页外壳：左侧账户列表 + 右侧账户备份详情（邮件客户端式主从布局）。"""
        body = tk.Frame(self.backup_page)
        body.pack(fill="both", expand=True)
        pane = self._build_accounts_pane(body)
        # 右边不留白：右侧内容区（配置区/运行区）自带 8px 内边距，这里再留一条就成了 16px，
        # 比「账户信息」与「邮箱文件夹」之间那条 8px 明显宽一截。
        pane.pack(side="left", fill="y", padx=(8, 0), pady=8)
        right = tk.Frame(body)
        right.pack(side="left", fill="both", expand=True)
        self._build_config_area(right)
        self._build_run_area(right)

    def _build_accounts_pane(self, parent: tk.Frame) -> tk.LabelFrame:
        """左侧账户列表：点选切换右侧详情，勾选决定「备份所选」跑哪些账户。

        边框用带标题的 LabelFrame，与右栏「账户信息」卡同款；行是自绘的（复选框 + 名称），
        一个控件只表达一件事——Listbox 一行放不下「点选切换」与「勾选备份」两个动作。
        """
        pane = self.account_pane = tk.LabelFrame(parent, text=ACCOUNT_LIST_TITLE)
        pane.configure(width=ACCOUNT_LIST_WIDTH)
        pane.pack_propagate(False)  # 固定宽：账户名再长也不挤动右栏（右栏吃剩余宽度）
        area = tk.Frame(pane)
        area.columnconfigure(0, weight=1)
        area.rowconfigure(0, weight=1)
        # 列表装在画布里，账户多到一屏放不下才挂滚条。滚条贴面板内沿，与「邮箱文件夹」同款；
        # 画布底色跟行一样是对话框灰：行下面那点空白也是列表的一部分。
        self.account_canvas = tk.Canvas(area, highlightthickness=0, background=ACCOUNT_ROW_BG)
        self.account_canvas.grid(row=0, column=0, sticky="nsew")
        scroll = self.account_scroll = ttk.Scrollbar(area, orient="vertical",
                                                     command=self.account_canvas.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.account_canvas.configure(yscrollcommand=scroll.set)
        self.account_list = tk.Frame(self.account_canvas, background=ACCOUNT_ROW_BG)
        self._account_win = self.account_canvas.create_window((0, 0), window=self.account_list, anchor="nw")
        self._account_scroll_shown = False
        scroll.grid_remove()  # 先不挂：去留由 _sync_account_scroll 按内容高度定
        scroll.bind("<MouseWheel>", self._on_wheel)  # 与面板内滚条同款：走窗口级节奏
        self.account_list.bind("<Configure>", lambda _e: self._sync_account_scroll())
        self.account_canvas.bind("<Configure>", self._on_account_canvas_configure)
        self.backup_selected_btn = tk.Button(pane, text=BACKUP_SELECTED_BTN_LABEL,
                                             command=self._on_start_selected)
        tools = tk.Frame(pane)
        tool_actions = (("全选", lambda: self._set_all_account_checks(True)),
                        ("反选", self._invert_account_checks),
                        ("清空", lambda: self._set_all_account_checks(False)))
        for column, (label, command) in enumerate(tool_actions):
            tools.columnconfigure(column, weight=1, uniform="account-tools")  # 三格等宽
            tk.Button(tools, text=label, command=command).grid(row=0, column=column, sticky="we", padx=2)
        # 三块都先于列表以 side=bottom 打包（先打包的落最底）：自上而下是 列表 / 勾选工具 / 备份所选
        self.backup_selected_btn.pack(side="bottom", fill="x", padx=4, pady=4)
        tools.pack(side="bottom", fill="x", padx=2, pady=(4, 0))
        area.pack(fill="both", expand=True, pady=(4, 0))
        return pane

    def _build_config_area(self, parent: tk.Frame) -> None:
        outer = tk.Frame(parent)
        # 下边距与运行区的上边距合起来 = 面板之间的 8px（上半区与下半区之间的那条）
        outer.pack(fill="x", padx=8, pady=(8, 4))
        outer.columnconfigure(0, weight=1)
        outer.columnconfigure(1, weight=1)
        left = tk.Frame(outer)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self._build_conn_group(left)
        self._build_output_group(left)
        self._build_folder_group(outer)

    def _build_conn_group(self, parent: tk.Frame) -> None:
        """账户信息卡：连接设置已在设置页维护，这里只读展示（无任何输入框）。"""
        group = self.conn_group = tk.LabelFrame(parent, text="账户信息")
        # 与「账户信息」「邮箱文件夹」之间那条 8px 同宽：面板之间的横竖间距都取 8px
        group.pack(fill="x", pady=(0, 8))
        group.columnconfigure(1, weight=1)

        tk.Label(group, text="邮箱类型：").grid(row=0, column=0, sticky="e", padx=(8, 0), pady=3)
        tk.Label(group, text="IMAP（仅接收，不发送）", anchor="w").grid(row=0, column=1, columnspan=4, sticky="w", pady=3)

        self._card_labels: dict[str, tk.Label] = {}
        for row, (key, caption) in enumerate((("user", "邮箱地址："), ("server", "收件服务器：")), start=1):
            tk.Label(group, text=caption).grid(row=row, column=0, sticky="e", padx=(8, 0), pady=3)
            label = tk.Label(group, text=CARD_EMPTY_TEXT, anchor="w")
            label.grid(row=row, column=1, columnspan=4, sticky="we", padx=4, pady=3)
            self._card_labels[key] = label

        tk.Label(group, text="SSL：").grid(row=3, column=0, sticky="e", padx=(8, 0), pady=3)
        ssl_label = tk.Label(group, text=CARD_EMPTY_TEXT, anchor="w")
        ssl_label.grid(row=3, column=1, sticky="w", padx=4, pady=3)
        self._card_labels["ssl"] = ssl_label
        tk.Label(group, text="端口：").grid(row=3, column=2, sticky="e", padx=(8, 0), pady=3)
        port_label = tk.Label(group, text=CARD_EMPTY_TEXT, anchor="w")
        port_label.grid(row=3, column=3, columnspan=2, sticky="w", padx=4, pady=3)
        self._card_labels["port"] = port_label

        tk.Label(group, text="密码：").grid(row=4, column=0, sticky="e", padx=(8, 0), pady=3)
        password_label = tk.Label(group, text=CARD_EMPTY_TEXT, anchor="w")
        password_label.grid(row=4, column=1, columnspan=4, sticky="we", padx=4, pady=3)
        self._card_labels["password"] = password_label

        self.connect_btn = self._register(tk.Button(group, text=CONNECT_BTN_LABEL, width=CONNECT_BTN_CHARS,
                                                     command=self._on_connect))
        self.connect_btn.grid(row=5, column=0, columnspan=6, pady=(10, 10))
        # 按需挂出：无提示时 grid_remove 让出这一行，按钮位置不变，让出的高度由下方邮件列表吸收
        self.conn_hint = tk.Label(group, text="")
        self.conn_hint.grid(row=6, column=0, columnspan=6, pady=(0, 8))
        self.conn_hint.grid_remove()

    def _build_output_group(self, parent: tk.Frame) -> None:
        group = tk.LabelFrame(parent, text="输出")
        group.pack(fill="x")
        group.columnconfigure(1, weight=1)

        tk.Label(group, text="备份保存位置：").grid(row=0, column=0, sticky="e", padx=(8, 0), pady=3)
        self.card_root_label = tk.Label(group, text=CARD_EMPTY_TEXT, anchor="w")
        self.card_root_label.grid(row=0, column=1, sticky="we", padx=4, pady=3)
        tk.Label(group, text=BACKUP_ROOT_SETTINGS_HINT, anchor="w",
                 foreground=EMPTY_HINT_FG).grid(row=0, column=2, sticky="w", padx=(0, 8))
        self.backup_root_var.trace_add("write", lambda *_a: self._render_backup_root())

        fmt_row = tk.Frame(group)
        fmt_row.grid(row=1, column=0, columnspan=3, sticky="w", padx=8, pady=(2, 8))
        self.mbox_var = tk.BooleanVar(value=False)
        self.eml_var = tk.BooleanVar(value=True)
        self._register(tk.Checkbutton(fmt_row, text="mbox 输出", variable=self.mbox_var,
                                      command=self._on_format_change)).pack(side="left")
        self._register(tk.Checkbutton(fmt_row, text="eml 输出", variable=self.eml_var,
                                      command=self._on_format_change)).pack(side="left", padx=(12, 0))

    def _build_folder_group(self, parent: tk.Frame) -> None:
        group = tk.LabelFrame(parent, text="邮箱文件夹")
        # 面板右边不留白：右边界与下方区域（邮件列表/运行日志/停止按钮）共用同一条线，
        # 即"内容右边距 == 内容左边距（8px）"。单独再留 8px 会让面板比下方窄一块。
        group.grid(row=0, column=1, sticky="nsew")
        self.folder_group = group
        group.columnconfigure(0, weight=1)
        group.rowconfigure(1, weight=1)

        toolbar = tk.Frame(group)
        toolbar.grid(row=0, column=0, columnspan=2, sticky="we", padx=6, pady=(2, 0))
        self._register(tk.Button(toolbar, text="全选", width=7, command=lambda: self._set_all_folders(True))).pack(side="left")
        self._register(tk.Button(toolbar, text="清空", width=7,
                                 command=lambda: self._set_all_folders(False))).pack(side="left", padx=(6, 0))
        self._register(tk.Button(toolbar, text="仅收件箱", width=7, command=self._set_inbox_only)).pack(side="left", padx=(6, 0))

        # 用嵌套 Frame 手搭树形，不用 ttk.Treeview：Treeview 的原生 <Button-1> 把整行都当展开/折叠
        # 目标，单行放不下"点箭头=展开、点名字=勾选"两个动作。这里箭头和名称都是真控件，命中区明确。
        self.folder_canvas = tk.Canvas(group, highlightthickness=0, height=200)
        self.folder_canvas.grid(row=1, column=0, sticky="nsew", padx=(6, 0), pady=4)
        # 滚条紧贴面板内沿（右边不留白），与下方"邮件列表/运行日志"页签里的滚条贴边方式一致
        folder_scroll = ttk.Scrollbar(group, orient="vertical", command=self.folder_canvas.yview)
        folder_scroll.grid(row=1, column=1, sticky="ns", pady=4)
        self.folder_scroll = folder_scroll
        self.folder_canvas.configure(yscrollcommand=folder_scroll.set)
        self.folder_inner = tk.Frame(self.folder_canvas)
        self._folder_win = self.folder_canvas.create_window((0, 0), window=self.folder_inner, anchor="nw")
        self.folder_inner.bind("<Configure>", self._on_folder_inner_configure)
        self.folder_canvas.bind("<Configure>", lambda e: self.folder_canvas.itemconfigure(self._folder_win, width=e.width))
        self.folder_ph = self._empty_hint(group, FOLDER_PLACEHOLDER_TEXT, 1, 0)

        # 滚轮只有一个入口：绑在 root 上（root 的路径是主窗口内所有控件的 toplevel bindtag）。
        # 绑在画布上只有画布自己那一小条空白生效——Tk 把事件投给指针正下方的控件、不冒泡，
        # 行里的 Label/Frame 不会转给画布，于是面板 90% 面积滚不动。作用域仅限主窗口：
        # 工具气泡与原生对话框的 toplevel tag 是它们自己的路径，不受影响。
        self.root.bind("<MouseWheel>", self._on_wheel)
        # 面板内的滚条得单独接管：TScrollbar 的类绑定自带一格 4 units（实测≈98px，比内容区快），
        # 控件级绑定先于类绑定执行，靠返回 "break" 把它压掉，全场节奏才一致。
        folder_scroll.bind("<MouseWheel>", self._on_wheel)

    # ------------------------------------------------------- 页面外壳与设置页

    def _build_menubar(self) -> None:
        """顶部菜单栏：备份 / 设置两项直接切页（tk 原生菜单栏，不做下拉、不弹新窗口）。

        Note:
            顶层可点项（radiobutton）在 Windows 上的呈现按「当前页带选中点」读，与主流应用的
            导航栏同义；以后加页面只需往 PAGE_LABELS 里加一行。
        """
        self._page_var = tk.StringVar(value=PAGE_BACKUP)
        self.menubar = tk.Menu(self.root, tearoff=False)
        for name, label in PAGE_LABELS:
            self.menubar.add_radiobutton(label=label, variable=self._page_var, value=name,
                                         command=lambda page=name: self._show_page(page))
        self.root.configure(menu=self.menubar)

    def _show_page(self, name: str) -> None:
        """切页唯一入口：抬升目标页并同步菜单栏选中态；进设置页时按内存偏好回填控件。"""
        if name == PAGE_SETTINGS:
            self._settings_action_var.set(self._close_action())
            self._show_settings_section(self._settings_section)
            self.settings_page.tkraise()
        else:
            name = PAGE_BACKUP
            self.backup_page.tkraise()
        if self._page_var.get() != name:
            self._page_var.set(name)

    def _build_settings_page(self) -> None:
        """设置页：左分类栏（表驱动 Listbox）+ 右详情区，四边与备份页共用同一条 8px 基线。"""
        outer = tk.Frame(self.settings_page)
        # 上/下都留 8：备份页的下边距由运行区自己带（pady=(2, 8)），设置页没有下方内容，
        # 少了这 8px 就会变成「三边等宽、底边贴边」
        outer.pack(fill="both", expand=True, padx=8, pady=8)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(0, weight=1)

        nav = self._panel_style_frame(outer)
        nav.configure(width=SETTINGS_SECTION_LIST_WIDTH)
        nav.pack_propagate(False)  # 固定宽：分类名/条目再长也不挤动右栏（右栏吃剩余宽度）
        nav.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        self.settings_nav = tk.Listbox(nav, exportselection=False, activestyle="none",
                                       background="#ffffff", highlightthickness=0, borderwidth=0)
        self.settings_nav.pack(fill="both", expand=True)
        for _key, title in SETTINGS_SECTIONS:
            self.settings_nav.insert("end", title)
        self.settings_nav.bind("<<ListboxSelect>>", self._on_settings_section_select)

        detail = tk.Frame(outer)
        detail.grid(row=0, column=1, sticky="nsew")
        detail.columnconfigure(0, weight=1)
        detail.rowconfigure(0, weight=1)
        self._settings_sections: dict[str, tk.Frame] = {}
        for key, title in SETTINGS_SECTIONS:
            page = self._panel_style_frame(detail)
            page.grid(row=0, column=0, sticky="nsew")
            self._settings_sections[key] = page
            if key == SETTINGS_ACCOUNTS_SECTION:
                self._build_accounts_settings(page, title)
            elif key == SETTINGS_BACKUP_SECTION:
                self._build_backup_settings(page, title)
            elif key == SETTINGS_WINDOW_SECTION:
                self._build_window_settings(page, title)
        self._settings_section = SETTINGS_SECTIONS[0][0]

    def _build_accounts_settings(self, parent: tk.Frame, title: str) -> None:
        """「账户」分类：左账户列表 + 新增/删除，右连接设置编辑表单 + 保存。

        Note:
            保存是显式动作（含校验与落盘）：表单里可能只填了一半，逐键生效会把半成品写进设置。
        """
        body = tk.Frame(parent)
        body.pack(fill="both", expand=True, padx=10, pady=10)
        tk.Label(body, text=title, anchor="w",
                 font=_bold_font(tkfont.nametofont("TkDefaultFont"))).pack(fill="x")
        tk.Label(body, text=ACCOUNTS_HINT_TEXT, justify="left", anchor="w", foreground=EMPTY_HINT_FG,
                 wraplength=TIP_WRAP).pack(fill="x", pady=(4, 0))

        wrap = tk.Frame(body)
        wrap.pack(fill="both", expand=True, pady=(10, 0))
        nav = self._panel_style_frame(wrap)
        nav.configure(width=SETTINGS_SECTION_LIST_WIDTH)
        nav.pack_propagate(False)
        nav.pack(side="left", fill="y")
        tools = tk.Frame(nav)
        tools.pack(side="bottom", fill="x", padx=4, pady=4)
        tk.Button(tools, text="新增", width=7, command=self._on_account_add).pack(side="left")
        tk.Button(tools, text="删除", width=7, command=self._on_account_delete).pack(side="left", padx=(6, 0))
        self.account_edit_list = tk.Listbox(nav, exportselection=False, activestyle="none",
                                            background="#ffffff", highlightthickness=0, borderwidth=0)
        self.account_edit_list.pack(fill="both", expand=True)
        self.account_edit_list.bind("<<ListboxSelect>>", self._on_account_edit_select)

        form = tk.Frame(wrap)
        form.pack(side="left", fill="both", expand=True, padx=(10, 0))
        form.columnconfigure(1, weight=1)
        self._acct_name_var = tk.StringVar()
        self._acct_user_var = tk.StringVar()
        self._acct_server_var = tk.StringVar()
        self._acct_ssl_var = tk.BooleanVar(value=DEFAULT_ACCOUNT_SSL)
        self._acct_port_var = tk.StringVar(value=DEFAULT_ACCOUNT_PORT)
        self._acct_password_var = tk.StringVar()
        form_entries = [
            self._account_form_row(form, 0, "名称：", self._acct_name_var),
            self._account_form_row(form, 1, "邮箱地址：", self._acct_user_var),
        ]
        server_entry = self._account_form_row(form, 2, "收件服务器：", self._acct_server_var)
        form_entries.append(server_entry)

        ssl_row = tk.Frame(form)
        ssl_row.grid(row=3, column=0, columnspan=2, sticky="w", pady=3)
        tk.Checkbutton(ssl_row, text="SSL", variable=self._acct_ssl_var,
                       command=self._on_acct_ssl_toggle).pack(side="left")
        tk.Label(ssl_row, text="端口：").pack(side="left", padx=(16, 4))
        port_entry = tk.Entry(ssl_row, textvariable=self._acct_port_var, width=7)
        port_entry.pack(side="left")
        form_entries.append(port_entry)

        tk.Label(form, text="密码：").grid(row=4, column=0, sticky="e", padx=(0, 4), pady=3)
        self._acct_password_entry = tk.Entry(form, textvariable=self._acct_password_var, show="*")
        self._acct_password_entry.grid(row=4, column=1, sticky="we", pady=3)
        tk.Label(form, text="留空则不修改已保存的密码", anchor="w", foreground=EMPTY_HINT_FG).grid(
            row=5, column=1, sticky="w", pady=(0, 6))
        tk.Button(form, text="保存", width=10, command=self._on_account_save).grid(row=6, column=1, sticky="w")

        for entry in [*form_entries, self._acct_password_entry]:
            for key in ("<Return>", "<KP_Enter>"):
                entry.bind(key, lambda _e: self._on_account_save())
            entry.bind("<Button-3>", self._on_entry_context)
        self._account_form_entries = [*form_entries, self._acct_password_entry]

    def _account_form_row(self, form: tk.Frame, row: int, caption: str, var: tk.StringVar) -> tk.Entry:
        """连接设置表单的一行：右对齐标签 + 吃满剩余宽度的输入框。"""
        tk.Label(form, text=caption).grid(row=row, column=0, sticky="e", padx=(0, 4), pady=3)
        entry = tk.Entry(form, textvariable=var)
        entry.grid(row=row, column=1, sticky="we", pady=3)
        return entry

    def _build_backup_settings(self, parent: tk.Frame, title: str) -> None:
        """「备份」分类：全局唯一的备份保存位置（所有账户共用，见 ADR 0010）。"""
        body = tk.Frame(parent)
        body.pack(fill="both", expand=True, padx=10, pady=10)
        tk.Label(body, text=title, anchor="w",
                 font=_bold_font(tkfont.nametofont("TkDefaultFont"))).pack(fill="x")

        row = tk.Frame(body)
        row.pack(fill="x", pady=(10, 0))
        tk.Label(row, text="备份保存位置：").pack(side="left")
        self.backup_root_entry = tk.Entry(row, textvariable=self.backup_root_var)
        self.backup_root_entry.pack(side="left", fill="x", expand=True, padx=4)
        tk.Button(row, text="浏览…", command=self._on_browse_backup_root).pack(side="left")
        self.backup_root_entry.bind("<Button-3>", self._on_entry_context)
        tk.Label(body, text=BACKUP_ROOT_HINT_TEXT, justify="left", anchor="w", foreground=EMPTY_HINT_FG,
                 wraplength=TIP_WRAP).pack(fill="x", pady=(8, 0))

    def _build_window_settings(self, parent: tk.Frame, title: str) -> None:
        """「窗口与后台」分类的详情：关闭行为三态 + 说明文字。

        Note:
            控件不进 _register 名单：关闭行为不影响正在跑的备份，运行中也不必跟着禁用。
        """
        body = tk.Frame(parent)
        body.pack(fill="both", expand=True, padx=10, pady=10)
        tk.Label(body, text=title, anchor="w",
                 font=_bold_font(tkfont.nametofont("TkDefaultFont"))).pack(fill="x")
        self._settings_action_var = tk.StringVar(value=DEFAULT_CLOSE_ACTION)
        for value, label in CLOSE_ACTION_LABELS:
            tk.Radiobutton(body, text=label, value=value, variable=self._settings_action_var,
                           anchor="w", command=self._on_close_action_change).pack(fill="x", pady=(6, 0))
        tk.Label(body, text=SETTINGS_HINT_TEXT, justify="left", anchor="w", foreground=EMPTY_HINT_FG,
                 wraplength=TIP_WRAP).pack(fill="x", pady=(14, 0))

    def _on_settings_section_select(self, _event=None) -> None:
        selection = self.settings_nav.curselection()
        if selection:
            self._show_settings_section(SETTINGS_SECTIONS[selection[0]][0])

    def _show_settings_section(self, key: str) -> None:
        """切设置分类的唯一入口：同步左栏选中态与右栏详情（表驱动，加分类只加一行）。"""
        keys = [section_key for section_key, _title in SETTINGS_SECTIONS]
        if key not in keys:
            key = keys[0]
        index = keys.index(key)
        self._settings_sections[key].tkraise()
        if self.settings_nav.curselection() != (index,):
            self.settings_nav.selection_clear(0, "end")
            self.settings_nav.selection_set(index)
        self.settings_nav.activate(index)
        self._settings_section = key

    def _on_close_action_change(self) -> None:
        """设置页单选钮变更：只更新内存，落盘沿用「关闭/隐藏/退出时统一保存一次」（ADR 0008）。"""
        action = parse_close_action(self._settings_action_var.get())
        self._settings_action_var.set(action)
        if action != self._close_action():
            self._ui_settings[CLOSE_ACTION_KEY] = action
            self.log.info("关闭行为偏好改为：%s", action)

    def _close_action(self) -> str:
        """当前关闭行为偏好：非法或缺失回退默认值（每次询问）。"""
        return parse_close_action(self._ui_settings.get(CLOSE_ACTION_KEY))

    def _tray_tooltip(self) -> str:
        return TRAY_TOOLTIP_RUNNING if self.running else TRAY_TOOLTIP_IDLE

    def _sync_tray(self) -> None:
        """把常驻托盘图标的状态追平界面状态：tooltip 与菜单「停止备份」的可用性。

        Note:
            图标本身不在这里挂出/收掉——它随程序启动常驻、只在 `_quit()` 里移除；这里只同步
            随运行状态变化的那两项。托盘缺席时静默跳过。
        """
        if self._tray is None:
            return
        self._tray.set_tooltip(self._tray_tooltip())
        self._tray.set_running(self.running)

    # --------------------------------------------------------------- run area

    def _panel_style_frame(self, parent: tk.Widget) -> tk.Frame:
        """生成页签的外包边框：与「邮箱文件夹」面板同款。

        Args:
            parent: 边框所属的父容器（页签页）。

        Returns:
            groove 边框、与面板同值 borderwidth 的 Frame，两处观感永远一致。

        Note:
            面板先于本区构建，这里能安全读到它的值。
        """
        return tk.Frame(parent, relief="groove",
                        borderwidth=int(self.folder_group.cget("borderwidth")))

    def _build_run_area(self, parent: tk.Frame) -> None:
        run = tk.Frame(parent)
        # 左右内边距是 7/5 而不是 8：notebook 客户区在 vista 主题里相对自身左缩 1px、右缩 3px，
        # 先把它补掉，页签包裹边框的左右线才能与上方面板线、下方按钮行落在同一条 8px 基线上
        # （探针实测，issues/15）；按钮行与状态行再用 padx=(1, 3) 把自己的 8px 边距补回去。
        # 上边距与配置区的下边距合起来 = 8px，与各面板之间那条同宽。
        run.pack(fill="both", expand=True, padx=(7, 5), pady=(4, 8))

        # 两行状态条都先于 Notebook 以 side=bottom 打包，Notebook 的 expand 才不会挤掉它们。
        # msg_bar 先打包所以落在最底。初始只显示一行（未开始备份时没有进度与状态可显示），
        # 首次开始备份时 _ensure_status_rows 放出进度条与第二行，之后不再收回。
        self._msg_bar = msg_bar = tk.Frame(run)
        msg_bar.pack(side="bottom", fill="x", pady=(4, 0), padx=(1, 3))
        self._bar = bar = tk.Frame(run)
        bar.pack(side="bottom", fill="x", pady=(6, 0), padx=(1, 3))
        self.notebook = ttk.Notebook(run)
        self.notebook.pack(fill="both", expand=True)
        # notebook 会在自己客户区四周自绘 1px 淡边线（vista 主题原生绘制，颜色改不了，issues/15）；
        # 页签包裹边框在客户区内侧，两者并排就是用户看到的「两条边框线」。用两条背景色窄条把
        # 客户区的 1px/3px 内缩带盖掉，页签里只剩包裹边框那一条与面板完全重合的线。
        self.tab_edge_left = edge_left = tk.Frame(self.notebook)
        edge_left.place(x=0, y=0, width=1, relheight=1.0)
        self.tab_edge_right = edge_right = tk.Frame(self.notebook)
        edge_right.place(relx=1.0, x=0, y=0, width=3, relheight=1.0, anchor="ne")
        # 底边还有一条 3px 内缩带：它的淡线压在包裹边框外侧，底边就多出一道「突出」的线，
        # 一并盖掉（用户复核，issues/15）
        self.tab_edge_bottom = edge_bottom = tk.Frame(self.notebook)
        edge_bottom.place(relx=0.0, rely=1.0, x=0, y=0, relwidth=1.0, height=3, anchor="sw")

        list_tab = self.list_tab = tk.Frame(self.notebook)
        self.notebook.add(list_tab, text="邮件列表")
        list_tab.columnconfigure(0, weight=1)
        list_tab.rowconfigure(0, weight=1)
        # 外框与「邮箱文件夹」面板同款（groove）；白色表格自己带凹陷边（白底 sunken：左/上 153
        # 阴影、右/下 230 高光），贴着外框内沿、不留灰边距——右缘那条 230 就是滚条左缘 1px 白色
        # 反光条的对比（用户口径，issues/15）。页签容器只做占位。
        list_frame = self.list_frame = self._panel_style_frame(list_tab)
        list_frame.grid(row=0, column=0, sticky="nsew")
        list_frame.columnconfigure(0, weight=1)
        list_frame.rowconfigure(0, weight=1)
        # 白底是为了让凹陷边的深浅与日志板同色：Tk 经典 3D 边框的色阶由控件背景推出。
        # 树自己的 vista field 边框压平（派生样式只留 treearea，全局 Treeview 布局不动）。
        ttk.Style().layout("Mail.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        tree_wrap = self.tree_wrap = tk.Frame(list_frame, relief="sunken", borderwidth=1,
                                              background="#ffffff")
        tree_wrap.grid(row=0, column=0, sticky="nsew")
        self.tree = ttk.Treeview(tree_wrap, style="Mail.Treeview",
                                 columns=("from", "subject", "when", "folder", "size"), show="headings")
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
        self._load_column_config()
        self.tree.bind("<Configure>", self._on_tree_configure, add="+")
        self.tree.tag_configure(ZEBRA_TAG, background=ZEBRA_BG)
        self._tree_resize = None
        self.tree.bind("<Button-1>", self._on_tree_resize_start, add="+")
        self.tree.bind("<B1-Motion>", self._on_tree_resize_motion, add="+")
        self.tree.bind("<ButtonRelease-1>", self._on_tree_resize_end, add="+")
        tree_scroll = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree_scroll = tree_scroll
        self.tree.configure(yscrollcommand=tree_scroll.set)
        # 树只用 place 铺满白板并往左上各溢出 1px、右下各多铺 1px：左/上/下三条凹陷线被树自己
        # 盖住（与外框合成一条线），只剩右边那条 230 高光。滚条紧贴白板右缘，反光条正对着它。
        # 宽多铺 1px 是为了吃掉顶边最后一个像素（不然会在白板右上角留下一个深色点）。
        self.tree.place(x=-1, y=-1, relwidth=1.0, relheight=1.0, width=1, height=2)
        tree_scroll.grid(row=0, column=1, sticky="ns")
        self.list_ph = self._empty_hint(list_frame, LIST_PLACEHOLDER_TEXT, 0, 0)
        # 选中高亮与输入框焦点共用同一个「窗口级单入口」，与滚轮同一套路：绑在 root 上（root 的
        # 路径是主窗口内所有控件的 toplevel bindtag），任何控件上的左键都流经这里。点行时 Treeview
        # 的类绑定先完成换选，本入口识别出「是行」就放行；其余落点（列表空白、列标题、配置区……）
        # 一律清掉选中。焦点收尾只看一种情况：点击落在不接焦点的控件（标签、空白、按钮、文件夹行、
        # 禁用中的输入框）时，把焦点从输入框收归主窗口——点输入框/列表/日志文本时类绑定已转走焦点。
        self.root.bind("<Button-1>", self._on_click_clear_selection_and_focus)
        # 右键与左键互不干扰：点在行上弹「复制单元格」菜单（先按 Windows 惯例选中该行），
        # 空白与列标题不弹也不动选中——判断与副作用都在 _prepare_tree_popup 里。
        self.tree.bind("<Button-3>", self._on_tree_context)

        log_tab = self.log_tab = tk.Frame(self.notebook)
        self.notebook.add(log_tab, text="运行日志")
        log_tab.columnconfigure(0, weight=1)
        log_tab.rowconfigure(0, weight=1)
        # 与邮件列表同一套外包边框：工具栏一起包进去，与文件夹面板「工具钮在框内」同构
        log_frame = self.log_frame = self._panel_style_frame(log_tab)
        log_frame.grid(row=0, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)
        log_toolbar = tk.Frame(log_frame)
        # 按钮与板面同一竖线；左/上留白与板面一样按 4px 统一（用户口径：按下边距统一）
        log_toolbar.grid(row=0, column=0, columnspan=2, sticky="we", padx=4, pady=(4, 0))
        tk.Button(log_toolbar, text="打开日志文件夹", command=self._on_open_log_dir).pack(side="left")
        # 日志板保留 Tk 自带的下沉边框：那圈凹陷边缘是「白色板面」的视觉标识，压平后白底与
        # 灰底直接相接很违和（用户复核，issues/15）。四周留出灰边距，免得这圈边缘与包裹边框
        # 贴成双线；右侧贴住滚条、不留缝。边距一律 4px：以原本最短的下边距为准（用户口径）。
        self.log_text = tk.Text(log_frame, state="disabled", wrap="none", background="#ffffff")
        self.log_text.grid(row=1, column=0, sticky="nsew", padx=(4, 0), pady=4)
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_scroll = log_scroll
        log_scroll.grid(row=1, column=1, sticky="ns", pady=4)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        # 日志只读但可选中：右键「复制全部」把整份日志放进剪贴板，排障转发用
        self.log_text.bind("<Button-3>", self._on_log_context)

        # 按钮 side=right 钉在右端：进度条隐藏时它们也在右边，放出后位置纹丝不动
        self.count_var = tk.StringVar(value=self._count_full)
        self.count_label = tk.Label(bar, textvariable=self.count_var, anchor="w")
        self.count_label.pack(side="left")
        # 两个按钮都不给 -width：带图标的按钮里该选项按像素解释（纯文字时才按字符数），
        # 写死就既不随 DPI 也不随文案长度走——实测 width=10 会把带图标的按钮塌到 18px。
        # 交给 Tk 按「图标 + 文字」的内在尺寸算，缩放与文案变化都自动跟得上。
        self.stop_btn = tk.Button(bar, text=STOP_BTN_LABEL, state="disabled", command=self._on_stop)
        self.stop_btn.pack(side="right")
        self.start_btn = tk.Button(bar, text=START_BTN_LABEL, command=self._on_start)
        self.start_btn.pack(side="right", padx=(0, 6))
        self._attach_button_icons()
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

    def _attach_button_icons(self) -> None:
        """给两个按钮挂上代码画的彩色图标（开始=绿三角、停止=红方块）。

        Note:
            位图必须由 Python 侧持引用：Tkinter 不持引用，PhotoImage 一旦被 GC，按钮上的图标
            会变空白。blank_icon 是加载态的透明占位（见 _blank_icon），同样要持引用。
        """
        size = _icon_size()
        self.start_icon = _play_icon(PLAY_ICON_COLOR, size)
        self.stop_icon = _stop_icon(STOP_ICON_COLOR, size)
        self.blank_icon = _blank_icon(size)
        self.start_btn.configure(image=self.start_icon, compound="left")
        self.stop_btn.configure(image=self.stop_icon, compound="left")

    # ------------------------------------------------------- connect handlers

    def _visible_state(self) -> dict | None:
        """当前可见账户的状态字典；没有账户时返回 None（账户存在时按需建立）。"""
        if self._selected_account_id is None or self._find_account(self._selected_account_id) is None:
            return None
        return self._state(self._selected_account_id)

    def _state(self, account_id: str) -> dict:
        """取（必要时建立）某账户的独立状态：连接、文件夹树、邮件行、计数与进度。"""
        state = self._states.get(account_id)
        if state is None:
            state = {"connect_state": "idle",       # idle | connecting | connected | stale
                     "snapshot": None,              # (server, user, password_dpapi) 连接当刻的值
                     "folder_rows": [], "open_keys": None,  # open_keys=None → 首次全部展开
                     "messages": [], "row_index": 0,
                     "server_total": 0, "pending_total": 0, "backed": 0,
                     "count_text": "尚未开始备份", "status_text": "", "status_error": False,
                     "progress": None, "result": None}
            self._states[account_id] = state
        return state

    def _find_account(self, account_id: str | None) -> dict | None:
        for account in self.accounts:
            if account["id"] == account_id:
                return account
        return None

    def _account_password(self, account: dict) -> str:
        """解出账户密码明文；没存过或解不开（换机器/换账户）都按空串处理。"""
        cipher = account.get("password_dpapi")
        if isinstance(cipher, str) and cipher:
            return _dpapi_decrypt(cipher) or ""
        return ""

    def _account_snapshot(self, account: dict) -> tuple[str, str, str]:
        """连接参数快照（密码取密文）：与快照不同即「文件夹快照过期」。"""
        cipher = account.get("password_dpapi")
        return (str(account.get("server", "")), str(account.get("user", "")),
                cipher if isinstance(cipher, str) else "")

    def _account_credentials(self, account: dict, *, warn: bool = True) -> dict | None:
        """账户记录 → 一次连接所需的取值；缺项时提示（warn=False 则静默）并返回 None。"""
        server = str(account.get("server", "")).strip()
        user = str(account.get("user", "")).strip()
        password = self._account_password(account)
        if not server:
            if warn:
                messagebox.showwarning("信息不完整", "请填写收件服务器。", parent=self.root)
            return None
        if not user:
            if warn:
                messagebox.showwarning("信息不完整", "请填写邮箱地址。", parent=self.root)
            return None
        if not password:
            if warn:
                messagebox.showwarning("信息不完整",
                                       "该账户还没有密码，请在设置页的「账户」里填写并保存。", parent=self.root)
            return None
        try:
            port = int(str(account.get("port", DEFAULT_ACCOUNT_PORT)).strip())
            if not 0 < port < 65536:
                raise ValueError
        except ValueError:
            if warn:
                messagebox.showwarning("端口无效", "端口必须是 1-65535 的整数。", parent=self.root)
            return None
        return {"server": server, "user": user, "password": password,
                "ssl": bool(account.get("ssl", DEFAULT_ACCOUNT_SSL)), "port": port,
                "mbox": bool(account.get("mbox", False)), "eml": bool(account.get("eml", True))}

    def _on_connect(self) -> None:
        if str(self.connect_btn.cget("state")) != "normal":
            # 连接中/已连接/备份运行时按钮被禁用；门控不挡住的话会起第二条连接线程。
            return
        account = self._find_account(self._selected_account_id)
        if account is None:
            return
        v = self._account_credentials(account)
        if v is None:
            return
        state = self._state(account["id"])
        state["connect_state"] = "connecting"
        self.connect_state = "connecting"
        self._apply_connect_state()
        self._update_start_state()
        threading.Thread(target=self._connect_worker,
                         args=(account["id"], v, self._account_snapshot(account)), daemon=True).start()

    def _core_config(self, v: dict, basedir: Path | None = None, eml_dir: Path | None = None) -> core.Config:
        return core.Config(overwrite=False, usessl=v["ssl"], thunderbird=False, nospinner=True,
                           basedir=basedir, icloud=False, quiet=False, user=v["user"], server=v["server"],
                           password=v["password"], timeout=60, port=v["port"], eml_dir=eml_dir)

    def _connect_worker(self, account_id: str, v: dict, snapshot: tuple[str, str, str]) -> None:
        """连接线程入口：登录并拉取文件夹清单，结果经事件队列回主线程。"""
        log = logging.getLogger("imapbackup")
        cfg = self._core_config(v)
        try:
            server = core.connect_and_login(cfg, log)
            try:
                names = core.get_names(server, False, True, quiet=False, log=log)
            finally:
                with contextlib.suppress(Exception):
                    server.logout()
            self.events.put(("folders_loaded", (account_id, names, snapshot)))
        except SystemExit as e:
            self.events.put(("connect_failed", (account_id, str(e))))
        except Exception as e:
            log.exception("连接失败")
            self.events.put(("connect_failed", (account_id, f"{type(e).__name__}: {e}")))

    def _on_folders_loaded(self, account_id: str, names: list[tuple], snapshot: tuple[str, str, str]) -> None:
        """连接成功：为该账户建文件夹行、记下连接参数快照，可见时才动界面。"""
        account = self._find_account(account_id)
        if account is None:
            return
        state = self._state(account_id)
        if account_id == self._selected_account_id:
            self._sync_visible_folder_choices()  # 重连前先把界面上的勾选收回账户，重建不丢改动
        self._fill_folder_rows(state, account, names)
        state["snapshot"] = snapshot
        state["connect_state"] = "connected"
        self._refresh_stale(account_id)  # 连接期间账户若被改过，立刻转 stale（快照口径）
        self.log.info("账户 %s：已加载 %d 个邮箱文件夹", account_display(account), len(names))
        if account_id == self._selected_account_id:
            self._render_folder_tree(state)
            self.connect_state = state["connect_state"]
            self._apply_connect_state()
            self._update_start_state()

    def _fill_folder_rows(self, state: dict, account: dict, names: list[tuple]) -> None:
        """按账户记录的勾选构建文件夹行：现存勾选保留，缺省或全部失效时默认仅 INBOX。"""
        available = {name[0] for name in names}
        persisted = {name for name in account.get("folders", []) if name in available}
        checked = persisted or {"INBOX"}
        rows: list[dict] = []
        for foldername, filename, eml_relpath, display in names:
            path = tuple(eml_relpath.split("/"))
            rows.append({"foldername": foldername, "filename": filename,
                         "eml_relpath": eml_relpath, "display": display, "path": path,
                         "label": last_folder_segment(display, path),
                         "var": tk.BooleanVar(value=foldername in checked),
                         "check_btn": None})
        state["folder_rows"] = sorted(rows, key=lambda r: r["path"])

    def _render_folder_tree(self, state: dict) -> None:
        """把某账户的文件夹行渲染成勾选树（切账户与连接完成共用）。

        Note:
            行本身留在状态字典里（勾选值随行存活），这里只重建控件；展开态按 open_keys 还原，
            None 表示首次加载——全部展开。重建只影响可见账户，别的账户的树不占控件。
        """
        rows = list(state["folder_rows"])
        self.folder_rows, self._folder_nodes, self._folder_clickables = [], {}, []
        for child in self.folder_inner.winfo_children():
            child.destroy()

        real: dict[tuple, list[dict]] = {}
        for row in rows:
            real.setdefault(row["path"], []).append(row)

        keys = sorted({row["path"][:i] for row in rows for i in range(1, len(row["path"]) + 1)},
                      key=lambda k: (len(k), k))
        with_kids = {k for k in keys if any(o != k and o[:len(k)] == k for o in keys)}
        for key in keys:  # 父节点必然先于子节点建成
            self._make_folder_node(key, real.get(key), with_kids, state["open_keys"])

        self.folder_rows.sort(key=lambda r: r["path"])
        state["folder_rows"] = self.folder_rows
        # 空树不改展开记忆：open_keys 保持原值（None 表示「下次加载全展开」），
        # 免得空态渲染把 None 变成空集合、下一次加载整棵树都是收起的
        if self.folder_rows:
            state["open_keys"] = {key for key, node in self._folder_nodes.items() if node["open"]}
        self._render_folder_checks()
        self._wheel_accum = 0   # 换了棵树，上一棵树没攒满一格的滚轮余量作废
        # 行距的同步挂在前面的 folder_inner <Configure> 上：此刻新建控件的几何还没结算，
        # 只有布局铺完那次 Configure 量到的行距才是真的（重建时量到的是 Tk 占位值）。空树
        # 不会再触发那个事件，所以这里显式归零——不然上次的行距会留在没内容的树上。
        if not self.folder_rows:
            self._sync_folder_row_increment()
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
        frame.pack(anchor="w", fill="x", pady=FOLDER_ROW_PAD_Y)
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

    def _empty_hint(self, parent: tk.Widget, text: str, row: int, column: int) -> tk.Label:
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
            return  # 运行中配置区锁定；标签只能置灰，点击要靠这里挡住
        node = self._folder_nodes[key]
        node["open"] = not node["open"]
        self._pack_kids(node)
        node["arrow"].configure(text=ARROW_OPEN if node["open"] else ARROW_CLOSED)
        state = self._visible_state()
        if state is not None:
            state["open_keys"] = {k for k, n in self._folder_nodes.items() if n["open"]}

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

    def _on_folder_inner_configure(self, _event=None) -> None:
        """树区内容重排后（首次铺满、加载完文件夹、窗口缩放）同步滚动范围与行距。"""
        self.folder_canvas.configure(scrollregion=self.folder_canvas.bbox("all"))
        self._sync_folder_row_increment()

    def _measure_folder_row_pitch(self) -> int:
        """量文件夹树的行距(px)。

        Returns:
            任取一对相邻行容器的真实间距（含 pady、随 DPI 与字体走）；量不到时为 0。

        Note:
            行容器是 folder_inner 的顶层节点框，也是各节点 kids 框里的子节点框——先看主宿主，
            只有一个顶层节点时（都挂在那一个父节点下面）退到它的 kids 框里量。新建控件的请求
            尺寸与位置要等布局铺完才可信，重建那一刻量到的是 Tk 的占位值，这时就量不到。
        """
        hosts = [self.folder_inner] + [n["kids"] for n in self._folder_nodes.values()]
        for host in hosts:
            frames = host.winfo_children()
            # 折叠节点的 kids 框没上屏，里头的控件还留着折叠前的位置，不能拿来量
            if len(frames) > 1 and (host is self.folder_inner or host.winfo_ismapped()):
                pitch = frames[1].winfo_rooty() - frames[0].winfo_rooty()
                if pitch > 0:
                    return pitch
        return 0

    def _sync_folder_row_increment(self) -> None:
        """让画布的 1 unit = 1 行。

        滚轮一格（`FOLDER_WHEEL_ROWS` 行）才落在行边界上，滚条箭头一格也正好一行。

        Note:
            量不到行距（还没有行 / 布局没铺完）时归 0，单位回到 Tk 默认的窗口高的 1/10。
            只在与画布现值不同时才写，免得布局事件里反复做无谓的配置。
        """
        pitch = self._measure_folder_row_pitch()
        if pitch != int(self.folder_canvas.cget("yscrollincrement")):
            self.folder_canvas.configure(yscrollincrement=pitch)

    # -------------------------------------------- 账户列表、详情与设置页编辑

    def _refresh_account_list(self) -> None:
        """重画备份页左栏账户列表并保持选中（新增/删除/改名后调用）。

        Note:
            勾选集合在这里同步到账户表：删掉的账户不再占勾选，上一次画列表时还没有的账户
            默认勾上——「勾」是默认态，用户只在行上表达「不勾」。
        """
        previous = set(self._account_rows)
        ids = {account["id"] for account in self.accounts}
        self._checked_accounts = (self._checked_accounts & ids) | (ids - previous)
        self._account_rows = {}
        for child in self.account_list.winfo_children():
            child.destroy()
        for account in self.accounts:
            self._make_account_row(account)
        self._render_account_rows()
        self._update_start_state()
        # 行数变了就重算滚条去留，但要等布局把新的请求尺寸算出来——当场量到的还是旧值
        self.account_canvas.after_idle(self._sync_account_scroll)

    def _on_account_canvas_configure(self, event) -> None:
        """画布尺寸变化：内嵌列表跟着变宽（行吃满宽度），并重算滚条去留。"""
        self.account_canvas.itemconfigure(self._account_win, width=event.width)
        self._sync_account_scroll()

    def _sync_account_scroll(self) -> None:
        """同步账户列表的滚动状态：画布单位 = 一行，内容装不下才把滚条挂出来。

        Note:
            比较内容框的请求高度与画布实际高度：账户增删、窗口缩放都会跑到这里。挂出滚条
            本身又会让画布变窄、再触发一次 <Configure>，第二次进来状态已稳定，不会来回抖。
        """
        self.account_canvas.configure(scrollregion=self.account_canvas.bbox("all"))
        pitch = self._measure_account_row_pitch()
        if pitch != int(self.account_canvas.cget("yscrollincrement")):
            self.account_canvas.configure(yscrollincrement=pitch)
        need = self.account_list.winfo_reqheight() > self.account_canvas.winfo_height()
        if need != self._account_scroll_shown:
            self._account_scroll_shown = need
            if need:
                self.account_scroll.grid()
            else:
                self.account_scroll.grid_remove()
                self.account_canvas.yview_moveto(0)   # 收进可视区后回到顶部

    def _measure_account_row_pitch(self) -> int:
        """量账户行的行距(px)：任取一对相邻行，不足两行（或还没铺完布局）时为 0。"""
        frames = self.account_list.winfo_children()
        if len(frames) > 1:
            pitch = frames[1].winfo_rooty() - frames[0].winfo_rooty()
            if pitch > 0:
                return pitch
        return 0

    def _make_account_row(self, account: dict) -> None:
        """画一行：底子 Frame + 复选框 + 名称，后两者的命中区分得开。

        行不是 Listbox 的一行，是为了让复选框只翻勾选、点行的其他地方只切详情——一个控件
        一次只能表达一件事。
        """
        account_id = account["id"]
        frame = tk.Frame(self.account_list, background=ACCOUNT_ROW_BG, cursor="hand2")
        frame.pack(fill="x", padx=2, pady=1)
        var = tk.BooleanVar(value=account_id in self._checked_accounts)
        # 复选框自己给「文字」预留了一圈内边距（约 7px），内边距清零、右侧也不留白，名称才贴得近
        check = tk.Checkbutton(frame, variable=var, background=ACCOUNT_ROW_BG,
                               activebackground=ACCOUNT_ROW_BG, highlightthickness=0, borderwidth=0,
                               padx=0, pady=0, cursor="hand2", command=self._sync_account_checks)
        check.pack(side="left", padx=(2, 0))
        label = tk.Label(frame, text=account_display(account), anchor="w", cursor="hand2", padx=0,
                         background=ACCOUNT_ROW_BG, foreground=ACCOUNT_ROW_FG)
        label.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self._account_rows[account_id] = {"frame": frame, "check": check, "label": label, "var": var}
        for widget in (frame, label):
            widget.bind("<Button-1>", lambda _e, _id=account_id: self._on_account_row_click(_id))

    def _render_account_rows(self) -> None:
        """按选中态给行上色：选中行整行系统高亮蓝（与原 Listbox 的选中色是同一个系统色）。"""
        for account_id, row in self._account_rows.items():
            selected = account_id == self._selected_account_id
            background = ACCOUNT_ROW_SELECTED_BG if selected else ACCOUNT_ROW_BG
            row["frame"].configure(background=background)
            row["label"].configure(background=background,
                                   foreground=ACCOUNT_ROW_SELECTED_FG if selected else ACCOUNT_ROW_FG)
            # activebackground 跟着行底色走：不然按复选框时会在蓝行上闪出灰方块
            row["check"].configure(background=background, activebackground=background)

    def _on_account_row_click(self, account_id: str) -> None:
        """点行（复选框以外的地方）= 切换右侧详情，与勾选互不牵连。"""
        if account_id != self._selected_account_id:
            self._switch_account(account_id)

    def _sync_account_checks(self) -> None:
        """把行的勾选收成账户 id 集合（三个工具按钮与单个复选框共用这一个入口）。"""
        self._checked_accounts = {account_id for account_id, row in self._account_rows.items()
                                  if row["var"].get()}
        self._update_start_state()

    def _set_all_account_checks(self, value: bool) -> None:
        for row in self._account_rows.values():
            row["var"].set(value)
        self._sync_account_checks()

    def _invert_account_checks(self) -> None:
        for row in self._account_rows.values():
            row["var"].set(not row["var"].get())
        self._sync_account_checks()

    def _switch_account(self, account_id: str) -> None:
        """切换可见账户：先收当前账户的进度态，再渲染目标账户的整套详情。"""
        self._save_visible_state()
        self._selected_account_id = account_id
        self._ui_settings["selected_account"] = account_id
        account = self._find_account(account_id)
        if account is not None:
            self.log.info("切换到账户 %s", account_display(account))
        self._render_account_detail()
        self._render_account_rows()

    def _save_visible_state(self) -> None:
        """把可见账户的进度条值收进状态字典（计数与状态文案在各自写入点已同步）。"""
        state = self._visible_state()
        if state is not None and self._msg_bar.winfo_manager():
            state["progress"] = (float(self.progress["value"]), float(self.progress["maximum"]))

    def _render_account_detail(self) -> None:
        """把选中账户的整套详情画进右栏：卡片、文件夹树、邮件列表、计数与状态。

        Note:
            切换账户不打断别的账户正在跑的连接或备份——那些事件都带 account_id，只更新各自状态；
            这个入口只负责「可见账户」这一份。
        """
        account = self._find_account(self._selected_account_id)
        state = self._visible_state()
        self._render_card(account)
        if account is None or state is None:
            self.folder_rows, self._folder_nodes, self._folder_clickables = [], {}, []
            for child in self.folder_inner.winfo_children():
                child.destroy()
            self._sync_folder_row_increment()
            self._toggle_hint(self.folder_ph, True)
            self._render_messages(None)
            self.server_total = self.pending_total = self.backed_count = 0
            self.progress.configure(maximum=1, value=0)
            self._set_counts("尚未开始备份")
            self._set_status("", error=False)
            self._set_config_enabled(False)
            self.mbox_var.set(False)
            self.eml_var.set(True)
            self.connect_btn.configure(text=CONNECT_BTN_LABEL, state="disabled")
            self._show_conn_hint("", HINT_GRAY)
            self._update_start_state()
            return
        self._refresh_stale(account["id"])
        self._render_folder_tree(state)
        self._render_messages(state)
        self._restore_run_state(state)
        # 格式复选框按账户还原（command 只在用户点击时触发，这里 set 不会写回账户）
        self.mbox_var.set(bool(account.get("mbox", False)))
        self.eml_var.set(bool(account.get("eml", True)))
        self._set_config_enabled(not self.running)
        self._apply_connect_state()
        self._update_start_state()

    def _render_card(self, account: dict | None) -> None:
        """只读信息卡：账户缺失时全部显示占位符，密码只报「已保存/未设置」。"""
        if account is None:
            values = {"user": CARD_EMPTY_TEXT, "server": CARD_EMPTY_TEXT, "ssl": CARD_EMPTY_TEXT,
                      "port": CARD_EMPTY_TEXT, "password": CARD_EMPTY_TEXT}
        else:
            values = {"user": account.get("user") or CARD_EMPTY_TEXT,
                      "server": account.get("server") or CARD_EMPTY_TEXT,
                      "ssl": "是" if account.get("ssl", DEFAULT_ACCOUNT_SSL) else "否",
                      "port": str(account.get("port") or CARD_EMPTY_TEXT),
                      "password": PASSWORD_SAVED_TEXT if account.get("password_dpapi") else PASSWORD_MISSING_TEXT}
        for key, label in self._card_labels.items():
            label.configure(text=values[key])
        self._render_backup_root()

    def _render_backup_root(self) -> None:
        self.card_root_label.configure(text=self.backup_root_var.get().strip() or DEFAULT_BACKUP_ROOT)

    def _render_messages(self, state: dict | None) -> None:
        """把账户的邮件行重画进邮件列表（切换账户时的一次性重绘）。"""
        self._clear_message_rows()
        if state is None:
            return
        for data in state["messages"]:
            self._row_index += 1
            self._insert_message_row(data, self._row_index)
        state["row_index"] = self._row_index
        self._toggle_hint(self.list_ph, not state["messages"])

    def _restore_run_state(self, state: dict) -> None:
        """把某账户的计数/状态/进度还原到界面（切换账户与运行事件共用同一口径）。"""
        self.server_total = state["server_total"]
        self.pending_total = state["pending_total"]
        self.backed_count = state["backed"]
        progress = state["progress"]
        if progress is None:
            self.progress.configure(maximum=1, value=0)
        else:
            value, maximum = progress
            self.progress.configure(maximum=max(maximum, 1), value=min(value, max(maximum, 1)))
        self._set_counts(state["count_text"])
        self._set_status(state["status_text"], state["status_error"])

    def _refresh_edit_list(self, select_id: str | None) -> None:
        """重画设置页账户列表；选中项回填表单，没有账户时表单清空。"""
        self.account_edit_list.delete(0, "end")
        for account in self.accounts:
            self.account_edit_list.insert("end", account_display(account))
        ids = [account["id"] for account in self.accounts]
        if select_id is None or select_id not in ids:
            select_id = ids[0] if ids else None
        self._editing_account_id = select_id
        if select_id is None:
            self._clear_account_form()
            return
        index = ids.index(select_id)
        self.account_edit_list.selection_clear(0, "end")
        self.account_edit_list.selection_set(index)
        self.account_edit_list.activate(index)
        self._load_account_form(self.accounts[index])

    def _on_account_edit_select(self, _event=None) -> None:
        selection = self.account_edit_list.curselection()
        if not selection:
            return
        account = self.accounts[selection[0]]
        self._editing_account_id = account["id"]
        self._load_account_form(account)

    def _load_account_form(self, account: dict) -> None:
        """把账户记录填进表单；密码框始终留空（留空 = 不修改已保存的密码）。"""
        self._acct_name_var.set(account.get("name", ""))
        self._acct_user_var.set(account.get("user", ""))
        self._acct_server_var.set(account.get("server", ""))
        self._acct_ssl_var.set(bool(account.get("ssl", DEFAULT_ACCOUNT_SSL)))
        self._acct_port_var.set(str(account.get("port", DEFAULT_ACCOUNT_PORT)))
        self._acct_password_var.set("")

    def _clear_account_form(self) -> None:
        self._acct_name_var.set("")
        self._acct_user_var.set("")
        self._acct_server_var.set("")
        self._acct_ssl_var.set(DEFAULT_ACCOUNT_SSL)
        self._acct_port_var.set(DEFAULT_ACCOUNT_PORT)
        self._acct_password_var.set("")

    def _on_acct_ssl_toggle(self) -> None:
        """SSL 勾选联动默认端口：只在端口仍是 993/143 这两个默认值时替换。"""
        if self._acct_port_var.get().strip() in ("993", "143"):
            self._acct_port_var.set("993" if self._acct_ssl_var.get() else "143")

    def _on_account_add(self) -> None:
        """新增一个空账户并立刻落盘：先有账户，再去表单里填连接信息。"""
        account = new_account()
        self.accounts.append(account)
        self._refresh_edit_list(account["id"])   # 勾选默认态由 _refresh_account_list 统一给出
        self._refresh_account_list()
        if self._selected_account_id is None:
            self._switch_account(account["id"])
        self._save_column_config()
        self.log.info("新增账户 %s", account_display(account))

    def _on_account_delete(self) -> None:
        """删除当前编辑的账户；磁盘上已备份的邮件不归这里管。"""
        account = self._find_account(self._editing_account_id)
        if account is None:
            return
        if not messagebox.askokcancel("删除账户",
                                      f"确定删除账户「{account_display(account)}」吗？\n"
                                      "已备份到磁盘的邮件不会被删除。", parent=self.root):
            return
        self.accounts.remove(account)
        self._states.pop(account["id"], None)
        if self._selected_account_id == account["id"]:
            self._selected_account_id = self.accounts[0]["id"] if self.accounts else None
            self._ui_settings["selected_account"] = self._selected_account_id
            self._render_account_detail()
        self._refresh_account_list()
        self._refresh_edit_list(None)
        self._save_column_config()
        self.log.info("删除账户 %s", account_display(account))

    def _on_account_save(self) -> None:
        """校验并保存当前编辑的账户；密码框留空表示不修改已存密码。"""
        account = self._find_account(self._editing_account_id)
        if account is None:
            return
        user = self._acct_user_var.get().strip()
        server = self._acct_server_var.get().strip()
        if not user:
            messagebox.showwarning("信息不完整", "请填写邮箱地址。", parent=self.root)
            return
        if not server:
            messagebox.showwarning("信息不完整", "请填写收件服务器。", parent=self.root)
            return
        try:
            port = int(self._acct_port_var.get().strip())
            if not 0 < port < 65536:
                raise ValueError
        except ValueError:
            messagebox.showwarning("端口无效", "端口必须是 1-65535 的整数。", parent=self.root)
            return
        password = self._acct_password_var.get()
        if password:
            cipher = _dpapi_encrypt(password) if sys.platform == "win32" else None
            if cipher is None:
                messagebox.showwarning("无法保存密码", "密码加密失败，本次未保存密码。", parent=self.root)
            else:
                account["password_dpapi"] = cipher
        account["name"] = self._acct_name_var.get().strip()
        account["user"] = user
        account["server"] = server
        account["ssl"] = bool(self._acct_ssl_var.get())
        account["port"] = str(port)
        self._acct_password_var.set("")
        self._refresh_account_list()
        self._refresh_edit_list(account["id"])
        self._refresh_stale(account["id"])
        if account["id"] == self._selected_account_id:
            self._render_card(account)
        self._save_column_config()
        self.log.info("保存账户 %s（%s）", account_display(account), user)

    # ------------------------------------------------------- stale state rules

    def _refresh_stale(self, account_id: str) -> None:
        """账户被编辑后刷新连接状态：取值 ≠ 连接当刻快照即 stale，回到原值即 connected。

        Note:
            只有连接过（快照非空）的账户才谈得上过期；idle/connecting 不动。快照取密码密文，
            换密码同样算改了连接信息。
        """
        state = self._states.get(account_id)
        if state is None or state["snapshot"] is None or state["connect_state"] not in ("connected", "stale"):
            return
        account = self._find_account(account_id)
        if account is None:
            return
        state["connect_state"] = "connected" if self._account_snapshot(account) == state["snapshot"] else "stale"
        if account_id == self._selected_account_id:
            self.connect_state = state["connect_state"]
            self._apply_connect_state()
            self._update_start_state()

    def _show_conn_hint(self, text: str, color: str) -> None:
        """显示连接提示并上色；空文案时整行让出空间，避免无提示时留一条空带。

        Args:
            text: 提示文案。
            color: 提示前景色。

        Note:
            判断是否已挂出用 winfo_manager 而不用 winfo_ismapped：后者反映显示端映射态，
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
        """连接中的按钮加载态：帧与节奏跟开始按钮共用（两处加载图标必须看起来一样）。"""
        self.connect_btn.configure(state="disabled")
        self._connect_spinner_tick(0)

    def _connect_spinner_tick(self, index: int) -> None:
        """画第 index 帧并按当前可见账户的连接状态决定排不排下一帧。

        Note:
            帧链只服务「当前可见账户正在连接」这一种情形：切到别的账户时 _apply_connect_state
            会取消它，连着的那个账户在后台照常完成，切回去时重新起链。
        """
        state = self._visible_state()
        if state is None or state["connect_state"] != "connecting":
            self._spinner_job = None
            return
        self._set_loading_frame(self.connect_btn, CONNECTING_BTN_LABEL, index)
        self._spinner_job = self.root.after(SPINNER_INTERVAL_MS, self._connect_spinner_tick, index + 1)

    def _apply_connect_state(self) -> None:
        state = self._visible_state()
        self.connect_state = state["connect_state"] if state is not None else "idle"
        if state is None:
            # 一个账户都没有：连接按钮无处可点，等去设置页建账户
            self._cancel_connect_spinner()
            self.connect_btn.configure(text=CONNECT_BTN_LABEL, state="disabled")
            self._show_conn_hint("", HINT_GRAY)
            return
        if self.connect_state == "connecting":
            if self._spinner_job is None:
                self._start_button_spinner()
        else:
            # 帧链取消要走 after_cancel：切账户后旧链若只清 job 槽，会和下一账户的新链并行
            self._cancel_connect_spinner()
            if self.running:
                # 运行中配置区整体锁定，connect_btn 已被 _set_config_enabled(False) 禁用
                self.connect_btn.configure(text=CONNECT_BTN_LABEL)
                hint = STOPPING_HINT_TEXT if self.stopping else RUNNING_HINT_TEXT
                self._show_conn_hint(hint, HINT_GRAY)
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

    def _cancel_connect_spinner(self) -> None:
        if self._spinner_job is not None:
            with contextlib.suppress(tk.TclError):
                self.root.after_cancel(self._spinner_job)
        self._spinner_job = None

    # --------------------------------------------------------- misc handlers

    def _on_browse_backup_root(self) -> None:
        initial = self.backup_root_var.get().strip() or str(Path.home())
        chosen = filedialog.askdirectory(parent=self.root, title="选择备份保存位置", initialdir=initial)
        if chosen:
            self.backup_root_var.set(Path(chosen).as_posix())

    def _on_format_change(self) -> None:
        """格式复选框变更：写进当前账户（每账户独立），再重算开始按钮。"""
        account = self._find_account(self._selected_account_id)
        if account is not None:
            account["mbox"] = bool(self.mbox_var.get())
            account["eml"] = bool(self.eml_var.get())
        self._update_start_state()

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

    def _on_wheel(self, event) -> str | None:
        """主窗口唯一的滚轮入口：指针落在「邮箱文件夹」或左侧账户列表里才接管，其余放行。

        Args:
            event: MouseWheel 事件。

        Returns:
            在接管的面板内返回 "break" 中断后续 bindtag；面板外返回 None 原样放行。

        Note:
            返回 "break" 才是真的中断（Tkinter 拿回调返回值与 "break" 比较）——面板内的滚条正是
            靠它压掉 TScrollbar 的类绑定；反过来 break 也会挡掉 root 上这条，所以滚条必须绑到
            这个函数、由它自己完成滚动。
        """
        canvas = self._wheel_canvas(event.widget)
        if canvas is None:
            return None  # 邮件列表 / 运行日志 / 设置页的滚轮节奏一字不动
        self._wheel_accum += event.delta
        notches = int(self._wheel_accum / WHEEL_DELTA)  # 向零取整：正反两个方向都不吃掉余量
        if notches:
            self._wheel_accum -= notches * WHEEL_DELTA
            canvas.yview_scroll(-notches * FOLDER_WHEEL_ROWS, "units")
        return "break"  # 不满一格也到此为止，别漏给面板内滚条的原生步长

    def _wheel_canvas(self, widget) -> tk.Canvas | None:
        """事件落在哪个可滚列表里：返回对应画布（邮箱文件夹 / 账户列表），其余返回 None。"""
        for panel, canvas in ((self.folder_group, self.folder_canvas),
                              (self.account_pane, self.account_canvas)):
            if self._widget_in_panel(widget, panel):
                return canvas
        return None

    def _widget_in_panel(self, widget, panel) -> bool:
        """判断事件控件是否属于某个面板。

        Args:
            widget: 事件携带的控件，沿 .master 上溯比对。
            panel: 目标面板。

        Returns:
            控件在面板内时为 True。

        Note:
            画布内嵌的行控件父链是：Label → 行容器 → 节点框 → inner → canvas → panel。用控件
            路径比而不是对象同一性：event.widget 与 panel 可能是同一条路径的两个 Python 对象。
            也不用 winfo_containing：那取决于窗口位置与层叠顺序，测试里不稳。
        """
        target = str(panel)
        while widget is not None:
            if str(widget) == target:
                return True
            widget = getattr(widget, "master", None)
        return False

    def _entry_widget_paths(self) -> frozenset[str]:
        """所有输入框（设置页账户表单 + 备份页地址栏）的控件路径：焦点归属判定用。"""
        widgets = [self.backup_root_entry, *self._account_form_entries]
        return frozenset(str(w) for w in widgets)

    def _on_click_clear_selection_and_focus(self, event) -> None:
        """主窗口唯一的左键入口：清邮件列表选中高亮，并把输入框焦点收归主窗口。

        Args:
            event: Button-1 事件。

        Note:
            只清理、不拦截：返回 None 让 bindtag 链照常走完（点行/换选是 Treeview 类绑定的事，
            本入口在类绑定之后执行，那时选中已就位，识别出「是行」就别动它）。选中与焦点共用
            两个例外：邮件列表自己的滚条——拖它多半是在查看选中行，不能误伤，焦点也一并保留；
            Alt-Tab 切窗口不产生 Button-1，天然不清，与资源管理器「切走再回来高亮还在」一致。
            焦点只在「点击后焦点仍在某个输入框、且落点不是这个框」时收归主窗口：Tk 只让输入框、
            列表、日志文本在点击时接焦（禁用中的输入框不接），其余落点（标签、空白、按钮、
            勾选框）焦点原地不动，正需要这里收走。
        """
        entry_paths = self._entry_widget_paths()
        focused = self.root.focus_get()
        focused_in_entry = focused is not None and str(focused) in entry_paths
        if not self.tree.selection() and not focused_in_entry:
            return  # 无选中且焦点不在输入框是常态：提前退出，之后的点击零代价
        widget = str(event.widget)  # 路径比较而非对象同一性，理由同 _wheel_in_folder_panel
        if widget == str(self.tree_scroll):
            return  # 列表滚条：查看选中行用的，选中与焦点都不动
        if widget == str(self.tree) and self.tree.identify_row(event.y):
            return  # 点中行：换选或多选交给 Treeview 自己
        self.tree.selection_set()  # 不带参数即清空选中
        if focused_in_entry and widget != str(focused):
            self.root.focus_set()  # 焦点收归主窗口：输入框光标消失，Tab 从第一个控件重新开始

    def _on_open_log_dir(self) -> None:
        try:
            os.startfile(str(LOG_DIR))  # type: ignore[attr-defined]
        except Exception as e:
            messagebox.showerror("无法打开", f"日志文件夹打开失败：{e}", parent=self.root)

    # ------------------------------------------------------------ context menus

    def _build_context_menus(self) -> None:
        """三张右键菜单各建一次复用；不进 _register——那是配置区控件的启停跟踪名单，菜单不是控件。"""
        self._entry_menu = tk.Menu(self.root, tearoff=0)
        self._entry_menu.add_command(label=MENU_CUT_LABEL, command=lambda: self._entry_edit("<<Cut>>"))
        self._entry_menu.add_command(label=MENU_COPY_LABEL, command=lambda: self._entry_edit("<<Copy>>"))
        self._entry_menu.add_command(label=MENU_PASTE_LABEL, command=lambda: self._entry_edit("<<Paste>>"))

        self._tree_menu = tk.Menu(self.root, tearoff=0)
        self._tree_menu.add_command(label=MENU_COPY_LABEL, command=self._copy_menu_cell)

        self._log_menu = tk.Menu(self.root, tearoff=0)
        self._log_menu.add_command(label=MENU_COPY_LABEL, command=self._log_copy_selection)
        self._log_menu.add_command(label=MENU_COPY_ALL_LABEL, command=self._log_copy_all)

    def _read_ui_settings(self) -> dict:
        """读界面设置，含 v1 → v2 迁移与逐节容错（ADR 0008/0010）。

        Returns:
            统一形状的设置字典（version 恒为当前版本）；文件缺失、整体损坏时回退默认值。

        Note:
            容错按节独立：columns 的细节校验沿用 `_load_column_config`，window/close_action 各自
            降级，accounts 非列表整节丢弃、单账户按 `parse_account` 逐个校验。旧的无版本「仅列宽」
            格式先被识别成 v1 形状再走迁移，存量用户不会因为升级丢掉列宽与账号。
        """
        try:
            with COLUMN_CONFIG_PATH.open(encoding="utf-8") as stream:
                raw = json.load(stream)
            if (isinstance(raw, dict) and set(raw) == set(MAIL_COLUMNS)
                    and all(isinstance(raw[name], int) and not isinstance(raw[name], bool)
                            for name in MAIL_COLUMNS)):
                return {"version": UI_SETTINGS_VERSION, "columns": raw}
            if not isinstance(raw, dict):
                raise ValueError("unsupported UI settings format")
            version = raw.get("version")
            if version == 1:
                self.log.info("界面设置为 v1 格式，迁移到 v%d", UI_SETTINGS_VERSION)
                raw = self._migrate_v1_settings(raw)
                version = UI_SETTINGS_VERSION
            if version != UI_SETTINGS_VERSION:
                raise ValueError("unsupported UI settings format")
            return self._parse_settings_v2(raw)
        except FileNotFoundError:
            return {"version": UI_SETTINGS_VERSION}
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self.log.warning("忽略无效的界面设置 %s: %s", COLUMN_CONFIG_PATH, exc)
            return {"version": UI_SETTINGS_VERSION}

    def _migrate_v1_settings(self, raw: dict) -> dict:
        """v1 → v2：旧 config 节（单账号）变成 accounts 单元素列表，outdir 变成 backup_root。

        Note:
            其余节（columns/window/close_action）原样带走；config 缺失或损坏时不造账户，
            只保留其余节——与「坏节丢弃」同一口径。
        """
        migrated = dict(raw)
        migrated["version"] = UI_SETTINGS_VERSION
        config = raw.get("config")
        accounts: list[dict] = []
        backup_root = None
        if isinstance(config, dict):
            account = new_account()
            for name in ("user", "server", "port"):
                value = config.get(name)
                if isinstance(value, str):
                    account[name] = value
            ssl = config.get("ssl")
            if isinstance(ssl, bool):
                account["ssl"] = ssl
            cipher = config.get("password_dpapi")
            if isinstance(cipher, str) and cipher:
                account["password_dpapi"] = cipher
            accounts.append(account)
            outdir = config.get("outdir")
            if isinstance(outdir, str) and outdir.strip():
                backup_root = outdir
        else:
            self.log.warning("忽略无效的用户配置节 %s", COLUMN_CONFIG_PATH)
        migrated["accounts"] = accounts
        migrated["backup_root"] = backup_root
        migrated["selected_account"] = accounts[0]["id"] if accounts else None
        migrated.pop("config", None)
        return migrated

    def _parse_settings_v2(self, raw: dict) -> dict:
        """逐节校验 v2 设置：坏节丢弃、坏字段回落，不让一处坏数据拖垮整份设置。"""
        columns = raw.get("columns")
        if columns is not None and not isinstance(columns, dict):
            raise ValueError("invalid columns section")
        action = raw.get(CLOSE_ACTION_KEY)
        if action is not None and parse_close_action(action) != action:
            self.log.warning("忽略无效的关闭行为偏好 %s: %r", COLUMN_CONFIG_PATH, action)
        accounts: list[dict] = []
        raw_accounts = raw.get("accounts")
        if raw_accounts is not None:
            if not isinstance(raw_accounts, list):
                self.log.warning("忽略无效的账户列表 %s", COLUMN_CONFIG_PATH)
            else:
                for item in raw_accounts:
                    account = parse_account(item)
                    if account is None:
                        self.log.warning("跳过无效的账户记录 %s", COLUMN_CONFIG_PATH)
                    else:
                        accounts.append(account)
        backup_root = raw.get("backup_root")
        if backup_root is not None and not (isinstance(backup_root, str) and backup_root.strip()):
            self.log.warning("忽略无效的备份保存位置 %s", COLUMN_CONFIG_PATH)
            backup_root = None
        selected = raw.get("selected_account")
        if selected not in {account["id"] for account in accounts}:
            selected = accounts[0]["id"] if accounts else None
        return {"version": UI_SETTINGS_VERSION, "columns": columns, "window": raw.get("window"),
                CLOSE_ACTION_KEY: parse_close_action(action), "accounts": accounts,
                "backup_root": backup_root, "selected_account": selected}

    def _restore_window_settings(self) -> tuple[int, int, int, int]:
        # 落地后要重设的那份几何（见 __init__ 末尾）：默认就是默认尺寸，恢复到保存值时在下面替换
        self._target_geometry = f"{DEFAULT_WINDOW_WIDTH}x{DEFAULT_WINDOW_HEIGHT}"
        # 记忆口径的最大化状态（见 _on_root_configure）：坏配置与 TclError 都落到 False
        self._window_maximized = False
        window = self._ui_settings.get("window")
        if window is None:
            return DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT, 0, 0
        if not isinstance(window, dict):
            self.log.warning("忽略无效的窗口状态配置 %s", COLUMN_CONFIG_PATH)
            return DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT, 0, 0
        geometry = window.get("normal_geometry")
        if not isinstance(geometry, dict):
            self.log.warning("忽略无效的窗口几何配置 %s", COLUMN_CONFIG_PATH)
            return DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT, 0, 0
        parsed: list[int] = []
        for name in ("width", "height", "x", "y"):
            value = geometry.get(name)
            if isinstance(value, bool) or not isinstance(value, int):
                self.log.warning("忽略无效的窗口几何配置 %s", COLUMN_CONFIG_PATH)
                return DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT, 0, 0
            parsed.append(value)
        width, height, x, y = parsed
        if width < MIN_WINDOW_WIDTH or height < MIN_WINDOW_HEIGHT:
            self.log.warning("忽略无效的窗口几何配置 %s", COLUMN_CONFIG_PATH)
            return DEFAULT_WINDOW_WIDTH, DEFAULT_WINDOW_HEIGHT, 0, 0
        maximized = window.get("maximized", False)
        if not isinstance(maximized, bool):
            self.log.warning("忽略无效的窗口最大化配置 %s", COLUMN_CONFIG_PATH)
            maximized = False
        self.root.geometry(f"{width}x{height}")
        self._target_geometry = f"{width}x{height}"
        if self._window_position_visible(width, height, x, y):
            self.root.geometry(f"{width}x{height}+{x}+{y}")
            self._target_geometry = f"{width}x{height}+{x}+{y}"
        else:
            self.log.warning("窗口位置不在当前显示器范围内，使用默认位置")
        self.root.update_idletasks()
        if maximized:
            try:
                self.root.state("zoomed")
            except tk.TclError:
                self.log.warning("无法恢复窗口最大化状态")
            else:
                self._window_maximized = True
        return width, height, x, y

    def _window_position_visible(self, width: int, height: int, x: int, y: int) -> bool:
        left = self.root.winfo_vrootx()
        top = self.root.winfo_vrooty()
        right = left + self.root.winfo_vrootwidth()
        bottom = top + self.root.winfo_vrootheight()
        return x < right and x + width > left and y < bottom and y + height > top

    def _load_column_config(self) -> None:
        """Load five widths while allowing window settings to be invalid independently."""
        self._ui_settings = self._read_ui_settings()
        raw = self._ui_settings.get("columns")
        if (not isinstance(raw, dict) or set(raw) != set(MAIL_COLUMNS)
                or any(isinstance(raw[name], bool) or not isinstance(raw[name], int)
                       for name in MAIL_COLUMNS)
                or any(raw[name] < int(self.tree.column(name, "minwidth"))
                       for name in MAIL_COLUMNS)):
            if raw is not None:
                self.log.warning("忽略无效的邮件列表列宽配置 %s", COLUMN_CONFIG_PATH)
            return
        self.tree.column("subject", stretch=False)
        for name in MAIL_COLUMNS:
            self.tree.column(name, width=raw[name])
        self._column_config_loaded = True

    def _save_column_config(self) -> None:
        """Atomically persist all UI settings, leaving the live layout untouched on failure.

        Note:
            账户节从内存态序列化（`account_record`）：文件夹勾选先从可见账户的树上收一次，
            别的账户的勾选在各自状态字典里原地存活，随下一次连接或落盘回到记录。
        """
        widths = {name: int(self.tree.column(name, "width")) for name in MAIL_COLUMNS}
        self._sync_visible_folder_choices()
        self._ui_settings["version"] = UI_SETTINGS_VERSION
        self._ui_settings["columns"] = widths
        self._ui_settings["window"] = self._window_settings()
        # 关闭行为偏好只由设置页/关闭对话框改内存，落盘统一走这里（ADR 0008 的单一写入路径）
        self._ui_settings[CLOSE_ACTION_KEY] = self._close_action()
        self._ui_settings["accounts"] = [account_record(account) for account in self.accounts]
        self._ui_settings["backup_root"] = self.backup_root_var.get().strip() or DEFAULT_BACKUP_ROOT
        self._ui_settings["selected_account"] = self._selected_account_id
        temporary: Path | None = None
        try:
            COLUMN_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=COLUMN_CONFIG_PATH.parent,
                                             prefix=f".{COLUMN_CONFIG_PATH.name}.",
                                             suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(self._ui_settings, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, COLUMN_CONFIG_PATH)
            self._column_config_loaded = True
        except (OSError, TypeError, ValueError) as exc:
            try:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            except OSError:
                pass
            self.log.warning("保存界面设置失败 %s: %s", COLUMN_CONFIG_PATH, exc)

    def _sync_visible_folder_choices(self) -> None:
        """把可见账户树上的勾选写回该账户记录（构造过树才有可写的勾选）。"""
        account = self._find_account(self._selected_account_id)
        if account is None or not self.folder_rows:
            return
        account["folders"] = [row["foldername"] for row in self.folder_rows if row["var"].get()]

    def _window_settings(self) -> dict:
        width, height, x, y = self._normal_geometry
        return {
            "normal_geometry": {"width": width, "height": height, "x": x, "y": y},
            # 记忆口径：窗口最小化（iconic）或已隐藏（withdrawn）时现场读 wm state 会把最大化记丢
            "maximized": self._window_maximized,
        }

    def _on_root_configure(self, _event) -> None:
        state = self.root.state()
        # 只有可见且未最小化时才算「窗口状态」的真值；派发时刻读的是当下状态，排队事件也安全
        if state in ("zoomed", "normal"):
            self._window_maximized = state == "zoomed"
        if state == "zoomed":
            return
        width, height = self.root.winfo_width(), self.root.winfo_height()
        if width >= MIN_WINDOW_WIDTH and height >= MIN_WINDOW_HEIGHT:
            self._normal_geometry = (width, height, self.root.winfo_x(), self.root.winfo_y())

    def _on_tree_configure(self, _event) -> None:
        if (not self._column_config_loaded or self._column_reflowing
                or self.tree.winfo_width() <= 1):
            return
        widths = [int(self.tree.column(name, "width")) for name in MAIL_COLUMNS]
        if not self._column_config_fitted or sum(widths) != self.tree.winfo_width():
            self._fit_loaded_columns()

    def _fit_loaded_columns(self) -> None:
        available = self.tree.winfo_width()
        widths = {name: int(self.tree.column(name, "width")) for name in MAIL_COLUMNS}
        if available <= 1 or not widths:
            return
        minimums = {name: int(self.tree.column(name, "minwidth")) for name in MAIL_COLUMNS}
        total = sum(widths.values())
        if total <= 0:
            return
        scale = available / total
        fitted = {name: max(minimums[name], round(width * scale))
                  for name, width in widths.items()}
        excess = sum(fitted.values()) - available
        if excess > 0:
            for name in sorted(MAIL_COLUMNS, key=lambda item: fitted[item] - minimums[item],
                               reverse=True):
                reduction = min(excess, fitted[name] - minimums[name])
                fitted[name] -= reduction
                excess -= reduction
                if excess == 0:
                    break
        self._column_reflowing = True
        try:
            for name, width in fitted.items():
                self.tree.column(name, stretch=False, width=width)
        finally:
            self._column_reflowing = False
        self._column_config_fitted = True

    def _on_entry_context(self, event) -> str:
        """输入框右键：记下弹窗目标、按当前选区与剪贴板刷新灰化，再弹菜单。"""
        self._entry_menu_target = event.widget
        self._refresh_entry_menu_state(event.widget)
        self._entry_menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def _refresh_entry_menu_state(self, entry: tk.Entry) -> None:
        """弹窗前刷新输入框菜单的灰化状态。

        无选中文本时剪切/复制灰；剪贴板没有文本或控件已禁用时粘贴灰。

        Args:
            entry: 右键命中的输入框。

        Note:
            禁用判断读控件自己的 state：备份运行中 _set_config_enabled 把配置区整片关掉，
            禁用态的输入框做不出选区，三项自然都该灰。
        """
        editable = str(entry.cget("state")) != "disabled"
        has_sel = editable and entry.selection_present()
        for label in (MENU_CUT_LABEL, MENU_COPY_LABEL):
            self._entry_menu.entryconfigure(label, state="normal" if has_sel else "disabled")
        self._entry_menu.entryconfigure(MENU_PASTE_LABEL,
                                        state="normal" if editable and self._clipboard_has_text() else "disabled")

    def _entry_edit(self, event_name: str) -> None:
        """菜单动作直通 Entry 的类绑定（<<Cut>>/<<Copy>>/<<Paste>>），与 Ctrl+X/C/V 完全同效。"""
        if self._entry_menu_target is not None:
            self._entry_menu_target.event_generate(event_name)

    def _on_tree_context(self, event) -> str:
        """邮件列表右键：行上弹「复制」菜单，空白与列标题不弹。"""
        if not self._prepare_tree_popup(event):
            return "break"
        self._tree_menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def _on_tree_resize_start(self, event) -> str | None:
        if self.tree.identify_region(event.x, event.y) != "separator":
            return None
        columns = tuple(self.tree["columns"])
        column = self.tree.identify_column(event.x)
        try:
            index = int(column[1:]) - 1
        except (ValueError, IndexError):
            return None
        if not 0 <= index < len(columns) - 1:
            return "break"
        left, right = columns[index:index + 2]
        subject_stretch = bool(self.tree.column("subject", "stretch"))
        if subject_stretch:
            # A stretch column otherwise grows while the fixed pair is
            # resized, moving the separator being dragged.
            self.tree.column("subject", stretch=False)
        self._tree_resize = (event.x, left, right,
                             int(self.tree.column(left, "width")),
                             int(self.tree.column(right, "width")),
                             subject_stretch)
        return "break"

    def _on_tree_resize_motion(self, event) -> str | None:
        if self._tree_resize is None:
            return None
        start_x, left, right, left_width, right_width, _ = self._tree_resize
        delta = event.x - start_x
        left_min = int(self.tree.column(left, "minwidth"))
        right_min = int(self.tree.column(right, "minwidth"))
        delta = max(left_min - left_width, min(delta, right_width - right_min))
        new_left = left_width + delta
        new_right = right_width - delta
        unchanged = {name: int(self.tree.column(name, "width"))
                     for name in self.tree["columns"] if name not in (left, right)}
        self.tree.column(left, width=new_left)
        self.tree.column(right, width=new_right)
        for name, width in unchanged.items():
            self.tree.column(name, width=width)
        return "break"

    def _on_tree_resize_end(self, _event) -> str | None:
        if self._tree_resize is None:
            return None
        _, _left, _right, _, _, subject_stretch = self._tree_resize
        if subject_stretch:
            self.tree.column("subject", stretch=True)
        self._tree_resize = None
        self._save_column_config()
        return "break"

    def _prepare_tree_popup(self, event) -> bool:
        """邮件列表右键弹窗前的全部判断与副作用。

        定位单元格、右键先选中该行（Windows 惯例）、记下复制目标。

        Args:
            event: Button-3 事件。

        Returns:
            落点有可复制的单元格时为 True；行下方空白、列标题、列号识别不出时为 False，
            此时既不弹菜单也不动选中。
        """
        target = self._tree_context_target(event)
        if target is None:
            return False
        if target[0] not in self.tree.selection():
            self.tree.selection_set(target[0])
        self._tree_menu_target = target
        return True

    def _tree_context_target(self, event) -> tuple[str, int] | None:
        """把右键落点定位成 (行 id, 列号 1 起)。

        Args:
            event: Button-3 事件。

        Returns:
            (行 id, 列号 1 起)；空白与无效列号返回 None。

        Note:
            identify 只吃控件内坐标（与 x_root/y_root 无关），测试可直接喂伪事件。
        """
        row = self.tree.identify_row(event.y)
        if not row:
            return None
        column = self.tree.identify_column(event.x)  # "#1"…"#5"；树列区与无效位置是 "" 或 "#0"
        try:
            col_idx = int(column[1:])
        except ValueError:
            return None
        if not 1 <= col_idx <= len(self.tree.item(row, "values")):
            return None
        return row, col_idx

    def _copy_menu_cell(self) -> None:
        """邮件菜单的「复制」：复制弹窗当刻定位的单元格。"""
        if self._tree_menu_target is not None:
            self._copy_cell(*self._tree_menu_target)

    def _copy_cell(self, row: str, col_idx: int) -> None:
        """把某行第 col_idx 列（1 起）的文本放进剪贴板。"""
        values = self.tree.item(row, "values")
        if not 1 <= col_idx <= len(values):
            return  # 菜单挂着时列表被别的事件换过内容：宁可不复制，也不抛
        self._set_clipboard(str(values[col_idx - 1]))

    def _on_log_context(self, event) -> str:
        """运行日志右键：「复制」（选中段）与「复制全部」，弹窗前刷新两项灰化。"""
        self._refresh_log_menu_state()
        self._log_menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def _refresh_log_menu_state(self) -> None:
        """没划选时「复制」灰；日志为空时「复制全部」灰。"""
        has_sel = bool(self.log_text.tag_ranges("sel"))
        self._log_menu.entryconfigure(MENU_COPY_LABEL, state="normal" if has_sel else "disabled")
        empty = self.log_text.index("end-1c") == "1.0"
        self._log_menu.entryconfigure(MENU_COPY_ALL_LABEL, state="disabled" if empty else "normal")

    def _log_copy_selection(self) -> None:
        """复制日志里划选的那一段；只读态照样能划选（探针见 issues/13）。"""
        if sel := self.log_text.tag_ranges("sel"):
            self._set_clipboard(self.log_text.get(sel[0], sel[1]))

    def _log_copy_all(self) -> None:
        self._set_clipboard(self.log_text.get("1.0", "end-1c"))

    def _set_clipboard(self, text: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(text)

    def _clipboard_has_text(self) -> bool:
        """剪贴板为空或只有非文本格式（如图片）时 clipboard_get 抛 TclError，一并视为不可粘贴。"""
        try:
            self.root.clipboard_get()
        except tk.TclError:
            return False
        return True

    # ------------------------------------------------------------- run control

    def _update_start_state(self, *_args) -> None:
        state = self._visible_state()
        ok = (state is not None and state["connect_state"] == "connected"
              and any(r["var"].get() for r in self.folder_rows)
              and (self.mbox_var.get() or self.eml_var.get())
              and not self.running)
        self.start_btn.configure(state="normal" if ok else "disabled")
        self.backup_selected_btn.configure(state="normal" if self.accounts and not self.running else "disabled")

    def _set_loading_frame(self, button: tk.Button, label: str, index: int) -> None:
        """把按钮画成加载态的第 index 帧——连接与备份共用这一个入口。

        Args:
            button: 要画帧的按钮。
            label: 加载态文案，帧字符作前缀。
            index: 帧序号，按 SPINNER_FRAMES 长度取模循环。

        Note:
            盲文点阵当前缀，节奏同为 SPINNER_INTERVAL_MS，所以两处的加载图标看起来一模一样。
        """
        button.configure(text=f"{SPINNER_FRAMES[index % len(SPINNER_FRAMES)]} {label}")

    def _start_run_spinner(self) -> None:
        """开始按钮的加载态（进入运行态时调用，此刻 self.running 已置真）：绿三角换透明占位、只留转圈文字。"""
        self._stop_run_spinner()  # 防重复进入：旧 job 不取消会留下第二条帧链
        self.start_btn.configure(image=self.blank_icon)
        self._run_spinner_tick(0)

    def _run_spinner_tick(self, index: int) -> None:
        """画第 index 帧，并按状态决定排不排下一帧、以及画在哪颗按钮上。

        Args:
            index: 帧序号。

        Note:
            帧链只有一条（job 槽 self._run_spinner_job）：运行中画开始按钮，停止中改画停止按钮
            （开始按钮已在 _on_stop 里退回静止外观），所以「谁在忙」永远只有一颗按钮在转。
            退出时把整段运行态界面复位（两颗按钮 + 配置区提示）：有的路径会把 running 直接置假
            而不走 _end_run_controls，不在这儿兜一下，按钮上会残留「正在备份…」/「正在停止…」、
            提示会滞留在「备份正在停止…」。帧推进单独成方法，测试可直接驱动。
        """
        if not self.running:
            self._run_spinner_job = None
            self.stopping = False
            self._restore_start_button()
            self._restore_stop_button()
            self._apply_connect_state()
            return
        button, label = ((self.stop_btn, STOPPING_BTN_LABEL) if self.stopping
                         else (self.start_btn, RUNNING_BTN_LABEL))
        self._set_loading_frame(button, label, index)
        self._run_spinner_job = self.root.after(SPINNER_INTERVAL_MS, self._run_spinner_tick, index + 1)

    def _stop_run_spinner(self) -> None:
        if self._run_spinner_job is not None:
            self.root.after_cancel(self._run_spinner_job)
            self._run_spinner_job = None

    def _restore_start_button(self) -> None:
        """回到静止态：文案与绿三角一并回位（完成/停止/失败三条结局与帧链自愈分支都收口到这里）。"""
        self.start_btn.configure(text=START_BTN_LABEL, image=self.start_icon)

    def _restore_stop_button(self) -> None:
        """回到静止态：文案与红方块一并回位（与 _restore_start_button 成对、同两个收口点）。"""
        self.stop_btn.configure(text=STOP_BTN_LABEL, image=self.stop_icon)

    def _set_config_enabled(self, enabled: bool) -> None:
        for widget, default_state in self._config_widgets:
            # Misc.__setitem__ 等价 configure({key: value})：存根把 configure 的 state 形参
            # 收成 Literal，而这里要存回的默认 state 是运行期 str，走按 Any 校验的下标路径
            with contextlib.suppress(tk.TclError):
                widget["state"] = default_state if enabled else "disabled"
        # 树里是 tk.Label：它有 -state 选项，但置灰和阻断绑定都不生效，
        # 所以靠前景色 + 光标提示不可点，真正的拦截在两个 handler 的 running 判定里。
        # 默认前景色是 Tk 的系统色名（如 SystemButtonText），"" 不是合法颜色名，须按控件记录还原。
        for w, default_fg in self._folder_clickables:
            w.configure(foreground=default_fg if enabled else "gray",
                        cursor="hand2" if enabled else "arrow")

    def _ensure_status_rows(self) -> None:
        """首次开始备份时放出进度条和第二行状态条；放出后不再收回。

        Note:
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
        """开始备份的界面侧（调用前 self.running 已置真）。

        锁配置区、亮出"先停止才能改"提示、切到列表。

        Note:
            必须调 _apply_connect_state：运行中那条提示只在连接状态机里输出，
            只锁控件不刷状态机的话，用户会以为可以照改配置。
        """
        self._ensure_status_rows()
        self._set_config_enabled(False)
        self._apply_connect_state()
        self._start_run_spinner()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.notebook.select(0)

    def _on_start(self) -> None:
        """备份当前账户：先校验连接信息与备份位置，再进入批量运行的公共入口。"""
        if self.running:
            return
        account = self._find_account(self._selected_account_id)
        if account is None:
            return
        if self._account_credentials(account) is None:
            return
        if not self._backup_root_ready():
            return
        if not any(r["var"].get() for r in self.folder_rows):
            return
        self._sync_visible_folder_choices()
        self._begin_run([account["id"]])

    def _on_start_selected(self) -> None:
        """备份所选账户：只跑勾选的账户，跳过信息不完整的并逐个提示，按列表顺序串行运行。"""
        if self.running:
            return
        if not self.accounts:
            messagebox.showwarning("信息不完整", "还没有账户，请先在设置页的「账户」里新增。", parent=self.root)
            return
        if not self._backup_root_ready():
            return
        chosen = [account for account in self.accounts if account["id"] in self._checked_accounts]
        if not chosen:
            messagebox.showwarning("未勾选账户", "请先在左侧勾选要备份的账户。", parent=self.root)
            return
        ready: list[dict] = []
        skipped: list[str] = []
        for account in chosen:
            if self._account_credentials(account, warn=False) is None:
                skipped.append(account_display(account))
            else:
                ready.append(account)
        if skipped:
            messagebox.showwarning("部分账户将跳过",
                                   "以下账户信息不完整（缺邮箱地址、收件服务器或密码），本次不备份：\n"
                                   + "\n".join(skipped), parent=self.root)
        if not ready:
            messagebox.showwarning("信息不完整", "勾选的账户信息都不完整，请先在设置页补全账户信息与密码。",
                                   parent=self.root)
            return
        self._sync_visible_folder_choices()
        self._begin_run([account["id"] for account in ready])

    def _backup_root_ready(self) -> bool:
        if self.backup_root_var.get().strip():
            return True
        messagebox.showwarning("信息不完整", "请先在设置页的「备份」里选择备份保存位置。", parent=self.root)
        return False

    def _begin_run(self, account_ids: list[str]) -> None:
        """进入运行态并把参与账户的重跑基线复位（公共入口：单账户与批量都走这里）。"""
        self.running = True
        self.stopping = False
        self.stop_flag.clear()
        self._run_account_ids = list(account_ids)
        self._run_t0 = time.monotonic()
        for index, account_id in enumerate(account_ids):
            state = self._state(account_id)
            state["messages"] = []
            state["row_index"] = 0
            state["server_total"] = state["pending_total"] = state["backed"] = 0
            state["progress"] = None
            state["result"] = None
            state["status_error"] = False
            state["status_text"] = CONNECTING_STATUS_TEXT if index == 0 else QUEUED_STATUS_TEXT
            state["count_text"] = "尚未开始备份"
        visible = self._visible_state()
        if visible is not None:
            self._render_messages(visible)
            self._restore_run_state(visible)
        self._begin_run_ui()
        outdir = self.backup_root_var.get().strip()
        snapshots = self._account_snapshots(account_ids)
        self.log.info("开始备份运行：%d 个账户，输出 %s", len(snapshots), outdir)
        self._sync_tray()  # 图标常驻时窗口可见也挂着：菜单「停止备份」此刻就该可点
        threading.Thread(target=self._batch_worker, args=(snapshots, outdir), daemon=True).start()

    def _account_snapshots(self, account_ids: list[str]) -> list[dict]:
        """开跑前把参与账户复制一份（含解出的密码）：运行中改设置不影响本次运行。"""
        snapshots: list[dict] = []
        for account_id in account_ids:
            account = self._find_account(account_id)
            if account is None:
                continue
            data = dict(account)
            data["folders"] = list(account.get("folders", []))
            data["password"] = self._account_password(account)
            snapshots.append(data)
        return snapshots

    def _batch_worker(self, snapshots: list[dict], outdir: str) -> None:
        """批量线程入口：按顺序逐账户备份，任一账户失败不影响其余；停止则跳过后续账户。"""
        log = logging.getLogger("imapbackup")
        eq = self.events
        failures: list[tuple[str, str]] = []
        for account in snapshots:
            account_id = account["id"]
            if self.stop_flag.is_set():
                eq.put(("account_skipped", account_id))
                continue
            eq.put(("account_begin", account_id))
            try:
                self._backup_account(account, outdir, eq, log)
            except SystemExit as e:
                failures.append((account_display(account), str(e)))
                eq.put(("account_failed", account_id, str(e)))
            except Exception as e:
                log.exception("账户备份失败：%s", account_display(account))
                failures.append((account_display(account), f"{type(e).__name__}: {e}"))
                eq.put(("account_failed", account_id, f"{type(e).__name__}: {e}"))
        eq.put(("batch_done", self.stop_flag.is_set(), failures))

    def _backup_account(self, account: dict, outdir: str, eq: queue.Queue, log: logging.Logger) -> None:
        """单个账户的备份：先统计待备份总量、再逐文件夹下载，进度经事件队列回主线程。

        Note:
            每次运行都自己 LIST 一次文件夹（不依赖界面上那份清单）：从没连接过的账户在批量里
            也要能直接跑，且以服务器此刻的文件夹为准。账户目录名取邮箱地址（ADR 0002），
            账户名称只是显示名、不参与路径。
        """
        account_id = account["id"]
        t0 = time.monotonic()
        backed = 0
        wanted = {name for name in account.get("folders", []) if isinstance(name, str) and name} or {"INBOX"}

        def report(d: dict) -> None:
            nonlocal backed
            backed += 1
            log.debug("已备份 %s：%s", d["folder"], d["msg_id"])
            eq.put(("message", account_id, d))

        v = {"user": account["user"], "server": account["server"], "password": account["password"],
             "ssl": bool(account.get("ssl", DEFAULT_ACCOUNT_SSL)),
             "port": int(str(account.get("port", DEFAULT_ACCOUNT_PORT)).strip()),
             "mbox": bool(account.get("mbox", False)), "eml": bool(account.get("eml", True))}
        root_dir = Path(outdir)
        basedir = root_dir if v["mbox"] else None
        eml_root = root_dir if v["eml"] else None
        cfg = self._core_config(v, basedir, eml_root)
        log.info("开始备份账户 %s（%s）：目标 %s（mbox=%s eml=%s）",
                 account_display(account), v["user"], outdir, v["mbox"], v["eml"])
        server = core.connect_and_login(cfg, log)
        account_dir = core.sanitize_segment(v["user"], core.EML_SEGMENT_MAX)
        try:
            names = core.get_names(server, False, True, quiet=False, log=log)
            selected = [n for n in names if n[0] in wanted]
            if not selected:
                log.warning("账户 %s：选中的文件夹在服务器上都不存在，本次无备份", account_display(account))

            # pass 1: enumerate + incremental scan, so 待备份 total is known before any download
            plans: list[tuple] = []
            server_total = 0
            pending_total = 0
            for i, n in enumerate(selected, 1):
                if self.stop_flag.is_set():
                    break
                foldername, filename, eml_relpath, display = n
                eq.put(("phase", account_id, f"扫描 {i}/{len(selected)}：{display}"))
                try:
                    remote = core.scan_folder(server, foldername, True, quiet=False, log=log, display=display)
                except core.SkipFolderException as e:
                    self.log.warning("%s：跳过（%s）", display, e)
                    continue
                # 两种格式各自判重：任一已选格式缺这封就算待备份，写盘时只补缺的那一份。
                local_mbox: dict[str, str] | None = None
                if basedir is not None:
                    local_mbox = core.scan_file(f"{account_dir}/{filename}", False, True, basedir,
                                                quiet=False, log=log)
                local_eml: dict[str, str] | None = None
                if eml_root is not None:
                    local_eml = core.scan_eml_dir(eml_root / account_dir / eml_relpath, False, True,
                                                  quiet=False, log=log)
                new = core.pending_messages(remote, local_mbox, local_eml)
                server_total += len(remote)
                pending_total += len(new)
                plans.append((n, new, local_mbox, local_eml))
            eq.put(("totals", account_id, server_total, pending_total))

            # pass 2: download; folders were re-selectable because scan_folder moved the selection each time
            if pending_total > 0 and not self.stop_flag.is_set():
                if basedir is not None:
                    core.ensure_basedir(basedir)
                    core.create_folder_structure([p[0] for p in plans], basedir, account_dir)
                for n, new, local_mbox, local_eml in plans:
                    if self.stop_flag.is_set():
                        break
                    foldername, filename, eml_relpath, display = n
                    if not new:
                        continue
                    eq.put(("phase", account_id, f"备份 {display}（{len(new)} 封）"))
                    typ, data = server.select(f'"{foldername}"', readonly=True)
                    if typ != "OK":
                        self.log.warning("%s：SELECT 失败 %s", display, data)
                        continue
                    label = f"{account_dir}/{filename}" if basedir is not None else f"{account_dir}/{eml_relpath}"
                    eml_folder_dir = eml_root / account_dir / eml_relpath if eml_root is not None else None
                    core.download_messages(server, label, new, False, True, False,
                                           basedir, False, quiet=False, log=log,
                                           eml_dir=eml_folder_dir, skip_mbox=local_mbox, skip_eml=local_eml,
                                           foldername=display,
                                           report=report, should_stop=self.stop_flag.is_set)
        finally:
            with contextlib.suppress(Exception):
                server.logout()
        seconds = time.monotonic() - t0
        eq.put(("account_done", account_id, backed, server_total - pending_total, seconds,
                self.stop_flag.is_set(), pending_total))

    def _on_stop(self) -> None:
        """停止中：多处同时给反馈。

        停止按钮接手转圈、开始按钮退回静止外观、状态行清空、配置区提示换口径。

        Note:
            帧链不用重启（工作线程还在收尾、self.running 仍为真），下一帧起 _run_spinner_tick
            会按 stopping 把帧画到停止按钮上；所以这里翻状态后当帧就把开始按钮复位，点击立刻
            有反馈。状态行只清文案、不收回整行：版本号与行高都留在原地，结束时已有「已停止：…」
            回填。
        """
        if not self.running:
            return
        self.stop_flag.set()
        self.stopping = True
        self.stop_btn.configure(state="disabled", image=self.blank_icon)
        self._restore_start_button()
        self._stop_run_spinner()  # 取消挂起的那一帧，立即按新目标重画第 0 帧：点下去当刻就见反馈
        self._run_spinner_tick(0)
        self._set_status("", error=False)
        self._apply_connect_state()

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
            self._on_connect_failed(*ev[1])
        elif kind == "account_begin":
            self._on_account_begin(ev[1])
        elif kind == "phase":
            self._on_phase(*ev[1:])
        elif kind == "totals":
            self._on_totals(*ev[1:])
        elif kind == "message":
            self._on_message_event(*ev[1:])
        elif kind == "account_done":
            self._on_account_done(*ev[1:])
        elif kind == "account_failed":
            self._on_account_failed(*ev[1:])
        elif kind == "account_skipped":
            self._on_account_skipped(ev[1])
        elif kind == "batch_done":
            self._on_batch_done(*ev[1:])
        elif kind == "tray_show":
            self._show_window()
        elif kind == "tray_stop":
            if self.running:
                self._on_stop()
        elif kind == "tray_quit":
            self._on_tray_quit()

    def _on_connect_failed(self, account_id: str, message: str) -> None:
        state = self._states.get(account_id)
        if state is not None:
            state["connect_state"] = "idle"
        if account_id == self._selected_account_id:
            self._apply_connect_state()
            self._update_start_state()
        messagebox.showerror("连接失败", message, parent=self.root)

    def _on_account_begin(self, account_id: str) -> None:
        """某账户轮到开跑：清掉它上一轮的行，把状态切到「正在连接」。"""
        state = self._states.get(account_id)
        if state is None:
            return
        state["messages"] = []
        state["row_index"] = 0
        state["server_total"] = state["pending_total"] = state["backed"] = 0
        state["progress"] = None
        state["status_text"] = CONNECTING_STATUS_TEXT
        state["status_error"] = False
        state["count_text"] = "正在统计…"
        if account_id == self._selected_account_id:
            self._render_messages(state)
            self._restore_run_state(state)

    def _on_phase(self, account_id: str, text: str) -> None:
        state = self._states.get(account_id)
        if state is None:
            return
        state["status_text"] = text
        state["status_error"] = False
        if account_id == self._selected_account_id:
            self._set_status(text, error=False)

    def _on_totals(self, account_id: str, server_total: int, pending_total: int) -> None:
        state = self._states.get(account_id)
        if state is None:
            return
        state["server_total"] = server_total
        state["pending_total"] = pending_total
        if account_id != self._selected_account_id:
            return
        self.server_total, self.pending_total = server_total, pending_total
        maximum = max(pending_total, 1)
        self.progress.configure(maximum=maximum, value=0)
        state["progress"] = (0.0, float(maximum))
        self._refresh_counts()
        if pending_total == 0:
            self._set_status(UPTODATE_STATUS, error=False)

    def _on_message_event(self, account_id: str, data: dict) -> None:
        state = self._states.get(account_id)
        if state is None:
            return
        state["messages"].append(data)
        state["backed"] += 1
        if account_id != self._selected_account_id:
            return
        self.backed_count = state["backed"]
        self._row_index = state["row_index"] = state["row_index"] + 1
        self._insert_message_row(data, self._row_index)
        self._refresh_counts()

    def _on_account_done(self, account_id: str, backed: int, skipped: int, seconds: float,
                         stopped: bool, pending: int) -> None:
        """单个账户跑完：写结果文案（停止 / 已是最新 / 完成三态）。"""
        state = self._states.get(account_id)
        if state is None:
            return
        state["result"] = ("done", backed, skipped, seconds, pending)
        if stopped:
            state["status_text"] = f"已停止：本次备份 {backed} 封"
        elif pending == 0:
            state["status_text"] = UPTODATE_STATUS
        else:
            state["status_text"] = (f"完成：本次备份 {backed} 封，跳过 {skipped} 封（本地已存在），"
                                    f"用时 {_fmt_duration(seconds)}")
        state["status_error"] = False
        account = self._find_account(account_id)
        self.log.info("账户 %s 备份结束：备份 %d 封，跳过 %d 封，用时 %s%s",
                      account_display(account) if account else account_id, backed, skipped,
                      _fmt_duration(seconds), "（用户停止）" if stopped else "")
        if account_id != self._selected_account_id:
            return
        if not stopped and pending > 0:
            self.progress.configure(value=self.progress["maximum"])
            state["progress"] = (float(self.progress["maximum"]), float(self.progress["maximum"]))
        self._set_status(state["status_text"], error=False)

    def _on_account_failed(self, account_id: str, message: str) -> None:
        state = self._states.get(account_id)
        if state is None:
            return
        state["result"] = ("failed", message)
        state["status_text"] = "备份失败，详见运行日志"
        state["status_error"] = True
        if account_id == self._selected_account_id:
            self._set_status(state["status_text"], error=True)

    def _on_account_skipped(self, account_id: str) -> None:
        """停止后没轮到的账户：状态退回「已停止」，不动它上一轮的计数与结果。"""
        state = self._states.get(account_id)
        if state is None:
            return
        state["status_text"] = "已停止（未开始）"
        state["status_error"] = False
        if account_id == self._selected_account_id:
            self._set_status(state["status_text"], error=False)

    def _on_batch_done(self, stopped: bool, failures: list[tuple[str, str]]) -> None:
        """整个批量结束：复位控件，按后台/前台与成败给一次总结。"""
        elapsed = time.monotonic() - self._run_t0
        backed = sum(self._state(aid)["backed"] for aid in self._run_account_ids)
        skipped = sum(max(self._state(aid)["server_total"] - self._state(aid)["pending_total"], 0)
                      for aid in self._run_account_ids)
        hidden = self._background_hidden()
        self._end_run_controls()
        self.log.info("备份运行结束：账户 %d 个，备份 %d 封，跳过 %d 封，用时 %s%s",
                      len(self._run_account_ids), backed, skipped, _fmt_duration(elapsed),
                      "（用户停止）" if stopped else "")
        if hidden:
            if stopped:
                self._notify_background(TRAY_TITLE_STOPPED, TRAY_BODY_STOPPED)
            elif failures:
                self._notify_background(TRAY_TITLE_FAILED, TRAY_BODY_FAILED)
            else:
                self._notify_background(TRAY_TITLE_DONE,
                                        f"本次备份 {backed} 封，跳过 {skipped} 封，用时 {_fmt_duration(elapsed)}")
            return
        if failures:
            detail = failures[0][1] if len(failures) == 1 else f"{len(failures)} 个账户备份失败"
            messagebox.showerror("备份失败", detail, parent=self.root)

    def _end_run_controls(self) -> None:
        self.running = False
        self.stopping = False
        # 停表先于解锁：转圈 job 必须取消，否则复位后它还会按 running 判定自己退出，白跑一轮
        self._stop_run_spinner()
        # 两颗按钮一起复位：停止中收尾时停止按钮上也挂着转圈文案
        self._restore_start_button()
        self._restore_stop_button()
        # 先解锁配置区再走连接状态机：状态机可能重新禁用连接按钮（过期连接），顺序反了会被解锁覆盖回去
        self._set_config_enabled(True)
        self._apply_connect_state()
        self.stop_btn.configure(state="disabled")
        # 开始按钮按当前连接与勾选情况重新判定（不是一律 disabled）：失败路径原先停在禁用态，
        # 用户想重跑还得先去动一下复选框才解禁
        self._update_start_state()
        # 运行结束：常驻托盘图标的 tooltip 与菜单「停止备份」跟着复位（窗口可见时也挂着）
        self._sync_tray()

    def _set_status(self, text: str, error: bool) -> None:
        """写状态行（截断显示），并同步进可见账户的状态字典——切账户后还要原样回来。"""
        self._status_full = text
        self._status_error = error
        state = self._visible_state()
        if state is not None:
            state["status_text"] = text
            state["status_error"] = error
        self.status_label.configure(foreground="red" if error else "black")
        self._fit_status()

    def _set_counts(self, text: str) -> None:
        """写计数行（截断显示），并同步进可见账户的状态字典。"""
        self._count_full = text
        state = self._visible_state()
        if state is not None:
            state["count_text"] = text
        self._fit_counts()

    def _fit_counts(self) -> None:
        """计数文案与进度条的退让：文字变长先缩进度条，触底 PROGRESS_MIN_WIDTH 后文字才截断。

        Note:
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
        """把 full 截到标签请求宽 ≤ budget（像素）。

        Args:
            label: 要截断的标签。
            var: 承载截断结果的文本变量。
            full: 全文。
            budget: 像素预算。

        Note:
            font.measure 与标签请求宽差一个固定边距（本机约 6px），先留 8px 余量砍一刀，
            再按真实请求宽补一刀收口。
        """
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
        self._set_counts(f"服务器共 {self.server_total} ｜ 待备份 {self.pending_total} ｜ "
                         f"已备份 {self.backed_count} / {self.pending_total}")
        self.progress.configure(value=self.backed_count)

    def _clear_message_rows(self) -> None:
        """清掉上一轮的邮件行；序号归零，斑马纹从第 1 行重新起算，空态提示重新挂出。"""
        if children := self.tree.get_children():
            self.tree.delete(*children)
        self._row_index = 0
        state = self._visible_state()
        if state is not None:
            state["row_index"] = 0
        self._toggle_hint(self.list_ph, True)

    def _insert_message_row(self, d: dict, row_index: int) -> None:
        """把一封邮件画进邮件列表的第 row_index 行（斑马纹按视觉行号奇偶）。"""
        dt: datetime = d["timestamp"]
        ts_fmt = "%m-%d %H:%M" if dt.year == datetime.now().year else "%Y-%m-%d %H:%M"
        sender_raw = core.decode_mime_words(d["from"]) if d["from"] else ""
        name, addr = email.utils.parseaddr(sender_raw)
        sender = name or addr or "?"
        subject = core.decode_mime_words(d["subject"]) if d["subject"] else ""
        # 行都是 append 到 "end"，插入顺序即显示顺序，按序号奇偶就是按视觉行号
        if row_index == 1:
            self._toggle_hint(self.list_ph, False)
        tags = (ZEBRA_TAG,) if row_index % 2 == 0 else ()
        iid = self.tree.insert("", "end", values=(sender, subject or "(no subject)",
                                                  dt.strftime(ts_fmt), d["folder"],
                                                  core.pretty_byte_count(d["size"])), tags=tags)
        self.tree.see(iid)

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
        """窗口关闭按钮：按关闭行为偏好（隐藏到后台 / 退出 / 每次询问）分派。

        Note:
            窗口已隐藏时收到的关闭请求来自外部程序（安装器、卸载器、任务管理器），一律真退出——
            把程序卡在后台会让升级/卸载关不掉旧实例。
        """
        action = decide_close(self._close_action(), hidden=self.root.state() == "withdrawn")
        if action == "ask":
            answer = self._ask_close_action()
            if answer is None:
                return
            preference, remember = answer
            if remember:
                self._ui_settings[CLOSE_ACTION_KEY] = preference
                self.log.info("关闭行为偏好已记住：%s", preference)
            # 对话框说的是偏好口径（tray/exit，勾了记住就直接存），这里换成动作口径
            action = "hide" if preference == CLOSE_ACTION_TRAY else "exit"
        if action == "hide":
            self._hide_to_background()
        else:
            self._save_column_config()
            self._quit()

    def _ask_close_action(self) -> tuple[str, bool] | None:
        """关闭询问对话框：返回 (动作, 是否记住)，取消（X / Esc）返回 None。

        Note:
            展示与决策分离：测试替换本方法即可绕开真实 Toplevel（与 tk_popup 的替换口径一致）。
        """
        dialog = tk.Toplevel(self.root)
        dialog.title(CLOSE_DLG_TITLE)
        dialog.transient(self.root)
        dialog.resizable(False, False)
        answer: list[tuple[str, bool]] = []
        remember_var = tk.BooleanVar(value=False)
        body = tk.Frame(dialog)
        body.pack(fill="both", expand=True, padx=14, pady=12)
        tk.Label(body, text=CLOSE_DLG_TEXT_RUNNING if self.running else CLOSE_DLG_TEXT_IDLE,
                 justify="left", anchor="w").pack(fill="x")
        tk.Checkbutton(body, text=CLOSE_DLG_REMEMBER, variable=remember_var).pack(anchor="w", pady=(10, 0))
        buttons = tk.Frame(body)
        buttons.pack(fill="x", pady=(12, 0))

        def choose(action: str) -> None:
            answer.append((action, bool(remember_var.get())))
            dialog.destroy()

        tk.Button(buttons, text=CLOSE_DLG_EXIT, command=lambda: choose(CLOSE_ACTION_EXIT)).pack(side="right")
        tk.Button(buttons, text=CLOSE_DLG_HIDE,
                  command=lambda: choose(CLOSE_ACTION_TRAY)).pack(side="right", padx=(0, 6))
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - dialog.winfo_reqwidth()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - dialog.winfo_reqheight()) // 2
        dialog.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        dialog.grab_set()
        self.root.wait_window(dialog)
        return answer[0] if answer else None

    def _hide_to_background(self) -> None:
        """隐藏到后台：先存界面设置（此刻窗口还可见，几何与最大化状态读得对），再收进托盘。

        Note:
            托盘图标此时通常已经挂着（启动时就挂了），这里再 `show()` 一次是为了拿「图标确实
            可用」的结果：拿不到就退化为最小化到任务栏，窗口必须留一条回到用户眼前的路径。
        """
        self._save_column_config()
        if self._tray is not None and self._tray.show(self._tray_tooltip()):
            self.root.withdraw()
            self._sync_tray()
            self.log.info("窗口已隐藏到托盘（%s）", "备份运行中" if self.running else "空闲")
            return
        self.log.warning("托盘不可用，改为最小化到任务栏")
        self.root.iconify()

    def _show_window(self) -> None:
        """从后台恢复窗口；托盘图标常驻不动，只把 tooltip 追平当前状态。"""
        state = self.root.state()
        if state == "withdrawn":
            self.root.deiconify()
            # withdraw/deiconify 不带回最大化（真机实测：回来是 normal + 最大化前的正常几何），
            # 按记忆重放；iconic 分支不用管——Tk 自己记得最小化前的状态（含最大化）
            if self._window_maximized:
                try:
                    self.root.state("zoomed")
                except tk.TclError:
                    self.log.warning("无法恢复窗口最大化状态")
        elif state == "iconic":
            self.root.state("normal")
        self.root.lift()
        self.root.focus_force()
        self._sync_tray()
        self.log.info("窗口已从后台恢复")

    def _quit(self) -> None:
        """真正退出：移除托盘图标并销毁主窗口（关闭按钮与托盘「退出」共用）。"""
        if self._tray is not None:
            self._tray.close()
            self._tray = None
        self.root.destroy()

    def _background_hidden(self) -> bool:
        """窗口是否正隐藏着（托盘驻留）：后台结果通知按它分派。"""
        return self.root.state() == "withdrawn"

    def _notify_background(self, title: str, text: str) -> None:
        """后台驻留期间的运行结果：窗口保持隐藏，只弹托盘气泡（tooltip 与菜单同步追平）。"""
        if self._tray is None:
            return
        self._tray.notify(title, text)
        self._sync_tray()

    def _on_tray_quit(self) -> None:
        """托盘菜单「退出」：备份运行中先确认（退出会中断本次备份）。"""
        if self.running and not messagebox.askokcancel(QUIT_CONFIRM_TITLE, QUIT_CONFIRM_TEXT, parent=self.root):
            return
        self._save_column_config()
        self._quit()

    def run(self) -> None:
        """启动事件泵与 Tk 主循环。"""
        self.root.after(100, self._pump)
        self.root.mainloop()


def main() -> int:
    """程序入口：单实例检测后启动主窗口，返回进程退出码。

    Note:
        已有实例在运行时，本次启动改为唤起那个实例的窗口：GUI exe 没有控制台，
        原来那句「已经在运行了」用户根本看不到。
    """
    if _another_instance_running():
        if imapbackup_tray.request_restore():
            return 0
        print("IMAP Backup GUI 已经在运行了。", file=sys.stderr)
        return 1
    app = BackupApp()
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
