"""关闭行为偏好与关闭决策的纯逻辑矩阵（不需要 Tk，也不碰托盘）。

这两件事都不依赖界面：偏好只认三个已知值、其余一律回退默认；关闭动作由
「偏好 × 窗口是否已隐藏」决定。真正的对话框与托盘接线在 test_gui_background.py 里测。
"""
import pytest

import imapbackup_gui as g


@pytest.mark.parametrize("raw", [value for value, _label in g.CLOSE_ACTION_LABELS])
def test_parse_close_action_keeps_every_known_value(raw):
    assert g.parse_close_action(raw) == raw


@pytest.mark.parametrize("raw", [None, "", "ASK", "always", "hide", 0, 1, True, False, [], {}])
def test_parse_close_action_falls_back_to_ask(raw):
    """缺失、大小写不符、类型不对（含 bool）都按「每次询问」处理。"""
    assert g.parse_close_action(raw) == g.CLOSE_ACTION_ASK


def test_parse_close_action_default_is_ask():
    assert g.DEFAULT_CLOSE_ACTION == g.CLOSE_ACTION_ASK


@pytest.mark.parametrize("preference", [value for value, _label in g.CLOSE_ACTION_LABELS])
def test_hidden_window_always_exits_for_real(preference):
    """窗口已隐藏时收到的关闭请求来自外部程序（安装器/卸载器），一律真退出。

    否则升级安装会被后台驻留的实例挡住，用户除了任务管理器没有别的出路。
    """
    assert g.decide_close(preference, hidden=True) == "force_exit"


def test_visible_window_follows_the_preference():
    assert g.decide_close(g.CLOSE_ACTION_TRAY, hidden=False) == "hide"
    assert g.decide_close(g.CLOSE_ACTION_EXIT, hidden=False) == "exit"
    assert g.decide_close(g.CLOSE_ACTION_ASK, hidden=False) == "ask"
