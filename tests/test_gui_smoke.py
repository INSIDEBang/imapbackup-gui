# -*- coding: utf-8 -*-
"""GUI 无头冒烟：实例化真实的 BackupApp，喂合成队列事件，断言 UI 状态。不联网。

整套用例共享同一个 module 级 app 实例，**pytest 按定义顺序执行**，所以前一节留下的
界面状态就是后一节的前置条件（连接 → 树 → stale → 工具栏 → 级联 → … → 状态条 → 气泡）。
这个顺序依赖是有意保留的，别重排测试。

标了 @pytest.mark.gui：需要真实 Tk 显示与 ttk 主题，几何断言（winfo_width、
winfo_rootx 等）在异 DPI / 异主题的机器上可能不成立。无显示环境的 runner 用
`pytest -m "not gui"` 跳过。
"""
import logging
from datetime import datetime

import pytest
from _pytest.monkeypatch import MonkeyPatch

tk = pytest.importorskip("tkinter")
import tkinter.font as tkfont

import imapbackup_gui as g

pytestmark = pytest.mark.gui

NAMES = [("INBOX", "INBOX.mbox", "INBOX", "INBOX"),
         ("&UXZO1mWHTvZZOQ-", "其他文件夹.mbox", "其他文件夹", "其他文件夹"),
         ("&UXZO1mWHTvZZOQ-/QQ&kK5O9ouilgU-", "其他文件夹.QQ邮件订阅.mbox",
          "其他文件夹/QQ邮件订阅", "其他文件夹/QQ邮件订阅"),
         ("&UXZO1mWHTvZZOQ-/a", "其他文件夹.a.mbox", "其他文件夹/草稿", "其他文件夹/草稿"),
         ("&UXZO1mWHTvZZOQ-/b", "其他文件夹.b.mbox", "其他文件夹/草稿", "其他文件夹/草稿"),
         ("Archive/2024", "Archive.2024.mbox", "Archive/2024", "Archive/2024")]
# 单条子文件夹的场景（用户报过的问题）；带上 Archive 分支，免得重载后合成父节点被折叠、
# 后面依赖展开态的断言失效
SINGLE = [("INBOX", "INBOX.mbox", "INBOX", "INBOX"),
          ("&UXZO1mWHTvZZOQ-", "其他文件夹.mbox", "其他文件夹", "其他文件夹"),
          ("&UXZO1mWHTvZZOQ-/QQ&kK5O9ouilgU-", "其他文件夹.QQ邮件订阅.mbox",
           "其他文件夹/QQ邮件订阅", "其他文件夹/QQ邮件订阅"),
          ("Archive/2024", "Archive.2024.mbox", "Archive/2024", "Archive/2024")]
V = {"server": "imap.example.com", "user": "u@e.com", "password": "p", "ssl": True, "port": 993}

PARENT, SYNTH, LEAF = ("其他文件夹",), ("Archive",), ("Archive", "2024")
DUP = ("其他文件夹", "草稿")   # 两条不同 foldername 消毒后落到同一路径，合并成一个节点
OTHER = "&UXZO1mWHTvZZOQ-"
CHILD = "&UXZO1mWHTvZZOQ-/QQ&kK5O9ouilgU-"
DUP_A = "&UXZO1mWHTvZZOQ-/a"
DUP_B = "&UXZO1mWHTvZZOQ-/b"

SHORT_DONE = "完成：本次备份 123456 封，跳过 123456 封（本地已存在），用时 1:05"
LONG_STATUS = "扫描 12/45：Deleted Messages/2024 年第四季度归档副本(2)/再一个很长的名字" * 4
LONG_COUNTS = ("服务器共 123456789012345 ｜ 待备份 123456789012345 ｜ "
               "已备份 123456789012345 / 123456789012345")
MID_COUNTS = "服务器共 123456789 ｜ 待备份 123456789 ｜ 已备份 123456789 / 123456789"


@pytest.fixture(scope="module", autouse=True)
def _redirect_log_dir(tmp_path_factory):
    """BackupApp._setup_logging 会写当日运行日志，测试不能碰真实的那份。

    imapbackup_gui 在模块级就把 core.LOG_DIR 拷成了自己的 LOG_DIR，两处都要改。
    """
    patch = MonkeyPatch()
    log_dir = tmp_path_factory.mktemp("log")
    patch.setattr(g.core, "LOG_DIR", log_dir)
    patch.setattr(g, "LOG_DIR", log_dir)
    yield log_dir
    patch.undo()


@pytest.fixture(scope="module")
def app():
    """The real app, created once for the whole module.

    见模块 docstring：用例顺序是有意义的，重排会破坏前置状态。
    """
    root = logging.getLogger()
    instance = g.BackupApp()
    try:
        yield instance
    finally:
        instance.root.geometry("960x736")
        instance.root.destroy()
        # _setup_logging 会把 root.handlers 换成 QueueLogHandler + 文件 handler；
        # 文件 handler 指向已销毁的临时目录，清掉免得留着
        for handler in [h for h in root.handlers if isinstance(h, logging.FileHandler)]:
            handler.close()
            root.removeHandler(handler)


def pump(instance, n=3):
    for _ in range(n):
        instance._pump()
        instance.root.update_idletasks()


def click(instance, widget):
    widget.event_generate("<Button-1>")
    instance.root.update()


def press_enter(instance, widget):
    # Tk 只在控件持有键盘焦点时才把合成的 KeyPress 派发给绑定；测试窗口不是前台窗口，须先抢焦点
    widget.focus_force()
    widget.event_generate("<Return>")
    instance.root.update()


def row_by(instance, foldername):
    return [r for r in instance.folder_rows if r["foldername"] == foldername][0]


def is_open(instance, key):
    return bool(instance._folder_nodes[key]["open"])


def row_tags(instance, iid):
    """tree.item(iid, "tags") 返回类型不稳定：无 tag 是空串、有 tag 是元组，统一成 list。"""
    tags = instance.tree.item(iid, "tags")
    return tags.split() if isinstance(tags, str) else list(tags)


def icon_rgb(color: str) -> tuple:
    """#rrggbb → Tk 的 (r, g, b)：按钮图标就是按这个色值逐像素画上去的。"""
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


def assert_start_button_idle(instance):
    """开始按钮回到静止态：文案与绿三角图标都回位。"""
    assert instance.start_btn.cget("text") == g.START_BTN_LABEL
    assert instance.start_btn.cget("image") == str(instance.start_icon)


