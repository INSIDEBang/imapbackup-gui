"""UID 批量正文取信与失败矩阵（规格 §12.2 B、C 组）。

正文按 UID 升序切批（≤64 封、≤32 MiB，超限/未知大小独占一批），每批解析完才写盘；
失败按 missing / no_bad / malformed / timeout 四桶计数，绝不冲抵已备份；批次 NO/BAD 整批重试
一次后逐封隔离，只有隔离结果全是 NO/BAD 才升级为文件夹失败；正文读超时后按对半缩小的
批次重取，只有单独取仍超时的那封才放弃。
假服务器，不需要凭证。
"""
import imaplib
import inspect
import logging
import mailbox
import threading
import time

import pytest

import imapbackup312 as core
from fakes import NullLog, UidFetchServer

MIB = 1024 * 1024


def raw_message(uid: int) -> bytes:
    return (f"From: sender{uid}@example.com\r\nSubject: message {uid}\r\n"
            f"Message-Id: <{uid}@x>\r\n\r\nbody {uid}\r\n").encode()


def make_server(uids, sizes=None, **kwargs) -> UidFetchServer:
    return UidFetchServer({uid: raw_message(uid) for uid in uids}, sizes=sizes, **kwargs)


def make_messages(uids, sizes=None) -> dict[str, tuple[int, int]]:
    sizes = sizes or {}
    return {f"<{uid}@x>": (uid, sizes.get(uid, len(raw_message(uid)))) for uid in uids}


def run(tmp_path, server, messages, progress=None, reports=None, log=None, report=None,
        filename="INBOX.mbox", foldername="INBOX", **kwargs):
    progress = progress if progress is not None else core.DownloadProgress()
    reports = reports if reports is not None else []
    batches: list[core.BatchStats] = []
    core.download_messages(server, filename, messages, False, True, False,
                           tmp_path, False, False, log or NullLog(), foldername=foldername,
                           report=report or reports.append, progress=progress,
                           on_batch=batches.append, **kwargs)
    return progress, reports, batches


def mbox_ids(path):
    mbox_path = path / "INBOX.mbox"
    if not mbox_path.exists():
        return []
    mb = mailbox.mbox(mbox_path)
    try:
        return [msg.get("Message-Id") for msg in mb]
    finally:
        mb.close()


def timeout_on(uids):
    """正文请求只要含这些 UID 就抛 TimeoutError，模拟 socket 读超时。"""
    wanted = set(uids)

    def hook(_server, batch):
        if wanted & set(batch):
            return TimeoutError("The read operation timed out")
        return None

    return hook


def timeout_once():
    """只在第一次正文请求抛 TimeoutError，模拟一次读超时之后链路恢复正常。"""
    calls: list[list[int]] = []

    def hook(_server, batch):
        calls.append(list(batch))
        return TimeoutError("The read operation timed out") if len(calls) == 1 else None

    return hook


def run_until_stable(tmp_path, server, messages, stall, progress=None, log=None):
    """按 download_folder_with_reconnect 的语义重跑：每轮只传剩余消息，直到不再抛 BatchStalled。"""
    progress = progress if progress is not None else core.DownloadProgress()
    log = log if log is not None else NullLog()
    written: set[str] = set()
    for _ in range(6):
        remaining = {msg_id: item for msg_id, item in messages.items() if msg_id not in written}
        try:
            run(tmp_path, server, remaining, progress=progress, log=log,
                written=written, stall=stall)
            return progress, written
        except core.BatchStalled:
            continue
    pytest.fail("BatchStalled kept repeating; the stall bookkeeping never converged")


# --- B 组：切批 -----------------------------------------------------------------

def test_sixty_four_messages_fit_one_batch_and_the_65th_starts_another(tmp_path, monkeypatch):
    monkeypatch.setattr(core.time, "sleep", lambda _s: pytest.fail("batch loop must not sleep"))
    uids = list(range(1, 131))
    server = make_server(uids)
    messages = dict(reversed(list(make_messages(uids).items())))   # 插入顺序故意逆序

    progress, reports, batches = run(tmp_path, server, messages)

    assert server.body_fetches == [list(range(1, 65)), list(range(65, 129)), [129, 130]], server.body_fetches
    assert [stats.msgs for stats in batches] == [64, 64, 2], batches
    assert progress.batches == 3 and progress.backed == 130
    # report.index 是消息在传入 messages 列表中的位置，与取信顺序无关
    assert [rec["index"] for rec in reports] == list(range(130, 0, -1)), [r["index"] for r in reports]
    assert server.max_in_flight == 1, server.max_in_flight


