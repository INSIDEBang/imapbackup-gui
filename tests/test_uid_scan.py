"""UID 扫描（规格 §12.2 A 组）。

一条 `UID SEARCH ALL` 取全量 UID，头部每 500 UID 一页；消息标识只从响应元数据的
`UID` 取，不依赖 IMAP 序号；缺 Message-Id 的消息走既有的合成标识回退；任何一页
或一条无法可靠解析都让整个文件夹跳过，不返回半截快照。
"""
import hashlib
import logging

import pytest

import imapbackup312 as core
from fakes import NullLog, UidFetchServer, fallback_literal

INBOX = "INBOX"


def raw_message(uid: int, msg_id: str | None = None) -> bytes:
    msg_id = msg_id if msg_id is not None else f"<{uid}@x>"
    return (f"From: sender{uid}@example.com\r\nSubject: message {uid}\r\n"
            f"Message-Id: {msg_id}\r\n\r\nbody {uid}\r\n").encode()


def raws(*uids: int) -> dict[int, bytes]:
    return {uid: raw_message(uid) for uid in uids}


def raw_without_msgid(uid: int) -> bytes:
    return (f"From: sender{uid}@example.com\r\nSubject: message {uid}\r\n"
            f"\r\nbody {uid}\r\n").encode()


def scan(server: UidFetchServer, log: logging.Logger | None = None) -> core.FolderScan:
    return core.scan_folder(server, INBOX, True, True, log or NullLog(), display=INBOX)


def test_scan_sends_one_uid_search_all_and_pages_headers_by_500():
    server = UidFetchServer(raws(*range(1, 502)))
    result = scan(server)

    searches = [call for call in server.uid_calls if call[0] == "SEARCH"]
    assert searches == [("SEARCH", ["ALL"])], searches
    assert [len(page) for page in server.header_fetches] == [500, 1], server.header_fetches
    assert server.fallback_fetches == [] and server.body_fetches == []
    assert len(result.messages) == 501
    assert result.uidvalidity == 777
    for items in [items for _, items in server.fetches]:
        assert items == core.SCAN_MSGID_FETCH_ITEMS, items


def test_scan_uses_uid_from_metadata_even_when_responses_are_reversed():
    server = UidFetchServer(
        {7: raw_message(7), 9: raw_message(9), 42: raw_message(42)},
        sizes={7: 123, 9: 456, 42: 0}, reverse_responses=True)
    result = scan(server)

    assert result.messages == {
        "<7@x>": (7, 123),
        "<9@x>": (9, 456),
        "<42@x>": (42, 0),
    }, result.messages


def test_scan_missing_message_id_uses_synthetic_id_from_the_fallback_page():
    server = UidFetchServer({1: raw_message(1), 2: raw_without_msgid(2)})
    result = scan(server)

    synthetic = "<" + core.UUID + "." + hashlib.sha1(
        fallback_literal(2).replace(b"\r\n", b"\t")).hexdigest() + ">"
    assert result.messages == {
        "<1@x>": (1, len(raw_message(1))),
        synthetic: (2, len(raw_without_msgid(2))),
    }, result.messages
    assert server.fallback_fetches == [[2]], server.fallback_fetches


def test_scan_duplicate_message_id_keeps_the_later_uid_and_warns(caplog):
    log = logging.getLogger("test.uid_scan.duplicate")
    caplog.set_level(logging.WARNING, logger=log.name)
    server = UidFetchServer({1: raw_message(1, "<dup@x>"), 2: raw_message(2, "<dup@x>")})
    result = scan(server, log)

    assert result.messages == {"<dup@x>": (2, len(raw_message(2, "<dup@x>")))}, result.messages
    assert any("duplicate Message-Id <dup@x>" in rec.message for rec in caplog.records), caplog.text


def test_scan_returns_uidvalidity_and_degrades_to_none(caplog):
    assert scan(UidFetchServer(raws(1))).uidvalidity == 777

    missing = UidFetchServer(raws(1), uidvalidity=None)
    log = logging.getLogger("test.uid_scan.uidvalidity")
    caplog.set_level(logging.WARNING, logger=log.name)
    assert scan(missing, log).uidvalidity is None
    assert any("did not report UIDVALIDITY" in rec.message for rec in caplog.records), caplog.text

    caplog.clear()
    malformed = UidFetchServer(raws(1), uidvalidity="not-a-number")
    assert scan(malformed, log).uidvalidity is None
    assert any("malformed UIDVALIDITY" in rec.message for rec in caplog.records), caplog.text


def test_scan_retries_uid_search_once_then_succeeds():
    server = UidFetchServer(raws(1, 2, 3), fail_search_times=1)
    result = scan(server)

    assert len(result.messages) == 3
    assert len([call for call in server.uid_calls if call[0] == "SEARCH"]) == 2


def test_scan_skips_the_folder_when_uid_search_fails_twice():
    server = UidFetchServer(raws(1), fail_search_times=2)
    with pytest.raises(core.SkipFolderException, match="UID SEARCH failed"):
        scan(server)
    assert len([call for call in server.uid_calls if call[0] == "SEARCH"]) == 2


def test_scan_retries_a_header_page_once():
    server = UidFetchServer(raws(1, 2), fail_header_times=1)
    result = scan(server)

    assert len(result.messages) == 2
    assert server.header_fetches == [[1, 2], [1, 2]], server.header_fetches


def test_scan_skips_the_folder_when_a_header_page_fails_twice():
    server = UidFetchServer(raws(1, 2), fail_header_times=2)
    with pytest.raises(core.SkipFolderException, match="HEADER FETCH failed"):
        scan(server)
    assert server.header_fetches == [[1, 2], [1, 2]], server.header_fetches


def test_scan_retries_a_missing_header_uid_alone_then_skips_the_folder():
    server = UidFetchServer(raws(1, 2, 3), missing_uids={2})
    with pytest.raises(core.SkipFolderException, match="UID 2"):
        scan(server)
    assert server.header_fetches == [[1, 2, 3], [2]], server.header_fetches


def test_scan_skips_the_folder_on_an_unattributed_header_response():
    server = UidFetchServer(raws(1, 2, 3), unattributed_uids={2})
    with pytest.raises(core.SkipFolderException, match="Malformed HEADER FETCH"):
        scan(server)
    assert server.header_fetches == [[1, 2, 3], [1, 2, 3]], server.header_fetches


def test_scan_skips_the_folder_when_select_fails():
    server = UidFetchServer(raws(1), select_ok=False)
    with pytest.raises(core.SkipFolderException, match="SELECT failed"):
        scan(server)
    assert server.uid_calls == []


def test_scan_empty_folder_makes_no_header_requests():
    server = UidFetchServer({})
    result = scan(server)

    assert result.messages == {} and result.uidvalidity == 777
    assert server.fetches == [] and server.header_fetches == []
