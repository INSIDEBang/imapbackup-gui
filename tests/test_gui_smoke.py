"""GUI 无头冒烟：实例化真实的 BackupApp，喂合成队列事件，断言 UI 状态。不联网。

整套用例共享同一个 module 级 app 实例，**pytest 按定义顺序执行**，所以前一节留下的
界面状态就是后一节的前置条件（账户 → 连接 → 树 → stale → 工具栏 → 级联 → … → 状态条 → 气泡）。
这个顺序依赖是有意保留的，别重排测试。

多账户改造后的事件都带 account_id：`folders_loaded` / `totals` / `message` / `account_done`
一律针对某个账户，批量结束走 `batch_done`。`load_folders` 是本文件唯一的“连接成功”入口。

标了 @pytest.mark.gui：需要真实 Tk 显示与 ttk 主题，几何断言（winfo_width、
winfo_rootx 等）在异 DPI / 异主题的机器上可能不成立。无显示环境的 runner 用
`pytest -m "not gui"` 跳过。
"""
import json
import logging
from datetime import datetime
from types import SimpleNamespace

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
# 三次重复：多账户改造后右栏少了一条账户列表的宽度，四次重复在 1924px 的屏幕上已经放不下了
LONG_STATUS = "扫描 12/45：Deleted Messages/2024 年第四季度归档副本(2)/再一个很长的名字" * 3
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


@pytest.fixture(scope="module", autouse=True)
def _redirect_column_config(tmp_path_factory):
    patch = MonkeyPatch()
    patch.setattr(g, "COLUMN_CONFIG_PATH", tmp_path_factory.mktemp("columns") / "treeview-columns.json")
    yield
    patch.undo()


DPAPI_OK = g._dpapi_encrypt("probe") is not None


@pytest.fixture(scope="module")
def app():
    """The real app, created once for the whole module.

    见模块 docstring：用例顺序是有意义的，重排会破坏前置状态。
    启动后先补一个账户：多账户改造后所有界面都挂在「当前账户」上。
    """
    root = logging.getLogger()
    instance = g.BackupApp()
    account = g.new_account()
    account.update({"name": "主账户", "user": V["user"], "server": V["server"], "ssl": True, "port": "993"})
    if DPAPI_OK:
        account["password_dpapi"] = g._dpapi_encrypt(V["password"])
    instance.accounts.append(account)
    instance._ui_settings["accounts"] = instance.accounts
    instance._selected_account_id = account["id"]
    instance._ui_settings["selected_account"] = account["id"]
    instance._refresh_account_list()
    instance._refresh_edit_list(account["id"])
    instance._render_account_detail()
    try:
        yield instance
    finally:
        instance.root.geometry("960x736")
        # 真实例带的是真托盘（Windows 上）：拆窗口前先收掉图标与消息窗口，别留孤儿
        if instance._tray is not None:
            instance._tray.close()
        instance.root.destroy()
        # _setup_logging 会把 root.handlers 换成 QueueLogHandler + 文件 handler；
        # 文件 handler 指向已销毁的临时目录，清掉免得留着
        for handler in [h for h in root.handlers if isinstance(h, logging.FileHandler)]:
            handler.close()
            root.removeHandler(handler)


def current_account(app):
    """当前可见账户记录。"""
    return app._find_account(app._selected_account_id)


def load_folders(app, names, account_id=None):
    """模拟连接成功：为（默认可见的）账户投一条 folders_loaded 事件并泵一轮。"""
    account_id = account_id or app._selected_account_id
    account = app._find_account(account_id)
    app.events.put(("folders_loaded", (account_id, names, app._account_snapshot(account))))
    pump(app)


def clear_folders(app):
    """把可见账户的文件夹行清空并重画（等价于服务器一个文件夹都没返回）。"""
    state = app._visible_state()
    state["folder_rows"] = []
    app._render_folder_tree(state)


def pump(instance, n=3):
    for _ in range(n):
        instance._pump()
        instance.root.update_idletasks()


def click(instance, widget, x=None, y=None):
    """点一下；给了坐标就点在该点（Treeview 的行与空白靠坐标区分）。"""
    extra = {} if x is None else {"x": x, "y": y}
    widget.event_generate("<Button-1>", **extra)
    instance.root.update()


def press_enter(instance, widget):
    # Tk 只在控件持有键盘焦点时才把合成的 KeyPress 派发给绑定；测试窗口不是前台窗口，须先抢焦点
    widget.focus_force()
    widget.event_generate("<Return>")
    instance.root.update()


def stub_popup(monkeypatch, menu):
    """替掉菜单的 tk_popup（真菜单会 grab 住键盘，后续合成事件全被它吃掉），记录弹出坐标。"""
    popups = []
    monkeypatch.setattr(menu, "tk_popup", lambda x, y: popups.append((x, y)))
    return popups


def row_by(instance, foldername):
    return next(r for r in instance.folder_rows if r["foldername"] == foldername)


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
    # 两个页签各有一层与「邮箱文件夹」面板同款的外包边框；空态提示挂在框内
    # （控件同一性在 tkinter 里不稳，按路径比较，理由同滚轮入口）
    assert str(app.list_frame.master) == str(app.list_tab)
    assert str(app.log_frame.master) == str(app.log_tab)
    assert str(app.list_ph.master) == str(app.list_frame)


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
    """folders_loaded 事件走完真实流程：账户先被读到，再投队列；勾选按账户记录默认 INBOX。"""
    account = current_account(app)
    account.update({"server": V["server"], "user": V["user"], "port": "993", "ssl": True})
    load_folders(app, NAMES)

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
    """账户在设置页被改动后（快照 ≠ 现值）连接即过期；改回原值即恢复。"""
    account = current_account(app)
    account["server"] = "imap.qq.com"
    app._refresh_stale(account["id"])
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

    account["server"] = V["server"]
    app._refresh_stale(account["id"])
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
    load_folders(app, SINGLE)
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
    load_folders(app, NAMES)
    assert row_by(app, OTHER)["var"].get() is True and row_by(app, "INBOX")["var"].get() is True
    assert row_by(app, "Archive/2024")["var"].get() is False
    assert not is_open(app, PARENT) and is_open(app, SYNTH)


def test_totals_message_and_done_events_fill_the_ui(app):
    aid = app._selected_account_id
    app.events.put(("totals", aid, 12345, 23))
    pump(app)
    assert "12345" in app.count_var.get() and "23" in app.count_var.get(), app.count_var.get()
    assert float(app.progress["maximum"]) == 23.0

    app.events.put(("message", aid, _message()))
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
    aid = app._selected_account_id
    app._clear_message_rows()
    app.events.put(("message", aid, _message()))
    pump(app)

    assert str(app.tree.tag_configure(g.ZEBRA_TAG, "background")).lower() == g.ZEBRA_BG.lower(), (
        app.tree.tag_configure(g.ZEBRA_TAG, "background"))
    first = app.tree.get_children()[0]
    assert row_tags(app, first) == [], row_tags(app, first)

    for i in range(2, 7):
        app.events.put(("message", aid, _message(index=i, from_="b@b.com", subject=f"第 {i} 封",
                                                 size=100, msg_id=f"<{i}@y>")))
    pump(app)
    assert [row_tags(app, iid) for iid in app.tree.get_children()] == (
        [[], [g.ZEBRA_TAG], [], [g.ZEBRA_TAG], [], [g.ZEBRA_TAG]])

    app._clear_message_rows()
    assert not app.tree.get_children() and app._row_index == 0
    # 新一轮开始前提示重新挂出（待备份为 0 时整轮都不会有行，提示必须留着）
    assert app.list_ph.winfo_manager() == "grid", app.list_ph.winfo_manager()

    app.events.put(("message", aid, _message(subject="重跑首轮", msg_id="<z@y>")))
    pump(app)
    first = app.tree.get_children()[0]
    assert row_tags(app, first) == [], row_tags(app, first)
    assert app.tree.item(first, "values")[1] == "重跑首轮"


def test_run_done_summaries(app):
    """单账户跑完 + 批量结束：完成 / 已是最新 / 已停止三种状态文案。"""
    aid = app._selected_account_id
    app.events.put(("log", "2026-09-11 10:00:00 | INFO    | gui        | _batch_worker:1 | 测试日志行"))
    app.events.put(("account_done", aid, 1, 12322, 65.0, False, 23))
    app.events.put(("batch_done", False, []))
    pump(app)
    assert "测试日志行" in app.log_text.get("1.0", "end")
    assert "完成：本次备份 1 封" in app.status_var.get() and "用时 1:05" in app.status_var.get(), (
        app.status_var.get())
    assert str(app.start_btn.cget("state")) == "normal"

    app.events.put(("totals", aid, 100, 0))
    app.events.put(("account_done", aid, 0, 100, 3.0, False, 0))
    app.events.put(("batch_done", False, []))
    pump(app)
    assert app.status_var.get() == "已是最新，无待备份邮件", app.status_var.get()

    app.events.put(("account_done", aid, 5, 100, 9.0, True, 20))
    app.events.put(("batch_done", True, []))
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
    lockable = [w for w, _ in app._config_widgets if isinstance(w, (tk.Button, tk.Checkbutton))]
    assert all(str(w.cget("state")) == "disabled" for w in lockable), len(app._config_widgets)
    assert bool(app._folder_clickables)
    assert all(str(w.cget("foreground")) == "gray" and str(w.cget("cursor")) == "arrow"
               for w, _ in app._folder_clickables), len(app._folder_clickables)
    assert app.conn_hint.cget("text") == "备份运行中，文件夹选择与输出格式不可修改，请先停止"
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
               if isinstance(w, (tk.Button, tk.Checkbutton)) and w is not app.connect_btn)
    assert app.conn_hint.cget("text") == "" and app.conn_hint.winfo_manager() == ""
    # 连接仍有效时，结束运行后连接按钮保持"已成功连接"禁用态（原设计：避免重复登录）
    assert str(app.connect_btn.cget("state")) == "disabled"

    # 连接已过期时，结束运行要把按钮重新放开并给出过期提示
    account = current_account(app)
    account["server"] = "imap.qq.com"
    app._refresh_stale(account["id"])
    pump(app)
    app._end_run_controls()
    assert str(app.connect_btn.cget("state")) == "normal"
    # 刚解锁时提示行是灰字，过期提示必须覆盖回红字（不能残留上一次的灰）
    assert app.conn_hint.cget("text") == "连接信息已更改，请重新连接并加载文件夹"
    assert str(app.conn_hint.cget("foreground")) == g.HINT_RED
    assert app.conn_hint.winfo_manager() == "grid"
    account["server"] = V["server"]
    app._refresh_stale(account["id"])
    pump(app)


