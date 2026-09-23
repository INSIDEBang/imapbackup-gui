# -*- coding: utf-8 -*-
"""eml 输出切片及其依赖的核心接缝。

覆盖 sanitize / 目标路径预算 / 唯一化命名 / INTERNALDATE 与 Date header 时间戳 /
折叠 header 解析 / MIME 解码 / 双格式下载与增量 / 协作式取消 / modified UTF-7 /
进度条与终端宽度 / 文件夹工具链。假服务器，不需要凭证。
"""
import inspect
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import imapbackup312 as m

from fakes import FetchServer, HeaderServer, ListServer, NullLog, recording_spinner

TS = datetime(2024, 5, 12, 10, 15, 30)
DATE_HEADER_INSTANT = datetime(2024, 5, 12, 2, 15, 30, tzinfo=timezone.utc)

# 缺 Message-Id 的合成 ID：UUID salt + 随机尾。改 m.UUID 会破坏既有备份的增量行为。
SYNTHETIC = "<" + m.UUID + "." + uuid.uuid4().hex[:12] + ">"
RAW1 = (b"From: Alice <a@b.com>\r\nSubject: Re: =?utf-8?B?5L2g5aW9?=\r\nMessage-Id: <aaa@x>\r\n"
        b"\r\nline1\r\nFrom nobody secret\r\nline2\r\n")
RESP1 = (b'1 (INTERNALDATE "12-May-2024 10:15:30 +0800" RFC822 {120}', RAW1)
RAW2 = b"Date: Sun, 12 May 2024 02:15:30 +0000\r\nFrom: b@x\r\n\r\nno msgid here\r\n"
RESP2 = (b'2 (RFC822 {50}', RAW2)
MSGS = {"<aaa@x>": 1, SYNTHETIC: 2}


def test_sanitize_segment():
    s = m.sanitize_segment("Re: 预算/草案?", 60)
    assert ":" not in s and "/" not in s and "?" not in s and s == "Re_ 预算_草案_", repr(s)
    assert m.sanitize_segment("CON", 60) == "_CON"
    assert m.sanitize_segment("   ", 60) == "_"
    long = m.sanitize_segment("a" * 200, 60)
    assert len(long) == 60 and long[51] == "-" and len(long.split("-")[1]) == 8, repr(long)
    assert m.sanitize_segment("hello world abc", 10) == "hello worl"
    assert m.sanitize_segment("..foo bar..", 60) == "foo bar"


def test_sanitize_msgid():
    assert m.sanitize_msgid("<CAF123@mail.gmail.com>") == "CAF123@mail.gmail.com"
    capped = m.sanitize_msgid("<" + "m" * 150 + "@x>")
    assert len(capped) == 100
    assert not capped.endswith(".eml")


def test_build_eml_target():
    bucket = Path(tempfile.gettempdir()) / "2024-05"
    target = m.build_eml_target(bucket, TS, "<CAF123@mail.gmail.com>", "Re: 预算")
    assert target.name == "Re_ 预算_CAF123@mail.gmail.com_20240512.eml", target.name

    deep_bucket = Path("D:/x" + "/verylongsegmentname" * 8 + "/2024-05")   # ~172 chars
    t2 = m.build_eml_target(deep_bucket, TS, "<CAF123@mail.gmail.com>", "Re: 预算" * 10)
    assert len(str(t2)) <= m.EML_PATH_BUDGET, f"len={len(str(t2))} name={t2.name}"
    assert t2.name.endswith("CAF123@mail.gmail.com_20240512.eml"), f"len={len(str(t2))} name={t2.name}"

    t3 = m.build_eml_target(deep_bucket, TS, "<" + "m" * 150 + "@x>", "Re: 预算" * 10)
    assert len(str(t3)) <= m.EML_PATH_BUDGET, f"len={len(str(t3))} name={t3.name}"
    assert len(t3.name) < 45 and "-" in t3.name.split("_")[0], f"len={len(str(t3))} name={t3.name}"

    t4 = m.build_eml_target(bucket, TS, "<CAF123@mail.gmail.com>", None)
    assert t4.name == "CAF123@mail.gmail.com_20240512.eml", t4.name


