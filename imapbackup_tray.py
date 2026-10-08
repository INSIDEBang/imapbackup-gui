#!/usr/bin/env python3
"""IMAP Backup GUI — 托盘宿主：纯标准库 ctypes 的 Shell_NotifyIconW 封装（fork addition）

设计决策见 docs/adr/0009-background-residency-and-tray.md；关键契约：
 - 只在 Windows 生效：其它平台 create_tray() 返回 None，调用方退化（最小化到任务栏）。
 - 自建隐藏消息窗口 + 专属消息循环线程。托盘回调不挂在 Tk 窗口上：那要子类化 Tk 的窗口
   过程、依赖 Tcl 内部的消息派发，且托盘消息会与 Tk 事件挤在同一条线程里。
 - 托盘线程绝不触碰 Tk 或任何界面状态：托盘回调只把事件投进调用方的事件队列；所有操作
   由调用线程经命令队列转给托盘线程执行（Shell_NotifyIcon 的 hWnd 必须归调用线程）。
 - 隐藏消息窗口的类名是「二次启动唤起」通道的键：第二个进程 FindWindowW(类名) 后
   PostMessageW(WM_RESTORE_REQUEST)，已有实例据此把主窗口显示出来。
"""
from __future__ import annotations

import ctypes
import logging
import queue
import sys
import threading

__author__ = "Chen Chong"
__copyright__ = "(C) 2026 Chen Chong. Code under MIT License."

# ---------------------------------------------------------------- Win32 类型与常量
# 不用 ctypes.wintypes：这里显式声明每个类型，跨平台 import 安全（wintypes 在非 Windows
# 上的可导入性没有保证），且 64 位的字段宽度一目了然。
HWND = ctypes.c_void_p
UINT = ctypes.c_uint
DWORD = ctypes.c_ulong
BOOL = ctypes.c_int
WCHAR = ctypes.c_wchar
LPARAM = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
LRESULT = ctypes.c_ssize_t
HICON = ctypes.c_void_p
HMENU = ctypes.c_void_p
HINSTANCE = ctypes.c_void_p
HCURSOR = ctypes.c_void_p
HBRUSH = ctypes.c_void_p
LPCWSTR = ctypes.c_wchar_p

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_NULL = 0x0000
WM_COMMAND = 0x0111
WM_CONTEXTMENU = 0x007B
WM_LBUTTONUP = 0x0202
WM_RBUTTONUP = 0x0205
WM_USER = 0x0400
WM_APP = 0x8000

# 托盘回调消息与自定义消息：WM_APP 段归应用私有，不与系统消息冲突
WM_TRAY_CALLBACK = WM_APP + 1
WM_RESTORE_REQUEST = WM_APP + 2
WM_WAKE = WM_APP + 3

NIN_SELECT = WM_USER + 0
NIN_KEYSELECT = WM_USER + 1
NIN_BALLOONUSERCLICK = WM_USER + 5

NIM_ADD = 0x00000000
NIM_MODIFY = 0x00000001
NIM_DELETE = 0x00000002

NIF_MESSAGE = 0x00000001
NIF_ICON = 0x00000002
NIF_TIP = 0x00000004
NIF_INFO = 0x00000010

MF_STRING = 0x00000000
MF_GRAYED = 0x00000001
MF_SEPARATOR = 0x00000800

TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100

IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
IDI_APPLICATION = 32512
SM_CXSMICON = 49
SM_CYSMICON = 50

CS_DBLCLKS = 0x0008
ERROR_CLASS_ALREADY_EXISTS = 1410
TRAY_ICON_ID = 1

# 菜单项 ID：TrackPopupMenu 用 TPM_RETURNCMD 直接返回被点项的 ID
MENU_SHOW = 1
MENU_STOP = 2
MENU_QUIT = 3

# 命令超时：托盘线程卡在 TrackPopupMenu（菜单打开着）时不能把调用线程一起拖住
COMMAND_TIMEOUT_SECONDS = 3.0
STARTUP_TIMEOUT_SECONDS = 5.0