def test_enter_triggers_connect_with_the_same_button_gating(app, monkeypatch):
    """设置页账户表单的输入框绑定 Enter = 保存；连接按钮的门控挡住重复连接。"""
    box = FakeBox()
    monkeypatch.setattr(g, "messagebox", box)

    # 只查绑定注册，不合成 KP_Enter：合成小键盘事件的语义不确定，断言会空过
    assert all(str(w.bind("<Return>")) != "" and str(w.bind("<KP_Enter>")) != ""
               for w in app._account_form_entries)

    app._connect_worker = lambda *_args: None   # 不起真实网络线程
    account = current_account(app)
    if not account.get("password_dpapi"):
        pytest.skip("非 Windows 没有可用的加密密码")
    app._states[account["id"]]["connect_state"] = "idle"   # 回到可连接状态，按钮才会放开
    app._apply_connect_state()
    app._on_connect()
    assert app.connect_state == "connecting"
    assert "正在连接" in str(app.connect_btn.cget("text")), (
        app.connect_state, app.connect_btn.cget("text"))

    # 连接中：按钮被禁用——门控必须挡住，否则起第二条连接线程
    app._on_connect()
    assert app.connect_state == "connecting"
    assert str(app.connect_btn.cget("state")) == "disabled"
    assert app.conn_hint.winfo_manager() == "", (
        app.connect_state, app.connect_btn.cget("state"), app.conn_hint.cget("text"))

    # 备份运行中：配置区被 _set_config_enabled 置灰，再点同样静默
    app._states[account["id"]]["connect_state"] = "connected"
    app.running = True
    app._begin_run_ui()
    app._on_connect()
    assert app.connect_state == "connected"
    assert str(app.connect_btn.cget("state")) == "disabled", (app.connect_state,)
    app.running = False
    app._end_run_controls()

    # 账户缺密码时只弹校验提示、不发起连接；清空密码让连接过期是既有行为
    cipher = account.pop("password_dpapi", None)
    app._refresh_stale(account["id"])   # 真实路径：设置页保存后刷新过期态
    box.warnings.clear()
    app._on_connect()
    assert app.connect_state == "stale"
    assert ("信息不完整", "该账户还没有密码，请在设置页的「账户」里填写并保存。") in box.warnings, box.warnings
    if cipher:
        account["password_dpapi"] = cipher   # 原密文放回：快照口径一致，连接恢复有效
    app._refresh_stale(account["id"])
    pump(app)
    assert app.connect_state == "connected", app.connect_state

    # 一个文件夹都没解析出来时提示必须重新挂回，否则那块区域是一片纯白
    clear_folders(app)
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
    app.root.geometry("960x736")   # 默认窗口宽度：右栏比最小宽度宽出账户列表那一条
    app.root.update()
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
    app.events.put(("batch_done", False, [("主账户", "boom")]))
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
    # 左侧是账户列表栏（固定宽），右栏的内容边距由右栏自己带
    pane = app.account_pane
    detail = app.folder_group.master.master          # 配置区外层 → 右栏
    assert left(pane) - client_left == 8, left(pane) - client_left
    assert pane.winfo_width() == g.ACCOUNT_LIST_WIDTH, pane.winfo_width()
    assert left(detail) == right(pane), (left(detail), right(pane))
    left_margin = left(app.folder_group.master) - left(detail)
    right_margin = client_right - right(app.folder_group)
    assert left_margin == right_margin == 8, (left_margin, right_margin)
    # 账户栏与右栏之间那条间隔 = 左右两栏各自内容之间的间隔（用户口径：与「账户信息」和
    # 「邮箱文件夹」之间那条一样宽）。回归自：账户栏曾自带 8px 外边距，叠上右栏的 8px 内边距
    # 成了 16px，比别处宽一倍。
    col_gap = left(app.folder_group) - right(app.conn_group)
    assert left(app.conn_group) - right(pane) == col_gap == 8, (
        left(app.conn_group) - right(pane), col_gap)

    # 面板内滚条贴住面板边框线：与面板内沿只差一个 borderwidth
    border = int(app.folder_group.cget("borderwidth"))
    assert right(app.folder_group) - right(app.folder_scroll) == border, (
        right(app.folder_group), right(app.folder_scroll), border)

    # 两个页签的包裹层同款：与面板同值的边框宽度；左缘落在配置区的 8px 基线上，右缘与上方面板
    # 线**精确**重合（notebook 客户区的 1px/3px 内缩已由容器内边距补掉，探针见 issues/15）；
    # 滚条贴包裹边框内侧。
    for tab, frame, scroll in ((app.list_tab, app.list_frame, app.tree_scroll),
                               (app.log_tab, app.log_frame, app.log_scroll)):
        assert int(frame.cget("borderwidth")) == border, (frame, frame.cget("borderwidth"))
        app.notebook.select(tab)  # 非当前页签的控件未映射，量不到几何
        app.root.update()
        assert left(frame) == left(app.folder_group.master), (
            left(frame), left(app.folder_group.master))
        assert right(frame) == right(app.folder_group), (right(frame), right(app.folder_group))
        assert right(frame) - right(scroll) == border, (
            frame, right(frame), right(scroll), border)

    # 内外边框之间的留白处处 4px（用户口径：按最短的下边距统一）。日志板保留 Tk 自带的下沉
    # 边框——压平后白底与灰底直接相接很违和（用户复核，issues/15）；右侧贴住滚条、不留缝。
    assert int(app.log_text.cget("borderwidth")) >= 1, app.log_text.cget("borderwidth")
    assert str(app.log_text.cget("relief")) == "sunken", app.log_text.cget("relief")
    lf_left, lf_top = left(app.log_frame) + border, app.log_frame.winfo_rooty() + border
    lf_bottom = app.log_frame.winfo_rooty() + app.log_frame.winfo_height() - border
    assert left(app.log_text) - lf_left == 4, (left(app.log_text), lf_left)
    assert lf_bottom - (app.log_text.winfo_rooty() + app.log_text.winfo_height()) == 4, (
        lf_bottom, app.log_text.winfo_rooty() + app.log_text.winfo_height())
    assert right(app.log_text) == left(app.log_scroll), (right(app.log_text), left(app.log_scroll))
    # 滚条与板面同高（都在工具栏下面那一行）
    assert (app.log_scroll.winfo_rooty(), app.log_scroll.winfo_height()) == (
        app.log_text.winfo_rooty(), app.log_text.winfo_height()), (
        app.log_scroll.winfo_rooty(), app.log_scroll.winfo_height(),
        app.log_text.winfo_rooty(), app.log_text.winfo_height())
    log_toolbar = next(w for w in app.log_frame.winfo_children() if isinstance(w, tk.Frame))
    assert left(log_toolbar) == left(app.log_text), (left(log_toolbar), left(app.log_text))
    assert left(log_toolbar) - lf_left == 4, (left(log_toolbar), lf_left)
    assert log_toolbar.winfo_rooty() - lf_top == 4, (log_toolbar.winfo_rooty(), lf_top)

    app.notebook.select(app.list_tab)  # 复位：后续用例默认在列表页签上
    app.root.update()

    # 邮件列表：外框与面板同款（groove）；白色表格自己带凹陷边（`tree_wrap`，白底 sunken，
    # 与日志板同一配方），整块贴着外框内沿、不留灰边距——凹陷边本身给滚条左缘那根 1px
    # 白色反光条做对比（用户口径，issues/15）。树自己的 vista field 边框压平。
    assert str(app.list_frame.cget("relief")) == "groove", app.list_frame.cget("relief")
    assert (app.tree_wrap.cget("relief"), int(app.tree_wrap.cget("borderwidth")),
            app.tree_wrap.cget("background")) == (
        app.log_text.cget("relief"), int(app.log_text.cget("borderwidth")),
        app.log_text.cget("background")), (
        app.tree_wrap.cget("relief"), app.tree_wrap.cget("borderwidth"),
        app.tree_wrap.cget("background"))
    li_left, li_top = left(app.list_frame) + border, app.list_frame.winfo_rooty() + border
    li_bottom = app.list_frame.winfo_rooty() + app.list_frame.winfo_height() - border
    li_right = left(app.list_frame) + app.list_frame.winfo_width() - border
    assert (left(app.tree_wrap), app.tree_wrap.winfo_rooty()) == (li_left, li_top), (
        left(app.tree_wrap), app.tree_wrap.winfo_rooty(), li_left, li_top)
    assert app.tree_wrap.winfo_rooty() + app.tree_wrap.winfo_height() == li_bottom, (
        app.tree_wrap.winfo_rooty() + app.tree_wrap.winfo_height(), li_bottom)
    # 树用 place 往左上各溢出 1px、右下各多铺 1px：白板左/上/下三条凹陷线被树盖住（与外框合成
    # 一条线），右边留出白板自己那条 230 高光，正对滚条左缘的反光条（用户口径，issues/15）。
    assert (left(app.tree), app.tree.winfo_rooty()) <= (li_left, li_top), (
        left(app.tree), app.tree.winfo_rooty(), li_left, li_top)
    assert app.tree.winfo_rooty() + app.tree.winfo_height() >= li_bottom, (
        app.tree.winfo_rooty() + app.tree.winfo_height(), li_bottom)
    # 右边留 1px 不盖：那就是白板自己那条 230 高光（盖多了会把滚条的对比线也吃掉）
    assert right(app.tree) == right(app.tree_wrap) - 1, (right(app.tree), right(app.tree_wrap))
    assert (right(app.tree_wrap), right(app.tree_scroll)) == (left(app.tree_scroll), li_right), (
        right(app.tree_wrap), left(app.tree_scroll), right(app.tree_scroll), li_right)
    assert (app.tree_scroll.winfo_rooty(), app.tree_scroll.winfo_height()) == (
        li_top, li_bottom - li_top), (app.tree_scroll.winfo_rooty(), app.tree_scroll.winfo_height())
    assert str(app.tree.cget("style")) == "Mail.Treeview", app.tree.cget("style")
    mail_layout = str(g.ttk.Style().layout("Mail.Treeview"))
    assert "treearea" in mail_layout and "field" not in mail_layout, mail_layout
    assert "Treeview.field" in str(g.ttk.Style().layout("Treeview")), "只派生，不该动全局布局"

    # 三条遮盖窄条正好盖住 notebook 客户区的左 1px / 右 3px / 下 3px 内缩带（尺寸 = 实测内缩）。
    # 底边那条的淡线压在包裹边框外侧，底边会多出一道「突出」的线，必须一并盖掉。将来换主题或
    # DPI 让内缩变了，这里会炸出来，提醒同步改窄条尺寸与容器内边距。
    insets = (app.list_tab.winfo_rootx() - app.notebook.winfo_rootx(),
              right(app.notebook) - right(app.list_tab),
              (app.notebook.winfo_rooty() + app.notebook.winfo_height())
              - (app.list_tab.winfo_rooty() + app.list_tab.winfo_height()))
    assert (app.tab_edge_left.winfo_width(), app.tab_edge_right.winfo_width(),
            app.tab_edge_bottom.winfo_height()) == insets, insets

    # 设置页：两栏共用同一条 8px 左右基线（左栏贴左边线、右栏贴右边线，栏间 8px），
    # 左分类栏固定宽，四边留白等宽（底边与上/左/右同为 8px）。量完切回备份页，
    # 后面的用例继续在备份页上跑。
    app._show_page(g.PAGE_SETTINGS)
    app.root.update()
    nav_frame = app.settings_nav.master
    detail_frame = app._settings_sections[g.SETTINGS_WINDOW_SECTION]
    assert left(nav_frame) - client_left == 8, (left(nav_frame), client_left)
    assert right(detail_frame) == right(app.folder_group), (right(detail_frame), right(app.folder_group))
    assert nav_frame.winfo_width() == g.SETTINGS_SECTION_LIST_WIDTH, nav_frame.winfo_width()
    assert left(detail_frame) - right(nav_frame) == 8, (left(detail_frame), right(nav_frame))
    client_bottom = app.root.winfo_rooty() + app.root.winfo_height()
    panel_bottom = detail_frame.winfo_rooty() + detail_frame.winfo_height()
    assert client_bottom - panel_bottom == 8, (client_bottom, panel_bottom)
    assert detail_frame.winfo_rooty() - app.root.winfo_rooty() == 8, detail_frame.winfo_rooty()
    app._show_page(g.PAGE_BACKUP)
    app.root.update()


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
    load_folders(app, many)
    app.root.geometry("960x736")
    app.root.update()
    # 行距同步挂在 folder_inner 的 <Configure> 上；重建后它偶尔还没跑过，画布的单位会停在旧值上，
    # 第一格滚轮就滚不到位（探针实测）。这里显式同步一次，后面的断言才是确定性的。
    app._sync_folder_row_increment()
    app.root.update()

    canvas = app.folder_canvas
    frames = app.folder_inner.winfo_children()          # 顶层节点框
    pitch = frames[1].winfo_rooty() - frames[0].winfo_rooty()
    assert len(frames) > 9 and pitch > 0, (len(frames), pitch)
    assert int(canvas.cget("yscrollincrement")) == pitch
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

    toolbar = next(w for w in app.folder_group.winfo_children() if isinstance(w, tk.Frame))
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
    app.notebook.select(app.list_tab)                   # 只有选中的页签才真的会滚
    for _ in range(20):
        app.tree.insert("", "end", values=("from", "主题", "2026-09-21", "INBOX", "1 KB"))
    app.root.update()
    for outside in (app.tree, app.log_text):
        at_top()
        assert wheel(outside) == 0
    assert app.tree.yview()[0] > 0                      # 枚举没被窗口级入口吞掉：邮件列表自己滚了

    # 空树把单位归回 0，别把上次的行距留给没有内容的树
    clear_folders(app)
    app.root.update()
    assert int(canvas.cget("yscrollincrement")) == 0