def test_unique_eml_path(tmp_path):
    target = tmp_path / "a_b_20240512.eml"
    assert m.unique_eml_path(target) == target
    target.write_bytes(b"x")
    u2 = m.unique_eml_path(target)
    assert u2.name == "a_b_20240512（2）.eml", u2.name
    u2.write_bytes(b"x")
    u3 = m.unique_eml_path(target)
    assert u3.name == "a_b_20240512（3）.eml", u3.name

    (tmp_path / "case_20240512.eml").write_bytes(b"x")   # Windows 大小写不敏感碰撞
    u4 = m.unique_eml_path(tmp_path / "CASE_20240512.eml")
    assert u4.name == "CASE_20240512（2）.eml", u4.name


def test_parse_internaldate():
    dt = m.parse_internaldate("12-May-2024 10:15:30 +0800")
    assert dt is not None and dt.utcoffset().total_seconds() == 8 * 3600 and dt.day == 12 and dt.month == 5
    dt2 = m.parse_internaldate("1-Jan-2024 00:00:00 -0500")
    assert dt2 is not None and dt2.utcoffset().total_seconds() == -5 * 3600
    assert m.parse_internaldate("garbage") is None
    assert m.parse_internaldate("99-Zzz-2024 00:00:00 +0000") is None


def test_message_timestamp():
    meta = '1 (INTERNALDATE "12-May-2024 10:15:30 +0800" RFC822 {123}'
    dt = m.message_timestamp(meta, b"")
    assert dt is not None and dt.tzinfo is not None and dt.year == 2024

    raw_with_date = b"From: a@b\r\nDate: Sun, 12 May 2024 02:15:30 +0000\r\nSubject: x\r\n\r\nbody"
    dt2 = m.message_timestamp("", raw_with_date)
    # 结果归一化到本机时区，所以比 UTC 瞬间而不是比 day/hour —— 后者跨时区不成立
    assert dt2 is not None and dt2.tzinfo is not None
    assert dt2.astimezone(timezone.utc) == DATE_HEADER_INSTANT, dt2

    dt3 = m.message_timestamp("", b"no headers here")
    assert dt3 is not None and dt3.tzinfo is not None


def test_folded_header():
    head = (b"From: Alice <a@b.com>\r\nSubject: Re: =?utf-8?B?5L2g5aW9?=\r\n and more\r\n"
            b"Message-ID: <x@y>\r\nX-Other: 1\r\n\r\nbody")
    assert m.folded_header(head, "From") == "Alice <a@b.com>", repr(m.folded_header(head, "From"))
    assert m.folded_header(head, "Subject") == "Re: =?utf-8?B?5L2g5aW9?= and more", (
        repr(m.folded_header(head, "Subject")))
    assert m.folded_header(head, "Message-Id") == "<x@y>", repr(m.folded_header(head, "Message-Id"))
    assert m.folded_header(head, "Cc") == ""


def test_decode_mime_words():
    assert m.decode_mime_words("=?utf-8?B?5L2g5aW9?=") == "你好"
    assert m.decode_mime_words("=?GBK?B?xOO6ww==?=") == "你好"
    assert "你好" in m.decode_mime_words("Re: =?utf-8?B?5L2g5aW9?=")
    assert m.decode_mime_words("plain subject") == "plain subject"


def test_remote_and_local_message_id_keys_agree():
    """两侧判重用的 key 必须同值，否则增量会永远重复下载。"""
    block = b"Message-Id:  <a@b>\r\n"
    normalized = m.BLANKS_RE.sub(" ", str(block, "utf-8", "replace").strip())
    remote_key = m.MSGID_RE.match(normalized).group(1)
    local_key = m.folded_header(b"From: x\r\nMessage-Id:  <a@b>\r\n\r\n", "Message-Id")
    assert remote_key == local_key == "<a@b>", f"remote={remote_key!r} local={local_key!r}"


