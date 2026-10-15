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
from collections.abc import Callable
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
START_BTN_LABEL = "开始备份"
SCANNING_BTN_LABEL = "正在统计…"   # 连接+统计阶段：转圈画在开始按钮上的文案
RUNNING_BTN_LABEL = "正在备份…"
STOP_BTN_LABEL = "停止"
STOPPING_BTN_LABEL = "正在停止…"  # 停止中：转圈挪到停止按钮上，开始按钮退回静止外观
STOP_ALL_BTN_LABEL = "停止所有"

# 账户备份阶段（self._states[id]["run_phase"]）：活动集合＝除 idle 外的四态
PHASE_IDLE = "idle"
PHASE_CONNECTING = "connecting"
PHASE_SCANNING = "scanning"
PHASE_BACKING = "backing"
PHASE_STOPPING = "stopping"
ACTIVE_PHASES = (PHASE_CONNECTING, PHASE_SCANNING, PHASE_BACKING, PHASE_STOPPING)

# 完成冷却＝绿勾显示窗＝「备份所选」跳过窗，同一个常量（ADR 0012）：刚跑完的账户短时间内
# 不被批量入口重连重扫，但右下「开始备份」可绕过它立即更新这一个账户。
DONE_COOLDOWN_S = 60
STOPPED_ICON_TTL_S = 5            # 已停止灰方块的显示窗（秒）：停止是用户发起的，只需一个短反馈
QUIT_STOP_TIMEOUT_S = 15.0        # 退出时等各账户写完当前封、干净收尾的上限，超时强退
SCAN_ICON_COLOR = "#2b6fd0"       # 统计中（含连接）：蓝色扫描

# 加载动画：开始按钮的加载态（停止中改画到停止按钮上）。
# 帧是盲文点阵（U+2800 段，Windows 字体普遍有字形），画在按钮文字前缀上，不需要图片资产；
# 按钮处在禁用态时 Tk 会用系统灰降级渲染，观感因此天然一致。
SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
SPINNER_INTERVAL_MS = 150

# 按钮图标：彩色图形只能由代码逐像素画出来——文字字形只有一种颜色，且按钮一进禁用态
# 就会被系统灰覆盖。不引图片资产：PhotoImage 运行时生成，打包零成本、没有图标路径问题。
PLAY_ICON_COLOR = "#1f9d55"     # 开始：绿色
STOP_ICON_COLOR = "#d13438"     # 停止：红色
ICON_MIN_SIZE = 9               # 图标边长下限(px)
ICON_LINESPACE_GAP = 5          # 图标边长 = 默认字体行高 − 这个值：行高变大图标同比例变大，不设上限
STOP_ICON_INSET_RATIO = 0.125   # 红方块相对外接框的内缩比例：方块视觉比同框三角重，缩一圈才平衡
ICON_STROKE = 2                 # 加号/叉的笔画粗细(px)
ICON_DISABLED_COLOR = "#9d9d9d"  # 禁用态灰度图标色（ttk 主题不给图片置灰，禁用时换挂）
ZEBRA_TAG = "zebra"    # 邮件列表偶数行（第 2、4… 行）的 tag；奇数行走 Treeview 默认白底
ZEBRA_BG = "#f0f0f0"   # 不跟随系统深色模式，与运行日志 Text 的 #ffffff 同一取舍

# 文件夹树的勾选态与层级符号（字形可用性以本机截图为准，缺字时退用 □/■/▦ 与 [-]/[+]）
CHECK_OFF = "☐"        # U+2610 未勾选
CHECK_ON = "☑"         # U+2611 已勾选
CHECK_PARTIAL = "⊟"    # U+229F 部分勾选
ARROW_OPEN = "▾"       # U+25BE 已展开
ARROW_CLOSED = "▸"     # U+25B8 已折叠
TREE_BRANCH = "├"      # U+251C 非末个同级节点的连接符
TREE_LAST = "└"        # U+2514 末个同级节点的连接符
ARROW_SLOT_W = 16      # 连接符+展开箭头的占位宽度，保证各级名称左边缘对齐
TREE_INDENT_W = 12     # 每级缩进(px)：子层挂进 kids 框的 padx，树形语言有了层级符号后收窄
FOLDER_ROW_PAD_Y = 1   # 文件夹行的上下 pady(px)；行距 = 行高 + 2×它，滚轮的按行对齐按行距算
WHEEL_DELTA = 120      # Windows 一格滚轮的标准 delta；高分辨率鼠标/触摸板给更小的值，须累加
FOLDER_WHEEL_ROWS = 1  # 一格滚轮滚几行：与下方「邮件列表」的 1 行/格（Tk Treeview 默认）一致
EMPTY_HINT_FG = "gray"            # 空态提示字色：只说明面板用途，不需要用户操作
FOLDER_PLACEHOLDER_TEXT = "这里会列出该邮箱的所有文件夹"
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
SETTINGS_SECTION_LIST_WIDTH = 160   # 左分类栏默认宽度(px)：右栏吃掉剩余宽度，可拖拽调宽
PAGE_BACKUP = "backup"
PAGE_SETTINGS = "settings"
PAGE_LABELS = ((PAGE_BACKUP, "备份"), (PAGE_SETTINGS, "设置"))

# 侧边栏拖拽调宽：备份页账户列表、设置页左栏与设置页账户列表各自独立、各自持久化（用户选定）。
# 拖拽手柄压在「侧边栏右边框与右侧内容之间」的空隙带上（隐形、只留光标提示），手柄宽＝带宽，
# 侧栏内容不再被手柄盖住；宽度钳在 [SIDEBAR_MIN_WIDTH, SIDEBAR_MAX_WIDTH]，
# 且拖拽中动态钳制（见 _sash_drag）：右侧内容最小完整宽吃不下就不再变宽。
SIDEBAR_MIN_WIDTH = 120
SIDEBAR_MAX_WIDTH = 240   # 硬上限（用户选定 240）：再宽右边详情显示不全
SIDEBAR_DEFAULT_WIDTH = 160
BACKUP_DETAIL_MIN_WIDTH = 420   # 备份页右侧（两卡+运行区）的最小完整宽：拖拽钳制的反推基数
BACKUP_SIDEBAR_WIDTH_KEY = "backup_sidebar_width"
SETTINGS_SIDEBAR_WIDTH_KEY = "settings_sidebar_width"
ACCOUNT_LIST_WIDTH_KEY = "settings_account_list_width"   # 设置页「账户」列表独立记忆宽度

# 版式封顶：卡片内容块不超过这个宽（窗口再大右侧留白，值列不无限拉伸），
# 设置页账户表单块同理（用户选定 640px）。超长值文本截断 + 悬停显示全文。
CARD_CONTENT_MAX_WIDTH = 620
ACCOUNT_FORM_MAX_WIDTH = 640

# 卡片内容四边内边距（用户选定统一 8px）：账户信息/输出/邮箱文件夹三卡同值。
CARD_PAD = 8
BTN_GAP = 4      # 按钮排里相邻按钮的统一间隔(px)：基准取备份页账户列表 全选/反选/清空 的间隔（用户选定）

# 上部两栏（账户信息列 ↔ 邮箱文件夹）固定 3:2（用户选定）：grid 权重做不到严格比例——
# 列宽 = 请求宽 + 按权重分余量，左列内容请求宽会在小窗下吃掉右列，改 place 定宽（_fit_top_columns）。
TOP_COLUMN_GAP = 8        # 两栏之间的间隙(px)，与面板间距基线同值
TOP_LEFT_SHARE = 3 / 5    # 账户信息列占两栏净宽的比例（账户信息:邮箱文件夹 = 3:2）
SERVER_MIN_BUDGET = 80    # 收件服务器值的像素地板：低于它就把 SSL chip 与端口挪到第二行
WRAP_HYSTERESIS = 12      # 换行迟滞带(px)：换行/恢复两个阈值错开，临界宽度来回拖不闪跳

# 收件服务器行同排时 chip/端口各段的 grid padx：布局与量宽共用同一组值——
# 「实测宽度 + 实际间隙」而不是估值（见 _layout_conn_row 的换行判定）。
CHIP_PADX_SAME_ROW = (8, 4)
PORT_CAPTION_PADX_SAME_ROW = (8, 0)
PORT_LABEL_PADX_SAME_ROW = (4, 4)

# SSL 色块 chip：方形色块标签（启用绿、未启用灰），备份卡只读展示与设置表单可点切换同一视觉。
SSL_CHIP_TEXT = "SSL"
SSL_CHIP_ON_BG = "#cdeccd"
SSL_CHIP_ON_FG = "#1e6b2f"
SSL_CHIP_OFF_BG = "#e2e2e2"
SSL_CHIP_OFF_FG = "#777777"

# 邮箱文件夹工具栏的刷新入口：icon-only 字形按钮（PhotoImage 画不了圆弧，用字形充当 icon）。
RELOAD_GLYPH = "⟳"        # U+27F3
RELOAD_BTN_TIP = "重新加载文件夹清单"
FOLDER_LOAD_FAIL_HINT = "文件夹清单加载失败：{reason}，点右上角刷新重试"
FOLDER_LOAD_NO_PASSWORD_HINT = "该账户未保存密码，点右上角刷新补录"
FOLDER_LOAD_INCOMPLETE_HINT = "连接信息不完整，请到设置页「账户」补全"
FOLDER_FAIL_REASON_MAX = 40   # 失败原因进提示行前的截断长度（防爆宽）
FOLDER_LOADING_TEXT = "正在加载邮箱文件夹…"   # 加载态（Q2）：树区整体替换为居中 spinner + 这行文案

# 保存反馈（Q5/Q9/Q10）：保存按钮旁的状态文案，一套出口，不再弹 messagebox。
SAVE_STATUS_OK_TEXT = "已保存"
SAVE_STATUS_FAIL_PREFIX = "保存失败："
SAVE_STATUS_CLEAR_MS = 3000   # 文案自动消失；重复保存重置计时
STATUS_OK_FG = SSL_CHIP_ON_FG  # 与 SSL chip 同一支绿
STATUS_ERR_FG = "#b3261e"

# 新增账户弹窗（Q11–Q13）与删除确认自绘弹窗（Q14）：与预检/密码补录/关闭询问同一家族。
NEW_ACCOUNT_DLG_TITLE = "新增账户"
NEW_ACCOUNT_DIALOG_CLOSE_DELAY_MS = 800   # 「已保存」的停留时长，随后自动关窗（用户选定 0.5–1s）
DELETE_DLG_TITLE = "删除账户"
DELETE_DLG_TEXT = "确定删除账户「{name}」吗？\n已备份到磁盘的邮件不会被删除。"
DELETE_DLG_CONFIRM = "删除"
CANCEL_BTN_TEXT = "取消"

# 账户（多账户配置）：连接设置在设置页「账户」分类维护；备份页左列表 + 右只读详情。
# 账户名称是纯显示名，不参与 <备份根>/<账号>/ 目录名生成（账号目录取邮箱地址，见 ADR 0002/0010）。
ACCOUNT_LIST_WIDTH = SIDEBAR_DEFAULT_WIDTH  # 备份页左侧账户列表默认宽度(px)：可拖拽调宽
ACCOUNT_LIST_TITLE = "账户"          # 备份页左侧列表的带标题边框（与「账户信息」卡同款）
ACCOUNT_NAME_FALLBACK = "未命名账户"
DEFAULT_ACCOUNT_SSL = True
DEFAULT_ACCOUNT_PORT = "993"
DEFAULT_BACKUP_ROOT = str(Path.home() / "邮箱备份")
CARD_EMPTY_TEXT = "—"               # 未选中账户时只读信息卡的占位
# 密码框的固定掩码：8 颗星，不回显内容也不透露真实位数；同时充当「未改动」哨兵——
# 原样保存 = 不修改已存密文，清空 = 删除已存密码，其余内容按新密码加密（见 _on_account_save）。
PASSWORD_MASK_TEXT = "********"
MAIL_TYPE_TEXT = "IMAP（仅接收，不发送）"   # 邮箱类型行的值：设置页表单与备份页卡片共用
HEADING_SIZE_DELTA = 2              # 设置页分类标题相对正文的字号增量(pt)
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

# 备份预检弹窗：点「开始备份/备份所选」后先串行检测各账户连接，全过才并发同步（ADR 0011）。
# 关闭弹窗 = 终止本次备份；密码错误弹密码补录窗（重试 / 跳过该账户）。
PREFLIGHT_TITLE = "连接检测"
PREFLIGHT_INTRO = "正在逐个检测账户连接，全部通过后自动开始备份。关闭本窗口将终止本次备份。"
PREFLIGHT_ROW_WAIT = "等待检测…"
PREFLIGHT_ROW_CHECKING = "正在连接…"
PREFLIGHT_ROW_OK = "✓ 连接成功"
PREFLIGHT_ROW_SKIPPED = "已跳过"
PREFLIGHT_ROW_CANCELLED = "已取消"
PREFLIGHT_CANCEL_BTN = "终止备份"
PASSWORD_PROMPT_TITLE = "输入密码"
PASSWORD_PROMPT_TEXT = "账户「{name}」的密码不正确（或未保存）。请输入密码后重试，或跳过该账户。"
PASSWORD_SAVE_CHECK = "保存密码（加密存储在本机）"
PASSWORD_RETRY_BTN = "重试"
PASSWORD_SKIP_BTN = "跳过该账户"
RUN_LOG_ACCOUNT_PREFIX = "[{name}] "   # 并发同步时日志行的账户前缀（日志混排、每行可辨）

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
# 活动账户未收尾时的退出确认（关闭按钮的「退出程序」与托盘退出共用，ADR 0012）
QUIT_CONFIRM_TITLE = "退出"
QUIT_CONFIRM_TEXT = ("仍有 {n} 个账户正在备份。\n\n"
                     "退出将先停止这些备份（当前邮件写完后干净退出），确定要退出吗？")
QUIT_CONFIRM_BTN = "退出程序"
QUIT_WAITING_TEXT = "正在停止，等待 {n} 个账户收尾…"
COOLING_WARN_TITLE = "账户刚完成备份"
COOLING_WARN_TEXT = ("以下账户刚完成备份，{cooldown} 秒内不会被「备份所选」重复启动：\n{names}\n\n"
                     "想立即更新其中某个账户，选中它后点右下角「开始备份」。")
ACTIVE_DELETE_WARN_TITLE = "账户正在备份"
ACTIVE_DELETE_WARN_TEXT = "请先停止该账户的备份，再删除账户。"
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


class _AccountPrefixFilter(logging.Filter):
    """给并发同步的单账户日志行加账户名前缀（日志混排、每行可辨，ADR 0011）。

    Note:
        挂在「每账户一个」的真实 Logger 上（core 的接口只收 Logger）：记录经本 logger 时
        在 msg 前拼前缀，再传播到 root 的 QueueLogHandler/文件 handler；% 格式化参数不受影响。
    """

    def __init__(self, prefix: str) -> None:
        super().__init__()
        self._prefix = prefix

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._prefix + str(record.msg)
        return True


def _account_logger(account_id: str, name: str) -> logging.Logger:
    """取（并重置）某账户的专用 Logger：只留当前这一轮的前缀 filter，避免重复叠加。"""
    logger = logging.getLogger(f"imapbackup.account.{account_id}")
    logger.filters = [_AccountPrefixFilter(RUN_LOG_ACCOUNT_PREFIX.format(name=name))]
    return logger


def _is_auth_failure(message: str) -> bool:
    """判定连接失败是否为「认证失败」（该弹密码补录窗而非报网络错误）。

    Note:
        core.connect_and_login 把 imaplib.IMAP4.error 统一包成 SystemExit
        "IMAP authentication failed: ..."，按这个前缀识别（与 core 的口径对齐）。
    """
    return message.startswith("IMAP authentication failed")


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


def _heading_font(base: tkfont.Font) -> tkfont.Font:
    """返回设置页分类标题字体：加粗、比基准大 HEADING_SIZE_DELTA 磅。

    Note:
        size 取绝对值——系统默认字号可能是负数（DPI 无关），Font 构造要正数。
    """
    return tkfont.Font(family=base.cget("family"),
                       size=abs(int(base.cget("size"))) + HEADING_SIZE_DELTA, weight="bold")