def test_thirty_two_mebibyte_boundary_splits_before_exceeding_the_cap(tmp_path):
    sizes_a = {1: 16 * MIB, 2: 16 * MIB, 3: 1}
    server_a = make_server([1, 2, 3], sizes=sizes_a)
    run(tmp_path / "a", server_a, make_messages([1, 2, 3], sizes_a))
    assert server_a.body_fetches == [[1, 2], [3]], server_a.body_fetches

    sizes_b = {1: 8 * MIB, 2: 8 * MIB, 3: 8 * MIB, 4: 8 * MIB, 5: 1}
    server_b = make_server([1, 2, 3, 4, 5], sizes=sizes_b)
    run(tmp_path / "b", server_b, make_messages([1, 2, 3, 4, 5], sizes_b))
    assert server_b.body_fetches == [[1, 2, 3, 4], [5]], server_b.body_fetches


def test_oversized_and_unknown_sizes_get_their_own_batch(tmp_path):
    sizes = {1: 100, 2: 32 * MIB + 1, 3: 100, 4: 0, 5: 100}
    server = make_server([1, 2, 3, 4, 5], sizes=sizes)
    run(tmp_path, server, make_messages([1, 2, 3, 4, 5], sizes))

    assert server.body_fetches == [[1], [2], [3], [4], [5]], server.body_fetches


def test_a_low_ceiling_shortens_the_batch_it_touches(tmp_path):
    """上限表怎么影响切批：上限 1 独占一批，一批的封数不会超过批内最小的那个上限。"""
    stall = core.BatchStall(caps={"<2@x>": 2, "<4@x>": 1})
    server = make_server([1, 2, 3, 4, 5, 6])
    run(tmp_path, server, make_messages([1, 2, 3, 4, 5, 6]), stall=stall)

    assert server.body_fetches == [[1, 2], [3], [4], [5, 6]], server.body_fetches


def test_a_bigger_payload_cuts_the_request_count_of_the_same_pending_set(tmp_path, monkeypatch):
    """对照只看请求次数与 UID 分组，不断言墙钟：纯小邮件正好降到旧策略的 1/8，混合大小更低。"""
    def old_policy_requests(dirname, uids, sizes) -> int:
        """把两个上限改回 8 封 / 8 MiB 跑一遍，得到旧策略对同一待备份集付的请求次数。"""
        monkeypatch.setattr(core, "FETCH_BATCH_MAX_MESSAGES", 8)
        monkeypatch.setattr(core, "FETCH_BATCH_MAX_BYTES", 8 * MIB)
        server = make_server(uids, sizes=sizes)
        run(tmp_path / dirname, server, make_messages(uids, sizes))
        monkeypatch.undo()
        return len(server.body_fetches)

    small = list(range(1, 257))
    small_sizes = dict.fromkeys(small, 1024)
    old_small = old_policy_requests("old-small", small, small_sizes)
    new_small = make_server(small, sizes=small_sizes)
    run(tmp_path / "new-small", new_small, make_messages(small, small_sizes))

    assert [len(batch) for batch in new_small.body_fetches] == [64] * 4, new_small.body_fetches
    assert (old_small, len(new_small.body_fetches)) == (32, 4), old_small

    mixed = list(range(1, 65))
    mixed_sizes = dict.fromkeys(mixed, 1024)
    mixed_sizes[64] = 9 * MIB      # 旧策略下它是「超大」，独占一批；32 MiB 下它和邻居同批
    old_mixed = old_policy_requests("old-mixed", mixed, mixed_sizes)
    new_mixed = make_server(mixed, sizes=mixed_sizes)
    run(tmp_path / "new-mixed", new_mixed, make_messages(mixed, mixed_sizes))

    assert (old_mixed, new_mixed.body_fetches) == (9, [mixed]), (old_mixed, new_mixed.body_fetches)


def test_body_requests_carry_uid_and_body_peek_only(tmp_path):
    server = make_server([1, 2])
    run(tmp_path, server, make_messages([1, 2]))

    assert server.fetches == [("1,2", core.BODY_FETCH_ITEMS)], server.fetches
    assert core.BODY_FETCH_ITEMS == "(UID INTERNALDATE BODY.PEEK[])"
    source = inspect.getsource(core)
    assert "1:*" not in source and "partial(" not in source
    assert "(INTERNALDATE RFC822)" not in source