def test_report_message_passes_a_structured_dict():
    """ADR 0003：回调拿到结构化 dict，终端行走 format_message_line。"""
    lines = []
    m.report_message("INBOX", 12, 345, TS, "Alice <a@b.com>", "Re: =?utf-8?B?5L2g5aW9?=",
                     12600, "<x@y>", False, 0, lines.append)
    assert len(lines) == 1 and isinstance(lines[0], dict), lines
    rec = lines[0]
    assert rec["folder"] == "INBOX" and rec["index"] == 12 and rec["total"] == 345
    assert rec["timestamp"] == TS and rec["from"] == "Alice <a@b.com>"
    assert rec["subject"] == "Re: =?utf-8?B?5L2g5aW9?=" and rec["size"] == 12600
    assert rec["msg_id"] == "<x@y>"

    line = m.format_message_line("INBOX", 12, 345, TS, "Alice <a@b.com>",
                                 "Re: =?utf-8?B?5L2g5aW9?=", 12600, "<x@y>", 0)
    assert line.startswith("[INBOX 12/345] 2024-05-12 10:15 | Alice <a@b.com> | Re: 你好 (12.30 KB)"), line

    verbose = m.format_message_line("INBOX", 12, 345, TS, "Alice <a@b.com>", "s", 100, "<x@y>", 1)
    assert " | <x@y>" in verbose, verbose

    m.report_message("INBOX", 12, 345, TS, "", "", 100, "<x@y>", True, 0, lines.append)
    assert len(lines) == 1


def test_folder_to_eml_relpath():
    assert m.folder_to_eml_relpath("INBOX", ".") == "INBOX"
    assert m.folder_to_eml_relpath("Archive/2024", "/") == "Archive/2024"
    assert m.folder_to_eml_relpath("INBOX.Sub A?", ".") == "INBOX/Sub A_", (
        m.folder_to_eml_relpath("INBOX.Sub A?", "."))


def test_scan_eml_dir(tmp_path):
    inbox = tmp_path / "INBOX"
    (inbox / "2024-05").mkdir(parents=True)
    (inbox / "2024-04").mkdir()
    (inbox / "2024-05" / "a.eml").write_bytes(b"From: a@b\r\nMessage-Id: <aaa@x>\r\n\r\nbody\r\n")
    (inbox / "2024-04" / "b.eml").write_bytes(
        b"Message-Id: <" + m.UUID.encode() + b".deadbeef>\r\n\r\nbody\r\n")
    (inbox / "2024-05" / "c.eml").write_bytes(b"no message id here\r\n")
    (inbox / "2024-05" / "ignore.txt").write_bytes(b"Message-Id: <zzz@x>\r\n")

    log = NullLog()
    got = m.scan_eml_dir(inbox, False, True, True, log)
    assert got == {"<aaa@x>": "<aaa@x>", "<" + m.UUID + ".deadbeef>": "<" + m.UUID + ".deadbeef>"}, got
    assert m.scan_eml_dir(inbox, True, True, True, log) == {}


def test_config_combinations():
    cfg = m.get_config(["-s", "srv", "-u", "u", "-p", "x"])
    assert str(cfg.basedir) != "None"
    assert cfg.eml_dir is None
    # 既没给 --mbox-dir 也没给 --eml-dir 时，默认输出目录就是当前工作目录
    assert cfg.basedir == Path.cwd(), cfg.basedir

    eml_only = m.get_config(["-s", "srv", "-u", "u", "-p", "x", "--eml-dir", "out"])
    assert eml_only.basedir is None and eml_only.eml_dir is not None and eml_only.eml_dir.name == "out"

    dual = m.get_config(["-s", "srv", "-u", "u", "-p", "x", "-d", "mb", "--eml-dir", "out"])
    assert dual.basedir is not None and dual.eml_dir is not None


def test_synthetic_id_fallback_fetch_is_read_only():
    """只读不变量：兜底 header 抓取也必须走 BODY.PEEK。"""
    src = inspect.getsource(m.scan_folder)
    assert "BODY.PEEK[HEADER.FIELDS (FROM TO CC DATE SUBJECT)]" in src
    assert "(BODY[HEADER.FIELDS" not in src

    fetch_src = inspect.getsource(m.download_messages)
    assert "(INTERNALDATE BODY.PEEK[])" in fetch_src
    assert "(INTERNALDATE RFC822)" not in fetch_src


def test_log_level_names_are_not_abbreviated():
    src = inspect.getsource(m.configure_logging)
    assert "%(levelname)s" in src and ".1s" not in src


