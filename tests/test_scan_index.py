"""本地扫描索引（规格 §5.6 / §12.2 D 组）。

索引是缓存，不是判重事实来源：命中必须与内容扫描给出同一集合，任何指纹变化、
数据库损坏、锁超时或不可写都只能让本次运行退回内容扫描，绝不能导致漏信。
所有用例由 conftest 的 isolate_index 重定向到各自的临时目录。
"""
import contextlib
import email
import logging
import mailbox
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import imapbackup312 as core
from fakes import MainServer, NullLog, UidFetchServer

LOG = NullLog()


def write_mbox(path: Path, msg_ids: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    mb = mailbox.mbox(str(path))
    try:
        for msg_id in msg_ids:
            raw = (f"From: sender@example.com\r\nSubject: {msg_id}\r\n"
                   f"Message-Id: {msg_id}\r\n\r\nbody {msg_id}\r\n")
            mb.add(email.message_from_string(raw))
    finally:
        mb.close()
    return path


def append_mbox(path: Path, msg_id: str) -> None:
    mb = mailbox.mbox(str(path))
    try:
        raw = (f"From: sender@example.com\r\nSubject: {msg_id}\r\n"
               f"Message-Id: {msg_id}\r\n\r\nbody {msg_id}\r\n")
        mb.add(email.message_from_string(raw))
    finally:
        mb.close()


def write_eml(folder: Path, name: str, msg_id: str | None) -> Path:
    path = folder / name
    path.parent.mkdir(parents=True, exist_ok=True)
    headers = ["From: sender@example.com", "Subject: test"]
    if msg_id is not None:
        headers.append(f"Message-Id: {msg_id}")
    path.write_bytes(("\r\n".join(headers) + f"\r\n\r\nbody {name}\r\n").encode())
    return path


def open_index(stats: core.IndexStats | None = None, log: logging.Logger = LOG) -> core.ScanIndex:
    index = core.ScanIndex.open(log, stats)
    assert index is not None
    return index


def db_rows(sql: str, params: tuple = ()) -> list[tuple]:
    conn = sqlite3.connect(core.INDEX_DIR / core.DB_NAME)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def mbox_scan(index: core.ScanIndex, root: Path | str, account: str = "acct",
              folder: str = "INBOX", relpath: str = "acct/INBOX.mbox",
              overwrite: bool = False, log: logging.Logger = LOG) -> dict[str, str] | None:
    return index.local_mbox_ids(root, account, folder, relpath, overwrite, True, True, log)


def eml_scan(index: core.ScanIndex, root: Path | str, account: str = "acct",
             folder: str = "INBOX", relpath: str = "acct/INBOX",
             overwrite: bool = False, log: logging.Logger = LOG) -> dict[str, str] | None:
    return index.local_eml_ids(root, account, folder, relpath, overwrite, True, True, log)


def test_mbox_cold_rebuild_then_warm_hit_without_content_scan(tmp_path, monkeypatch, caplog):
    """冷运行建索引；指纹未变时直接读 message 行，不再逐封读 mbox（D 组冷/热）。"""
    root = tmp_path / "backup"
    ids = ["<a@x>", "<b@x>"]
    write_mbox(root / "acct/INBOX.mbox", ids)
    log = logging.getLogger("test.scan_index.hit")
    stats = core.IndexStats()
    index = open_index(stats, log)
    try:
        assert mbox_scan(index, root) == {msg_id: msg_id for msg_id in ids}
    finally:
        index.close()
    assert (stats.hits, stats.misses, stats.rebuilds) == (0, 1, 1)

    def fail_scan(*_args, **_kwargs):
        raise AssertionError("warm run must not read mbox contents")

    monkeypatch.setattr(core, "scan_file", fail_scan)
    stats = core.IndexStats()
    index = open_index(stats, log)
    try:
        with caplog.at_level(logging.INFO, logger=log.name):
            assert mbox_scan(index, root, log=log) == {msg_id: msg_id for msg_id in ids}
    finally:
        index.close()
    assert (stats.hits, stats.misses, stats.rebuilds) == (1, 0, 0)
    assert any("Local index hit" in record.message for record in caplog.records)


def test_mbox_size_or_mtime_change_rebuilds_and_never_skips_new_messages(tmp_path):
    """新增消息改变 size、只改 mtime 也失效；重建结果必须包含新增的 Message-Id。"""
    root = tmp_path / "backup"
    path = write_mbox(root / "acct/INBOX.mbox", ["<a@x>", "<b@x>"])
    stats = core.IndexStats()
    index = open_index(stats)
    try:
        assert set(mbox_scan(index, root) or {}) == {"<a@x>", "<b@x>"}

        append_mbox(path, "<c@x>")
        assert set(mbox_scan(index, root) or {}) == {"<a@x>", "<b@x>", "<c@x>"}
        assert (stats.misses, stats.rebuilds) == (2, 2)

        assert set(mbox_scan(index, root) or {}) == {"<a@x>", "<b@x>", "<c@x>"}
        assert stats.hits == 1

        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        assert set(mbox_scan(index, root) or {}) == {"<a@x>", "<b@x>", "<c@x>"}
        assert (stats.misses, stats.rebuilds) == (3, 3)
    finally:
        index.close()


def test_missing_mbox_returns_empty_and_drops_the_sink(tmp_path):
    """mbox 被删后本地集合为空，旧 sink 行必须清掉，避免下次拿陈旧 Message-Id 判重。"""
    root = tmp_path / "backup"
    path = write_mbox(root / "acct/INBOX.mbox", ["<a@x>"])
    stats = core.IndexStats()
    index = open_index(stats)
    try:
        assert set(mbox_scan(index, root) or {}) == {"<a@x>"}
        path.unlink()
        assert mbox_scan(index, root) == {}
    finally:
        index.close()
    assert db_rows("SELECT COUNT(*) FROM sink WHERE kind = 'mbox'") == [(0,)]
    assert stats.misses == 2


def test_eml_cold_rebuild_then_warm_hit_without_header_reads(tmp_path, monkeypatch):
    """eml 逐文件指纹全一致时直接返回映射，不再打开任何 .eml 读头。"""
    root = tmp_path / "backup"
    folder = root / "acct/INBOX"
    write_eml(folder, "a.eml", "<a@x>")
    write_eml(folder, "sub/b.eml", "<b@x>")
    stats = core.IndexStats()
    index = open_index(stats)
    try:
        assert eml_scan(index, root) == {"<a@x>": "<a@x>", "<b@x>": "<b@x>"}
    finally:
        index.close()
    assert (stats.hits, stats.misses, stats.rebuilds) == (0, 1, 1)

    def fail_scan(*_args, **_kwargs):
        raise AssertionError("warm run must not read eml headers")

    monkeypatch.setattr(core, "_scan_eml_files", fail_scan)
    stats = core.IndexStats()
    index = open_index(stats)
    try:
        assert eml_scan(index, root) == {"<a@x>": "<a@x>", "<b@x>": "<b@x>"}
    finally:
        index.close()
    assert (stats.hits, stats.misses, stats.rebuilds) == (1, 0, 0)


def test_eml_add_remove_size_and_mtime_changes_rebuild(tmp_path):
    """新增/缺失/改大小/改 mtime 任一变化都退回内容扫描，且结果不落后于磁盘。"""
    root = tmp_path / "backup"
    folder = root / "acct/INBOX"
    a = write_eml(folder, "a.eml", "<a@x>")
    b = write_eml(folder, "b.eml", "<b@x>")
    stats = core.IndexStats()
    index = open_index(stats)
    try:
        assert set(eml_scan(index, root) or {}) == {"<a@x>", "<b@x>"}

        c = write_eml(folder, "c.eml", "<c@x>")
        assert set(eml_scan(index, root) or {}) == {"<a@x>", "<b@x>", "<c@x>"}
        assert set(eml_scan(index, root) or {}) == {"<a@x>", "<b@x>", "<c@x>"}   # 重建后再命中

        a.unlink()
        assert set(eml_scan(index, root) or {}) == {"<b@x>", "<c@x>"}
        assert set(eml_scan(index, root) or {}) == {"<b@x>", "<c@x>"}

        b.write_bytes(b.read_bytes() + b"extra")
        assert set(eml_scan(index, root) or {}) == {"<b@x>", "<c@x>"}

        st = c.stat()
        os.utime(c, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        assert set(eml_scan(index, root) or {}) == {"<b@x>", "<c@x>"}

        write_eml(folder, "no-id.eml", None)   # 没有 Message-Id 的文件也要参与指纹
        expected = set(core.scan_eml_dir(folder, False, True, quiet=True, log=LOG))
        assert set(eml_scan(index, root) or {}) == expected == {"<b@x>", "<c@x>"}
    finally:
        index.close()
    assert stats.rebuilds >= 5
    assert stats.hits >= 2


def test_overwrite_clears_only_the_selected_sink(tmp_path):
    """--overwrite 只清对应格式的 sink；未选格式的行原样保留（规格 §5.6.3）。"""
    root = tmp_path / "backup"
    write_mbox(root / "acct/INBOX.mbox", ["<a@x>"])
    write_eml(root / "acct/INBOX", "a.eml", "<a@x>")
    index = open_index()
    try:
        assert mbox_scan(index, root) is not None
        assert eml_scan(index, root) is not None

        assert eml_scan(index, root, overwrite=True) is None
        assert db_rows("SELECT COUNT(*) FROM sink WHERE kind = 'mbox'") == [(1,)]
        assert db_rows("SELECT COUNT(*) FROM sink WHERE kind = 'eml'") == [(0,)]

        assert mbox_scan(index, root, overwrite=True) is None
        assert db_rows("SELECT COUNT(*) FROM sink WHERE kind = 'mbox'") == [(0,)]

        # eml-only 运行（不碰 mbox）不删 mbox 行
        assert mbox_scan(index, root) is not None
        assert eml_scan(index, root) is not None
        assert db_rows("SELECT COUNT(*) FROM sink WHERE kind = 'mbox'") == [(1,)]
    finally:
        index.close()


def test_corrupt_db_is_deleted_and_rebuilt(tmp_path, caplog):
    """文件不是 SQLite 库时删掉重建，本次备份继续用索引而不是永久禁用。"""
    core.INDEX_DIR.mkdir(parents=True, exist_ok=True)
    db_path = core.INDEX_DIR / core.DB_NAME
    db_path.write_bytes(b"not a sqlite database")
    log = logging.getLogger("test.scan_index.corrupt")
    stats = core.IndexStats()
    with caplog.at_level(logging.WARNING, logger=log.name):
        index = open_index(stats, log)
    try:
        assert not stats.disabled
        assert db_rows("PRAGMA user_version") == [(1,)]
    finally:
        index.close()
    assert any("unusable, rebuilding" in record.message for record in caplog.records)


def test_unsupported_schema_version_is_deleted_and_rebuilt(tmp_path):
    """user_version 不是本程序支持的版本时同样删库重建。"""
    core.INDEX_DIR.mkdir(parents=True, exist_ok=True)
    db_path = core.INDEX_DIR / core.DB_NAME
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA user_version = 99")
        conn.commit()
    finally:
        conn.close()
    stats = core.IndexStats()
    index = open_index(stats)
    try:
        assert not stats.disabled
        assert db_rows("PRAGMA user_version") == [(1,)]
    finally:
        index.close()


def test_schema_matches_the_spec_and_enforces_integrity(tmp_path):
    """§5.6.2/§11.3：列、PRAGMA、CHECK、UNIQUE 与级联删除与规格一致。"""
    index = open_index()
    try:
        conn = index._conn
        assert [row[1] for row in conn.execute("PRAGMA table_info(sink)")] == [
            "id", "kind", "root", "account", "folder", "relpath", "size", "mtime_ns"]
        assert [row[1] for row in conn.execute("PRAGMA table_info(message)")] == [
            "sink_id", "msg_id"]
        assert [row[1] for row in conn.execute("PRAGMA table_info(eml_file)")] == [
            "sink_id", "relpath", "size", "mtime_ns", "msg_id"]
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == core.INDEX_BUSY_TIMEOUT_MS
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA user_version").fetchone()[0] == core.INDEX_SCHEMA_VERSION

        sink_id = conn.execute(
            "INSERT INTO sink(kind, root, account, folder, relpath) "
            "VALUES('mbox', 'r', 'a', 'f', 'a/f.mbox')").lastrowid
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO sink(kind, root, account, folder, relpath) "
                         "VALUES('bad', 'r', 'a', 'g', 'a/g.mbox')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO sink(kind, root, account, folder, relpath) "
                         "VALUES('mbox', 'r', 'a', 'f', 'a/f2.mbox')")
        conn.execute("INSERT INTO message(sink_id, msg_id) VALUES(?, '<x@y>')", (sink_id,))
        conn.execute("DELETE FROM sink WHERE id = ?", (sink_id,))
        assert conn.execute("SELECT COUNT(*) FROM message").fetchone() == (0,)
    finally:
        index.close()


def test_corruption_found_after_open_drops_the_db_and_rescans(tmp_path, monkeypatch, caplog):
    """打开后才发现的损坏：删库、本次内容扫描、下次重建，不留下永久禁用的库。"""
    root = tmp_path / "backup"
    write_mbox(root / "acct/INBOX.mbox", ["<a@x>"])
    log = logging.getLogger("test.scan_index.corrupt_late")
    log.setLevel(logging.INFO)
    stats = core.IndexStats()
    index = open_index(stats, log)

    def corrupt_get_sink(*_args, **_kwargs):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(index, "_get_sink", corrupt_get_sink)
    with caplog.at_level(logging.WARNING, logger=log.name):
        assert mbox_scan(index, root, log=log) == {"<a@x>": "<a@x>"}
    index.close()
    assert stats.disabled
    assert (stats.misses, stats.rebuilds) == (1, 1)
    assert not (core.INDEX_DIR / core.DB_NAME).exists()
    assert any("Local scan index disabled" in record.message for record in caplog.records)

    rebuilt = open_index()
    try:
        assert mbox_scan(rebuilt, root) == {"<a@x>": "<a@x>"}
    finally:
        rebuilt.close()
    assert (core.INDEX_DIR / core.DB_NAME).exists()


def test_lock_timeout_disables_only_this_run(tmp_path, monkeypatch, caplog):
    """锁超时：打开失败 → None + WARNING；锁释放后下一次打开恢复。"""
    core.INDEX_DIR.mkdir(parents=True, exist_ok=True)
    db_path = core.INDEX_DIR / core.DB_NAME
    blocker = sqlite3.connect(db_path)
    log = logging.getLogger("test.scan_index.locked")
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        monkeypatch.setattr(core, "INDEX_BUSY_TIMEOUT_MS", 50)
        stats = core.IndexStats()
        with caplog.at_level(logging.WARNING, logger=log.name):
            assert core.ScanIndex.open(log, stats) is None
        assert stats.disabled
        assert any("Local scan index disabled" in record.message for record in caplog.records)
    finally:
        blocker.rollback()
        blocker.close()

    stats = core.IndexStats()
    index = core.ScanIndex.open(log, stats)
    assert index is not None and not stats.disabled
    index.close()


def test_unwritable_index_dir_disables_the_index(tmp_path, monkeypatch, caplog):
    """索引目录建不出来（父路径是普通文件）：返回 None + WARNING，绝不抛给备份。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(core, "INDEX_DIR", blocker / "index")
    log = logging.getLogger("test.scan_index.unwritable")
    stats = core.IndexStats()
    with caplog.at_level(logging.WARNING, logger=log.name):
        assert core.ScanIndex.open(log, stats) is None
    assert stats.disabled
    assert any("Local scan index disabled" in record.message for record in caplog.records)


RAW = {
    1: b"From: sender@example.com\r\nSubject: one\r\nMessage-Id: <1@x>\r\n\r\nbody one\r\n",
    2: b"From: sender@example.com\r\nSubject: two\r\nMessage-Id: <2@x>\r\n\r\nbody two\r\n",
}


def test_index_write_failure_only_disables_the_index(tmp_path):
    """登记失败：索引本次禁用，但 mbox 照写、report 照常逐封回调。"""
    root = tmp_path / "backup"
    ids = {"<1@x>": (1, len(RAW[1])), "<2@x>": (2, len(RAW[2]))}
    index = open_index()
    index._conn.close()   # 模拟一次真实的写入失败
    writer = index.writer("mbox", root, "acct", "INBOX", "acct/INBOX.mbox")
    reported: list[dict] = []
    try:
        progress = core.download_messages(
            UidFetchServer(RAW), "acct/INBOX.mbox", ids, False, True, False,
            root, False, False, LOG, foldername="INBOX", report=reported.append,
            index_mbox=writer)
    finally:
        index.close()
    assert progress.backed == 2
    assert len(reported) == 2
    assert (root / "acct/INBOX.mbox").read_bytes().count(b"From nobody ") == 2
    assert index._stats.disabled and index._disabled


def test_writer_commits_registered_messages_at_every_batch_end(tmp_path):
    """每批结束即提交：另一连接在 on_batch 时能看到本批已登记的消息。"""
    root = tmp_path / "backup"
    raws = {uid: (f"From: s@example.com\r\nSubject: {uid}\r\n"
                  f"Message-Id: <{uid}@x>\r\n\r\nbody {uid}\r\n").encode()
            for uid in range(1, 10)}
    ids = {f"<{uid}@x>": (uid, len(raws[uid])) for uid in raws}
    index = open_index()
    writer = index.writer("mbox", root, "acct", "INBOX", "acct/INBOX.mbox")
    counts: list[int] = []
    reader = sqlite3.connect(core.INDEX_DIR / core.DB_NAME)
    try:
        def on_batch(_stats):
            counts.append(reader.execute("SELECT COUNT(*) FROM message").fetchone()[0])

        progress = core.download_messages(
            UidFetchServer(raws), "acct/INBOX.mbox", ids, False, True, False,
            root, False, True, LOG, foldername="INBOX", index_mbox=writer,
            on_batch=on_batch)
    finally:
        reader.close()
        index.close()
    assert counts == [8, 9], counts
    assert progress.backed == 9

    stats = core.IndexStats()
    index = open_index(stats)
    try:
        assert set(mbox_scan(index, root) or {}) == set(ids)
    finally:
        index.close()
    assert stats.hits == 1


def test_uncommitted_batch_forces_a_rebuild_next_run(tmp_path):
    """批中被强杀（登记未提交）：文件已变长，指纹不一致 → 重建，绝不跳过新消息。"""
    root = tmp_path / "backup"
    path = write_mbox(root / "acct/INBOX.mbox", ["<a@x>", "<b@x>"])
    index = open_index()
    try:
        assert set(mbox_scan(index, root) or {}) == {"<a@x>", "<b@x>"}
        append_mbox(path, "<c@x>")
        writer = index.writer("mbox", root, "acct", "INBOX", "acct/INBOX.mbox")
        writer.mbox_written("<c@x>")
        writer.abort()   # 模拟硬杀时未提交的事务被丢弃
    finally:
        index.close()

    stats = core.IndexStats()
    index = open_index(stats)
    try:
        assert set(mbox_scan(index, root) or {}) == {"<a@x>", "<b@x>", "<c@x>"}
    finally:
        index.close()
    assert (stats.misses, stats.rebuilds) == (1, 1)


def test_root_and_account_are_part_of_the_cache_key(tmp_path):
    """不同输出根目录不能互相命中；sink 存邮箱账号与文件夹显示名，不存账户名称。"""
    root1 = tmp_path / "one"
    root2 = tmp_path / "two"
    write_mbox(root1 / "acct/INBOX.mbox", ["<one@x>"])
    write_mbox(root2 / "acct/INBOX.mbox", ["<two@x>"])
    stats = core.IndexStats()
    index = open_index(stats)
    try:
        assert set(mbox_scan(index, str(root1), folder="工作邮箱") or {}) == {"<one@x>"}
        assert set(mbox_scan(index, root2, folder="工作邮箱") or {}) == {"<two@x>"}
        assert mbox_scan(index, str(root1), folder="工作邮箱") == {"<one@x>": "<one@x>"}
        assert stats.hits == 1
    finally:
        index.close()
    rows = db_rows("SELECT kind, account, folder FROM sink ORDER BY root")
    assert rows == [("mbox", "acct", "工作邮箱"), ("mbox", "acct", "工作邮箱")], rows


def test_warm_local_scan_is_clearly_faster_on_a_large_mbox(tmp_path):
    """足够大的 mbox 上，热扫描只做 stat + 查表，明显低于逐封内容扫描（§12.3）。"""
    root = tmp_path / "backup"
    ids = [f"<m{i}@x>" for i in range(1, 2001)]
    write_mbox(root / "acct/INBOX.mbox", ids)

    cold_stats = core.IndexStats()
    index = open_index(cold_stats)
    try:
        assert len(mbox_scan(index, root) or {}) == len(ids)
    finally:
        index.close()

    hot_stats = core.IndexStats()
    index = open_index(hot_stats)
    try:
        assert len(mbox_scan(index, root) or {}) == len(ids)
    finally:
        index.close()
    assert hot_stats.hits == 1
    assert cold_stats.local_ms > 0
    assert hot_stats.local_ms < cold_stats.local_ms * 0.8, (cold_stats.local_ms, hot_stats.local_ms)


def test_warm_eml_scan_is_clearly_faster_than_a_cold_one(tmp_path):
    """足够大的 eml 目录上，热扫描只做逐文件 stat，明显低于逐文件读头（ticket 8）。"""
    root = tmp_path / "backup"
    ids = [f"<e{i}@x>" for i in range(400)]
    for i, msg_id in enumerate(ids):
        write_eml(root / "acct/INBOX", f"2024-05/{i}.eml", msg_id)

    cold_stats = core.IndexStats()
    index = open_index(cold_stats)
    try:
        assert len(eml_scan(index, root) or {}) == len(ids)
    finally:
        index.close()

    hot_stats = core.IndexStats()
    index = open_index(hot_stats)
    try:
        assert len(eml_scan(index, root) or {}) == len(ids)
    finally:
        index.close()
    assert hot_stats.hits == 1
    assert cold_stats.local_ms > 0
    assert hot_stats.local_ms < cold_stats.local_ms * 0.8, (cold_stats.local_ms, hot_stats.local_ms)


def test_parallel_threads_use_independent_connections_without_lock_errors(tmp_path):
    """每个线程一个连接，WAL 下并发读写不抛 database is locked（GUI 多账户同构）。"""
    roots = [tmp_path / f"root{i}" for i in range(4)]
    for i, root in enumerate(roots):
        write_mbox(root / "acct/INBOX.mbox", [f"<{i}a@x>", f"<{i}b@x>"])
    barrier = threading.Barrier(len(roots))
    errors: list[BaseException] = []
    stats_all: list[core.IndexStats] = []

    def worker(i: int) -> None:
        root = roots[i]
        stats = core.IndexStats()
        stats_all.append(stats)
        try:
            barrier.wait()
            index = core.ScanIndex.open(LOG, stats)
            assert index is not None
            try:
                assert set(mbox_scan(index, root) or {}) == {f"<{i}a@x>", f"<{i}b@x>"}
                append_mbox(root / "acct/INBOX.mbox", f"<{i}c@x>")
                writer = index.writer("mbox", root, "acct", "INBOX", "acct/INBOX.mbox")
                writer.mbox_written(f"<{i}c@x>")
                with (root / "acct/INBOX.mbox").open("rb") as fh:
                    writer.batch_end(fh.fileno())
                writer.finish()
            finally:
                index.close()
        except BaseException as exc:   # 线程异常要带回主线程断言
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=len(roots)) as pool:
        list(pool.map(worker, range(len(roots))))
    assert errors == []
    assert all(not stats.disabled for stats in stats_all)
    assert db_rows("SELECT COUNT(*) FROM sink") == [(len(roots),)]
    assert db_rows("SELECT COUNT(*) FROM message") == [(len(roots) * 3,)]


def test_cli_cold_warm_overwrite_and_db_deletion(tmp_path, monkeypatch, caplog):
    """CLI 集成：冷/热/删库/--overwrite 四轮都完整且不重复取信。"""
    root = tmp_path / "backup"
    log = logging.getLogger("test.scan_index.cli")
    log.setLevel(logging.INFO)
    monkeypatch.setattr(core, "configure_logging", lambda _cfg: log)
    args = ["-s", "imap.example.com", "-e", "-u", "x@y.com", "-p", "secret",
            "-d", str(root), "--folders", "INBOX", "--nospinner"]

    def run(server: MainServer) -> None:
        monkeypatch.setattr(core, "connect_and_login", lambda _cfg, _log: server)
        assert core.main(args) == 0

    cold = MainServer(messages=3)
    run(cold)
    assert cold.body_fetches == [[1, 2, 3]]
    assert (root / "x@y.com/INBOX.mbox").exists()

    scans: list[str] = []
    real_scan_file = core.scan_file

    def counting_scan_file(label, *args, **kwargs):
        scans.append(label)
        return real_scan_file(label, *args, **kwargs)

    monkeypatch.setattr(core, "scan_file", counting_scan_file)
    warm = MainServer(messages=3)
    with caplog.at_level(logging.INFO, logger=log.name):
        run(warm)
    assert warm.body_fetches == []
    assert scans == []
    assert any("Local index hit" in record.message for record in caplog.records)

    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(FileNotFoundError):
            Path(f"{core.INDEX_DIR / core.DB_NAME}{suffix}").unlink()
    scans.clear()
    rebuilt = MainServer(messages=3)
    run(rebuilt)
    assert rebuilt.body_fetches == []          # 重建索引，但本地都已存在
    assert scans == ["x@y.com/INBOX.mbox"], scans

    overwritten = MainServer(messages=3)
    run_with_overwrite = overwritten
    monkeypatch.setattr(core, "connect_and_login", lambda _cfg, _log: run_with_overwrite)
    assert core.main([*args, "-y"]) == 0
    assert overwritten.body_fetches == [[1, 2, 3]]
    assert (root / "x@y.com/INBOX.mbox").read_bytes().count(b"From nobody ") == 3

    after = MainServer(messages=3)
    run(after)
    assert after.body_fetches == []


def test_cli_backup_continues_when_the_index_directory_is_unwritable(tmp_path, monkeypatch, caplog):
    """索引不可写：WARNING 后本次禁用，备份仍完整、正确（ticket 3 / §12.2 D）。"""
    core.INDEX_DIR.write_bytes(b"not a directory")
    log = logging.getLogger("test.scan_index.cli_unwritable")
    log.setLevel(logging.INFO)
    monkeypatch.setattr(core, "configure_logging", lambda _cfg: log)
    root = tmp_path / "backup"
    args = ["-s", "imap.example.com", "-u", "x@y.com", "-p", "secret",
            "-d", str(root), "--folders", "INBOX", "--nospinner"]
    server = MainServer(messages=2)
    monkeypatch.setattr(core, "connect_and_login", lambda _cfg, _log: server)

    with caplog.at_level(logging.WARNING, logger=log.name):
        assert core.main(args) == 0

    assert server.body_fetches == [[1, 2]]
    assert (root / "x@y.com/INBOX.mbox").read_bytes().count(b"From nobody ") == 2
    assert any("Local scan index disabled" in record.message for record in caplog.records)


def test_cli_eml_only_run_preserves_mbox_index_rows(tmp_path, monkeypatch):
    """只备份 eml 的 CLI 运行不碰 mbox sink，原有索引行原样保留（ticket 4 / §5.6.3）。"""
    log = logging.getLogger("test.scan_index.cli_eml_only")
    log.setLevel(logging.INFO)
    monkeypatch.setattr(core, "configure_logging", lambda _cfg: log)
    mbox_root = tmp_path / "backup"
    eml_root = tmp_path / "eml"
    common = ["-s", "imap.example.com", "-u", "x@y.com", "-p", "secret",
              "--folders", "INBOX", "--nospinner"]
    mbox_rows_sql = ("SELECT m.msg_id FROM message m JOIN sink s ON s.id = m.sink_id "
                     "WHERE s.kind = 'mbox' ORDER BY m.msg_id")

    def run(args: list[str], server: MainServer) -> None:
        monkeypatch.setattr(core, "connect_and_login", lambda _cfg, _log: server)
        assert core.main(args) == 0

    run([*common, "-d", str(mbox_root)], MainServer(messages=2))
    assert db_rows("SELECT COUNT(*) FROM sink WHERE kind = 'mbox'") == [(1,)]
    mbox_ids = db_rows(mbox_rows_sql)
    assert len(mbox_ids) == 2

    run([*common, "--eml-dir", str(eml_root)], MainServer(messages=2))
    assert db_rows("SELECT COUNT(*) FROM sink WHERE kind = 'eml'") == [(1,)]
    assert db_rows("SELECT COUNT(*) FROM sink WHERE kind = 'mbox'") == [(1,)]
    assert db_rows(mbox_rows_sql) == mbox_ids
    assert list((eml_root / "x@y.com/INBOX").rglob("*.eml"))