def _make_square_button(btn: ttk.Button) -> None:
    """把 icon-only 按钮钉成正方形（误差 ≤2px）。

    Note:
        tk/ttk 的 width 都是「0」字符宽单位（约 8px 粒度）：实测 ttk 的 ⟳ 默认 24×27、
        差 3px，字符步进 round(3/8)=0 直接卡死，永远进不了 ±2px。改用 ttk 的 padding
        按像素精调：先 `padding=[0, 0]` 量出内容基准，再把「目标边长 − 基准」对半摊到
        横/纵 padding，一次算准不迭代。目标边长取按钮默认请求高（与工具栏行高同源）。
    """
    btn.update_idletasks()
    target = btn.winfo_reqheight()
    btn.configure(padding=[0, 0])
    btn.update_idletasks()
    w0, h0 = btn.winfo_reqwidth(), btn.winfo_reqheight()
    btn.configure(padding=[max(0, (target - w0) // 2), max(0, (target - h0) // 2)])


def widget_state(widget: tk.Widget) -> str:
    """控件可用状态归一化：ttk 的 `cget("state")` 返回 index 对象（不是 str），tk 返回 str。"""
    if isinstance(widget, ttk.Widget):
        return "disabled" if widget.instate(["disabled"]) else "normal"
    return str(widget.cget("state"))


def _make_equal_button_row(parent: tk.Widget, specs: list[tuple[str, Callable[[], None]]]) -> list[ttk.Button]:
    """弹窗按钮排（messagebox 口径）：同排按钮等宽、间隔精确 BTN_GAP、整排居中。

    Args:
        parent: 按钮排的宿主。
        specs: [(按钮文案, command), …]，视觉顺序即列表顺序（主操作的位置由调用方定）。

    Returns:
        建好的按钮列表（顺序同 specs），调用方拿去做默认聚焦等后续处理。

    Note:
        等宽靠 grid 的 uniform 列：同组列取组内最大请求宽，按钮 `sticky="we"` 铺满列就渲染成
        等宽（= 最宽自然宽）——messagebox 按钮排的另一半特征。行内只放按钮，收缩到内容宽后
        `pack(anchor="center")` 居中（用户选定；调用方的容器要 `fill="x"` 才居得中，
        与状态文案同排时先包一层内容宽 group、再整体 `anchor="center"`）。
    """
    row = tk.Frame(parent)
    row.pack(anchor="center")
    buttons: list[ttk.Button] = []
    for i, (text, command) in enumerate(specs):
        btn = ttk.Button(row, text=text, command=command)
        # 两侧各 BTN_GAP//2（grid 口径，gui.md）：相邻间距合成 BTN_GAP，且各按钮可用宽一致——
        # 不对称 padx 会让末颗比别的宽一截（回归自等宽断言炸掉）
        btn.grid(row=0, column=i, sticky="we", padx=BTN_GAP // 2)
        row.columnconfigure(i, uniform="dlgbtn")
        buttons.append(btn)
    return buttons


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


def parse_sidebar_width(raw: object) -> int:
    """校验侧边栏宽度：钳在 [SIDEBAR_MIN_WIDTH, SIDEBAR_MAX_WIDTH]，越界/坏值回落默认。"""
    if isinstance(raw, int) and not isinstance(raw, bool):
        return max(SIDEBAR_MIN_WIDTH, min(SIDEBAR_MAX_WIDTH, raw))
    return SIDEBAR_DEFAULT_WIDTH


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


def _plus_icon(color: str, size: int) -> tk.PhotoImage:
    """加号（新增）：横竖两条居中笔画，内缩与方块同款（同框视觉重量接近）。"""
    img = tk.PhotoImage(width=size, height=size)
    inset = max(1, round(size * STOP_ICON_INSET_RATIO))
    mid = size // 2
    _put(img, color, inset, mid - ICON_STROKE // 2, size - inset, mid - ICON_STROKE // 2 + ICON_STROKE)
    _put(img, color, mid - ICON_STROKE // 2, inset, mid - ICON_STROKE // 2 + ICON_STROKE, size - inset)
    return img


def _cross_icon(color: str, size: int) -> tk.PhotoImage:
    """叉（删除）：两条对角线逐行阶梯画出，中点两笔交叉（中心像素必有颜色）。"""
    img = tk.PhotoImage(width=size, height=size)
    inset = max(1, round(size * STOP_ICON_INSET_RATIO))
    for y in range(inset, size - inset):
        x = min(y, size - inset - ICON_STROKE)                       # 45° 对角线（笔画不出内缩框）
        _put(img, color, x, y, x + ICON_STROKE, y + 1)
        xm = max(inset, size - inset - ICON_STROKE - (y - inset))    # 镜像对角线
        _put(img, color, xm, y, xm + ICON_STROKE, y + 1)
    return img


def _scan_icon(color: str, size: int) -> tk.PhotoImage:
    """放大镜（统计中，含连接阶段）：圆环 + 右下 45° 柄，蓝色逐像素画。"""
    img = tk.PhotoImage(width=size, height=size)
    cx = cy = round(size * 0.40)
    r = size * 0.30
    ring = max(1.2, size / 7.0)
    hx0 = round(cx + r * 0.55)
    hx1 = size - 1
    for y in range(size):
        for x in range(size):
            dx, dy = x - cx, y - cy
            dist2 = dx * dx + dy * dy
            on_ring = (r - ring) ** 2 <= dist2 <= (r + ring) ** 2
            if not on_ring and hx0 <= x <= hx1 and hx0 <= y <= hx1:
                d = abs((x - hx0) - (y - hx0))   # 点到 y=x 柄线的横向距离
                if d <= ring:
                    on_ring = True
            if on_ring:
                _put(img, color, x, y, x + 1, y + 1)
    return img


def _download_icon(color: str, size: int) -> tk.PhotoImage:
    """向下箭头（备份中）：竖杆接实心三角头，与开始/停止图标同外接框。"""
    img = tk.PhotoImage(width=size, height=size)
    stroke = max(2, round(size * 0.16))
    inset = max(1, round(size * STOP_ICON_INSET_RATIO))
    mid = size // 2
    tip_y = size - inset - 1
    head_top = round(size * 0.40)
    half = round(size * 0.32)
    # 竖杆：从顶到头部中部，与三角重叠一段
    _put(img, color, mid - stroke // 2, inset, mid - stroke // 2 + stroke, round(size * 0.62))
    # 三角头：从头部上沿收到中轴尖端
    height = max(1, tip_y - head_top)
    for y in range(head_top, tip_y + 1):
        t = (y - head_top) / height
        w = max(stroke, round(half * (1 - t) + stroke / 2))
        _put(img, color, mid - w // 2, y, mid - w // 2 + w, y + 1)
    return img


def _check_icon(color: str, size: int) -> tk.PhotoImage:
    """对勾（完成）：两段折线，先下后上，笔画粗细与加号/叉一致。"""
    img = tk.PhotoImage(width=size, height=size)
    stroke = max(2, round(size * 0.14))
    ax, ay = round(size * 0.14), round(size * 0.52)
    bx, by = round(size * 0.40), size - round(size * 0.16)
    cx, cy = size - round(size * 0.12), round(size * 0.18)

    def segment(px: int, py: int, qx: int, qy: int) -> None:
        steps = max(abs(qx - px), abs(qy - py), 1) * 4
        for i in range(steps + 1):
            t = i / steps
            x = round(px + (qx - px) * t)
            y = round(py + (qy - py) * t)
            _put(img, color, x - stroke // 2, y - stroke // 2,
                 x - stroke // 2 + stroke, y - stroke // 2 + stroke)

    segment(ax, ay, bx, by)
    segment(bx, by, cx, cy)
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
        # 每账户独立的协作停止信号（ADR 0012）：停一个账户不影响其余；账户启动时建、终态后留着
        self._stop_events: dict[str, threading.Event] = {}
        self._stopping_all = False              # 「停止所有」进行中：按钮转圈画在它自己身上
        # 本周期（活动集合「空→非空」到再次排空）的汇总记账，供结束日志与后台气泡
        self._cycle = {"t0": 0.0, "backed": 0, "skipped": 0, "failures": [], "stopped": False, "ids": set()}
        # 左栏勾选（「备份所选」跑哪些账户）：纯界面态，不落盘；新建/读入的账户默认勾上
        self._checked_accounts: set[str] = {account["id"] for account in self.accounts}
        self._account_rows: dict[str, dict] = {}

        # ---- state ----
        # running＝存在活动账户（run_phase ∈ ACTIVE_PHASES），由 _on_active_set_changed 维护；
        # 每账户各自的停止中状态在 state["run_phase"]=="stopping"，没有全局 stopping。
        self.running = False
        self.folder_rows: list[dict] = []
        self._folder_nodes: dict[tuple[str, ...], dict] = {}
        self._folder_clickables: list[tuple[tk.Label, str]] = []
        self._run_spinner_job: str | None = None  # root.after id，备份运行中开始按钮的转圈帧链
        self._loading_folder_ids: set[str] = set()  # 静默拉文件夹清单的去重：同一账户不重复起线程
        self._sash_grips: dict[str, tk.Frame] = {}   # 侧边栏拖拽手柄（backup/settings/account 三处）
        self._sash_layout: dict[str, tuple[tk.Widget, int, int]] = {}   # kind -> (宿主, 空隙带宽, 带左缘偏移)
        self._run_passwords: dict[str, str] = {}  # 本次运行的临时密码（预检补录但未保存时只在本次生效）
        self._preflight: dict | None = None  # 连接预检弹窗状态（None = 未在预检）
        self._quit_wait_win: tuple | None = None  # 退出确认窗转等待态后的 (弹窗, 文案 Label)
        self._backup_sidebar_width = self._ui_settings.get(
            BACKUP_SIDEBAR_WIDTH_KEY, SIDEBAR_DEFAULT_WIDTH)   # 备份页账户列表宽（可拖拽）
        self._settings_sidebar_width = self._ui_settings.get(
            SETTINGS_SIDEBAR_WIDTH_KEY, SIDEBAR_DEFAULT_WIDTH)  # 设置页左栏宽（可拖拽）
        self._account_list_width = self._ui_settings.get(
            ACCOUNT_LIST_WIDTH_KEY, SIDEBAR_DEFAULT_WIDTH)      # 设置页账户列表宽（可拖拽）
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
        self._attach_button_icons()   # 图标挂载统一入口：须在设置页建完（新增/删除按钮）之后
        self._build_context_menus()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.bind("<Configure>", self._on_root_configure, add="+")
        self._refresh_account_list()
        self._refresh_edit_list(self.accounts[0]["id"] if self.accounts else None)
        self._render_account_detail()
        self._update_start_state()
        self._maybe_load_folders(self._selected_account_id)   # 启动静默拉选中账户的文件夹清单
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
        self._config_widgets.append((widget, widget_state(widget)))
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
        # 手柄压在「账户栏右边框与右栏内容之间」那条 8px 空隙带上（= 右栏的左内边距），
        # 必须晚于 right 建出来：带落在 right 的地盘上，早建会被它整个盖住、点不到。
        self._make_sash(body, "backup", gap=8, offset=8)

    def _make_sash(self, host: tk.Widget, kind: str, gap: int, offset: int = 0) -> tk.Frame:
        """侧边栏与右侧内容之间空隙带上的拖拽手柄（overlay，不占布局宽、不改既有间距）。

        Args:
            host: 空隙带所在的宿主——带是它的内边距/格距，手柄作为它的子控件压在带上。
            kind: "backup"（备份页账户列表）/"settings"（设置页左栏）/"account"（设置页账户列表）。
            gap: 空隙带宽度(px)，手柄同宽：拖拽区就是「侧边栏右边框与右侧内容之间那一部分」（用户选定）。
            offset: 带左缘相对 host 左缘的偏移(px)，加当前侧栏宽即手柄 x。

        Note:
            三处各自独立持久化（用户选定）；背景取 host 同色，手柄隐形、只留光标提示。手柄
            先前贴在侧边栏内侧的右边线上，压出一条灰条（用户截图），挪进空隙带后带里本就没有内容。
            必须晚于占住这条带的兄弟内容创建：带常落在右侧内容的内边距上，早建会被兄弟节点
            整个盖住、点不到（z-order，见 tests 的命中测试）。
        """
        grip = tk.Frame(host, width=gap, background=str(host.cget("background")),
                        cursor="sb_h_double_arrow")
        grip.bind("<Button-1>", lambda e, k=kind: self._sash_start(e, k))
        grip.bind("<B1-Motion>", lambda e, k=kind: self._sash_drag(e, k))
        grip.bind("<ButtonRelease-1>", lambda e, k=kind: self._sash_end(e, k))
        self._sash_grips[kind] = grip
        self._sash_layout[kind] = (host, gap, offset)
        self._place_sash(kind)
        return grip

    def _place_sash(self, kind: str) -> None:
        """把手柄摆到空隙带上：x = 带左缘偏移 + 当前侧栏宽（改宽后必须重摆）。"""
        gap, offset = self._sash_layout[kind][1:]
        width = int(self._sash_target(kind).cget("width"))
        self._sash_grips[kind].place(x=offset + width, y=0, width=gap, relheight=1.0)

    def _sash_target(self, kind: str) -> tk.Widget:
        if kind == "backup":
            return self.account_pane
        if kind == "account":
            return self._account_nav_frame
        return self._settings_nav_frame

    def _sash_start(self, event, kind: str) -> None:
        target = self._sash_target(kind)
        self._sash_dragging = (kind, event.x_root, int(target.cget("width")))

    def _sash_drag(self, event, kind: str) -> None:
        dragging = getattr(self, "_sash_dragging", None)
        if not dragging:
            return
        drag_kind, start_x, start_w = dragging
        if drag_kind != kind:
            return
        delta = event.x_root - start_x
        # 动态上限每次 motion 重算 = 拖拽中实时判断右侧内容还能不能显示完整（用户选定）
        width = max(SIDEBAR_MIN_WIDTH, min(self._sash_max_width(kind), start_w + delta))
        self._apply_sidebar_width(kind, width)

    def _sash_max_width(self, kind: str) -> int:
        """拖拽中的宽度上限：硬上限与「右侧内容最小完整宽」反推的动态上限取小。

        Note:
            动态上限 = 窗口宽 - 同页其它杂项宽 - 右侧最小完整宽。右侧最小完整宽：
            设置页两处手柄取账户表单块的实测请求宽（钳在 320..ACCOUNT_FORM_MAX_WIDTH，
            表单块本身也封顶 640），备份页取 _backup_detail_min_width（含「邮箱文件夹」
            工具栏实测宽——先前只按 BACKUP_DETAIL_MIN_WIDTH 反推，拖宽后右上 ⟳ 会被挤没）。
            拖到右侧内容刚好完整显示就不再变宽——小窗口下侧栏再宽就把详情挤没了。
        """
        if kind == "backup":
            detail_min = self._backup_detail_min_width()
            others = 8   # 账户栏左侧那条页边距（pane.pack(padx=(8,0))）：右栏可用宽里没它
        else:
            form = getattr(self, "_account_form_block", None)
            detail_min = (min(max(form.winfo_reqwidth(), 320), ACCOUNT_FORM_MAX_WIDTH)
                          if form is not None else BACKUP_DETAIL_MIN_WIDTH)
            others = self._settings_sidebar_width if kind == "account" else self._account_list_width
        return max(SIDEBAR_MIN_WIDTH, min(SIDEBAR_MAX_WIDTH, self.root.winfo_width() - others - detail_min))

    def _backup_detail_min_width(self) -> int:
        """备份页右栏最小完整宽：整体最小宽 与 「邮箱文件夹」工具栏装得下反推的宽，取大。

        Note:
            工具栏（全选/清空/仅收件箱 + 右上 ⟳）是 pack 的定宽按钮排，卡宽不够时后打包的
            刷新按钮先被挤没（用户截图：⟳ 压成一条竖缝）。文件夹列宽 = (右栏宽 - 左右内边距
            - 栏间隙) × (1 - TOP_LEFT_SHARE)，卡内可用再扣 2×CARD_PAD 与卡片边框；把工具栏
            实测请求宽反推回右栏宽即下限，随字体/DPI 走，末尾留 2px 抹平取整。
        """
        toolbar = getattr(self, "_folder_toolbar", None)
        if toolbar is None or not hasattr(self, "folder_group"):
            return BACKUP_DETAIL_MIN_WIDTH
        border = int(self.folder_group.cget("borderwidth"))
        need = (toolbar.winfo_reqwidth() + 2 * CARD_PAD + 2 * border) / (1 - TOP_LEFT_SHARE)
        return max(BACKUP_DETAIL_MIN_WIDTH, round(need) + 2 * CARD_PAD + TOP_COLUMN_GAP + 2)

    def _clamp_backup_sidebar(self) -> None:
        """窗口变窄后把账户栏实际宽度收到「右侧内容完整」以内，⟳ 别再被挤没（用户截图）。

        Note:
            实际宽度 = min(用户选定的 `_backup_sidebar_width`, 拖拽上限)：窄窗只收实际宽度、
            不动用户选定的宽，窗口再拉宽自动回到原宽；与拖拽同口径（_sash_max_width），
            所以拖拽与缩放两条路判的都是「内容完整」。
        """
        if "backup" not in self._sash_layout or not hasattr(self, "account_pane"):
            return
        want = min(self._backup_sidebar_width, self._sash_max_width("backup"))
        if want != int(self.account_pane.cget("width")):
            self.account_pane.configure(width=want)
            self._place_sash("backup")

    def _sash_end(self, _event, kind: str) -> None:
        if getattr(self, "_sash_dragging", None):
            self._sash_dragging = None
            self._save_column_config()   # 拖完落盘（各处独立记忆）

    def _apply_sidebar_width(self, kind: str, width: int) -> None:
        if kind == "backup":
            self._backup_sidebar_width = width
            self.account_pane.configure(width=width)
        elif kind == "account":
            self._account_list_width = width
            self._account_nav_frame.configure(width=width)   # 账户编辑列表独立记忆宽（用户选定）
        else:
            self._settings_sidebar_width = width
            self._settings_nav_frame.configure(width=width)
        self._place_sash(kind)   # 侧栏一变宽，空隙带跟着挪，手柄重摆

    def _build_accounts_pane(self, parent: tk.Frame) -> tk.LabelFrame:
        """左侧账户列表：点选切换右侧详情，勾选决定「备份所选」跑哪些账户。

        边框用带标题的 LabelFrame，与右栏「账户信息」卡同款；行是自绘的（复选框 + 名称），
        一个控件只表达一件事——Listbox 一行放不下「点选切换」与「勾选备份」两个动作。
        """
        pane = self.account_pane = tk.LabelFrame(parent, text=ACCOUNT_LIST_TITLE)
        pane.configure(width=self._backup_sidebar_width)
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
        tools = tk.Frame(pane)
        tool_actions = (("全选", lambda: self._set_all_account_checks(True)),
                        ("反选", self._invert_account_checks),
                        ("清空", lambda: self._set_all_account_checks(False)))
        # 三格视觉等宽由 grid uniform 列 + sticky="we" 强制分配，与按钮自身请求宽无关；
        # width 只给负值（-1＝至少 1 字符、内容更长照长）：正 width 是固定文本槽，短中文案
        # 超出槽宽会被直接裁切（正 4 槽约 28px，放两字 CJK 勉强、放「浏览…」这类必裁）。
        for column, (label, command) in enumerate(tool_actions):
            tools.columnconfigure(column, weight=1, uniform="account-tools")  # 三格等宽
            ttk.Button(tools, text=label, width=-1, command=command).grid(row=0, column=column, sticky="we",
                                                                          padx=BTN_GAP // 2)  # 两侧各一半=BTN_GAP
        # 底部容器先于 tools 以 side=bottom 打包（先打包的落最底）：自上而下是 列表 / 勾选工具 /
        # [备份所选 / 停止所有]。容器恒定留 4px 下边距：「停止所有」隐藏时「备份所选」也不贴
        # LabelFrame 底边框；「停止所有」仅在有活动账户时 pack 出来（_on_active_set_changed 收口）。
        bottom_actions = tk.Frame(pane)
        bottom_actions.pack(side="bottom", fill="x", pady=(0, 4))
        self.backup_selected_btn = ttk.Button(bottom_actions, text=BACKUP_SELECTED_BTN_LABEL,
                                              command=self._on_start_selected)
        self.backup_selected_btn.pack(side="top", fill="x", padx=4, pady=(4, 0))
        self.stop_all_btn = ttk.Button(bottom_actions, text=STOP_ALL_BTN_LABEL, command=self._on_stop_all)
        # 初始不 pack：无活动账户时这颗按钮不存在于界面上
        tools.pack(side="bottom", fill="x", padx=2, pady=(4, 0))
        area.pack(fill="both", expand=True, pady=(4, 0))
        return pane

    def _build_config_area(self, parent: tk.Frame) -> None:
        outer = self._top_outer = tk.Frame(parent)
        # 下边距与运行区的上边距合起来 = 面板之间的 8px（上半区与下半区之间的那条）
        outer.pack(fill="x", padx=8, pady=(8, 4))
        left = self._left_col = tk.Frame(outer)
        self._build_conn_group(left)
        self._build_output_group(left)
        self._build_folder_group(outer)
        # 上部两栏固定 3:2（账户信息:邮箱文件夹，用户选定）：place 定宽（见 _fit_top_columns）。
        left.place(x=0, y=0)
        self.folder_group.place(x=0, y=0)
        outer.bind("<Configure>", self._fit_top_columns)
        # 底边对齐（图1「底部边框没有对齐」）：左列两卡叠出的自然高度是基准线（place 不定高），
        # 「邮箱文件夹」卡的高度与 outer 的高度都去贴它（用户选定「动邮箱文件夹的高度」），
        # 两卡下边框因此落在同一条线上；outer 的高度还要带住下方运行区的排布。
        left.bind("<Configure>", lambda _e: self._sync_top_region_height())

    def _fit_top_columns(self, event=None) -> None:
        """上部两栏定宽：账户信息:邮箱文件夹 = 3:2（净宽含 8px 栏间隙），所有窗口尺寸固定比例。

        Note:
            不用 grid 权重：列宽 = 请求宽 + 按权重分余量，左列内容请求宽几百像素，
            小窗下余量再怎么分右列都只剩零头（图1「邮箱文件夹被挤扁」的根因）。
        """
        total = int(event.width) if event is not None else self._top_outer.winfo_width()
        if total < 20:
            return
        self._clamp_backup_sidebar()   # 窗口变窄先收账户栏宽，右栏（含⟳）才完整；再分两栏
        inner = max(total - TOP_COLUMN_GAP, 0)
        left_w = round(inner * TOP_LEFT_SHARE)
        self._left_col.place_configure(x=0, width=left_w)
        self.folder_group.place_configure(x=left_w + TOP_COLUMN_GAP, width=inner - left_w)

    def _build_conn_group(self, parent: tk.Frame) -> None:
        """账户信息卡：连接设置已在设置页维护，这里只读展示（无任何输入框）。

        版式封顶：值列 weight=0 不拉伸（窗口再大右侧留白），超长值截断 + 悬停显示全文，
        整块内容宽度封顶 CARD_CONTENT_MAX_WIDTH（见 _fit_card_values）。
        """
        group = self.conn_group = tk.LabelFrame(parent, text="账户信息")
        # 与「账户信息」「邮箱文件夹」之间那条 8px 同宽：面板之间的横竖间距都取 8px
        group.pack(fill="x", pady=(0, 8))
        # 内容块四边内边距统一 CARD_PAD（用户选定）：行自带 pady=3，两段合成内容距边框 8px。
        # anchor="w" 而不是 fill="x"：内容块按内容定宽贴左，窗口再大右侧整片留白（用户选定）；
        # 一旦填满整卡，grid 会把自己居中在更宽的 master 里，看着又像居中了。
        inner = tk.Frame(group)
        inner.pack(padx=CARD_PAD, pady=CARD_PAD - 3, anchor="w")
        # 值列不设 weight：内容紧凑左对齐，不吃满整卡宽度（图1「分隔太远」的根因）
        inner.columnconfigure(1, weight=0)
        self._card_inners: list[tk.Frame] = [inner]            # 两卡内容块（值列同线用）
        self._card_caption_labels: list[tk.Label] = []         # 两卡标签（取最大宽做列下限）
        self._card_fits: list[dict] = []   # 每个可截断值的 {key,var,label,caption,tails,group}

        type_caption = tk.Label(inner, text="邮箱类型：")
        type_caption.grid(row=0, column=0, sticky="e", pady=3)
        self._card_caption_labels.append(type_caption)
        # 值列统一 padx=4：首行漏了它，「IMAP（仅接收，不发送）」比下一行值左移 4px（用户截图）
        tk.Label(inner, text=MAIL_TYPE_TEXT, anchor="w").grid(row=0, column=1, columnspan=5,
                                                             sticky="w", padx=4, pady=3)

        self._card_labels: dict[str, tk.Label] = {}
        self._card_vars: dict[str, tk.StringVar] = {}
        self._card_full: dict[str, str] = {}

        user_caption = tk.Label(inner, text="邮箱地址：")
        user_caption.grid(row=1, column=0, sticky="e", pady=3)
        self._card_caption_labels.append(user_caption)
        user_var = tk.StringVar(value=CARD_EMPTY_TEXT)
        user_label = tk.Label(inner, textvariable=user_var, anchor="w")
        user_label.grid(row=1, column=1, columnspan=5, sticky="w", padx=4, pady=3)
        self._card_labels["user"] = user_label
        self._card_vars["user"] = user_var
        self._card_fits.append({"key": "user", "var": user_var, "label": user_label,
                                "caption": user_caption, "tails": [], "group": group})
        self._bind_tip(user_label, user_var, lambda: self._card_full.get("user", ""))

        # 收件服务器与 SSL chip、端口同排（foxmail 版式）；卡片是只读展示，SSL 是色块 chip 不是文字值
        server_caption = tk.Label(inner, text="收件服务器：")
        server_caption.grid(row=2, column=0, sticky="e", pady=3)
        self._card_caption_labels.append(server_caption)
        server_var = tk.StringVar(value=CARD_EMPTY_TEXT)
        server_label = tk.Label(inner, textvariable=server_var, anchor="w")
        server_label.grid(row=2, column=1, sticky="w", padx=4, pady=3)
        self._card_labels["server"] = server_label
        self._card_vars["server"] = server_var
        ssl_chip = tk.Label(inner, text=SSL_CHIP_TEXT, padx=6, pady=1)
        ssl_chip.grid(row=2, column=2, sticky="w", padx=CHIP_PADX_SAME_ROW, pady=3)
        self._card_labels["ssl"] = ssl_chip
        self._style_ssl_chip(ssl_chip, False)
        port_caption = tk.Label(inner, text="端口：")
        port_caption.grid(row=2, column=3, sticky="e", padx=PORT_CAPTION_PADX_SAME_ROW, pady=3)
        self._card_caption_labels.append(port_caption)
        port_label = tk.Label(inner, text=CARD_EMPTY_TEXT, anchor="w")
        port_label.grid(row=2, column=4, sticky="w", padx=PORT_LABEL_PADX_SAME_ROW, pady=3)
        self._card_labels["port"] = port_label
        # 换行时的第二行容器：chip/端口挪进来紧凑排一排，不吃第一行的列宽——
        # 先前直接塞进 row3 的 col1/2/3，列宽按第一行定，端口被推到最右还截断（用户截图）。
        # 它跨列摆放（columnspan），不参与列宽计算，左边缘贴值列（服务器值左对齐）。
        self._conn_wrap_row = tk.Frame(inner)
        self._conn_row = {"chip": ssl_chip, "port_caption": port_caption, "port_label": port_label}
        self._conn_wrapped = False
        # 服务器值的可用宽要扣掉同排的 SSL chip/端口两截（tails），只截服务器名本身；
        # _layout_conn_row 换行时把 tails 清空——第二行不占服务器行的宽
        self._conn_server_spec = {"key": "server", "var": server_var, "label": server_label,
                                  "caption": server_caption,
                                  "tails": [ssl_chip, port_caption, port_label],
                                  "group": group}
        self._card_fits.append(self._conn_server_spec)
        self._bind_tip(server_label, server_var, lambda: self._card_full.get("server", ""))
        group.bind("<Configure>", lambda _e: self._fit_card_values())

    def _style_ssl_chip(self, chip: tk.Label, enabled: bool) -> None:
        """SSL chip 的两态配色：启用绿、未启用灰（备份卡只读展示与设置表单可点切换共用）。"""
        bg, fg = ((SSL_CHIP_ON_BG, SSL_CHIP_ON_FG) if enabled else (SSL_CHIP_OFF_BG, SSL_CHIP_OFF_FG))
        chip.configure(background=bg, foreground=fg, activebackground=bg, activeforeground=fg)

    def _layout_conn_row(self) -> None:
        """SSL chip 与端口的排位：放得下与收件服务器同排（foxmail 版式），放不下挪到第二行。

        Note:
            判定是实时的（每次卡片 <Configure> 重算）：各段取 winfo_reqwidth 实测宽 + 同排
            grid padx 实际间隙，拼上服务器值的像素地板 SERVER_MIN_BUDGET 与可用宽比。
            恢复同行比换行多要 WRAP_HYSTERESIS 的富余（迟滞带），临界宽度来回拖不闪跳。
            换行与否决定 _fit_card_values 给服务器值的预算里扣不扣 tails，先排位再截断；
            第二行进 _conn_wrap_row 紧凑排一排，不吃第一行的列宽。
        """
        group_w = self.conn_group.winfo_width()
        if group_w < 20:      # 未映射时宽度是 1，等真实 <Configure> 再排位
            return
        chip = self._conn_row["chip"]
        port_caption = self._conn_row["port_caption"]
        port_label = self._conn_row["port_label"]
        caption_w = self._conn_server_spec["caption"].winfo_reqwidth()
        pad_w = sum(sum(p) for p in (CHIP_PADX_SAME_ROW, PORT_CAPTION_PADX_SAME_ROW,
                                    PORT_LABEL_PADX_SAME_ROW))
        tails_w = sum(w.winfo_reqwidth() for w in (chip, port_caption, port_label)) + pad_w
        budget = min(CARD_CONTENT_MAX_WIDTH, group_w - 2 * CARD_PAD)
        needed = caption_w + tails_w + SERVER_MIN_BUDGET
        threshold = budget - (WRAP_HYSTERESIS if self._conn_wrapped else 0)
        wrapped = needed > threshold
        if wrapped == self._conn_wrapped:
            return
        self._conn_wrapped = wrapped
        for widget in (chip, port_caption, port_label):
            widget.grid_forget()
            widget.pack_forget()
        if wrapped:
            self._conn_wrap_row.grid(row=3, column=1, columnspan=4, sticky="w", pady=3)
            # 三段的 master 仍是 inner（控件换不了爹），pack 用 in_ 挂进换行容器紧凑排一排
            chip.pack(side="left", padx=(0, 4), in_=self._conn_wrap_row)
            port_caption.pack(side="left", padx=(8, 0), in_=self._conn_wrap_row)
            port_label.pack(side="left", padx=(4, 0), in_=self._conn_wrap_row)
            self._conn_server_spec["tails"] = []
        else:
            self._conn_wrap_row.grid_forget()
            chip.grid(row=2, column=2, sticky="w", padx=CHIP_PADX_SAME_ROW, pady=3)
            port_caption.grid(row=2, column=3, sticky="e", padx=PORT_CAPTION_PADX_SAME_ROW, pady=3)
            port_label.grid(row=2, column=4, sticky="w", padx=PORT_LABEL_PADX_SAME_ROW, pady=3)
            self._conn_server_spec["tails"] = [chip, port_caption, port_label]

    def _build_output_group(self, parent: tk.Frame) -> None:
        group = self._output_group = tk.LabelFrame(parent, text="输出")
        # expand：左列高度贴两栏底边对齐基准（见 _sync_top_region_height）后若还有富余，
        # 由「输出」卡吸收——两卡底边框永远落在同一条线上，卡内内容顶部对齐、下方留白。
        group.pack(fill="both", expand=True)
        # 内容块四边内边距统一 CARD_PAD（用户选定）：与「账户信息」卡同款（含 anchor="w" 贴左）
        inner = tk.Frame(group)
        inner.pack(padx=CARD_PAD, pady=CARD_PAD - 3, anchor="w")
        inner.columnconfigure(1, weight=0)   # 值列不拉伸：同「账户信息」封顶口径
        self._card_inners.append(inner)

        root_caption = tk.Label(inner, text="备份保存位置：")
        root_caption.grid(row=0, column=0, sticky="e", pady=3)
        self._card_caption_labels.append(root_caption)
        root_var = tk.StringVar(value=CARD_EMPTY_TEXT)
        self.card_root_label = tk.Label(inner, textvariable=root_var, anchor="w")
        self.card_root_label.grid(row=0, column=1, sticky="w", padx=4, pady=3)
        hint_label = tk.Label(inner, text=BACKUP_ROOT_SETTINGS_HINT, anchor="w",
                              foreground=EMPTY_HINT_FG)
        hint_label.grid(row=0, column=2, sticky="w")
        self._card_labels["root"] = self.card_root_label
        self._card_vars["root"] = root_var
        self._card_fits.append({"key": "root", "var": root_var, "label": self.card_root_label,
                                "caption": root_caption, "tails": [hint_label], "group": group})
        self._bind_tip(self.card_root_label, root_var, lambda: self._card_full.get("root", ""))
        self.backup_root_var.trace_add("write", lambda *_a: self._render_backup_root())
        group.bind("<Configure>", lambda _e: self._fit_card_values())

        fmt_row = tk.Frame(inner)
        fmt_row.grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 3))
        self.mbox_var = tk.BooleanVar(value=False)
        self.eml_var = tk.BooleanVar(value=True)
        self._register(tk.Checkbutton(fmt_row, text="mbox 输出", variable=self.mbox_var,
                                      command=self._on_format_change)).pack(side="left")
        self._register(tk.Checkbutton(fmt_row, text="eml 输出", variable=self.eml_var,
                                      command=self._on_format_change)).pack(side="left", padx=(12, 0))
        self._align_card_caption_columns()

    def _align_card_caption_columns(self) -> None:
        """两卡标签列同宽：「账户信息」与「输出」的值列起点落在同一条竖线（用户选定）。

        Note:
            标签列（grid 列 0）取所有标签的最大请求宽做下限 minsize，各卡标签仍右对齐、
            值列贴它起排；标签文案静态，建完两张卡量一次即可，无需挂事件。
        """
        caption_w = max((c.winfo_reqwidth() for c in self._card_caption_labels), default=0)
        for inner in self._card_inners:
            inner.columnconfigure(0, minsize=caption_w)

    def _fit_card_values(self) -> None:
        """按卡片宽度把超长值截断（悬停看全文）：值列 + 标题 + 尾巴 ≤ CARD_CONTENT_MAX_WIDTH。

        Note:
            先放全文量真实请求宽再决定截不截；省略号消失是双向的——变宽后从全文重算（同 _fit_status）。
            各卡片的值列互不影响，整体宽度由截断保证 ≤ 封顶值，窗口再大右侧只是留白。
            收件服务器行先排位（_layout_conn_row）再截断：SSL chip/端口换行与否决定扣不扣 tails。
        """
        self._layout_conn_row()
        for spec in self._card_fits:
            group_w = spec["group"].winfo_width()
            if group_w < 20:      # 未映射时宽度是 1，等真实 <Configure> 再算
                continue
            caption_w = spec["caption"].winfo_reqwidth()
            tails_w = sum(t.winfo_reqwidth() for t in spec["tails"])
            budget = min(CARD_CONTENT_MAX_WIDTH, group_w - 2 * CARD_PAD) - caption_w - tails_w
            if budget < 40:
                continue
            full = self._card_full.get(spec["key"]) or str(spec["var"].get())
            spec["var"].set(full)   # 先放全文拿真实请求宽
            if spec["label"].winfo_reqwidth() > budget:
                self._shrink_to_fit(spec["label"], spec["var"], full, budget)

    def _sync_top_region_height(self) -> None:
        """上半区高度贴合左列（账户信息+输出）自然高度，两卡底边落在同一条线上。

        Note:
            左列是 place 定宽不定高，它的请求高度就是两卡叠出的自然高度，是基准线；
            「邮箱文件夹」卡与 outer 的高度都听这里——place 子控件不向上传播请求高，
            外层 outer 的高度得显式给，下方运行区才排在上半区之下。
            兜底取两列请求高的较大者：卡高若低于内容的最小排布，grid 会把行顶出卡片、
            内容盖住 LabelFrame 的底边框（「下边线没了」），宁可整体加高也不能压掉边框。
        """
        left_h = max(self._left_col.winfo_reqheight(), self.folder_group.winfo_reqheight())
        if left_h <= 1:
            return
        # 高度走 place 级、不能用 configure：folder_group 的子控件在 grid 里，grid 传播会在子控件
        # 重新请求尺寸时把 height 选项覆盖回内容高（实测 119 掉回 52），卡片矮一截、底边就对不上；
        # place 的高度选项不受请求变化影响。左列同样定高，富余高度由「输出」卡吸收（expand），
        # 两卡底边框因此永远落在同一条线上。
        if self.folder_group.winfo_height() != left_h:
            self.folder_group.place_configure(height=left_h)
        if self._left_col.winfo_height() != left_h:
            self._left_col.place_configure(height=left_h)
        if self._top_outer.winfo_height() != left_h:
            self._top_outer.configure(height=left_h)

    def _build_folder_group(self, parent: tk.Frame) -> None:
        group = tk.LabelFrame(parent, text="邮箱文件夹")
        # 面板右边不留白：右边界与下方区域（邮件列表/运行日志/停止按钮）共用同一条线，
        # 即"内容右边距 == 内容左边距（8px）"。单独再留 8px 会让面板比下方窄一块。
        # 位置与宽高不归 grid：宽听 _fit_top_columns（上部两栏 3:2），高听 _sync_top_region_height。
        self.folder_group = group
        group.columnconfigure(0, weight=1)
        group.rowconfigure(1, weight=1)

        # 内容四边内边距统一 CARD_PAD（用户选定）：toolbar 顶左、树区左下同值，滚条仍贴面板右内沿。
        toolbar = tk.Frame(group)
        toolbar.grid(row=0, column=0, columnspan=2, sticky="we", padx=CARD_PAD, pady=(CARD_PAD, 0))
        self._folder_toolbar = toolbar   # 拖拽钳制按它的实测请求宽反推右栏最小宽（见 _backup_detail_min_width）
        # width=-7＝文本槽下限 7 字符（约 59px）：三颗取同一下限保持等宽整齐，「仅收件箱」
        # 更长也不会被裁；负号让槽只做下限、不做上限（正数是固定槽，长文案必被裁）。
        self._register(ttk.Button(toolbar, text="全选", width=-7, command=lambda: self._set_all_folders(True))).pack(side="left")
        self._register(ttk.Button(toolbar, text="清空", width=-7,
                                  command=lambda: self._set_all_folders(False))).pack(side="left", padx=(BTN_GAP, 0))
        self._register(ttk.Button(toolbar, text="仅收件箱", width=-7, command=self._set_inbox_only)).pack(side="left", padx=(BTN_GAP, 0))
        # 刷新入口（icon-only）：静默加载失败后唯一的手动重试入口，右对齐与文件夹工具同排。
        # 正方形（用户选定）完全靠 _make_square_button 的 padding 像素精调；width=0 让按钮
        # 先按 ⟳ 单字形取自然请求宽，padding 再补成精确正方形（实测误差 0px）。
        reload_btn = self._register(ttk.Button(toolbar, text=RELOAD_GLYPH, width=0,
                                               command=self._on_refresh_folders))
        _make_square_button(reload_btn)
        self.reload_btn = reload_btn
        reload_btn.pack(side="right", padx=(BTN_GAP, 0))   # 与左排至少 BTN_GAP（右贴工具栏内沿）
        self._bind_widget_tip(reload_btn, RELOAD_BTN_TIP)

        # 用嵌套 Frame 手搭树形，不用 ttk.Treeview：Treeview 的原生 <Button-1> 把整行都当展开/折叠
        # 目标，单行放不下"点箭头=展开、点名字=勾选"两个动作。这里箭头和名称都是真控件，命中区明确。
        # 高度只给 1px 的最小请求：grid 行不会缩到请求高以下，请求大了（曾是 200px）会把整个
        # grid 布局顶出卡片、盖掉 LabelFrame 底边框（用户截图「下边线没了」的根因）；
        # 实际高度由 row=1 的 weight=1 吃满卡高（高听 _sync_top_region_height）。
        self.folder_canvas = tk.Canvas(group, highlightthickness=0, height=1)
        self.folder_canvas.grid(row=1, column=0, sticky="nsew", padx=(CARD_PAD, 0), pady=(4, CARD_PAD))
        # 滚条紧贴面板内沿（右边不留白），与下方"邮件列表/运行日志"页签里的滚条贴边方式一致
        folder_scroll = ttk.Scrollbar(group, orient="vertical", command=self._folder_scroll_command)
        folder_scroll.grid(row=1, column=1, sticky="ns", pady=(4, CARD_PAD))
        self.folder_scroll = folder_scroll
        self.folder_canvas.configure(yscrollcommand=folder_scroll.set)
        self.folder_inner = tk.Frame(self.folder_canvas)
        self._folder_win = self.folder_canvas.create_window((0, 0), window=self.folder_inner, anchor="nw")
        self.folder_inner.bind("<Configure>", self._on_folder_inner_configure)
        # 树区宽度一变就重算名称截断（窗口缩放 / 分栏比例变化都从这里进，双向恢复全文）
        self.folder_canvas.bind("<Configure>", self._on_folder_canvas_configure)
        self.folder_ph = self._empty_hint(group, FOLDER_PLACEHOLDER_TEXT, 1, 0)
        # 加载态（Q2）：拉取清单期间动画层盖住整个树区（含滚条列），树与旧内容不接受交互；
        # 只 grid_remove 收放，树与滚条的格位参数不用动（_show_folder_loading）。
        self.folder_loading = tk.Frame(group)
        self.folder_loading_label = tk.Label(self.folder_loading, text=FOLDER_LOADING_TEXT)
        self.folder_loading_label.pack(expand=True)
        self._folder_loading_job = None

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
        outer.columnconfigure(1, weight=1)   # 列 0=左栏、列 1=右详情（吃剩余宽）
        outer.rowconfigure(0, weight=1)

        nav = self._settings_nav_frame = self._panel_style_frame(outer)
        nav.configure(width=self._settings_sidebar_width)
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
        # 手柄压在「左栏右边框与右侧详情之间」那条 8px 空隙带上（= nav 的 grid 右格距），
        # 挂 outer、隐形只留光标；晚于 detail 建出（z-order 保险，与备份页同款建序口径）。
        self._make_sash(outer, "settings", gap=8)

    def _build_accounts_settings(self, parent: tk.Frame, title: str) -> None:
        """「账户」分类：左账户列表 + 新增/删除，右连接设置编辑表单 + 保存。

        Note:
            保存是显式动作（含校验与落盘）：表单里可能只填了一半，逐键生效会把半成品写进设置。
        """
        body = tk.Frame(parent)
        body.pack(fill="both", expand=True, padx=8, pady=8)
        tk.Label(body, text=title, anchor="w",
                 font=_heading_font(tkfont.nametofont("TkDefaultFont"))).pack(fill="x")
        tk.Label(body, text=ACCOUNTS_HINT_TEXT, justify="left", anchor="w", foreground=EMPTY_HINT_FG,
                 wraplength=TIP_WRAP).pack(fill="x", pady=(4, 0))

        wrap = tk.Frame(body)
        wrap.pack(fill="both", expand=True, pady=(10, 0))
        nav = self._account_nav_frame = self._panel_style_frame(wrap)
        nav.configure(width=self._account_list_width)   # 独立记忆宽（用户选定），不跟设置左栏联动
        nav.pack_propagate(False)
        nav.pack(side="left", fill="y")
        # 新增/删除占满账户列表的全部宽度（Q15）：两列等权各占一半，随列表宽度拖拽联动；
        # 外边距 4px（Q1-A 按钮排基线，与备份页 全选/反选/清空 排净 4px 同值）
        tools = tk.Frame(nav)
        tools.pack(side="bottom", fill="x", padx=4, pady=4)
        tools.columnconfigure(0, weight=1)
        tools.columnconfigure(1, weight=1)
        # ttk.Button：vista 主题原生观感（与自绘弹窗按钮同口径，Q4/Q5）；图标挂 _attach_button_icons。
        # 两格视觉等宽由 grid uniform 列分配；width=-1 只做下限，避免正 width 固定槽裁 CJK 文案。
        self.add_btn = ttk.Button(tools, text="新增", width=-1, command=self._on_account_add)
        self.add_btn.grid(row=0, column=0, sticky="we")
        self.del_btn = ttk.Button(tools, text="删除", width=-1, command=self._on_account_delete)
        self.del_btn.grid(row=0, column=1, sticky="we", padx=(BTN_GAP, 0))
        # 列表 + 智能滚条：内容装不下才把滚条挂出来（与备份页账户列表同口径）
        area = tk.Frame(nav)
        area.pack(fill="both", expand=True)
        area.columnconfigure(0, weight=1)
        area.rowconfigure(0, weight=1)
        self.account_edit_list = tk.Listbox(area, exportselection=False, activestyle="none",
                                            background="#ffffff", highlightthickness=0, borderwidth=0)
        self.account_edit_list.grid(row=0, column=0, sticky="nsew")
        scroll = self.account_edit_scroll = ttk.Scrollbar(area, orient="vertical",
                                                          command=self.account_edit_list.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.account_edit_list.configure(yscrollcommand=scroll.set)
        self._account_edit_scroll_shown = False
        scroll.grid_remove()   # 去留由 _sync_account_edit_scroll 按内容定
        self.account_edit_list.bind("<<ListboxSelect>>", self._on_account_edit_select)
        self.account_edit_list.bind("<Configure>", lambda _e: self._sync_account_edit_scroll())

        form_host = tk.Frame(wrap)
        form_host.pack(side="left", fill="both", expand=True, padx=(10, 0))
        # 手柄压在「账户列表右边框与表单之间」那条 10px 空隙带上（= form_host 的左内边距），
        # 挂 wrap、隐形只留光标；必须晚于 form_host 建出——带落在它的地盘上，早建会被盖住点不到。
        self._make_sash(wrap, "account", gap=10)
        # 表单块封顶 ACCOUNT_FORM_MAX_WIDTH（图3「最大化时输入框被拉太长」）：place 定宽、
        # 内容自然高，块内输入框照常拉伸，块外留白；宽度随宿主收缩，窄窗不溢出。
        form = tk.Frame(form_host)
        form.place(x=0, y=0)
        self._account_form_block = form
        form_host.bind("<Configure>", self._fit_account_form_block)
        form.columnconfigure(1, weight=1)
        self._acct_name_var = tk.StringVar()
        self._acct_user_var = tk.StringVar()
        self._acct_server_var = tk.StringVar()
        self._acct_ssl_var = tk.BooleanVar(value=DEFAULT_ACCOUNT_SSL)
        self._acct_port_var = tk.StringVar(value=DEFAULT_ACCOUNT_PORT)
        self._acct_password_var = tk.StringVar()
        # 行序与收件服务器那排照 foxmail 的连接设置：类型置顶，SSL 与端口并进服务器行
        tk.Label(form, text="邮箱类型：").grid(row=0, column=0, sticky="e", padx=(0, 4), pady=3)
        tk.Label(form, text=MAIL_TYPE_TEXT, anchor="w").grid(row=0, column=1, columnspan=4, sticky="w", pady=3)
        form_entries = [
            self._account_form_row(form, 1, "名称：", self._acct_name_var),
            self._account_form_row(form, 2, "邮箱地址：", self._acct_user_var),
        ]
        server_entry = self._account_form_row(form, 3, "收件服务器：", self._acct_server_var)
        form_entries.append(server_entry)
        # SSL 用可点击切换的色块 chip（与备份卡同一视觉）：点一下翻状态，默认端口照旧联动
        ssl_chip = self._acct_ssl_chip = tk.Label(form, text=SSL_CHIP_TEXT, padx=6, pady=1,
                                                  cursor="hand2")
        ssl_chip.grid(row=3, column=2, sticky="w", padx=(8, 0), pady=3)
        ssl_chip.bind("<Button-1>", self._on_acct_ssl_chip_click)
        self._style_ssl_chip(ssl_chip, self._acct_ssl_var.get())
        tk.Label(form, text="端口：").grid(row=3, column=3, sticky="e", padx=(4, 4), pady=3)
        port_entry = tk.Entry(form, textvariable=self._acct_port_var, width=7)
        port_entry.grid(row=3, column=4, sticky="w", pady=3)
        form_entries.append(port_entry)

        tk.Label(form, text="密码：").grid(row=4, column=0, sticky="e", padx=(0, 4), pady=3)
        self._acct_password_entry = tk.Entry(form, textvariable=self._acct_password_var, show="*")
        self._acct_password_entry.grid(row=4, column=1, columnspan=4, sticky="we", pady=3)
        # 保存反馈（Q5/Q9）：状态文案与保存按钮同排、贴在按钮旁 8px，约 3 秒自动消失
        # 按钮自然宽（Q8）：与弹窗按钮、messagebox 同口径，不设 width=10
        save_row = tk.Frame(form)
        save_row.grid(row=5, column=1, sticky="w", pady=(6, 0))
        ttk.Button(save_row, text="保存", command=self._on_account_save).pack(side="left")
        self._account_save_status = tk.Label(save_row, text="")
        self._account_save_status.pack(side="left", padx=(8, 0))
        self._account_save_status_job = None

        for entry in [*form_entries, self._acct_password_entry]:
            for key in ("<Return>", "<KP_Enter>"):
                entry.bind(key, lambda _e: self._on_account_save())
            entry.bind("<Button-3>", self._on_entry_context)
        self._acct_password_entry.bind("<KeyPress>", self._on_acct_password_key)
        self._acct_password_entry.bind("<<Paste>>", self._on_acct_password_clip)
        self._acct_password_entry.bind("<<Cut>>", self._on_acct_password_clip)
        self._account_form_entries = [*form_entries, self._acct_password_entry]

    def _account_form_row(self, form: tk.Frame, row: int, caption: str, var: tk.StringVar) -> tk.Entry:
        """连接设置表单的一行：右对齐标签 + 吃满剩余宽度的输入框。"""
        tk.Label(form, text=caption).grid(row=row, column=0, sticky="e", padx=(0, 4), pady=3)
        entry = tk.Entry(form, textvariable=var)
        entry.grid(row=row, column=1, sticky="we", pady=3)
        return entry

    def _fit_account_form_block(self, event) -> None:
        """把账户表单块宽度钉在 min(宿主宽, ACCOUNT_FORM_MAX_WIDTH)：块内拉伸、块外留白。"""
        self._account_form_block.place_configure(width=min(int(event.width), ACCOUNT_FORM_MAX_WIDTH))

    def _build_backup_settings(self, parent: tk.Frame, title: str) -> None:
        """「备份」分类：全局唯一的备份保存位置（所有账户共用，见 ADR 0010）。"""
        body = tk.Frame(parent)
        body.pack(fill="both", expand=True, padx=8, pady=8)
        tk.Label(body, text=title, anchor="w",
                 font=_heading_font(tkfont.nametofont("TkDefaultFont"))).pack(fill="x")

        row_host = tk.Frame(body)
        row_host.pack(fill="x", pady=(10, 0))
        # path 行封顶 ACCOUNT_FORM_MAX_WIDTH（与账户表单同口径）：place 定宽、块内拉伸、块外留白，
        # 免得最大化窗口时输入行拉成一条长线
        row = tk.Frame(row_host)
        row.place(x=0, y=0)
        self._backup_root_row = row
        row_host.bind("<Configure>", self._fit_backup_root_row)
        tk.Label(row, text="备份保存位置：").pack(side="left")
        self.backup_root_entry = tk.Entry(row, textvariable=self.backup_root_var)
        self.backup_root_entry.pack(side="left", fill="x", expand=True, padx=4)
        # width=-1 贴内容（「浏览…」三字带省略号，正 width=4 固定槽实测裁掉尾字）
        ttk.Button(row, text="浏览…", width=-1, command=self._on_browse_backup_root).pack(side="left")
        self.backup_root_entry.bind("<Button-3>", self._on_entry_context)
        tk.Label(body, text=BACKUP_ROOT_HINT_TEXT, justify="left", anchor="w", foreground=EMPTY_HINT_FG,
                 wraplength=TIP_WRAP).pack(fill="x", pady=(8, 0))

    def _fit_backup_root_row(self, event) -> None:
        """把备份保存位置行钉在 min(宿主宽, ACCOUNT_FORM_MAX_WIDTH)：块内拉伸、块外留白。

        Note:
            行是 place 子控件、不向宿主传播请求高，宿主的高度显式贴行高，下方说明文字才不压上来。
        """
        self._backup_root_row.place_configure(width=min(int(event.width), ACCOUNT_FORM_MAX_WIDTH))
        row_h = self._backup_root_row.winfo_reqheight()
        if row_h > 1 and event.height != row_h:
            event.widget.configure(height=row_h)

    def _build_window_settings(self, parent: tk.Frame, title: str) -> None:
        """「窗口与后台」分类的详情：关闭行为三态 + 说明文字。

        Note:
            控件不进 _register 名单：关闭行为不影响正在跑的备份，运行中也不必跟着禁用。
        """
        body = tk.Frame(parent)
        body.pack(fill="both", expand=True, padx=8, pady=8)
        tk.Label(body, text=title, anchor="w",
                 font=_heading_font(tkfont.nametofont("TkDefaultFont"))).pack(fill="x")
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
        ttk.Button(log_toolbar, text="打开日志文件夹", command=self._on_open_log_dir).pack(side="left")
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
        # 两颗按钮 width=-1：负 width 是文本槽下限（至少 1 字符），按钮请求宽随当前文案+图标
        # 贴合——静止态「开始备份」74px/「停止」50px，加载态长文案「⠋ 正在备份/停止…」长到 97px。
        # 不能用正 width（如 4）：正数是固定槽（约 4 个 avgchar=28px），长文案被标签元素直接
        # 裁切、按钮也不会被撑宽（旧注释「下限、照样撑宽」是错的，实测）。盲文转圈 10 帧等宽，
        # 帧内不横跳，宽度只在状态切换瞬间变一次；pack(side=right) 变宽时向左生长，进度条让宽。
        self.stop_btn = ttk.Button(bar, text=STOP_BTN_LABEL, width=-1, state="disabled", command=self._on_stop)
        self.stop_btn.pack(side="right")
        self.start_btn = ttk.Button(bar, text=START_BTN_LABEL, width=-1, command=self._on_start)
        self.start_btn.pack(side="right", padx=(0, BTN_GAP))
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
        """给带图标按钮挂上代码画的彩色图标（开始=绿三角、停止=红方块、新增=绿加号、删除=红叉）。

        Note:
            位图必须由 Python 侧持引用：Tkinter 不持引用，PhotoImage 一旦被 GC，按钮上的图标
            会变空白。blank_icon 是加载态的透明占位（见 _blank_icon），同样要持引用。
            时机在设置页建完之后（新增/删除按钮在 `_build_settings_account` 里）。
        """
        size = _icon_size()
        self.start_icon = _play_icon(PLAY_ICON_COLOR, size)
        self.stop_icon = _stop_icon(STOP_ICON_COLOR, size)
        self.blank_icon = _blank_icon(size)
        self.add_icon = _plus_icon(PLAY_ICON_COLOR, size)
        self.del_icon = _cross_icon(STOP_ICON_COLOR, size)
        # 灰度版（Q4）：ttk 主题只压暗按钮面、不给图片置灰，禁用时换挂灰度图
        self.start_icon_off = _play_icon(ICON_DISABLED_COLOR, size)
        self.stop_icon_off = _stop_icon(ICON_DISABLED_COLOR, size)
        self.start_btn.configure(image=self.start_icon, compound="left")
        self.stop_btn.configure(image=self.stop_icon_off, compound="left")   # 创建即禁用：先挂灰度
        self.add_btn.configure(image=self.add_icon, compound="left")
        self.del_btn.configure(image=self.del_icon, compound="left")
        # 「停止所有」与右下停止钮同红方块；停止中转圈期换 1px 透明占位（与单账户停止同口径）
        self.stop_all_btn.configure(image=self.stop_icon, compound="left")
        # 账户行行尾状态图标（ADR 0012）：同尺寸、代码逐像素；blank 是整格透明位图，
        # 与按钮转圈用的 1px 宽 blank_icon 不是一个东西——行槽要固定占住图标宽，名称才不横跳
        self.row_icon_blank = tk.PhotoImage(width=size, height=size)
        self.row_scan_icon = _scan_icon(SCAN_ICON_COLOR, size)
        self.row_download_icon = _download_icon(PLAY_ICON_COLOR, size)
        self.row_done_icon = _check_icon(PLAY_ICON_COLOR, size)
        self.row_stopped_icon = _stop_icon(ICON_DISABLED_COLOR, size)
        self.row_fail_icon = _cross_icon(STOP_ICON_COLOR, size)
        self.row_stop_icon = _stop_icon(STOP_ICON_COLOR, size)

    # ------------------------------------------------------- connect handlers

    def _visible_state(self) -> dict | None:
        """当前可见账户的状态字典；没有账户时返回 None（账户存在时按需建立）。"""
        if self._selected_account_id is None or self._find_account(self._selected_account_id) is None:
            return None
        return self._state(self._selected_account_id)

    def _state(self, account_id: str) -> dict:
        """取（必要时建立）某账户的独立状态：文件夹树、邮件行、计数与进度。"""
        state = self._states.get(account_id)
        if state is None:
            state = {"snapshot": None,              # (server, user, password_dpapi) 静默拉清单当刻的值
                     "folder_rows": [], "open_keys": None,  # open_keys=None → 首次全部展开
                     "messages": [], "row_index": 0,
                     "server_total": 0, "pending_total": 0, "backed": 0,
                     "count_text": "尚未开始备份", "status_text": "", "status_error": False,
                     "progress": None, "result": None,
                     "run_phase": PHASE_IDLE,        # 账户独立备份阶段（见 ACTIVE_PHASES）
                     "result_at": None,             # 终态时刻（monotonic）：冷却窗与绿勾 TTL 共用
                     "icon_job": None}              # 终态图标的自动清除 after 句柄
            self._states[account_id] = state
        return state

    def _find_account(self, account_id: str | None) -> dict | None:
        for account in self.accounts:
            if account["id"] == account_id:
                return account
        return None

    def _account_password(self, account: dict) -> str:
        """解出账户密码明文；没存过或解不开（换机器/换账户）都按空串处理。

        Note:
            兜底取本次补录的临时密码（_run_passwords，勾了「保存」才落密文）——补录窗
            选不保存也能先把文件夹清单拉出来，只在本次会话生效。
        """
        cipher = account.get("password_dpapi")
        if isinstance(cipher, str) and cipher:
            return _dpapi_decrypt(cipher) or ""
        return self._run_passwords.get(str(account.get("id", "")), "")

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

    def _maybe_load_folders(self, account_id: str | None) -> None:
        """按需静默拉某账户的文件夹清单（启动 / 切账户 / 保存账户后触发）。

        Note:
            不是用户可见的连接动作：无按钮、无状态图标。账户没有清单、或连接参数变过
            （快照口径）才拉；同一账户进行中不重复起线程。失败在文件夹区提示可刷新重试，
            密码类失败再弹密码补录窗（补完自动重拉，见 ADR 0011 的连接口径）。
        """
        if account_id is None:
            return
        account = self._find_account(account_id)
        if account is None:
            return
        state = self._state(account_id)
        if account_id in self._loading_folder_ids:
            return
        if state["snapshot"] is not None and state["snapshot"] == self._account_snapshot(account):
            return
        v = self._account_credentials(account, warn=False)
        if v is None:
            if not self._account_password(account):
                self._show_folder_hint(account_id, FOLDER_LOAD_NO_PASSWORD_HINT)
                self._maybe_prompt_folder_password(account_id)
            else:
                self._show_folder_hint(account_id, FOLDER_LOAD_INCOMPLETE_HINT)
            return
        self._loading_folder_ids.add(account_id)
        threading.Thread(target=self._folder_worker,
                         args=(account_id, v, self._account_snapshot(account)), daemon=True).start()
        self._sync_folder_loading_view()   # 加载态立即显示（Q3）：可见账户才盖动画层

    def _on_refresh_folders(self) -> None:
        """手动刷新（邮箱文件夹工具栏的 ⟳）：丢掉快照强制重拉，密码类失败照旧弹补录。

        Note:
            「同快照只弹一次」的记忆一并清掉——手动刷新就是要再问一次密码。
        """
        account_id = self._selected_account_id
        if account_id is None or self._visible_account_active():
            return
        state = self._state(account_id)
        state["snapshot"] = None
        state["password_prompted"] = None
        self._maybe_load_folders(account_id)

    def _show_folder_hint(self, account_id: str, text: str) -> None:
        """文件夹区的提示行：只对可见账户显示；树上已有内容时不打扰（提示叠在树上没意义）。"""
        if account_id != self._selected_account_id or self.folder_rows:
            return
        self.folder_ph.configure(text=text)
        self._toggle_hint(self.folder_ph, True)

    def _show_folder_loading(self, show: bool) -> None:
        """加载层的收放：盖在树区上方（Q2 的「整体替换」观感），收掉即恢复树与提示的原样。"""
        self.folder_loading.grid_remove()
        if show:
            self.folder_loading.grid(row=1, column=0, columnspan=2, sticky="nsew")
            self.folder_loading.tkraise()

    def _start_folder_loading(self) -> None:
        """文件夹加载动画的帧链入口（防重复进入：旧 job 不取消会留下第二条帧链）。"""
        if self._folder_loading_job is None:
            self._folder_loading_tick(0)

    def _folder_loading_tick(self, index: int) -> None:
        """画第 index 帧；可见账户仍在拉取就排下一帧，否则收链（帧推进单独成方法，测试可直接驱动）。"""
        if self._selected_account_id not in self._loading_folder_ids:
            self._folder_loading_job = None
            return
        self.folder_loading_label.configure(
            text=f"{SPINNER_FRAMES[index % len(SPINNER_FRAMES)]} {FOLDER_LOADING_TEXT}")
        self._folder_loading_job = self.root.after(SPINNER_INTERVAL_MS, self._folder_loading_tick, index + 1)

    def _stop_folder_loading(self) -> None:
        if self._folder_loading_job is not None:
            self.root.after_cancel(self._folder_loading_job)
            self._folder_loading_job = None

    def _sync_folder_loading_view(self) -> None:
        """加载态的唯一裁决点（Q8）：可见账户在拉清单就盖动画层并禁 ⟳，否则收掉恢复。

        Note:
            只跟随当前选中账户：别的账户在后台加载/完成都不打断眼前的树（切账户经
            `_render_account_detail` 回到这里换视图）。⟳ 的禁用只管加载态；运行中的
            配置锁由 `_set_config_enabled` 负责，两者不抢同一个 state。
        """
        loading = (self._selected_account_id is not None
                   and self._selected_account_id in self._loading_folder_ids)
        self._show_folder_loading(loading)
        if not self._visible_account_active():
            self.reload_btn.configure(state="disabled" if loading else "normal")
        if loading:
            self._start_folder_loading()
        else:
            self._stop_folder_loading()

    def _maybe_prompt_folder_password(self, account_id: str) -> None:
        """密码类失败的补录入口：同账户同快照只弹一次（手动刷新清记忆后必再弹）。

        Note:
            经 after_idle 弹窗：启动期也走这里，等主循环起来再弹模态窗，不在 __init__ 里嵌事件循环。
        """
        account = self._find_account(account_id)
        if account is None:
            return
        state = self._state(account_id)
        snapshot = self._account_snapshot(account)
        if state.get("password_prompted") == snapshot:
            return
        state["password_prompted"] = snapshot
        self.root.after_idle(lambda: self._ask_folder_password(account_id))

    def _ask_folder_password(self, account_id: str) -> None:
        """弹密码补录窗：拿到密码就存（勾了保存）并强制重拉清单；跳过则维持失败提示。"""
        answer = self._ask_password(account_id)
        if answer is None:
            return
        password, save = answer
        self._run_passwords[account_id] = password
        account = self._find_account(account_id)
        if save and account is not None:
            cipher = _dpapi_encrypt(password) if sys.platform == "win32" else None
            if cipher:
                account["password_dpapi"] = cipher
                self._save_column_config()
        state = self._state(account_id)
        state["snapshot"] = None
        state["password_prompted"] = None
        self._maybe_load_folders(account_id)

    def _core_config(self, v: dict, basedir: Path | None = None, eml_dir: Path | None = None) -> core.Config:
        return core.Config(overwrite=False, usessl=v["ssl"], thunderbird=False, nospinner=True,
                           basedir=basedir, icloud=False, quiet=False, user=v["user"], server=v["server"],
                           password=v["password"], timeout=60, port=v["port"], eml_dir=eml_dir)

    def _folder_worker(self, account_id: str, v: dict, snapshot: tuple[str, str, str]) -> None:
        """静默拉文件夹清单的线程入口：登录并 LIST，结果经事件队列回主线程。"""
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
            self.events.put(("folder_load_failed", (account_id, str(e))))
        except Exception as e:
            log.exception("拉取文件夹清单失败")
            self.events.put(("folder_load_failed", (account_id, f"{type(e).__name__}: {e}")))

    def _on_folders_loaded(self, account_id: str, names: list[tuple], snapshot: tuple[str, str, str]) -> None:
        """静默拉到清单：为该账户建文件夹行、记下快照，可见时才动界面。"""
        self._loading_folder_ids.discard(account_id)
        account = self._find_account(account_id)
        if account is None:
            return
        state = self._state(account_id)
        if account_id == self._selected_account_id:
            self._sync_visible_folder_choices()  # 重建前先把界面上的勾选收回账户，重建不丢改动
        self._fill_folder_rows(state, account, names)
        state["snapshot"] = snapshot
        self.log.info("账户 %s：已加载 %d 个邮箱文件夹", account_display(account), len(names))
        if account_id == self._selected_account_id:
            self._render_folder_tree(state)
            self._update_start_state()
        self._sync_folder_loading_view()   # 收动画层；后台账户完成不打断可见账户的显示（Q8）

    def _on_folder_load_failed(self, account_id: str, message: str) -> None:
        """静默拉清单失败：写日志 + 文件夹区提示可刷新重试；密码类失败再弹补录窗。"""
        self._loading_folder_ids.discard(account_id)
        account = self._find_account(account_id)
        name = account_display(account) if account else account_id
        self.log.warning("账户 %s：文件夹清单加载失败（%s）", name, message)
        reason = message if len(message) <= FOLDER_FAIL_REASON_MAX else message[:FOLDER_FAIL_REASON_MAX] + ELLIPSIS
        self._show_folder_hint(account_id, FOLDER_LOAD_FAIL_HINT.format(reason=reason))
        self._sync_folder_loading_view()   # 失败即退出加载态（Q4），旧树/提示恢复显示
        if _is_auth_failure(message):
            self._maybe_prompt_folder_password(account_id)

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
        # 同级末个节点的连接符取 └、其余取 ├（见 _make_folder_node）；keys 已按 (深度, 名) 排好，
        # 每组兄弟的最后一条就是末个
        by_parent: dict[tuple, list[tuple]] = {}
        for k in keys:
            by_parent.setdefault(k[:-1], []).append(k)
        last_keys = {siblings[-1] for siblings in by_parent.values()}
        for key in keys:  # 父节点必然先于子节点建成
            self._make_folder_node(key, real.get(key), with_kids, state["open_keys"], key in last_keys)

        self.folder_rows.sort(key=lambda r: r["path"])
        state["folder_rows"] = self.folder_rows
        # 空树不改展开记忆：open_keys 保持原值（None 表示「下次加载全展开」），
        # 免得空态渲染把 None 变成空集合、下一次加载整棵树都是收起的
        if self.folder_rows:
            state["open_keys"] = {key for key, node in self._folder_nodes.items() if node["open"]}
        self._render_folder_checks()
        self._wheel_accum = 0   # 换了棵树，上一棵树没攒满一格的滚轮余量作废
        # 空树不会触发 folder_inner 的 <Configure>（只有增长触发），请求高度也不会自行重算，
        # 范围与行距在这里显式归零——不然上一棵树的区域和单位会留在没内容的树上
        if not self.folder_rows:
            self._sync_folder_scroll()
        # 重建会铺满树区，空态提示得跟着收掉；一个文件夹都没解析出来时重新挂回（提示文还原成默认，
        # 上一轮失败提示的文案不留到下棵树上）
        self.folder_ph.configure(text=FOLDER_PLACEHOLDER_TEXT)
        self._toggle_hint(self.folder_ph, not self.folder_rows)
        # 换了一棵树：旧树的滚动位置对新树没有意义，视图放回顶部（仍溢出时长树也从第一行开始，
        # 只重写滚动范围的话 Tk 会把原点钳到新底部）。滚动范围与行距随后由 after_idle 同步按
        # 新树重写——变矮不送达 <Configure>，旧树的滚动范围会一直留着；与 _refresh_account_list 同宗
        self.folder_canvas.yview_moveto(0)
        self.folder_canvas.after_idle(self._sync_folder_scroll)

    def _make_folder_node(self, key: tuple[str, ...], rows: list[dict] | None,
                          with_kids: set[tuple], open_keys: set[tuple] | None,
                          is_last: bool) -> None:
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
        node["prefix_label"] = None
        if len(key) == 1:
            # 一级行：不画 ├/└ 层级符号（用户选定），复选框直接贴树区左缘的 CARD_PAD 标准内边距，
            # 不再留前置槽；有子文件夹时展开箭头跟在名称后（用户选定）。
            name_label = tk.Label(row, cursor="hand2" if rows else "arrow")
            name_label.pack(side="left")
            if key in with_kids:
                arrow = tk.Label(row, text=ARROW_OPEN if open_now else ARROW_CLOSED, cursor="hand2")
                arrow.pack(side="left", padx=(4, 0))
                arrow.bind("<Button-1>", lambda _e, _k=key: self._toggle_folder_open(_k))
                node["arrow"] = arrow
                self._folder_clickables.append((arrow, str(arrow.cget("foreground"))))
        else:
            # 二级及以下：连接符 + 展开箭头共一个固定宽槽，各级名称左边缘才对齐；连接符是树形
            # 层级语言，同级末个用 └、其余用 ├（不画祖先竖线，用户选定）。
            # 槽定宽但必须 fill="y"：pack_propagate(False) 连高度传播也关掉，不给高度槽会塌成 1px，
            # Windows 把子标签裁成一条横切片——├/└/▾ 全都显示成「-」（用户截图问题的根因）。
            slot = tk.Frame(row)
            slot.pack(side="left", fill="y")
            slot.pack_propagate(False)
            slot.configure(width=ARROW_SLOT_W)
            prefix = tk.Label(slot, text=TREE_LAST if is_last else TREE_BRANCH)
            prefix.pack(side="left")
            node["prefix_label"] = prefix
            if key in with_kids:
                arrow = tk.Label(slot, text=ARROW_OPEN if open_now else ARROW_CLOSED, cursor="hand2")
                arrow.pack(side="left")
                arrow.bind("<Button-1>", lambda _e, _k=key: self._toggle_folder_open(_k))
                node["arrow"] = arrow
                self._folder_clickables.append((arrow, str(arrow.cget("foreground"))))
            name_label = tk.Label(row, cursor="hand2" if rows else "arrow")
            name_label.pack(side="left")
        node["label_widget"] = name_label
        # 名称截断的 store：全文/当前文本/气泡内容（勾选行的全文带勾选符，见 _render_folder_checks）
        node["text_full"] = node["label"]
        node["text_shown"] = node["label"]
        node["tip_full"] = rows[0]["display"] if rows else "/".join(key)
        name_label.configure(text=node["label"])
        self._bind_name_tip(name_label, node)
        if rows:
            name_label.bind("<Button-1>", lambda _e, _k=key: self._on_folder_check(_k))
            self._folder_clickables.append((name_label, str(name_label.cget("foreground"))))
            for r in rows:
                self.folder_rows.append(r)
                r["check_btn"] = name_label

        node["kids"] = tk.Frame(frame)
        self._pack_kids(node)

    def _pack_kids(self, node: dict) -> None:
        """展开挂出子文件夹框、折叠收起；控件不销毁，勾选与展开状态都留在内存里。"""
        if node["open"]:
            node["kids"].pack(anchor="w", fill="x", padx=(TREE_INDENT_W, 0))
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
        if self._visible_account_active():
            return  # 可见账户备份中配置区锁定；标签只能置灰，点击要靠这里挡住
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
        if self._visible_account_active():
            return  # 可见账户备份中配置区锁定；标签只能置灰，点击要靠这里挡住
        node = self._folder_nodes[key]
        node["open"] = not node["open"]
        self._pack_kids(node)
        # 折叠是唯一的原地变矮路径：folder_inner 变矮不送达 <Configure>，滚动范围得按内容重写，
        # Tk 随重写把越界的原点钳回内容内（仍溢出钳到新底部、装得下回顶）。画布内嵌窗口的高度
        # 要等空闲布局逐轮跟上 pack_forget，当场量 bbox 还是旧高度，所以先 update_idletasks
        # 把传播排空再同步（同开始按钮恢复静止文案那处先重排的口径）。展开是增长，随后那次
        # <Configure> 也会同步一遍，两路都幂等。
        self.folder_canvas.update_idletasks()
        self._sync_folder_scroll()
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
            # 全文带勾选符（气泡与「没截断」判定都按这一份比），显示文本按树区宽截断
            node["text_full"] = f"{glyph} {node['label']}"
            self._fit_folder_names_for(node)

    def _fit_folder_names_for(self, node: dict) -> None:
        """单个节点的名称截断（_fit_folder_names 的逐节点口径，重建与勾选翻转共用）。"""
        label = node.get("label_widget")
        if label is None or "text_full" not in node:
            return
        key = node["key"]
        canvas_w = self.folder_canvas.winfo_width()
        if canvas_w < 40:
            label.configure(text=node["text_full"])
            node["text_shown"] = node["text_full"]
            return
        glyph_w = self._text_font().measure(CHECK_ON + " ")
        if len(key) == 1:
            # 一级行没有前置槽：只扣勾选符与右留白；展开箭头跟在名称后时再扣它那一截
            arrow = node.get("arrow")
            trailing_w = (arrow.winfo_reqwidth() + 4) if arrow is not None else 0
            budget = canvas_w - glyph_w - 12 - trailing_w
        else:
            budget = canvas_w - TREE_INDENT_W * (len(key) - 1) - ARROW_SLOT_W - glyph_w - 12
        self._fit_name_label(label, node, max(int(budget), 40))

    def _on_folder_inner_configure(self, _event=None) -> None:
        """树区内容重排后（首次铺满、加载完文件夹、窗口缩放）同步滚动范围与行距。"""
        self._sync_folder_scroll()

    def _sync_folder_scroll(self) -> None:
        """文件夹树的滚动范围与行距按当前内容重写。

        Note:
            增长由 folder_inner 的 <Configure> 驱动，但 Tk 不在内容变矮时送达该事件
            （实测换账户重画、折叠分支都是 0 次）；整棵树销毁后 folder_inner 的请求高度
            也不会自行重算（实测还是旧树的高度），按 bbox 量到的还是旧范围。所以变矮与
            清空的路径都显式调到这里：空树直接写零范围，非空树按 bbox("all") 量。
            scrollregion 改小时 Tk 会把越界的视图原点钳回内容内，无需另行复位。
        """
        if self.folder_rows:
            self.folder_canvas.configure(scrollregion=self.folder_canvas.bbox("all"))
        else:
            self.folder_canvas.configure(scrollregion=(0, 0, self.folder_canvas.winfo_width(), 0))
        self._sync_folder_row_increment()

    def _on_folder_canvas_configure(self, event) -> None:
        """树区宽度变化：内嵌树跟着变宽，并重算各层名称截断（变宽自动恢复全文）。"""
        self.folder_canvas.itemconfigure(self._folder_win, width=event.width)
        self._fit_folder_names()

    def _measure_folder_row_pitch(self) -> int:
        """量文件夹树的行距(px)。

        Returns:
            任取一对相邻行容器量到的行距（行高 + 上下各一份 pady，随 DPI 与字体走）；量不到时为 0。

        Note:
            行容器是 folder_inner 的顶层节点框，也是各节点 kids 框里的子节点框——先看主宿主，
            只有一个顶层节点时（都挂在那一个父节点下面）退到它的 kids 框里量。按**请求高度**量
            而不是相邻行的 winfo_rooty：请求尺寸建完控件就有，rooty 要等布局铺完——重建后早到的
            <Configure>（换账户时 yview_moveto 冲刷出来的那串）量到的是占位值（实测 4 而不是 26），
            一格的步长会一直留错。行距恒等于「首行请求高 + 2×FOLDER_ROW_PAD_Y」，与结算后的
            rooty 间距一致。折叠节点的 kids 框没上屏，里头的控件留着折叠前的位置，不能拿来量。
        """
        hosts = [self.folder_inner] + [n["kids"] for n in self._folder_nodes.values()]
        for host in hosts:
            frames = host.winfo_children()
            if len(frames) > 1 and (host is self.folder_inner or host.winfo_ismapped()):
                height = frames[0].winfo_reqheight()
                if height > 0:
                    return height + 2 * FOLDER_ROW_PAD_Y
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
        """画布尺寸变化：内嵌列表跟着变宽（行吃满宽度），并重算滚条去留与名称截断。"""
        self.account_canvas.itemconfigure(self._account_win, width=event.width)
        self._sync_account_scroll()
        self._fit_account_names()

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
        # 行尾状态图标槽（统计/备份/完成/已停止/失败，ADR 0012）：先 pack 到右边占位，空白期挂
        # 透明 blank，名称横跳与行高变化都不会发生；背景随行底色（选中蓝行上彩图标仍可辨）。
        icon = tk.Label(frame, image=self.row_icon_blank, background=ACCOUNT_ROW_BG, padx=0, pady=0,
                        cursor="hand2")
        icon.pack(side="right", padx=(0, 2))
        label = tk.Label(frame, text=account_display(account), anchor="w", cursor="hand2", padx=0,
                         background=ACCOUNT_ROW_BG, foreground=ACCOUNT_ROW_FG)
        label.pack(side="left", fill="x", expand=True, padx=(0, 4))
        self._account_rows[account_id] = {"frame": frame, "check": check, "label": label,
                                          "icon": icon, "var": var}
        # 名称截断的 store（口径同文件夹名）：超侧边栏宽就省略号，悬停看全文
        store = self._account_rows[account_id]
        store["text_full"] = store["tip_full"] = account_display(account)
        store["text_shown"] = store["text_full"]
        self._bind_name_tip(label, store)
        # 点行内任何位置（含行尾状态图标）都切换到该账户；点复选框由它自己的 command 处理
        for widget in (frame, label, icon):
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
            row["icon"].configure(background=background)
            self._apply_account_icon(account_id)

    def _apply_account_icon(self, account_id: str) -> None:
        """按账户阶段/终态把对应状态图标挂到行尾（无行控件或活动集合外的清理后静默跳过）。"""
        row = self._account_rows.get(account_id)
        if row is None:
            return
        state = self._states.get(account_id)
        phase = state["run_phase"] if state is not None else PHASE_IDLE
        result = state["result"] if state is not None else None
        if phase in (PHASE_CONNECTING, PHASE_SCANNING):
            image = self.row_scan_icon
        elif phase == PHASE_BACKING:
            image = self.row_download_icon
        elif phase == PHASE_STOPPING:
            image = self.row_stop_icon
        elif isinstance(result, tuple) and result[0] == "failed":
            image = self.row_fail_icon
        elif isinstance(result, tuple) and result[0] == "stopped":
            image = self.row_stopped_icon
        elif isinstance(result, tuple) and result[0] == "done":
            image = self.row_done_icon
        else:
            image = self.row_icon_blank
        row["icon"].configure(image=image)

    def _schedule_result_icon(self, account_id: str, result_tuple: tuple, ttl_s: float | None) -> None:
        """落终态结果与图标；给了 ttl_s 的（完成/已停止）到期自动清回空白，失败常驻到下次启动。"""
        state = self._state(account_id)
        state["result"] = result_tuple
        state["result_at"] = time.monotonic()
        self._cancel_result_icon_job(state)
        self._apply_account_icon(account_id)
        if ttl_s is not None:
            state["icon_job"] = self.root.after(int(ttl_s * 1000),
                                                lambda a=account_id, t=state["result_at"]:
                                                self._clear_result_icon(a, t))

    def _clear_result_icon(self, account_id: str, stamped_at: float | None) -> None:
        """TTL 到期：终态时刻没被新一轮结果替换才清（重启账户/新结果会换戳，旧 after 空转）。"""
        state = self._states.get(account_id)
        if state is None or state["run_phase"] != PHASE_IDLE or state["result_at"] != stamped_at:
            return
        state["result"] = None
        state["result_at"] = None
        state["icon_job"] = None
        self._apply_account_icon(account_id)

    def _cancel_result_icon_job(self, state: dict) -> None:
        job = state.get("icon_job")
        if job is not None:
            with contextlib.suppress(tk.TclError):
                self.root.after_cancel(job)
            state["icon_job"] = None

    def _on_account_row_click(self, account_id: str) -> None:
        """点行（复选框以外的地方）= 切换右侧详情，与勾选互不牵连。"""
        if account_id != self._selected_account_id:
            self._switch_account(account_id)

    def _sync_account_checks(self) -> None:
        """把行的勾选收成账户 id 集合（三个工具按钮与单个复选框共用这一个入口）。"""
        previous = set(self._checked_accounts)
        self._checked_accounts = {account_id for account_id, row in self._account_rows.items()
                                  if row["var"].get()}
        for account_id in previous - self._checked_accounts:
            # 脱钩＝撤掉备份意愿：空闲账户的终态图标/冷却立即清掉（活动账户跑完也不再挂终态图标）
            state = self._states.get(account_id)
            if state is not None and state["run_phase"] == PHASE_IDLE and state["result"] is not None:
                self._cancel_result_icon_job(state)
                state["result"] = None
                state["result_at"] = None
                self._apply_account_icon(account_id)
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
            # 行全销毁了：滚动范围与视图都得按空树收口，别把上一个账户的深度和陈旧的滚动范围留着
            self._sync_folder_scroll()
            self.folder_canvas.yview_moveto(0)
            self.folder_ph.configure(text=FOLDER_PLACEHOLDER_TEXT)
            self._toggle_hint(self.folder_ph, True)
            self._render_messages(None)
            self.server_total = self.pending_total = self.backed_count = 0
            self.progress.configure(maximum=1, value=0)
            self._set_counts("尚未开始备份")
            self._set_status("", error=False)
            self._set_config_enabled(False)
            self.mbox_var.set(False)
            self.eml_var.set(True)
            self._update_start_state()
            self._sync_folder_loading_view()
            return
        self._render_folder_tree(state)
        self._render_messages(state)
        self._restore_run_state(state)
        # 格式复选框按账户还原（command 只在用户点击时触发，这里 set 不会写回账户）
        self.mbox_var.set(bool(account.get("mbox", False)))
        self.eml_var.set(bool(account.get("eml", True)))
        self._set_config_enabled(not self._visible_account_active())
        self._update_start_state()
        self._sync_folder_loading_view()   # 切账户换视图：目标账户在拉清单就盖动画层（Q8）
        # 别的账户还在跑时帧链仍在：切账户当帧重画，转圈立刻落到新选中账户对应的按钮上
        if self.running:
            self._stop_run_spinner()
            self._run_spinner_tick(0)

    def _render_card(self, account: dict | None) -> None:
        """只读信息卡：账户缺失时全部显示占位符；密码不在卡片上（只在设置页账户表单维护）。"""
        if account is None:
            values = {"user": CARD_EMPTY_TEXT, "server": CARD_EMPTY_TEXT, "ssl": False,
                      "port": CARD_EMPTY_TEXT}
        else:
            values = {"user": account.get("user") or CARD_EMPTY_TEXT,
                      "server": account.get("server") or CARD_EMPTY_TEXT,
                      "ssl": bool(account.get("ssl", DEFAULT_ACCOUNT_SSL)),
                      "port": str(account.get("port") or CARD_EMPTY_TEXT)}
        self._set_card_value("user", values["user"])
        self._set_card_value("server", values["server"])
        self._style_ssl_chip(self._card_labels["ssl"], values["ssl"])
        self._card_labels["port"].configure(text=values["port"])
        self._render_backup_root()

    def _set_card_value(self, key: str, text: str) -> None:
        """写入卡片某值的全文，再按当前卡宽截断（悬停看全文，见 _fit_card_values）。"""
        self._card_full[key] = text
        self._card_vars[key].set(text)

    def _render_backup_root(self) -> None:
        self._set_card_value("root", self.backup_root_var.get().strip() or DEFAULT_BACKUP_ROOT)
        self._fit_card_values()

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

    def _sync_account_edit_scroll(self) -> None:
        """设置页账户列表的智能滚条：内容装不下才把滚条挂出来，装下就收走。

        Note:
            与备份页账户列表（_sync_account_scroll）同口径：行数 × 行高比可视高。
            挂出滚条又会让列表变窄、再触发一次 <Configure>，第二次进来状态已稳定，不来回抖。
        """
        rows = int(self.account_edit_list.index("end"))
        first = self.account_edit_list.bbox(0)
        show = bool(first) and rows * first[3] > self.account_edit_list.winfo_height() + 1
        if show == self._account_edit_scroll_shown:
            return
        self._account_edit_scroll_shown = show
        if show:
            self.account_edit_scroll.grid()
        else:
            self.account_edit_scroll.grid_remove()

    def _refresh_edit_list(self, select_id: str | None) -> None:
        """重画设置页账户列表；选中项回填表单，没有账户时表单清空。"""
        self.account_edit_list.delete(0, "end")
        for account in self.accounts:
            self.account_edit_list.insert("end", account_display(account))
        self._sync_account_edit_scroll()   # 增删后行数变了，滚条去留跟着走
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
        """把账户记录填进表单；已存密码只显示固定掩码（原样保存 = 不修改，清空 = 删除）。"""
        self._acct_name_var.set(account.get("name", ""))
        self._acct_user_var.set(account.get("user", ""))
        self._acct_server_var.set(account.get("server", ""))
        self._acct_ssl_var.set(bool(account.get("ssl", DEFAULT_ACCOUNT_SSL)))
        self._style_ssl_chip(self._acct_ssl_chip, self._acct_ssl_var.get())
        self._acct_port_var.set(str(account.get("port", DEFAULT_ACCOUNT_PORT)))
        self._acct_password_var.set(PASSWORD_MASK_TEXT if account.get("password_dpapi") else "")

    def _clear_account_form(self) -> None:
        self._acct_name_var.set("")
        self._acct_user_var.set("")
        self._acct_server_var.set("")
        self._acct_ssl_var.set(DEFAULT_ACCOUNT_SSL)
        self._style_ssl_chip(self._acct_ssl_chip, self._acct_ssl_var.get())
        self._acct_port_var.set(DEFAULT_ACCOUNT_PORT)
        self._acct_password_var.set("")

    def _on_acct_password_key(self, event) -> None:
        """掩码保护（键盘）：会改文本的按键先清掉掩码，再让按键落进空框。

        Note:
            掩码是「已保存」的哨兵、不是密码内容，在它中间插字符会拼出假密码；导航键
            （方向、Home/End、Tab、Esc）与 Ctrl+A 等控制组合不改内容，直接放行。
        """
        if self._acct_password_var.get() != PASSWORD_MASK_TEXT:
            return
        if event.keysym in ("BackSpace", "Delete") or (event.char and event.char.isprintable()):
            self._acct_password_entry.delete(0, "end")

    def _on_acct_password_clip(self, _event=None) -> None:
        """掩码保护（剪贴板）：<<Paste>>/<<Cut>> 落在掩码上时先把整框清掉。

        Note:
            不拦的话，粘贴会插进掩码中间、剪切会留下半截星号，保存时都变成假密码。
        """
        if self._acct_password_var.get() == PASSWORD_MASK_TEXT:
            self._acct_password_entry.delete(0, "end")

    def _on_acct_ssl_toggle(self) -> None:
        """SSL 开关联动默认端口：只在端口仍是 993/143 这两个默认值时替换。"""
        if self._acct_port_var.get().strip() in ("993", "143"):
            self._acct_port_var.set("993" if self._acct_ssl_var.get() else "143")
        self._style_ssl_chip(self._acct_ssl_chip, self._acct_ssl_var.get())

    def _on_acct_ssl_chip_click(self, _event=None) -> None:
        """点 SSL chip = 翻转开关：语义与原 Checkbutton 的 command 完全一致，只是控件换成色块。"""
        self._acct_ssl_var.set(not self._acct_ssl_var.get())
        self._on_acct_ssl_toggle()

    def _ask_new_account(self) -> dict | None:
        """新增账户弹窗（Q11–Q13）：自绘模态表单，返回填好的账户字段；取消/叉掉返回 None。

        Note:
            展示与决策分离：测试替换本方法即可绕开真实 Toplevel（与 `_ask_close_action` 同口径）。
            与编辑表单不同：密码框没有掩码哨兵（新账户没有已存密码），空 = 不设密码。
            保存成功在保存按钮旁闪「已保存」，约 0.8 秒后自动关窗（Q13）；校验失败红字留在窗内。
        """
        dialog = tk.Toplevel(self.root)
        dialog.title(NEW_ACCOUNT_DLG_TITLE)
        dialog.transient(self.root)
        dialog.resizable(False, False)
        result: list[dict] = []
        name_var, user_var, server_var = tk.StringVar(), tk.StringVar(), tk.StringVar()
        ssl_var = tk.BooleanVar(value=DEFAULT_ACCOUNT_SSL)
        port_var = tk.StringVar(value=DEFAULT_ACCOUNT_PORT)
        password_var = tk.StringVar()
        body = tk.Frame(dialog)
        body.pack(fill="both", expand=True, padx=14, pady=12)
        form = tk.Frame(body)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        # 行序与设置页表单一致（邮箱类型置顶，SSL 与端口并进服务器行）
        tk.Label(form, text="邮箱类型：").grid(row=0, column=0, sticky="e", padx=(0, 4), pady=3)
        tk.Label(form, text=MAIL_TYPE_TEXT, anchor="w").grid(row=0, column=1, columnspan=4, sticky="w", pady=3)
        entries = [self._account_form_row(form, 1, "名称：", name_var),
                   self._account_form_row(form, 2, "邮箱地址：", user_var)]
        entries.append(self._account_form_row(form, 3, "收件服务器：", server_var))
        ssl_chip = tk.Label(form, text=SSL_CHIP_TEXT, padx=6, pady=1, cursor="hand2")
        ssl_chip.grid(row=3, column=2, sticky="w", padx=(8, 0), pady=3)

        def on_ssl_click(_event=None) -> None:
            ssl_var.set(not ssl_var.get())
            if port_var.get().strip() in ("993", "143"):   # 只联动默认端口（与编辑表单同规则）
                port_var.set("993" if ssl_var.get() else "143")
            self._style_ssl_chip(ssl_chip, ssl_var.get())

        ssl_chip.bind("<Button-1>", on_ssl_click)
        self._style_ssl_chip(ssl_chip, ssl_var.get())
        tk.Label(form, text="端口：").grid(row=3, column=3, sticky="e", padx=(4, 4), pady=3)
        port_entry = tk.Entry(form, textvariable=port_var, width=7)
        port_entry.grid(row=3, column=4, sticky="w", pady=3)
        entries.append(port_entry)
        tk.Label(form, text="密码：").grid(row=4, column=0, sticky="e", padx=(0, 4), pady=3)
        password_entry = tk.Entry(form, textvariable=password_var, show="*")
        password_entry.grid(row=4, column=1, columnspan=4, sticky="we", pady=3)
        entries.append(password_entry)

        # 按钮排（messagebox 口径）：整排居中（用户选定），状态文案贴保存按钮左侧 8px（Q13），
        # [保存/取消] 等宽（_make_equal_button_row）；与文案同排时包一层内容宽 group 整体居中
        buttons = tk.Frame(body)
        buttons.pack(fill="x", pady=(12, 0))
        center = tk.Frame(buttons)
        center.pack(anchor="center")
        status = tk.Label(center, text="")
        status.pack(side="left", padx=(0, 8))

        def do_save() -> None:
            user = user_var.get().strip()
            server = server_var.get().strip()
            if not user:
                status.configure(text=SAVE_STATUS_FAIL_PREFIX + "请填写邮箱地址。", foreground=STATUS_ERR_FG)
                return
            if not server:
                status.configure(text=SAVE_STATUS_FAIL_PREFIX + "请填写收件服务器。", foreground=STATUS_ERR_FG)
                return
            try:
                port = int(port_var.get().strip())
                if not 0 < port < 65536:
                    raise ValueError
            except ValueError:
                status.configure(text=SAVE_STATUS_FAIL_PREFIX + "端口必须是 1-65535 的整数。",
                                 foreground=STATUS_ERR_FG)
                return
            fields = {"name": name_var.get().strip(), "user": user, "server": server,
                      "ssl": bool(ssl_var.get()), "port": str(port)}
            password = password_var.get()
            cipher = _dpapi_encrypt(password) if password and sys.platform == "win32" else None
            if password and cipher is None:
                # 与编辑保存同语义：其余字段照存，只是密码没落盘
                status.configure(text="密码加密失败，本次未保存密码。", foreground=STATUS_ERR_FG)
            else:
                status.configure(text=SAVE_STATUS_OK_TEXT, foreground=STATUS_OK_FG)
            if cipher:
                fields["password_dpapi"] = cipher
            result.append(fields)
            save_btn.state(["disabled"])   # 防重复提交（ttk state 口径）
            dialog.after(NEW_ACCOUNT_DIALOG_CLOSE_DELAY_MS, dialog.destroy)

        save_btn, _cancel_btn = _make_equal_button_row(
            center, [("保存", do_save), (CANCEL_BTN_TEXT, dialog.destroy)])
        for entry in entries:
            for key in ("<Return>", "<KP_Enter>"):
                entry.bind(key, lambda _e: do_save())
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        self._center_on_root(dialog)   # 弹窗居中到主窗口（与其余自绘弹窗同一助手）
        save_btn.focus_set()           # 默认按钮 = 保存（Q6/Q9）：Enter 触发主操作
        dialog.grab_set()
        self.root.wait_window(dialog)
        return result[0] if result else None

    def _on_account_add(self) -> None:
        """新增账户（Q11）：弹窗填表单，叉掉/取消零副作用；保存成功入库并选中、静默补拉清单。"""
        fields = self._ask_new_account()
        if fields is None:
            return
        account = new_account()
        account.update(fields)
        self.accounts.append(account)
        self._refresh_edit_list(account["id"])   # 勾选默认态由 _refresh_account_list 统一给出
        self._switch_account(account["id"])      # 新账户进列表并选中（Q13）
        self._refresh_account_list()
        self._save_column_config()
        self._maybe_load_folders(account["id"])  # 加载态动画随即出现在文件夹卡
        self.log.info("新增账户 %s", account_display(account))

    def _ask_delete_account(self, account: dict) -> bool:
        """删除确认自绘弹窗（Q14）：居中到主窗口，X/Esc = 不删。

        Note:
            展示与决策分离：测试替换本方法即可绕开真实 Toplevel（与 `_ask_close_action` 同口径）。
            自绘的原因：系统 messagebox 的居中不保证落在主窗口内，本家族一律 `_center_on_root`。
        """
        dialog = tk.Toplevel(self.root)
        dialog.title(DELETE_DLG_TITLE)
        dialog.transient(self.root)
        dialog.resizable(False, False)
        answer: list[bool] = []
        body = tk.Frame(dialog)
        body.pack(fill="both", expand=True, padx=14, pady=12)
        tk.Label(body, text=DELETE_DLG_TEXT.format(name=account_display(account)),
                 justify="left", anchor="w").pack(fill="x")
        buttons = tk.Frame(body)
        buttons.pack(fill="x", pady=(12, 0))

        def choose(ok: bool) -> None:
            answer.append(ok)
            dialog.destroy()

        # 视觉序「取消 / 删除」：主操作最右（与关闭询问家族一致）；等宽、整排居中（用户选定）
        cancel_btn, _del_btn = _make_equal_button_row(
            buttons, [(CANCEL_BTN_TEXT, lambda: choose(False)), (DELETE_DLG_CONFIRM, lambda: choose(True))])
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        self._center_on_root(dialog)   # 弹窗居中到主窗口（与其余自绘弹窗同一助手）
        cancel_btn.focus_set()         # 默认按钮给安全侧（Q6）：回车不误删
        dialog.grab_set()
        self.root.wait_window(dialog)
        return answer[0] if answer else False

    def _on_account_delete(self) -> None:
        """删除当前编辑的账户；磁盘上已备份的邮件不归这里管。"""
        account = self._find_account(self._editing_account_id)
        if account is None:
            return
        if self._is_account_active(account["id"]):
            messagebox.showwarning(ACTIVE_DELETE_WARN_TITLE, ACTIVE_DELETE_WARN_TEXT, parent=self.root)
            return
        if not self._ask_delete_account(account):
            return
        state = self._states.pop(account["id"], None)
        if state is not None:
            self._cancel_result_icon_job(state)
        self._stop_events.pop(account["id"], None)
        self.accounts.remove(account)
        if self._selected_account_id == account["id"]:
            self._selected_account_id = self.accounts[0]["id"] if self.accounts else None
            self._ui_settings["selected_account"] = self._selected_account_id
            self._render_account_detail()
        self._refresh_account_list()
        self._refresh_edit_list(None)
        self._save_column_config()
        self.log.info("删除账户 %s", account_display(account))

    def _show_account_save_status(self, text: str, ok: bool) -> None:
        """保存反馈（Q5/Q9/Q10）：保存按钮旁的状态文案，约 3 秒自动消失；重复保存重置计时。"""
        self._account_save_status.configure(text=text, foreground=STATUS_OK_FG if ok else STATUS_ERR_FG)
        if self._account_save_status_job is not None:
            self.root.after_cancel(self._account_save_status_job)
        self._account_save_status_job = self.root.after(SAVE_STATUS_CLEAR_MS, self._clear_account_save_status)

    def _clear_account_save_status(self) -> None:
        self._account_save_status_job = None
        self._account_save_status.configure(text="")

    def _on_account_save(self) -> None:
        """校验并保存当前编辑的账户；密码框原样（掩码）= 不修改，清空 = 删除已存密码。

        Note:
            校验与保存结果一律用保存按钮旁的状态文案反馈（Q10 收口，不再弹 messagebox）；
            只陈述保存本身，保存后自动重拉的加载态是另一套反馈（Q6）。
        """
        account = self._find_account(self._editing_account_id)
        if account is None:
            return
        user = self._acct_user_var.get().strip()
        server = self._acct_server_var.get().strip()
        if not user:
            self._show_account_save_status(SAVE_STATUS_FAIL_PREFIX + "请填写邮箱地址。", ok=False)
            return
        if not server:
            self._show_account_save_status(SAVE_STATUS_FAIL_PREFIX + "请填写收件服务器。", ok=False)
            return
        try:
            port = int(self._acct_port_var.get().strip())
            if not 0 < port < 65536:
                raise ValueError
        except ValueError:
            self._show_account_save_status(SAVE_STATUS_FAIL_PREFIX + "端口必须是 1-65535 的整数。", ok=False)
            return
        password = self._acct_password_var.get()
        if password == PASSWORD_MASK_TEXT:
            pass                                  # 掩码原样：未改动，保留已存密文
        elif password:
            cipher = _dpapi_encrypt(password) if sys.platform == "win32" else None
            if cipher is None:
                # 半保存语义不变：其余字段照存，只是密码没落盘——红字说清楚是哪一半
                self._show_account_save_status("密码加密失败，本次未保存密码。", ok=False)
            else:
                account["password_dpapi"] = cipher
        else:
            account.pop("password_dpapi", None)   # 删空 = 删除已存密码
        account["name"] = self._acct_name_var.get().strip()
        account["user"] = user
        account["server"] = server
        account["ssl"] = bool(self._acct_ssl_var.get())
        account["port"] = str(port)
        self._refresh_account_list()
        self._refresh_edit_list(account["id"])
        if account["id"] == self._selected_account_id:
            self._render_card(account)
        self._save_column_config()
        self._show_account_save_status(SAVE_STATUS_OK_TEXT, ok=True)   # 只报保存本身（Q6）
        self._maybe_load_folders(account["id"])   # 保存后静默补拉该账户的文件夹清单
        self.log.info("保存账户 %s（%s）", account_display(account), user)

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
            这个函数、由它自己完成滚动。内容装得下（scrollregion 不高于可视区）时滚轮空转：
            那种状态下 Tk 不把视图原点钳在 0，硬滚会把内容推出负坐标（账户列表复现：行被推
            下去、上方露一片对话框灰，而 yview 始终报 (0.0, 1.0)，看不出这里不该滚）。
        """
        canvas = self._wheel_canvas(event.widget)
        if canvas is None:
            return None  # 邮件列表 / 运行日志 / 设置页的滚轮节奏一字不动
        if not self._canvas_overflows(canvas):
            return "break"  # 装得下就没有滚动这回事：不攒余量、也不滚
        self._wheel_accum += event.delta
        notches = int(self._wheel_accum / WHEEL_DELTA)  # 向零取整：正反两个方向都不吃掉余量
        if notches:
            self._wheel_accum -= notches * WHEEL_DELTA
            canvas.yview_scroll(-notches * FOLDER_WHEEL_ROWS, "units")
        return "break"  # 不满一格也到此为止，别漏给面板内滚条的原生步长

    def _canvas_overflows(self, canvas: tk.Canvas) -> bool:
        """画布内容是否高出可视区：scrollregion 比视口高才谈得上滚动。

        Args:
            canvas: 账户列表或邮箱文件夹的画布。

        Returns:
            内容高出可视区（此时 yview_scroll 会被 Tk 正常钳在两端）时为 True。

        Note:
            按 scrollregion 判而不是 bbox("all")：树缩小后 Tk 不送达 folder_inner 的
            <Configure>，两者会不一致（实测 bbox 78 而 scrollregion 还是旧树的 1170），
            而 scrollregion 才是 yview_scroll 真正参照的那一份——按它判，不会把用户困在
            一个已经漂移的视图里连滚回去的余地都没有。
        """
        return self._canvas_region_height(canvas) > canvas.winfo_height()

    def _canvas_region_height(self, canvas: tk.Canvas) -> float:
        """画布 scrollregion 的内容高度(px)；没设过区域（或写坏了）时为 0。"""
        region = canvas.cget("scrollregion")
        parts = region.split() if region else []
        if len(parts) != 4:
            return 0.0
        return float(parts[3]) - float(parts[1])

    def _folder_scroll_command(self, *args) -> None:
        """文件夹滚条的命令入口（-command）：内容装得下时箭头/滑轨/拖动全部空转。

        Args:
            *args: TScrollbar 传来的指令：scroll N units / moveto fraction。

        Note:
            邮箱文件夹的滚条常驻（不按内容显隐），树装得下时 Tk 不把画布原点钳在 0——
            箭头与拖动走的 canvas.yview 命令和滚轮是同一条会漂移的路径，这里同一口径挡掉。
            账户列表的滚条装得下时已 grid_remove，事件到不了，无需包这一层。
        """
        if not self._canvas_overflows(self.folder_canvas):
            return
        self.folder_canvas.yview(*args)

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
                "backup_root": backup_root, "selected_account": selected,
                BACKUP_SIDEBAR_WIDTH_KEY: parse_sidebar_width(raw.get(BACKUP_SIDEBAR_WIDTH_KEY)),
                SETTINGS_SIDEBAR_WIDTH_KEY: parse_sidebar_width(raw.get(SETTINGS_SIDEBAR_WIDTH_KEY)),
                ACCOUNT_LIST_WIDTH_KEY: parse_sidebar_width(raw.get(ACCOUNT_LIST_WIDTH_KEY))}

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
        self._ui_settings[BACKUP_SIDEBAR_WIDTH_KEY] = self._backup_sidebar_width
        self._ui_settings[SETTINGS_SIDEBAR_WIDTH_KEY] = self._settings_sidebar_width
        self._ui_settings[ACCOUNT_LIST_WIDTH_KEY] = self._account_list_width
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
        """右下开始/停止按钮只描述**当前选中账户**（ADR 0012：各账户独立运行/停止）。"""
        state = self._visible_state()
        phase = state["run_phase"] if state is not None else PHASE_IDLE
        if phase in (PHASE_CONNECTING, PHASE_SCANNING, PHASE_BACKING):
            # 活动中：开始位禁用、绿三角换透明占位（转圈文案由帧链写），停止位可用、红方块
            self.start_btn.configure(state="disabled", image=self.blank_icon)
            self.stop_btn.configure(state="normal", image=self.stop_icon)
        elif phase == PHASE_STOPPING:
            # 该账户停止中：开始位静止禁用，停止位禁用并由帧链写「正在停止…」
            self.start_btn.configure(state="disabled")
            self._restore_start_button()
            self.stop_btn.configure(state="disabled", image=self.blank_icon)
        else:
            ok = (state is not None
                  and any(r["var"].get() for r in self.folder_rows)
                  and (self.mbox_var.get() or self.eml_var.get()))
            self.start_btn.configure(state="normal" if ok else "disabled")
            self._restore_start_button()   # 图标随可用性换彩色/灰度
            self.stop_btn.configure(state="disabled")
            self._restore_stop_button()
        # 按钮文案/图标（如停止后恢复「开始备份」）改变了请求宽，立即重排按钮行，
        # 否则要等下次窗口 <Configure> 才按新宽度布局，期间文本被裁（问题：停止后显示不全）
        if self._bar.winfo_ismapped():
            self._bar.update_idletasks()

    def _set_loading_frame(self, button: ttk.Button, label: str, index: int) -> None:
        """把按钮画成加载态的第 index 帧（开始/停止/停止所有的转圈共用这一个入口）。

        Args:
            button: 要画帧的按钮。
            label: 加载态文案，帧字符作前缀。
            index: 帧序号，按 SPINNER_FRAMES 长度取模循环。

        Note:
            盲文点阵当前缀，节奏同为 SPINNER_INTERVAL_MS。
        """
        button.configure(text=f"{SPINNER_FRAMES[index % len(SPINNER_FRAMES)]} {label}")

    def _start_run_spinner(self) -> None:
        """启动（或重启）唯一一条运行 spinner 帧链；防重复进入：旧 job 不取消会留下第二条链。"""
        self._stop_run_spinner()
        self._run_spinner_tick(0)

    def _run_spinner_tick(self, index: int) -> None:
        """画第 index 帧：同时画「停止所有」按钮（停止所有进行中）与选中账户的开始/停止按钮。

        Note:
            帧链只有一条（job 槽 self._run_spinner_job），但目标可以有两颗：左栏的「停止所有」
            与右下属于选中账户的按钮。选中账户空闲（别的账户在跑）时两颗右下按钮保持静止。
            running 变假（活动集合排空）时帧链在这里自愈退出并复位右下按钮。
        """
        if not self.running:
            self._run_spinner_job = None
            self._update_start_state()
            return
        if self._stopping_all:
            self._set_loading_frame(self.stop_all_btn, STOPPING_BTN_LABEL, index)
        state = self._visible_state()
        phase = state["run_phase"] if state is not None else PHASE_IDLE
        if phase == PHASE_STOPPING:
            self._set_loading_frame(self.stop_btn, STOPPING_BTN_LABEL, index)
        elif phase in (PHASE_CONNECTING, PHASE_SCANNING):
            self._set_loading_frame(self.start_btn, SCANNING_BTN_LABEL, index)
        elif phase == PHASE_BACKING:
            self._set_loading_frame(self.start_btn, RUNNING_BTN_LABEL, index)
        self._run_spinner_job = self.root.after(SPINNER_INTERVAL_MS, self._run_spinner_tick, index + 1)

    def _stop_run_spinner(self) -> None:
        if self._run_spinner_job is not None:
            self.root.after_cancel(self._run_spinner_job)
            self._run_spinner_job = None

    def _restore_start_button(self) -> None:
        """回到静止态：文案与图标一并回位，图标按可用性取彩色/灰度（完成/停止/失败与帧链自愈收口）。"""
        self.start_btn.configure(
            text=START_BTN_LABEL,
            image=self.start_icon if widget_state(self.start_btn) != "disabled" else self.start_icon_off)

    def _restore_stop_button(self) -> None:
        """回到静止态：文案与图标一并回位（与 _restore_start_button 成对、同两个收口点）。

        Note:
            静止的停止按钮永远是禁用态，图标取灰度版（Q4）。
        """
        self.stop_btn.configure(
            text=STOP_BTN_LABEL,
            image=self.stop_icon if widget_state(self.stop_btn) != "disabled" else self.stop_icon_off)

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

    def _on_start(self) -> None:
        """备份当前账户（右下入口）：校验后走连接预检。显式单账户命令绕过完成冷却（ADR 0012）。"""
        if self._visible_account_active() or self._preflight_active():
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
        self._begin_preflight([account["id"]])

    def _in_done_cooldown(self, account: dict) -> bool:
        """该账户是否处于完成冷却窗（绿勾显示期）：批量入口此时跳过它，单账户入口可绕过。"""
        state = self._states.get(account["id"])
        if state is None or state.get("result_at") is None:
            return False
        result = state["result"]
        if not (isinstance(result, tuple) and result[0] == "done"):
            return False
        return time.monotonic() - state["result_at"] < DONE_COOLDOWN_S

    def _on_start_selected(self) -> None:
        """「备份所选」：跑勾着、空闲且不在完成冷却窗内的账户；活动账户照跑不受影响（ADR 0012）。"""
        if self._preflight_active():
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
        cooling: list[str] = []
        for account in chosen:
            if self._is_account_active(account["id"]):
                continue   # 已在备份：不受影响、不重入
            if self._account_credentials(account, warn=False) is None:
                skipped.append(account_display(account))
            elif self._in_done_cooldown(account):
                cooling.append(account_display(account))
            else:
                ready.append(account)
        if skipped:
            messagebox.showwarning("部分账户将跳过",
                                   "以下账户信息不完整（缺邮箱地址、收件服务器或密码），本次不备份：\n"
                                   + "\n".join(skipped), parent=self.root)
        if not ready:
            if cooling and not skipped:
                messagebox.showwarning(
                    COOLING_WARN_TITLE,
                    COOLING_WARN_TEXT.format(cooldown=DONE_COOLDOWN_S, names="\n".join(cooling)),
                    parent=self.root)
            elif not skipped:
                # 勾着的账户都已在备份中：不弹打断框，按钮反馈在行图标与进度上
                self.log.info("勾选的账户都已在备份中，无需重复启动")
            return
        self._sync_visible_folder_choices()
        self._begin_preflight([account["id"] for account in ready])

    # ---------------------------------------------------------------- 弹窗

    def _center_on_root(self, win: tk.Toplevel) -> None:
        """把自绘弹窗摆到主窗口正中（用户选定：所有弹窗都显示在应用窗口正中间）。

        Note:
            `wm geometry` 的 +x+y 落在**外框**，`winfo_rootx` 量的是**客户区**，中间差着一圈装饰
            （真机实测 8px 边框 + 31px 标题栏）——直接套「root 中心 - 弹窗一半」的公式会整体偏右下
            一截。这里先 withdraw 着摆一次 +0+0 量出装饰偏移，再按客户区中心反推外框坐标，
            实测落点误差 0px；弹窗全程未显示过，不会闪。调用时机在弹窗内容建完之后、grab/wait 之前。
            系统 messagebox/filedialog 走原生居中、不保证落在主窗口内，本助手只管自绘弹窗
            （删除确认已并入自绘家族）；悬停气泡跟随鼠标，也不走这里。
        """
        win.withdraw()
        win.update_idletasks()
        win.geometry("+0+0")
        win.update_idletasks()
        deco_x, deco_y = win.winfo_rootx(), win.winfo_rooty()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - win.winfo_reqwidth()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - win.winfo_reqheight()) // 2
        win.geometry(f"+{max(x - deco_x, 0)}+{max(y - deco_y, 0)}")
        win.deiconify()

    # ------------------------------------------------------------ 连接预检

    def _preflight_active(self) -> bool:
        return self._preflight is not None

    def _begin_preflight(self, account_ids: list[str]) -> None:
        """打开连接预检弹窗，串行检测各账户连接；全部通过后自动关闭并并发同步。

        Note:
            关闭弹窗（含「终止备份」按钮）= 终止本次备份，已检测/未检测账户全部取消。
            密码错误（认证失败）时弹密码补录窗：重试用新密码重新检测该账户，跳过则略过该账户。
        """
        snapshots = self._account_snapshots(account_ids)
        if not snapshots:
            return
        rows: dict[str, tk.Label] = {}
        win = tk.Toplevel(self.root)
        win.title(PREFLIGHT_TITLE)
        win.transient(self.root)
        win.resizable(False, False)
        body = tk.Frame(win)
        body.pack(fill="both", expand=True, padx=12, pady=12)
        tk.Label(body, text=PREFLIGHT_INTRO, justify="left", anchor="w",
                 wraplength=420).pack(fill="x")
        list_frame = tk.Frame(body)
        list_frame.pack(fill="both", expand=True, pady=(10, 0))
        for snapshot in snapshots:
            line = tk.Frame(list_frame)
            line.pack(fill="x", pady=2)
            tk.Label(line, text=account_display(snapshot), anchor="w", width=20).pack(side="left")
            status = tk.Label(line, text=PREFLIGHT_ROW_WAIT, anchor="w", foreground=EMPTY_HINT_FG)
            status.pack(side="left", fill="x", expand=True)
            rows[snapshot["id"]] = status
        ttk.Button(body, text=PREFLIGHT_CANCEL_BTN,
                   command=self._cancel_preflight).pack(pady=(12, 0))
        self._center_on_root(win)   # 弹窗居中到主窗口（内容建完后量真实尺寸）
        win.protocol("WM_DELETE_WINDOW", self._cancel_preflight)
        self._preflight = {"win": win, "rows": rows, "snapshots": snapshots,
                           "index": -1, "results": {}, "cancelled": False}
        self._preflight_next()

    def _preflight_next(self) -> None:
        """推进预检：检测下一个账户；跑完全部则收口（并发同步或全部失败提示）。"""
        pf = self._preflight
        if pf is None or pf["cancelled"]:
            return
        pf["index"] += 1
        if pf["index"] >= len(pf["snapshots"]):
            self._finish_preflight()
            return
        snapshot = pf["snapshots"][pf["index"]]
        account_id = snapshot["id"]
        self._set_preflight_row(account_id, PREFLIGHT_ROW_CHECKING)
        v = {"server": snapshot.get("server", ""), "user": snapshot.get("user", ""),
             "password": snapshot.get("password", ""),
             "ssl": bool(snapshot.get("ssl", DEFAULT_ACCOUNT_SSL)),
             "port": int(str(snapshot.get("port", DEFAULT_ACCOUNT_PORT)).strip())}
        threading.Thread(target=self._preflight_worker, args=(account_id, v), daemon=True).start()

    def _preflight_worker(self, account_id: str, v: dict) -> None:
        """预检线程：登录 + LIST + 登出，结果经事件队列回主线程。"""
        log = logging.getLogger("imapbackup")
        cfg = self._core_config(v)
        try:
            server = core.connect_and_login(cfg, log)
            try:
                core.get_names(server, False, True, quiet=False, log=log)
            finally:
                with contextlib.suppress(Exception):
                    server.logout()
            self.events.put(("preflight_result", (account_id, True, "")))
        except SystemExit as e:
            self.events.put(("preflight_result", (account_id, False, str(e))))
        except Exception as e:
            self.events.put(("preflight_result", (account_id, False, f"{type(e).__name__}: {e}")))

    def _on_preflight_result(self, account_id: str, ok: bool, message: str) -> None:
        """预检单账户结果：成功→下一个；认证失败→密码补录窗；其余失败→标失败继续。"""
        pf = self._preflight
        if pf is None or pf["cancelled"]:
            return
        if ok:
            pf["results"][account_id] = True
            self._set_preflight_row(account_id, PREFLIGHT_ROW_OK)
            self._preflight_next()
            return
        if _is_auth_failure(message):
            answer = self._ask_password(account_id)
            if answer is None:            # 关窗/跳过：略过该账户，继续检测
                self._set_preflight_row(account_id, PREFLIGHT_ROW_SKIPPED)
                self._preflight_next()
                return
            password, save = answer
            self._run_passwords[account_id] = password
            snapshot = pf["snapshots"][pf["index"]]
            snapshot["password"] = password
            if save:
                account = self._find_account(account_id)
                cipher = _dpapi_encrypt(password) if sys.platform == "win32" else None
                if account is not None and cipher:
                    account["password_dpapi"] = cipher
                    self._save_column_config()
            pf["index"] -= 1          # 回退一格：_preflight_next 会重新检测同一账户
            self._preflight_next()
            return
        pf["results"][account_id] = False
        self._set_preflight_row(account_id, f"✗ {message}")
        self._preflight_next()

    def _set_preflight_row(self, account_id: str, text: str) -> None:
        pf = self._preflight
        if pf is not None and account_id in pf["rows"]:
            pf["rows"][account_id].configure(text=text)

    def _cancel_preflight(self) -> None:
        """关闭预检弹窗 = 终止本次备份（已检测/未检测账户全部取消）。"""
        pf = self._preflight
        if pf is None:
            return
        pf["cancelled"] = True
        self._close_preflight_win(pf)

    def _finish_preflight(self) -> None:
        """全部检测完毕：收口弹窗，通过检测的账户并发同步（全失败则只提示）。"""
        pf = self._preflight
        if pf is None:
            return
        ready = [s["id"] for s in pf["snapshots"] if pf["results"].get(s["id"])]
        self._close_preflight_win(pf)
        self._preflight = None
        if not ready:
            messagebox.showwarning("没有可备份的账户", "所有账户都未通过连接检测，本次未备份。",
                                   parent=self.root)
            return
        self._launch_accounts(ready)
        self.notebook.select(0)   # 开跑即切到邮件列表页（与旧批量入口一致）

    def _close_preflight_win(self, pf: dict) -> None:
        win = pf.get("win")
        self._preflight = None
        if win is not None:
            with contextlib.suppress(tk.TclError):
                win.destroy()

    def _ask_password(self, account_id: str) -> tuple[str, bool] | None:
        """密码补录窗（模态）：返回 (密码, 是否保存)；跳过/关闭返回 None。"""
        account = self._find_account(account_id)
        name = account_display(account) if account else account_id
        result: dict[str, tuple[str, bool] | None] = {"value": None}
        win = tk.Toplevel(self.root)
        win.title(PASSWORD_PROMPT_TITLE)
        win.transient(self.root)
        win.resizable(False, False)
        body = tk.Frame(win)
        body.pack(fill="both", expand=True, padx=12, pady=12)
        tk.Label(body, text=PASSWORD_PROMPT_TEXT.format(name=name), justify="left",
                 anchor="w", wraplength=360).pack(fill="x")
        row = tk.Frame(body)
        row.pack(fill="x", pady=(10, 0))
        tk.Label(row, text="密码：").pack(side="left")
        password_var = tk.StringVar()
        entry = tk.Entry(row, textvariable=password_var, show="*", width=28)
        entry.pack(side="left", fill="x", expand=True)
        save_var = tk.BooleanVar(value=True)
        tk.Checkbutton(body, text=PASSWORD_SAVE_CHECK, variable=save_var,
                       anchor="w").pack(fill="x", pady=(6, 0))
        buttons = tk.Frame(body)
        buttons.pack(fill="x", pady=(12, 0))

        def retry() -> None:
            if password_var.get():
                result["value"] = (password_var.get(), bool(save_var.get()))
                win.destroy()

        def skip() -> None:
            result["value"] = None
            win.destroy()

        # [重试/跳过] 等宽、整排居中（用户选定）；初始焦点照旧给密码输入框
        _make_equal_button_row(buttons, [(PASSWORD_RETRY_BTN, retry), (PASSWORD_SKIP_BTN, skip)])
        self._center_on_root(win)   # 弹窗居中到主窗口（内容建完后量真实尺寸）
        win.protocol("WM_DELETE_WINDOW", skip)
        entry.focus_set()
        win.grab_set()
        win.wait_window()
        return result["value"]

    def _backup_root_ready(self) -> bool:
        if self.backup_root_var.get().strip():
            return True
        messagebox.showwarning("信息不完整", "请先在设置页的「备份」里选择备份保存位置。", parent=self.root)
        return False

    def _launch_accounts(self, account_ids: list[str]) -> None:
        """预检通过后启动这些账户的备份（账户各自独立，可在已有活动账户时追加，ADR 0012）。

        每账户复位自己的重跑基线、建独立停止信号并各起一条下载线程；活动集合从空变非空时
        开一个新周期（汇总记账），随后追加的账户并入当前周期。
        """
        account_ids = [aid for aid in account_ids if not self._is_account_active(aid)]
        if not account_ids:
            return
        outdir = self.backup_root_var.get().strip()
        snapshots = self._account_snapshots(account_ids)
        if not self._has_active_accounts():
            self._cycle = {"t0": time.monotonic(), "backed": 0, "skipped": 0,
                           "failures": [], "stopped": False, "ids": set()}
            self.log.info("开始备份运行：输出 %s", outdir)
        for account_id in account_ids:
            state = self._state(account_id)
            state["messages"] = []
            state["row_index"] = 0
            state["server_total"] = state["pending_total"] = state["backed"] = 0
            state["progress"] = None
            state["result"] = None
            state["result_at"] = None
            state["status_error"] = False
            state["status_text"] = CONNECTING_STATUS_TEXT
            state["count_text"] = "正在统计…"
            state["run_phase"] = PHASE_CONNECTING
            self._cancel_result_icon_job(state)
            self._apply_account_icon(account_id)
        self._cycle["ids"].update(account_ids)
        self._ensure_status_rows()
        visible = self._visible_state()
        if visible is not None and visible["run_phase"] in ACTIVE_PHASES:
            self._render_messages(visible)
            self._restore_run_state(visible)
        log = logging.getLogger("imapbackup")
        for snapshot in snapshots:
            account_id = snapshot["id"]
            stop_event = threading.Event()
            self._stop_events[account_id] = stop_event
            self.events.put(("account_begin", account_id))
            thread = threading.Thread(target=self._backup_account_thread,
                                      args=(snapshot, outdir, self.events, log, stop_event), daemon=True)
            thread.start()
        self._on_active_set_changed()

    def _account_snapshots(self, account_ids: list[str]) -> list[dict]:
        """开跑前把参与账户复制一份（含解出的密码）：运行中改设置不影响本次运行。

        Note:
            密码优先取本次运行的临时值（预检补录但未保存时只在本次生效，见 _run_passwords）。
        """
        snapshots: list[dict] = []
        for account_id in account_ids:
            account = self._find_account(account_id)
            if account is None:
                continue
            data = dict(account)
            data["folders"] = list(account.get("folders", []))
            data["password"] = self._run_passwords.get(account_id) or self._account_password(account)
            snapshots.append(data)
        return snapshots

    def _backup_account_thread(self, account: dict, outdir: str, eq: queue.Queue, log: logging.Logger,
                               stop_event: threading.Event) -> None:
        """单账户下载线程：跑 _backup_account，异常统一收口为 account_failed 事件（ADR 0012）。"""
        name = account_display(account)
        account_log = _account_logger(account["id"], name)
        try:
            self._backup_account(account, outdir, eq, account_log, stop_event)
        except SystemExit as e:
            eq.put(("account_failed", account["id"], str(e)))
        except Exception as e:
            account_log.exception("账户备份失败：%s", name)
            eq.put(("account_failed", account["id"], f"{type(e).__name__}: {e}"))

    def _backup_account(self, account: dict, outdir: str, eq: queue.Queue, log: logging.Logger,
                        stop_event: threading.Event) -> None:
        """单个账户的备份：先统计待备份总量、再逐文件夹下载，进度经事件队列回主线程。

        Note:
            每次运行都自己 LIST 一次文件夹（不依赖界面上那份清单）：从没连接过的账户在批量里
            也要能直接跑，且以服务器此刻的文件夹为准。账户目录名取邮箱地址（ADR 0002），
            账户名称只是显示名、不参与路径。停止只看本账户的 stop_event（ADR 0012：停一个
            账户不影响其余）。
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
                if stop_event.is_set():
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
            if pending_total > 0 and not stop_event.is_set():
                if basedir is not None:
                    core.ensure_basedir(basedir)
                    core.create_folder_structure([p[0] for p in plans], basedir, account_dir)
                for n, new, local_mbox, local_eml in plans:
                    if stop_event.is_set():
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
                                           report=report, should_stop=stop_event.is_set)
        finally:
            with contextlib.suppress(Exception):
                server.logout()
        seconds = time.monotonic() - t0
        eq.put(("account_done", account_id, backed, server_total - pending_total, seconds,
                stop_event.is_set(), pending_total))

    def _is_account_active(self, account_id: str | None) -> bool:
        """该账户是否处于备份活动态（连接/统计/下载/停止中）。"""
        if account_id is None:
            return False
        state = self._states.get(account_id)
        return state is not None and state["run_phase"] in ACTIVE_PHASES

    def _visible_account_active(self) -> bool:
        return self._is_account_active(self._selected_account_id)

    def _active_account_ids(self) -> list[str]:
        return [account["id"] for account in self.accounts if self._is_account_active(account["id"])]

    def _has_active_accounts(self) -> bool:
        return bool(self._active_account_ids())

    def _on_stop(self) -> None:
        """停止当前选中账户（右下停止钮，ADR 0012）：只给这一个账户发信号，其余账户照常备份。"""
        account_id = self._selected_account_id
        if account_id is None:
            return
        state = self._states.get(account_id)
        if state is None or state["run_phase"] not in ACTIVE_PHASES:
            return
        self._stop_events.setdefault(account_id, threading.Event()).set()
        state["run_phase"] = PHASE_STOPPING
        self.stop_btn.configure(state="disabled", image=self.blank_icon)
        self._restore_start_button()
        self._stop_run_spinner()  # 取消挂起的那一帧，立即按新目标重画第 0 帧：点下去当刻就见反馈
        self._run_spinner_tick(0)
        self._set_status("", error=False)
        self._apply_account_icon(account_id)
        if self._bar.winfo_ismapped():
            self._bar.update_idletasks()   # 开始按钮恢复静止后请求宽变化，立即重排避免文本被裁

    def _on_stop_all(self) -> None:
        """「停止所有」：给每个活动账户发各自的停止信号，按钮自身接手转圈，全部收尾后消失。"""
        active = self._active_account_ids()
        if not active:
            return
        for account_id in active:
            self._stop_events.setdefault(account_id, threading.Event()).set()
            state = self._states[account_id]
            if state["run_phase"] != PHASE_STOPPING:
                state["run_phase"] = PHASE_STOPPING
                self._apply_account_icon(account_id)
        self._stopping_all = True
        self.stop_all_btn.configure(state="disabled", image=self.blank_icon)
        self._update_start_state()                 # 可见账户若在活动，右下停止钮同步进停止态
        self._stop_run_spinner()
        self._run_spinner_tick(0)
        if self._visible_account_active():
            self._set_status("", error=False)

    def _on_active_set_changed(self) -> None:
        """活动集合（哪些账户在备份）变化的唯一收口：按钮、spinner、配置锁、托盘统一在这里刷。"""
        active = self._active_account_ids()
        self.running = bool(active)
        # 「停止所有」：有活动才出现；排空时恢复静止外观并隐藏（停止所有转圈也到此结束）
        if active:
            if not self.stop_all_btn.winfo_ismapped():
                # 上 4px 与「备份所选」分隔；下边距由容器统一提供（4px），避免叠成 8px
                self.stop_all_btn.pack(side="top", fill="x", padx=4, pady=(4, 0),
                                       after=self.backup_selected_btn)
            if not self._stopping_all:
                self.stop_all_btn.configure(state="normal", text=STOP_ALL_BTN_LABEL,
                                            image=self.stop_icon)
            self._start_run_spinner()
        else:
            self._stopping_all = False
            self.stop_all_btn.configure(state="normal", text=STOP_ALL_BTN_LABEL, image=self.stop_icon)
            self.stop_all_btn.pack_forget()
            self._stop_run_spinner()
        # 「备份所选」运行中始终可点（预检弹窗的模态 grab 会自然挡住重入），只在没有账户时禁用
        self.backup_selected_btn.configure(state="normal" if self.accounts else "disabled")
        self._update_start_state()
        self._set_config_enabled(not self._visible_account_active())
        self._sync_tray()  # 图标常驻时窗口可见也挂着：菜单「停止备份」按活动集合可点

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
        elif kind == "folder_load_failed":
            self._on_folder_load_failed(*ev[1])
        elif kind == "preflight_result":
            self._on_preflight_result(*ev[1])
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
        elif kind == "tray_show":
            self._show_window()
        elif kind == "tray_stop":
            self._on_stop_all()
        elif kind == "tray_quit":
            self._on_tray_quit()

    def _on_account_begin(self, account_id: str) -> None:
        """某账户开跑：清掉它上一轮的行，状态切到「正在连接」（launch 已置 phase，这里幂等）。"""
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
        if state["run_phase"] != PHASE_STOPPING:
            state["run_phase"] = PHASE_CONNECTING
            self._apply_account_icon(account_id)
        if account_id == self._selected_account_id:
            self._render_messages(state)
            self._restore_run_state(state)

    def _on_phase(self, account_id: str, text: str) -> None:
        state = self._states.get(account_id)
        if state is None:
            return
        # 停止信号已发：worker 可能还在投文件夹间隙的 phase 事件，不允许把 stopping 降回活动态
        if state["run_phase"] == PHASE_STOPPING:
            return
        state["status_text"] = text
        state["status_error"] = False
        if text.startswith("备份"):
            state["run_phase"] = PHASE_BACKING
        else:
            state["run_phase"] = PHASE_SCANNING
        self._apply_account_icon(account_id)
        if account_id == self._selected_account_id:
            self._set_status(text, error=False)

    def _on_totals(self, account_id: str, server_total: int, pending_total: int) -> None:
        state = self._states.get(account_id)
        if state is None:
            return
        state["server_total"] = server_total
        state["pending_total"] = pending_total
        if state["run_phase"] != PHASE_STOPPING and pending_total > 0:
            state["run_phase"] = PHASE_BACKING
            self._apply_account_icon(account_id)
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
        """单个账户跑完：结果文案（停止 / 已是最新 / 完成三态）+ 行尾终态图标（带 TTL）。"""
        state = self._states.get(account_id)
        if state is None:
            return
        kind = "stopped" if stopped else "done"
        state["run_phase"] = PHASE_IDLE
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
        # 终态图标只挂给仍勾选（仍有备份意愿）的账户：脱钩即意愿移除，不挂结果也不进冷却
        if account_id in self._checked_accounts:
            ttl = STOPPED_ICON_TTL_S if stopped else DONE_COOLDOWN_S
            self._schedule_result_icon(
                account_id, (kind, backed, skipped, seconds, pending), ttl)
        else:
            state["result"] = None
            state["result_at"] = None
            self._apply_account_icon(account_id)
        self._cycle["backed"] += backed
        self._cycle["skipped"] += skipped
        self._cycle["stopped"] = self._cycle["stopped"] or stopped
        if account_id == self._selected_account_id:
            if not stopped and pending > 0:
                self.progress.configure(value=self.progress["maximum"])
                state["progress"] = (float(self.progress["maximum"]), float(self.progress["maximum"]))
            self._set_status(state["status_text"], error=False)
        self._after_account_terminal()

    def _on_account_failed(self, account_id: str, message: str) -> None:
        state = self._states.get(account_id)
        if state is None:
            return
        failed_account = self._find_account(account_id)
        name = account_display(failed_account) if failed_account is not None else account_id
        state["run_phase"] = PHASE_IDLE
        state["result"] = ("failed", message)
        state["result_at"] = time.monotonic()
        state["status_text"] = "备份失败，详见运行日志"
        state["status_error"] = True
        if account_id in self._checked_accounts:
            self._schedule_result_icon(account_id, ("failed", message), None)  # 红叉常驻到下次启动
        else:
            self._apply_account_icon(account_id)
        self._cycle["failures"].append((name, message))
        self.log.error("账户 %s 备份失败：%s", name, message)
        # 前台只在失败账户正被看着时弹一次打断框；其余账户走红叉 + 切过去看状态行
        if account_id == self._selected_account_id and not self._background_hidden():
            messagebox.showerror("备份失败", message, parent=self.root)
        if account_id == self._selected_account_id:
            self._set_status(state["status_text"], error=True)
        self._after_account_terminal()

    def _after_account_terminal(self) -> None:
        """一个账户到终态后的统一收口：刷界面；若活动集合就此排空，记汇总日志/发后台气泡。"""
        still_active = self._has_active_accounts()
        self._on_active_set_changed()
        if still_active or self._preflight_active():
            return
        elapsed = time.monotonic() - self._cycle["t0"]
        backed, skipped = self._cycle["backed"], self._cycle["skipped"]
        failures = self._cycle["failures"]
        self.log.info("备份运行结束：备份 %d 封，跳过 %d 封，用时 %s%s",
                      backed, skipped, _fmt_duration(elapsed),
                      "（用户停止）" if self._cycle["stopped"] else "")
        if self._background_hidden():
            if self._cycle["stopped"]:
                self._notify_background(TRAY_TITLE_STOPPED, TRAY_BODY_STOPPED)
            elif failures:
                self._notify_background(TRAY_TITLE_FAILED, TRAY_BODY_FAILED)
            else:
                self._notify_background(TRAY_TITLE_DONE,
                                        f"本次备份 {backed} 封，跳过 {skipped} 封，用时 {_fmt_duration(elapsed)}")

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
            new_length = 220
        elif spare >= PROGRESS_MIN_WIDTH:
            new_length = max(spare, 1)   # 缩水阶段：文字照旧全文
        else:
            new_length = PROGRESS_MIN_WIDTH  # 触底：文字开始截断
        # length 只在真正变化时配：每封邮件都重配几何会让原生进度条填充块错位（显示成满格）
        if new_length != getattr(self, "_progress_length", None):
            self.progress.configure(length=new_length)
            self._progress_length = new_length
        if spare < PROGRESS_MIN_WIDTH:
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

    # --------------------------------------------- 名称截断（文件夹名 / 账户名）

    def _bind_name_tip(self, widget: tk.Widget, store: dict) -> None:
        """名称标签的悬停气泡：只在文本被截断时弹（store 记全文、当前文本与气泡内容）。"""
        widget.bind("<Enter>", lambda e: self._on_name_tip_enter(e, store))
        widget.bind("<Leave>", lambda _e: self._on_tip_leave())

    def _on_name_tip_enter(self, event, store: dict) -> None:
        if store["text_shown"] == store["text_full"]:
            return   # 没截断不弹
        self._schedule_tip(event, store["tip_full"])

    def _bind_widget_tip(self, widget: tk.Widget, text: str) -> None:
        """无截断语义的控件气泡：悬停即弹（图标按钮没有「全文」可对比）。"""
        widget.bind("<Enter>", lambda e: self._schedule_tip(e, text))
        widget.bind("<Leave>", lambda _e: self._on_tip_leave())

    def _schedule_tip(self, event, text: str) -> None:
        x, y = event.x_root, event.y_root
        self._tip_after = self.root.after(TIP_DELAY_MS, lambda: self._show_tip(x, y, text))

    def _fit_name_label(self, label: tk.Label, store: dict, budget: int) -> None:
        """把名称标签截到 ≤ budget 像素（悬停看全文）：变宽后从全文重算，双向恢复。"""
        full = store["text_full"]
        font = self._text_font()
        text = full if font.measure(full) <= budget else _ellipsize(full, budget - 8, font)
        if text != store["text_shown"]:
            store["text_shown"] = text
            label.configure(text=text)

    def _fit_folder_names(self) -> None:
        """文件夹名按树区宽截断：各层缩进与固定槽宽都扣掉，深的行可用宽更小（画布变宽自动恢复）。"""
        for node in self._folder_nodes.values():
            self._fit_folder_names_for(node)

    def _fit_account_names(self) -> None:
        """账户名按列表宽截断：复选框、行尾状态图标槽与行内边距是固定占用，拖宽后自动恢复全文。"""
        blank = getattr(self, "row_icon_blank", None)   # 启动期画布 Configure 可能早于图标挂载
        if blank is None:
            return
        icon_slot = blank.width() + 6   # 图标边 + 图标右 2px + 名称右留白 4px
        budget = self.account_canvas.winfo_width() - 32 - icon_slot
        if budget < 20:
            return
        for row in self._account_rows.values():
            self._fit_name_label(row["label"], row, budget)

    def _refresh_counts(self) -> None:
        # 计数文案的分母（pending_total）与进度条 maximum 同源；value 钳进 [0, maximum]，
        # 多账户事件流下二者绝不脱节。镜像由可见账户的 totals/message 事件实时同步。
        maximum = max(self.pending_total, 1)
        value = max(0, min(self.backed_count, maximum))
        self._set_counts(f"服务器共 {self.server_total} ｜ 待备份 {self.pending_total} ｜ "
                         f"已备份 {self.backed_count} / {self.pending_total}")
        # range 只在真正变化时配（每封邮件都重配 maximum 会让原生进度条在几何抖动时填充错位）
        if float(self.progress.cget("maximum")) != float(maximum):
            self.progress.configure(maximum=maximum)
        self.progress.configure(value=value)
        # 同步进可见账户状态：切走再切回时进度原样还原（旧实现只在 totals 时记 0，会归零）
        state = self._visible_state()
        if state is not None:
            state["progress"] = (float(value), float(maximum))

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
        preference: str | None = None
        remember = False
        if action == "ask":
            answer = self._ask_close_action()
            if answer is None:
                return
            preference, remember = answer
            # 对话框说的是偏好口径（tray/exit，勾了记住就直接存），这里换成动作口径
            action = "hide" if preference == CLOSE_ACTION_TRAY else "exit"
        if action == "hide":
            self._hide_to_background()
            return
        # 退出：有活动账户先二次确认并优雅停止（拒绝则偏好也不写，什么都不变）
        if self._has_active_accounts():
            if not self._confirm_and_quit():
                return
            if remember and preference is not None:
                self._ui_settings[CLOSE_ACTION_KEY] = preference
                self.log.info("关闭行为偏好已记住：%s", preference)
        else:
            if remember and preference is not None:
                self._ui_settings[CLOSE_ACTION_KEY] = preference
                self.log.info("关闭行为偏好已记住：%s", preference)
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

        # 视觉序「隐藏到后台 / 退出程序」：主操作（退出）最右；等宽、整排居中（用户选定）
        hide_btn, _exit_btn = _make_equal_button_row(
            buttons, [(CLOSE_DLG_HIDE, lambda: choose(CLOSE_ACTION_TRAY)),
                      (CLOSE_DLG_EXIT, lambda: choose(CLOSE_ACTION_EXIT))])
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        self._center_on_root(dialog)   # 弹窗居中到主窗口（与其余自绘弹窗同一助手）
        hide_btn.focus_set()           # 默认按钮给安全侧（Q6）：回车=隐藏而非退出
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

    def _ask_quit_while_running(self, n: int) -> bool:
        """活动账户未收尾时的退出确认（自绘、居中）：确认后弹窗转等待态并启动优雅停止。

        Note:
            展示与决策分离：测试替换本方法即可绕开真实 Toplevel（与 _ask_close_action 同口径）。
            [取消 / 退出程序] 等宽居中，默认焦点与 Esc/X 都在安全侧（取消）。确认后弹窗不关：
            按钮置灰、文案转「正在停止…」，由优雅停止的轮询在收尾后关掉它并真退出。
        """
        if self.root.state() == "withdrawn":
            self._show_window()   # 隐藏时从托盘退出：先把窗口亮回来，用户要做决定
        dialog = tk.Toplevel(self.root)
        dialog.title(QUIT_CONFIRM_TITLE)
        dialog.transient(self.root)
        dialog.resizable(False, False)
        answer: list[bool] = []
        body = tk.Frame(dialog)
        body.pack(fill="both", expand=True, padx=14, pady=12)
        text_label = tk.Label(body, text=QUIT_CONFIRM_TEXT.format(n=n), justify="left", anchor="w")
        text_label.pack(fill="x")
        buttons = tk.Frame(body)
        buttons.pack(fill="x", pady=(12, 0))
        cancel_btn, confirm_btn = _make_equal_button_row(
            buttons, [(CANCEL_BTN_TEXT, lambda: finish(False)),
                      (QUIT_CONFIRM_BTN, lambda: finish(True))])

        def finish(ok: bool) -> None:
            answer.append(ok)
            if not ok:
                self._quit_wait_win = None
                dialog.destroy()
                return
            # 确认后弹窗转等待态：不可再取消；真正的停止/退出由调用方拿到本窗口后启动
            cancel_btn.configure(state="disabled")
            confirm_btn.configure(state="disabled")
            text_label.configure(text=QUIT_WAITING_TEXT.format(n=n))
            dialog.protocol("WM_DELETE_WINDOW", lambda: None)
            dialog.bind("<Escape>", lambda _event: None)
            self._quit_wait_win = (dialog, text_label)

        dialog.protocol("WM_DELETE_WINDOW", lambda: finish(False))
        dialog.bind("<Escape>", lambda _event: finish(False))
        self._center_on_root(dialog)
        cancel_btn.focus_set()      # 默认焦点给安全侧：回车不退出
        dialog.grab_set()
        self.root.wait_window(dialog)
        return bool(answer and answer[0])

    def _confirm_and_quit(self) -> bool:
        """活动账户在跑时的统一退出路径：自绘确认 → 优雅停止（等待态留在确认窗里）。

        Returns:
            True＝已启动优雅停止流程；False＝用户取消，什么都不变。
        """
        n = len(self._active_account_ids())
        self._quit_wait_win = None
        if not self._ask_quit_while_running(n):
            return False
        waiting = self._quit_wait_win
        dialog = waiting[0] if waiting is not None else None
        label = waiting[1] if waiting is not None else None

        def on_done() -> None:
            if dialog is not None:
                with contextlib.suppress(tk.TclError):
                    dialog.destroy()
            self._quit()

        self._begin_quit_after_stop(waiting_label=label, on_done=on_done)
        return True

    def _begin_quit_after_stop(self, waiting_label: tk.Label | None = None,
                               on_done: Callable[[], None] | None = None) -> None:
        """确认退出后：停掉全部活动账户，轮询等它们干净收尾（写盘安全），再走 on_done。"""
        self._on_stop_all()
        if waiting_label is not None:
            waiting_label.configure(text=QUIT_WAITING_TEXT.format(n=len(self._active_account_ids())))
        self._quit_deadline = time.monotonic() + QUIT_STOP_TIMEOUT_S

        def poll() -> None:
            if self._quit_after_stop_step(time.monotonic()):
                self._finish_quit_after_stop(
                    on_done, forced=self._has_active_accounts())
            else:
                if waiting_label is not None:
                    waiting_label.configure(text=QUIT_WAITING_TEXT.format(
                        n=len(self._active_account_ids())))
                self.root.after(100, poll)

        self.root.after(100, poll)

    def _quit_after_stop_step(self, now: float) -> bool:
        """优雅退出轮询的一步：活动账户排空（干净退）或超过等待上限（强退）才放行。"""
        return (not self._has_active_accounts()) or now > self._quit_deadline

    def _finish_quit_after_stop(self, on_done: Callable[[], None] | None, forced: bool) -> None:
        """收尾完成（或超时）：落盘后退出。on_done 给弹窗路径（含关窗），直接路径落盘+_quit。"""
        if forced:
            self.log.warning("等待备份账户干净收尾超过 %s 秒，强制退出", QUIT_STOP_TIMEOUT_S)
        self._save_column_config()
        if on_done is not None:
            on_done()
        else:
            self._quit()

    def _on_tray_quit(self) -> None:
        """托盘菜单「退出」：有活动账户先二次确认并优雅停止，空闲则直接退出。"""
        if self._has_active_accounts():
            self._confirm_and_quit()
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
