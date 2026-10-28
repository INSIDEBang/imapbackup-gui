"""有界重连与续跑（规格 §5.7，工单 03）。

覆盖 E 组（分类、预算、退避、LIST/扫描重跑、UIDVALIDITY 续跑、写盘错误）与
F 组的「停止优先 / 停止后无新请求」。退避一律注入，不产生真实睡眠。
"""
import contextlib
import imaplib
import io
import types

import pytest

import imapbackup312 as core
from fakes import MainServer, NullLog, UidFetchServer


class _Conn:
    """只记录 shutdown 的连接替身：run_with_reconnect 只要求可关闭。"""

    def __init__(self):
        self.closed = False

    def shutdown(self):
        self.closed = True


@pytest.fixture(autouse=True)
def no_backoff_sleep(monkeypatch):
    """全抖动取 0：退避路径不等待，测试只关心次数与顺序。"""
    monkeypatch.setattr(core.random, "uniform", lambda _low, _high: 0.0)


# ---------------------------------------------------------------- 错误分类

@pytest.mark.parametrize(("exc", "reason"), [
    (imaplib.IMAP4.abort("socket error: EOF"), "abort"),
    (ConnectionResetError("connection reset"), "oserror"),
    (imaplib.IMAP4.error("server said BYE"), "bye"),
])
def test_connection_errors_are_retried(exc, reason):
    """abort / 非 BackupWriteError 的 OSError / 文本含 BYE 的 IMAP4.error 都可重试。"""
    old, fresh = _Conn(), _Conn()
    calls = []

    def op(conn):
        calls.append(conn)
        if len(calls) == 1:
            raise exc
        return "done"

    stats = core.ReconnectStats()
    result, live = core.run_with_reconnect(old, lambda: fresh, op, should_stop=None,
                                           log=NullLog(), stats=stats)
    assert result == "done"
    assert live is fresh                     # 调用方用返回的连接替换旧连接
    assert old.closed and not fresh.closed
    assert (stats.attempts, stats.successes, stats.last_reason) == (1, 1, reason)


@pytest.mark.parametrize("exc", [
    imaplib.IMAP4.error("NO such mailbox"),
    imaplib.IMAP4.error("BAD command"),
    core.BackupWriteError("disk full"),
    core.SkipFolderException("no uidvalidity"),
    core.FolderFetchError("folder failed"),
    core.ReconnectStopped("stop"),
    ValueError("bug"),
])
def test_non_connection_errors_propagate_without_reconnect(exc):
    """认证/NO/BAD、文件夹级失败、写盘错误、停止都不是连接级错误。"""
    connects = []

    def op(_conn):
        raise exc

    with pytest.raises(type(exc)) as info:
        core.run_with_reconnect(_Conn(), lambda: connects.append(_Conn()), op,
                                should_stop=None, log=NullLog())
    assert info.value is exc
    assert connects == []


def test_backup_write_error_is_not_an_oserror():
    """分类顺序依赖这一点：否则写盘错误会被当成连接错误重试。"""
    assert not issubclass(core.BackupWriteError, OSError)


def test_a_connection_left_closed_by_a_previous_call_is_reconnected():
    """上一轮失败把新连接也关掉后，调用方只剩 LOGOUT 旧连接：下一轮先重连，不在死链上发请求。"""
    class _DeadConn(_Conn):
        state = "LOGOUT"

    old, fresh = _DeadConn(), _Conn()
    seen = []

    def op(conn):
        seen.append(conn)
        return "ok"

    stats = core.ReconnectStats()
    result, live = core.run_with_reconnect(old, lambda: fresh, op, should_stop=None,
                                           log=NullLog(), stats=stats)
    assert result == "ok" and live is fresh
    assert seen == [fresh]                   # 请求只落在新连接上
    assert old.closed                        # 死连接先被关掉
    assert (stats.attempts, stats.successes) == (1, 1)


def test_authentication_failure_stops_the_budget_immediately():
    """密码错误不烧预算：一次认证失败直接交给调用方。"""
    connects = []

    def connect():
        connects.append(1)
        raise core.ReconnectAuthError("IMAP authentication failed: bad")

    def op(_conn):
        raise imaplib.IMAP4.abort("socket error: EOF")

    stats = core.ReconnectStats()
    with pytest.raises(core.ReconnectAuthError):
        core.run_with_reconnect(_Conn(), connect, op, should_stop=None,
                                log=NullLog(), stats=stats)
    assert len(connects) == 1


def test_sixth_consecutive_failure_is_not_retried():
    """一次连续失败最多重试 5 次；第 6 次失败原样上抛并标记耗尽。"""
    calls = []

    def op(_conn):
        calls.append(1)
        raise imaplib.IMAP4.abort("socket error: EOF")

    stats = core.ReconnectStats()
    with pytest.raises(imaplib.IMAP4.abort):
        core.run_with_reconnect(_Conn(), _Conn, op, should_stop=None,
                                log=NullLog(), stats=stats)
    assert len(calls) == 6                   # 首次 + 5 次重试
    assert stats.attempts == 5
    assert stats.exhausted is True