def test_stopping_feedback_covers_buttons_status_line_and_hint(app):
    """「停止中」三处同时换口径：停止按钮接手转圈、开始按钮退回静止外观、状态行清空、提示改文案。

    回归自：点了停止之后开始按钮还在转圈「正在备份…」、停止按钮只是变灰且文案不变、
    第二行还写着「正在停止…」（与按钮重复）——按钮和状态行都没有「我收到了」的反应。
    现在的口径：转圈挪到停止按钮上（红方块换透明占位）、开始按钮复位成静止外观（仍禁用）、
    第二行只清文案（行与版本号留着，收尾出「已停止：…」时不跳版）、配置区提示改口径。
    """
    app._states[app._selected_account_id]["connect_state"] = "connected"
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
    aid = app._selected_account_id
    app.events.put(("account_done", aid, 5, 100, 9.0, True, 20))
    app.events.put(("batch_done", True, []))
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
    app._states[app._selected_account_id]["connect_state"] = "connected"
    app.running = True
    app._begin_run_ui()
    app._on_stop()
    assert app.stopping is True

    app.events.put(("account_failed", app._selected_account_id, "boom"))
    app.events.put(("batch_done", False, [("主账户", "boom")]))
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
    有图 28px / 无图 30px，与文案是中文还是盲文帧无关）。修法是加载态换一枚全透明占位图
    （blank_icon）：照旧占着「有 image」这个高度前提，宽度只留 1px——转圈符贴近左边框、
    左右留白与静止态一样对称（回归自：占位按整图标宽时左边一大块空白、左松右紧）。
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
    # 占位只借高度、不占宽：1px 宽 × 图标高，转圈符才贴近左边框
    assert (app.blank_icon.width(), app.blank_icon.height()) == (1, g._icon_size()), (
        app.blank_icon.width(), app.blank_icon.height())
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


def test_row_selection_clears_on_click_outside(app):
    """选中高亮只属于「刚点过的那一行」：点非行区域即清空；换选、Ctrl 多选与列表滚条是例外。

    回归自：点行选中（蓝底）没问题，但点别的地方清不掉。这个用例排在全文件最后，按模块
    docstring 的约定不打断前面那条状态链。
    """
    app._clear_message_rows()
    for i in range(1, 4):
        app.events.put(("message", app._selected_account_id,
                        _message(index=i, from_="b@b.com", subject=f"第 {i} 封",
                                 size=100, msg_id=f"<{i}@y>")))
    pump(app)
    ids = app.tree.get_children()
    assert len(ids) == 3, len(ids)

    def row_center(iid):
        x, y, w, h = app.tree.bbox(iid)
        return x + w // 2, y + h // 2

    # 点另一行：换选是 Treeview 的原生行为，本入口不许插手
    app.tree.selection_set(ids[0])
    x, y = row_center(ids[1])
    assert app.tree.identify_row(y) == ids[1], (app.tree.identify_row(y), ids[1])
    click(app, app.tree, x, y)
    assert app.tree.selection() == (ids[1],), app.tree.selection()

    # Ctrl+点第三行：多选同样是原生行为，本入口不许把它压回单选
    click_x, click_y = row_center(ids[2])
    app.tree.event_generate("<Control-Button-1>", x=click_x, y=click_y)
    app.root.update()
    assert set(app.tree.selection()) == {ids[1], ids[2]}, app.tree.selection()

    # 点最后一行下方的空白：清空
    bx, by, _bw, bh = app.tree.bbox(ids[-1])
    blank_y = by + bh + 4
    assert app.tree.identify_row(blank_y) == "", app.tree.identify_row(blank_y)
    click(app, app.tree, bx, blank_y)
    assert app.tree.selection() == (), app.tree.selection()

    # 点列标题：列标题当前没有任何功能，也算「非行区域」
    head_y = 2
    assert app.tree.identify("region", 30, head_y) == "heading", app.tree.identify("region", 30, head_y)
    app.tree.selection_set(ids[0])
    click(app, app.tree, 30, head_y)
    assert app.tree.selection() == (), app.tree.selection()

    # 点列表外的控件（账户信息卡、文件夹面板空白与其滚条）：同样清空
    for target in (app.card_root_label, app.folder_canvas, app.folder_scroll):
        app.tree.selection_set(ids[0])
        click(app, target)
        assert app.tree.selection() == (), (target, app.tree.selection())

    # 例外一：邮件列表自己的滚条——点击多半是在滚动查看选中行
    app.tree.selection_set(ids[0])
    app.tree_scroll.event_generate("<Button-1>")
    app.tree_scroll.event_generate("<ButtonRelease-1>")
    app.root.update()
    assert app.tree.selection() == (ids[0],), app.tree.selection()

    # 例外二：滚轮滚动不是点击，同样不清
    app.tree.event_generate("<MouseWheel>", delta=-g.WHEEL_DELTA, x=5, y=5)
    app.root.update()
    assert app.tree.selection() == (ids[0],), app.tree.selection()

    # 清过之后点行照常选中：清高亮不是关掉交互
    click(app, app.tree, *row_center(ids[2]))
    assert app.tree.selection() == (ids[2],), app.tree.selection()
    app.tree.selection_set()  # 收尾：别把选中态留给后面的用例


