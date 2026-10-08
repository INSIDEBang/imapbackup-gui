"""后台驻留与托盘的 GUI 回归：关闭三态、隐藏/恢复、后台结束通知、设置页与菜单栏。

托盘换成假替身（记录调用），窗口是真实 Tk 窗口：断言真实的 withdraw/deiconify/iconic
状态，而不是替身行为。整套用例共享一个 module 级真实例——每个用例重建一次 Tk 会让
Tcl 解释器（与库文件加载）反复起停，慢且脆；状态复位统一走 tray 夹具。
"""
import contextlib
import json
import logging
import sys

import pytest
from _pytest.monkeypatch import MonkeyPatch

tk = pytest.importorskip("tkinter")

import imapbackup_gui as g

pytestmark = pytest.mark.gui


class FakeTray:
    """假托盘宿主：只记录调用，不碰 Win32；show 的结果可注入（模拟图标挂出失败）。

    Note:
        真托盘图标是常驻的（程序启动时就挂着），所以替身默认 `visible=True`；
        「图标挂不出来」的场景用 `FakeTray(show_result=False)`。
    """

    def __init__(self, show_result: bool = True) -> None:
        self.calls: list[tuple] = []
        self.show_result = show_result
        self.visible = True
        self.running = False
        self.tooltip: str | None = None

    def show(self, tooltip: str) -> bool:
        self.calls.append(("show", tooltip))
        self.tooltip = tooltip
        self.visible = self.show_result
        return self.show_result

    # 没有 hide()：图标常驻，界面侧只能显示/刷新，唯一移除点是真宿主的 close()
    def set_tooltip(self, tooltip: str) -> None:
        self.calls.append(("set_tooltip", tooltip))
        self.tooltip = tooltip

    def set_running(self, running: bool) -> None:
        self.calls.append(("set_running", running))
        self.running = running

    def notify(self, title: str, text: str) -> None:
        self.calls.append(("notify", title, text))

    def close(self) -> None:
        self.calls.append(("close",))
        self.visible = False


_startup_trays: list[FakeTray] = []  # app 夹具构造时用的替身（见 app 夹具）


def widgets(node, kind):
    """递归收集某种控件（设置页详情是「面板 → 内容框 → 选项」的嵌套结构）。"""
    for child in node.winfo_children():
        if isinstance(child, kind):
            yield child
        yield from widgets(child, kind)


def drain_log(app) -> list[str]:
    """取事件队列里的运行日志行（日志面板与运行日志文件同一批记录）。"""
    lines: list[str] = []
    while not app.events.empty():
        event = app.events.get_nowait()
        if event[0] == "log":
            lines.append(event[1])
    return lines


def _normalize_window(app) -> None:
    """把共享窗口拉回 normal：上个用例可能留它 withdrawn/iconic/最大化（deiconify 不带回 normal）。"""
    if app.root.state() != "normal":
        app.root.deiconify()
        app.root.state("normal")
    app.root.update()


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    """真实实例，整个模块共用一个：托盘换成假替身（构造点被替换）。

    构造时用的那枚替身留在 `_startup_trays` 里，供「图标随启动常驻」的用例断言——
    每个用例的 `tray` 夹具会把 `app._tray` 换成新的替身。
    """
    patch = MonkeyPatch()
    log_dir = tmp_path_factory.mktemp("log")
    patch.setattr(g.core, "LOG_DIR", log_dir)
    patch.setattr(g, "LOG_DIR", log_dir)
    patch.setattr(g, "COLUMN_CONFIG_PATH", tmp_path_factory.mktemp("settings") / "treeview-columns.json")

    def factory(events, log):
        fake = FakeTray(show_result=True)
        fake.visible = False  # 启动前图标还没挂
        _startup_trays.append(fake)
        return fake

    patch.setattr(g, "_make_tray", factory)
    instance = g.BackupApp()
    try:
        yield instance
    finally:
        for handler in [h for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler)]:
            handler.close()
            logging.getLogger().removeHandler(handler)
        with contextlib.suppress(tk.TclError):
            tk.Tk.destroy(instance.root)
        patch.undo()