def test_success_clears_the_consecutive_failure_budget():
    """成功一次后预算清零：下一轮操作失败仍能重试满 5 次，不被上一轮用掉的额度拖累。"""
    outcomes = [imaplib.IMAP4.abort("socket error: EOF")] * 2 + ["ok"]
    calls = []

    def succeed_after_two(_conn):
        calls.append(1)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    stats = core.ReconnectStats()
    result, _live = core.run_with_reconnect(_Conn(), _Conn, succeed_after_two,
                                            should_stop=None, log=NullLog(), stats=stats)
    assert result == "ok"
    assert (len(calls), stats.attempts, stats.successes, stats.exhausted) == (3, 2, 2, False)

    seen = []

    def always_aborts(_conn):
        seen.append(1)
        raise imaplib.IMAP4.abort("socket error: EOF")

    with pytest.raises(imaplib.IMAP4.abort):
        core.run_with_reconnect(_Conn(), _Conn, always_aborts,
                                should_stop=None, log=NullLog(), stats=stats)
    assert len(seen) == 6                    # 新一轮从零开始：首次 + 5 次重试


def test_connect_failures_share_the_same_budget(monkeypatch):
    """重连拨号失败也计入 5 次预算，耗尽时抛出最后一次连接错误。"""
    waits = []
    monkeypatch.setattr(core, "_wait_before_reconnect",
                        lambda delay, should_stop: waits.append(delay) or False)

    def op(_conn):
        raise imaplib.IMAP4.abort("socket error: EOF")

    def connect():
        raise ConnectionError("connection refused")

    stats = core.ReconnectStats()
    with pytest.raises(ConnectionError):
        core.run_with_reconnect(_Conn(), connect, op, should_stop=None,
                                log=NullLog(), stats=stats)
    assert len(waits) == 5
    assert stats.exhausted is True


# ---------------------------------------------------------------- 退避与停止

def test_backoff_caps_are_2_8_32_128_300_with_full_jitter(monkeypatch):
    """第 n 次重试的抖动区间是 [0, min(300, 2*4**(n-1))]。"""
    caps = []
    waits = []

    def fake_wait(delay, should_stop):
        waits.append(delay)
        return False

    def fake_uniform(low, high):
        caps.append((low, high))
        return high                          # 取上界，验证上限被真的用上

    monkeypatch.setattr(core, "_wait_before_reconnect", fake_wait)
    monkeypatch.setattr(core.random, "uniform", fake_uniform)

    def op(_conn):
        raise imaplib.IMAP4.abort("socket error: EOF")

    with pytest.raises(imaplib.IMAP4.abort):
        core.run_with_reconnect(_Conn(), _Conn, op, should_stop=None, log=NullLog())
    assert caps == [(0.0, 2.0), (0.0, 8.0), (0.0, 32.0), (0.0, 128.0), (0.0, 300.0)]
    assert waits == [2.0, 8.0, 32.0, 128.0, 300.0]