def test_download_messages_writes_both_formats(tmp_path):
    server = FetchServer({1: RESP1, 2: RESP2})
    m.download_messages(server, "INBOX.mbox", MSGS, False, True, False,
                        tmp_path / "mbox", False, True, NullLog(),
                        eml_dir=tmp_path / "eml" / "INBOX", foldername="INBOX", verbose=0)

    assert all(items == "(INTERNALDATE BODY.PEEK[])" for _, items in server.fetches), server.fetches

    mbox_bytes = (tmp_path / "mbox" / "INBOX.mbox").read_bytes()
    assert b"From nobody " in mbox_bytes
    assert b"\n>From nobody" in mbox_bytes and b"\nFrom nobody secret" not in mbox_bytes
    assert ("Message-Id: " + SYNTHETIC).encode() in mbox_bytes
    assert b"\r\n" not in mbox_bytes

    eml_files = sorted((tmp_path / "eml" / "INBOX").rglob("*.eml"))
    assert len(eml_files) == 2, [p.name for p in eml_files]
    f1 = [p for p in eml_files if "aaa@x" in p.name][0]
    f2 = [p for p in eml_files if p.name != f1.name][0]
    assert f1.name == "Re_ 你好_aaa@x_20240512.eml", f1.name
    assert f1.parent.name == "2024-05" and f1.parent.parent.name == "INBOX"
    b1 = f1.read_bytes()
    assert b"line1\r\nFrom nobody secret\r\nline2" in b1 and not b1.startswith(b"From nobody")
    b2 = f2.read_bytes()
    assert b2.startswith(("Message-Id: " + SYNTHETIC).encode()) and b"no msgid here" in b2
    assert f2.parent.name == "2024-05", f2.parent.name


def test_download_messages_reports_structured_records(tmp_path):
    recs = []
    m.download_messages(FetchServer({1: RESP1, 2: RESP2}), "INBOX.mbox", MSGS, False, True, False,
                        tmp_path / "mbox", False, False, NullLog(),
                        eml_dir=tmp_path / "eml" / "INBOX", foldername="INBOX", verbose=1,
                        report=recs.append)
    assert len(recs) == 2, recs
    assert recs[0]["folder"] == "INBOX" and recs[0]["index"] == 1 and recs[0]["total"] == 2
    assert recs[0]["from"] == "Alice <a@b.com>" and recs[0]["size"] > 0 and recs[0]["msg_id"] == "<aaa@x>"
    assert recs[1]["index"] == 2 and recs[1]["subject"] == "" and recs[1]["msg_id"] == SYNTHETIC


def test_overwrite_wipes_both_sinks_then_redownloads(tmp_path):
    m.download_messages(FetchServer({1: RESP1}), "INBOX.mbox", {"<aaa@x>": 1}, False, True, False,
                        tmp_path / "mbox", False, True, NullLog(),
                        eml_dir=tmp_path / "eml" / "INBOX", foldername="INBOX")
    junk = tmp_path / "eml" / "INBOX" / "2024-04"
    junk.mkdir(parents=True)
    (junk / "junk.eml").write_bytes(b"Message-Id: <junk@x>\r\n")

    m.download_messages(FetchServer({1: RESP1}), "INBOX.mbox", {"<aaa@x>": 1}, True, True, False,
                        tmp_path / "mbox", False, True, NullLog(),
                        eml_dir=tmp_path / "eml" / "INBOX", foldername="INBOX")
    assert (tmp_path / "mbox" / "INBOX.mbox").exists()
    assert not junk.exists() and len(list((tmp_path / "eml" / "INBOX").rglob("*.eml"))) == 1


def test_download_uses_body_peek_fetch_items_for_all_servers(tmp_path):
    for icloud in (False, True):
        server = FetchServer({1: RESP1})
        m.download_messages(server, "INBOX.mbox", {"<aaa@x>": 1}, False, True, False,
                            tmp_path / f"mbox-{icloud}", icloud, True, NullLog(),
                            eml_dir=tmp_path / f"eml-{icloud}" / "INBOX", foldername="INBOX")
        assert server.fetches[0][1] == "(INTERNALDATE BODY.PEEK[])", server.fetches


def test_zero_new_messages_creates_no_directories(tmp_path):
    m.download_messages(FetchServer({}), "INBOX.mbox", {}, False, True, False,
                        tmp_path / "mbox", False, True, NullLog(),
                        eml_dir=tmp_path / "eml" / "INBOX", foldername="INBOX")
    assert not (tmp_path / "mbox").exists() and not (tmp_path / "eml").exists()