def test_response_order_and_duplicate_uid_keep_one_write_per_message(tmp_path):
    server = make_server([1, 2, 3], reverse_responses=True, duplicate_uids={2})
    progress, reports, _ = run(tmp_path, server, make_messages([1, 2, 3]))

    assert progress.backed == 3 and len(reports) == 3
    assert [rec["msg_id"] for rec in reports] == ["<1@x>", "<2@x>", "<3@x>"]
    assert mbox_ids(tmp_path) == ["<1@x>", "<2@x>", "<3@x>"]


def test_duplicate_uid_response_warns_and_keeps_the_first(tmp_path, caplog):
    server = make_server([1, 2], duplicate_uids={2})
    log = logging.getLogger("test.batch.duplicate")
    caplog.set_level(logging.WARNING, logger=log.name)
    progress, reports, _ = run(tmp_path, server, make_messages([1, 2]), log=log)

    assert progress.backed == 2 and len(reports) == 2
    assert any("duplicate UID" in rec.message and "2" in rec.message for rec in caplog.records), caplog.text


def test_batch_stats_cover_messages_bytes_and_failures(tmp_path):
    sizes = {uid: uid * 100 for uid in range(1, 66)}
    server = make_server(range(1, 66), sizes=sizes)
    progress, _, batches = run(tmp_path, server, make_messages(range(1, 66), sizes))

    assert [stats.msgs for stats in batches] == [64, 1], batches
    assert [stats.bytes for stats in batches] == [208000, 6500], batches
    assert [stats.backed for stats in batches] == [64, 1]
    assert [stats.fetch_failed for stats in batches] == [0, 0]
    assert all(stats.fetch_ms >= 0 for stats in batches)
    assert progress.batches == 2 and progress.last_batch is batches[-1]
    assert progress.done_bytes == 214500, progress.done_bytes


def test_fetch_ms_measures_fetch_only_not_parse_or_write(tmp_path):
    server = make_server([1])
    batches: list[core.BatchStats] = []

    def slow_report(_record):
        time.sleep(0.2)

    core.download_messages(server, "INBOX.mbox", make_messages([1]), False, True, False,
                           tmp_path, False, False, NullLog(), foldername="INBOX",
                           report=slow_report, on_batch=batches.append)

    assert batches[0].fetch_ms < 100, batches[0]


def test_the_inflight_batch_is_published_before_the_request_and_cleared_after_it(tmp_path):
    """请求还在飞的这段时间里也要有读数可读：发出前发布封数与声明字节，请求一终止就清空。"""
    sizes = {uid: uid * 100 for uid in range(1, 66)}      # 64 封 + 1 封两批
    seen: list[tuple[list[int], object]] = []
    progress = core.DownloadProgress()

    def watch(_server, uids):
        seen.append((list(uids), progress.inflight))

    server = make_server(range(1, 66), sizes=sizes, body_hook=watch)
    run(tmp_path, server, make_messages(range(1, 66), sizes), progress=progress)

    assert [uids for uids, _ in seen] == [list(range(1, 65)), [65]], seen
    first, second = (batch for _, batch in seen)
    assert (first.msgs, first.bytes) == (64, 208000), first    # 声明字节，不是实际取回的字节
    assert (second.msgs, second.bytes) == (1, 6500), second
    assert first.started_at <= second.started_at <= core.time.monotonic()
    assert progress.inflight is None


def test_progress_keeps_the_longest_batch_of_the_run(tmp_path):
    """慢批要跨采样窗留住：窗口只看最近完成的批次，最长的一批只能靠本账户累计。"""
    def slow_the_multi_message_batch(_server, uids):
        if len(uids) > 1:
            time.sleep(0.2)

    server = make_server(range(1, 66), body_hook=slow_the_multi_message_batch)
    progress, _, batches = run(tmp_path, server, make_messages(range(1, 66)))

    assert [stats.msgs for stats in batches] == [64, 1], batches
    # 0.2 秒只是「明显比正常批慢」：本机测过 time.sleep(0.2) 只走出 188 ms 的 fetch_ms（Windows 粗时钟）
    assert batches[0].fetch_ms >= 100 and batches[1].fetch_ms < batches[0].fetch_ms
    assert progress.max_fetch_ms == pytest.approx(batches[0].fetch_ms)


