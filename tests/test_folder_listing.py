# -*- coding: utf-8 -*-
"""连接后的文件夹列表，以及 --folders / --exclude-folders 的匹配语义。

用假 IMAP 服务器驱动 main() 跑完全流程，不联网。两条核心规则：

* 按**分隔符段边界**匹配 —— ``系统文件夹`` 选中它和整棵子树，``Sent`` 不误中 ``Sent Items``，
  ``INBOX`` 不误中 ``INBOX/archive``；中文显示名直接可用，原始 modified UTF-7 名也仍可用。
* include 0 匹配**报错并非 0 退出**并附可用文件夹列表；exclude 0 匹配只警告不退出。
"""
import contextlib
import itertools
import re

import pytest
from _pytest.monkeypatch import MonkeyPatch

import imapbackup312 as core

from fakes import ListServer, imap_utf7_encode

SEP_RE = re.compile(r"^─+\s+\[(\d+)/(\d+)\]\s+(.*?)\s+─+\s*$", re.MULTILINE)

# 中文文件夹集：分隔符是 "/"
QQ = ["系统文件夹/发件箱", "已删除邮件/子文件夹", "系统文件夹", "Sent Items", "INBOX", "系统文件夹/其他文件夹"]
# 分段元组按码点比较，所以 ASCII 文件夹名排在中文前面
QQ_SORTED = ["INBOX", "Sent Items", "已删除邮件/子文件夹", "系统文件夹", "系统文件夹/其他文件夹", "系统文件夹/发件箱"]
# Outlook 风格账号：分隔符是 "."
OUT = ["Archive.2024", "Archived", "Archive.2025", "Drafts", "Archive.Sub.Deep"]
OUT_SORTED = ["Archive.2024", "Archive.2025", "Archive.Sub.Deep", "Archived", "Drafts"]


def synced(text):
    """(idx, total, display_name) per folder the run actually synced, in order."""
    return [(int(a), int(b), n) for a, b, n in SEP_RE.findall(text)]


def synced_names(text):
    return [name for _, _, name in synced(text)]


def listing(text):
    """The bullet lines printed under 'Found N folders:', still carrying the '- ' bullet."""
    m = re.search(r"Found \d+ folders:\n", text)
    if not m:
        return []
    out = []
    for line in text[m.end():].split("\n"):
        if line.startswith("─"):
            break
        if line:
            out.append(line)
    return out


def stems(lines):
    """Bullet lines back to plain folder names."""
    return [line[2:] for line in lines]


@pytest.fixture(scope="module", autouse=True)
def _redirect_log_dir(tmp_path_factory):
    """main() writes a daily run log; keep the suite off the real run log.

    这些测试原本从不重定向 LOG_DIR，26 次 main() 一直往 %LOCALAPPDATA% 的真实当日
    运行日志里写 INFO 行。
    """
    patch = MonkeyPatch()
    patch.setattr(core, "LOG_DIR", tmp_path_factory.mktemp("log"))
    yield
    patch.undo()


@pytest.fixture
def run_cli(tmp_path, monkeypatch, console_capture):
    """Drive main() against a fake server; each call gets its own output directory.

    返回 (exit_code, combined stdout+stderr)。同一测试里连跑几次时每次先清空缓冲，
    否则上一次运行留下的分隔行会串进这一次的断言。
    """
    calls = itertools.count()

    def run(display_names, extra, delim="/", mbox_dir=None):
        index = next(calls)
        target = mbox_dir if mbox_dir is not None else tmp_path / f"out{index}"
        monkeypatch.setattr(core, "connect_and_login", lambda cfg, log: ListServer(display_names, delim))
        argv = ["-s", "imap.example.com", "-e", "-u", "x@y.com", "-p", "secret",
                "-d", str(target), *extra]
        console_capture.seek(0)
        console_capture.truncate(0)
        with contextlib.redirect_stdout(console_capture), contextlib.redirect_stderr(console_capture):
            try:
                code = core.main(argv)
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
                if not isinstance(exc.code, int) and exc.code:
                    console_capture.write(str(exc.code) + "\n")
        return code, console_capture.getvalue()

    return run