def test_mail_column_resize_only_redistributes_adjacent_columns(app, monkeypatch):
    """四条可拖分隔线只改相邻列，拖动时冻结 subject stretch，释放后恢复。"""
    monkeypatch.setattr(app.tree, "identify_region", lambda _x, _y: "separator")
    for separator in range(1, 5):
        before = {name: int(app.tree.column(name, "width")) for name in app.tree["columns"]}
        monkeypatch.setattr(app.tree, "identify_column", lambda _x, n=separator: f"#{n}")
        event = SimpleNamespace(x=100, y=2)
        assert app._on_tree_resize_start(event) == "break"
        assert int(app.tree.column("subject", "stretch")) == 0
        assert app._on_tree_resize_motion(SimpleNamespace(x=110)) == "break"
        after = {name: int(app.tree.column(name, "width")) for name in app.tree["columns"]}
        left, right = app.tree["columns"][separator - 1:separator + 1]
        assert after[left] != before[left]
        assert after[right] != before[right]
        for name in app.tree["columns"]:
            if name not in (left, right):
                assert after[name] == before[name], (separator, name)
        app._on_tree_resize_end(event)
        assert int(app.tree.column("subject", "stretch")) == 1


def test_mail_size_column_stays_inside_tree_boundary(app, monkeypatch):
    """大小列不能被拖到邮件表格右侧滚条之外，且仍尊重最小宽度。"""
    monkeypatch.setattr(app.tree, "identify_region", lambda _x, _y: "separator")
    monkeypatch.setattr(app.tree, "identify_column", lambda _x: "#4")
    widths = [int(app.tree.column(name, "width")) for name in app.tree["columns"]]
    boundary = sum(widths) + 5
    monkeypatch.setattr(app.tree, "winfo_width", lambda: boundary)
    event = SimpleNamespace(x=100, y=2)
    app._on_tree_resize_start(event)
    app._on_tree_resize_motion(SimpleNamespace(x=0))
    final = sum(int(app.tree.column(name, "width")) for name in app.tree["columns"])
    assert final <= boundary
    assert int(app.tree.column("size", "width")) >= int(app.tree.column("size", "minwidth"))
    app._on_tree_resize_end(event)


def test_mail_size_column_can_grow_when_dragging_left_boundary_left(app, monkeypatch):
    """大小列左缘向左拖时，大小变宽且邮箱文件夹等量变窄。"""
    monkeypatch.setattr(app.tree, "identify_region", lambda _x, _y: "separator")
    monkeypatch.setattr(app.tree, "identify_column", lambda _x: "#4")
    widths = {name: int(app.tree.column(name, "width")) for name in app.tree["columns"]}
    monkeypatch.setattr(app.tree, "winfo_width", lambda: sum(widths.values()))
    event = SimpleNamespace(x=100, y=2)
    assert app._on_tree_resize_start(event) == "break"
    assert app._on_tree_resize_motion(SimpleNamespace(x=90)) == "break"
    assert int(app.tree.column("size", "width")) == widths["size"] + 10
    assert int(app.tree.column("folder", "width")) == widths["folder"] - 10
    app._on_tree_resize_end(event)


def test_mail_size_right_boundary_is_not_draggable(app, monkeypatch):
    """大小列右缘不可拖，但 folder|size 左缘仍可进入拖拽。"""
    monkeypatch.setattr(app.tree, "identify_region", lambda _x, _y: "separator")
    monkeypatch.setattr(app.tree, "identify_column", lambda _x: "#5")
    assert app._on_tree_resize_start(SimpleNamespace(x=100, y=2)) == "break"


def test_mail_column_resize_uses_treeview_native_minwidth(app, monkeypatch):
    """拖拽下限读取 Treeview 的原生 minwidth，而不是 GUI 常量。"""
    native_minwidth = int(app.tree.column("from", "minwidth"))
    app.tree.column("from", minwidth=native_minwidth)
    monkeypatch.setattr(app.tree, "identify_region", lambda _x, _y: "separator")
    monkeypatch.setattr(app.tree, "identify_column", lambda _x: "#1")
    event = SimpleNamespace(x=100, y=2)
    assert app._on_tree_resize_start(event) == "break"
    app._on_tree_resize_motion(SimpleNamespace(x=-100))
    assert int(app.tree.column("from", "width")) == native_minwidth
    app._on_tree_resize_end(event)


def test_mail_column_config_loads_only_complete_valid_widths(app, monkeypatch, tmp_path):
    config = tmp_path / "treeview-columns.json"
    widths = {name: int(app.tree.column(name, "minwidth")) + 17 for name in app.tree["columns"]}
    config.write_text(json.dumps(widths), encoding="utf-8")
    monkeypatch.setattr(g, "COLUMN_CONFIG_PATH", config)
    app._column_config_loaded = False
    app._load_column_config()
    assert {name: int(app.tree.column(name, "width")) for name in app.tree["columns"]} == widths

    config.write_text(json.dumps({"from": widths["from"]}), encoding="utf-8")
    before = {name: int(app.tree.column(name, "width")) for name in app.tree["columns"]}
    app._load_column_config()
    assert {name: int(app.tree.column(name, "width")) for name in app.tree["columns"]} == before

    warnings = []
    monkeypatch.setattr(app.log, "warning", lambda *args: warnings.append(args))
    config.write_text("{broken", encoding="utf-8")
    app._load_column_config()
    config.write_text(json.dumps(dict.fromkeys(app.tree["columns"], 1)), encoding="utf-8")
    app._load_column_config()
    assert len(warnings) == 2
    for name, width in {"from": 200, "subject": 320, "when": 110, "folder": 140, "size": 90}.items():
        app.tree.column(name, width=width)
    app.tree.column("subject", stretch=True)


def test_mail_column_config_save_is_atomic_and_persists_user_config(app, monkeypatch, tmp_path):
    """列宽/窗口/账户同一原子写：写盘失败时设置文件保持旧内容、界面不受影响。"""
    config = tmp_path / "treeview-columns.json"
    monkeypatch.setattr(g, "COLUMN_CONFIG_PATH", config)
    app._save_column_config()
    assert app._column_config_loaded
    saved = json.loads(config.read_text(encoding="utf-8"))
    assert saved["version"] == g.UI_SETTINGS_VERSION
    assert saved["columns"] == {
        name: int(app.tree.column(name, "width")) for name in app.tree["columns"]}
    assert "window" in saved
    assert saved["accounts"] and saved["accounts"][0]["user"] == current_account(app)["user"]
    before = {name: int(app.tree.column(name, "width")) for name in app.tree["columns"]}
    stored_before = config.read_text(encoding="utf-8")
    warnings = []
    monkeypatch.setattr(app.log, "warning", lambda *args: warnings.append(args))
    monkeypatch.setattr(g.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("nope")))
    app._save_column_config()
    assert {name: int(app.tree.column(name, "width")) for name in app.tree["columns"]} == before
    assert config.read_text(encoding="utf-8") == stored_before, "写盘失败不得覆盖仍可读的旧文件"
    assert warnings


def test_mail_column_release_saves_immediately(app, monkeypatch):
    saved = []
    monkeypatch.setattr(app, "_save_column_config", lambda: saved.append(True))
    monkeypatch.setattr(app.tree, "identify_region", lambda _x, _y: "separator")
    monkeypatch.setattr(app.tree, "identify_column", lambda _x: "#1")
    event = SimpleNamespace(x=100, y=2)
    app._on_tree_resize_start(event)
    app._on_tree_resize_end(event)
    assert saved == [True]


def test_ui_settings_accept_legacy_columns_and_restore_window(app, monkeypatch, tmp_path):
    config = tmp_path / "treeview-columns.json"
    widths = {name: int(app.tree.column(name, "minwidth")) + 19 for name in app.tree["columns"]}
    config.write_text(json.dumps(widths), encoding="utf-8")
    monkeypatch.setattr(g, "COLUMN_CONFIG_PATH", config)
    app._ui_settings = app._read_ui_settings()
    assert app._ui_settings["columns"] == widths

    app._ui_settings = {
        "version": g.UI_SETTINGS_VERSION,
        "columns": widths,
        "window": {
            "normal_geometry": {"width": 900, "height": 700, "x": 20, "y": 30},
            "maximized": False,
        },
    }
    monkeypatch.setattr(app, "_window_position_visible", lambda *_args: True)
    app._normal_geometry = app._restore_window_settings()
    assert app._normal_geometry == (900, 700, 20, 30)
    assert app.root.geometry().startswith("900x700+20+30")