def test_stop_is_seen_within_a_quarter_second_slice(monkeypatch):
    """退避期间每 0.2 秒检查停止，不睡满整个延迟。"""
    clock = [100.0]
    sleeps = []

    def fake_monotonic():
        return clock[0]

    def fake_sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(core.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(core.time, "sleep", fake_sleep)
    started = clock[0]

    def should_stop():
        return clock[0] - started >= 0.5

    assert core._wait_before_reconnect(5.0, should_stop) is True
    assert sleeps and max(sleeps) <= core.RECONNECT_STOP_POLL_S
    assert 0.5 <= clock[0] - started < 1.0


def test_stop_during_backoff_forbids_further_attempts(monkeypatch):
    """退避中收到停止：不再拨号，也不触发第 6 次重试。"""
    monkeypatch.setattr(core, "_wait_before_reconnect", lambda delay, should_stop: True)
    connects = []

    def op(_conn):
        raise imaplib.IMAP4.abort("socket error: EOF")

    stats = core.ReconnectStats()
    with pytest.raises(core.ReconnectStopped):
        core.run_with_reconnect(_Conn(), lambda: connects.append(_Conn()), op,
                                should_stop=lambda: False, log=NullLog(), stats=stats)
    assert connects == []
    assert stats.attempts == 1
    assert stats.exhausted is False


def test_stop_wins_over_exhaustion_after_the_last_failed_attempt():
    """第 6 次失败时停止已到达：按停止收口，不把最后一跳记成耗尽（规格 §5.7.8）。"""
    calls = []
    connects = []

    def op(_conn):
        calls.append(1)
        raise imaplib.IMAP4.abort("socket error: EOF")

    def connect():
        connects.append(_Conn())
        return _Conn()

    stats = core.ReconnectStats()
    with pytest.raises(core.ReconnectStopped):
        core.run_with_reconnect(_Conn(), connect, op,
                                should_stop=lambda: len(calls) >= 6,
                                log=NullLog(), stats=stats)
    assert len(calls) == 6                   # 停止前已经发生的失败照常计数
    assert len(connects) == 5                # 停止到达后不再拨号
    assert stats.exhausted is False


def test_stop_before_the_next_request_sends_nothing():
    """停止信号到达时关闭旧连接、不发新请求（规格 §5.7.8）。"""
    calls = []
    old = _Conn()
    with pytest.raises(core.ReconnectStopped):
        core.run_with_reconnect(old, _Conn, lambda conn: calls.append(conn),
                                should_stop=lambda: True, log=NullLog())
    assert calls == []
    assert old.closed


# ---------------------------------------------------------------- 拨号分类

@pytest.mark.parametrize(("message", "expected"), [
    ("IMAP authentication failed: bad", core.ReconnectAuthError),
    ("DNS lookup failed for 'imap.example.com': boom", ConnectionError),
    ("Connection to 'imap.example.com' failed: refused", ConnectionError),
    ("Invalid port in --server", SystemExit),
])
def test_connect_for_reconnect_classifies_connect_and_login_failures(monkeypatch, message, expected):
    """connect_and_login 的 SystemExit 按消息分类：只有连接类失败可重试。"""

    def fail(_cfg, _log):
        raise SystemExit(message)

    monkeypatch.setattr(core, "connect_and_login", fail)
    with pytest.raises(expected):
        core.connect_for_reconnect(types.SimpleNamespace(), NullLog())


# ---------------------------------------------------------------- 下载续跑

def _raw(uid: int) -> bytes:
    return (f"From: sender{uid}@example.com\r\nSubject: message {uid}\r\n"
            f"Message-Id: <m{uid}@x>\r\n\r\nbody {uid}\r\n").encode()


def test_download_resumes_after_abort_without_rewriting(tmp_path):
    """UIDVALIDITY 不变：重连后只取剩余 UID，已写消息不重复写（规格 §5.7.6）。"""
    log = NullLog()
    raws = {uid: _raw(uid) for uid in range(1, 11)}
    server1, server2 = UidFetchServer(dict(raws)), UidFetchServer(dict(raws))
    body_calls = []

    def drop_on_second_body(_server, uids):
        body_calls.append(list(uids))
        if len(body_calls) == 2:             # 第一批 8 封写完后、第二批取信时断线
            raise imaplib.IMAP4.abort("socket error: EOF")

    for server in (server1, server2):
        server.body_hook = drop_on_second_body
    scan = core.scan_folder(server1, "INBOX", True, quiet=True, log=log)
    state = core.FolderDownloadState(scan, core.pending_messages(scan.messages, {}, None))
    written: set[str] = set()
    progress = core.DownloadProgress()
    attempts = []

    def download(conn, messages, overwrite_now):
        attempts.append(sorted(uid for uid, _size in messages.values()))
        core.download_messages(conn, "INBOX.mbox", messages, overwrite_now, True, False,
                               tmp_path, False, quiet=True, log=log,
                               written=written, progress=progress)

    fresh = iter([server2])
    live, state = core.download_folder_with_reconnect(
        server1, lambda: next(fresh), state, foldername="INBOX", display="INBOX",
        local_mbox=None, local_eml=None, written=written, download=download,
        rescan=lambda conn: core.scan_folder(conn, "INBOX", True, quiet=True, log=log),
        should_stop=None, log=log)
    assert live is server2
    assert attempts == [list(range(1, 11)), [9, 10]]  # 第二次只剩断线时未写的两封
    assert server2.body_fetches == [[9, 10]]  # 重连后重新 SELECT，只请求剩余两封
    assert progress.backed == 10
    assert len(written) == 10
    box = core.mailbox.mbox(tmp_path / "INBOX.mbox")
    try:
        ids = sorted(m.get("Message-Id") for m in box)
    finally:
        box.close()
    assert ids == sorted(f"<m{uid}@x>" for uid in range(1, 11))


def test_uidvalidity_change_rescans_once_reuses_local_and_subtracts_written():
    """变化：远端重扫一次、复用本地快照、减去 written；二次变化判文件夹失败。"""
    log = NullLog()
    server1 = UidFetchServer({1: _raw(1), 2: _raw(2)}, uidvalidity=777)
    server2 = UidFetchServer({1: _raw(1), 2: _raw(2), 3: _raw(3)}, uidvalidity=888)
    server3 = UidFetchServer({1: _raw(1), 2: _raw(2), 3: _raw(3)}, uidvalidity=999)
    scan = core.FolderScan({"<m1@x>": (1, len(_raw(1))), "<m2@x>": (2, len(_raw(2)))}, 777)
    local_mbox = {"<m1@x>": "<m1@x>"}        # 本地已有 m1，不应再次出现在待备份里
    state = core.FolderDownloadState(scan, core.pending_messages(scan.messages, local_mbox, None))
    written: set[str] = set()
    downloads = []
    changes = []

    def download(conn, messages, overwrite_now):
        downloads.append(dict(messages))
        if len(downloads) == 1:
            written.add("<m2@x>")            # 模拟断线前已写完一封
        raise imaplib.IMAP4.abort("socket error: EOF")

    def on_scan_changed(new_scan, pending):
        changes.append((new_scan.uidvalidity, set(pending)))

    fresh = iter([server2, server3])
    stats = core.ReconnectStats()
    with pytest.raises(core.FolderFetchError):
        core.download_folder_with_reconnect(
            server1, lambda: next(fresh), state, foldername="INBOX", display="INBOX",
            local_mbox=local_mbox, local_eml=None, written=written, download=download,
            rescan=lambda conn: core.scan_folder(conn, "INBOX", True, quiet=True, log=log),
            should_stop=None, log=log, on_scan_changed=on_scan_changed, stats=stats)
    assert set(downloads[0]) == {"<m2@x>"}   # m1 命中本地快照
    assert set(downloads[1]) == {"<m3@x>"}   # m2 已 written，新出现的 m3 继续取
    assert changes == [(888, {"<m3@x>"})]    # 重扫后的 pending 也要减去 written
    assert set(state.pending) == {"<m3@x>"}
    assert stats.uidvalidity_changes == 1
    assert stats.attempts == 2
    assert state.uidvalidity_checked is True
    assert state.scan.uidvalidity == 888


def test_write_errors_are_not_retried():
    """本地写盘错误不进入重连，原样交给账户级失败路径（规格 §5.7.7）。"""
    log = NullLog()
    server = UidFetchServer({1: _raw(1)})
    state = core.FolderDownloadState(core.FolderScan({"<m1@x>": (1, len(_raw(1)))}, 777),
                                     {"<m1@x>": (1, len(_raw(1)))})
    connects = []
    downloads = []

    def download(conn, messages, overwrite_now):
        downloads.append(dict(messages))
        raise core.BackupWriteError("disk full")

    with pytest.raises(core.BackupWriteError):
        core.download_folder_with_reconnect(
            server, lambda: connects.append(_Conn()), state, foldername="INBOX", display="INBOX",
            local_mbox=None, local_eml=None, written=set(), download=download,
            rescan=lambda conn: None, should_stop=None, log=log)
    assert connects == []
    assert len(downloads) == 1


# ---------------------------------------------------------------- CLI 全流程

class _FlakyServer(MainServer):
    """LIST 或 SEARCH 第一次 abort，之后恢复正常；用来驱动 CLI 的两次重连。"""

    def __init__(self, *args, fail_list=0, fail_search=0, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_list = fail_list
        self.fail_search = fail_search

    def list(self, directory='""', pattern="*"):
        if self.fail_list:
            self.fail_list -= 1
            raise imaplib.IMAP4.abort("socket error: EOF")
        return super().list(directory, pattern)

    def uid(self, command, *args):
        if command == "SEARCH" and self.fail_search:
            self.fail_search -= 1
            raise imaplib.IMAP4.abort("socket error: EOF")
        return super().uid(command, *args)


def test_cli_reruns_list_and_scan_after_reconnect(tmp_path, monkeypatch, clean_logging):
    """CLI：LIST 与扫描各自断线一次，重连后重跑，最终正常备份且退出码不变。"""
    servers = [_FlakyServer(messages=1, fail_list=1),
               _FlakyServer(messages=1, fail_search=1),
               _FlakyServer(messages=1)]
    fresh = iter(servers)

    def connect(_cfg, _log):
        return next(fresh)

    monkeypatch.setattr(core, "connect_and_login", connect)
    monkeypatch.setattr(core, "LOG_DIR", tmp_path / "log")
    out = io.StringIO()
    argv = ["-s", "imap.example.com", "-e", "-u", "x@y.com", "-p", "secret",
            "-d", str(tmp_path / "mbox"), "--folders", "INBOX", "--nospinner"]
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = core.main(argv)
    assert code == 0
    assert [len(server.body_fetches) for server in servers] == [0, 0, 1]
    assert servers[2].body_fetches == [[1]]
    box = core.mailbox.mbox(tmp_path / "mbox" / "x@y.com" / "INBOX.mbox")
    try:
        assert [m.get("Message-Id") for m in box] == ["<msg1@x>"]
    finally:
        box.close()