def test_listing_is_bulleted_with_parents_before_descendants(run_cli):
    code, text = run_cli(QQ, [])
    assert code == 0, code
    assert stems(listing(text)) == QQ_SORTED, listing(text)
    assert all(line.startswith("- ") and not line.startswith("-  ") for line in listing(text)), listing(text)
    assert synced_names(text) == QQ_SORTED, synced_names(text)


def test_exact_full_path_still_matches(run_cli):
    _, text = run_cli(QQ, ["--folders", "系统文件夹/其他文件夹"])
    assert synced_names(text) == ["系统文件夹/其他文件夹"]


def test_leaf_only_name_no_longer_silently_matches_nothing(run_cli):
    code, text = run_cli(QQ, ["--folders", "其他文件夹"])
    assert code != 0, code
    assert "No folder matches --folders '其他文件夹'" in text, text
    assert "Available folders:\n- INBOX\n- Sent Items" in text, text


def test_prefix_matches_a_segment_boundary_not_a_look_alike(run_cli):
    _, text = run_cli(QQ, ["--folders", "系统文件夹"])
    assert synced_names(text) == ["系统文件夹", "系统文件夹/其他文件夹", "系统文件夹/发件箱"], synced_names(text)
    _, text = run_cli(QQ, ["--folders", "Sent"])
    assert "No folder matches --folders 'Sent'" in text, text
    _, text = run_cli(QQ, ["--folders", "INBOX"])
    assert synced_names(text) == ["INBOX"]
    _, text = run_cli(QQ, ["--folders", "系统"])
    assert "No folder matches" in text, text


def test_prefix_boundary_on_a_dot_delimited_account(run_cli):
    _, text = run_cli(OUT, ["--folders", "Archive"], delim=".")
    assert synced_names(text) == ["Archive.2024", "Archive.2025", "Archive.Sub.Deep"], synced_names(text)
    _, text = run_cli(OUT, [])
    assert stems(listing(text)) == OUT_SORTED, listing(text)


def test_raw_modified_utf7_name_is_still_accepted(run_cli):
    _, text = run_cli(QQ, ["--folders", imap_utf7_encode("系统文件夹/发件箱")])
    assert synced_names(text) == ["系统文件夹/发件箱"]


def test_multiple_patterns_blank_items_and_trailing_delimiter(run_cli):
    _, text = run_cli(QQ, ["--folders", "INBOX, ,系统文件夹"])
    assert synced_names(text) == ["INBOX", "系统文件夹", "系统文件夹/其他文件夹", "系统文件夹/发件箱"], synced_names(text)
    _, text = run_cli(QQ, ["--folders", "系统文件夹/"])
    assert len(synced(text)) == 3
    code, text = run_cli(QQ, ["--folders", ""])
    assert code != 0 and "Available folders:" in text


def test_exclude_folders_subtree_and_zero_match_warning(run_cli):
    _, text = run_cli(QQ, ["--exclude-folders", "系统文件夹"])
    assert synced_names(text) == ["INBOX", "Sent Items", "已删除邮件/子文件夹"], synced_names(text)
    code, text = run_cli(QQ, ["--exclude-folders", "不存在"])
    assert "No folder matches --exclude-folders '不存在'" in text, text
    assert code == 0 and len(synced(text)) == 6
    _, text = run_cli(QQ, ["--exclude-folders", "已删除邮件/子文件夹"])
    assert "No folder matches" not in text and "已删除邮件/子文件夹" not in " ".join(synced_names(text))


def test_quiet_suppresses_the_listing_but_not_the_backup(run_cli, tmp_path):
    out_dir = tmp_path / "mbox"
    code, text = run_cli(QQ, ["--quiet", "--folders", "INBOX"], mbox_dir=out_dir)
    assert code == 0, code
    assert text.strip() == "", repr(text)
    assert (out_dir / "x@y.com").exists()
    assert not (out_dir / "x@y.com" / "系统文件夹").exists()


def test_thunderbird_inbox_spelling_maps_onto_server_inbox(run_cli):
    _, text = run_cli(QQ, ["--thunderbird", "--folders", "Inbox"])
    assert synced_names(text) == ["INBOX"]