def test_invalid_window_settings_do_not_discard_columns(app, monkeypatch, tmp_path):
    config = tmp_path / "treeview-columns.json"
    widths = {name: int(app.tree.column(name, "minwidth")) + 19 for name in app.tree["columns"]}
    config.write_text(json.dumps({
        "version": g.UI_SETTINGS_VERSION,
        "columns": widths,
        "window": {"normal_geometry": {"width": 1}, "maximized": True},
    }), encoding="utf-8")
    monkeypatch.setattr(g, "COLUMN_CONFIG_PATH", config)
    app._ui_settings = app._read_ui_settings()
    app._column_config_loaded = False
    app._load_column_config()
    assert {name: int(app.tree.column(name, "width")) for name in app.tree["columns"]} == widths


def test_close_saves_window_settings_before_destroy(app, monkeypatch):
    """偏好=退出程序：关闭按钮仍然「先存界面设置、再销毁窗口」。

    默认偏好是「每次询问」，这里注入退出偏好绕过询问对话框；_quit 也换替身，
    免得把模块级共享实例的真托盘一起拆掉。
    """
    calls = []
    monkeypatch.setattr(app, "_save_column_config", lambda: calls.append("save"))
    monkeypatch.setattr(app, "_quit", lambda: calls.append("destroy"))
    monkeypatch.setattr(app, "_close_action", lambda: g.CLOSE_ACTION_EXIT)
    app.running = False
    app._on_close()
    assert calls == ["save", "destroy"]


def test_mail_column_restore_shrinks_proportionally_but_keeps_native_minwidth(app, monkeypatch):
    app._column_config_loaded = True
    app._column_config_fitted = False
    monkeypatch.setattr(app.tree, "winfo_width", lambda: 300)
    app._fit_loaded_columns()
    assert sum(int(app.tree.column(name, "width")) for name in app.tree["columns"]) <= 300
    assert all(int(app.tree.column(name, "width")) >= int(app.tree.column(name, "minwidth"))
               for name in app.tree["columns"])


def test_mail_columns_reflow_again_when_window_shrinks_after_widening(app, monkeypatch):
    """全屏时放宽非主题列后缩窗，所有列都应按比例回收而不被顶出范围。"""
    available = [2000]
    monkeypatch.setattr(app.tree, "winfo_width", lambda: available[0])
    app._column_config_loaded = True
    app._column_config_fitted = False
    app.tree.column("from", width=500)
    app.tree.column("subject", width=700)
    app.tree.column("when", width=300)
    app.tree.column("folder", width=350)
    app.tree.column("size", width=150)
    app._on_tree_configure(None)
    assert sum(int(app.tree.column(name, "width")) for name in g.MAIL_COLUMNS) <= 2000

    available[0] = 800
    app._on_tree_configure(None)
    widths = {name: int(app.tree.column(name, "width")) for name in g.MAIL_COLUMNS}
    assert sum(widths.values()) <= 800
    assert all(widths[name] >= int(app.tree.column(name, "minwidth")) for name in g.MAIL_COLUMNS)


def test_mail_columns_expand_proportionally_when_window_grows(app, monkeypatch):
    """窗口放大时，各列按比例增长，而不是只让主题列吸收剩余宽度。"""
    available = [800]
    monkeypatch.setattr(app.tree, "winfo_width", lambda: available[0])
    app._column_config_loaded = True
    app._column_config_fitted = False
    for name, width in {"from": 120, "subject": 240, "when": 90, "folder": 200, "size": 70}.items():
        app.tree.column(name, width=width)
    app._on_tree_configure(None)
    before = {name: int(app.tree.column(name, "width")) for name in g.MAIL_COLUMNS}

    available[0] = 1600
    app._on_tree_configure(None)
    after = {name: int(app.tree.column(name, "width")) for name in g.MAIL_COLUMNS}
    assert after["from"] > before["from"]
    assert after["when"] > before["when"]
    assert after["folder"] > before["folder"]
    assert after["size"] > before["size"]
    assert after["subject"] > before["subject"]
    assert sum(after.values()) <= 1600


def test_tree_context_menu_copies_the_clicked_cell(app, monkeypatch):
    """邮件列表右键：行上弹「复制」并复制所在单元格（点哪列复制哪格）；空白与列标题不弹。

    右键先选中该行（Windows 惯例）；已在多选组里的行保留整组选中。
    """
    assert [app._tree_menu.entrycget(i, "label") for i in range(app._tree_menu.index("end") + 1)] == [
        g.MENU_COPY_LABEL]
    assert app.tree.bind("<Button-3>")
    assert not app.folder_canvas.bind("<Button-3>"), "文件夹面板按拍板不加右键"

    app._clear_message_rows()
    for i in range(1, 3):
        app.events.put(("message", app._selected_account_id,
                        _message(index=i, subject=f"主题{i}", size=100 + i, msg_id=f"<{i}@y>")))
    pump(app)
    ids = app.tree.get_children()
    assert len(ids) == 2, ids
    values = tuple(str(v) for v in app.tree.item(ids[0], "values"))
    assert values[1] == "主题1" and values[1] != values[4], values

    popups = stub_popup(monkeypatch, app._tree_menu)

    def cell_center(iid, column):
        x, y, w, h = app.tree.bbox(iid, column)
        return x + w // 2, y + h // 2

    # 主题列与大小列各右键一次：复制值随落点列走，不是整行
    for column, col_idx in (("#2", 2), ("#5", 5)):
        cx, cy = cell_center(ids[0], column)
        assert app.tree.identify_column(cx) == column, (app.tree.identify_column(cx), column)
        before = len(popups)
        app.tree.event_generate("<Button-3>", x=cx, y=cy)
        app.root.update()
        assert len(popups) == before + 1, popups
        assert app.tree.selection() == (ids[0],), app.tree.selection()
        assert app._tree_menu_target == (ids[0], col_idx), app._tree_menu_target
        app._copy_menu_cell()
        app.root.update()
        assert app.root.clipboard_get() == values[col_idx - 1], app.root.clipboard_get()

    # 行下方空白与列标题：不弹，也不清选中
    app.tree.selection_set(ids[0])
    bx, by, _bw, bh = app.tree.bbox(ids[-1])
    blank_y = by + bh + 4
    assert app.tree.identify_row(blank_y) == ""
    app.tree.event_generate("<Button-3>", x=bx, y=blank_y)
    app.root.update()
    head_y = 2
    assert app.tree.identify("region", 30, head_y) == "heading"
    app.tree.event_generate("<Button-3>", x=30, y=head_y)
    app.root.update()
    assert len(popups) == 2, popups
    assert app.tree.selection() == (ids[0],), app.tree.selection()

    # 右键另一行：先选中它；已多选时再右键组内一行：保留整组
    cx, cy = cell_center(ids[1], "#2")
    app.tree.event_generate("<Button-3>", x=cx, y=cy)
    app.root.update()
    assert app.tree.selection() == (ids[1],), app.tree.selection()
    app.tree.selection_set(ids[0], ids[1])
    app.tree.event_generate("<Button-3>", x=cx, y=cy)
    app.root.update()
    assert set(app.tree.selection()) == {ids[0], ids[1]}, app.tree.selection()

    app.tree.selection_set()  # 收尾：别把选中态留给后面的用例


def test_log_context_menu_copies_selection_and_all(app, monkeypatch):
    """日志菜单两项：「复制」划选段 + 「复制全部」整篇；无选区/空日志各自置灰；菜单里没有「全选」。"""
    assert [app._log_menu.entrycget(i, "label") for i in range(app._log_menu.index("end") + 1)] == [
        g.MENU_COPY_LABEL, g.MENU_COPY_ALL_LABEL]
    assert app.log_text.bind("<Button-3>")

    # 日志页签摆到前台，并让窗口先完成一次映射：合成的鼠标/按键事件要有真实窗口才派发——
    # 孤立跑本用例（前面的用例都没跑）时少了这一步，事件会被丢掉，菜单状态停在默认值上。
    app.notebook.select(app.log_tab)
    app.root.update()
    app._pump()  # 先放完队列里排着的启动日志，断言期间不再冒新行

    # 清空日志（应用的写入路径就是先 normal 再 disabled）：空日志两项都无可复制的内容
    app.log_text.configure(state="normal")
    app.log_text.delete("1.0", "end")
    app.log_text.configure(state="disabled")

    popups = stub_popup(monkeypatch, app._log_menu)

    def popup_state():
        """走真实入口弹一次菜单，返回（复制, 复制全部）的灰化状态。"""
        app.log_text.event_generate("<Button-3>", x=5, y=5)
        app.root.update()
        return (app._log_menu.entrycget(g.MENU_COPY_LABEL, "state"),
                app._log_menu.entrycget(g.MENU_COPY_ALL_LABEL, "state"))

    assert popup_state() == ("disabled", "disabled")
    assert len(popups) == 1, popups

    app._append_log("第一行")
    app._append_log("第二行")

    # Tk 平台把 <<SelectAll>> 映射到 Ctrl+A（探针实测，issues/13）：日志上按下去会整篇选中。
    # 这不是菜单项（菜单里没有「全选」），是平台默认；顺手用它验证「有选区 → 复制可用」
    app.log_text.focus_force()
    app.log_text.event_generate("<Control-a>")
    app.root.update()
    whole = app.log_text.get("sel.first", "sel.last")
    assert whole.rstrip("\n").splitlines() == ["第一行", "第二行"], repr(whole)  # 选中到 end，尾部带空行
    assert popup_state() == ("normal", "normal"), popups
    app._log_copy_selection()
    app.root.update()
    assert app.root.clipboard_get() == whole, repr(app.root.clipboard_get())

    # 清掉选区：「复制」回到灰，「复制全部」不受影响（`end-1c` 以换行结尾，按行核对而不是逐字比对）
    app.log_text.tag_remove("sel", "1.0", "end")
    app.root.update()
    assert popup_state() == ("disabled", "normal"), popups
    app._log_copy_all()
    app.root.update()
    assert app.root.clipboard_get().splitlines() == ["第一行", "第二行"], repr(app.root.clipboard_get())

    # 只读态照样能鼠标划选（探针实测，issues/13）：在第 1 行里从第 1 个字拖到第 3 个字前，
    # 「复制」只复制这一小段——坐标用 bbox 现算，不依赖字体宽度
    sx, sy = app.log_text.bbox("1.0")[:2]
    ex, ey = app.log_text.bbox("1.2")[:2]
    app.log_text.event_generate("<Button-1>", x=sx + 1, y=sy + 2)
    app.log_text.event_generate("<B1-Motion>", x=ex - 1, y=ey + 2)
    app.log_text.event_generate("<ButtonRelease-1>", x=ex - 1, y=ey + 2)
    app.root.update()
    dragged = app.log_text.get("sel.first", "sel.last")
    assert dragged == "第一", repr(dragged)
    assert popup_state() == ("normal", "normal"), popups
    app._log_copy_selection()
    app.root.update()
    assert app.root.clipboard_get() == "第一", repr(app.root.clipboard_get())
    assert app.log_text.cget("state") == "disabled", "复制不该动到日志的只读态"


