"""CLI 当日运行日志（ADR 0005）。

CLI 与 GUI 共用同一日文件，共享构造不共享安装。这里用假 IMAP 服务器驱动 main() 跑全
流程，断言：日志内容与共享格式、--quiet 不削减文件内 INFO、失败/异常/中断被记录但
不在 stderr 重复打印、日志目录不可写时降级、configure_logging 幂等、退出码不变。
"""
import contextlib
import io
import itertools
import logging
import re
from collections import namedtuple
from datetime import datetime

import pytest

import imapbackup312 as core
from fakes import MainServer

# 共享日志格式：时间 | 级别 | 来源 | 函数:行号 | 内容
LINE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \| (?P<level>[A-Z]+)\s*\| (?P<source>\S+)\s*\| "
    r"(?P<loc>[\w<>]+:\d+) \| (?P<msg>.+)$"
)
DOWNGRADE = "imapbackup: cannot write run log to "
ABORTED = "Run aborted:"
FAILED = "Run failed"
INTERRUPTED = "Run interrupted by user"

Result = namedtuple("Result", ["outcome", "code", "stdout", "stderr", "log_dir", "mbox_dir"])


def argv(mbox_dir, *extra):
    return ["-s", "imap.example.com", "-e", "-u", "x@y.com", "-p", "secret",
            "-d", str(mbox_dir), *extra]


def connect_like(server):
    """A connect_and_login replacement that mirrors the real function's INFO lines.

    那两行是不受 quiet 门控的，正是「--quiet 不削减文件内 INFO」的证据来源，不能省。
    """
    def connect(cfg, log):
        log.info("Connecting to '%s' TCP %s, SSL", cfg.server, cfg.port)
        log.info("Logging in as '%s'", cfg.user)
        return server
    return connect


def auth_fail(cfg, log):
    log.info("Connecting to '%s' TCP %s, SSL", cfg.server, cfg.port)
    log.info("Logging in as '%s'", cfg.user)
    raise SystemExit("IMAP authentication failed: [AUTHENTICATIONFAILED] Invalid credentials")


def log_lines(log_dir):
    path = log_dir / f"run-{datetime.now():%Y-%m-%d}.log"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def records(lines):
    """Group physical lines into records.

    一条记录的 message 可能跨多个物理行（文件夹列表、traceback 都含换行），
    所以只有以时间戳开头的行才算新记录。
    """
    out = []
    for line in lines:
        m = LINE_RE.match(line)
        if m:
            out.append(m.groupdict())
        elif out:
            out[-1]["msg"] += "\n" + line
        else:
            out.append({"level": "?", "source": "?", "loc": "?", "msg": line})
    return out


def run_raw(buf, argv_list):
    """Run main() with a raw argv and return its exit code."""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(buf):
        try:
            return core.main(argv_list)
        except SystemExit as exc:
            return exc.code


@pytest.fixture
def run_main(tmp_path, monkeypatch, console_capture):
    """Drive main() with a fake server; each call gets its own log and output dirs.

    connect 是 (cfg, log) 签名的替身；不传则不动 connect_and_login。
    log_dir 用于不可写目录这类需要自定义路径的用例。
    """
    calls = itertools.count()

    def run_main(connect=None, *extra, log_dir=None):
        index = next(calls)
        log_dir = log_dir if log_dir is not None else tmp_path / f"log{index}"
        mbox_dir = tmp_path / f"mbox{index}"
        monkeypatch.setattr(core, "LOG_DIR", log_dir)
        if connect is not None:
            monkeypatch.setattr(core, "connect_and_login", connect)
        console_capture.seek(0)
        console_capture.truncate(0)
        out = io.StringIO()
        outcome, code = "returned", 0
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(console_capture):
            try:
                code = core.main(argv(mbox_dir, *extra))
            except SystemExit as exc:
                outcome, code = "SystemExit", exc.code if isinstance(exc.code, int) else 1
            except KeyboardInterrupt:
                outcome, code = "KeyboardInterrupt", 1
            except Exception:
                outcome, code = "Exception", 1
        return Result(outcome, code, out.getvalue(), console_capture.getvalue(), log_dir, mbox_dir)

    return run_main