@pytest.fixture
def tray(app):
    """每个用例复位共享实例：可见窗口、空闲状态、默认偏好、一枚全新的假托盘。

    用例自己把 app._tray 换成 None / 换 show 结果时不需要还原——下一个用例会装上新的。
    """
    app._tray = FakeTray()
    app.running = False
    app.stopping = False
    app.stop_flag.clear()
    app._ui_settings[g.CLOSE_ACTION_KEY] = g.CLOSE_ACTION_ASK
    app._settings_action_var.set(g.CLOSE_ACTION_ASK)
    _normalize_window(app)
    yield app._tray
    _normalize_window(app)


# ------------------------------------------------------------------ 托盘常驻

def test_tray_icon_is_up_from_startup(app):
    """图标随程序启动常驻（窗口可见时也在），直到退出才收掉。"""
    assert _startup_trays, "app 夹具没记录构造时用的托盘替身"
    startup = _startup_trays[0]
    assert startup.visible
    assert ("show", g.TRAY_TOOLTIP_IDLE) in startup.calls


def test_tray_state_follows_the_run(app, tray):
    """tooltip 与菜单「停止备份」的可用性随运行状态走，窗口可见时也同步。"""
    app.running = True
    app._sync_tray()
    assert ("set_tooltip", g.TRAY_TOOLTIP_RUNNING) in tray.calls
    assert ("set_running", True) in tray.calls
    app.running = False
    app._sync_tray()
    assert ("set_tooltip", g.TRAY_TOOLTIP_IDLE) in tray.calls
    assert ("set_running", False) in tray.calls


# ------------------------------------------------------------------ 关闭三态

def test_close_with_tray_preference_hides_and_keeps_saving(app, tray, monkeypatch):
    """偏好=隐藏到后台：先存界面设置、收进托盘，窗口不销毁。"""
    saved = []
    monkeypatch.setattr(app, "_save_column_config", lambda: saved.append(True))
    monkeypatch.setattr(app, "_ask_close_action", lambda: pytest.fail("有偏好就不该弹询问框"))
    app._ui_settings[g.CLOSE_ACTION_KEY] = g.CLOSE_ACTION_TRAY
    app._on_close()
    assert app.root.state() == "withdrawn"
    assert saved == [True]
    assert ("show", g.TRAY_TOOLTIP_IDLE) in tray.calls
    assert ("set_running", False) in tray.calls


def test_close_with_tray_preference_uses_the_running_tooltip(app, tray, monkeypatch):
    monkeypatch.setattr(app, "_save_column_config", lambda: None)
    app._ui_settings[g.CLOSE_ACTION_KEY] = g.CLOSE_ACTION_TRAY
    app.running = True
    app._on_close()
    assert ("show", g.TRAY_TOOLTIP_RUNNING) in tray.calls
    assert ("set_running", True) in tray.calls


def test_close_with_exit_preference_closes_tray_and_window(app, tray, monkeypatch):
    """偏好=退出程序：托盘收掉、窗口销毁（沿用「先存设置再销毁」的既有顺序）。"""
    calls = []
    monkeypatch.setattr(app, "_save_column_config", lambda: calls.append("save"))
    monkeypatch.setattr(app.root, "destroy", lambda: calls.append("destroy"))
    app._ui_settings[g.CLOSE_ACTION_KEY] = g.CLOSE_ACTION_EXIT
    app._on_close()
    assert calls == ["save", "destroy"]
    assert ("close",) in tray.calls
    assert app._tray is None


def test_close_while_hidden_always_exits_without_asking(app, tray, monkeypatch):
    """隐藏状态下收到的是外部关闭请求（安装器/卸载器）：不询问、直接真退出。"""
    monkeypatch.setattr(app, "_ask_close_action", lambda: pytest.fail("隐藏状态不该弹询问框"))
    destroyed = []
    monkeypatch.setattr(app.root, "destroy", lambda: destroyed.append(True))
    app._ui_settings[g.CLOSE_ACTION_KEY] = g.CLOSE_ACTION_TRAY
    app.root.withdraw()
    app._on_close()
    assert destroyed == [True]
    assert ("close",) in tray.calls