def test_entry_focus_clears_on_click_outside(app, monkeypatch):
    """输入框焦点与邮件选中同一口径：点非输入框的开阔地（标签、面板空白、滚条）即收归主窗口；
    输入框自身与邮件列表滚条是例外。

    回归自：点别处焦点留在输入框，只有点邮件列表区才走得掉（探针实测，issues/14）。

    焦点断言分两路：`focus_set` 是「非强制」的——测试窗口被外部抢走前台时请求会被推迟，
    focus_get() 停在原处（全量偶发失败就是它）。所以「收归主窗口」用替身盯住处理器有没有
    发出请求（确定性契约），焦点去向只做宽松校验：前台在时立即落到主窗口，被抢时仍停在
    输入框，两种都算过；没发请求才失败。点可聚焦控件（另一个输入框、列表）时 Tk 类绑定
    完成自然转移，本入口不插手也不抢占——同样允许它的 focus 请求被推迟。
    """
    def focus_path():
        f = app.root.focus_get()
        return None if f is None else str(f)

    def walk(widget):
        yield widget
        for child in widget.winfo_children():
            yield from walk(child)

    label = next(w for w in walk(app.root)
                 if isinstance(w, tk.Label) and w.cget("text") == "邮箱地址：")
    entry = app._account_form_entries[1]
    other_entry = app._account_form_entries[2]

    app._clear_message_rows()
    for i in range(1, 3):
        app.events.put(("message", app._selected_account_id,
                        _message(index=i, subject=f"焦点 {i}", size=100, msg_id=f"<f{i}@y>")))
    pump(app)
    ids = app.tree.get_children()
    assert len(ids) == 2, len(ids)

    # focus_set 的替身：数「处理器有没有发出收归请求」——这是不依赖前台归属的确定性契约
    asked = []
    monkeypatch.setattr(app.root, "focus_set", lambda *a, **k: asked.append(1))

    def click_expecting_clear(target, x=None, y=None):
        before = len(asked)
        click(app, target, x, y)
        if len(asked) == before:
            # 合成的鼠标事件偶发被丢弃（窗口尚未完成映射 / 系统繁忙，见 issues/13 的教训），
            # 重发一次再判：真回归会两次都不响应，仍会失败。
            click(app, target, x, y)
        assert len(asked) == before + 1, (target, "处理器应把焦点收归主窗口")
        # 前台在：立即落到主窗口；被抢前台：请求被推迟、focus_get() 仍停在输入框
        assert focus_path() in (str(app.root), str(entry)), (target, focus_path())

    # 点不可聚焦的标签：选中与焦点一次点击各归各位
    app.tree.selection_set(ids[0])
    entry.focus_force()
    app.root.update()
    assert focus_path() == str(entry), focus_path()
    click_expecting_clear(label)
    assert app.tree.selection() == (), app.tree.selection()

    # 文件夹面板空白与其滚条：同为「别处」，焦点同样收归主窗口
    for target in (app.folder_canvas, app.folder_scroll):
        entry.focus_force()
        app.root.update()
        click_expecting_clear(target)

    # 点输入框自身：正常编辑焦点保持，且不该发收归请求
    entry.focus_force()
    app.root.update()
    before = len(asked)
    click(app, entry, 5, 5)
    assert len(asked) == before, "点输入框自身不该发收归请求"
    assert focus_path() == str(entry), focus_path()
    # 点另一个输入框：类绑定自然转移，本入口不许抢占（前台被抢时类绑定的请求也会被推迟）
    click(app, other_entry, 5, 5)
    assert len(asked) == before, "点另一个输入框时焦点归类绑定，本入口不插手"
    assert focus_path() in (str(other_entry), str(entry)), focus_path()

    # 禁用输入框不接焦点（探针实测，issues/14），点它不算「点了那个框」，
    # 旧焦点同样要收走，不能滞留在框里
    other_entry["state"] = "disabled"
    entry.focus_force()
    app.root.update()
    assert focus_path() == str(entry), focus_path()
    click_expecting_clear(other_entry, 5, 5)
    other_entry["state"] = "normal"

    # 点邮件列表（列标题区域）：Treeview 类绑定拿焦点，本入口不插手。
    # 列表页签要摆到前台：上一用例把 notebook 留在了日志页签，不可映射的控件拿不到焦点
    app.notebook.select(app.list_tab)
    app.root.update()
    entry.focus_force()
    app.root.update()
    before = len(asked)
    click(app, app.tree, 30, 2)
    got = focus_path()
    assert got in (str(app.tree), str(entry)), got
    if got == str(entry):
        # 前台被抢、类绑定的 focus 请求被推迟时，处理器会补发一次收归主窗口的请求
        assert len(asked) > before, "焦点停在输入框时应补发收归请求"

    # 例外：邮件列表自己的滚条——滚动查看时的点击，选中与焦点都保持，也不发收归请求
    app.tree.selection_set(ids[0])
    entry.focus_force()
    app.root.update()
    before = len(asked)
    app.tree_scroll.event_generate("<Button-1>")
    app.tree_scroll.event_generate("<ButtonRelease-1>")
    app.root.update()
    assert len(asked) == before, "点列表滚条不该发收归请求"
    assert app.tree.selection() == (ids[0],), app.tree.selection()
    assert focus_path() == str(entry), focus_path()

    app.root.focus_set()  # 收尾：不把聚焦与选中态留给后续用例
    app.tree.selection_set()


def test_accounts_save_and_restore_round_trip(app, monkeypatch, tmp_path):
    """save → 重读：账户各字段、密码密文、全局备份位置与选中账户都在；明文不落盘。"""
    config = tmp_path / "treeview-columns.json"
    monkeypatch.setattr(g, "COLUMN_CONFIG_PATH", config)
    account = current_account(app)
    account.update({"name": "公司", "user": "bob@example.com", "server": "imap.corp.com",
                    "ssl": False, "port": "1143", "folders": ["INBOX"], "mbox": True, "eml": False})
    if DPAPI_OK:
        account["password_dpapi"] = g._dpapi_encrypt("密päss")
    load_folders(app, NAMES)     # 勾选按账户记录恢复：INBOX 在树上就是勾上的
    assert row_by(app, "INBOX")["var"].get()
    app.backup_root_var.set("D:\\bob 备份")
    app._save_column_config()
    assert app._column_config_loaded

    saved = json.loads(config.read_text(encoding="utf-8"))
    assert saved["version"] == g.UI_SETTINGS_VERSION
    assert saved["backup_root"] == "D:\\bob 备份"
    assert saved["selected_account"] == account["id"]
    record = saved["accounts"][0]
    assert (record["name"], record["user"], record["server"]) == ("公司", "bob@example.com", "imap.corp.com")
    assert record["ssl"] is False and record["port"] == "1143"
    assert record["mbox"] is True and record["eml"] is False
    assert record["folders"] == ["INBOX"]
    if DPAPI_OK:
        assert g._dpapi_decrypt(record["password_dpapi"]) == "密päss"
        assert "密päss" not in config.read_text(encoding="utf-8"), "明文密码不落盘"

    # 重读（等价于重启后的解析）：字段逐个回到账户记录
    reloaded = app._read_ui_settings()
    assert reloaded["backup_root"] == "D:\\bob 备份"
    assert reloaded["selected_account"] == account["id"]
    parsed = reloaded["accounts"][0]
    assert (parsed["name"], parsed["user"], parsed["server"], parsed["port"]) == (
        "公司", "bob@example.com", "imap.corp.com", "1143")
    assert parsed["ssl"] is False and parsed["mbox"] is True and parsed["eml"] is False
    assert parsed["folders"] == ["INBOX"]
    if DPAPI_OK:
        assert g._dpapi_decrypt(parsed["password_dpapi"]) == "密päss"

    # 写盘失败：设置文件保持上一份可读内容，界面不受影响
    before = config.read_text(encoding="utf-8")
    warnings = []
    monkeypatch.setattr(app.log, "warning", lambda *args: warnings.append(args))
    monkeypatch.setattr(g.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("nope")))
    app._save_column_config()
    assert warnings
    assert config.read_text(encoding="utf-8") == before

    # 收尾：把共享实例还原成用例链后续需要的状态
    monkeypatch.undo()
    account.update({"name": "主账户", "user": V["user"], "server": V["server"], "ssl": True, "port": "993",
                    "folders": [], "mbox": False, "eml": True})
    if DPAPI_OK:
        account["password_dpapi"] = g._dpapi_encrypt(V["password"])
    app.backup_root_var.set(g.DEFAULT_BACKUP_ROOT)
    app._refresh_account_list()
    app._render_account_detail()