def test_a_timed_out_batch_leaves_no_inflight_and_is_still_the_longest(tmp_path):
    """超时结束的那批不留残值，也不该被忘掉：它没写完所以不进 batches，但耗时仍进最长单批。"""
    def time_out_slowly(_server, batch):
        time.sleep(0.2)
        return TimeoutError("The read operation timed out")

    server = make_server([1, 2], raise_body_hook=time_out_slowly)
    progress = core.DownloadProgress()

    with pytest.raises(core.BatchStalled):
        run(tmp_path, server, make_messages([1, 2]), progress=progress, stall=core.BatchStall())

    assert progress.inflight is None
    # 本机 time.sleep(0.2) 在 Windows 粗时钟下只走出约 188 ms：只要求明显比正常批慢
    assert progress.batches == 0 and progress.max_fetch_ms >= 100, progress.max_fetch_ms


# --- C 组：失败矩阵 -------------------------------------------------------------

def test_missing_uid_is_retried_alone_then_counted_missing(tmp_path):
    server = make_server([1, 2], missing_uids={2})
    progress, reports, _ = run(tmp_path, server, make_messages([1, 2]))

    assert progress.backed == 1 and progress.fetch_failed == 1
    assert progress.fetch_failed_missing == 1
    assert progress.fetch_failed_no_bad == 0 and progress.fetch_failed_malformed == 0
    assert server.body_fetches == [[1, 2], [2]], server.body_fetches
    assert [rec["index"] for rec in reports] == [1] and reports[0]["total"] == 2
    assert mbox_ids(tmp_path) == ["<1@x>"]


def test_no_bad_on_the_single_retry_wins_the_bucket(tmp_path):
    server = make_server([1, 2], missing_uids={2}, no_bad_single_uids={2})
    progress, _, _ = run(tmp_path, server, make_messages([1, 2]))

    assert server.body_fetches == [[1, 2], [2]], server.body_fetches
    assert progress.backed == 1 and progress.fetch_failed == 1
    assert progress.fetch_failed_no_bad == 1
    assert progress.fetch_failed_missing == 0 and progress.fetch_failed_malformed == 0


def test_single_uid_no_bad_is_counted_and_the_folder_continues(tmp_path):
    server = make_server([1, 2], no_bad_uids={2})
    progress, reports, _ = run(tmp_path, server, make_messages([1, 2]))

    assert progress.backed == 1 and progress.fetch_failed == 1
    assert progress.fetch_failed_no_bad == 1
    assert server.body_fetches == [[1, 2], [1, 2], [1], [2]], server.body_fetches
    assert mbox_ids(tmp_path) == ["<1@x>"]
    assert [rec["msg_id"] for rec in reports] == ["<1@x>"]


def test_malformed_response_is_retried_alone_then_counted_malformed(tmp_path):
    server = make_server([1, 2], malformed_uids={2})
    progress, reports, _ = run(tmp_path, server, make_messages([1, 2]))

    assert progress.backed == 1 and progress.fetch_failed == 1
    assert progress.fetch_failed_malformed == 1
    assert server.body_fetches == [[1, 2], [2]], server.body_fetches
    assert [rec["msg_id"] for rec in reports] == ["<1@x>"]


def test_response_without_a_uid_is_malformed_not_missing(tmp_path):
    server = make_server([1, 2], unattributed_uids={2})
    progress, reports, _ = run(tmp_path, server, make_messages([1, 2]))

    assert progress.backed == 1 and progress.fetch_failed == 1
    assert progress.fetch_failed_malformed == 1 and progress.fetch_failed_missing == 0
    assert server.body_fetches == [[1, 2], [2]], server.body_fetches
    assert [rec["msg_id"] for rec in reports] == ["<1@x>"]


def test_rejected_batch_is_retried_once_before_isolation(tmp_path):
    server = make_server([1, 2], no_bad_batch_times=1)
    progress, _, _ = run(tmp_path, server, make_messages([1, 2]))

    assert server.body_fetches == [[1, 2], [1, 2]], server.body_fetches
    assert progress.backed == 2 and progress.fetch_failed == 0


def test_rejected_batch_after_retry_is_isolated_per_uid(tmp_path):
    server = make_server([1, 2], no_bad_batch_times=2)
    progress, _, _ = run(tmp_path, server, make_messages([1, 2]))

    assert server.body_fetches == [[1, 2], [1, 2], [1], [2]], server.body_fetches
    assert progress.backed == 2 and progress.fetch_failed == 0