def test_ask_preference_dialog_result_drives_the_action(app, tray, monkeypatch):
    """偏好=每次询问：对话框选哪个就做哪个，勾了「记住」才写偏好。"""
    monkeypatch.setattr(app, "_save_column_config", lambda: None)
    monkeypatch.setattr(app, "_ask_close_action", lambda: (g.CLOSE_ACTION_TRAY, False))
    app._on_close()
    assert app.root.state() == "withdrawn"
    assert app._close_action() == g.CLOSE_ACTION_ASK  # 没勾记住：偏好不动

    app.root.deiconify()
    destroyed = []
    monkeypatch.setattr(app.root, "destroy", lambda: destroyed.append(True))
    monkeypatch.setattr(app, "_ask_close_action", lambda: (g.CLOSE_ACTION_EXIT, True))
    app._on_close()
    assert destroyed == [True]
    assert app._ui_settings[g.CLOSE_ACTION_KEY] == g.CLOSE_ACTION_EXIT


def test_cancelled_dialog_changes_nothing(app, tray, monkeypatch):
    destroyed = []
    monkeypatch.setattr(app.root, "destroy", lambda: destroyed.append(True))
    monkeypatch.setattr(app, "_ask_close_action", lambda: None)
    app._on_close()
    assert destroyed == []
    assert app.root.state() == "normal"
    assert app._ui_settings[g.CLOSE_ACTION_KEY] == g.CLOSE_ACTION_ASK


# ------------------------------------------------------------------ 托盘不可用

@pytest.fixture
def no_tray(app):
    """托盘宿主缺席（非 Windows 或创建失败）：关闭时「隐藏到后台」必须退化，不能把人关在外面。"""
    app._tray = None
    app._ui_settings[g.CLOSE_ACTION_KEY] = g.CLOSE_ACTION_TRAY
    return app


def test_hide_degrades_to_iconify_without_a_tray(no_tray, monkeypatch):
    monkeypatch.setattr(no_tray, "_save_column_config", lambda: None)
    no_tray._on_close()
    assert no_tray.root.state() == "iconic"


def test_hide_degrades_when_the_icon_cannot_be_added(app, monkeypatch):
    app._tray = FakeTray(show_result=False)
    app._ui_settings[g.CLOSE_ACTION_KEY] = g.CLOSE_ACTION_TRAY
    monkeypatch.setattr(app, "_save_column_config", lambda: None)
    app._on_close()
    assert app.root.state() == "iconic"


# ------------------------------------------------------------------ 后台事件

def test_tray_show_event_restores_the_window(app, tray):
    """恢复窗口不碰图标：图标常驻，只把 tooltip 追平。"""
    app.root.withdraw()
    app._handle_event(("tray_show",))
    assert app.root.state() == "normal"
    assert tray.visible is True
    assert ("set_tooltip", g.TRAY_TOOLTIP_IDLE) in tray.calls


def test_tray_stop_event_only_stops_a_running_backup(app):
    app._handle_event(("tray_stop",))
    assert (app.running, app.stopping) == (False, False)
    app.running = True
    app._handle_event(("tray_stop",))
    assert app.stop_flag.is_set() and app.stopping is True


def test_run_finished_in_background_only_sends_a_balloon(app, tray):
    """后台隐藏期间备份结束：窗口保持隐藏，只弹托盘气泡。"""
    app.root.withdraw()
    app.running = True
    app._handle_event(("run_done", 3, 7, 61.5, False, 3))
    assert app.root.state() == "withdrawn"
    assert ("notify", g.TRAY_TITLE_DONE,
            f"本次备份 3 封，跳过 7 封，用时 {g._fmt_duration(61.5)}") in tray.calls
    assert tray.running is False
    assert tray.tooltip == g.TRAY_TOOLTIP_IDLE


def test_stopped_run_in_background_sends_the_stopped_balloon(app, tray):
    app.root.withdraw()
    app.running = True
    app._handle_event(("run_done", 0, 0, 3.0, True, 5))
    assert app.root.state() == "withdrawn"
    assert ("notify", g.TRAY_TITLE_STOPPED, g.TRAY_BODY_STOPPED) in tray.calls


def test_failed_run_in_background_does_not_pop_a_dialog(app, tray, monkeypatch):
    """后台失败也只发气泡：不在隐藏窗口背后弹阻断式错误框。"""
    monkeypatch.setattr(g.messagebox, "showerror", lambda *a, **k: pytest.fail("后台不该弹错误框"))
    app.root.withdraw()
    app.running = True
    app._handle_event(("run_failed", "boom"))
    assert ("notify", g.TRAY_TITLE_FAILED, g.TRAY_BODY_FAILED) in tray.calls