def assert_stop_button_idle(instance):
    """停止按钮回到静止态：文案与红方块图标都回位。"""
    assert instance.stop_btn.cget("text") == g.STOP_BTN_LABEL
    assert instance.stop_btn.cget("image") == str(instance.stop_icon)


class FakeBox:
    """messagebox 替身：记录弹了什么，askokcancel 一律放行。"""
    def __init__(self):
        self.warnings = []

    def showwarning(self, title, msg, parent=None):
        self.warnings.append((title, msg))

    def showerror(self, title, msg, parent=None):
        self.warnings.append((title, msg))

    def askokcancel(self, title, msg, parent=None):
        return True


class _Ev:
    x_root = 100
    y_root = 100


def _message(folder="INBOX", index=1, total=23, **overrides):
    fields = {"folder": folder, "index": index, "total": total, "timestamp": datetime.now(),
              "from": "Alice <a@b.com>", "subject": "=?utf-8?B?5L2g5aW9?=",
              "size": 12600, "msg_id": "<x@y>"}
    fields.update(overrides)
    return fields


def test_window_initial_state(app):
    assert app.root.title() == "IMAP Backup GUI"
    assert str(app.start_btn.cget("state")) == "disabled"
    # 两个按钮各自挂着一枚代码画的图标，静止态文案固定（改动前开始/停止按钮都是纯文字）
    assert app.start_btn.cget("text") == g.START_BTN_LABEL
    assert app.stop_btn.cget("text") == g.STOP_BTN_LABEL
    assert app.start_btn.cget("image") == str(app.start_icon)
    assert app.stop_btn.cget("image") == str(app.stop_icon)
    assert app.folder_ph.cget("text") == g.FOLDER_PLACEHOLDER_TEXT
    assert app.folder_ph.winfo_manager() == "grid"
    assert app.list_ph.cget("text") == g.LIST_PLACEHOLDER_TEXT
    assert app.list_ph.winfo_manager() == "grid"