def test_every_isolated_uid_no_bad_is_a_folder_failure(tmp_path):
    server = make_server([1, 2], no_bad_uids={1, 2})
    progress = core.DownloadProgress()

    with pytest.raises(core.FolderFetchError):
        run(tmp_path, server, make_messages([1, 2]), progress=progress)

    assert server.body_fetches == [[1, 2], [1, 2], [1], [2]], server.body_fetches
    assert progress.backed == 0 and progress.fetch_failed == 0
    assert (tmp_path / "INBOX.mbox").read_bytes() == b""


def test_all_missing_uids_are_not_a_folder_failure(tmp_path):
    server = make_server([1, 2], missing_uids={1, 2})
    progress, reports, _ = run(tmp_path, server, make_messages([1, 2]))

    assert progress.backed == 0 and progress.fetch_failed == 2
    assert progress.fetch_failed_missing == 2
    assert reports == [] and mbox_ids(tmp_path) == []
    assert server.body_fetches == [[1, 2], [1], [2]], server.body_fetches


def test_mixed_isolation_buckets_do_not_escalate_to_a_folder_failure(tmp_path):
    server = make_server([1, 2, 3], no_bad_uids={2}, missing_uids={3})
    progress, reports, _ = run(tmp_path, server, make_messages([1, 2, 3]))

    assert progress.backed == 1 and progress.fetch_failed == 2
    assert progress.fetch_failed_no_bad == 1 and progress.fetch_failed_missing == 1
    assert [rec["msg_id"] for rec in reports] == ["<1@x>"]
    assert server.body_fetches == [[1, 2, 3], [1, 2, 3], [1], [2], [3]], server.body_fetches


def test_a_folder_warning_reports_only_the_failures_that_call_added(tmp_path, caplog):
    log = logging.getLogger("test.batch.failure_delta")
    caplog.set_level(logging.WARNING, logger=log.name)
    progress = core.DownloadProgress()          # GUI shares one per account
    server = make_server([1, 2, 3], missing_uids={2, 3})

    run(tmp_path / "inbox", server, make_messages([1, 2]), progress=progress, log=log,
        filename="INBOX.mbox", foldername="INBOX")
    run(tmp_path / "sent", server, make_messages([1, 3]), progress=progress, log=log,
        filename="Sent.mbox", foldername="Sent")
    run(tmp_path / "sent-retry", server, make_messages([1, 3]), progress=progress, log=log,
        filename="Sent.mbox", foldername="Sent")

    warnings = [rec.getMessage() for rec in caplog.records if "could not be fetched" in rec.getMessage()]
    assert warnings == [
        "INBOX: 1 message(s) could not be fetched (missing=1, no_bad=0, malformed=0, timeout=0)",
        "Sent: 1 message(s) could not be fetched (missing=1, no_bad=0, malformed=0, timeout=0)",
    ], warnings
    assert progress.fetch_failed == 2


def test_a_retried_failure_that_succeeds_is_removed_from_the_count(tmp_path):
    progress = core.DownloadProgress()
    server = make_server([1, 2], missing_uids={2})
    messages = make_messages([1, 2])

    run(tmp_path / "first", server, messages, progress=progress)
    assert (progress.fetch_failed, progress.fetch_failed_missing) == (1, 1)

    server.missing_uids.clear()
    _, _, batches = run(tmp_path / "retry", server, messages, progress=progress,
                        written={"<1@x>"})

    assert progress.backed == 2
    assert progress.fetch_failed == 0
    assert progress.fetch_failed_missing == 0
    assert batches[-1].fetch_failed == 0


def test_a_retried_failure_moves_to_its_new_bucket_without_double_counting(tmp_path):
    progress = core.DownloadProgress()
    server = make_server([1, 2], missing_uids={2})
    messages = make_messages([1, 2])

    run(tmp_path / "first", server, messages, progress=progress)
    assert (progress.fetch_failed, progress.fetch_failed_missing) == (1, 1)

    server.missing_uids.clear()
    server.no_bad_uids = {2}
    _, _, batches = run(tmp_path / "retry", server, messages, progress=progress,
                        written={"<1@x>"})

    assert progress.fetch_failed == 1
    assert progress.fetch_failed_missing == 0
    assert progress.fetch_failed_no_bad == 1
    assert batches[-1].fetch_failed == 0   # 换桶不算本批新增失败