def test_visible_run_finished_keeps_the_dialog_path(app, monkeypatch):
    """窗口可见时行为不变：失败仍走原来的错误框。"""
    shown = []
    monkeypatch.setattr(g.messagebox, "showerror", lambda *a, **k: shown.append(a))
    app.running = True
    app._handle_event(("run_failed", "boom"))
    assert shown


def test_tray_quit_confirms_only_while_running(app, tray, monkeypatch):
    destroyed = []
    monkeypatch.setattr(app.root, "destroy", lambda: destroyed.append(True))
    app._handle_event(("tray_quit",))
    assert destroyed == [True]  # 空闲：直接退出

    destroyed.clear()
    asked = []
    monkeypatch.setattr(g.messagebox, "askokcancel", lambda *a, **k: asked.append(a) or False)
    app.running = True
    app._handle_event(("tray_quit",))
    assert asked and destroyed == []  # 运行中且用户拒绝：留在后台

    monkeypatch.setattr(g.messagebox, "askokcancel", lambda *a, **k: True)
    app._handle_event(("tray_quit",))
    assert destroyed == [True]


# ------------------------------------------------------------------ 窗口状态记忆

def test_hide_and_show_keeps_the_maximized_window(app, tray, monkeypatch):
    """最大化时隐藏、从托盘恢复：窗口带回最大化。

    withdraw/deiconify 自己不带回最大化（真机实测：回来是 normal + 最大化前的正常几何），
    恢复路径必须按记忆重放，否则用户看到「隐藏一次就丢最大化」。
    """
    monkeypatch.setattr(app, "_save_column_config", lambda: None)
    monkeypatch.setitem(app._ui_settings, g.CLOSE_ACTION_KEY, g.CLOSE_ACTION_TRAY)
    try:
        app.root.state("zoomed")
        app.root.update()
        app._on_close()
        assert app.root.state() == "withdrawn"
        app._handle_event(("tray_show",))
        assert app.root.state() == "zoomed"
    finally:
        _normalize_window(app)


def test_maximized_is_recorded_when_iconic_at_save(app, tray, monkeypatch):
    """最大化 → 任务栏最小化后关闭：保存的最大化不许丢。

    保存发生在窗口还 iconic 时（现场读 wm state 会记成普通窗口），托盘偏好下正是这条路径。
    """
    monkeypatch.setitem(app._ui_settings, g.CLOSE_ACTION_KEY, g.CLOSE_ACTION_TRAY)
    try:
        app.root.state("zoomed")
        app.root.update()
        app.root.iconify()
        app.root.update()
        assert app.root.state() == "iconic"
        app._on_close()
        assert app.root.state() == "withdrawn"
        saved = json.loads(g.COLUMN_CONFIG_PATH.read_text(encoding="utf-8"))
        assert saved["window"]["maximized"] is True
    finally:
        _normalize_window(app)


def test_tray_quit_while_hidden_keeps_the_maximized_choice(app, tray, monkeypatch):
    """隐藏态从托盘菜单退出：第二次落盘时 state 已是 withdrawn，也不许把最大化记丢。"""
    monkeypatch.setattr(app, "_quit", lambda: None)  # 只验落盘口径，不真拆共享实例
    monkeypatch.setitem(app._ui_settings, g.CLOSE_ACTION_KEY, g.CLOSE_ACTION_TRAY)
    try:
        app.root.state("zoomed")
        app.root.update()
        app._on_close()
        assert app.root.state() == "withdrawn"
        app._on_tray_quit()
        saved = json.loads(g.COLUMN_CONFIG_PATH.read_text(encoding="utf-8"))
        assert saved["window"]["maximized"] is True
    finally:
        _normalize_window(app)


# ------------------------------------------------------------------ 页面与设置

def test_menubar_carries_the_two_pages(app):
    labels = [app.menubar.entrycget(i, "label") for i in range(app.menubar.index("end") + 1)]
    assert labels == [label for _name, label in g.PAGE_LABELS] == ["备份", "设置"]
    assert app._page_var.get() == g.PAGE_BACKUP


def test_menubar_navigation_switches_pages(app):
    app.menubar.invoke(1)  # 设置
    assert app._page_var.get() == g.PAGE_SETTINGS
    # Tk 的 winfo children 按叠放次序返回（最低在前）：最后一项就是抬到最上层的那页
    assert app.page_host.winfo_children()[-1] is app.settings_page
    app.menubar.invoke(0)  # 备份
    assert app._page_var.get() == g.PAGE_BACKUP
    assert app.page_host.winfo_children()[-1] is app.backup_page


