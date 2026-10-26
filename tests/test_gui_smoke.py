"""GUI 无头冒烟：实例化真实的 BackupApp，喂合成队列事件，断言 UI 状态。不联网。

整套用例共享同一个 module 级 app 实例，**pytest 按定义顺序执行**，所以前一节留下的
界面状态就是后一节的前置条件（账户 → 连接 → 树 → stale → 工具栏 → 级联 → … → 状态条 → 气泡）。
这个顺序依赖是有意保留的，别重排测试。

多账户改造后的事件都带 account_id：`folders_loaded` / `totals` / `message` / `account_done`
一律针对某个账户；账户各自独立运行（ADR 0012），活动集合排空即本轮结束，没有 `batch_done`。
`load_folders` 是本文件唯一的“连接成功”入口。

标了 @pytest.mark.gui：需要真实 Tk 显示与 ttk 主题，几何断言（winfo_width、
winfo_rootx 等）在异 DPI / 异主题的机器上可能不成立。无显示环境的 runner 用
`pytest -m "not gui"` 跳过。
"""
import json
import logging
import time
from datetime import datetime
from itertools import pairwise
from types import SimpleNamespace

import pytest
from _pytest.monkeypatch import MonkeyPatch

tk = pytest.importorskip("tkinter")
import tkinter.font as tkfont
import tkinter.ttk as ttk

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


# 三行平铺：一屏装得下的文件夹树（滚动类用例的「装得下」基准）
SHORT_FOLDERS = [("INBOX", "INBOX.mbox", "INBOX", "INBOX"),
                 ("Drafts", "Drafts.mbox", "Drafts", "Drafts"),
                 ("Junk", "Junk.mbox", "Junk", "Junk")]


def flat_folder_names(count):
    """count 个同名平铺文件夹（Box00…）：把文件夹树撑出可视区用。"""
    return [(f"Box{i:02d}", f"Box{i:02d}.mbox", f"Box{i:02d}", f"Box{i:02d}")
            for i in range(count)]


def folder_scroll_state(canvas):
    """文件夹画布的 (scrollregion 高, 内容 bbox 高)；两者不一致说明滚动范围还留着旧高度。"""
    box = canvas.bbox("all")
    region = tuple(map(float, canvas.cget("scrollregion").split()))
    return region[3] - region[1], box[3] - box[1]


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


def sync_thread(target=None, args=(), **_kwargs):
    """假线程：start() 当场跑完，用例不用跟真线程赛跑（只替换 threading.Thread）。"""
    def run() -> None:
        if target is not None:
            target(*args)
    return SimpleNamespace(start=run)


def set_phase(instance, account_id, phase):
    """把账户直接置为某备份阶段并刷新活动集合（不联网、不起线程；ADR 0012 测试专用）。"""
    instance._state(account_id)["run_phase"] = phase
    if phase != g.PHASE_IDLE:
        instance._cycle = {"t0": time.monotonic(), "backed": 0, "skipped": 0,
                           "failures": [], "stopped": False, "ids": {account_id}}
    instance._on_active_set_changed()
    instance.root.update_idletasks()   # 让「停止所有」的 pack/forget 落到映射状态


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


def image_name(btn):
    """按钮挂的图标名：ttk 的 image 选项返回元组，tk.Button 返回 str。"""
    img = btn.cget("image")
    return img[0] if isinstance(img, tuple) else str(img)


def is_disabled(w):
    """控件可用状态判定：ttk 的 cget("state") 返回 index 对象，走归一化口径。"""
    return g.widget_state(w) == "disabled"


def assert_start_button_idle(instance):
    """开始按钮回到静止态：文案回位，图标按可用性取彩色/灰度（Q4）。"""
    assert instance.start_btn.cget("text") == g.START_BTN_LABEL
    expected = instance.start_icon_off if is_disabled(instance.start_btn) else instance.start_icon
    assert image_name(instance.start_btn) == str(expected)


