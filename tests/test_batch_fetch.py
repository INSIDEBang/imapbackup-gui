"""UID 批量正文取信与失败矩阵（规格 §12.2 B、C 组）。

正文按 UID 升序切批（≤8 封、≤8 MiB，超限/未知大小独占一批），每批解析完才写盘；
失败按 missing / no_bad / malformed 三桶计数，绝不冲抵已备份；批次 NO/BAD 整批重试
一次后逐封隔离，只有隔离结果全是 NO/BAD 才升级为文件夹失败。假服务器，不需要凭证。
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


def run(tmp_path, server, messages, progress=None, reports=None, log=None, report=None, **kwargs):
    progress = progress if progress is not None else core.DownloadProgress()
    reports = reports if reports is not None else []
    batches: list[core.BatchStats] = []
    core.download_messages(server, "INBOX.mbox", messages, False, True, False,
                           tmp_path, False, False, log or NullLog(), foldername="INBOX",
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


# --- B 组：切批 -----------------------------------------------------------------

def test_eight_messages_fit_one_batch_and_the_ninth_starts_another(tmp_path, monkeypatch):
    monkeypatch.setattr(core.time, "sleep", lambda _s: pytest.fail("batch loop must not sleep"))
    uids = list(range(1, 18))
    server = make_server(uids)
    messages = dict(reversed(list(make_messages(uids).items())))   # 插入顺序故意逆序

    progress, reports, batches = run(tmp_path, server, messages)

    assert server.body_fetches == [list(range(1, 9)), list(range(9, 17)), [17]], server.body_fetches
    assert [stats.msgs for stats in batches] == [8, 8, 1], batches
    assert progress.batches == 3 and progress.backed == 17
    # report.index 是消息在传入 messages 列表中的位置，与取信顺序无关
    assert [rec["index"] for rec in reports] == list(range(17, 0, -1)), [r["index"] for r in reports]
    assert server.max_in_flight == 1, server.max_in_flight


def test_eight_mebibyte_boundary_splits_before_exceeding_the_cap(tmp_path):
    server_a = make_server([1, 2, 3], sizes={1: 4 * MIB, 2: 4 * MIB, 3: 1})
    run(tmp_path / "a", server_a, make_messages([1, 2, 3], {1: 4 * MIB, 2: 4 * MIB, 3: 1}))
    assert server_a.body_fetches == [[1, 2], [3]], server_a.body_fetches

    sizes = {1: 2 * MIB, 2: 2 * MIB, 3: 2 * MIB, 4: 2 * MIB, 5: 1}
    server_b = make_server([1, 2, 3, 4, 5], sizes=sizes)
    run(tmp_path / "b", server_b, make_messages([1, 2, 3, 4, 5], sizes))
    assert server_b.body_fetches == [[1, 2, 3, 4], [5]], server_b.body_fetches


def test_oversized_and_unknown_sizes_get_their_own_batch(tmp_path):
    sizes = {1: 100, 2: 8 * MIB + 1, 3: 100, 4: 0, 5: 100}
    server = make_server([1, 2, 3, 4, 5], sizes=sizes)
    run(tmp_path, server, make_messages([1, 2, 3, 4, 5], sizes))

    assert server.body_fetches == [[1], [2], [3], [4], [5]], server.body_fetches


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
    sizes = {uid: uid * 100 for uid in range(1, 10)}
    server = make_server(range(1, 10), sizes=sizes)
    progress, _, batches = run(tmp_path, server, make_messages(range(1, 10), sizes))

    assert [stats.msgs for stats in batches] == [8, 1], batches
    assert [stats.bytes for stats in batches] == [3600, 900], batches
    assert [stats.backed for stats in batches] == [8, 1]
    assert [stats.fetch_failed for stats in batches] == [0, 0]
    assert all(stats.fetch_ms >= 0 for stats in batches)
    assert progress.batches == 2 and progress.last_batch is batches[-1]
    assert progress.done_bytes == 4500, progress.done_bytes


def test_fetch_ms_measures_fetch_only_not_parse_or_write(tmp_path):
    server = make_server([1])
    batches: list[core.BatchStats] = []

    def slow_report(_record):
        time.sleep(0.2)

    core.download_messages(server, "INBOX.mbox", make_messages([1]), False, True, False,
                           tmp_path, False, False, NullLog(), foldername="INBOX",
                           report=slow_report, on_batch=batches.append)

    assert batches[0].fetch_ms < 100, batches[0]


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
    uids = list(range(1, 11))
    server = make_server(uids)
    reports = []
    progress, reports, _ = run(tmp_path, server, make_messages(uids), reports=reports,
                               should_stop=lambda: len(reports) >= 8)

    assert server.body_fetches == [list(range(1, 9))], server.body_fetches
    assert progress.stopped and progress.batches == 1 and progress.backed == 8


def test_empty_messages_returns_the_caller_progress_without_requests(tmp_path):
    progress = core.DownloadProgress(backed=5)
    result = core.download_messages(make_server([]), "INBOX.mbox", {}, False, True, False,
                                    tmp_path, False, True, NullLog(), progress=progress)

    assert result is progress and progress.batches == 0
    assert not (tmp_path / "INBOX.mbox").exists()