def test_accounts_v1_settings_migrate_to_accounts(app, monkeypatch, tmp_path):
    """v1（单账号 config 节）迁到 v2：账号成列表首项，outdir 变 backup_root，其余节保留。"""
    config = tmp_path / "treeview-columns.json"
    widths = {name: int(app.tree.column(name, "minwidth")) + 21 for name in app.tree["columns"]}
    legacy = {
        "version": 1,
        "columns": widths,
        "window": {"normal_geometry": {"width": 900, "height": 700, "x": 20, "y": 30}, "maximized": False},
        g.CLOSE_ACTION_KEY: g.CLOSE_ACTION_TRAY,
        "config": {"user": "old@example.com", "server": "imap.old.com", "ssl": False, "port": "1143",
                   "outdir": "E:\\旧备份"},
    }
    if DPAPI_OK:
        legacy["config"]["password_dpapi"] = g._dpapi_encrypt("旧密码")
    config.write_text(json.dumps(legacy), encoding="utf-8")
    monkeypatch.setattr(g, "COLUMN_CONFIG_PATH", config)

    parsed = app._read_ui_settings()
    assert parsed["version"] == g.UI_SETTINGS_VERSION
    assert parsed["columns"] == widths
    assert parsed["backup_root"] == "E:\\旧备份"
    assert parsed[g.CLOSE_ACTION_KEY] == g.CLOSE_ACTION_TRAY
    assert parsed["window"] == legacy["window"]
    assert len(parsed["accounts"]) == 1
    account = parsed["accounts"][0]
    assert account["user"] == "old@example.com" and account["server"] == "imap.old.com"
    assert account["ssl"] is False and account["port"] == "1143"
    assert account["folders"] == [] and account["mbox"] is False and account["eml"] is True
    assert parsed["selected_account"] == account["id"]
    if DPAPI_OK:
        assert g._dpapi_decrypt(account["password_dpapi"]) == "旧密码"

    # 坏 config 节：不造账户，其余节照常
    legacy["config"] = "nope"
    config.write_text(json.dumps(legacy), encoding="utf-8")
    parsed = app._read_ui_settings()
    assert parsed["accounts"] == [] and parsed["selected_account"] is None
    assert parsed["columns"] == widths


def test_accounts_restore_tolerates_bad_records(app, monkeypatch, tmp_path):
    """账户列表的坏项逐个跳过：非列表整节丢弃；缺 id / 类型不符按字段降级。"""
    config = tmp_path / "treeview-columns.json"
    monkeypatch.setattr(g, "COLUMN_CONFIG_PATH", config)
    warnings = []
    monkeypatch.setattr(app.log, "warning", lambda *args: warnings.append(args))

    config.write_text(json.dumps({"version": g.UI_SETTINGS_VERSION, "accounts": "nope"}), encoding="utf-8")
    parsed = app._read_ui_settings()
    assert parsed["accounts"] == [] and warnings

    warnings.clear()
    config.write_text(json.dumps({
        "version": g.UI_SETTINGS_VERSION,
        "backup_root": 42,
        "accounts": [
            {"user": "no-id@example.com"},                       # 缺稳定 id：跳过
            {"id": "keep", "user": 42, "server": "imap.ok.com", "ssl": "yes", "port": 993,
             "folders": ["INBOX", 7, ""], "mbox": "yes"},
        ],
    }), encoding="utf-8")
    parsed = app._read_ui_settings()
    assert parsed["backup_root"] is None                      # 非字符串回退默认
    assert len(parsed["accounts"]) == 1
    account = parsed["accounts"][0]
    assert account["user"] == ""                              # 非字符串，回落默认
    assert account["server"] == "imap.ok.com"
    assert account["ssl"] is True                             # "yes" 不是 bool，回落默认
    assert account["port"] == g.DEFAULT_ACCOUNT_PORT          # int 不是 str，回落默认
    assert account["folders"] == ["INBOX"]                    # 只留非空字符串
    assert account["mbox"] is False                           # "yes" 不是 bool，回落默认
    assert parsed["selected_account"] == "keep"               # 选中的就是留下的那个


def test_backup_detail_has_no_entry_widgets(app):
    """备份详情页没有输入框：连接信息与备份地址都是只读信息卡（用户口径）。"""
    def walk(widget):
        yield widget
        for child in widget.winfo_children():
            yield from walk(child)

    entries = [w for w in walk(app.backup_page) if isinstance(w, tk.Entry)]
    assert entries == [], entries
    assert current_account(app) is not None
    assert app.card_root_label.cget("text") == app.backup_root_var.get().strip()
    assert app._card_labels["password"].cget("text") in (g.PASSWORD_SAVED_TEXT, g.PASSWORD_MISSING_TEXT)


def test_format_checkboxes_are_per_account(app):
    """格式复选框按账户独立：切到另一个账户看到的是它自己的勾选。"""
    first = current_account(app)
    app.mbox_var.set(True)
    app.eml_var.set(False)
    app._on_format_change()
    assert first["mbox"] is True and first["eml"] is False

    second = g.new_account()
    second.update({"name": "第二账户", "user": "second@example.com", "server": "imap.second.com"})
    app.accounts.append(second)
    app._refresh_account_list()
    app._switch_account(second["id"])
    assert app.mbox_var.get() is False and app.eml_var.get() is True   # 新账户默认 mbox 关、eml 开
    assert app.card_root_label.cget("text") == app.backup_root_var.get().strip()

    app._switch_account(first["id"])
    assert app.mbox_var.get() is True and app.eml_var.get() is False   # 切回来还是它自己的勾选
    assert app._card_labels["user"].cget("text") == first["user"]

    app.accounts.remove(second)
    app._states.pop(second["id"], None)
    app._refresh_account_list()
    app._render_account_detail()


def test_account_rows_are_gray_until_selected(app):
    """行底色：未选中是对话框灰（不再是 Listbox 的白底），选中那行整行系统高亮蓝（用户口径）。"""
    first = current_account(app)
    second = g.new_account()
    second.update({"name": "灰底对照", "user": "gray@example.com", "server": "imap.example.com"})
    app.accounts.append(second)
    app._refresh_account_list()
    app._switch_account(first["id"])

    def paint(account_id):
        row = app._account_rows[account_id]
        return {"row_bg": row["frame"].cget("background"), "label_bg": row["label"].cget("background"),
                "label_fg": row["label"].cget("foreground"), "check_bg": row["check"].cget("background")}

    def selected():   # 整行同色，含复选框：不然在蓝行上按它会闪出灰方块
        return {"row_bg": g.ACCOUNT_ROW_SELECTED_BG, "label_bg": g.ACCOUNT_ROW_SELECTED_BG,
                "label_fg": g.ACCOUNT_ROW_SELECTED_FG, "check_bg": g.ACCOUNT_ROW_SELECTED_BG}

    def plain():
        return {"row_bg": g.ACCOUNT_ROW_BG, "label_bg": g.ACCOUNT_ROW_BG,
                "label_fg": g.ACCOUNT_ROW_FG, "check_bg": g.ACCOUNT_ROW_BG}

    assert paint(first["id"]) == selected()
    assert paint(second["id"]) == plain()

    app.root.update()   # 合成点击前先让新行落地：控件还没映射时事件不会派发
    click(app, app._account_rows[second["id"]]["label"])
    assert app._selected_account_id == second["id"]
    assert paint(second["id"]) == selected()
    assert paint(first["id"]) == plain()

    app._switch_account(first["id"])
    app.accounts.remove(second)
    app._states.pop(second["id"], None)
    app._refresh_account_list()
    app._render_account_detail()


def test_account_checkboxes_are_independent_of_selection(app):
    """复选框与选中是两件事：翻勾选不切详情、点行不改勾选；全选/反选/清空只作用在勾选上。"""
    first = current_account(app)
    second = g.new_account()
    second.update({"name": "勾选对照", "user": "check@example.com", "server": "imap.example.com"})
    app.accounts.append(second)
    app._refresh_account_list()                # 新账户默认勾上（勾是默认态）
    app._switch_account(first["id"])
    assert app._account_rows[second["id"]]["var"].get()

    assert app._checked_accounts == {first["id"], second["id"]}
    app._account_rows[second["id"]]["check"].invoke()
    assert second["id"] not in app._checked_accounts
    assert app._selected_account_id == first["id"]      # 翻勾选不切详情

    app.root.update()   # 合成点击前先让行落地：控件还没映射时事件不会派发
    click(app, app._account_rows[second["id"]]["label"])
    assert app._selected_account_id == second["id"]
    assert second["id"] not in app._checked_accounts    # 点行不改勾选

    app._set_all_account_checks(True)
    assert app._checked_accounts == {first["id"], second["id"]}
    assert all(row["var"].get() for row in app._account_rows.values())

    app._invert_account_checks()
    assert app._checked_accounts == set()
    assert not any(row["var"].get() for row in app._account_rows.values())

    app._invert_account_checks()
    assert app._checked_accounts == {first["id"], second["id"]}

    app._set_all_account_checks(False)
    assert app._checked_accounts == set()
    app._refresh_account_list()                # 重画列表不会把用户取消的勾选又补回来
    assert app._checked_accounts == set()

    app._switch_account(first["id"])
    app.accounts.remove(second)
    app._states.pop(second["id"], None)
    app._refresh_account_list()
    app._render_account_detail()
    assert app._checked_accounts == set()               # 删掉的账户不再占勾选
    app._set_all_account_checks(True)                   # 交还给模块时恢复默认全勾
    assert app._checked_accounts == {first["id"]}