def assert_stop_button_idle(instance):
    """停止按钮回到静止态（永远禁用）：文案回位，图标按可用性取彩色/灰度。"""
    assert instance.stop_btn.cget("text") == g.STOP_BTN_LABEL
    expected = instance.stop_icon_off if is_disabled(instance.stop_btn) else instance.stop_icon
    assert image_name(instance.stop_btn) == str(expected)


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
    assert is_disabled(app.start_btn)
    # 两个按钮各自挂着一枚代码画的图标，静止态文案固定（改动前开始/停止按钮都是纯文字）
    assert app.start_btn.cget("text") == g.START_BTN_LABEL
    assert app.stop_btn.cget("text") == g.STOP_BTN_LABEL
    # 图标按可用性取彩色/灰度（Q4）：初始两颗都禁用，挂的应是灰度版
    assert image_name(app.start_btn) == str(app.start_icon_off if is_disabled(app.start_btn) else app.start_icon)
    assert image_name(app.stop_btn) == str(app.stop_icon_off if is_disabled(app.stop_btn) else app.stop_icon)
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
    for icon, color in ((app.start_icon, g.PLAY_ICON_COLOR), (app.stop_icon, g.STOP_ICON_COLOR),
                        (app.add_icon, g.PLAY_ICON_COLOR), (app.del_icon, g.STOP_ICON_COLOR),
                        (app.start_icon_off, g.ICON_DISABLED_COLOR), (app.stop_icon_off, g.ICON_DISABLED_COLOR)):
        center = size // 2
        assert icon.width() == size, icon.width()
        assert tuple(icon.get(center, center))[:3] == icon_rgb(color), (icon.get(center, center), color)
        assert not icon.transparency_get(center, center)
    assert app.start_icon.get(size // 2, size // 2) != app.stop_icon.get(size // 2, size // 2)
    # 新增=绿加号 / 删除=红叉，icon + text 挂载（compound=left，与开始/停止同口径）
    assert image_name(app.add_btn) == str(app.add_icon)
    assert image_name(app.del_btn) == str(app.del_icon)
    assert str(app.add_btn.cget("compound")) == "left" and str(app.del_btn.cget("compound")) == "left"


def test_version_is_shared_and_status_bar_starts_on_one_row(app):
    """版本统一 + 初始单行状态条 —— 必须在任何 _begin_run_ui 之前查，行一旦放出不再收回。"""
    assert g.__version__ == g.core.__version__, (g.__version__, g.core.__version__)
    assert app.progress.winfo_manager() == "" and app._msg_bar.winfo_manager() == ""
    assert app.start_btn.winfo_manager() == "pack"
    assert app.start_btn.pack_info()["side"] == "right"
    assert app.stop_btn.pack_info()["side"] == "right"
    assert app.count_var.get() == "尚未开始备份", app.count_var.get()


def test_folder_load_fills_the_tree(app):
    """folders_loaded 事件走完真实流程：账户先被读到，再投队列；勾选按账户记录默认 INBOX。"""
    account = current_account(app)
    account.update({"server": V["server"], "user": V["user"], "port": "993", "ssl": True})
    load_folders(app, NAMES)

    assert app.root.winfo_width() == 960 and app.root.winfo_height() == 736, (
        f"{app.root.winfo_width()}x{app.root.winfo_height()}")
    assert app.folder_ph.winfo_manager() == "", app.folder_ph.winfo_manager()
    assert len(app.folder_rows) == 6, [r["display"] for r in app.folder_rows]
    assert [r["foldername"] for r in app.folder_rows] == ["Archive/2024", "INBOX", OTHER, CHILD, DUP_A, DUP_B]
    assert row_by(app, "INBOX")["var"].get() is True
    assert not row_by(app, "Archive/2024")["var"].get()
    assert not is_disabled(app.start_btn), app.start_btn.cget("state")


def branch_of(instance, key):
    """节点行的前缀槽文本：层级连接符（└/├）+ 槽内展开箭头（仅父行）；一级行没有槽，返回 []。"""
    node = instance._folder_nodes[key]
    if node["prefix_label"] is None:
        return []
    slot = node["prefix_label"].master
    return [str(w.cget("text")) for w in slot.winfo_children()]


def trailing_arrow_of(instance, key):
    """一级行跟在名称后面的展开箭头文本；无箭头或箭头在槽里（二级及以下）返回 None。"""
    node = instance._folder_nodes[key]
    arrow = node["arrow"]
    if arrow is None or node["prefix_label"] is not None:
        return None
    return str(arrow.cget("text"))


def conn_row_of(instance, widget):
    """chip/端口所在 grid 行：同排时自己 grid 在服务器行；换行时 pack 在 _conn_wrap_row（下一行）。"""
    if widget.winfo_manager() == "pack":
        return int(instance._conn_wrap_row.grid_info()["row"])
    return int(widget.grid_info()["row"])


def hit_test(instance, widget):
    """widget 中心点的命中控件（z-order 回归）。

    先把测试窗抬到最上层再 winfo_containing：被别的应用窗口遮住时它会误报 None，
    而 z-order 要验的只是控件间的叠放（手柄有没有被同级的 Listbox 盖住）。
    """
    root = instance.root
    root.lift()
    try:
        root.attributes("-topmost", True)
        root.update()
        x = widget.winfo_rootx() + max(widget.winfo_width(), 1) // 2
        y = widget.winfo_rooty() + max(widget.winfo_height(), 1) // 2
        return root.winfo_containing(x, y)
    finally:
        root.attributes("-topmost", False)
        root.update()


def test_folder_tree_structure(app):
    """末段显示、真实/合成父节点、箭头、勾选符号、层级连接符、子节点缩进。"""
    assert sorted(app._folder_nodes) == [SYNTH, LEAF, ("INBOX",), PARENT, ("其他文件夹", "QQ邮件订阅"), DUP], (
        sorted(app._folder_nodes))
    assert app._folder_nodes[PARENT]["rows"]
    assert app._folder_nodes[SYNTH]["arrow"] is not None and not app._folder_nodes[SYNTH]["rows"]
    assert app._folder_nodes[("INBOX",)]["arrow"] is None
    assert is_open(app, SYNTH)
    # 一级行不画层级符号（用户选定）：没有前缀槽；有子文件夹时展开箭头跟在名称后
    assert branch_of(app, PARENT) == [] and trailing_arrow_of(app, PARENT) == g.ARROW_OPEN
    assert branch_of(app, SYNTH) == [] and trailing_arrow_of(app, SYNTH) == g.ARROW_OPEN
    assert branch_of(app, ("INBOX",)) == [], branch_of(app, ("INBOX",))
    assert trailing_arrow_of(app, ("INBOX",)) is None                     # 一级叶子行连箭头都没有
    # 二级及以下维持树形语言：同级末个用 └、其余用 ├，父行的展开箭头跟在连接符后
    assert branch_of(app, LEAF) == [g.TREE_LAST], branch_of(app, LEAF)                    # 父下唯一子
    assert branch_of(app, ("其他文件夹", "QQ邮件订阅")) == [g.TREE_BRANCH], "同级头一个"  # CHILD 的路径键
    assert branch_of(app, DUP) == [g.TREE_LAST], branch_of(app, DUP)                       # 同级末个（叶子无箭头）
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


def test_folder_prefix_labels_are_not_clipped(app):
    """连接符/箭头不得被前缀槽裁成「-」横切片：pack_propagate(False) 槽高度塌陷回归。"""
    app.root.update()
    linespace = tkfont.nametofont("TkDefaultFont").metrics("linespace")
    for node in app._folder_nodes.values():
        if node["prefix_label"] is None:   # 一级行没有前缀槽，箭头（若有）直接挂在行上
            if node["arrow"] is not None:
                assert node["arrow"].winfo_height() >= linespace, (node["key"], node["arrow"].winfo_height())
            continue
        slot = node["prefix_label"].master
        assert slot.winfo_height() >= linespace, (node["key"], slot.winfo_height(), linespace)
        labels = [node["prefix_label"]] + ([node["arrow"]] if node["arrow"] is not None else [])
        for label in labels:
            assert label.winfo_height() <= slot.winfo_height(), (node["key"], label.winfo_height())


def test_folder_top_rows_have_no_tree_symbol(app):
    """一级行不画 ├/└、复选框贴 CARD_PAD 标准内边距；展开箭头跟在名称后（用户选定）。"""
    app.root.update()
    # 标准内边距的参照是工具栏左缘（同为 CARD_PAD）：画布自己就带了 8px 内边距，
    # 一级行的前置槽一去掉，复选框就与工具栏落在同一条竖线上
    ref_x = app.reload_btn.master.winfo_rootx()
    canvas_x = app.folder_canvas.winfo_rootx()
    assert abs(ref_x - canvas_x) <= 1, (ref_x, canvas_x)
    for key, node in app._folder_nodes.items():
        if len(key) > 1:
            assert node["prefix_label"] is not None, key   # 二级及以下照旧带层级符号
            continue
        row = node["frame"].winfo_children()[0]
        name_x = node["label_widget"].winfo_rootx()
        assert abs(name_x - ref_x) <= 1, (key, name_x - ref_x)
        arrow = node["arrow"]
        if arrow is not None:
            assert arrow.master is row, key                       # 不在前缀槽里
            assert arrow.winfo_rootx() > name_x, key               # 跟在名称后


def test_folder_card_bottom_border_not_overflow(app):
    """「邮箱文件夹」内容不得顶出卡片盖住底边框：卡高 ≥ 请求高、画布底不出卡。"""
    app.root.update()
    group, canvas = app.folder_group, app.folder_canvas
    assert group.winfo_height() >= group.winfo_reqheight() - 1, (group.winfo_height(), group.winfo_reqheight())
    bottom = canvas.winfo_y() + canvas.winfo_height() + g.CARD_PAD
    assert bottom <= group.winfo_height(), (bottom, group.winfo_height())


def test_reload_button_is_square(app):
    """⟳ 是正方形 icon 按钮：宽高相等（误差 ≤2px），别再是竖长条。"""
    btn = app.reload_btn
    assert btn.cget("text") == g.RELOAD_GLYPH
    assert abs(btn.winfo_reqwidth() - btn.winfo_reqheight()) <= 2, (btn.winfo_reqwidth(), btn.winfo_reqheight())


def test_editing_credentials_triggers_a_silent_reload(app, monkeypatch):
    """改账户连接参数后（快照 ≠ 现值）再静默补拉清单；参数没变则不重拉（去重口径）。"""
    account = current_account(app)
    state = app._state(account["id"])
    calls: list[str] = []
    monkeypatch.setattr(app, "_folder_worker", lambda *a: calls.append(a[0]))
    app._loading_folder_ids.clear()

    # 快照与现值一致 → 不重拉
    state["snapshot"] = app._account_snapshot(account)
    app._maybe_load_folders(account["id"])
    assert calls == [], calls

    # 改了连接参数 → 触发一次静默拉取
    account["server"] = "imap.qq.com"
    app._maybe_load_folders(account["id"])
    assert calls == [account["id"]], calls

    # 进行中不重复起线程
    app._maybe_load_folders(account["id"])
    assert calls == [account["id"]], calls
    app._loading_folder_ids.clear()
    account["server"] = V["server"]


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
    assert not is_disabled(app.start_btn)
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

    # 一级行没有前缀槽，行内控件顺序是 [名称, 尾随箭头]，别再按下标取——点到箭头就把节点折叠了
    synthetic_row = app._folder_nodes[SYNTH]["label_widget"]
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
    """单账户跑完：完成 / 已是最新 / 已停止三种状态文案（账户各自独立，无批量结束事件）。"""
    aid = app._selected_account_id
    app.events.put(("log", "2026-09-11 10:00:00 | INFO    | gui        | _launch_accounts:1 | 测试日志行"))
    app.events.put(("account_done", aid, 1, 12322, 65.0, False, 23))
    pump(app)
    assert "测试日志行" in app.log_text.get("1.0", "end")
    assert "完成：本次备份 1 封" in app.status_var.get() and "用时 1:05" in app.status_var.get(), (
        app.status_var.get())
    assert not is_disabled(app.start_btn)

    app.events.put(("totals", aid, 100, 0))
    app.events.put(("account_done", aid, 0, 100, 3.0, False, 0))
    pump(app)
    assert app.status_var.get() == "已是最新，无待备份邮件", app.status_var.get()

    app.events.put(("account_done", aid, 5, 100, 9.0, True, 20))
    pump(app)
    assert app.status_var.get() == "已停止：本次备份 5 封", app.status_var.get()


def test_config_is_locked_during_a_run(app):
    """树里的箭头与名称是 tk.Label：它有 -state 选项，但置灰和阻断绑定都不生效，
    所以不进 _config_widgets，改由 _set_config_enabled 置灰 + 两个 handler 的「可见账户
    活动中」判定拦截。Entry/Button（含文件夹工具栏）走 state=disabled。后台别的账户在跑、
    当前可见账户空闲时不锁（ADR 0012）。
    """
    aid = app._selected_account_id
    set_phase(app, aid, g.PHASE_BACKING)
    lockable = [w for w, _ in app._config_widgets
                if isinstance(w, (tk.Button, tk.Checkbutton, ttk.Button))]
    assert all(is_disabled(w) for w in lockable), len(app._config_widgets)
    assert bool(app._folder_clickables)
    assert all(str(w.cget("foreground")) == "gray" and str(w.cget("cursor")) == "arrow"
               for w, _ in app._folder_clickables), len(app._folder_clickables)
    assert is_disabled(app.start_btn)
    assert not is_disabled(app.stop_btn)
    assert app.stop_all_btn.winfo_ismapped()   # 有活动账户：「停止所有」出现

    # 锁定期间点击必须无效：勾选不翻转、展开态不变
    states_before = [r["var"].get() for r in app.folder_rows]
    open_before = {key: node["open"] for key, node in app._folder_nodes.items()}
    click(app, app._folder_nodes[("INBOX",)]["rows"][0]["check_btn"])
    click(app, app._folder_nodes[PARENT]["arrow"])
    assert [r["var"].get() for r in app.folder_rows] == states_before
    assert {key: node["open"] for key, node in app._folder_nodes.items()} == open_before

    set_phase(app, aid, g.PHASE_IDLE)
    assert not app.stop_all_btn.winfo_ismapped()  # 活动集合排空：「停止所有」消失
    assert all(str(w.cget("foreground")) == fg and str(w.cget("cursor")) == "hand2"
               for w, fg in app._folder_clickables)

    # 解锁后点击重新生效（点两次还原，避免影响后续断言）
    inbox_btn = app._folder_nodes[("INBOX",)]["rows"][0]["check_btn"]
    click(app, inbox_btn)
    assert [r["var"].get() for r in app.folder_rows] != states_before
    click(app, inbox_btn)
    assert [r["var"].get() for r in app.folder_rows] == states_before
    assert all(not is_disabled(w) for w, _ in app._config_widgets
               if isinstance(w, (tk.Button, tk.Checkbutton, ttk.Button)))
    app._update_start_state()


def test_account_form_enter_binds_to_save(app, monkeypatch):
    """设置页账户表单的输入框绑定 Enter = 保存；保存后触发该账户的静默清单补拉。"""
    box = FakeBox()
    monkeypatch.setattr(g, "messagebox", box)

    # 只查绑定注册，不合成 KP_Enter：合成小键盘事件的语义不确定，断言会空过
    assert all(str(w.bind("<Return>")) != "" and str(w.bind("<KP_Enter>")) != ""
               for w in app._account_form_entries)

    # 缺密码时 _account_credentials 只弹校验提示、不放行（预检/开始都走它把关）
    account = current_account(app)
    cipher = account.pop("password_dpapi", None)
    box.warnings.clear()
    assert app._account_credentials(account) is None
    assert ("信息不完整", "该账户还没有密码，请在设置页的「账户」里填写并保存。") in box.warnings, box.warnings
    if cipher:
        account["password_dpapi"] = cipher   # 原密文放回

    # 保存后静默补拉该账户清单
    loaded: list[str] = []
    monkeypatch.setattr(app, "_maybe_load_folders", lambda aid: loaded.append(aid))
    monkeypatch.setattr(app, "_account_credentials", lambda account, warn=True: {"server": "s"})
    app._on_account_save()
    assert loaded == [account["id"]], loaded


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
    """选中账户活动时开始按钮把绿三角换成透明占位、转圈显示阶段文案；停止后转圈挪到停止钮。

    帧推进（_run_spinner_tick / _set_loading_frame）与 after 调度是分开的，所以第 i 帧长什么样
    可以直接断言，不必 sleep 等定时器。
    """
    aid = app._selected_account_id
    for index in (0, 3, len(g.SPINNER_FRAMES) - 1):
        app._set_loading_frame(app.start_btn, g.RUNNING_BTN_LABEL, index)
        assert app.start_btn.cget("text") == f"{g.SPINNER_FRAMES[index]} {g.RUNNING_BTN_LABEL}"

    # 统计阶段：开始钮转圈文案「正在统计…」、透明占位、禁用；停止钮可用红方块
    set_phase(app, aid, g.PHASE_SCANNING)
    app._stop_run_spinner()
    app._run_spinner_tick(0)
    text = str(app.start_btn.cget("text"))
    assert text.endswith(g.SCANNING_BTN_LABEL) and text[0] in g.SPINNER_FRAMES, text
    assert image_name(app.start_btn) == str(app.blank_icon)  # 绿三角让位给透明占位（撤成 "" 会矮/高一截）
    assert is_disabled(app.start_btn)
    assert not is_disabled(app.stop_btn)

    # 下载阶段：文案换「正在备份…」
    set_phase(app, aid, g.PHASE_BACKING)
    app._stop_run_spinner()
    app._run_spinner_tick(0)
    text = str(app.start_btn.cget("text"))
    assert text.endswith(g.RUNNING_BTN_LABEL) and text[0] in g.SPINNER_FRAMES, text

    # 单账户停止：转圈挪到停止钮，开始钮复位静止
    app._on_stop()
    assert app._state(aid)["run_phase"] == g.PHASE_STOPPING
    assert image_name(app.stop_btn) == str(app.blank_icon)
    assert is_disabled(app.stop_btn)
    assert_start_button_idle(app)

    # 活动集合排空：帧链自愈退出，两按钮复位
    set_phase(app, aid, g.PHASE_IDLE)
    app._run_spinner_tick(2)
    assert_start_button_idle(app)
    assert_stop_button_idle(app)
    assert app._run_spinner_job is None

    # 失败路径同样要走复位（messagebox 换替身，否则会弹真对话框）
    monkeypatch.setattr(g, "messagebox", FakeBox())
    set_phase(app, aid, g.PHASE_BACKING)
    app.events.put(("account_failed", aid, "boom"))
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


def test_wheel_does_nothing_when_the_panel_content_fits(app):
    """内容装得下时滚轮无感：账户列表与邮箱文件夹两块画布都不滚，行停在原地。

    回归自：滚轮入口不判断内容是否高出可视区，而 scrollregion 比视口小时 Tk 不把视图
    原点钳在 0——每格往上跑一个行距，两行账户被推下去、上方露一片对话框灰，yview 却始终
    报 (0.0, 1.0)、滚条也照旧隐藏，用户完全看不出「这里不该滚」（用户截图：账户列表
    可以向上滚）。邮箱文件夹是同一条入口，树短时一样中招。
    """
    app.root.geometry("960x736")
    app.root.update()

    def wheel(canvas, target, delta=-g.WHEEL_DELTA):
        before = canvas.canvasy(0)
        target.event_generate("<MouseWheel>", delta=delta, x=5, y=5)
        app.root.update()
        return canvas.canvasy(0) - before

    def row_visible(canvas, row_frame):
        top = canvas.winfo_rooty()
        return (top <= row_frame.winfo_rooty()
                and row_frame.winfo_rooty() + row_frame.winfo_height() <= top + canvas.winfo_height())

    # 账户列表：一个账户一行，一屏装得下，滚轮上下都不该动
    canvas = app.account_canvas
    assert not app.account_scroll.winfo_ismapped(), "装得下就不该挂滚条"
    row = app._account_rows[current_account(app)["id"]]
    for delta in (-g.WHEEL_DELTA, g.WHEEL_DELTA):
        assert wheel(canvas, row["label"], delta) == 0
        assert wheel(canvas, canvas, delta) == 0
    assert canvas.canvasy(0) == 0 and canvas.yview() == (0.0, 1.0)
    assert row_visible(canvas, row["frame"]), "行还老老实实在第一行"

    # 邮箱文件夹：三行装得下，滚轮落在行名、行容器、画布、常驻滚条上都该空转
    load_folders(app, SHORT_FOLDERS)
    app.root.update()
    folder_canvas = app.folder_canvas
    box = folder_canvas.bbox("all")
    assert box[3] - box[1] <= folder_canvas.winfo_height(), "三行要装得下"
    folder_row = app._folder_nodes[("INBOX",)]["frame"]
    for delta in (-g.WHEEL_DELTA, g.WHEEL_DELTA):
        for target in (app.folder_rows[0]["check_btn"], app.folder_inner,
                       folder_canvas, app.folder_scroll):
            assert wheel(folder_canvas, target, delta) == 0, target
    assert folder_canvas.canvasy(0) == 0 and folder_canvas.yview() == (0.0, 1.0)
    assert row_visible(folder_canvas, folder_row), "第一行还在可视区里"

    clear_folders(app)
    app.root.update()


def test_folder_view_starts_at_the_top_after_switching_accounts(app):
    """换账户（或重拉清单）后文件夹树从顶部开始：旧树的深度与陈旧的滚动范围都不带过来。

    回归自：账户 A 的长树滚到底后切到账户 B 的短树，视图原点按像素留在旧深度、
    scrollregion 还留着旧树的高度（Tk 不在 folder_inner 变矮时送达 <Configure>，
    实测换账户 0 次）——B 的第一行在视口上方两百多像素，滚条滑块挂在 phantom 区域
    底部，向上要滚三十多格才见到内容，事后拖窗口也不自愈。
    """
    keep = list(app.accounts)
    first = current_account(app)
    long_names = flat_folder_names(30)
    short_names = SHORT_FOLDERS
    second = g.new_account()
    second.update({"name": "另一个", "user": "other@example.com", "server": "imap.example.com"})
    app.accounts.append(second)
    app._refresh_account_list()
    try:
        app.root.geometry("960x736")
        app.root.update()
        canvas = app.folder_canvas

        load_folders(app, long_names)                  # 可见账户（first）挂长树
        app.root.update()
        region_h, box_h = folder_scroll_state(canvas)
        assert box_h > canvas.winfo_height(), "长树要装不下"
        assert region_h == box_h, (region_h, box_h)
        canvas.yview_moveto(1)                         # 用户滚到底
        app._wheel_accum = 0
        app.root.update()
        assert canvas.canvasy(0) > 0, "先确认真的滚到底了"

        # 切到短树账户：树从顶部开始，滚动范围就是新树的真实高度（不陈旧），行距仍是实测值
        app._switch_account(second["id"])
        load_folders(app, short_names, account_id=second["id"])
        app.root.update()
        assert canvas.canvasy(0) == 0, canvas.canvasy(0)
        region_h, box_h = folder_scroll_state(canvas)
        assert region_h == box_h, (region_h, box_h)
        assert box_h <= canvas.winfo_height(), "短树装得下"
        frames = app.folder_inner.winfo_children()
        pitch = frames[1].winfo_rooty() - frames[0].winfo_rooty()
        assert pitch > 0 and int(canvas.cget("yscrollincrement")) == pitch, (
            int(canvas.cget("yscrollincrement")), pitch)
        first_row = app._folder_nodes[("INBOX",)]["frame"]
        top = canvas.winfo_rooty()
        assert top <= first_row.winfo_rooty() < top + canvas.winfo_height(), "第一行要在可视区里"

        # 切回长树账户：同样从顶部开始（旧树的深度对另一个账户的树没有意义）
        app._switch_account(first["id"])
        app.root.update()
        assert canvas.canvasy(0) == 0, canvas.canvasy(0)
        assert canvas.bbox("all")[3] > canvas.winfo_height(), "切回来还是那棵长树"

        # 同一个账户重拉清单（手动刷新走同一条路）：深处也回顶部
        canvas.yview_moveto(1)
        app.root.update()
        load_folders(app, long_names)
        app.root.update()
        assert canvas.canvasy(0) == 0, canvas.canvasy(0)
    finally:
        app.accounts[:] = keep
        app._states = {k: v for k, v in app._states.items() if k in {a["id"] for a in keep}}
        app._selected_account_id = first["id"]
        app._ui_settings["selected_account"] = first["id"]
        app._refresh_account_list()
        app._render_account_detail()
        clear_folders(app)
        app.root.update()


def test_folder_scrollbar_cannot_scroll_a_tree_that_fits(app):
    """邮箱文件夹的滚条常驻：树装得下时箭头/滑轨/拖动全部空转，和滚轮同一口径。

    回归自：滚条命令直画布 yview，内容装得下时 Tk 不把视图原点钳在 0——点一下箭头树就
    顺着漂出一片空白。账户列表的滚条装得下时已 grid_remove、事件到不了，只有常驻的
    邮箱文件夹滚条需要这道守卫。
    """
    app.root.geometry("960x736")
    app.root.update()
    canvas = app.folder_canvas
    load_folders(app, SHORT_FOLDERS)
    app.root.update()
    box = canvas.bbox("all")
    assert box[3] - box[1] <= canvas.winfo_height(), "三行要装得下"
    # 走滚条真正的 -command 接线（不直接调方法）：箭头/滑轨/拖动就是这么进来的
    command = app.folder_scroll.cget("command")
    for args in (("scroll", "1", "units"), ("scroll", "-1", "units"), ("moveto", "0.5")):
        app.root.tk.call(command, *args)
        app.root.update()
        assert canvas.canvasy(0) == 0, args

    # 装不下时命令照常转发：滚条还能用
    load_folders(app, flat_folder_names(30))
    app.root.update()
    assert canvas.bbox("all")[3] > canvas.winfo_height(), "三十行装不下"
    app.root.tk.call(command, "scroll", "1", "units")
    app.root.update()
    assert canvas.canvasy(0) > 0

    clear_folders(app)
    app.root.update()


def test_deleting_the_last_account_leaves_no_stale_folder_scroll(app):
    """删掉最后一个账户：文件夹树清空后滚动范围归零、视图回顶，滚条不挂在旧高度上。

    回归自：_render_account_detail 的空分支（_on_account_delete 删掉最后一个账户时走到）
    当场量 bbox 还是旧树的高度——和折叠同一个传播时差，scrollregion 停留在旧长树的高度、
    原点留在旧深度：空树上滚轮还滚得动，滚条滑块还挂在很深的位置。
    """
    app.root.geometry("960x736")
    app.root.update()
    keep = list(app.accounts)
    first = current_account(app)
    try:
        load_folders(app, flat_folder_names(30))
        app.root.update()
        canvas = app.folder_canvas
        assert canvas.bbox("all")[3] > canvas.winfo_height(), "长树要装不下"
        canvas.yview_moveto(1)
        app.root.update()
        assert canvas.canvasy(0) > 0, "先确认真的滚到底了"

        # 与 _on_account_delete 收尾同一序列：删最后一个账户 → 选中清空 → 重画详情
        app.accounts.remove(first)
        app._states.pop(first["id"], None)
        app._selected_account_id = None
        app._ui_settings["selected_account"] = None
        app._render_account_detail()
        app.root.update()
        # 空树的滚动范围直接写零（销毁后内嵌窗口的高度不会自行重算，bbox 仍是旧值，不看它）
        region_h, _box_h = folder_scroll_state(canvas)
        assert region_h == 0, region_h
        assert canvas.canvasy(0) == 0, canvas.canvasy(0)
        # 空树上滚轮与滚条命令都空转——守卫的前提（滚动范围归零）在这里也成立
        app._folder_scroll_command("scroll", 1, "units")
        canvas.event_generate("<MouseWheel>", delta=-g.WHEEL_DELTA, x=5, y=5)
        app.root.update()
        assert canvas.canvasy(0) == 0
    finally:
        app.accounts[:] = keep
        app._selected_account_id = first["id"]
        app._ui_settings["selected_account"] = first["id"]
        app._refresh_account_list()
        app._render_account_detail()
        clear_folders(app)
        app.root.update()


def test_collapsing_a_branch_keeps_the_folder_view_in_range(app):
    """折叠大分支后视图被钳回内容内：不会留在变矮前的深度对着空白。

    回归自：滚到底后折叠一个几十个子文件夹的父节点，folder_inner 变矮不送达 <Configure>，
    scrollregion 停留在旧高度、原点按像素留在旧深度——内容只剩几十像素而原点差着近千像素，
    整棵树都在视口上方，要向上滚三十多格才回来。滚动范围在折叠后按内容重写，Tk 随重写把
    越界的原点钳回内容内：仍溢出时钳到新底部（贴着尾部），装得下时回顶。
    """
    app.root.geometry("960x736")
    app.root.update()
    canvas = app.folder_canvas
    names = [("INBOX", "INBOX.mbox", "INBOX", "INBOX")]
    names += [(f"ZGroup/Leaf{i:02d}", f"ZGroup.Leaf{i:02d}.mbox",
               f"ZGroup/Leaf{i:02d}", f"ZGroup/Leaf{i:02d}") for i in range(30)]
    names += [(f"WGroup/Leaf{i:02d}", f"WGroup.Leaf{i:02d}.mbox",
               f"WGroup/Leaf{i:02d}", f"WGroup/Leaf{i:02d}") for i in range(30)]
    load_folders(app, names)
    app.root.update()
    # 重载保留展开态是既有口径（open_keys 不再是 None），这里自设成首次加载的口径全部展开，
    # 免得用例结果取决于前面谁加载过什么树
    state = app._visible_state()
    state["open_keys"] = None
    app._render_folder_tree(state)
    app.root.update()
    assert canvas.bbox("all")[3] > canvas.winfo_height(), "全展开要装不下"

    # 折掉一组、另一组还撑着：滚动范围跟着变矮，视图贴着新底部（首行滚出视口、末行还在）
    canvas.yview_moveto(1)
    app.root.update()
    assert canvas.canvasy(0) > 0, "先确认真的滚到底了"
    app._toggle_folder_open(("ZGroup",))
    app.root.update()
    region_h, box_h = folder_scroll_state(canvas)
    assert region_h == box_h, (region_h, box_h)
    assert box_h > canvas.winfo_height(), "还剩一组，装不下"
    assert canvas.canvasy(0) > 0, "还贴着尾部，没跳回顶部"
    first_row = app._folder_nodes[("INBOX",)]["frame"]
    assert first_row.winfo_rooty() < canvas.winfo_rooty(), "首行已滚出视口"
    last_row = app._folder_nodes[("WGroup", "Leaf29")]["frame"]
    assert (last_row.winfo_rooty() + last_row.winfo_height()
            <= canvas.winfo_rooty() + canvas.winfo_height()), "最后一行要在可视区里"

    # 再折掉另一组：内容装得下，视图回顶
    app._toggle_folder_open(("WGroup",))
    app.root.update()
    region_h, box_h = folder_scroll_state(canvas)
    assert region_h == box_h, (region_h, box_h)
    assert box_h <= canvas.winfo_height(), "只剩 INBOX 和两个父行，装得下"
    assert canvas.canvasy(0) == 0, canvas.canvasy(0)
    first_row = app._folder_nodes[("INBOX",)]["frame"]
    top = canvas.winfo_rooty()
    assert top <= first_row.winfo_rooty() < top + canvas.winfo_height(), "第一行要在可视区里"

    clear_folders(app)
    app.root.update()


def test_stopping_feedback_covers_buttons_status_line_and_hint(app):
    """「停止中」三处同时换口径：停止按钮接手转圈、开始按钮退回静止外观、状态行清空、提示改文案。

    回归自：点了停止之后开始按钮还在转圈「正在备份…」、停止按钮只是变灰且文案不变、
    第二行还写着「正在停止…」（与按钮重复）——按钮和状态行都没有「我收到了」的反应。
    现在的口径：转圈挪到停止按钮上（红方块换透明占位）、开始按钮复位成静止外观（仍禁用）、
    第二行只清文案（行与版本号留着，收尾出「已停止：…」时不跳版）、配置区提示改口径。
    """
    aid = app._selected_account_id
    set_phase(app, aid, g.PHASE_BACKING)
    app._stop_run_spinner()  # 停掉真实帧链，手动驱动帧（与开始按钮那套用例同法）
    app._set_counts("已备份 3 / 10")
    app._set_status("备份 INBOX（7 封）", error=False)
    counts_before, progress_before = app.count_var.get(), float(app.progress["value"])

    app._on_stop()   # 右下停止只停当前选中账户
    assert app._state(aid)["run_phase"] == g.PHASE_STOPPING and app._stop_events[aid].is_set()
    text = str(app.stop_btn.cget("text"))
    assert text.endswith(g.STOPPING_BTN_LABEL) and text[0] in g.SPINNER_FRAMES, text
    assert image_name(app.stop_btn) == str(app.blank_icon)  # 红方块让位给转圈文字
    assert is_disabled(app.stop_btn)
    assert_start_button_idle(app)                             # 开始按钮：文案与绿三角回位
    assert is_disabled(app.start_btn)     # 但仍不可点（运行还没真正结束）
    assert app.status_var.get() == "" and app._status_full == ""           # 第二行清空
    assert app._msg_bar.winfo_manager() == "pack"             # 行不收回
    assert app.version_label.winfo_manager() == "pack"        # 版本号留在原处
    # 停止这个动作本身不动第一行计数与进度条（工作线程在途的那封照常落盘、计数据实更新，
    # 那是有意义的真实进展，这里不冻结它）
    assert app.count_var.get() == counts_before, app.count_var.get()
    assert float(app.progress["value"]) == progress_before

    # 帧推进落在停止按钮上，开始按钮不再被写文案（全行只剩一个转圈）——走真函数，别抄它的映射
    for index in (2, len(g.SPINNER_FRAMES) - 1):
        app._run_spinner_tick(index)
        assert app.stop_btn.cget("text") == f"{g.SPINNER_FRAMES[index]} {g.STOPPING_BTN_LABEL}"
        assert_start_button_idle(app)

    # 运行真正结束：按钮、状态行全部回位，不残留「正在停止…」
    app.events.put(("account_done", aid, 5, 100, 9.0, True, 20))
    pump(app)
    assert app.running is False
    assert app.status_var.get() == "已停止：本次备份 5 封", app.status_var.get()
    assert_stop_button_idle(app)
    assert_start_button_idle(app)

    # 不在运行时按停止（那时按钮本就禁用）不产生任何变化
    app._on_stop()
    assert app._state(aid)["run_phase"] == g.PHASE_IDLE
    assert_stop_button_idle(app)


def test_stopping_then_failing_restores_the_controls(app, monkeypatch):
    """停止中撞上运行失败：除弹窗外按钮与状态行也要完整复位（messagebox 换替身，别弹真框）。"""
    monkeypatch.setattr(g, "messagebox", FakeBox())
    aid = app._selected_account_id
    set_phase(app, aid, g.PHASE_BACKING)
    app._on_stop()
    assert app._state(aid)["run_phase"] == g.PHASE_STOPPING

    app.events.put(("account_failed", aid, "boom"))
    pump(app)
    assert app.running is False
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
    aid = app._selected_account_id
    app.root.geometry("960x736")
    app.root.update()
    idle = (app.start_btn.winfo_reqheight(), app.stop_btn.winfo_reqheight())
    assert idle[0] == idle[1], idle                    # 同一行两颗按钮本就该等高

    # 运行中：开始按钮的图标槽换成透明占位，高度与静止态一致，同行的停止按钮不被顶高
    set_phase(app, aid, g.PHASE_BACKING)
    app._stop_run_spinner()                            # 停掉真实帧链，免得它把文案改回别的帧
    app.root.update()
    assert image_name(app.start_btn) == str(app.blank_icon), app.start_btn.cget("image")
    # 占位只借高度、不占宽：1px 宽 × 图标高，转圈符才贴近左边框
    assert (app.blank_icon.width(), app.blank_icon.height()) == (1, g._icon_size()), (
        app.blank_icon.width(), app.blank_icon.height())
    assert (app.start_btn.winfo_reqheight(), app.stop_btn.winfo_reqheight()) == idle, (
        app.start_btn.winfo_reqheight(), app.stop_btn.winfo_reqheight())

    # 停止中：转圈换到停止按钮上，两颗按钮的高度仍然一致
    app._on_stop()
    app.root.update()
    assert image_name(app.stop_btn) == str(app.blank_icon)
    assert (app.start_btn.winfo_reqheight(), app.stop_btn.winfo_reqheight()) == idle, (
        app.start_btn.winfo_reqheight(), app.stop_btn.winfo_reqheight())

    set_phase(app, aid, g.PHASE_IDLE)
    assert_stop_button_idle(app)
    assert_start_button_idle(app)


def test_run_buttons_fit_each_state_and_hold_width_across_frames(app):
    """开始/停止按钮宽度逐态贴合文案（不裁切），转圈 10 帧间宽度恒定。

    回归自：正 width=4 是固定文本槽（约 4 个 avgchar ≈ 28px），「⠋ 正在停止…」要约 97px，
    被标签元素裁成「⠋ 正在…」，且按钮不会被撑宽（旧注释误把正 width 当下限）。改负 width 后
    请求宽随文案贴合：每态都与同文案/同图标的临时参照按钮（同主题同 chrome、width=-1）等宽；
    盲文各帧等宽，帧推进按钮不许左右横跳，宽度只在状态切换瞬间变一次。
    """
    app.root.geometry("960x736")

    def fit_width(text, image):
        """同内容参照按钮的贴内容请求宽（量完即销，不留在控件树里）。"""
        ref = ttk.Button(app.root, text=text, image=image, compound="left", width=-1)
        app.root.update_idletasks()
        width = ref.winfo_reqwidth()
        ref.destroy()
        return width

    def assert_hugs(btn, text, image):
        app.root.update_idletasks()
        assert btn.winfo_reqwidth() == fit_width(text, image), (
            text, btn.winfo_reqwidth(), fit_width(text, image))

    aid = app._selected_account_id
    # 静止态：开始钮图标按可用性取彩色/灰度，直接拿它当前挂的图
    assert_hugs(app.start_btn, g.START_BTN_LABEL, app.start_btn.cget("image"))
    assert_hugs(app.stop_btn, g.STOP_BTN_LABEL, app.stop_btn.cget("image"))

    # 统计态：开始钮挂透明占位 +「⠋ 正在统计…」；停止钮彩色方块，两钮都贴各自内容
    set_phase(app, aid, g.PHASE_SCANNING)
    app._stop_run_spinner()
    scanning_text = f"{g.SPINNER_FRAMES[0]} {g.SCANNING_BTN_LABEL}"
    assert str(app.start_btn.cget("text")) == scanning_text
    assert_hugs(app.start_btn, scanning_text, app.blank_icon)
    assert_hugs(app.stop_btn, g.STOP_BTN_LABEL, app.stop_icon)

    # 备份态：开始钮文案换「正在备份…」
    set_phase(app, aid, g.PHASE_BACKING)
    app._stop_run_spinner()
    running_text = f"{g.SPINNER_FRAMES[0]} {g.RUNNING_BTN_LABEL}"
    assert str(app.start_btn.cget("text")) == running_text
    assert_hugs(app.start_btn, running_text, app.blank_icon)
    assert_hugs(app.stop_btn, g.STOP_BTN_LABEL, app.stop_icon)

    # 停止态：转圈挪到停止钮。逐帧量宽——10 帧必须同一个请求宽（帧内零横跳）
    app._on_stop()
    frame_widths = set()
    for index in range(len(g.SPINNER_FRAMES)):
        app._run_spinner_tick(index)
        app.root.update_idletasks()
        frame_widths.add(app.stop_btn.winfo_reqwidth())
        app._stop_run_spinner()   # 取消该帧刚排的下一帧，不留游离 after
    assert len(frame_widths) == 1, frame_widths
    stopping_text = f"{g.SPINNER_FRAMES[0]} {g.STOPPING_BTN_LABEL}"
    assert_hugs(app.stop_btn, stopping_text, app.blank_icon)
    # 动态变宽真实发生：停止态明显宽于静止态，而不是一直被钉在短槽里
    assert app.stop_btn.winfo_reqwidth() > fit_width(g.STOP_BTN_LABEL, app.stop_icon_off) + 20
    # 开始钮已复位静止，依旧贴合
    assert_hugs(app.start_btn, g.START_BTN_LABEL, app.start_btn.cget("image"))

    set_phase(app, aid, g.PHASE_IDLE)
    app.root.update_idletasks()
    assert_hugs(app.start_btn, g.START_BTN_LABEL, app.start_btn.cget("image"))
    assert_hugs(app.stop_btn, g.STOP_BTN_LABEL, app.stop_btn.cget("image"))


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
    # 显式前置：给出确定列宽 + subject 可伸展（不依赖前面用例留下的 reflow 状态）
    for name, width in zip(app.tree["columns"], (160, 320, 110, 140, 90), strict=True):
        app.tree.column(name, width=width, stretch=(name == "subject"))
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
    # 显式前置：确定列宽，避免前序 reflow 把 folder 压到 minwidth、拖不动
    for name, width in zip(app.tree["columns"], (160, 320, 110, 140, 90), strict=True):
        app.tree.column(name, width=width, stretch=(name == "subject"))
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

    # 卡片不再有密码行（密码只在设置页表单里维护）；SSL 是色块 chip，与端口同排在收件服务器行，
    # 放不下时 _layout_conn_row 把它们挪到第二行（见 test_conn_row_wraps_when_tight）
    assert "password" not in app._card_labels
    assert any(isinstance(w, tk.Label) and w.cget("text") == g.MAIL_TYPE_TEXT for w in walk(app.conn_group))
    chip = app._card_labels["ssl"]
    assert chip.cget("text") == g.SSL_CHIP_TEXT
    server_row = int(app._card_labels["server"].grid_info()["row"])
    chip_row = conn_row_of(app, chip)
    assert chip_row in (server_row, server_row + 1), (server_row, chip_row)
    assert conn_row_of(app, app._card_labels["port"]) == chip_row


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


def test_account_row_checkbox_sits_next_to_the_name(app):
    """复选框紧挨名称：控件里那圈给「文字」预留的内边距清零、右侧也不再留白。

    用户口径：原来那条缝太宽。剩下的一点空隙在复选框控件内部（Tk 固定留出），收不掉。
    """
    row = app._account_rows[current_account(app)["id"]]
    app.root.update()   # 上一个用例刚重画完列表，几何要等这一轮布局
    check, label = row["check"], row["label"]
    assert (int(check.cget("padx")), int(check.cget("pady"))) == (0, 0)
    # 控件之间零留白：名称紧贴复选框控件的右缘
    assert label.winfo_rootx() == check.winfo_rootx() + check.winfo_width(), (
        label.winfo_rootx(), check.winfo_rootx(), check.winfo_width())
    # 行自己的左缩 2px（原来是挂在列表容器上的 padx）
    assert check.winfo_rootx() - row["frame"].winfo_rootx() == 2, (
        check.winfo_rootx(), row["frame"].winfo_rootx())


def test_account_list_scrolls_only_when_accounts_do_not_fit(app):
    """账户列表：一屏装得下就没有滚条，装不下才挂出来；滚条贴面板内沿、滚轮一格一行。

    回归自：列表曾是直接打包的 Frame——账户多到装不下就被裁掉，既没有滚条也没有滚轮。
    用例自己收尾：账户表还原成进来时的样子，滚条跟着收起。
    """
    keep = list(app.accounts)
    first = current_account(app)
    app.root.geometry("960x736")
    app.root.update()
    canvas = app.account_canvas
    assert not app.account_scroll.winfo_ismapped(), "账户不多时不该有滚条"

    # 一次加够装不下的行数（行高 + 行自己的 pady=1 上下各一）
    room = canvas.winfo_height()
    pitch = app._account_rows[first["id"]]["frame"].winfo_height() + 2
    for i in range(room // pitch + 3):
        account = g.new_account()
        account.update({"name": f"溢出{i:03d}", "user": f"over{i}@example.com",
                        "server": "imap.example.com"})
        app.accounts.append(account)
    app._refresh_account_list()
    app.root.update()
    assert app.account_list.winfo_reqheight() > room, (app.account_list.winfo_reqheight(), room)
    assert app.account_scroll.winfo_ismapped(), "装不下就该挂滚条"

    border = int(app.account_pane.cget("borderwidth"))
    assert (app.account_pane.winfo_rootx() + app.account_pane.winfo_width()
            - (app.account_scroll.winfo_rootx() + app.account_scroll.winfo_width()) == border), (
        app.account_pane.winfo_rootx(), app.account_pane.winfo_width(),
        app.account_scroll.winfo_rootx(), app.account_scroll.winfo_width(), border)

    row_pitch = app._measure_account_row_pitch()
    assert row_pitch > 0
    assert int(canvas.cget("yscrollincrement")) == row_pitch
    canvas.yview_moveto(0)
    app._wheel_accum = 0
    app.root.update()
    before = canvas.canvasy(0)
    app._account_rows[first["id"]]["label"].event_generate("<MouseWheel>", delta=-g.WHEEL_DELTA, x=5, y=5)
    app.root.update()
    assert canvas.canvasy(0) - before == g.FOLDER_WHEEL_ROWS * row_pitch, (
        canvas.canvasy(0), before, row_pitch)

    # 账户数不动、只把窗口缩小：装不下了滚条要自己冒出来（走的是画布 <Configure> 这条路，
    # 和上面「账户变多」不是同一条）。窗口高度按量到的差值给足余量，不钉像素。
    grew = app.root.winfo_height() + (app.account_list.winfo_reqheight() - canvas.winfo_height()) + 40
    app.root.geometry(f"960x{grew}")
    app.root.update()
    assert not app.account_scroll.winfo_ismapped(), (
        "窗口拉高到装得下，滚条该收起", app.root.winfo_height(), canvas.winfo_height())
    app.root.geometry("960x736")
    app.root.update()
    assert app.account_scroll.winfo_ismapped(), (
        "窗口缩回去，滚条该自己冒出来", app.root.winfo_height(), canvas.winfo_height())

    app.accounts[:] = keep
    app._states = {k: v for k, v in app._states.items() if k in {a["id"] for a in keep}}
    app._selected_account_id = first["id"]
    app._ui_settings["selected_account"] = first["id"]
    app._refresh_account_list()
    app._render_account_detail()
    app.root.update()
    assert app.accounts == keep
    assert not app.account_scroll.winfo_ismapped(), "行收进可视区后滚条该收起"


def test_backup_selected_runs_only_checked_accounts(app, monkeypatch):
    """「备份所选」只跑勾选的账户；一个都没勾时只提示、不进预检。"""
    first = current_account(app)
    second = g.new_account()
    second.update({"name": "不选它", "user": "skip@example.com", "server": "imap.example.com"})
    app.accounts.append(second)
    app._refresh_account_list()
    app._switch_account(first["id"])

    started: list[list[str]] = []
    monkeypatch.setattr(app, "_begin_preflight", started.append)
    monkeypatch.setattr(app, "_account_credentials", lambda account, warn=True: {"server": "s"})
    monkeypatch.setattr(app, "_backup_root_ready", lambda: True)
    box = FakeBox()
    monkeypatch.setattr(g, "messagebox", box)

    app._set_all_account_checks(False)
    app._on_start_selected()
    assert started == []                                    # 一个都没勾：不进预检
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


def test_launch_runs_accounts_concurrently_and_isolates_failures(app, monkeypatch):
    """每账户一线程独立备份：单个账户失败不影响其余；没有批量结束事件（ADR 0012）。"""
    first = g.new_account()
    first.update({"name": "甲", "user": "a@example.com", "server": "imap.example.com", "password": "p"})
    second = g.new_account()
    second.update({"name": "乙", "user": "b@example.com", "server": "imap.example.com", "password": "p"})
    third = g.new_account()
    third.update({"name": "丙", "user": "c@example.com", "server": "imap.example.com", "password": "p"})
    app.accounts.extend([first, second, third])
    calls: list[str] = []

    def fake_backup(account, outdir, eq, log, stop_event):
        calls.append(account["id"])
        if account["id"] == second["id"]:
            raise RuntimeError("boom")
        eq.put(("account_done", account["id"], 1, 0, 0.5, False, 1))

    monkeypatch.setattr(g.threading, "Thread", lambda *a, **k: sync_thread(*a, **k))
    monkeypatch.setattr(app, "_backup_account", fake_backup)
    monkeypatch.setattr(g, "messagebox", FakeBox())   # 失败账户非选中：本来也不弹，双保险
    app._launch_accounts([first["id"], second["id"], third["id"]])
    assert sorted(calls) == sorted([first["id"], second["id"], third["id"]])   # 各起一线，顺序不保证

    events = []
    while True:
        try:
            events.append(app.events.get_nowait())
        except g.queue.Empty:
            break
    kinds = [e[0] for e in events if e[0] != "log"]
    # 三个账户各收到 account_begin；两个 done、一个 failed；不存在 batch_done/account_skipped
    assert kinds.count("account_begin") == 3
    assert kinds.count("account_done") == 2
    assert kinds.count("account_failed") == 1
    assert "batch_done" not in kinds and "account_skipped" not in kinds

    for ev in events:   # 上面 drain 已把事件取空，直接回放给分发器，再刷布局看收口
        app._handle_event(ev)
    app.root.update_idletasks()
    assert not app.running and not app.stop_all_btn.winfo_ismapped()
    assert app._state(second["id"])["result"][0] == "failed"  # 失败红叉持久

    # 「停止所有」给每个活动账户各自的信号：单停一个不影响其余
    for account in (first, second, third):
        app._stop_events[account["id"]] = g.threading.Event()
        set_phase(app, account["id"], g.PHASE_BACKING)
    assert app.stop_all_btn.winfo_ismapped()
    app._on_stop_all()
    assert all(app._stop_events[a["id"]].is_set() for a in (first, second, third))
    assert all(app._state(a["id"])["run_phase"] == g.PHASE_STOPPING for a in (first, second, third))
    assert is_disabled(app.stop_all_btn)
    text = str(app.stop_all_btn.cget("text"))
    assert text.endswith(g.STOPPING_BTN_LABEL) and text[0] in g.SPINNER_FRAMES, text

    # 收尾还原：三账户回空闲，「停止所有」消失
    for account in (first, second, third):
        set_phase(app, account["id"], g.PHASE_IDLE)
    app.accounts[:] = [a for a in app.accounts if a["id"] not in {first["id"], second["id"], third["id"]}]
    for account in (first, second, third):
        app._states.pop(account["id"], None)
    app._refresh_account_list()
    app._on_active_set_changed()


def test_stop_all_does_not_touch_an_unaffected_account(app):
    """右下停止只停选中账户：另一活动账户的信号不被置位、阶段不变。"""
    other = g.new_account()
    other.update({"name": "隔壁", "user": "other@example.com", "server": "imap.example.com", "password": "p"})
    app.accounts.append(other)
    app._refresh_account_list()
    aid = app._selected_account_id
    app._stop_events[aid] = g.threading.Event()
    app._stop_events[other["id"]] = g.threading.Event()
    set_phase(app, aid, g.PHASE_BACKING)
    set_phase(app, other["id"], g.PHASE_BACKING)

    app._on_stop()   # 右下停止：只停当前选中账户
    assert app._stop_events[aid].is_set()
    assert not app._stop_events[other["id"]].is_set()
    assert app._state(other["id"])["run_phase"] == g.PHASE_BACKING
    assert app.running and app.stop_all_btn.winfo_ismapped()   # 另一账户仍在跑，按钮不消失

    set_phase(app, aid, g.PHASE_IDLE)
    set_phase(app, other["id"], g.PHASE_IDLE)
    app.accounts.remove(other)
    app._states.pop(other["id"], None)
    app._refresh_account_list()


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
    monkeypatch.setattr(g.core, "scan_folder",
                        lambda *a, **k: g.core.FolderScan({"<m@x>": (1, 10)}, None))

    def fake_scan_file(label, *_a, **_k):
        scanned["label"] = label
        return {}

    monkeypatch.setattr(g.core, "scan_file", fake_scan_file)
    monkeypatch.setattr(g.core, "scan_eml_dir", lambda *a, **k: {})
    monkeypatch.setattr(g.core, "ensure_basedir", lambda *a, **k: None)
    monkeypatch.setattr(g.core, "create_folder_structure",
                        lambda names, basedir, account: scanned.setdefault("dir", account))
    monkeypatch.setattr(g.core, "download_messages", lambda *a, **k: None)

    app._backup_account(account, str(tmp_path), app.events, logging.getLogger("test"),
                        g.threading.Event())
    assert scanned["dir"] == "a@example.com", scanned
    assert scanned["label"] == "a@example.com/INBOX.mbox", scanned
    assert "工作邮箱" not in str(scanned)


def test_backup_account_continues_after_a_folder_fetch_failure(app, monkeypatch, tmp_path):
    """FolderFetchError 只算一个文件夹失败（规格 §5.3），不能升级为账户失败。"""
    account = {"id": "acc-fetch", "name": "工作邮箱", "user": "a@example.com", "server": "imap.example.com",
               "ssl": True, "port": "993", "password": "p", "folders": ["INBOX", "ZZ"],
               "mbox": True, "eml": False}
    calls: list[str] = []

    class FakeServer:
        def select(self, *_args, **_kwargs):
            return "OK", b""

        def logout(self):
            pass

    monkeypatch.setattr(g.core, "connect_and_login", lambda cfg, log: FakeServer())
    monkeypatch.setattr(g.core, "get_names", lambda *a, **k: [
        ("INBOX", "INBOX.mbox", "INBOX", "INBOX"), ("ZZ", "ZZ.mbox", "ZZ", "ZZ")])
    monkeypatch.setattr(g.core, "scan_folder",
                        lambda *a, **k: g.core.FolderScan({"<m@x>": (1, 10)}, None))
    monkeypatch.setattr(g.core, "scan_file", lambda *a, **k: {})
    monkeypatch.setattr(g.core, "ensure_basedir", lambda *a, **k: None)
    monkeypatch.setattr(g.core, "create_folder_structure", lambda *a, **k: None)

    def fake_download(_server, label, *_a, **_k):
        calls.append(label)
        if label.endswith("INBOX.mbox"):
            raise g.core.FolderFetchError("every UID in a FETCH batch was rejected by the server")

    monkeypatch.setattr(g.core, "download_messages", fake_download)

    app._backup_account(account, str(tmp_path), app.events, logging.getLogger("test"),
                        g.threading.Event())
    assert calls == ["a@example.com/INBOX.mbox", "a@example.com/ZZ.mbox"], calls


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


def test_account_form_password_mask_reflects_the_saved_password(app):
    """密码框不回显内容：已存密码的账户显示固定掩码（不代表位数），没有密码的账户是空框。"""
    saved = dict(current_account(app), password_dpapi="cipher")
    app._load_account_form(saved)
    assert app._acct_password_var.get() == g.PASSWORD_MASK_TEXT
    assert app._acct_password_entry.get() == g.PASSWORD_MASK_TEXT

    plain = {key: value for key, value in saved.items() if key != "password_dpapi"}
    app._load_account_form(plain)
    assert app._acct_password_var.get() == ""

    app._load_account_form(current_account(app))   # 表单复位到当前选中账户


def test_password_box_saves_keeps_and_clears(app):
    """密码框三态保存语义：掩码原样 = 不修改，清空 = 删除已存密码，其余 = 写入新密码。"""
    if not DPAPI_OK:
        pytest.skip("非 Windows 没有可用的加密密码")
    account = g.new_account()
    account.update({"name": "密码语义", "user": "pw@example.com", "server": "imap.pw.com", "port": "993"})
    app.accounts.append(account)
    app._refresh_account_list()
    app._refresh_edit_list(account["id"])
    assert app._editing_account_id == account["id"]

    app._acct_password_var.set("新密码-1")
    app._on_account_save()
    assert g._dpapi_decrypt(account["password_dpapi"]) == "新密码-1"
    assert app._acct_password_var.get() == g.PASSWORD_MASK_TEXT, "保存后按新状态回填掩码"

    cipher = account["password_dpapi"]
    app._on_account_save()                                     # 掩码原样：一个字节都不动
    assert account["password_dpapi"] == cipher

    app._acct_password_var.set("")
    app._on_account_save()                                     # 清空 = 删除已存密码
    assert "password_dpapi" not in account

    app.accounts.remove(account)
    app._states.pop(account["id"], None)
    app._refresh_account_list()
    app._refresh_edit_list(current_account(app)["id"])


def test_password_mask_guards_typing_and_paste(app):
    """掩码保护：会改文本的动作先整框清空，导航键放行——免得在掩码中间拼出假密码。"""
    entry = app._acct_password_entry
    app._acct_password_var.set(g.PASSWORD_MASK_TEXT)

    app._on_acct_password_key(SimpleNamespace(keysym="Left", char=""))      # 导航键：掩码不动
    assert entry.get() == g.PASSWORD_MASK_TEXT
    app._on_acct_password_key(SimpleNamespace(keysym="a", char="a"))        # 打字：先清空再落字符
    assert entry.get() == ""
    app._acct_password_var.set(g.PASSWORD_MASK_TEXT)
    app._on_acct_password_key(SimpleNamespace(keysym="BackSpace", char="\x08"))
    assert entry.get() == ""
    app._acct_password_var.set(g.PASSWORD_MASK_TEXT)
    app._on_acct_password_clip()                                           # 粘贴/剪切：先清空
    assert entry.get() == ""

    # 不是掩码的内容（用户自己敲的）概不插手
    app._acct_password_var.set("typed")
    app._on_acct_password_key(SimpleNamespace(keysym="a", char="a"))
    app._on_acct_password_clip()
    assert entry.get() == "typed"
    app._acct_password_var.set("")


def test_settings_section_titles_use_a_bigger_bold_font(app):
    """设置页分类标题：加粗、比正文大 HEADING_SIZE_DELTA 磅（拉开层级，用户口径）。"""
    def walk(widget):
        yield widget
        for child in widget.winfo_children():
            yield from walk(child)

    base_size = abs(int(tkfont.nametofont("TkDefaultFont").cget("size")))
    for key, title in g.SETTINGS_SECTIONS:
        label = next(w for w in walk(app._settings_sections[key])
                     if isinstance(w, tk.Label) and w.cget("text") == title)
        font = tkfont.Font(font=label.cget("font"))
        assert int(font.cget("size")) == base_size + g.HEADING_SIZE_DELTA, (title, font.cget("size"))
        assert str(font.cget("weight")) == "bold", title


def test_settings_detail_padding_matches_the_8px_gap(app):
    """设置页右详情四边内边距 = 面板间距 8px：内容留白与栏间缝共用同一条基线。"""
    for key, title in g.SETTINGS_SECTIONS:
        body = app._settings_sections[key].winfo_children()[0]
        info = body.pack_info()
        assert (int(info["padx"]), int(info["pady"])) == (8, 8), (title, info)


def test_account_form_follows_the_mail_client_layout(app):
    """账户表单照 foxmail 的连接设置：类型置顶，收件服务器与 SSL、端口同一行，密码提示已撤。"""
    def walk(widget):
        yield widget
        for child in widget.winfo_children():
            yield from walk(child)

    section = app._settings_sections[g.SETTINGS_ACCOUNTS_SECTION]
    captions = ("邮箱类型：", "名称：", "邮箱地址：", "收件服务器：", "SSL", "端口：", "密码：")
    widgets = {}
    for widget in walk(section):
        if isinstance(widget, (tk.Label, tk.Checkbutton)) and str(widget.cget("text")) in captions:
            widgets[str(widget.cget("text"))] = widget
    assert set(widgets) == set(captions), sorted(widgets)
    rows = {name: int(widget.grid_info()["row"]) for name, widget in widgets.items()}
    assert rows["邮箱类型："] < rows["名称："] < rows["邮箱地址："] < rows["收件服务器："]
    assert rows["收件服务器："] == rows["SSL"] == rows["端口："] < rows["密码："]
    assert any(isinstance(w, tk.Label) and w.cget("text") == g.MAIL_TYPE_TEXT for w in walk(section))
    assert not any(isinstance(w, tk.Label) and "留空" in str(w.cget("text")) for w in walk(section))
    # 输入框顺序不变：名称 / 邮箱地址 / 收件服务器 / 端口 / 密码（Enter 保存与右键菜单靠它索引）
    assert int(app._account_form_entries[2].grid_info()["row"]) == rows["收件服务器："]
    assert int(app._account_form_entries[3].grid_info()["row"]) == rows["收件服务器："]


def test_top_columns_split_three_to_two(app):
    """上部两栏固定 3:2（账户信息:邮箱文件夹，用户选定）：小窗不塌、大窗照比例。"""
    for width in (800, 960, 1600):
        app.root.geometry(f"{width}x736")
        app.root.update()
        left = app._left_col.winfo_width()
        folder = app.folder_group.winfo_width()
        inner = left + folder    # 净宽：两栏之间那条 TOP_COLUMN_GAP 不算在比例里
        assert abs(left - round(inner * g.TOP_LEFT_SHARE)) <= 2, (width, left, folder)
        assert left > folder, (width, left, folder)   # 账户信息栏拿 3 份，文件夹栏 2 份
    app.root.geometry("960x736")
    app.root.update()


def test_card_content_is_left_aligned_and_values_line_up(app):
    """两卡内容贴左显示、右侧留白（用户选定）；首行值与邮箱地址值同列起排（4px 回归）。"""
    app.root.update()
    for group, inner in ((app.conn_group, app._card_inners[0]), (app._output_group, app._card_inners[1])):
        left_gap = inner.winfo_rootx() - group.winfo_rootx()
        right_gap = (group.winfo_rootx() + group.winfo_width()) - (inner.winfo_rootx() + inner.winfo_width())
        assert left_gap <= g.CARD_PAD + 4, (str(group), left_gap)     # 贴左，不居中
        assert right_gap > left_gap, (str(group), left_gap, right_gap)  # 右侧留白
    type_label = next(w for w in app._card_inners[0].winfo_children()
                      if isinstance(w, tk.Label) and str(w.cget("text")) == g.MAIL_TYPE_TEXT)
    assert type_label.winfo_rootx() == app._card_labels["user"].winfo_rootx(), "首行值漏 padx=4 的回归"


def test_card_value_columns_align_across_cards(app):
    """「账户信息」与「输出」的值列起点同一条竖线（用户选定「统一到同一条线」）。"""
    app.root.update()
    assert app._card_labels["user"].winfo_rootx() == app.card_root_label.winfo_rootx()


def test_folder_card_bottom_matches_output_card(app):
    """「邮箱文件夹」底边与「输出」底边同线：place 级定高回归（grid 传播会吃掉 configure 的高度）。"""
    for width, height in ((800, 620), (960, 736), (1600, 900)):
        app.root.geometry(f"{width}x{height}")
        app.root.update()
        folder_bottom = app.folder_group.winfo_rooty() + app.folder_group.winfo_height()
        output_bottom = app._output_group.winfo_rooty() + app._output_group.winfo_height()
        assert abs(folder_bottom - output_bottom) <= 2, (width, folder_bottom, output_bottom)
    app.root.geometry("960x736")
    app.root.update()


def test_ssl_chip_renders_and_toggles(app):
    """SSL 色块 chip：备份卡随账户切配色；设置表单的 chip 点一下翻开关并联动默认端口。"""
    account = current_account(app)
    chip = app._card_labels["ssl"]
    assert chip.cget("text") == g.SSL_CHIP_TEXT

    account["ssl"] = True
    app._render_card(account)
    assert chip.cget("background") == g.SSL_CHIP_ON_BG
    account["ssl"] = False
    app._render_card(account)
    assert chip.cget("background") == g.SSL_CHIP_OFF_BG
    account["ssl"] = True
    app._render_card(account)

    # 设置表单：chip 可点击切换（command 语义与原 Checkbutton 一致）
    app._load_account_form(account)
    form_chip = app._acct_ssl_chip
    assert app._acct_ssl_var.get() is True
    assert form_chip.cget("background") == g.SSL_CHIP_ON_BG
    app._acct_port_var.set("993")
    app._on_acct_ssl_chip_click()
    assert app._acct_ssl_var.get() is False
    assert form_chip.cget("background") == g.SSL_CHIP_OFF_BG
    assert app._acct_port_var.get() == "143"
    app._on_acct_ssl_chip_click()
    assert app._acct_ssl_var.get() is True
    assert app._acct_port_var.get() == "993"
    app._load_account_form(account)   # 还原表单


def test_conn_row_wraps_when_tight(app, monkeypatch):
    """服务器行放不下就换行：SSL/端口进独立第二行紧凑排一排，放宽回到同一行（双向 + 迟滞）。

    两栏 3:2 后账户信息列变宽，最小窗 800px 也放得下——用 CARD_CONTENT_MAX_WIDTH 顶小
    来强制进换行态，验的是排位与迟滞机制本身。
    """
    chip, port = app._card_labels["ssl"], app._card_labels["port"]
    caption = app._conn_server_spec["caption"]
    wrap = app._conn_wrap_row
    orig_max = g.CARD_CONTENT_MAX_WIDTH
    pad_w = sum(sum(p) for p in (g.CHIP_PADX_SAME_ROW, g.PORT_CAPTION_PADX_SAME_ROW,
                                g.PORT_LABEL_PADX_SAME_ROW))
    needed = (caption.winfo_reqwidth()
              + sum(w.winfo_reqwidth() for w in (chip, app._conn_row["port_caption"], port))
              + pad_w + g.SERVER_MIN_BUDGET)

    monkeypatch.setattr(g, "CARD_CONTENT_MAX_WIDTH", 200)   # 远小于 needed：必须换行
    app._fit_card_values()
    app.root.update_idletasks()
    assert app._conn_wrapped, "放不下就该换行"
    server_row = int(app._card_labels["server"].grid_info()["row"])
    assert chip.winfo_manager() == "pack" and port.winfo_manager() == "pack", "换行后进独立第二行容器"
    assert wrap.grid_info() and int(wrap.grid_info()["row"]) == server_row + 1
    assert chip.winfo_x() < app._conn_row["port_caption"].winfo_x() < port.winfo_x(), "第二行紧凑排一排"
    assert wrap.winfo_reqwidth() < 200, wrap.winfo_reqwidth()   # 不吃第一行的列宽，整排短

    # 迟滞带：富余不足 WRAP_HYSTERESIS 就不急着换回来（临界宽度来回拖不闪跳）
    monkeypatch.setattr(g, "CARD_CONTENT_MAX_WIDTH", needed + g.WRAP_HYSTERESIS - 1)
    app._fit_card_values()
    assert app._conn_wrapped, "富余不足迟滞带，维持换行"
    monkeypatch.setattr(g, "CARD_CONTENT_MAX_WIDTH", needed + g.WRAP_HYSTERESIS)
    app._fit_card_values()
    assert not app._conn_wrapped, "富余够了才回到同一行"
    server_row = int(app._card_labels["server"].grid_info()["row"])
    assert int(chip.grid_info()["row"]) == server_row and int(port.grid_info()["row"]) == server_row

    monkeypatch.setattr(g, "CARD_CONTENT_MAX_WIDTH", orig_max)   # 还原，给后面的用例留干净状态
    app._fit_card_values()
    assert not app._conn_wrapped


def test_settings_sash_is_on_top_and_draggable(app, monkeypatch):
    """设置页左栏手柄压在空隙带上不被盖住（z-order 回归）、隐形同底色，拖动改宽并落盘。"""
    grip = app._sash_grips["settings"]
    app._show_page(g.PAGE_SETTINGS)
    app.root.update()
    assert hit_test(app, grip) is grip, "手柄被别的控件盖住就点不到"
    assert str(grip.cget("background")) == str(grip.master.cget("background")), "手柄与空隙同色：隐形"

    saved: list[int] = []
    monkeypatch.setattr(app, "_save_column_config", lambda: saved.append(1))
    before = int(app._settings_nav_frame.cget("width"))
    app._sash_start(SimpleNamespace(x_root=100), "settings")
    app._sash_drag(SimpleNamespace(x_root=130), "settings")
    app.root.update()
    # 上限是动态的（右侧内容最小完整宽反推，见 _sash_max_width），硬上限 SIDEBAR_MAX_WIDTH 封顶
    assert int(app._settings_nav_frame.cget("width")) == min(before + 30, app._sash_max_width("settings"))
    assert app._sash_max_width("settings") <= g.SIDEBAR_MAX_WIDTH
    app._sash_end(None, "settings")
    assert saved, "拖完要落盘"
    app._apply_sidebar_width("settings", g.SIDEBAR_DEFAULT_WIDTH)
    app._show_page(g.PAGE_BACKUP)
    app.root.update()


def test_account_edit_list_has_sash_and_smart_scroll(app, monkeypatch):
    """设置页「账户」列表：手柄独立拖宽（不牵动设置左栏），滚条按内容显隐（智能滚条）。"""
    app._show_page(g.PAGE_SETTINGS)
    app.root.update()
    grip = app._sash_grips["account"]
    assert hit_test(app, grip) is grip, "账户列表手柄同样不能被 Listbox 盖住"
    assert str(grip.cget("background")) == str(grip.master.cget("background")), "手柄与空隙同色：隐形"

    saved: list[int] = []
    monkeypatch.setattr(app, "_save_column_config", lambda: saved.append(1))
    nav_before = int(app._settings_nav_frame.cget("width"))
    before = int(app._account_nav_frame.cget("width"))
    app._sash_start(SimpleNamespace(x_root=100), "account")
    app._sash_drag(SimpleNamespace(x_root=124), "account")
    app.root.update()
    after = int(app._account_nav_frame.cget("width"))
    assert after == min(before + 24, app._sash_max_width("account")), (before, after)
    assert int(app._settings_nav_frame.cget("width")) == nav_before, "账户列表拖宽不该牵动设置左栏"
    app._sash_end(None, "account")
    assert saved, "拖完要落盘"
    app._apply_sidebar_width("account", g.SIDEBAR_DEFAULT_WIDTH)

    # 智能滚条：内容装不下才挂出来，装下就收走
    lst = app.account_edit_list
    assert not app._account_edit_scroll_shown
    for i in range(60):
        lst.insert("end", f"滚动条用的临时账户{i}")
    app._sync_account_edit_scroll()
    assert app._account_edit_scroll_shown, "60 行装不下就该挂滚条"
    lst.delete(0, "end")
    app._refresh_edit_list(app._editing_account_id)   # 还原成真实账户清单
    assert not app._account_edit_scroll_shown, "内容装下了滚条要收走"
    app._show_page(g.PAGE_BACKUP)
    app.root.update()


def test_sash_sits_in_the_gap_band(app):
    """手柄压在「侧栏右边框与右侧内容之间」的空隙带上（用户选定），不再贴在侧栏内容上。"""
    app.root.update()
    for kind, target in (("backup", app.account_pane), ("settings", app._settings_nav_frame),
                         ("account", app._account_nav_frame)):
        grip = app._sash_grips[kind]
        assert grip.master is not target, kind        # 不挂在侧栏里边（别再压出灰条）
        assert grip.winfo_rootx() == target.winfo_rootx() + target.winfo_width(), kind   # 紧贴右边框、落在空隙上
        assert 6 <= grip.winfo_width() <= 12, kind    # 手柄宽 = 空隙带宽（8/8/10px）
    # 命中测试要各自页面可见时做：两页同窗叠放，被盖住的页面量不到自己
    app._show_page(g.PAGE_BACKUP)
    app.root.update()
    assert hit_test(app, app._sash_grips["backup"]) is app._sash_grips["backup"]
    app._show_page(g.PAGE_SETTINGS)
    app.root.update()
    assert hit_test(app, app._sash_grips["settings"]) is app._sash_grips["settings"]
    assert hit_test(app, app._sash_grips["account"]) is app._sash_grips["account"]
    app._show_page(g.PAGE_BACKUP)
    app.root.update()


def test_backup_sash_keeps_the_folder_toolbar_visible(app, monkeypatch):
    """拖宽账户栏/窗口收窄都不得把「邮箱文件夹」工具栏（尤其右上 ⟳）挤没。

    上限按工具栏实测请求宽反推（_backup_detail_min_width）：先前只按 BACKUP_DETAIL_MIN_WIDTH
    反推，拖宽后 ⟳ 被 pack 挤成一条竖缝（用户截图）。
    """
    monkeypatch.setattr(app, "_save_column_config", lambda: None)

    def toolbar_fits():
        app.root.update()
        card, bar = app.folder_group, app._folder_toolbar
        assert bar.winfo_reqwidth() <= bar.winfo_width(), (bar.winfo_reqwidth(), bar.winfo_width())
        edge = card.winfo_rootx() + card.winfo_width()
        assert app.reload_btn.winfo_rootx() + app.reload_btn.winfo_reqwidth() <= edge, "⟳ 被挤出卡片"

    app.root.geometry("800x736")   # 窄窗最能暴露问题
    app.root.update()
    app._sash_start(SimpleNamespace(x_root=100), "backup")
    app._sash_drag(SimpleNamespace(x_root=2000), "backup")   # 一把拉到底
    app._sash_end(None, "backup")
    assert int(app.account_pane.cget("width")) == app._sash_max_width("backup"), "拖拽上限按右栏最小完整宽钳"
    toolbar_fits()

    # 窗口收窄同样要护住：实际宽度自动收、用户选定的宽不动（拉宽后自动回来）
    app._apply_sidebar_width("backup", g.SIDEBAR_MAX_WIDTH)
    app.root.geometry("800x736")
    toolbar_fits()
    assert int(app.account_pane.cget("width")) < g.SIDEBAR_MAX_WIDTH, "窄窗下账户栏要自动收窄"
    app.root.geometry("1600x736")
    app.root.update()
    assert int(app.account_pane.cget("width")) == g.SIDEBAR_MAX_WIDTH, "窗口拉宽回到用户选定的宽"
    toolbar_fits()

    app.root.geometry("960x736")
    app.root.update()
    app._apply_sidebar_width("backup", g.SIDEBAR_DEFAULT_WIDTH)


def test_button_gaps_are_uniform(app):
    """按钮排里相邻按钮的间隔统一为 BTN_GAP（基准 = 备份页账户列表 全选/反选/清空，用户选定）。"""
    app.root.update()

    def button_row(*texts):
        """按钮文案恰好是 texts（视觉从左到右）的那一排按钮；文案要能唯一定位那排。"""
        stack = [app.root]
        while stack:
            widget = stack.pop()
            stack.extend(widget.winfo_children())
            if not isinstance(widget, tk.Frame):
                continue
            btns = sorted((w for w in widget.winfo_children()
                           if isinstance(w, (tk.Button, ttk.Button)) and str(w.cget("text")) in texts),
                          key=lambda b: b.winfo_rootx())
            if [str(b.cget("text")) for b in btns] == list(texts):
                return btns
        raise AssertionError(f"找不到按钮排: {texts}")

    def gaps_of(*texts, exact=True):
        btns = button_row(*texts)
        gaps = [b.winfo_rootx() - (a.winfo_rootx() + a.winfo_width()) for a, b in pairwise(btns)]
        if exact:
            assert all(gap == g.BTN_GAP for gap in gaps), (texts, gaps)
        return btns, gaps

    gaps_of("全选", "反选", "清空")                       # 账户列表工具排（基准）
    gaps_of("新增", "删除")                              # 设置页「账户」列表工具排
    gaps_of(g.START_BTN_LABEL, g.STOP_BTN_LABEL)         # 底栏 开始备份/停止
    # 文件夹工具栏：左排按钮之间精确 BTN_GAP；⟳ 右对齐（右上角，用户选定），与左排之间是弹性余量、只保下限
    gaps = gaps_of("全选", "清空", "仅收件箱", g.RELOAD_GLYPH, exact=False)[1]
    assert gaps[0] == g.BTN_GAP and gaps[1] == g.BTN_GAP, gaps
    assert gaps[2] >= g.BTN_GAP, gaps


def test_dialogs_are_centered_on_root(app):
    """自绘弹窗一律落在主窗口正中（_center_on_root：预检/密码补录/关闭询问共用）。

    客户区中心精确重合才算过：geometry 落外框、winfo_rootx 量客户区，装饰偏移补不掉就会偏一截。
    """
    win = tk.Toplevel(app.root)
    win.resizable(False, False)
    tk.Frame(win, width=240, height=120).pack()
    app._center_on_root(win)
    app.root.update()
    err_x = ((win.winfo_rootx() + win.winfo_width() // 2)
             - (app.root.winfo_rootx() + app.root.winfo_width() // 2))
    err_y = ((win.winfo_rooty() + win.winfo_height() // 2)
             - (app.root.winfo_rooty() + app.root.winfo_height() // 2))
    assert abs(err_x) <= 2 and abs(err_y) <= 2, (err_x, err_y)
    win.destroy()


def test_backup_root_row_is_capped(app):
    """设置页「备份」的 path 行封顶 ACCOUNT_FORM_MAX_WIDTH：最大化也不拉成一条长线。"""
    app.root.geometry("1600x736")
    app.root.update()
    app._show_settings_section(g.SETTINGS_BACKUP_SECTION)
    app.root.update()
    width = app._backup_root_row.winfo_width()
    assert 100 < width <= g.ACCOUNT_FORM_MAX_WIDTH, width
    app.root.geometry("960x736")
    app.root.update()


def test_refresh_button_reloads_folders(app, monkeypatch):
    """⟳ 强制重拉：快照命中也照样拉（静默去重对它是例外），补录记忆一并清掉。"""
    account = current_account(app)
    state = app._state(account["id"])
    calls: list[str] = []
    monkeypatch.setattr(g.threading, "Thread", sync_thread)
    monkeypatch.setattr(app, "_folder_worker", lambda *a: calls.append(a[0]))
    app._loading_folder_ids.clear()
    state["snapshot"] = app._account_snapshot(account)
    state["password_prompted"] = state["snapshot"]

    app._on_refresh_folders()
    assert calls == [account["id"]]
    assert state["snapshot"] is None
    assert state["password_prompted"] is None
    app._loading_folder_ids.clear()
    app._interactive_folder_loads.clear()
    # 还原快照：后续用例切回该账户时按快照去重，不会原地起一次多余的重拉
    state["snapshot"] = app._account_snapshot(account)


def test_folder_load_failure_shows_hint_and_prompts_password(app, monkeypatch):
    """加载失败：面板提示可刷新重试；密码补录窗只挂显式动作——被动拉取的认证失败只留提示，
    手动刷新（唯一的交互式拉取入口）发起的失败才弹补录；同快照不重复弹；补完自动重拉。"""
    account = current_account(app)
    state = app._state(account["id"])
    prompts: list[str] = []
    answers: list = []
    loads: list[str] = []
    monkeypatch.setattr(g.threading, "Thread", sync_thread)
    monkeypatch.setattr(app, "_folder_worker", lambda *a: loads.append(a[0]))
    monkeypatch.setattr(app, "_ask_password", lambda aid: prompts.append(aid) or answers.pop(0))
    app._loading_folder_ids.clear()
    app._interactive_folder_loads.clear()
    state["folder_rows"] = []
    app.folder_rows = []      # 树上没内容才显示提示（失败提示不叠在已加载的树上）

    # 被动拉取的认证失败：只提示不弹窗（补录窗只跟显式动作；消息前缀与 core 的口径一致）
    app._on_folder_load_failed(account["id"], "IMAP authentication failed: bad credentials")
    assert app.folder_ph.winfo_manager() == "grid"
    assert str(app.folder_ph.cget("text")).startswith("文件夹清单加载失败")
    app.root.update()
    assert prompts == []

    # 手动刷新发起的交互式拉取认证失败：弹补录（无密码的平台上弹窗在刷新当刻、有密码的平台
    # 在失败当刻，殊途同归弹一次）；同快照不重复弹
    answers.append(None)
    app._on_refresh_folders()
    app._loading_folder_ids.clear()
    app._on_folder_load_failed(account["id"], "IMAP authentication failed: bad credentials")
    app.root.update()
    assert prompts == [account["id"]]
    app._interactive_folder_loads.add(account["id"])   # 模拟同一轮交互拉取的再次失败
    app._on_folder_load_failed(account["id"], "IMAP authentication failed: bad credentials")
    app.root.update()
    assert prompts == [account["id"]], "同快照只弹一次"

    # 非认证失败：只提示不弹窗
    app._on_folder_load_failed(account["id"], "OSError: network is unreachable")
    app.root.update()
    assert len(prompts) == 1

    # 手动刷新清掉记忆：失败后又弹
    answers.append(None)
    app._on_refresh_folders()
    app._loading_folder_ids.clear()
    app._on_folder_load_failed(account["id"], "IMAP authentication failed: bad credentials")
    app.root.update()
    assert prompts == [account["id"], account["id"]]

    # 补录拿到密码 → 自动强制重拉（勾了保存就落密文）
    answers.append(("new-pass", True))
    loads.clear()
    app._loading_folder_ids.clear()
    app._ask_folder_password(account["id"])
    assert loads == [account["id"]]
    if DPAPI_OK:
        assert current_account(app)["password_dpapi"]
    assert state["password_prompted"] is None

    # 缺密码的账户：被动拉取只提示不弹窗；手动刷新（显式动作）才弹补录
    prompts.clear()
    answers.append(None)
    cipher = current_account(app).pop("password_dpapi", None)
    app._run_passwords.pop(account["id"], None)
    app._loading_folder_ids.clear()
    app._interactive_folder_loads.clear()
    state["snapshot"] = None
    state["password_prompted"] = None
    app._maybe_load_folders(account["id"])
    assert str(app.folder_ph.cget("text")) == g.FOLDER_LOAD_NO_PASSWORD_HINT
    app.root.update()
    assert prompts == [], "被动拉取不弹补录窗"
    app._on_refresh_folders()
    app.root.update()
    assert prompts == [account["id"]], "手动刷新才弹补录窗"
    if cipher:
        current_account(app)["password_dpapi"] = cipher
    app._interactive_folder_loads.clear()
    # 还原快照：后续用例切回该账户时按快照去重，不会原地起一次多余的重拉
    state["snapshot"] = app._account_snapshot(account)
    app._render_account_detail()


def test_long_names_are_ellipsized_with_tip(app):
    """长名称截断 + 悬停看全文：文件夹名与账户名同一口径，拉宽后自动恢复全文（双向）。"""
    account = current_account(app)
    account["name"] = "一个稍长的账户名称十二字"
    app._refresh_account_list()
    app.root.update()
    app._fit_account_names()
    row = app._account_rows[account["id"]]
    assert row["text_shown"] != row["text_full"]
    assert row["text_shown"].endswith(g.ELLIPSIS)
    assert row["label"].cget("text") == row["text_shown"]
    assert row["tip_full"] == row["text_full"]

    app._apply_sidebar_width("backup", g.SIDEBAR_MAX_WIDTH)
    app.root.update()
    app._fit_account_names()
    assert row["label"].cget("text") == row["text_full"], "拉宽后恢复全文"
    app._apply_sidebar_width("backup", g.SIDEBAR_DEFAULT_WIDTH)

    # 两段就够长：800px（文件夹栏 2/5）下必截断、1600px 下恢复全文；三段在 3:2 分栏后
    # 连 1600px 也放不下，那验的就不是「变宽恢复」而是常态截断了
    long_display = "一个非常非常长的邮箱文件夹名称" * 2
    load_folders(app, [("LongName", "LongName.mbox", long_display, long_display)])
    app.root.geometry("800x736")
    app.root.update()
    app._fit_folder_names()
    node = app._folder_nodes[(long_display,)]
    label = node["label_widget"]
    assert node["text_shown"] != node["text_full"]
    assert node["text_shown"].endswith(g.ELLIPSIS)
    assert label.cget("text") == node["text_shown"]
    assert node["tip_full"] == long_display

    app.root.geometry("1600x736")
    app.root.update()
    app._fit_folder_names()
    assert label.cget("text") == node["text_full"], "树区变宽后恢复全文"

    # 还原模块状态：几何回默认、文件夹树回到标准数据集
    app.root.geometry("960x736")
    load_folders(app, NAMES)
    account["name"] = "主账户"
    app._refresh_account_list()
    app.root.update()


# ── 加载态 / 保存反馈 / 新增弹窗 / 删除确认（Q1–Q15 定案） ──────────────────

def descendants(widget):
    """递归遍历后代控件（驱动真实弹窗时按文本找按钮/输入框用）。"""
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


def titled_toplevel(app, title):
    """主窗口名下标题为 title 的弹窗（after 预排驱动时定位真实 Toplevel）。"""
    return next(w for w in app.root.winfo_children()
                if isinstance(w, tk.Toplevel) and w.title() == title)


def test_folder_loading_shows_spinner_and_hides_tree(app, monkeypatch):
    """加载态（Q2/Q3/Q4）：拉取清单期间树区盖上旋转 spinner、⟳ 禁用；加载完恢复。"""
    monkeypatch.setattr(g.threading, "Thread", sync_thread)
    monkeypatch.setattr(app, "_folder_worker", lambda *a: None)   # 不投事件：加载态停留
    account = current_account(app)
    app._loading_folder_ids.clear()
    app._state(account["id"])["snapshot"] = None

    app._maybe_load_folders(account["id"])
    assert account["id"] in app._loading_folder_ids
    assert app.folder_loading.winfo_manager() == "grid"
    assert app.folder_canvas.winfo_manager() == "grid"   # 树还在，只是被动画层盖住（不可交互）
    text = str(app.folder_loading_label.cget("text"))
    assert text.startswith(tuple(g.SPINNER_FRAMES)) and text.endswith(g.FOLDER_LOADING_TEXT), text
    assert is_disabled(app.reload_btn)

    load_folders(app, NAMES)   # folders_loaded：树回、动画层收、⟳ 恢复
    assert app.folder_loading.winfo_manager() == ""
    assert not is_disabled(app.reload_btn)
    assert len(app.folder_rows) == 6
    app._loading_folder_ids.clear()


def test_folder_loading_follows_the_selected_account(app, monkeypatch):
    """加载态只跟随当前选中账户（Q8）：切走即换目标账户视图，切回继续转。"""
    monkeypatch.setattr(app, "_maybe_load_folders", lambda aid: None)
    first = current_account(app)
    second = g.new_account()
    second.update({"name": "后台账户", "user": "bg@example.com", "server": "imap.example.com"})
    app.accounts.append(second)
    app._refresh_account_list()
    app._loading_folder_ids.clear()
    app._loading_folder_ids.add(first["id"])   # A 在拉取
    try:
        app._switch_account(first["id"])
        assert app.folder_loading.winfo_manager() == "grid"

        app._switch_account(second["id"])      # B 没在拉取：动画层收起，B 的树/占位正常
        assert app.folder_loading.winfo_manager() == ""
        assert not is_disabled(app.reload_btn)

        app._switch_account(first["id"])       # 切回 A：动画层回来
        assert app.folder_loading.winfo_manager() == "grid"
    finally:
        app._loading_folder_ids.clear()
        app._sync_folder_loading_view()
        app.accounts.remove(second)
        app._states.pop(second["id"], None)
        app._refresh_account_list()
        app._switch_account(first["id"])
        app._render_account_detail()


def test_account_save_reports_result_in_status_text(app, monkeypatch):
    """保存反馈（Q5/Q7/Q10）：校验失败红字写原因、成功绿「已保存」，都不再弹 messagebox。"""
    box = FakeBox()
    monkeypatch.setattr(g, "messagebox", box)
    monkeypatch.setattr(app, "_maybe_load_folders", lambda aid: None)
    account = current_account(app)
    app._refresh_edit_list(account["id"])
    orig_port = app._acct_port_var.get()

    app._acct_port_var.set("0")
    app._on_account_save()
    assert str(app._account_save_status.cget("text")) == g.SAVE_STATUS_FAIL_PREFIX + "端口必须是 1-65535 的整数。"
    assert str(app._account_save_status.cget("foreground")) == g.STATUS_ERR_FG
    assert box.warnings == [], box.warnings   # Q10：校验提示收口到状态文案

    app._acct_port_var.set(orig_port)
    app._on_account_save()
    assert str(app._account_save_status.cget("text")) == g.SAVE_STATUS_OK_TEXT
    assert str(app._account_save_status.cget("foreground")) == g.STATUS_OK_FG
    assert app._account_save_status_job is not None   # 约 3 秒后自动消失（job 已排定）
    app._clear_account_save_status()
    assert str(app._account_save_status.cget("text")) == ""


def test_new_account_dialog_creates_account_only_on_save(app, monkeypatch):
    """新增弹窗（Q11–Q13）：叉掉/取消零副作用；校验红字留窗内，成功闪「已保存」自动关窗并选中。"""
    loaded: list[str] = []
    monkeypatch.setattr(app, "_maybe_load_folders", lambda aid: loaded.append(aid))
    main_id = app._selected_account_id

    # 取消路径（叉掉/取消同路）：零副作用
    real_ask = app._ask_new_account
    monkeypatch.setattr(app, "_ask_new_account", lambda: None)
    before = len(app.accounts)
    app._on_account_add()
    assert len(app.accounts) == before

    # 真实弹窗：after 预排驱动（wait_window 的事件循环里执行），填值 → 校验 → 保存
    seen: dict = {}
    monkeypatch.setattr(app, "_ask_new_account", lambda: real_ask())

    def drive():
        dialog = titled_toplevel(app, g.NEW_ACCOUNT_DLG_TITLE)
        entries = [w for w in descendants(dialog) if isinstance(w, tk.Entry)]
        seen["entry_count"] = len(entries)   # 名称/邮箱/服务器/端口/密码
        save = next(w for w in descendants(dialog)
                    if isinstance(w, ttk.Button) and str(w.cget("text")) == "保存")
        cancel = next(w for w in descendants(dialog)
                      if isinstance(w, ttk.Button) and str(w.cget("text")) == g.CANCEL_BTN_TEXT)
        status = next(w for w in save.master.master.winfo_children() if isinstance(w, tk.Label))
        app.root.update()
        seen["equal_width"] = abs(save.winfo_width() - cancel.winfo_width()) <= 2   # messagebox 口径（Q4）
        seen["gap"] = cancel.winfo_rootx() - (save.winfo_rootx() + save.winfo_width())
        seen["focus_is_save"] = dialog.focus_get() is save   # 默认按钮 = 保存（Q6/Q9）
        save.invoke()                              # 空表单：红字原因留在窗内
        seen["fail_text"] = str(status.cget("text"))
        seen["fail_fg"] = str(status.cget("foreground"))
        seen["open_after_fail"] = bool(dialog.winfo_exists())
        entries[0].insert(0, "弹窗账户")
        entries[1].insert(0, "popup@example.com")
        entries[2].insert(0, "imap.example.com")
        entries[4].insert(0, "pw")
        save.invoke()                              # 合法：绿「已保存」，0.8 秒后自动关窗
        seen["ok_text"] = str(status.cget("text"))
        seen["ok_fg"] = str(status.cget("foreground"))

    app.root.after(0, drive)
    app._on_account_add()
    assert seen["entry_count"] == 5, seen
    assert seen["equal_width"] and seen["gap"] == g.BTN_GAP, seen   # messagebox 口径（Q4）
    assert seen["focus_is_save"] is True                            # 默认按钮 = 保存（Q6/Q9）
    assert seen["fail_text"] == g.SAVE_STATUS_FAIL_PREFIX + "请填写邮箱地址。"
    assert seen["fail_fg"] == g.STATUS_ERR_FG and seen["open_after_fail"] is True
    assert seen["ok_text"] == g.SAVE_STATUS_OK_TEXT and seen["ok_fg"] == g.STATUS_OK_FG
    account = current_account(app)
    assert account["name"] == "弹窗账户" and account["user"] == "popup@example.com"
    assert account["id"] == app._editing_account_id   # 进列表并选中（Q13）
    assert loaded and loaded[-1] == account["id"]

    # 还原模块状态：删掉弹窗建的账户，选中回主账户
    app.accounts.remove(account)
    app._states.pop(account["id"], None)
    app._refresh_account_list()
    app._switch_account(main_id)
    app._refresh_edit_list(main_id)


def test_delete_confirm_is_a_self_drawn_centered_dialog(app):
    """删除确认（Q14）：自绘弹窗居中到主窗口；X/Esc/取消不删，「删除」才删。"""
    account = g.new_account()
    account.update({"name": "待删账户", "user": "del@example.com", "server": "imap.example.com"})
    seen: dict = {}

    def drive_cancel():
        dialog = titled_toplevel(app, g.DELETE_DLG_TITLE)
        app.root.update()
        seen["err_x"] = ((dialog.winfo_rootx() + dialog.winfo_width() // 2)
                         - (app.root.winfo_rootx() + app.root.winfo_width() // 2))
        seen["err_y"] = ((dialog.winfo_rooty() + dialog.winfo_height() // 2)
                         - (app.root.winfo_rooty() + app.root.winfo_height() // 2))
        seen["text"] = str(next(w for w in descendants(dialog)
                                if isinstance(w, tk.Label)).cget("text"))
        cancel = next(w for w in descendants(dialog)
                      if isinstance(w, ttk.Button) and str(w.cget("text")) == g.CANCEL_BTN_TEXT)
        delete = next(w for w in descendants(dialog)
                      if isinstance(w, ttk.Button) and str(w.cget("text")) == g.DELETE_DLG_CONFIRM)
        seen["equal_width"] = abs(cancel.winfo_width() - delete.winfo_width()) <= 2   # messagebox 口径（Q4）
        seen["gap"] = delete.winfo_rootx() - (cancel.winfo_rootx() + cancel.winfo_width())
        seen["focus_is_cancel"] = dialog.focus_get() is cancel   # 默认按钮给安全侧（Q6）
        dialog.destroy()   # X/Esc 同路：answer 为空 = 不删

    app.root.after(0, drive_cancel)
    assert app._ask_delete_account(account) is False
    assert abs(seen["err_x"]) <= 2 and abs(seen["err_y"]) <= 2, (seen["err_x"], seen["err_y"])
    assert seen["equal_width"] and seen["gap"] == g.BTN_GAP, seen
    assert seen["focus_is_cancel"] is True
    assert "已备份到磁盘的邮件不会被删除。" in seen["text"]

    def drive_confirm():
        dialog = titled_toplevel(app, g.DELETE_DLG_TITLE)
        next(w for w in descendants(dialog)
             if isinstance(w, ttk.Button) and str(w.cget("text")) == g.DELETE_DLG_CONFIRM).invoke()

    app.root.after(0, drive_confirm)
    assert app._ask_delete_account(account) is True


def test_add_delete_buttons_fill_the_account_list_width(app):
    """新增/删除占满账户列表全部宽度（Q15）：并排各半、间隔 BTN_GAP、随列表宽度联动。"""
    app.root.update()
    nav = app._account_nav_frame
    area = app.account_edit_list.master
    tools = next(w for w in nav.winfo_children()
                 if isinstance(w, tk.Frame)
                 and {str(b.cget("text")) for b in w.winfo_children()
                      if isinstance(b, (tk.Button, ttk.Button))} == {"新增", "删除"})
    add_btn, del_btn = sorted((b for b in tools.winfo_children() if isinstance(b, (tk.Button, ttk.Button))),
                              key=lambda b: b.winfo_rootx())
    assert isinstance(add_btn, ttk.Button) and isinstance(del_btn, ttk.Button)   # 原生观感（Q5）

    def assert_tiles_list():
        app.root.update()
        left = add_btn.winfo_rootx()
        right = del_btn.winfo_rootx() + del_btn.winfo_width()
        # 净 4px 内缩（Q1-A 按钮排基线）：整排平铺列表宽 − 2×4px
        assert abs(left - (area.winfo_rootx() + 4)) <= 2, (left, area.winfo_rootx())
        assert abs(right - (area.winfo_rootx() + area.winfo_width() - 4)) <= 2, (right, area.winfo_width())
        gap = del_btn.winfo_rootx() - (add_btn.winfo_rootx() + add_btn.winfo_width())
        assert gap == g.BTN_GAP, gap
        assert abs(add_btn.winfo_width() - del_btn.winfo_width()) <= 2   # 各占一半

    assert_tiles_list()
    nav.configure(width=app._account_list_width + 80)   # 拖宽联动：按钮排跟着占满
    assert_tiles_list()
    nav.configure(width=app._account_list_width)
    app.root.update()


def test_account_row_status_icons_follow_phases_and_ttl(app, monkeypatch):
    """行尾图标：统计蓝/备份绿/停止红块/完成绿勾 60s/已停止灰块 5s/失败红叉持久（ADR 0012）。"""
    monkeypatch.setattr(g, "messagebox", FakeBox())   # 可见账户失败会弹错误框，替身挡掉
    aid = app._selected_account_id
    row = app._account_rows[aid]
    state = app._state(aid)

    def icon_is(image):
        app.root.update_idletasks()
        return str(row["icon"].cget("image")) == str(image)

    assert icon_is(app.row_icon_blank)
    app._on_phase(aid, "扫描 1/3：INBOX")
    assert icon_is(app.row_scan_icon) and state["run_phase"] == g.PHASE_SCANNING
    app._on_phase(aid, "备份 INBOX（7 封）")
    assert icon_is(app.row_download_icon) and state["run_phase"] == g.PHASE_BACKING
    # stopping 后到达的 phase 事件不许把阶段降回去，图标保持红块
    app._stop_events[aid] = g.threading.Event()
    app._on_stop()
    assert icon_is(app.row_stop_icon)
    app._on_phase(aid, "备份 INBOX（7 封）")
    assert state["run_phase"] == g.PHASE_STOPPING and icon_is(app.row_stop_icon)

    # 已停止：灰方块 + 5s TTL；到期回调把图标清回空白
    app._on_account_done(aid, 5, 100, 9.0, True, 20)
    assert icon_is(app.row_stopped_icon) and state["result"][0] == "stopped"
    stamp = state["result_at"]
    app._clear_result_icon(aid, stamp)
    assert icon_is(app.row_icon_blank) and state["result"] is None

    # 正常完成：绿勾 + 60s 冷却窗（同一常量）；到期清回空白
    state["run_phase"] = g.PHASE_BACKING
    app._on_account_done(aid, 3, 0, 2.0, False, 3)
    assert icon_is(app.row_done_icon) and state["result"][0] == "done"
    assert app._in_done_cooldown(app._find_account(aid))
    state["result_at"] = time.monotonic() - (g.DONE_COOLDOWN_S + 1)
    assert not app._in_done_cooldown(app._find_account(aid))
    app._clear_result_icon(aid, state["result_at"] - 1)  # 旧戳：空转不清
    assert icon_is(app.row_done_icon)
    app._clear_result_icon(aid, app._state(aid)["result_at"])
    assert icon_is(app.row_icon_blank)

    # 失败：红叉常驻到下次启动；脱钩立即清
    state["run_phase"] = g.PHASE_BACKING
    app._on_account_failed(aid, "boom")
    assert icon_is(app.row_fail_icon) and state["result"][0] == "failed"
    app._account_rows[aid]["var"].set(False)
    app._sync_account_checks()
    assert icon_is(app.row_icon_blank) and state["result"] is None
    app._account_rows[aid]["var"].set(True)
    app._sync_account_checks()
    set_phase(app, aid, g.PHASE_IDLE)


def test_done_cooldown_skips_batch_but_not_single_start(app, monkeypatch):
    """完成冷却：批量入口跳过该账户并提示，到期后纳入；右下「开始备份」始终绕过冷却。"""
    aid = app._selected_account_id
    started: list[list[str]] = []
    monkeypatch.setattr(app, "_begin_preflight", lambda ids: started.append(list(ids)))
    monkeypatch.setattr(app, "_backup_root_ready", lambda: True)
    box = FakeBox()
    monkeypatch.setattr(g, "messagebox", box)
    state = app._state(aid)

    state["run_phase"] = g.PHASE_BACKING
    app._on_account_done(aid, 3, 0, 2.0, False, 3)
    app._on_start_selected()
    assert started == []                                   # 冷却中：不进预检
    assert box.warnings and box.warnings[-1][0] == g.COOLING_WARN_TITLE

    state["result_at"] = time.monotonic() - (g.DONE_COOLDOWN_S + 1)
    app._on_start_selected()
    assert started == [[aid]]                              # 冷却过：纳入

    state["result_at"] = time.monotonic()                  # 重新进窗
    started.clear()
    app._on_start()                                        # 右下入口绕过冷却
    assert started == [[aid]]

    state["result"] = None
    state["result_at"] = None
    app._apply_account_icon(aid)
    set_phase(app, aid, g.PHASE_IDLE)


def test_backup_selected_appends_only_the_new_account_while_others_run(app, monkeypatch):
    """已有账户在跑时再点「备份所选」：只启动勾着、空闲的新账户，在跑的不受影响。"""
    first = current_account(app)
    second = g.new_account()
    second.update({"name": "后来者", "user": "later@example.com", "server": "imap.example.com"})
    app.accounts.append(second)
    app._refresh_account_list()
    app._switch_account(first["id"])

    started: list[list[str]] = []
    monkeypatch.setattr(app, "_begin_preflight", lambda ids: started.append(list(ids)))
    monkeypatch.setattr(app, "_account_credentials", lambda account, warn=True: {"server": "s"})
    monkeypatch.setattr(app, "_backup_root_ready", lambda: True)

    set_phase(app, first["id"], g.PHASE_BACKING)
    app._on_start_selected()
    assert started == [[second["id"]]], started

    set_phase(app, first["id"], g.PHASE_IDLE)
    app.accounts.remove(second)
    app._states.pop(second["id"], None)
    app._refresh_account_list()
    app._render_account_detail()


def test_run_controls_follow_the_selected_account(app):
    """右下开始/停止只描述选中账户：看活动账户时停止可用，切到空闲账户停止即禁用。"""
    first = current_account(app)
    second = g.new_account()
    second.update({"name": "安静账户", "user": "idle@example.com", "server": "imap.example.com"})
    app.accounts.append(second)
    app._refresh_account_list()

    app._switch_account(first["id"])
    set_phase(app, first["id"], g.PHASE_BACKING)
    assert is_disabled(app.start_btn) and not is_disabled(app.stop_btn)

    app._switch_account(second["id"])
    assert is_disabled(app.stop_btn)                        # 空闲账户：停止禁用
    assert not is_disabled(app.backup_selected_btn)        # 备份所选运行中也始终可用

    app._switch_account(first["id"])
    assert not is_disabled(app.stop_btn)
    set_phase(app, first["id"], g.PHASE_IDLE)
    app.accounts.remove(second)
    app._states.pop(second["id"], None)
    app._refresh_account_list()
    app._render_account_detail()


def test_active_account_cannot_be_deleted(app, monkeypatch):
    """活动账户删除被拦在确认弹窗前，弹「请先停止」警告；空闲后删除流程照常。"""
    aid = app._selected_account_id
    app._editing_account_id = aid
    box = FakeBox()
    monkeypatch.setattr(g, "messagebox", box)
    asked: list[bool] = []
    monkeypatch.setattr(app, "_ask_delete_account", lambda account: asked.append(True) or False)

    set_phase(app, aid, g.PHASE_BACKING)
    app._on_account_delete()
    assert box.warnings and box.warnings[-1][0] == g.ACTIVE_DELETE_WARN_TITLE
    assert asked == [] and app._find_account(aid) is not None

    set_phase(app, aid, g.PHASE_IDLE)
    app._on_account_delete()
    assert asked == [True]


def test_stop_all_button_lives_below_backup_selected(app):
    """「停止所有」在「备份所选」正下方、同宽；初始不映射，有活动才出现；隐藏时底距仍在。"""
    assert not app.stop_all_btn.winfo_ismapped()
    parent = app.stop_all_btn.master
    assert parent is app.backup_selected_btn.master
    aid = app._selected_account_id
    set_phase(app, aid, g.PHASE_BACKING)
    app.root.update_idletasks()
    assert app.stop_all_btn.winfo_ismapped()
    assert app.stop_all_btn.winfo_rooty() > app.backup_selected_btn.winfo_rooty()
    assert app.stop_all_btn.winfo_width() == app.backup_selected_btn.winfo_width()
    set_phase(app, aid, g.PHASE_IDLE)
    app.root.update_idletasks()
    assert not app.stop_all_btn.winfo_ismapped()
    # 「停止所有」隐藏时，底部容器到 LabelFrame 内底边仍留 4px（容器统一 pady），按钮不贴边框
    pane = parent.master
    pane_inner_bottom = pane.winfo_rooty() + pane.winfo_height() - int(pane.cget("borderwidth"))
    container_bottom = parent.winfo_rooty() + parent.winfo_height()
    assert pane_inner_bottom - container_bottom >= 4


def test_row_icon_click_selects_the_account(app):
    """点行尾状态图标等同点行：切换到该账户（图标本身也是行的命中区）。"""
    first = current_account(app)
    other = g.new_account()
    other.update({"name": "点图标", "user": "icon@x.com", "server": "s"})
    app.accounts.append(other)
    app._refresh_account_list()
    app._switch_account(first["id"])
    icon = app._account_rows[other["id"]]["icon"]
    icon.event_generate("<Button-1>", x=1, y=1)
    app.root.update_idletasks()
    assert app._selected_account_id == other["id"]
    app.accounts.remove(other)
    app._states.pop(other["id"], None)
    app._refresh_account_list()
    app._switch_account(first["id"])   # 删除后恢复选中真实账户（与 _on_account_delete 同口径）


def test_progress_bar_value_tracks_messages_without_overfilling(app):
    """多消息高频更新：进度条 value/max 始终与计数同源，2.5% 不会显示成满格。"""
    aid = current_account(app)["id"]
    app._switch_account(aid)
    st = app._state(aid)
    st["backed"] = 0
    st["run_phase"] = g.PHASE_BACKING
    app._handle_event(("totals", aid, 5626, 4839))
    for i in range(1, 123):
        app._handle_event(("message", aid, _message(index=i, total=4839, msg_id=f"<p{i}>")))
    assert float(app.progress.cget("maximum")) == 4839.0
    assert float(app.progress.cget("value")) == 122.0
    assert float(app.progress.cget("value")) / float(app.progress.cget("maximum")) < 0.1
    # state 进度被持续回写：切走再切回不归零
    assert app._state(aid)["progress"] == (122.0, 4839.0)
    # 计数超出 maximum 也被钳住，不产生满格
    app.backed_count = 99999
    app._refresh_counts()
    assert float(app.progress.cget("value")) == 4839.0


def test_progress_length_not_reconfigured_every_message(app):
    """连续消息在同一布局档位：进度条 length 不被逐封重配（原生填充错位的抖动源）。"""
    aid = current_account(app)["id"]
    app._switch_account(aid)
    st = app._state(aid)
    st["backed"] = 0
    st["run_phase"] = g.PHASE_BACKING
    app.root.geometry("960x736")
    app.root.update()
    app._handle_event(("totals", aid, 5626, 4839))
    configured = []
    original = app.progress.configure

    def spy(*a, **k):
        if (a and a[0] == "length") or "length" in k:
            configured.append(k.get("length", a[1] if len(a) > 1 else None))
        return original(*a, **k)

    app.progress.configure = spy
    length_before = getattr(app, "_progress_length", None)
    for i in range(1, 50):
        app._handle_event(("message", aid, _message(index=i, total=4839, msg_id=f"<l{i}>")))
    app.progress.configure = original
    # 位数没变（都 <100），length 全程不重配
    assert configured == [] or all(v == length_before for v in configured)


def test_stop_restores_start_button_at_full_width_without_resize(app):
    """停止后开始按钮恢复静止文案，同批即完成布局（不必 resize 窗口，文本不被裁）。"""
    aid = app._selected_account_id
    set_phase(app, aid, g.PHASE_BACKING)
    app.root.update_idletasks()
    app._on_stop()
    app.root.update_idletasks()
    assert str(app.start_btn.cget("text")) == g.START_BTN_LABEL
    assert app.start_btn.winfo_width() >= app.start_btn.winfo_reqwidth() - 1


def test_fit_account_names_survives_missing_icons(app):
    """启动期画布 Configure 早于图标挂载时，名称截断静默跳过而不抛 AttributeError。"""
    saved = app.row_icon_blank
    del app.row_icon_blank
    try:
        app._fit_account_names()   # 不抛异常即可
    finally:
        app.row_icon_blank = saved


def test_all_buttons_are_themed(app):
    """所有按钮都是 ttk.Button（Q3 硬契约）：全应用不再有经典立体按钮残留。"""
    classics = [w for w in descendants(app.root) if isinstance(w, tk.Button)]
    assert classics == [], [str(w) for w in classics]


def test_all_ttk_buttons_use_flexible_width(app):
    """width 硬契约：正 width 是固定文本槽（avgchar 倍数），CJK 文案超出即裁切且不撑宽。

    全库按钮只许 width≤0：0＝按内容自然宽（⟳ 再经 _make_square_button 的 padding 补成正方形），
    负数＝字符数下限（-1 贴内容、文件夹工具栏 -7 保三颗等宽）。回归自开始/停止/浏览按钮被裁。
    """
    offenders = []
    for w in descendants(app.root):
        if isinstance(w, ttk.Button):
            raw = str(w.cget("width"))
            width = int(raw) if raw else 0
            if width > 0:
                offenders.append((str(w.cget("text")), raw))
    assert offenders == [], offenders


def test_switching_accounts_triggers_a_silent_load(app, monkeypatch):
    """切账户即静默加载（GLOSSARY「静默加载」口径）：目标账户没清单就拉；快照命中的不重拉。"""
    first = current_account(app)
    calls: list[str] = []
    monkeypatch.setattr(g.threading, "Thread", sync_thread)
    monkeypatch.setattr(app, "_folder_worker", lambda *a: calls.append(a[0]))
    app._loading_folder_ids.clear()

    second = g.new_account()
    second.update({"name": "惰性账户", "user": "lazy@example.com", "server": "imap.example.com"})
    app.accounts.append(second)
    app._run_passwords[second["id"]] = "p"   # 会话内临时密码口径，免依赖本机 DPAPI
    app._refresh_account_list()
    try:
        app._switch_account(second["id"])    # 从未拉过 → 触发一次静默加载
        assert calls == [second["id"]], calls

        app._state(first["id"])["snapshot"] = app._account_snapshot(first)
        app._switch_account(first["id"])     # 快照命中 → 不重复起线程
        assert calls == [second["id"]], calls
    finally:
        app._loading_folder_ids.clear()
        app._interactive_folder_loads.discard(second["id"])
        app._run_passwords.pop(second["id"], None)
        app.accounts.remove(second)
        app._states.pop(second["id"], None)
        app._refresh_account_list()
        app._render_account_detail()


def test_folder_toolbar_buttons_follow_list_content(app):
    """全选/清空/仅收件箱 只在清单非空时可点（空清单点了也是空转）；⟳ 刷新永远可用——
    它是加载失败后唯一的重试入口。"""
    set_phase(app, current_account(app)["id"], g.PHASE_IDLE)   # 前序用例把主账户留在 BACKING：先收口
    load_folders(app, SHORT_FOLDERS)
    assert all(not is_disabled(b) for b in app._folder_tool_btns)
    assert not is_disabled(app.reload_btn)

    clear_folders(app)
    assert all(is_disabled(b) for b in app._folder_tool_btns)
    assert not is_disabled(app.reload_btn)

    load_folders(app, SHORT_FOLDERS)   # 还原有清单的状态
    assert all(not is_disabled(b) for b in app._folder_tool_btns)