def test_an_aborted_attempts_failures_are_reported_once_by_the_retry(tmp_path, caplog):
    log = logging.getLogger("test.batch.aborted_warning")
    caplog.set_level(logging.WARNING, logger=log.name)
    progress = core.DownloadProgress()
    aborted = False

    def drop_second_batch(_server, uids):
        nonlocal aborted
        if 65 in uids and not aborted:
            aborted = True
            raise imaplib.IMAP4.abort("connection lost")

    server = make_server(range(1, 66), missing_uids={1}, body_hook=drop_second_batch)
    messages = make_messages(range(1, 66))

    with pytest.raises(imaplib.IMAP4.abort):
        run(tmp_path / "first", server, messages, progress=progress, log=log)
    assert progress.fetch_failed == 1

    written = {f"<{uid}@x>" for uid in range(2, 65)}
    run(tmp_path / "retry", server, messages, progress=progress, log=log, written=written)

    warnings = [rec.getMessage() for rec in caplog.records if "could not be fetched" in rec.getMessage()]
    assert warnings == [
        "INBOX: 1 message(s) could not be fetched (missing=1, no_bad=0, malformed=0, timeout=0)",
    ], warnings
    assert progress.fetch_failed == 1 and progress.backed == 64


def test_folders_sharing_an_output_name_count_their_failures_separately(tmp_path, caplog):
    """A/B 与 A.B 都输出 A.B.mbox，但失败按文件夹身份计，不互相掩盖。"""
    log = logging.getLogger("test.batch.folder_identity")
    caplog.set_level(logging.WARNING, logger=log.name)
    progress = core.DownloadProgress()
    server = make_server([1, 2], missing_uids={2})

    run(tmp_path / "a", server, make_messages([1, 2]), progress=progress, log=log,
        filename="A.B.mbox", foldername="A/B")
    run(tmp_path / "b", server, make_messages([1, 2]), progress=progress, log=log,
        filename="A.B.mbox", foldername="A.B")

    warnings = [rec.getMessage() for rec in caplog.records if "could not be fetched" in rec.getMessage()]
    assert warnings == [
        "A/B: 1 message(s) could not be fetched (missing=1, no_bad=0, malformed=0, timeout=0)",
        "A.B: 1 message(s) could not be fetched (missing=1, no_bad=0, malformed=0, timeout=0)",
    ], warnings
    assert progress.fetch_failed == 2


# --- C 组补充：正文取信超时（socket 读超时）--------------------------------------

def test_a_timed_out_64_message_batch_is_rescued_in_two_smaller_batches(tmp_path):
    """64 封一批超时：对半缩小后两次请求收齐全批，没有一封写两遍（ADR 0016）。"""
    uids = list(range(1, 65))
    server = make_server(uids, raise_body_hook=timeout_once())
    stall = core.BatchStall()
    progress, written = run_until_stable(tmp_path, server, make_messages(uids), stall)

    assert server.body_fetches == [uids, list(range(1, 33)), list(range(33, 65))], server.body_fetches
    assert stall.caps == {f"<{uid}@x>": 32 for uid in uids}, stall.caps
    assert progress.backed == 64 and progress.fetch_failed == 0
    assert written == {f"<{uid}@x>" for uid in uids}
    assert len(mbox_ids(tmp_path)) == 64                 # 救回的一批没有一封写两遍


def test_a_batch_that_keeps_timing_out_halves_down_to_one_message(tmp_path):
    """一批里有一封读不动：8→4→2→1 逐级对半，只有单独取仍超时的那封才放弃。

    救回的同轮请求仍是缩过的小批——上限只降不升，成功也不会把批次合回 8 封。
    """
    uids = list(range(1, 9))
    server = make_server(uids, raise_body_hook=timeout_on([1]))
    stall = core.BatchStall()
    progress, written = run_until_stable(tmp_path, server, make_messages(uids), stall)

    assert server.body_fetches == [uids, [1, 2, 3, 4], [1, 2], [1], [2], [3, 4],
                                   [5, 6, 7, 8]], server.body_fetches
    assert stall.skipped == {"<1@x>"} and stall.caps == {
        "<2@x>": 1, "<3@x>": 2, "<4@x>": 2, "<5@x>": 4, "<6@x>": 4, "<7@x>": 4, "<8@x>": 4}
    assert written == {f"<{uid}@x>" for uid in range(2, 9)}
    assert progress.backed == 7 and (progress.fetch_failed, progress.fetch_failed_timeout) == (1, 1)
    assert len(mbox_ids(tmp_path)) == 7