MENU_LABEL_SHOW = "显示主窗口(&O)"
MENU_LABEL_STOP = "停止备份(&S)"
MENU_LABEL_QUIT = "退出(&X)"

WINDOW_CLASS_NAME = "IMAPBackupGui.TrayHost"


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _MSG(ctypes.Structure):
    _fields_ = [("hwnd", HWND), ("message", UINT), ("wParam", WPARAM), ("lParam", LPARAM),
                ("time", DWORD), ("pt", _POINT)]


class _WNDCLASSEXW(ctypes.Structure):
    # lpfnWndProc 用 c_void_p 存回调地址：WINFUNCTYPE 只在 Windows 上存在，
    # 结构体定义要能在任何平台上 import（见模块 docstring）。
    _fields_ = [("cbSize", UINT), ("style", UINT), ("lpfnWndProc", ctypes.c_void_p),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", HINSTANCE), ("hIcon", HICON), ("hCursor", HCURSOR),
                ("hbrBackground", HBRUSH), ("lpszMenuName", LPCWSTR),
                ("lpszClassName", LPCWSTR), ("hIconSm", HICON)]


class _NOTIFYICONDATAW(ctypes.Structure):
    """NOTIFYICONDATAW（Vista 及以后）：cbSize 由 ctypes.sizeof 给，布局随平台对齐。"""
    _fields_ = [("cbSize", DWORD), ("hWnd", HWND), ("uID", UINT), ("uFlags", UINT),
                ("uCallbackMessage", UINT), ("hIcon", HICON),
                ("szTip", WCHAR * 128),
                ("dwState", DWORD), ("dwStateMask", DWORD),
                ("szInfo", WCHAR * 256), ("uTimeoutOrVersion", UINT),
                ("szInfoTitle", WCHAR * 64), ("dwInfoFlags", DWORD),
                ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", HICON)]


class _Api:
    """user32/shell32/kernel32 的载入与签名声明。只在 Windows 上构造。"""

    def __init__(self) -> None:
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        u, s, k = self.user32, self.shell32, self.kernel32

        u.RegisterClassExW.argtypes = [ctypes.POINTER(_WNDCLASSEXW)]
        u.RegisterClassExW.restype = ctypes.c_ushort
        u.CreateWindowExW.argtypes = [DWORD, LPCWSTR, LPCWSTR, DWORD, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, HWND, HMENU, HINSTANCE, ctypes.c_void_p]
        u.CreateWindowExW.restype = HWND
        u.DefWindowProcW.argtypes = [HWND, UINT, WPARAM, LPARAM]
        u.DefWindowProcW.restype = LRESULT
        u.DestroyWindow.argtypes = [HWND]
        u.DestroyWindow.restype = BOOL
        u.GetMessageW.argtypes = [ctypes.POINTER(_MSG), HWND, UINT, UINT]
        u.GetMessageW.restype = ctypes.c_int
        u.TranslateMessage.argtypes = [ctypes.POINTER(_MSG)]
        u.DispatchMessageW.argtypes = [ctypes.POINTER(_MSG)]
        u.DispatchMessageW.restype = LRESULT
        u.PostMessageW.argtypes = [HWND, UINT, WPARAM, LPARAM]
        u.PostMessageW.restype = BOOL
        u.PostQuitMessage.argtypes = [ctypes.c_int]
        u.FindWindowW.argtypes = [LPCWSTR, LPCWSTR]
        u.FindWindowW.restype = HWND
        u.RegisterWindowMessageW.argtypes = [LPCWSTR]
        u.RegisterWindowMessageW.restype = UINT
        u.LoadImageW.argtypes = [HINSTANCE, LPCWSTR, UINT, ctypes.c_int, ctypes.c_int, UINT]
        u.LoadImageW.restype = HICON
        # 资源 ID 走指针（MAKEINTRESOURCE），所以第二参声明成 c_void_p 而不是字符串
        u.LoadIconW.argtypes = [HINSTANCE, ctypes.c_void_p]
        u.LoadIconW.restype = HICON
        u.DestroyIcon.argtypes = [HICON]
        u.DestroyIcon.restype = BOOL
        u.GetSystemMetrics.argtypes = [ctypes.c_int]
        u.GetSystemMetrics.restype = ctypes.c_int
        u.SetForegroundWindow.argtypes = [HWND]
        u.SetForegroundWindow.restype = BOOL
        u.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]
        u.GetCursorPos.restype = BOOL
        u.CreatePopupMenu.restype = HMENU
        u.AppendMenuW.argtypes = [HMENU, UINT, ctypes.c_size_t, LPCWSTR]
        u.AppendMenuW.restype = BOOL
        u.TrackPopupMenu.argtypes = [HMENU, UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, HWND, ctypes.c_void_p]
        u.TrackPopupMenu.restype = ctypes.c_int
        u.DestroyMenu.argtypes = [HMENU]
        u.DestroyMenu.restype = BOOL

        s.Shell_NotifyIconW.argtypes = [DWORD, ctypes.POINTER(_NOTIFYICONDATAW)]
        s.Shell_NotifyIconW.restype = BOOL

        k.GetModuleHandleW.argtypes = [LPCWSTR]
        k.GetModuleHandleW.restype = HINSTANCE