def test_should_stop_cancels_mid_folder(tmp_path):
    """ADR 0003：协作式取消，停下的那一刻已下载的部分仍然落盘。"""
    seen = []
    server = FetchServer({1: RESP1, 2: RESP2})
    m.download_messages(server, "INBOX.mbox", MSGS, False, True, False,
                        tmp_path / "mbox", False, False, NullLog(),
                        eml_dir=tmp_path / "eml" / "INBOX", foldername="INBOX",
                        report=seen.append, should_stop=lambda: len(seen) >= 1)
    assert len(server.fetches) == 1, server.fetches
    assert len(seen) == 1, seen
    assert (tmp_path / "mbox" / "INBOX.mbox").exists()
    assert len(list((tmp_path / "eml" / "INBOX").rglob("*.eml"))) == 1

    server2 = FetchServer({1: RESP1, 2: RESP2})
    m.download_messages(server2, "INBOX.mbox", MSGS, False, True, False,
                        tmp_path / "mbox2", False, True, NullLog(),
                        eml_dir=tmp_path / "eml2" / "INBOX", foldername="INBOX", should_stop=lambda: True)
    assert server2.fetches == [], server2.fetches


def test_imap_utf7_decode():
    assert m.imap_utf7_decode("&UXZO1mWHTvZZOQ-") == "其他文件夹"
    assert m.imap_utf7_decode("R&D") == "R&D"
    assert m.imap_utf7_decode("a&-b") == "a&b"
    assert m.imap_utf7_decode("INBOX") == "INBOX"
    assert m.imap_utf7_decode("&XYZ") == "&XYZ"
    assert m.imap_utf7_decode("INBOX&UXZO1mWHTvZZOQ-") == "INBOX其他文件夹"


def test_progress_bar_and_display_width():
    assert m.progress_bar(0, 7) == m.BAR_EMPTY * 12
    assert m.progress_bar(7, 7) == m.BAR_FILL * 12
    assert m.progress_bar(3, 7) == m.BAR_FILL * 5 + m.BAR_EMPTY * 7
    assert m.progress_bar(0, 0) == m.BAR_EMPTY * 12
    assert m._display_width("中文ab") == 6
    assert m._display_width("abc") == 3


def test_account_layer_paths(tmp_path):
    cfg = m.get_config(["-s", "srv", "-u", "3273910043@qq.com", "-p", "x", "--eml-dir", "out"])
    account = m.sanitize_segment(cfg.user, m.EML_SEGMENT_MAX)
    assert account == "3273910043@qq.com"
    assert "3273910043@qq.com" in str(cfg.eml_dir / account / "INBOX"), (
        str(cfg.eml_dir / account / "INBOX"))

    compact = m.get_config(["-s", "srv", "-u", "u", "-p", "x", "--compact"])
    assert compact.compact is True

    m.create_folder_structure([("INBOX", "INBOX.mbox", "INBOX", "INBOX")], tmp_path, "acc@x")
    assert (tmp_path / "acc@x").is_dir()
    m.create_folder_structure([("INBOX.Sub", "Inbox.sbd/Sub.mbox", "INBOX/Sub", "INBOX.Sub")],
                              tmp_path, "acc@x")
    assert (tmp_path / "acc@x" / "Inbox.sbd").is_dir()


def test_get_names_decodes_utf7_folder_names():
    names = m.get_names(ListServer(["INBOX", "其他文件夹"]), False, True, True, NullLog())
    assert len(names) == 2, names
    cn_row = names[1]
    assert cn_row[0] == "&UXZO1mWHTvZZOQ-"
    assert cn_row[3] == "其他文件夹", cn_row
    assert cn_row[2] == "其他文件夹", cn_row
    assert cn_row[1] == "其他文件夹.mbox", cn_row
    assert names[0][1] == "INBOX.mbox" and names[0][3] == "INBOX"


def test_folder_matching_accepts_raw_or_decoded_name():
    names = m.get_names(ListServer(["其他文件夹"]), False, True, True, NullLog())
    decoded = [n for n in names if n[0] in {"其他文件夹"} or n[3] in {"其他文件夹"}]
    assert len(decoded) == 1 and decoded[0][3] == "其他文件夹"
    raw = [n for n in names if n[0] in {"&UXZO1mWHTvZZOQ-"} or n[3] in {"&UXZO1mWHTvZZOQ-"}]
    assert len(raw) == 1