def test_a_byte_limited_batch_still_halves_after_a_timeout(tmp_path):
    """受字节上限约束的一批（远不到 64 封）同样缩得下来：否则下一轮原样重发同一个请求。"""
    uids = list(range(1, 6))
    sizes = dict.fromkeys(uids, 8 * MIB)
    server = make_server(uids, sizes=sizes, raise_body_hook=timeout_once())
    progress, written = run_until_stable(tmp_path, server, make_messages(uids, sizes),
                                         core.BatchStall())

    assert server.body_fetches == [[1, 2, 3, 4], [1, 2], [3, 4], [5]], server.body_fetches
    assert progress.backed == 5 and written == {f"<{uid}@x>" for uid in uids}


def test_a_new_run_starts_the_ceiling_table_empty(tmp_path):
    """上限表只活一轮：新 tracker 下同一批消息重新一次请求拿回，不带着上一轮的缩批。"""
    uids = list(range(1, 9))
    server = make_server(uids, raise_body_hook=timeout_once())
    run_until_stable(tmp_path / "first", server, make_messages(uids), core.BatchStall())
    assert server.body_fetches == [uids, list(range(1, 5)), list(range(5, 9))], server.body_fetches

    server.raise_body_hook = None
    server.body_fetches.clear()
    progress, _written = run_until_stable(tmp_path / "second", server, make_messages(uids),
                                          core.BatchStall())
    assert server.body_fetches == [uids], server.body_fetches
    assert progress.backed == 8


def test_a_timed_out_batch_lowers_the_ceiling_of_every_message_in_it(tmp_path):
    """整批超时：不写半批，批内每封的批次上限对半——两封的一批缩到 1，就是各自独占一批。"""
    server = make_server([1, 2], raise_body_hook=timeout_on([2]))
    stall = core.BatchStall()
    progress = core.DownloadProgress()

    with pytest.raises(core.BatchStalled):
        run(tmp_path, server, make_messages([1, 2]), progress=progress, stall=stall)

    assert server.body_fetches == [[1, 2]], server.body_fetches
    assert stall.caps == {"<1@x>": 1, "<2@x>": 1} and stall.skipped == set()
    assert progress.backed == 0 and progress.fetch_failed == 0
    assert mbox_ids(tmp_path) == []


def test_a_lone_message_that_stalls_is_abandoned_and_counted_as_timeout(tmp_path, caplog):
    """拆开后仍超时的那封记 timeout 跳过；其余照常写完，文件夹不判失败。"""
    log = logging.getLogger("test.batch.stall_give_up")
    caplog.set_level(logging.WARNING, logger=log.name)
    messages = make_messages([1, 2])
    server = make_server([1, 2], raise_body_hook=timeout_on([2]))
    stall = core.BatchStall()

    progress, written = run_until_stable(tmp_path, server, messages, stall, log=log)

    assert server.body_fetches == [[1, 2], [1], [2]], server.body_fetches
    assert written == {"<1@x>"} and mbox_ids(tmp_path) == ["<1@x>"]
    assert (progress.backed, progress.fetch_failed) == (1, 1)
    assert progress.fetch_failed_timeout == 1
    assert progress.failures == {("INBOX", "<2@x>"): "timeout"}
    assert stall.skipped == {"<2@x>"}
    warnings = [rec.getMessage() for rec in caplog.records if "could not be fetched" in rec.getMessage()]
    assert warnings == [
        "INBOX: 1 message(s) could not be fetched (missing=0, no_bad=0, malformed=0, timeout=1)",
    ], warnings


def test_a_single_message_batch_that_stalls_gets_one_more_attempt(tmp_path):
    """独占一批的大邮件先单独重试一次，不把一次超时直接当放弃。"""
    sizes = {1: 32 * MIB + 1}
    server = make_server([1], sizes=sizes, raise_body_hook=timeout_once())
    stall = core.BatchStall()
    progress, written = run_until_stable(tmp_path, server, make_messages([1], sizes), stall)

    assert server.body_fetches == [[1], [1]], server.body_fetches
    assert written == {"<1@x>"}
    assert (progress.backed, progress.fetch_failed) == (1, 0)
    assert stall.caps == {"<1@x>": 1} and stall.skipped == set()


def test_a_stall_without_a_tracker_propagates_the_timeout(tmp_path):
    """不传 BatchStall（旧调用方）时保持原行为：TimeoutError 原样上抛。"""
    server = make_server([1, 2], raise_body_hook=timeout_on([2]))

    with pytest.raises(TimeoutError):
        run(tmp_path, server, make_messages([1, 2]))

    assert server.body_fetches == [[1, 2]], server.body_fetches