def test_button_icons_are_code_drawn_in_color(app):
    """图标是代码逐像素画进 PhotoImage 的彩色图形，不是字库字形。

    字形只有一种颜色、且按钮禁用时会被系统灰盖掉，所以中心像素必须正好是指定色值，
    并且不是透明像素（透明＝那块根本没画）。
    """
    size = app.start_icon.width()
    assert size == app.stop_icon.width()
    assert size == g._icon_size()
    for icon, color in ((app.start_icon, g.PLAY_ICON_COLOR), (app.stop_icon, g.STOP_ICON_COLOR)):
        center = size // 2
        assert tuple(icon.get(center, center))[:3] == icon_rgb(color), (icon.get(center, center), color)
        assert not icon.transparency_get(center, center)
    assert app.start_icon.get(size // 2, size // 2) != app.stop_icon.get(size // 2, size // 2)


def test_version_is_shared_and_status_bar_starts_on_one_row(app):
    """版本统一 + 初始单行状态条 —— 必须在任何 _begin_run_ui 之前查，行一旦放出不再收回。"""
    assert g.__version__ == g.core.__version__, (g.__version__, g.core.__version__)
    assert app.progress.winfo_manager() == "" and app._msg_bar.winfo_manager() == ""
    assert app.start_btn.winfo_manager() == "pack"
    assert app.start_btn.pack_info()["side"] == "right"
    assert app.stop_btn.pack_info()["side"] == "right"
    assert app.count_var.get() == "尚未开始备份", app.count_var.get()


def test_folder_load_marks_the_account_connected(app):
    """folders_loaded 事件走完真实流程：字段先被读进 app，再投队列。"""
    app.server_var.set(V["server"])
    app.user_var.set(V["user"])
    app.password_var.set(V["password"])
    app.events.put(("folders_loaded", (NAMES, V)))
    pump(app)

    assert app.connect_state == "connected"
    assert app.connect_btn.cget("text") == "✓ 已成功连接"
    assert str(app.connect_btn.cget("state")) == "disabled"
    # 已连接：提示行未挂出（winfo_manager 判布局管理器，winfo_ismapped 滞后不可靠）
    assert app.conn_hint.cget("text") == "" and app.conn_hint.winfo_manager() == ""
    assert app.root.winfo_width() == 960 and app.root.winfo_height() == 736, (
        f"{app.root.winfo_width()}x{app.root.winfo_height()}")
    assert app.folder_ph.winfo_manager() == "", app.folder_ph.winfo_manager()
    assert len(app.folder_rows) == 6, [r["display"] for r in app.folder_rows]
    assert [r["foldername"] for r in app.folder_rows] == ["Archive/2024", "INBOX", OTHER, CHILD, DUP_A, DUP_B]
    assert row_by(app, "INBOX")["var"].get() is True
    assert not row_by(app, "Archive/2024")["var"].get()
    assert str(app.start_btn.cget("state")) == "normal", app.start_btn.cget("state")


def test_folder_tree_structure(app):
    """末段显示、真实/合成父节点、箭头、勾选符号、子节点缩进。"""
    assert sorted(app._folder_nodes) == [SYNTH, LEAF, ("INBOX",), PARENT, ("其他文件夹", "QQ邮件订阅"), DUP], (
        sorted(app._folder_nodes))
    assert app._folder_nodes[PARENT]["rows"]
    assert app._folder_nodes[SYNTH]["arrow"] is not None and not app._folder_nodes[SYNTH]["rows"]
    assert app._folder_nodes[("INBOX",)]["arrow"] is None
    assert is_open(app, SYNTH)
    assert row_by(app, "INBOX")["check_btn"].cget("text") == "☑ INBOX"
    assert row_by(app, "Archive/2024")["check_btn"].cget("text") == "☐ 2024"
    assert row_by(app, CHILD)["check_btn"].cget("text") == "☐ QQ邮件订阅"
    assert row_by(app, CHILD)["display"] == "其他文件夹/QQ邮件订阅"
    assert row_by(app, DUP_A)["check_btn"].cget("text") == "☐ 草稿"
    assert row_by(app, DUP_B)["check_btn"].cget("text") == "☐ 草稿"
    assert len(app._folder_nodes[DUP]["rows"]) == 2, len(app._folder_nodes[DUP]["rows"])
    assert row_by(app, DUP_A)["check_btn"] is row_by(app, DUP_B)["check_btn"]
    dup_widgets = app._folder_nodes[DUP]["frame"].winfo_children()[0].winfo_children()
    assert len(dup_widgets) == 2, len(dup_widgets)
    assert app._folder_nodes[DUP]["frame"].master is app._folder_nodes[PARENT]["kids"]
    assert app._folder_nodes[LEAF]["frame"].master is app._folder_nodes[SYNTH]["kids"]
    app.root.update()
    leaf_x = (app._folder_nodes[LEAF]["frame"].winfo_rootx()
              - app._folder_nodes[SYNTH]["frame"].winfo_rootx())
    assert 10 <= leaf_x <= 40, leaf_x


def test_editing_credentials_makes_the_connection_stale(app):
    app.server_var.set("imap.qq.com")
    pump(app)
    assert app.connect_state == "stale"
    assert app.connect_btn.cget("text") == "⇄ 连接并加载文件夹"
    assert str(app.connect_btn.cget("state")) == "normal"
    assert str(app.start_btn.cget("state")) == "disabled"
    # 过期提示是红字（需要用户重新连接），提示行重新挂出
    assert app.conn_hint.cget("text") == "连接信息已更改，请重新连接并加载文件夹"
    assert str(app.conn_hint.cget("foreground")) == g.HINT_RED
    assert app.conn_hint.winfo_manager() == "grid"
    app.root.update()
    group_h_with_hint = app.conn_hint.master.winfo_height()

    app.server_var.set(V["server"])
    pump(app)
    app.root.update()
    assert app.connect_state == "connected"
    assert app.conn_hint.cget("text") == "" and app.conn_hint.winfo_manager() == ""
    # 空提示让出整行：连接组变矮，让出的高度由下方邮件列表面板吸收
    assert app.conn_hint.master.winfo_height() < group_h_with_hint, (
        app.conn_hint.master.winfo_height(), group_h_with_hint)
    assert str(app.start_btn.cget("state")) == "normal"


def test_folder_toolbar_select_all_and_inbox_only(app):
    app._set_all_folders(True)
    assert all(r["var"].get() for r in app.folder_rows)
    assert all(is_open(app, key) for key, node in app._folder_nodes.items() if node["arrow"] is not None)
    app._set_inbox_only()
    assert row_by(app, "INBOX")["var"].get() and not row_by(app, OTHER)["var"].get()


def test_parent_check_cascades_and_shows_a_third_state(app):
    app._on_folder_check(PARENT)
    assert row_by(app, OTHER)["var"].get() and row_by(app, CHILD)["var"].get()
    assert row_by(app, DUP_A)["var"].get() and row_by(app, DUP_B)["var"].get()
    assert row_by(app, OTHER)["check_btn"].cget("text") == "☑ 其他文件夹"
    assert str(app.start_btn.cget("state")) == "normal"
    sub = {r["foldername"] for r in app._folder_subtree(PARENT)}
    for fn in (OTHER, CHILD, DUP_A, DUP_B):
        assert fn in sub, sorted(sub)
    assert "Archive/2024" not in sub and "INBOX" not in sub, sorted(sub)

    row_by(app, CHILD)["var"].set(False)
    app._render_folder_checks()
    assert row_by(app, OTHER)["check_btn"].cget("text") == "⊟ 其他文件夹"

    app._on_folder_check(PARENT)
    assert not any(r["var"].get() for r in app._folder_subtree(PARENT))
    assert row_by(app, OTHER)["check_btn"].cget("text") == "☐ 其他文件夹"

    # 反向部分态：只勾最深层子节点，父文件夹自身跟着进清单，符号是混合态 ⊟
    app._on_folder_check(DUP)
    assert row_by(app, OTHER)["check_btn"].cget("text") == "⊟ 其他文件夹"
    assert row_by(app, OTHER)["var"].get()
    assert row_by(app, DUP_A)["var"].get() and row_by(app, DUP_B)["var"].get()
    assert not row_by(app, CHILD)["var"].get()

    app._on_folder_check(DUP)
    assert not any(r["var"].get() for r in app._folder_subtree(PARENT))
    assert not row_by(app, OTHER)["var"].get()


def test_single_child_folder_toggles_the_parent(app):
    app.events.put(("folders_loaded", (SINGLE, V)))
    pump(app)
    assert not row_by(app, OTHER)["var"].get() and not row_by(app, CHILD)["var"].get()
    assert row_by(app, OTHER)["check_btn"].cget("text") == "☐ 其他文件夹"
    assert len(app._descendant_rows(PARENT)) == 1
    assert len(app._folder_nodes[PARENT]["rows"]) == 1
    click(app, row_by(app, CHILD)["check_btn"])
    assert row_by(app, CHILD)["var"].get() is True
    assert row_by(app, OTHER)["var"].get() is True
    assert row_by(app, OTHER)["check_btn"].cget("text") == "☑ 其他文件夹"
    click(app, row_by(app, CHILD)["check_btn"])
    assert row_by(app, CHILD)["var"].get() is False
    assert row_by(app, OTHER)["var"].get() is False
    assert row_by(app, OTHER)["check_btn"].cget("text") == "☐ 其他文件夹"


def test_click_checks_expands_and_synthetic_node_is_inert(app):
    click(app, row_by(app, "Archive/2024")["check_btn"])
    assert row_by(app, "Archive/2024")["var"].get() is True
    click(app, row_by(app, "Archive/2024")["check_btn"])
    assert row_by(app, "Archive/2024")["var"].get() is False
    assert app._folder_nodes[SYNTH]["arrow"].cget("text") == g.ARROW_OPEN
    app._toggle_folder_open(SYNTH)
    assert not is_open(app, SYNTH)
    assert app._folder_nodes[SYNTH]["kids"].winfo_manager() == ""
    assert not app._folder_nodes[LEAF]["frame"].winfo_ismapped()
    assert app._folder_nodes[SYNTH]["arrow"].cget("text") == g.ARROW_CLOSED
    # 折叠只收起，不销毁：展开态、勾选态和控件都得还在
    assert app._folder_nodes[LEAF]["frame"].winfo_exists()
    assert row_by(app, "Archive/2024")["check_btn"] is not None
    assert not row_by(app, "Archive/2024")["var"].get()
    app._toggle_folder_open(SYNTH)
    assert is_open(app, SYNTH) and app._folder_nodes[SYNTH]["kids"].winfo_manager() == "pack"

    synthetic_row = app._folder_nodes[SYNTH]["frame"].winfo_children()[0].winfo_children()[1]
    click(app, synthetic_row)
    assert not row_by(app, "Archive/2024")["var"].get()
    app._set_all_folders(False)
    assert not any(r["var"].get() for r in app.folder_rows)


def test_reload_keeps_checks_and_expansion_state(app):
    row_by(app, OTHER)["var"].set(True)
    row_by(app, "INBOX")["var"].set(True)
    app._toggle_folder_open(PARENT)
    app.root.update()
    app.events.put(("folders_loaded", (NAMES, V)))
    pump(app)
    assert row_by(app, OTHER)["var"].get() is True and row_by(app, "INBOX")["var"].get() is True
    assert row_by(app, "Archive/2024")["var"].get() is False
    assert not is_open(app, PARENT) and is_open(app, SYNTH)


def test_totals_message_and_done_events_fill_the_ui(app):
    app.events.put(("totals", 12345, 23))
    pump(app)
    assert "12345" in app.count_var.get() and "23" in app.count_var.get(), app.count_var.get()
    assert float(app.progress["maximum"]) == 23.0

    app.events.put(("message", _message()))
    pump(app)
    kids = app.tree.get_children()
    assert len(kids) == 1
    values = app.tree.item(kids[0], "values")
    assert values[0] == "Alice" and values[1] == "你好" and values[3] == "INBOX" and values[4] == "12.30 KB", values
    assert app.backed_count == 1 and "已备份 1 / 23" in app.count_var.get(), app.count_var.get()
    # 第一行插入时收掉空态提示
    assert app.list_ph.winfo_manager() == "", app.list_ph.winfo_manager()


def test_message_rows_are_zebra_striped(app):
    """偶数行打 zebra tag，奇数行走 Treeview 默认白底；新一轮从第 1 行重新起算。"""
    app._clear_message_rows()
    app.events.put(("message", _message()))
    pump(app)

    assert str(app.tree.tag_configure(g.ZEBRA_TAG, "background")).lower() == g.ZEBRA_BG.lower(), (
        app.tree.tag_configure(g.ZEBRA_TAG, "background"))
    first = app.tree.get_children()[0]
    assert row_tags(app, first) == [], row_tags(app, first)

    for i in range(2, 7):
        app.events.put(("message", _message(index=i, from_="b@b.com", subject=f"第 {i} 封",
                                            size=100, msg_id=f"<{i}@y>")))
    pump(app)
    assert [row_tags(app, iid) for iid in app.tree.get_children()] == (
        [[], [g.ZEBRA_TAG], [], [g.ZEBRA_TAG], [], [g.ZEBRA_TAG]])

    app._clear_message_rows()
    assert not app.tree.get_children() and app._row_index == 0
    # 新一轮开始前提示重新挂出（待备份为 0 时整轮都不会有行，提示必须留着）
    assert app.list_ph.winfo_manager() == "grid", app.list_ph.winfo_manager()

    app.events.put(("message", _message(subject="重跑首轮", msg_id="<z@y>")))
    pump(app)
    first = app.tree.get_children()[0]
    assert row_tags(app, first) == [], row_tags(app, first)
    assert app.tree.item(first, "values")[1] == "重跑首轮"


def test_run_done_summaries(app):
    app.events.put(("log", "2026-09-11 10:00:00 | INFO    | gui        | _backup_worker:1 | 测试日志行"))
    app.events.put(("run_done", 1, 12322, 65.0, False, 23))
    pump(app)
    assert "测试日志行" in app.log_text.get("1.0", "end")
    assert "完成：本次备份 1 封" in app.status_var.get() and "用时 1:05" in app.status_var.get(), (
        app.status_var.get())
    assert str(app.start_btn.cget("state")) == "normal"

    app.events.put(("totals", 100, 0))
    app.events.put(("run_done", 0, 100, 3.0, False, 0))
    pump(app)
    assert app.status_var.get() == "已是最新，无待备份邮件", app.status_var.get()

    app.events.put(("run_done", 5, 100, 9.0, True, 20))
    pump(app)
    assert app.status_var.get() == "已停止：本次备份 5 封", app.status_var.get()


def test_config_is_locked_during_a_run(app):
    """树里的箭头与名称是 tk.Label：它有 -state 选项，但置灰和阻断绑定都不生效，
    所以不进 _config_widgets，改由 _set_config_enabled 置灰 + 两个 handler 的 running
    判定拦截。Entry/Button（含文件夹工具栏）走 state=disabled。

    走 _on_start 的真实界面侧路径：运行中的提示必须自己亮出来。
    """
    app.running = True
    app._begin_run_ui()
    lockable = [w for w, _ in app._config_widgets if isinstance(w, (tk.Entry, tk.Button))]
    assert all(str(w.cget("state")) == "disabled" for w in lockable), len(app._config_widgets)
    assert bool(app._folder_clickables)
    assert all(str(w.cget("foreground")) == "gray" and str(w.cget("cursor")) == "arrow"
               for w, _ in app._folder_clickables), len(app._folder_clickables)
    assert app.conn_hint.cget("text") == "备份运行中，账户与服务器信息不可修改，请先停止"
    assert str(app.conn_hint.cget("foreground")) == g.HINT_GRAY
    assert app.conn_hint.winfo_manager() == "grid"
    assert app.connect_btn.cget("text") == "⇄ 连接并加载文件夹"
    assert str(app.connect_btn.cget("state")) == "disabled"
    assert str(app.start_btn.cget("state")) == "disabled"
    assert str(app.stop_btn.cget("state")) == "normal"

    # 锁定期间点击必须无效：勾选不翻转、展开态不变
    states_before = [r["var"].get() for r in app.folder_rows]
    open_before = {key: node["open"] for key, node in app._folder_nodes.items()}
    click(app, app._folder_nodes[("INBOX",)]["rows"][0]["check_btn"])
    click(app, app._folder_nodes[PARENT]["arrow"])
    assert [r["var"].get() for r in app.folder_rows] == states_before
    assert {key: node["open"] for key, node in app._folder_nodes.items()} == open_before

    app.running = False
    app._set_config_enabled(True)
    app._apply_connect_state()
    assert all(str(w.cget("foreground")) == fg and str(w.cget("cursor")) == "hand2"
               for w, fg in app._folder_clickables)

    # 解锁后点击重新生效（点两次还原，避免影响后续断言）
    inbox_btn = app._folder_nodes[("INBOX",)]["rows"][0]["check_btn"]
    click(app, inbox_btn)
    assert [r["var"].get() for r in app.folder_rows] != states_before
    click(app, inbox_btn)
    assert [r["var"].get() for r in app.folder_rows] == states_before
    assert all(str(w.cget("state")) == "normal" for w, _ in app._config_widgets
               if isinstance(w, (tk.Entry, tk.Button)) and w is not app.connect_btn)
    assert app.conn_hint.cget("text") == "" and app.conn_hint.winfo_manager() == ""
    # 连接仍有效时，结束运行后连接按钮保持"已成功连接"禁用态（原设计：避免重复登录）
    assert str(app.connect_btn.cget("state")) == "disabled"

    # 连接已过期时，结束运行要把按钮重新放开并给出过期提示
    app.server_var.set("imap.qq.com")
    pump(app)
    app._end_run_controls()
    assert str(app.connect_btn.cget("state")) == "normal"
    # 刚解锁时提示行是灰字，过期提示必须覆盖回红字（不能残留上一次的灰）
    assert app.conn_hint.cget("text") == "连接信息已更改，请重新连接并加载文件夹"
    assert str(app.conn_hint.cget("foreground")) == g.HINT_RED
    assert app.conn_hint.winfo_manager() == "grid"
    app.server_var.set(V["server"])
    pump(app)


def test_enter_triggers_connect_with_the_same_button_gating(app, monkeypatch):
    """五个框都绑定 Enter，且与鼠标共用 _on_connect 的按钮态门控。"""
    box = FakeBox()
    monkeypatch.setattr(g, "messagebox", box)

    # 只查绑定注册，不合成 KP_Enter：合成小键盘事件的语义不确定，断言会空过
    entries = (app.user_entry, app.server_entry, app.port_entry, app.password_entry, app.outdir_entry)
    assert all(str(w.bind("<Return>")) != "" and str(w.bind("<KP_Enter>")) != "" for w in entries)

    app._connect_worker = lambda _v: None   # 不起真实网络线程
    app.connect_state = "idle"
    app._apply_connect_state()
    press_enter(app, app.password_entry)
    assert app.connect_state == "connecting"
    assert "正在连接" in str(app.connect_btn.cget("text")), (
        app.connect_state, app.connect_btn.cget("text"))

    # 连接中：输入框并未禁用，只有按钮被禁用——门控必须挡住，否则起第二条连接线程
    press_enter(app, app.password_entry)
    assert app.connect_state == "connecting"
    assert str(app.connect_btn.cget("state")) == "disabled"
    assert app.conn_hint.winfo_manager() == "", (
        app.connect_state, app.connect_btn.cget("state"), app.conn_hint.cget("text"))

    # 备份运行中：配置区被 _set_config_enabled 置灰，Enter 同样静默
    app.connect_state = "connected"
    app.running = True
    app._begin_run_ui()
    press_enter(app, app.outdir_entry)
    assert app.connect_state == "connected"
    assert str(app.connect_btn.cget("state")) == "disabled", (app.connect_state,)
    app.running = False
    app._end_run_controls()

    # 字段不全时 Enter 只弹校验提示、不发起连接；清空密码让连接过期是既有行为
    app.password_var.set("")
    box.warnings.clear()
    press_enter(app, app.outdir_entry)
    assert app.connect_state == "stale"
    assert ("信息不完整", "请填写密码。") in box.warnings, (app.connect_state, box.warnings)
    app.password_var.set(V["password"])
    pump(app)

    # 一个文件夹都没解析出来时提示必须重新挂回，否则那块区域是一片纯白
    app._rebuild_folders([])
    assert app.folder_ph.winfo_manager() == "grid" and not app.folder_rows and not app._folder_nodes, (
        app.folder_ph.winfo_manager())


def test_status_bar_rows_version_and_button_width(app):
    """两行结构、版本号斜体、两个按钮在任何宽度下都保持完整自然宽度。"""
    # 行已在锁定段被 _begin_run_ui 放出；再调一次验证幂等（已挂出时直接返回）
    app._ensure_status_rows()
    # 状态消息必须和进度条/按钮不在同一行：同一行里没有可退让的控件，文案变长就会把按钮挤扁
    assert app.status_label.master != app.start_btn.master
    assert app.status_label.master != app.progress.master
    app.root.update()
    assert app.progress.winfo_manager() == "pack" and app._msg_bar.winfo_manager() == "pack"
    assert app._msg_bar.winfo_rooty() > app._bar.winfo_rooty(), (
        app.progress.winfo_manager(), app._msg_bar.winfo_manager())
    assert app.version_label.cget("text") == f"v{g.__version__}"
    assert app.version_label.pack_info()["side"] == "right"

    version_font = tkfont.Font(font=str(app.version_label.cget("font")))
    base_font = tkfont.nametofont("TkDefaultFont")
    assert version_font.cget("slant") == "italic"
    assert version_font.cget("family") == base_font.cget("family")
    assert abs(int(version_font.cget("size"))) == abs(int(base_font.cget("size")))

    # 回归：最小窗口宽度 + 6 位数计数 + 长完成消息，两个按钮都得保持完整自然宽度
    # （winfo_width == winfo_reqwidth 就是"没被挤扁"的可执行定义）
    # 用 update 而非 update_idletasks：后者只跑布局管理器、不真正改窗口尺寸，bar 会停在旧宽度上
    app.root.geometry("800x620")
    app.server_total = app.pending_total = app.backed_count = 123456
    app._refresh_counts()
    app._set_status(SHORT_DONE, error=False)
    app.root.update()
    bar_width = app.start_btn.master.winfo_width()
    assert bar_width < 820, bar_width
    squish = [(b, b.winfo_width(), b.winfo_reqwidth()) for b in (app.start_btn, app.stop_btn)
              if b.winfo_width() < b.winfo_reqwidth()]
    assert not squish, squish
    # 计数行与状态行各自一行；状态行现在有两个孩子：状态文本 + 版本号
    assert len(app.count_var.get()) > 40 and len(app.status_var.get()) > 40
    assert len(app.progress.master.winfo_children()) == 4
    assert len(app.status_label.master.winfo_children()) == 2
    # 这个量级还没到截断阈值：两行显示即全文
    assert app.status_var.get() == SHORT_DONE and app._status_full == SHORT_DONE
    assert app.count_var.get() == app._count_full and "123456" in app.count_var.get()


def test_status_and_counts_ellipsis_are_bidirectional(app):
    """超长消息在 800px 截断、拉宽恢复全文；进度条先缩到地板宽度，文字才截断。"""
    app._set_status(LONG_STATUS, error=False)
    app.root.update()
    assert app.status_var.get().endswith(g.ELLIPSIS) and app._status_full == LONG_STATUS, (
        app.status_var.get()[-20:])
    assert app.version_label.winfo_width() == app.version_label.winfo_reqwidth(), (
        app.version_label.winfo_width(), app.version_label.winfo_reqwidth())
    assert all(b.winfo_width() == b.winfo_reqwidth() for b in (app.start_btn, app.stop_btn))
    app.root.geometry("2000x736")
    app.root.update()
    assert app.status_var.get() == LONG_STATUS, app.status_var.get()[-20:]
    app.root.geometry("800x620")
    app.root.update()
    assert app.status_var.get().endswith(g.ELLIPSIS), app.status_var.get()[-20:]

    app._set_counts(LONG_COUNTS)
    app.root.update()
    assert app.count_var.get().endswith(g.ELLIPSIS) and app._count_full == LONG_COUNTS, (
        app.count_var.get()[-20:])
    assert app.progress.winfo_width() >= g.PROGRESS_MIN_WIDTH
    assert all(b.winfo_width() == b.winfo_reqwidth() for b in (app.start_btn, app.stop_btn))
    # 回归：进度条与按钮的 parcel 不得重叠——pack 亏空时 expand 控件收缩、后打包按钮会叠进
    # 它的残 parcel（探针实测叠影）。解法是靠 length 主动改请求，让整行请求 ≤ 行宽
    assert app.start_btn.winfo_x() >= app.progress.winfo_x() + app.progress.winfo_width() + 8, (
        app.progress.winfo_x(), app.progress.winfo_width(), app.start_btn.winfo_x())
    app.root.geometry("2000x736")
    app.root.update()
    assert app.count_var.get() == LONG_COUNTS
    assert float(app.progress["length"]) == 220, (app.count_var.get()[-20:], app.progress["length"])
    app.root.geometry("800x620")
    app.root.update()

    # 中间档（用户要的"先缩进度条"语义）：9 位数计数全文照显，进度条缩到地板与 220 之间
    app._set_counts(MID_COUNTS)
    app.root.update()
    assert app.count_var.get() == MID_COUNTS
    assert g.PROGRESS_MIN_WIDTH <= app.progress.winfo_width() < 220
    assert all(b.winfo_width() == b.winfo_reqwidth() for b in (app.start_btn, app.stop_btn))


def test_ellipsis_tooltip_arms_only_when_truncated(app):
    """截断时悬停才调度，leave 取消；气泡含全文；未截断时悬停不调度。

    直接调 handler 不合成 <Enter>：合成 Enter/Leave 事件的语义不确定，断言会空过
    （KP_Enter 同款教训）。
    """
    app._set_status(LONG_STATUS, error=False)
    app._set_counts(LONG_COUNTS)
    app.root.update()

    app._on_tip_enter(_Ev(), app.status_var, lambda: app._status_full)
    assert app._tip_after is not None
    app._on_tip_leave()
    assert app._tip_after is None and app._tip is None
    app._show_tip(100, 100, app._status_full)
    assert app._tip is not None and bool(app._tip.wm_overrideredirect())
    assert app._tip.winfo_children()[0].cget("text") == LONG_STATUS
    app._hide_tip()
    assert app._tip is None

    app._on_tip_enter(_Ev(), app.count_var, lambda: app._count_full)
    assert app._tip_after is not None
    app._on_tip_leave()
    app._show_tip(100, 100, app._count_full)
    assert app._tip is not None
    assert app._tip.winfo_children()[0].cget("text") == LONG_COUNTS
    app._hide_tip()

    app._set_status(SHORT_DONE, error=False)
    app._set_counts("尚未开始备份")
    app.root.update()
    app._on_tip_enter(_Ev(), app.status_var, lambda: app._status_full)
    app._on_tip_enter(_Ev(), app.count_var, lambda: app._count_full)
    assert app._tip_after is None and app._tip is None


def test_start_button_borrows_the_connecting_spinner_while_backing_up(app, monkeypatch):
    """备份中开始按钮把绿三角换成透明占位、显示「⠋ 正在备份…」，与连接按钮同一套帧与节奏。

    帧推进（_run_spinner_tick / _set_loading_frame）与 after 调度是分开的，所以第 i 帧长什么样
    可以直接断言，不必 sleep 等定时器。
    """
    for index in (0, 3, len(g.SPINNER_FRAMES) - 1):
        app._set_loading_frame(app.start_btn, g.RUNNING_BTN_LABEL, index)
        assert app.start_btn.cget("text") == f"{g.SPINNER_FRAMES[index]} {g.RUNNING_BTN_LABEL}"
        app._set_loading_frame(app.connect_btn, g.CONNECTING_BTN_LABEL, index)
        assert app.connect_btn.cget("text") == f"{g.SPINNER_FRAMES[index]} {g.CONNECTING_BTN_LABEL}"

    app.running = True
    app._begin_run_ui()
    text = str(app.start_btn.cget("text"))
    assert text.endswith(g.RUNNING_BTN_LABEL) and text[0] in g.SPINNER_FRAMES, text
    assert app.start_btn.cget("image") == str(app.blank_icon)  # 绿三角让位给透明占位（撤成 "" 会矮/高一截）
    assert str(app.start_btn.cget("state")) == "disabled"
    assert str(app.stop_btn.cget("state")) == "normal"

    app._end_run_controls()
    assert_start_button_idle(app)
    assert app._run_spinner_job is None
    app._apply_connect_state()  # 上面借连接按钮验了共享帧，把它的文案交回状态机

    # 自愈：有的路径直接把 running 置假、不走 _end_run_controls，帧推进函数得自己复位，
    # 否则「正在备份…」会残留在按钮上
    app.running = True
    app._begin_run_ui()
    app._stop_run_spinner()  # 停掉真实帧链，下面手动推进
    app.running = False
    app._run_spinner_tick(2)
    assert_start_button_idle(app)
    assert app._run_spinner_job is None

    # 失败路径同样要走复位（messagebox 换替身，否则会弹真对话框）
    monkeypatch.setattr(g, "messagebox", FakeBox())
    app.running = True
    app._begin_run_ui()
    app.events.put(("run_failed", "boom"))
    pump(app)
    assert_start_button_idle(app)
    assert app._run_spinner_job is None


def test_right_edges_share_one_baseline(app):
    """右边界基线：内容右边距 == 左边距，「邮箱文件夹」面板与下方区域同一条右边线。

    用户口径：左边留多少，右边就留多少；面板内的滚条要紧贴面板边框线。回归自：面板曾
    单独多留 8px 右留白、滚条自己再留 6px，于是面板边框比下方窄一块、滚条比下方滚条左 13px。
    断言全走控件之间的相对关系，不钉绝对坐标，换 DPI/主题也成立。
    """
    app.root.geometry("960x736")
    app.root.update()

    def left(widget):
        return widget.winfo_rootx()

    def right(widget):
        return widget.winfo_rootx() + widget.winfo_width()

    # 上半区容器的左边距 vs 面板的右边距：两边都得是 8px
    client_left = app.root.winfo_rootx()
    client_right = client_left + app.root.winfo_width()
    left_margin = left(app.folder_group.master) - client_left
    right_margin = client_right - right(app.folder_group)
    assert left_margin == right_margin == 8, (left_margin, right_margin)

    # 面板右边界 == 下方区域（Notebook）右边界
    assert right(app.folder_group) == right(app.notebook), (
        right(app.folder_group), right(app.notebook))

    # 面板内滚条贴住面板边框线：与面板内沿只差一个 borderwidth
    border = int(app.folder_group.cget("borderwidth"))
    assert right(app.folder_group) - right(app.folder_scroll) == border, (
        right(app.folder_group), right(app.folder_scroll), border)

    # 跨面板观感：文件夹滚条与邮件列表滚条的右边线差 ≤ 2px（实测 1px，差在 ttk 主题内边距）
    assert abs(right(app.folder_scroll) - right(app.tree_scroll)) <= 2, (
        right(app.folder_scroll), right(app.tree_scroll))


def test_folder_panel_wheel_covers_the_whole_panel(app):
    """「邮箱文件夹」面板整块都能用滚轮滚：一格 1 行（与下方邮件列表同节奏）、停在行边界。

    回归自：滚轮只有画布自己那一小条空白和面板内滚条吃得到，文件夹名字、箭头、行容器、
    工具栏、面板边框全无反应（Tk 把事件投给指针正下方的控件、不冒泡到画布）；面板内滚条
    还自带 TScrollbar 一格 4 units 的更快步长；不足一格的 delta（高分辨率鼠标/触摸板）一律
    没反应。断言走控件间的相对关系，行距现量，不钉 26 这类机器相关的像素值。

    本用例会把文件夹列表换成 32 个文件夹行（要能滚出可视区），排在文件最后，不影响前面的用例。
    """
    many = [(f"Box{i:02d}", f"Box{i:02d}.mbox", f"Box{i:02d}", f"Box{i:02d}") for i in range(30)]
    many += [("Group", "Group.mbox", "Group", "Group"),
             ("Group/Leaf", "Group.Leaf.mbox", "Group/Leaf", "Group/Leaf")]
    app.events.put(("folders_loaded", (many, V)))
    pump(app)
    app.root.geometry("960x736")
    app.root.update()

    canvas = app.folder_canvas
    frames = app.folder_inner.winfo_children()          # 顶层节点框
    pitch = frames[1].winfo_rooty() - frames[0].winfo_rooty()
    assert len(frames) > 9 and pitch > 0, (len(frames), pitch)
    notch = g.FOLDER_WHEEL_ROWS * pitch                 # 一格该滚的距离（行距现量，不钉像素值）

    def at_top():
        canvas.yview_moveto(0)
        app._wheel_accum = 0                            # 不把上一格没攒满的余量带进下一个子用例
        app.root.update()
        return canvas.canvasy(0)

    def wheel(target, delta=-g.WHEEL_DELTA):
        before = canvas.canvasy(0)
        target.event_generate("<MouseWheel>", delta=delta, x=5, y=5)
        app.root.update()
        return canvas.canvasy(0) - before

    toolbar = [w for w in app.folder_group.winfo_children() if isinstance(w, tk.Frame)][0]
    assert at_top() == 0
    for name, target in [("文件夹名字", app.folder_rows[0]["check_btn"]),
                         ("父节点箭头", app._folder_nodes[("Group",)]["arrow"]),
                         ("行容器", app.folder_inner),
                         ("画布空白条", canvas),
                         ("工具栏按钮", toolbar.winfo_children()[0]),
                         ("面板边框", app.folder_group),
                         ("面板内滚条", app.folder_scroll)]:
        at_top()
        # 面板内任何落点都是同一节奏：一格 1 行（滚条的原生 4 units 已被压掉）
        assert wheel(target) == notch, name

    # 一格滚得多远由常量定：两格一次给完就滚两倍
    at_top()
    assert wheel(app.folder_rows[0]["check_btn"], delta=-2 * g.WHEEL_DELTA) == 2 * notch

    # 按行对齐：滚完停在行边界上
    at_top()
    wheel(app.folder_rows[0]["check_btn"])
    assert canvas.canvasy(0) % pitch == 0, canvas.canvasy(0)

    # 不足一格的 delta 攒够一格才滚（高分辨率鼠标/触摸板）
    at_top()
    for _ in range(4):
        wheel(app.folder_rows[0]["check_btn"], delta=-g.WHEEL_DELTA // 4)
    assert canvas.canvasy(0) == notch

    # 面板外（下方两页签）的滚轮不该动到文件夹面板，且它们自己照常滚
    app.notebook.select(app.tree.master)                # 只有选中的页签才真的会滚
    for _ in range(20):
        app.tree.insert("", "end", values=("from", "主题", "2026-09-21", "INBOX", "1 KB"))
    app.root.update()
    for outside in (app.tree, app.log_text):
        at_top()
        assert wheel(outside) == 0
    assert app.tree.yview()[0] > 0                      # 枚举没被窗口级入口吞掉：邮件列表自己滚了

    # 空树把单位归回 0，别把上次的行距留给没有内容的树
    app._rebuild_folders([])
    app.root.update()
    assert int(canvas.cget("yscrollincrement")) == 0


def test_stopping_feedback_covers_buttons_status_line_and_hint(app):
    """「停止中」三处同时换口径：停止按钮接手转圈、开始按钮退回静止外观、状态行清空、提示改文案。

    回归自：点了停止之后开始按钮还在转圈「正在备份…」、停止按钮只是变灰且文案不变、
    第二行还写着「正在停止…」（与按钮重复）——按钮和状态行都没有「我收到了」的反应。
    现在的口径：转圈挪到停止按钮上（红方块换透明占位）、开始按钮复位成静止外观（仍禁用）、
    第二行只清文案（行与版本号留着，收尾出「已停止：…」时不跳版）、配置区提示改口径。
    """
    app.connect_state = "connected"
    app.running = True
    app._begin_run_ui()
    app._stop_run_spinner()  # 停掉真实帧链，手动驱动帧（与开始/连接按钮那套用例同法）
    app._set_counts("已备份 3 / 10")
    app._set_status("备份 INBOX（7 封）", error=False)
    counts_before, progress_before = app.count_var.get(), float(app.progress["value"])

    app._on_stop()
    assert app.stopping is True and app.stop_flag.is_set()
    text = str(app.stop_btn.cget("text"))
    assert text.endswith(g.STOPPING_BTN_LABEL) and text[0] in g.SPINNER_FRAMES, text
    assert app.stop_btn.cget("image") == str(app.blank_icon)  # 红方块让位给转圈文字
    assert str(app.stop_btn.cget("state")) == "disabled"
    assert_start_button_idle(app)                             # 开始按钮：文案与绿三角回位
    assert str(app.start_btn.cget("state")) == "disabled"     # 但仍不可点（运行还没真正结束）
    assert app.status_var.get() == "" and app._status_full == ""           # 第二行清空
    assert app._msg_bar.winfo_manager() == "pack"             # 行不收回
    assert app.version_label.winfo_manager() == "pack"        # 版本号留在原处
    assert app.conn_hint.cget("text") == g.STOPPING_HINT_TEXT
    # 停止这个动作本身不动第一行计数与进度条（工作线程在途的那封照常落盘、计数据实更新，
    # 那是有意义的真实进展，这里不冻结它）
    assert app.count_var.get() == counts_before, app.count_var.get()
    assert float(app.progress["value"]) == progress_before

    # 帧推进落在停止按钮上，开始按钮不再被写文案（全行只剩一个转圈）——走真函数，别抄它的映射
    for index in (2, len(g.SPINNER_FRAMES) - 1):
        app._run_spinner_tick(index)
        assert app.stop_btn.cget("text") == f"{g.SPINNER_FRAMES[index]} {g.STOPPING_BTN_LABEL}"
        assert_start_button_idle(app)

    # 运行真正结束：按钮、状态行、提示全部回位，不残留「正在停止…」
    app.events.put(("run_done", 5, 100, 9.0, True, 20))
    pump(app)
    assert app.running is False and app.stopping is False
    assert app.status_var.get() == "已停止：本次备份 5 封", app.status_var.get()
    assert_stop_button_idle(app)
    assert_start_button_idle(app)
    assert app.conn_hint.cget("text") == "", app.conn_hint.cget("text")

    # 不在运行时按停止（那时按钮本就禁用）不产生任何变化
    app._on_stop()
    assert app.stopping is False
    assert_stop_button_idle(app)


def test_stopping_then_failing_restores_the_controls(app, monkeypatch):
    """停止中撞上运行失败：除弹窗外按钮与状态行也要完整复位（messagebox 换替身，别弹真框）。"""
    monkeypatch.setattr(g, "messagebox", FakeBox())
    app.connect_state = "connected"
    app.running = True
    app._begin_run_ui()
    app._on_stop()
    assert app.stopping is True

    app.events.put(("run_failed", "boom"))
    pump(app)
    assert app.running is False and app.stopping is False
    assert app._run_spinner_job is None
    assert_stop_button_idle(app)
    assert_start_button_idle(app)
    assert app.status_var.get() == "备份失败，详见运行日志", app.status_var.get()


def test_loading_buttons_keep_their_height(app):
    """加载态不许改按钮高度：开始/停止/连接三颗按钮在加载与非加载之间高度一致。

    回归自：用户看到运行中的开始按钮比旁边的停止按钮高一块。探针量出的根因不是盲文帧，
    而是加载态把 image 撤成 ""——Tk 的按钮请求高度取决于「有没有 image」（同一按钮实测
    有图 28px / 无图 30px，与文案是中文还是盲文帧无关）。修法是加载态换一枚同尺寸的
    全透明占位图（blank_icon），图标槽一直占着、只换里面的像素。
    断言只比同一颗按钮的前后高度，不钉绝对像素值（异 DPI/主题下数字会变）。
    """
    app.root.geometry("960x736")
    app.root.update()
    idle = (app.start_btn.winfo_reqheight(), app.stop_btn.winfo_reqheight())
    assert idle[0] == idle[1], idle                    # 同一行两颗按钮本就该等高
    connect_idle = app.connect_btn.winfo_reqheight()

    # 连接按钮：加载态只换文案（它本来就没有图标），高度不变
    app._set_loading_frame(app.connect_btn, g.CONNECTING_BTN_LABEL, 0)
    app.root.update()
    assert app.connect_btn.winfo_reqheight() == connect_idle
    app._apply_connect_state()                         # 文案交回连接状态机

    # 运行中：开始按钮的图标槽换成透明占位，高度与静止态一致，同行的停止按钮不被顶高
    app.running = True
    app._begin_run_ui()
    app._stop_run_spinner()                            # 停掉真实帧链，免得它把文案改回别的帧
    app.root.update()
    assert app.start_btn.cget("image") == str(app.blank_icon), app.start_btn.cget("image")
    assert (app.start_btn.winfo_reqheight(), app.stop_btn.winfo_reqheight()) == idle, (
        app.start_btn.winfo_reqheight(), app.stop_btn.winfo_reqheight())

    # 停止中：转圈换到停止按钮上，两颗按钮的高度仍然一致
    app._on_stop()
    app.root.update()
    assert app.stop_btn.cget("image") == str(app.blank_icon)
    assert (app.start_btn.winfo_reqheight(), app.stop_btn.winfo_reqheight()) == idle, (
        app.start_btn.winfo_reqheight(), app.stop_btn.winfo_reqheight())

    app._end_run_controls()
    assert_stop_button_idle(app)
    assert_start_button_idle(app)