def test_backup_selected_runs_only_checked_accounts(app, monkeypatch):
    """「备份所选」只跑勾选的账户；一个都没勾时只提示、不进运行态。"""
    first = current_account(app)
    second = g.new_account()
    second.update({"name": "不选它", "user": "skip@example.com", "server": "imap.example.com"})
    app.accounts.append(second)
    app._refresh_account_list()
    app._switch_account(first["id"])

    started: list[list[str]] = []
    monkeypatch.setattr(app, "_begin_run", started.append)
    monkeypatch.setattr(app, "_account_credentials", lambda account, warn=True: {"server": "s"})
    monkeypatch.setattr(app, "_backup_root_ready", lambda: True)
    box = FakeBox()
    monkeypatch.setattr(g, "messagebox", box)

    app._set_all_account_checks(False)
    app._on_start_selected()
    assert started == []                                    # 一个都没勾：不进运行态
    assert box.warnings and box.warnings[-1][0] == "未勾选账户"

    box.warnings.clear()
    app._account_rows[second["id"]]["var"].set(False)
    app._account_rows[first["id"]]["var"].set(True)
    app._sync_account_checks()
    app._on_start_selected()
    assert started == [[first["id"]]]                        # 只跑勾上的那一个
    assert box.warnings == []

    app.accounts.remove(second)
    app._states.pop(second["id"], None)
    app._refresh_account_list()
    app._render_account_detail()
    assert app._checked_accounts == {first["id"]}


def test_batch_worker_runs_accounts_in_order_and_isolates_failures(app, monkeypatch):
    """批量串行：按列表顺序逐个跑，单个账户失败不影响其余；停止后不再开新账户。"""
    first = g.new_account()
    first.update({"name": "甲", "user": "a@example.com", "server": "imap.example.com", "password": "p"})
    second = g.new_account()
    second.update({"name": "乙", "user": "b@example.com", "server": "imap.example.com", "password": "p"})
    third = g.new_account()
    third.update({"name": "丙", "user": "c@example.com", "server": "imap.example.com", "password": "p"})
    calls: list[str] = []

    def fake_backup(account, outdir, eq, log):
        calls.append(account["id"])
        if account["id"] == second["id"]:
            raise RuntimeError("boom")
        eq.put(("account_done", account["id"], 1, 0, 0.5, False, 1))

    monkeypatch.setattr(app, "_backup_account", fake_backup)
    snapshots = [{"id": a["id"], "user": a["user"], "server": a["server"], "password": "p",
                  "ssl": True, "port": "993", "folders": [], "mbox": False, "eml": True,
                  "name": a["name"]}
                 for a in (first, second, third)]
    app.stop_flag.clear()
    app._batch_worker(snapshots, "D:\\out")
    assert calls == [first["id"], second["id"], third["id"]]

    events = []
    while True:
        try:
            events.append(app.events.get_nowait())
        except g.queue.Empty:
            break
    kinds = [e[0] for e in events if e[0] != "log"]
    assert kinds == ["account_begin", "account_done", "account_begin", "account_failed",
                     "account_begin", "account_done", "batch_done"]
    assert events[-1][2] == [("乙", "RuntimeError: boom")]

    # 停止后：剩下的账户只收到 account_skipped，不再发起备份
    calls.clear()
    app.stop_flag.set()
    app._batch_worker(snapshots, "D:\\out")
    app.stop_flag.clear()
    assert calls == []
    events = []
    while True:
        try:
            events.append(app.events.get_nowait())
        except g.queue.Empty:
            break
    kinds = [e[0] for e in events if e[0] != "log"]
    assert kinds == ["account_skipped", "account_skipped", "account_skipped", "batch_done"]


def test_backup_account_uses_email_for_the_account_directory(app, monkeypatch, tmp_path):
    """账号目录名取邮箱地址（ADR 0002），账户名称只是显示名、不参与输出路径（ADR 0010）。"""
    account = {"id": "acc-dir", "name": "工作邮箱", "user": "a@example.com", "server": "imap.example.com",
               "ssl": True, "port": "993", "password": "p", "folders": ["INBOX"], "mbox": True, "eml": False}
    scanned: dict = {}

    class FakeServer:
        def select(self, *_args, **_kwargs):
            return "OK", b""

        def logout(self):
            pass

    monkeypatch.setattr(g.core, "connect_and_login", lambda cfg, log: FakeServer())
    monkeypatch.setattr(g.core, "get_names",
                        lambda *a, **k: [("INBOX", "INBOX.mbox", "INBOX", "INBOX")])
    monkeypatch.setattr(g.core, "scan_folder", lambda *a, **k: {"<m@x>": 1})

    def fake_scan_file(label, *_a, **_k):
        scanned["label"] = label
        return {}

    monkeypatch.setattr(g.core, "scan_file", fake_scan_file)
    monkeypatch.setattr(g.core, "scan_eml_dir", lambda *a, **k: {})
    monkeypatch.setattr(g.core, "ensure_basedir", lambda *a, **k: None)
    monkeypatch.setattr(g.core, "create_folder_structure",
                        lambda names, basedir, account: scanned.setdefault("dir", account))
    monkeypatch.setattr(g.core, "download_messages", lambda *a, **k: None)

    app.stop_flag.clear()
    app._backup_account(account, str(tmp_path), app.events, logging.getLogger("test"))
    assert scanned["dir"] == "a@example.com", scanned
    assert scanned["label"] == "a@example.com/INBOX.mbox", scanned
    assert "工作邮箱" not in str(scanned)


def test_entry_context_menu_has_three_actions(app, monkeypatch):
    """账户表单输入框共用一张右键菜单：剪切/复制/粘贴，动作与快捷键同一路径；不含全选。"""
    assert [app._entry_menu.entrycget(i, "label") for i in range(app._entry_menu.index("end") + 1)] == [
        g.MENU_CUT_LABEL, g.MENU_COPY_LABEL, g.MENU_PASTE_LABEL]
    for entry in [*app._account_form_entries, app.backup_root_entry]:
        assert entry.bind("<Button-3>"), entry

    popups = stub_popup(monkeypatch, app._entry_menu)
    app._acct_user_var.set("alice@example.com")
    app._account_form_entries[1].select_range(0, "end")
    app._account_form_entries[1].event_generate("<Button-3>", x=5, y=5)
    app.root.update()
    assert len(popups) == 1, popups
    assert str(app._entry_menu_target) == str(app._account_form_entries[1])
    assert app._entry_menu.entrycget(g.MENU_COPY_LABEL, "state") == "normal"

    # 剪切与粘贴走 Entry 的 <<Cut>>/<<Paste>> 类绑定，与 Ctrl+X/V 完全同效
    app._entry_edit("<<Cut>>")
    app.root.update()
    assert app._account_form_entries[1].get() == "", app._account_form_entries[1].get()
    assert app.root.clipboard_get() == "alice@example.com"
    app._entry_edit("<<Paste>>")
    app.root.update()
    assert app._account_form_entries[1].get() == "alice@example.com"

    # 密码框与普通输入框同菜单同路径；复制走 Tk 原生 <<Copy>>。探针实测（Tk 8.6.12，issues/13）：
    # show="*" 的选区按显示形态导出——复制到的是星号，不是明文；框里存的仍是明文
    app._acct_password_var.set("hunter2")
    app._entry_menu_target = app._acct_password_entry
    app._acct_password_entry.select_range(0, "end")
    app._entry_edit("<<Copy>>")
    app.root.update()
    assert app.root.clipboard_get() == "*" * len("hunter2"), app.root.clipboard_get()
    assert app._acct_password_entry.get() == "hunter2"


def test_entry_context_menu_items_grey_out(app, monkeypatch):
    """灰化口径：无选区剪切/复制灰；剪贴板为空或控件被禁用时粘贴灰。"""
    entry = app._account_form_entries[1]
    app._entry_menu_target = entry
    entry.select_range(0, "end")

    # 空剪贴板用替身触发真实的 TclError 分支，不动系统剪贴板
    def no_text():
        raise tk.TclError("CLIPBOARD clipboard is empty or has no text")

    monkeypatch.setattr(app.root, "clipboard_get", no_text)
    app._refresh_entry_menu_state(entry)
    assert app._entry_menu.entrycget(g.MENU_CUT_LABEL, "state") == "normal"
    assert app._entry_menu.entrycget(g.MENU_PASTE_LABEL, "state") == "disabled"

    monkeypatch.undo()
    app.root.clipboard_append("剪贴板内容")
    app.root.update()
    app._refresh_entry_menu_state(entry)
    assert app._entry_menu.entrycget(g.MENU_PASTE_LABEL, "state") == "normal"

    entry.select_clear()
    app._refresh_entry_menu_state(entry)
    assert app._entry_menu.entrycget(g.MENU_CUT_LABEL, "state") == "disabled"
    assert app._entry_menu.entrycget(g.MENU_COPY_LABEL, "state") == "disabled"

    # 禁用态的输入框（如运行中锁定的那种）：三项全灰，处理路径与真实禁用一致
    entry["state"] = "disabled"
    app._refresh_entry_menu_state(entry)
    assert app._entry_menu.entrycget(g.MENU_CUT_LABEL, "state") == "disabled"
    assert app._entry_menu.entrycget(g.MENU_COPY_LABEL, "state") == "disabled"
    assert app._entry_menu.entrycget(g.MENU_PASTE_LABEL, "state") == "disabled"
    entry["state"] = "normal"