def test_an_abandoned_message_that_succeeds_in_a_later_run_clears_the_timeout_bucket(tmp_path):
    progress = core.DownloadProgress()
    messages = make_messages([1, 2])
    server = make_server([1, 2], raise_body_hook=timeout_on([2]))

    run_until_stable(tmp_path / "first", server, messages, core.BatchStall(), progress)
    assert (progress.fetch_failed, progress.fetch_failed_timeout) == (1, 1)

    server.raise_body_hook = None
    pending = {"<2@x>": messages["<2@x>"]}      # 下次运行按本地快照只算缺的那封
    _, written = run_until_stable(tmp_path / "retry", server, pending, core.BatchStall(), progress)

    assert written == {"<2@x>"}
    assert progress.backed == 2
    assert (progress.fetch_failed, progress.fetch_failed_timeout) == (0, 0)


@pytest.mark.parametrize("exc_type", [imaplib.IMAP4.abort, OSError])
def test_connection_error_propagates_without_writing_half_a_batch(tmp_path, exc_type):
    server = make_server([1, 2], raise_body=exc_type("connection lost"))
    progress = core.DownloadProgress()

    with pytest.raises(exc_type):
        run(tmp_path, server, make_messages([1, 2]), progress=progress)

    assert server.body_fetches == [[1, 2]], server.body_fetches
    assert progress.backed == 0
    assert (tmp_path / "INBOX.mbox").read_bytes() == b""


def test_write_error_is_a_backup_write_error_not_an_oserror(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    server = make_server([1])

    with pytest.raises(core.BackupWriteError) as info:
        core.download_messages(server, "INBOX.mbox", make_messages([1]), False, True, False,
                               blocker, False, True, NullLog(), foldername="INBOX")
    assert not isinstance(info.value, OSError)


def test_report_fires_once_per_written_message_and_failures_leave_gaps(tmp_path):
    server = make_server([1, 2, 3], missing_uids={2})
    progress, reports, _ = run(tmp_path, server, make_messages([1, 2, 3]), verbose=1)

    assert [rec["index"] for rec in reports] == [1, 3], reports
    assert all(rec["total"] == 3 and rec["msg_id"] != "<2@x>" for rec in reports)
    assert all(rec["size"] > 0 for rec in reports)
    assert progress.backed == 2 and progress.fetch_failed == 1


# --- 停止 -----------------------------------------------------------------------

def test_stop_before_the_first_batch_sends_no_request(tmp_path):
    server = make_server([1, 2])
    progress, reports, _ = run(tmp_path, server, make_messages([1, 2]), should_stop=lambda: True)

    assert progress.stopped and progress.fetch_failed == 0 and progress.backed == 0
    assert server.body_fetches == [] and reports == []
    assert (tmp_path / "INBOX.mbox").read_bytes() == b""


def test_stop_during_an_in_flight_batch_discards_it(tmp_path):
    stop = threading.Event()
    server = make_server([1, 2], body_hook=lambda _srv, _uids: stop.set())
    progress, reports, _ = run(tmp_path, server, make_messages([1, 2]), should_stop=stop.is_set)

    assert server.body_fetches == [[1, 2]], server.body_fetches
    assert progress.stopped and progress.backed == 0 and progress.fetch_failed == 0
    assert reports == [] and (tmp_path / "INBOX.mbox").read_bytes() == b""


def test_stop_after_a_message_write_keeps_that_message(tmp_path):
    server = make_server([1, 2, 3])
    reports = []
    progress, reports, _ = run(tmp_path, server, make_messages([1, 2, 3]), reports=reports,
                               should_stop=lambda: len(reports) >= 1)

    assert server.body_fetches == [[1, 2, 3]], server.body_fetches
    assert progress.stopped and progress.backed == 1 and len(reports) == 1
    assert mbox_ids(tmp_path) == ["<1@x>"]


def test_stop_between_batches_sends_no_more_requests(tmp_path):
    uids = list(range(1, 66))
    server = make_server(uids)
    reports = []
    progress, reports, _ = run(tmp_path, server, make_messages(uids), reports=reports,
                               should_stop=lambda: len(reports) >= 64)

    assert server.body_fetches == [list(range(1, 65))], server.body_fetches
    assert progress.stopped and progress.batches == 1 and progress.backed == 64


def test_empty_messages_returns_the_caller_progress_without_requests(tmp_path):
    progress = core.DownloadProgress(backed=5)
    result = core.download_messages(make_server([]), "INBOX.mbox", {}, False, True, False,
                                    tmp_path, False, True, NullLog(), progress=progress)

    assert result is progress and progress.batches == 0
    assert not (tmp_path / "INBOX.mbox").exists()