def test_settings_page_lists_the_sections_and_selects_the_first(app):
    assert [app.settings_nav.get(i) for i in range(app.settings_nav.size())] == \
        [title for _key, title in g.SETTINGS_SECTIONS]
    app._show_page(g.PAGE_SETTINGS)
    index = [key for key, _title in g.SETTINGS_SECTIONS].index(g.SETTINGS_WINDOW_SECTION)
    assert app.settings_nav.curselection() == (index,)
    assert app._settings_section == g.SETTINGS_WINDOW_SECTION
    assert app._settings_action_var.get() == g.CLOSE_ACTION_ASK
    app._show_page(g.PAGE_BACKUP)


def test_settings_page_offers_the_three_close_options(app):
    detail = app._settings_sections[g.SETTINGS_WINDOW_SECTION]
    assert [w.cget("text") for w in widgets(detail, tk.Radiobutton)] == \
        [label for _value, label in g.CLOSE_ACTION_LABELS]


def test_settings_page_reflects_a_saved_preference(app):
    app._ui_settings[g.CLOSE_ACTION_KEY] = g.CLOSE_ACTION_TRAY
    app._show_page(g.PAGE_SETTINGS)
    assert app._settings_action_var.get() == g.CLOSE_ACTION_TRAY
    app._show_page(g.PAGE_BACKUP)


def test_changing_the_option_lands_in_memory_then_on_disk_with_the_close(app, tray, monkeypatch):
    """设置页改动即时进内存，落盘仍沿用「关闭/隐藏/退出时统一保存一次」。"""
    settings_path = g.COLUMN_CONFIG_PATH
    app._show_page(g.PAGE_SETTINGS)
    app._settings_action_var.set(g.CLOSE_ACTION_TRAY)
    app._on_close_action_change()
    assert app._close_action() == g.CLOSE_ACTION_TRAY
    assert app._ui_settings[g.CLOSE_ACTION_KEY] == g.CLOSE_ACTION_TRAY
    app._show_page(g.PAGE_BACKUP)
    app._on_close()  # 偏好=隐藏：落盘走的就是这条路径
    assert app.root.state() == "withdrawn"
    saved = json.loads(settings_path.read_text(encoding="utf-8"))
    assert saved[g.CLOSE_ACTION_KEY] == g.CLOSE_ACTION_TRAY
    app.root.deiconify()


def test_invalid_close_action_falls_back_to_ask_and_warns(app, tmp_path, monkeypatch):
    settings_path = tmp_path / "treeview-columns.json"
    settings_path.write_text(json.dumps({"version": 1, "close_action": "always"}), encoding="utf-8")
    monkeypatch.setattr(g, "COLUMN_CONFIG_PATH", settings_path)
    original = app._ui_settings
    try:
        app._ui_settings = app._read_ui_settings()
        assert app._close_action() == g.CLOSE_ACTION_ASK
        assert any("关闭行为偏好" in line for line in drain_log(app))
    finally:
        app._ui_settings = original


def test_tray_host_wiring_is_available_on_windows():
    """托盘宿主接线（ctypes 全套）：真造一个宿主，挂出图标、走通二次启动唤起通道再收掉。

    Note:
        受限环境（无通知区域、沙箱拦 Win32 资源）下 `create_tray` 按契约返回 None——
        这里跳过而不是失败，图标外观与菜单仍以真机人工验收为准。
    """
    if sys.platform != "win32":
        pytest.skip("托盘只在 Windows 上可用")
    host = g.imapbackup_tray.create_tray(
        icon_path=g._window_icon_path(), tooltip="tray-test",
        on_show=lambda: None, on_stop=lambda: None, on_quit=lambda: None,
        log=logging.getLogger("tray-test"))
    if host is None:
        pytest.skip("当前环境建不出托盘宿主（无通知区域或被沙箱拦截）")
    try:
        if not host.show("tray-test"):
            pytest.skip("当前环境不给挂托盘图标（通知区域不可用）")
        assert g.imapbackup_tray.request_restore()  # 找到隐藏消息窗口并投递唤起请求
    finally:
        host.close()
