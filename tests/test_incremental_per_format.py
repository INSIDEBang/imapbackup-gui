"""按输出格式独立判重（ADR 0004）。

任一**已选**格式缺这封就算待备份，写盘时只写还缺它的那个格式，每封只 fetch 一次。
这里的断言覆盖报障场景（eml 齐全而 mbox 完全缺失不得被读成「全部已备份」）以及
让那次修复安全的重复写入防护。假服务器，不需要凭证。

未选中的 sink 传 None（而不是空 dict）：None 表示「这个格式这次没参与」，空 dict 表示
「参与但全都有」。这是整个规则能成立的前提，所以场景 3/4 专门钉住这两种语义。
"""
import mailbox

import imapbackup312 as core
from fakes import NullLog, UidFetchServer

LOG = NullLog()

RAW = {
    1: b"From: A <a@b.com>\r\nSubject: one\r\nMessage-Id: <1@x>\r\n\r\nbody one\r\n",
    2: b"From: B <b@b.com>\r\nSubject: two\r\nMessage-Id: <2@x>\r\n\r\nbody two\r\n",
    3: b"From: C <c@b.com>\r\nSubject: three\r\nMessage-Id: <3@x>\r\n\r\nbody three\r\n",
}
REMOTE = {"<1@x>": (1, len(RAW[1])), "<2@x>": (2, len(RAW[2])), "<3@x>": (3, len(RAW[3]))}
IDS = list(REMOTE)


def server():
    """A fake UID server answering body FETCH with a fixed INTERNALDATE for all three messages."""
    return UidFetchServer(RAW)


def fetch_uids(srv):
    return [uid for batch in srv.body_fetches for uid in batch]


def scan(td, basedir=True, eml=True, overwrite=False):
    """Scan only the selected sinks, returning None for the rest — mirroring main()."""
    local_mbox = (core.scan_file("acct/INBOX.mbox", overwrite, True, td, quiet=True, log=LOG)
                  if basedir else None)
    local_eml = (core.scan_eml_dir(td / "acct" / "INBOX", overwrite, True, quiet=True, log=LOG)
                 if eml else None)
    return local_mbox, local_eml


def download(td, new, local_mbox, local_eml, basedir=True, eml=True):
    """Drive the real download path with a fake server; returns the server used."""
    srv = server()
    core.download_messages(srv, "acct/INBOX.mbox", new, False, True, False,
                           td if basedir else None, False, True, LOG,
                           eml_dir=td / "acct" / "INBOX" if eml else None,
                           skip_mbox=local_mbox, skip_eml=local_eml, foldername="INBOX")
    return srv


def mbox_ids(td):
    """Every Message-Id in the mbox, duplicates included (scan_file would hide them)."""
    mb = mailbox.mbox(td / "acct" / "INBOX.mbox")
    try:
        ids = []
        for msg in mb:
            mid = msg.get("Message-Id")
            assert mid is not None   # 夹具每封都带 Message-Id；混进 None 时 sorted() 会直接崩
            ids.append(mid)
        return ids
    finally:
        mb.close()


def eml_files(td):
    return sorted((td / "acct" / "INBOX").rglob("*.eml"))


def test_eml_complete_mbox_absent_is_not_all_backed_up(tmp_path):
    """The reported bug: eml complete while the mbox does not exist at all."""
    local_mbox, local_eml = scan(tmp_path, basedir=False)
    assert local_mbox is None
    assert local_eml == {}
    download(tmp_path, REMOTE, local_mbox, local_eml, basedir=False)
    assert len(eml_files(tmp_path)) == 3

    local_mbox, local_eml = scan(tmp_path)
    assert local_mbox == {}, local_mbox
    assert local_eml is not None
    assert set(local_eml) == set(REMOTE), sorted(local_eml)
    new = core.pending_messages(REMOTE, local_mbox, local_eml)
    # 这个曾经返回 {}，整轮被读成「已是最新，无待备份邮件」
    assert set(new) == set(REMOTE), sorted(new)
    srv = download(tmp_path, new, local_mbox, local_eml)
    assert sorted(fetch_uids(srv)) == [1, 2, 3], fetch_uids(srv)
    assert sorted(mbox_ids(tmp_path)) == IDS, mbox_ids(tmp_path)
    emls = eml_files(tmp_path)
    assert len(emls) == 3
    assert not any("（" in p.name for p in emls), [p.name for p in emls]

    # 两个 sink 都齐了才是真正的「已是最新」
    assert core.pending_messages(REMOTE, *scan(tmp_path)) == {}