_api: _Api | None = None
_api_lock = threading.Lock()


def _get_api() -> _Api:
    """进程内只载入一次系统库（托盘线程调用，锁防并发）。"""
    global _api
    with _api_lock:
        if _api is None:
            _api = _Api()
    return _api


class TrayHost:
    """托盘图标宿主：隐藏消息窗口 + 消息循环线程 + 图标/菜单/气泡。

    Args:
        icon_path: 托盘图标文件路径（.ico）；空或读不出来时退回系统默认图标。
        tooltip: 图标悬停提示。
        on_show: 用户要求显示主窗口（左键单击、气泡单击、二次启动唤起）。
        on_stop: 用户在托盘菜单里点了「停止备份」。
        on_quit: 用户在托盘菜单里点了「退出」。
        log: 诊断日志。

    Note:
        三个回调都在托盘线程上执行，只允许做线程安全的事（例如往事件队列里投元组）；
        所有公开方法只从调用线程（Tk 主线程）使用，内部经命令队列转给托盘线程。
    """

    def __init__(self, *, icon_path: str | None, tooltip: str, on_show, on_stop, on_quit,
                 log: logging.Logger) -> None:
        self._icon_path = icon_path
        self._tooltip = tooltip
        self._on_show = on_show
        self._on_stop = on_stop
        self._on_quit = on_quit
        self._log = log
        self._commands: queue.Queue[tuple] = queue.Queue()
        self._ready = threading.Event()
        self._hwnd: int | None = None
        self._api: _Api | None = None
        self._wndproc = None          # 必须持引用：回调被 GC 后窗口过程就是野指针
        self._icon_handle: int | None = None
        self._own_icon = False        # 图标句柄是否由 LoadImage 得到（只有它需要 DestroyIcon）
        self._icon_visible = False
        self._running = False
        self._taskbar_created = 0
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="tray-host", daemon=True)
        self._thread.start()
        self._ready.wait(STARTUP_TIMEOUT_SECONDS)

    @property
    def available(self) -> bool:
        """消息窗口建起来了没有：没有则调用方退化（存在一个可用的托盘通道才算可用）。"""
        return self._hwnd is not None

    # ------------------------------------------------------------- 调用线程接口

    def show(self, tooltip: str) -> bool:
        """确保托盘图标已挂出并刷新悬停提示，返回图标当前是否可用（幂等）。

        Note:
            图标随程序启动常驻、直到 close()；重复调用只是 NIM_MODIFY。启动时挂不出图标
            不算致命（记 warning），隐藏窗口前再调一次即可重试——失败时调用方不得隐藏窗口。
        """
        return self._post("show", tooltip)

    def set_tooltip(self, tooltip: str) -> None:
        """更新悬停提示（NIM_MODIFY）。"""
        self._post("tooltip", tooltip)

    def set_running(self, running: bool) -> None:
        """记录备份是否在跑：托盘菜单「停止备份」项的灰化依据。"""
        self._post("running", running)

    def notify(self, title: str, text: str) -> None:
        """弹一条气泡通知（NIF_INFO）；点击气泡会回调 on_show。"""
        self._post("notify", title, text)

    def close(self) -> None:
        """移除图标、销毁消息窗口并结束托盘线程（幂等）。

        Note:
            命令确认后还要等线程真的退出：进程紧接着结束（退出程序、测试收尾）时，
            守护线程若正卡在消息循环里，解释器收尾会与它抢同一批 ctypes 状态。
        """
        if self._closed:
            return
        self._closed = True
        self._post("close")
        self._thread.join(COMMAND_TIMEOUT_SECONDS)
        if self._thread.is_alive():
            self._log.warning("托盘线程未在超时内结束")

    # ------------------------------------------------------------- 命令队列

    def _post(self, name: str, *payload) -> bool:
        """把命令交给托盘线程执行并等它做完；超时/线程已退出一律返回 False。"""
        if not self._hwnd or self._api is None:
            return False
        ack = threading.Event()
        result: list[bool] = []
        self._commands.put((name, payload, ack, result))
        if not self._api.user32.PostMessageW(self._hwnd, WM_WAKE, 0, 0):
            return False
        if not ack.wait(COMMAND_TIMEOUT_SECONDS):
            self._log.warning("托盘命令 %s 超时（消息循环忙）", name)
            return False
        return result[0] if result else False

    # ------------------------------------------------------------- 托盘线程

    def _run(self) -> None:
        """托盘线程主体：建窗口、跑消息循环；任何失败都只降级不抛出。"""
        try:
            self._api = _get_api()
            if not self._create_window():
                return
            if self._closed:  # 构造侧已放弃（启动超时）：别把窗口留成孤儿
                self._api.user32.DestroyWindow(self._hwnd)
                return
            self._ready.set()
            self._message_loop()
        except OSError as exc:
            self._log.warning("托盘不可用：%s", exc)
            self._ready.set()
        except Exception:  # 托盘线程绝不允许把异常带出线程边界
            self._log.exception("托盘线程异常退出")
            self._ready.set()
        finally:
            self._destroy_icon_handle()
            self._hwnd = None

    def _create_window(self) -> bool:
        api = self._api
        assert api is not None
        WNDPROC = ctypes.WINFUNCTYPE(LRESULT, HWND, UINT, WPARAM, LPARAM)
        self._wndproc = WNDPROC(self._window_proc)
        wc = _WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(_WNDCLASSEXW)
        wc.style = CS_DBLCLKS
        wc.lpfnWndProc = ctypes.cast(self._wndproc, ctypes.c_void_p)
        wc.hInstance = api.kernel32.GetModuleHandleW(None)
        wc.lpszClassName = WINDOW_CLASS_NAME
        if (not api.user32.RegisterClassExW(ctypes.byref(wc))
                and ctypes.get_last_error() != ERROR_CLASS_ALREADY_EXISTS):
            self._log.warning("托盘不可用：注册窗口类失败（错误码 %d）", ctypes.get_last_error())
            return False
        hwnd = api.user32.CreateWindowExW(0, WINDOW_CLASS_NAME, "IMAP Backup GUI Tray",
                                          0, 0, 0, 0, 0, None, None, wc.hInstance, None)
        if not hwnd:
            self._log.warning("托盘不可用：创建消息窗口失败（错误码 %d）", ctypes.get_last_error())
            return False
        self._hwnd = hwnd
        self._taskbar_created = api.user32.RegisterWindowMessageW("TaskbarCreated")
        self._load_icon()
        return True

    def _message_loop(self) -> None:
        api = self._api
        assert api is not None
        msg = _MSG()
        while True:
            ret = api.user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:
                return
            api.user32.TranslateMessage(ctypes.byref(msg))
            api.user32.DispatchMessageW(ctypes.byref(msg))

    def _window_proc(self, hwnd, message, wparam, lparam):  # Win32 窗口过程：签名由系统固定
        """窗口过程：托盘回调、自定义消息与任务栏重建都在这里分派。"""
        api = self._api
        if api is None:
            return 0
        try:
            if message == WM_TRAY_CALLBACK:
                self._on_tray_event(int(lparam) & 0xFFFF)
            elif message == WM_RESTORE_REQUEST:
                self._fire("show")
            elif message == WM_WAKE:
                self._drain_commands()
            elif self._taskbar_created and message == self._taskbar_created:
                self._on_taskbar_created()
            elif message == WM_DESTROY:
                api.user32.PostQuitMessage(0)
        except Exception:  # 回调异常不能打断消息循环
            self._log.exception("托盘消息处理失败")
        return api.user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _on_tray_event(self, event: int) -> None:
        if event in (WM_LBUTTONUP, NIN_SELECT, NIN_BALLOONUSERCLICK):
            self._fire("show")
        elif event in (WM_RBUTTONUP, NIN_KEYSELECT, WM_CONTEXTMENU):
            self._popup_menu()

    def _on_taskbar_created(self) -> None:
        """explorer 重启后托盘区被清空：隐藏期间要重新挂出图标。"""
        if self._icon_visible:
            self._add_icon(self._tooltip)
            self._log.info("任务栏重建，托盘图标已重新挂出")

    def _drain_commands(self) -> None:
        while True:
            try:
                name, payload, ack, result = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                result.append(self._execute(name, payload))
            except Exception:  # 单条命令失败不影响后续命令
                self._log.exception("托盘命令 %s 执行失败", name)
                result.append(False)
            finally:
                ack.set()

    def _execute(self, name: str, payload: tuple) -> bool:
        if name == "show":
            return self._add_icon(payload[0])
        if name == "tooltip":
            self._tooltip = payload[0]
            return self._add_icon(payload[0]) if self._icon_visible else True
        if name == "running":
            self._running = bool(payload[0])
            return True
        if name == "notify":
            return self._balloon(payload[0], payload[1])
        if name == "close":
            self._delete_icon()
            api = self._api
            if api is not None and self._hwnd:
                api.user32.DestroyWindow(self._hwnd)
            return True
        return False

    # ------------------------------------------------------------- 图标与通知

    def _load_icon(self) -> None:
        api = self._api
        assert api is not None
        handle = None
        if self._icon_path:
            cx = api.user32.GetSystemMetrics(SM_CXSMICON)
            cy = api.user32.GetSystemMetrics(SM_CYSMICON)
            handle = api.user32.LoadImageW(None, self._icon_path, IMAGE_ICON, cx, cy, LR_LOADFROMFILE)
        if handle:
            self._own_icon = True
        else:
            handle = api.user32.LoadIconW(None, ctypes.c_void_p(IDI_APPLICATION))
        self._icon_handle = handle or None

    def _destroy_icon_handle(self) -> None:
        api = self._api
        if api is not None and self._own_icon and self._icon_handle:
            api.user32.DestroyIcon(self._icon_handle)
        self._icon_handle = None
        self._own_icon = False

    def _notify_data(self, flags: int) -> _NOTIFYICONDATAW:
        data = _NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
        data.hWnd = self._hwnd
        data.uID = TRAY_ICON_ID
        data.uFlags = flags
        data.uCallbackMessage = WM_TRAY_CALLBACK
        data.hIcon = self._icon_handle
        data.szTip = self._tooltip[:127]
        return data

    def _add_icon(self, tooltip: str) -> bool:
        """挂出或刷新图标：已挂出时走 NIM_MODIFY，否则 NIM_ADD（失败再退 NIM_MODIFY）。

        Note:
            NIM_ADD 失败退 NIM_MODIFY 是为 explorer 刚重建、图标其实还在的情形兜底。
        """
        api = self._api
        assert api is not None
        self._tooltip = tooltip
        data = self._notify_data(NIF_MESSAGE | NIF_ICON | NIF_TIP)
        if self._icon_visible:
            return bool(api.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data)))
        if (not api.shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data))
                and not api.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))):
            self._log.warning("托盘图标挂出失败（错误码 %d）", ctypes.get_last_error())
            return False
        self._icon_visible = True
        return True

    def _delete_icon(self) -> None:
        api = self._api
        if api is None or not self._icon_visible or not self._hwnd:
            self._icon_visible = False
            return
        data = self._notify_data(0)
        api.shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(data))
        self._icon_visible = False

    def _balloon(self, title: str, text: str) -> bool:
        api = self._api
        assert api is not None
        if not self._icon_visible:
            return False
        data = self._notify_data(NIF_INFO | NIF_MESSAGE | NIF_ICON | NIF_TIP)
        data.szInfoTitle = title[:63]
        data.szInfo = text[:255]
        data.dwInfoFlags = 0x1  # NIIF_INFO
        return bool(api.shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data)))

    # ------------------------------------------------------------- 菜单

    def _popup_menu(self) -> None:
        """右键菜单：显示主窗口 / 停止备份（仅运行中可用）/ 退出。"""
        api = self._api
        if api is None or not self._hwnd:
            return
        menu = api.user32.CreatePopupMenu()
        if not menu:
            return
        try:
            api.user32.AppendMenuW(menu, MF_STRING, MENU_SHOW, MENU_LABEL_SHOW)
            flags = MF_STRING if self._running else MF_STRING | MF_GRAYED
            api.user32.AppendMenuW(menu, flags, MENU_STOP, MENU_LABEL_STOP)
            api.user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
            api.user32.AppendMenuW(menu, MF_STRING, MENU_QUIT, MENU_LABEL_QUIT)
            point = _POINT()
            api.user32.GetCursorPos(ctypes.byref(point))
            # 菜单归属窗口必须先拿到前台，否则点空白处收不起来
            api.user32.SetForegroundWindow(self._hwnd)
            choice = api.user32.TrackPopupMenu(menu, TPM_RETURNCMD | TPM_RIGHTBUTTON,
                                               point.x, point.y, 0, self._hwnd, None)
            api.user32.PostMessageW(self._hwnd, WM_NULL, 0, 0)
        finally:
            api.user32.DestroyMenu(menu)
        if choice == MENU_SHOW:
            self._fire("show")
        elif choice == MENU_STOP:
            self._fire("stop")
        elif choice == MENU_QUIT:
            self._fire("quit")

    # ------------------------------------------------------------- 回调

    def _fire(self, which: str) -> None:
        """把托盘事件转给调用方的回调（只允许线程安全的动作）。"""
        callback = {"show": self._on_show, "stop": self._on_stop, "quit": self._on_quit}[which]
        try:
            callback()
        except Exception:  # 调用方异常只记日志
            self._log.exception("托盘回调 %s 失败", which)