def test_scan_folder_returns_ids_under_the_remote_name():
    remote = m.scan_folder(HeaderServer(), "&UXZO1mWHTvZZOQ-", True, True, NullLog(), display="其他文件夹")
    assert remote == {"<r1@x>": 1}, remote


def test_spinner_status_line_rendering(capsys):
    # 构造时 disabled=True，再手动翻开：不依赖 _has_console() 的真实终端判定
    sp = m.Spinner("", True, quiet=False)
    sp.disabled = False
    sp.status("0123456789AB")
    sp.status("xy")
    out1 = capsys.readouterr().out
    sp.erase()
    out2 = capsys.readouterr().out

    sp2 = m.Spinner("", True, quiet=False)
    sp2.disabled = False
    sp2.status("中文")
    sp2.status("a")
    out3 = capsys.readouterr().out

    assert "\r| 0123456789AB" in out1, repr(out1)
    assert out1.startswith("\r| "), repr(out1[:20])
    assert "\r| xy" + " " * 10 in out1, repr(out1)
    assert ("\r" + " " * 14 + "\r") in out2, repr(out2)
    assert "\r| a" + " " * 3 in out3, repr(out3)


def test_download_messages_compact_status_line_wiring(monkeypatch, tmp_path):
    events = []
    recs = []
    monkeypatch.setattr(m, "Spinner", recording_spinner(events))
    m.download_messages(FetchServer({1: RESP1, 2: RESP2}), "INBOX.mbox", MSGS, False, True, False,
                        tmp_path / "mbox", False, True, NullLog(),
                        eml_dir=tmp_path / "eml" / "INBOX", foldername="其他文件夹",
                        verbose=0, compact=True, report=recs)
    assert [e[0] for e in events] == ["init", "status", "spin", "status", "spin", "stop"], events
    assert "其他文件夹" in events[0][1], events[0]
    first_status = [e for e in events if e[0] == "status"][0][1]
    assert "[" + m.BAR_FILL in first_status
    assert "1/2" in first_status and "其他文件夹" in first_status, first_status
    assert "Re: 你好" in first_status, first_status
    assert recs == [], recs   # compact 抑制逐封终端行，report 回调不触发


def test_download_messages_non_compact_status_line_wiring(monkeypatch, tmp_path):
    events = []
    monkeypatch.setattr(m, "Spinner", recording_spinner(events))
    m.download_messages(FetchServer({1: RESP1}), "INBOX.mbox", {"<aaa@x>": 1}, False, True, False,
                        tmp_path / "mbox", False, False, NullLog(),
                        eml_dir=tmp_path / "eml" / "INBOX", foldername="INBOX", verbose=0, compact=False)
    assert [e[0] for e in events] == ["init", "erase", "status", "spin", "stop"], events
    status = events[2][1]
    assert "INBOX" in status and "1/1" in status
    assert "[" + m.BAR_FILL * 12 + "]" in status and " · " in status, status


def test_spinner_status_line_clamps_to_terminal_width(capsys):
    cols = shutil.get_terminal_size().columns
    sp = m.Spinner("", True, quiet=False)
    sp.disabled = False
    sp.status("x" * 300)
    out1 = capsys.readouterr().out

    sp2 = m.Spinner("", True, quiet=False)
    sp2.disabled = False
    sp2.status("下" * 100)
    out2 = capsys.readouterr().out

    segs1 = [s for s in out1.split("\r") if s]
    assert len(segs1) == 1 and len(segs1[0]) <= cols, (cols, [len(s) for s in segs1])
    segs2 = [s for s in out2.split("\r") if s]
    assert len(segs2) == 1 and segs2[0].endswith("…"), (segs2[0][-6:] if segs2 else "")


def test_folder_separator(capsys):
    m.folder_separator(3, 8, "其他文件夹")
    out = capsys.readouterr().out
    lines = [line for line in out.split("\n") if line]
    cols = shutil.get_terminal_size().columns
    assert "[3/8] 其他文件夹" in out, repr(out[:40])
    assert out.startswith("\n"), repr(out[:10])
    assert lines and all(m._display_width(line) <= cols for line in lines), (
        [(cols, len(line), m._display_width(line)) for line in lines])