def test_mbox_complete_eml_absent_is_not_all_backed_up(tmp_path):
    """Mirror of the reported bug: mbox complete while the eml tree is missing entirely."""
    download(tmp_path, REMOTE, None, None, basedir=True, eml=False)
    assert sorted(mbox_ids(tmp_path)) == IDS
    assert not (tmp_path / "acct" / "INBOX").exists()
    local_mbox, local_eml = scan(tmp_path)
    assert local_mbox is not None
    assert set(local_mbox) == set(REMOTE)
    assert local_eml == {}
    new = core.pending_messages(REMOTE, local_mbox, local_eml)
    assert set(new) == set(REMOTE), sorted(new)
    srv = download(tmp_path, new, local_mbox, local_eml)
    assert len(eml_files(tmp_path)) == 3, len(eml_files(tmp_path))
    assert sorted(mbox_ids(tmp_path)) == IDS, mbox_ids(tmp_path)
    assert sorted(fetch_uids(srv)) == [1, 2, 3], fetch_uids(srv)


def test_eml_only_run_leaves_mbox_pending(tmp_path):
    """Single-format run keeps today's semantics: an unselected sink stays None."""
    download(tmp_path, {"<1@x>": REMOTE["<1@x>"], "<2@x>": REMOTE["<2@x>"]}, None, None, basedir=False)
    local_mbox, local_eml = scan(tmp_path, basedir=False)
    assert local_mbox is None
    new = core.pending_messages(REMOTE, local_mbox, local_eml)
    assert set(new) == {"<3@x>"}, sorted(new)


def test_mbox_only_run_leaves_eml_pending(tmp_path):
    """The mbox-only mirror of the previous test."""
    download(tmp_path, {"<1@x>": REMOTE["<1@x>"], "<2@x>": REMOTE["<2@x>"]}, None, None, basedir=True, eml=False)
    local_mbox, local_eml = scan(tmp_path, eml=False)
    assert local_eml is None
    new = core.pending_messages(REMOTE, local_mbox, local_eml)
    assert set(new) == {"<3@x>"}, sorted(new)


def test_overwrite_treats_a_full_sink_as_empty(tmp_path):
    """overwrite means a full re-download: both sinks scan as empty even when full."""
    assert set(core.pending_messages(REMOTE, {}, {})) == set(REMOTE)
    download(tmp_path, REMOTE, None, None)
    assert scan(tmp_path, overwrite=True) == ({}, {})


def test_one_message_missing_from_eml_only(tmp_path):
    """Mixed split: one message present in the mbox but absent from the eml tree."""
    core.download_messages(server(), "acct/INBOX.mbox", {"<1@x>": REMOTE["<1@x>"], "<2@x>": REMOTE["<2@x>"]}, False, True, False,
                           tmp_path, False, True, LOG, foldername="INBOX")
    core.download_messages(server(), "acct/INBOX.mbox", REMOTE, False, True, False,
                           None, False, True, LOG, eml_dir=tmp_path / "acct" / "INBOX", foldername="INBOX")
    assert sorted(mbox_ids(tmp_path)) == IDS[:2] and len(eml_files(tmp_path)) == 3, (
        mbox_ids(tmp_path), len(eml_files(tmp_path)))
    local_mbox, local_eml = scan(tmp_path)
    new = core.pending_messages(REMOTE, local_mbox, local_eml)
    assert set(new) == {"<3@x>"}, sorted(new)
    srv = download(tmp_path, new, local_mbox, local_eml)
    # 只取缺的那一封，两个 sink 各写各的
    assert fetch_uids(srv) == [3], fetch_uids(srv)
    assert sorted(mbox_ids(tmp_path)) == IDS, mbox_ids(tmp_path)
    emls = eml_files(tmp_path)
    assert len(emls) == 3 and not any("（" in p.name for p in emls), [p.name for p in emls]