def create_tray(*, icon_path: str | None, tooltip: str, on_show, on_stop, on_quit,
                log: logging.Logger) -> TrayHost | None:
    """建立托盘宿主；非 Windows 或窗口建不起来时返回 None（调用方据此退化）。

    Args:
        icon_path: 托盘图标路径（.ico）。
        tooltip: 图标悬停提示。
        on_show: 左键单击 / 气泡单击 / 二次启动唤起的回调（托盘线程执行）。
        on_stop: 托盘菜单「停止备份」的回调（托盘线程执行）。
        on_quit: 托盘菜单「退出」的回调（托盘线程执行）。
        log: 诊断日志。

    Returns:
        可用的 TrayHost；不可用时 None。
    """
    if sys.platform != "win32":
        return None
    host = TrayHost(icon_path=icon_path, tooltip=tooltip, on_show=on_show, on_stop=on_stop,
                    on_quit=on_quit, log=log)
    if host.available:
        return host
    host.close()
    return None


def request_restore() -> bool:
    """请已有实例把主窗口显示出来（第二个进程调用；找不到实例返回 False）。

    Note:
        单实例互斥量只负责「拒绝第二个实例」，唤起靠这条消息通道：GUI exe 没有控制台，
        原有的「已经在运行了」提示用户根本看不到。
    """
    if sys.platform != "win32":
        return False
    try:
        api = _get_api()
    except OSError:
        return False
    hwnd = api.user32.FindWindowW(WINDOW_CLASS_NAME, None)
    if not hwnd:
        return False
    return bool(api.user32.PostMessageW(hwnd, WM_RESTORE_REQUEST, 0, 0))