def test_successful_run_writes_a_formatted_daily_log(run_main):
    res = run_main(connect_like(MainServer()), "--folders", "INBOX")
    lines = log_lines(res.log_dir)
    parsed = records(lines)
    ts_lines = [line for line in lines if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \| ", line)]

    assert res.outcome == "returned" and res.code == 0, f"{res.outcome} {res.code}"
    assert len(lines) > 0, lines
    assert len(parsed) == len(ts_lines) and parsed and parsed[0]["level"] != "?", (
        f"records={len(parsed)} timestamped={len(ts_lines)}")
    assert {p["level"] for p in parsed} == {"INFO"}, {p["level"] for p in parsed}
    assert {p["source"] for p in parsed} == {"imapbackup"}, {p["source"] for p in parsed}
    assert all(re.match(r"[\w<>]+:\d+$", p["loc"]) for p in parsed), [p["loc"] for p in parsed]
    assert any("Found 1 folders:" in p["msg"] and "- INBOX" in p["msg"] for p in parsed), (
        [p["msg"] for p in parsed])
    assert (any("Connecting to" in p["msg"] for p in parsed)
            and any("Logging in as" in p["msg"] for p in parsed)
            and any("Found 1 folders" in p["msg"] for p in parsed)
            and any("Disconnecting" in p["msg"] for p in parsed)), lines
    assert parsed[0]["msg"] == f"imapbackup v{core.__version__} starting", parsed[0]["msg"]
    assert sum(1 for p in parsed if "starting" in p["msg"]) == 1, [p["msg"] for p in parsed]
    assert f"imapbackup v{core.__version__}" in res.stderr, res.stderr
    assert "secret" not in "\n".join(lines)
    assert (res.mbox_dir / "x@y.com").exists()


def test_quiet_silences_stderr_but_the_file_still_gets_info(run_main):
    res = run_main(connect_like(MainServer()), "--quiet", "--folders", "INBOX")
    parsed = records(log_lines(res.log_dir))

    assert res.stdout.strip() == "" and res.stderr.strip() == "", repr(res.stderr)
    assert any(p["level"] == "INFO" for p in parsed), log_lines(res.log_dir)
    assert any(p["msg"] == f"imapbackup v{core.__version__} starting" for p in parsed), (
        log_lines(res.log_dir))
    assert f"imapbackup v{core.__version__}" not in res.stderr, repr(res.stderr)
    assert any("Connecting to" in p["msg"] for p in parsed) and any(
        "Logging in as" in p["msg"] for p in parsed), log_lines(res.log_dir)
    # call-site 门控的行仍然没有 —— 这是既知行为，不是回归
    assert not any("Disconnecting" in p["msg"] for p in parsed) and not any(
        "INBOX: 0 messages" in p["msg"] for p in parsed), log_lines(res.log_dir)
    assert res.outcome == "returned" and res.code == 0


def test_controlled_abort_is_recorded_not_reprinted_on_stderr(run_main):
    res = run_main(auth_fail, "--folders", "INBOX")
    parsed = records(log_lines(res.log_dir))

    assert res.outcome == "SystemExit" and res.code != 0, f"{res.outcome} {res.code}"
    assert any(ABORTED in p["msg"] for p in parsed), log_lines(res.log_dir)
    assert any("IMAP authentication failed" in p["msg"] for p in parsed), log_lines(res.log_dir)
    assert any(p["level"] == "WARNING" for p in parsed if ABORTED in p["msg"])
    assert [p["source"] for p in parsed if ABORTED in p["msg"]] == [core.RUN_LOG_LOGGER], (
        [p["source"] for p in parsed])
    assert ABORTED not in res.stderr, res.stderr
    assert any("Connecting to" in p["msg"] for p in parsed), log_lines(res.log_dir)


def test_unexpected_exception_is_recorded_with_a_traceback(run_main):
    res = run_main(connect_like(MainServer(messages=1, fail_fetch="boom")), "--folders", "INBOX")
    lines = log_lines(res.log_dir)
    joined = "\n".join(lines)

    assert res.outcome == "Exception" and res.code != 0, f"{res.outcome} {res.code}"
    assert any(FAILED in p["msg"] for p in records(lines)), lines
    assert "Traceback (most recent call last):" in joined, lines
    assert "FETCH failed for UID" in joined
    assert FAILED not in res.stderr, res.stderr


def test_user_interrupt_is_recorded(run_main):
    res = run_main(connect_like(MainServer(messages=1, interrupt_fetch=True)), "--folders", "INBOX")
    lines = log_lines(res.log_dir)

    assert res.outcome == "KeyboardInterrupt", res.outcome
    assert any(INTERRUPTED in p["msg"] for p in records(lines)), lines
    assert INTERRUPTED not in res.stderr and "Traceback" not in res.stderr, repr(res.stderr)


def test_unopenable_log_dir_degrades_instead_of_aborting(run_main, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    res = run_main(connect_like(MainServer()), "--folders", "INBOX", log_dir=blocker / "log")

    assert res.outcome == "returned" and res.code == 0, f"{res.outcome} {res.code}"
    assert DOWNGRADE in res.stderr, res.stderr
    assert not (blocker / "log").exists()
    assert (res.mbox_dir / "x@y.com").exists()


def test_configuring_twice_in_one_process_cannot_write_twice(tmp_path, monkeypatch, console_capture):
    log_dir = tmp_path / "log"
    monkeypatch.setattr(core, "LOG_DIR", log_dir)
    cfg = core.parse_args_to_config(argv(tmp_path / "mbox", "--folders", "INBOX"))
    console_capture.seek(0)
    console_capture.truncate(0)
    with contextlib.redirect_stderr(console_capture):
        core.configure_logging(cfg)
        core.configure_logging(cfg)   # 第二次调用必须幂等
    err = console_capture.getvalue()

    file_handlers = [h for h in logging.root.handlers if isinstance(h, logging.FileHandler)]
    assert len(file_handlers) == 1, len(file_handlers)
    assert DOWNGRADE not in err, err
    assert len(logging.getLogger(core.RUN_LOG_LOGGER).handlers) == 1
    logging.getLogger("imapbackup").info("marker")
    logging.getLogger(core.RUN_LOG_LOGGER).warning("marker")
    lines = log_lines(log_dir)
    assert sum(1 for line in lines if "marker" in line) == 2, lines


def test_exit_codes_unchanged(run_main, tmp_path, monkeypatch, console_capture):
    res = run_main(connect_like(MainServer()), "--folders", "INBOX")
    assert res.code == 0, res.code

    argparse_log = tmp_path / "argparse-log"
    monkeypatch.setattr(core, "LOG_DIR", argparse_log)
    console_capture.seek(0)
    console_capture.truncate(0)
    code = run_raw(console_capture, ["-s", "imap.example.com"])
    assert code == 2, code
    assert not argparse_log.exists()
    assert DOWNGRADE not in console_capture.getvalue(), console_capture.getvalue()

    def auth_denied(cfg, log):
        raise SystemExit("IMAP authentication failed: denied")

    monkeypatch.setattr(core, "LOG_DIR", tmp_path / "auth-log")
    monkeypatch.setattr(core, "connect_and_login", auth_denied)
    console_capture.seek(0)
    console_capture.truncate(0)
    code = run_raw(console_capture, argv(tmp_path / "auth-mbox", "--folders", "INBOX"))
    assert code != 0, code


def test_version_answers_without_starting_a_run(tmp_path, monkeypatch, console_capture):
    log_dir = tmp_path / "log"
    monkeypatch.setattr(core, "LOG_DIR", log_dir)
    out = io.StringIO()
    console_capture.seek(0)
    console_capture.truncate(0)
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(console_capture):
        try:
            core.main(["--version"])
            code = 0
        except SystemExit as exc:
            code = exc.code

    assert code == 0, code
    assert out.getvalue().strip() == f"imapbackup {core.__version__}", repr(out.getvalue())
    assert console_capture.getvalue() == "", repr(console_capture.getvalue())
    assert not log_dir.exists()
    assert "--version" in core.build_arg_parser().format_help()


def test_cli_runs_uid_scan_and_batch_fetch_end_to_end(run_main):
    """WP-1：CLI 一次备份走 UID 扫描 + 单批正文取信，逐封报告与账户汇总保持不变。"""
    server = MainServer(messages=3)
    res = run_main(connect_like(server), "--folders", "INBOX")
    parsed = records(log_lines(res.log_dir))

    assert res.outcome == "returned" and res.code == 0, f"{res.outcome} {res.code}"
    assert [call for call in server.uid_calls if call[0] == "SEARCH"] == [("SEARCH", ["ALL"])]
    assert server.body_fetches == [[1, 2, 3]], server.body_fetches
    assert [call for call in server.fetches if "BODY.PEEK[]" in call[1]] == [
        ("1,2,3", core.BODY_FETCH_ITEMS)], server.fetches

    summary = [p for p in parsed if p["msg"].startswith("Account ")]
    assert len(summary) == 1 and summary[0]["level"] == "INFO", summary
    assert "3 backed, 0 skipped, 0 fetch failed, 0 folder failure(s)" in summary[0]["msg"], summary
    assert "elapsed" in summary[0]["msg"], summary

    report_lines = [line for line in res.stdout.splitlines() if line.startswith("[INBOX ")]
    assert [line.split("]")[0] for line in report_lines] == ["[INBOX 1/3", "[INBOX 2/3", "[INBOX 3/3"]
    mbox = (res.mbox_dir / "x@y.com" / "INBOX.mbox").read_bytes()
    assert mbox.count(b"From nobody ") == 3, mbox


def test_cli_continues_after_a_folder_select_failure(run_main):
    """K 组：单个文件夹失败记 WARNING、继续下一个文件夹，退出码仍为 0。"""
    server = MainServer(messages=2, folders=("Archive", "INBOX"), select_fail_folders={"Archive"})
    res = run_main(connect_like(server), "--folders", "Archive,INBOX")
    parsed = records(log_lines(res.log_dir))

    assert res.outcome == "returned" and res.code == 0, f"{res.outcome} {res.code}"
    summary = [p for p in parsed if p["msg"].startswith("Account ")]
    assert len(summary) == 1, summary
    assert "2 backed, 0 skipped, 0 fetch failed, 1 folder failure(s)" in summary[0]["msg"], summary
    assert any(p["level"] == "WARNING" and "Archive: folder failed (scan): SELECT failed" in p["msg"]
               for p in parsed), parsed
    mbox = (res.mbox_dir / "x@y.com" / "INBOX.mbox").read_bytes()
    assert mbox.count(b"From nobody ") == 2, mbox


def test_cli_continues_after_a_whole_folder_body_rejection(run_main):
    """C 组：整批逐封 NO/BAD 升级为文件夹失败，CLI 记录后继续下一个文件夹。"""
    server = MainServer(messages=1, folders=("INBOX", "ZZ_Ok"), no_bad_folders={"INBOX"})
    res = run_main(connect_like(server), "--folders", "INBOX,ZZ_Ok")
    parsed = records(log_lines(res.log_dir))

    assert res.outcome == "returned" and res.code == 0, f"{res.outcome} {res.code}"
    summary = [p for p in parsed if p["msg"].startswith("Account ")]
    assert len(summary) == 1, summary
    assert "1 backed, 0 skipped, 0 fetch failed, 1 folder failure(s)" in summary[0]["msg"], summary
    assert any(p["level"] == "WARNING" and "INBOX: folder failed (fetch): "
               "every UID in a FETCH batch was rejected" in p["msg"] for p in parsed), parsed
    mbox = (res.mbox_dir / "x@y.com" / "ZZ_Ok.mbox").read_bytes()
    assert mbox.count(b"From nobody ") == 1, mbox
