#!/usr/bin/env python3
"""IMAP Incremental Backup Script (Python 3.12+ edition)

Modernized for Python 3.12:
 - argparse + dataclass config
 - pathlib usage
 - type hints
 - removed legacy socket monkey patch
 - quiet mode & safer error handling

Fork additions:
 - optional per-message .eml output tree (--eml-dir)
 - per-format incremental dedupe when both -d and --eml-dir are selected
 - per-message progress lines; structured report callback and cooperative
   stop hook consumed by the Tkinter GUI (imapbackup_gui.py)
 - shared daily run log written to %LOCALAPPDATA%\\imapbackup-gui\\log alongside
   the stderr output, so a failed run leaves something on disk to diagnose

Original contributors (abridged): jwagnerhki, Bob Ippolito, Michael Leonhard,
Giuseppe Scrivano, Ronan Sheth, Brandon Long, Christian Schanz, A. Bovett,
Mark Feit, Marco Machicao, and Rui Carmo.
"""
from __future__ import annotations

__version__ = "1.6.0"
__author__ = "Rui Carmo (http://taoofmac.com)"
__copyright__ = "(C) 2006-2025 Rui Carmo. Code under MIT License.(C)"
__contributors__ = ("jwagnerhki, Bob Ippolito, Michael Leonhard, Giuseppe Scrivano <gscrivano@gnu.org>, "
                    "Ronan Sheth, Brandon Long, Christian Schanz, A. Bovett, Mark Feit, Marco Machicao")

import argparse
import base64
import contextlib
import email.utils
import getpass
import hashlib
import imaplib
import logging
import mailbox
import os
import random
import re
import shutil
import socket
import sqlite3
import ssl
import sys
import threading
import time
import unicodedata
from collections.abc import Callable, Container, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from functools import cache
from pathlib import Path
from typing import IO

# Daily run log shared with the Tkinter GUI: both entry points append to one
# file per day under the app data directory, which the uninstaller deletes whole.
# LOG_DIR is a module attribute rather than a function-local so tests can redirect it.
LOG_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppDataLocal") / "imapbackup-gui" / "log"
LOG_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-10s | %(funcName)s:%(lineno)d | %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
LOG_FORMATTER = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)
# Run failures are recorded through this logger instead of 'imapbackup'. It holds
# only the file handler and does not propagate, so the failure is not printed a
# second time on stderr: the interpreter already prints the SystemExit message.
# When the run log cannot be created it is left without handlers, and the record
# falls back to stderr via propagation to root.
RUN_LOG_LOGGER = 'imapbackup.runlog'


def make_log_file_handler() -> logging.FileHandler | None:
    """Handler for today's run log, or None so the caller can degrade gracefully.

    The log is a diagnostic convenience and never a precondition for the backup:
    an unopenable directory or file returns None instead of raising. Hands back
    the handler already attached to root for the same file, so configuring
    logging twice in one process cannot write the daily log twice.
    """
    filename = LOG_DIR / f"run-{datetime.now():%Y-%m-%d}.log"
    base = os.path.normcase(os.path.normpath(str(filename)))
    for existing in logging.getLogger().handlers:
        if isinstance(existing, logging.FileHandler) and \
                os.path.normcase(os.path.normpath(existing.baseFilename)) == base:
            return existing
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        return logging.FileHandler(filename, encoding="utf-8")
    except OSError:
        return None


class SkipFolderException(Exception):
    """Indicates aborting processing of current folder, continue with next folder."""


class FolderFetchError(Exception):
    """A whole body-fetch batch was rejected per UID: one folder failure, not an account failure."""


class BackupWriteError(Exception):
    """Local output write failed; never retried as a connection error.

    Deliberately not an OSError subclass: reconnect classification treats OSError as
    a connection-level failure, and a disk error must not look like a dropped link.
    """


class ReconnectStopped(Exception):
    """Stop was requested while reconnecting; not a failure and not retried."""


class ReconnectAuthError(Exception):
    """A reconnect reached the server but authentication failed; never retried."""


@dataclass
class ReconnectStats:
    """Per-run reconnect counters; diagnostics read them, backup never depends on them."""

    attempts: int = 0
    successes: int = 0
    exhausted: bool = False
    uidvalidity_changes: int = 0
    last_reason: str | None = None


@dataclass
class FolderScan:
    """One folder's message scan: Message-Id -> (UID, RFC822.SIZE)."""

    messages: dict[str, tuple[int, int]]
    uidvalidity: int


@dataclass(frozen=True)
class BatchStats:
    """One completed body-fetch batch, for progress/UI/diagnostics."""

    msgs: int
    bytes: int
    fetch_ms: float
    backed: int
    fetch_failed: int


@dataclass
class DownloadProgress:
    """Mutable per-folder accumulator; a reconnect keeps adding to the same instance."""

    backed: int = 0
    fetch_failed: int = 0
    fetch_failed_missing: int = 0
    fetch_failed_no_bad: int = 0
    fetch_failed_malformed: int = 0
    done_bytes: int = 0
    batches: int = 0
    last_batch: BatchStats | None = None
    stopped: bool = False


@dataclass
class IndexStats:
    """Per-run local-scan index counters; diagnostics read them, backup never depends on them."""

    hits: int = 0
    misses: int = 0
    rebuilds: int = 0
    disabled: bool = False
    local_ms: float = 0.0


def _has_console() -> bool:
    """True when the status line can redraw in place: interactive stdout (and stdin).

    Gating on stdout matters: when output is redirected to a file, stdin is still a
    TTY, and \\r spinner frames would pollute the log. False under PyInstaller --windowed.
    """
    return (sys.stdin is not None and sys.stdout is not None
            and sys.stdout.isatty() and sys.stdin.isatty())


def _char_width(ch: str) -> int:
    """Terminal cells taken by one char: East Asian wide/fullwidth count as 2 (ambiguous as 1)."""
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _display_width(text: str) -> int:
    """Terminal display width of text."""
    return sum(_char_width(ch) for ch in text)


def _truncate_width(text: str, max_width: int) -> str:
    """Truncate text to fit max_width terminal cells, appending an ellipsis when cut."""
    if max_width <= 0:
        return ""
    if _display_width(text) <= max_width:
        return text
    out: list[str] = []
    width = 0
    for ch in text:
        cw = _char_width(ch)
        if width + cw > max_width - 1:
            break
        out.append(ch)
        width += cw
    return "".join(out) + "…"


def _terminal_cols() -> int:
    """Usable status-line columns: one cell reserved so a full line never wraps before \\r."""
    return max(shutil.get_terminal_size().columns - 1, 20)


class Spinner:
    """Single-line in-place status using \\r overwriting only (no ANSI escapes, old conhost safe).

    Every frame renders as `<glyph> <text>` so the spinning glyph stays pinned at
    column 0 — its position must never depend on the length of the text after it.
    """
    glyphs = "|/-\\"
    def __init__(self, message: str, disabled: bool, quiet: bool = False):
        self.message = message
        self.disabled = disabled or quiet or (not _has_console())
        self.pos = 0
        self._width = 0
        if not self.disabled:
            self._render(message)
    def _render(self, text: str) -> None:
        # A line wider than the terminal wraps, and \r could only return to the last
        # wrapped row — clamp every frame to the terminal width so redraw stays 1:1.
        cols = _terminal_cols()
        line = _truncate_width(f"{self.glyphs[self.pos]} {text}", cols)
        pad = " " * max(self._width - _display_width(line), 0)
        self._width = _display_width(line) + len(pad)
        sys.stdout.write("\r" + line + pad)
        sys.stdout.flush()
    def status(self, text: str) -> None:
        """Replace the status line content without advancing the glyph."""
        if self.disabled:
            return
        self.message = text
        self._render(text)
    def erase(self) -> None:
        """Blank the status line so a report line can be printed above it."""
        if self.disabled:
            return
        sys.stdout.write("\r" + " " * self._width + "\r")
        sys.stdout.flush()
        self._width = 0
    def spin(self) -> None:
        if self.disabled:
            return
        self.pos = (self.pos + 1) % len(self.glyphs)
        self._render(self.message)
    def stop(self) -> None:
        if self.disabled:
            return
        self.erase()


def pretty_byte_count(num: int) -> str:
    if num == 1:
        return "1 byte"
    if num < 1024:
        return f"{num} bytes"
    if num < 1 << 20:
        return f"{num/1024.0:.2f} KB"
    if num < 1 << 30:
        return f"{num/1048576.0:.3f} MB"
    if num < 1 << 40:
        return f"{num/1073741824.0:.3f} GB"
    return f"{num/1099511627776.0:.3f} TB"


MSGID_RE = re.compile(r"^Message-Id:\s*(.+)", re.IGNORECASE | re.MULTILINE)
BLANKS_RE = re.compile(r"\s+", re.MULTILINE)
UUID = '19AF1258-1AAF-44EF-9D9A-731079D6FAD7'

EML_SUFFIX = ".eml"
EML_SUBJECT_MAX = 60
EML_MSGID_MAX = 100
EML_SEGMENT_MAX = 80
EML_PATH_BUDGET = 240
EML_DUP_MARK = "（{}）"  # full-width parens, per fork convention
WIN_ILLEGAL_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f\x7f]')
WIN_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{n}" for p in ("COM", "LPT") for n in range(1, 10)}
INTERNALDATE_RE = re.compile(r'INTERNALDATE "([^"]+)"')
INTERNALDATE_TS_RE = re.compile(r"(\d{1,2})-([A-Za-z]{3})-(\d{4}) (\d{2}):(\d{2}):(\d{2}) ([+-])(\d{2})(\d{2})$")
DATE_HEADER_RE = re.compile(rb"^Date:[ \t]*(.+?)[ \t]*$", re.IGNORECASE | re.MULTILINE)
IMAP_MONTHS = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
               "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}
BAR_FILL = "█"
BAR_EMPTY = "░"
FOLDER_DELIMS = "/\\."  # IMAP hierarchy delimiters a server may use; iterate per char

SCAN_PAGE_UIDS = 500
FETCH_BATCH_MAX_MESSAGES = 8
FETCH_BATCH_MAX_BYTES = 8 * 1024 * 1024
RECONNECT_MAX_ATTEMPTS = 5
RECONNECT_BACKOFF_BASE_S = 2.0
RECONNECT_BACKOFF_CAP_S = 300.0
RECONNECT_STOP_POLL_S = 0.2
SCAN_MSGID_FETCH_ITEMS = "(UID RFC822.SIZE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])"
SCAN_FALLBACK_FETCH_ITEMS = "(UID BODY.PEEK[HEADER.FIELDS (FROM TO CC DATE SUBJECT)])"
BODY_FETCH_ITEMS = "(UID INTERNALDATE BODY.PEEK[])"
# 本地扫描索引是缓存，不是判重事实来源：删掉只会让下次运行变慢（规格 §5.6）。
INDEX_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppDataLocal") / "imapbackup-gui" / "index"
DB_NAME = "scan-index.sqlite3"
INDEX_SCHEMA_VERSION = 1
INDEX_BUSY_TIMEOUT_MS = 5000
_INDEX_OPEN_LOCK = threading.Lock()
UID_RE = re.compile(rb"\bUID (\d+)\b")
FETCH_SIZE_RE = re.compile(rb"\bRFC822\.SIZE (\d+)\b")


def string_from_file(value: str) -> str:
    if not value or value[0] not in ("\\", "@"):
        return value
    if value[0] == "\\":
        return value[1:]
    path = Path(os.path.expanduser(value[1:]))
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:  # pragma: no cover
        raise SystemExit(f"Unable to read password file '{path}': {exc}") from exc


def sanitize_segment(text: str, max_len: int) -> str:
    """Sanitize arbitrary text into one safe filesystem path segment."""
    text = WIN_ILLEGAL_RE.sub("_", BLANKS_RE.sub(" ", text)).strip()
    if len(text) > max_len:
        if max_len < 12:
            text = text[:max_len]
        else:
            digest = hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:8]
            text = text[: max_len - 9].rstrip() + "-" + digest
    text = text.strip(". ")
    if not text:
        return "_"
    if text.split(".")[0].upper() in WIN_RESERVED_NAMES:
        text = "_" + text
    return text


def sanitize_msgid(msg_id: str, max_len: int = EML_MSGID_MAX) -> str:
    text = msg_id.strip()
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1]
    return sanitize_segment(text, max_len)


def build_eml_target(bucket_dir: Path, timestamp: datetime, msg_id: str, subject: str | None) -> Path:
    """eml target path `<subject>_<Message-Id>_yyyyMMdd.eml`; subject, then Message-Id, shrink until the path fits the budget."""

    def assemble(subj_part: str, mid_part: str) -> Path:
        parts = ([subj_part] if subj_part else []) + [mid_part, timestamp.strftime("%Y%m%d")]
        return bucket_dir / ("_".join(parts) + EML_SUFFIX)

    target: Path
    for subj_cap, mid_cap in ((EML_SUBJECT_MAX, EML_MSGID_MAX), (30, EML_MSGID_MAX), (0, 60), (0, 30), (0, 16)):
        subject_part = sanitize_segment(subject, subj_cap) if subject and subj_cap else ""
        if subject_part == "_":
            subject_part = ""
        target = assemble(subject_part, sanitize_msgid(msg_id, mid_cap))
        if len(str(target)) <= EML_PATH_BUDGET:
            return target
    return target


def unique_eml_path(target: Path) -> Path:
    """Append a （n） suffix when the target already exists (.exists() covers Windows case-insensitivity)."""
    if not target.exists():
        return target
    n = 2
    while (candidate := target.with_name(f"{target.stem}{EML_DUP_MARK.format(n)}{target.suffix}")).exists():
        n += 1
    return candidate


def parse_internaldate(value: str) -> datetime | None:
    """Parse an IMAP INTERNALDATE (English month abbreviations, zone offset) without locale traps."""
    m = INTERNALDATE_TS_RE.match(value.strip())
    if not m:
        return None
    day, mon, year, hh, mi, ss, sign, oh, om = m.groups()
    month = IMAP_MONTHS.get(mon)
    if month is None:
        return None
    delta = timedelta(hours=int(oh), minutes=int(om))
    if sign == "-":
        delta = -delta
    try:
        return datetime(int(year), month, int(day), int(hh), int(mi), int(ss), tzinfo=timezone(delta))
    except ValueError:
        return None


def message_timestamp(fetch_meta: str, raw_bytes: bytes) -> datetime:
    """Bucketing timestamp: INTERNALDATE, else the Date header, else now — always normalized to local time."""
    m = INTERNALDATE_RE.search(fetch_meta)
    if m:
        dt = parse_internaldate(m.group(1))
        if dt is not None:
            return dt.astimezone()
    m = DATE_HEADER_RE.search(raw_bytes[:8192])
    if m:
        try:
            dt = email.utils.parsedate_to_datetime(m.group(1).decode("ascii", "replace"))
        except (TypeError, ValueError, UnicodeDecodeError):
            dt = None
        if dt is not None:
            return dt.astimezone()
    return datetime.now().astimezone()


def decode_mime_words(value: str) -> str:
    """Decode RFC 2047 encoded words so Chinese subjects/senders display properly."""
    if "=?" not in value:
        return value
    try:
        chunks = decode_header(value)
    except Exception:
        return value
    out: list[str] = []
    for data, charset in chunks:
        if isinstance(data, bytes):
            try:
                out.append(data.decode(charset or "ascii", "replace"))
            except LookupError:
                out.append(data.decode("utf-8", "replace"))
        else:
            out.append(data)
    return "".join(out).strip()


def _subject_label(subject: str, max_len: int) -> str:
    """Display label for a subject: RFC 2047 decoded, placeholder when empty, ellipsized to max_len."""
    title = decode_mime_words(subject) if subject else "(no subject)"
    return title[: max_len - 1] + "…" if len(title) > max_len else title


@cache
def _folded_header_re(name: str) -> re.Pattern[str]:
    """Regex extracting a named header plus its continuation (folded) lines; compiled once per name."""
    return re.compile(rf"^{name}[ \t]*:(.*(?:\r?\n[ \t].*)*)", re.IGNORECASE | re.MULTILINE)


def folded_header(head: bytes, name: str) -> str:
    """Extract a (possibly folded) header value from raw head bytes, whitespace-collapsed."""
    m = _folded_header_re(name).search(head.decode("utf-8", "replace"))
    return BLANKS_RE.sub(" ", m.group(1)).strip() if m else ""


def progress_bar(idx: int, total: int, width: int = 12) -> str:
    """Count-based textual progress bar."""
    if total <= 0:
        return BAR_EMPTY * width
    filled = min(width, max(0, round(width * idx / total)))
    return BAR_FILL * filled + BAR_EMPTY * (width - filled)


def imap_utf7_decode(name: str) -> str:
    """Decode an IMAP4 modified UTF-7 mailbox name (RFC 3501 §5.1.3); malformed runs are kept raw."""
    if "&" not in name:
        return name
    out: list[str] = []
    i = 0
    while i < len(name):
        if name[i] != "&":
            out.append(name[i])
            i += 1
            continue
        j = name.find("-", i + 1)
        if j < 0:
            out.append(name[i:])
            break
        b64 = name[i + 1:j]
        if not b64:
            out.append("&")
        else:
            try:
                padded = b64.replace(",", "/") + "=" * (-len(b64) % 4)
                out.append(base64.b64decode(padded).decode("utf-16-be"))
            except Exception:
                out.append(name[i:j + 1])
        i = j + 1
    return "".join(out)


def folder_separator(idx: int, total: int, display_name: str) -> None:
    """Full-width separator line marking the start of a folder's sync (blank line above it)."""
    title = f"[{idx}/{total}] {display_name}"
    cols = _terminal_cols()
    dashes = max(cols - _display_width(title) - 2, 4)
    left = dashes // 2
    line = "─" * left + " " + title + " " + "─" * (dashes - left)
    if sys.stdout is not None:
        sys.stdout.write("\n" + line + "\n")
        sys.stdout.flush()


def format_message_line(foldername: str, idx: int, total: int, timestamp: datetime,
                        from_value: str, subject: str, size: int, msg_id: str,
                        verbose: int) -> str:
    """Render the one-line per-message terminal report from raw fields."""
    sender = decode_mime_words(from_value) if from_value else "?"
    name, addr = email.utils.parseaddr(sender)
    sender = f"{name} <{addr}>" if name and addr else (addr or sender)
    if len(sender) > 60:
        sender = sender[:59] + "…"
    title = _subject_label(subject, 80)
    line = f"[{foldername} {idx}/{total}] {timestamp:%Y-%m-%d %H:%M} | {sender} | {title} ({pretty_byte_count(size)})"
    if verbose:
        line += f" | {msg_id}"
    return line


def report_message(foldername: str, idx: int, total: int, timestamp: datetime,
                   from_value: str, subject: str, size: int, msg_id: str,
                   quiet: bool, verbose: int, report: Callable[[dict], None] | None = None,
                   erase: Callable[[], None] | None = None) -> None:
    """Emit per-message progress: a structured dict to the GUI callback, else one terminal line.

    The callback receives raw header values (undecoded From/Subject) — consumers
    run decode_mime_words themselves. See ADR 0003.
    """
    if quiet:
        return
    if report is not None:
        report({"folder": foldername, "index": idx, "total": total, "timestamp": timestamp,
                "from": from_value, "subject": subject, "size": size, "msg_id": msg_id})
        return
    line = format_message_line(foldername, idx, total, timestamp, from_value, subject, size, msg_id, verbose)
    if sys.stdout is not None:
        if erase is not None:
            erase()
        sys.stdout.write(line + "\n")
        sys.stdout.flush()


def _parse_uid_response(data: list[object]) -> tuple[dict[int, tuple[bytes, bytes | None]], int, set[int]]:
    """Parse a flat UID FETCH response into uid -> (metadata, literal).

    Non-tuple items (the trailing b")") are ignored. A tuple whose UID cannot be
    read is counted as unattributed; a tuple with a UID but no bytes literal keeps
    the UID so the caller can retry that message alone. A repeated UID keeps the
    first response and is reported separately for a WARNING.
    """
    parsed: dict[int, tuple[bytes, bytes | None]] = {}
    unattributed = 0
    duplicates: set[int] = set()
    for item in data:
        if not isinstance(item, tuple) or len(item) < 2:
            continue
        meta, literal = item[0], item[1]
        if not isinstance(meta, bytes):
            unattributed += 1
            continue
        match = UID_RE.search(meta)
        if match is None:
            unattributed += 1
            continue
        uid = int(match.group(1))
        if uid not in parsed:
            parsed[uid] = (meta, literal if isinstance(literal, bytes) else None)
        else:
            duplicates.add(uid)
    return parsed, unattributed, duplicates


def _parse_fetch_size(meta: bytes) -> int:
    match = FETCH_SIZE_RE.search(meta)
    return int(match.group(1)) if match else 0


def _fetch_one(server: imaplib.IMAP4, uid: int, items: str) -> tuple[str, tuple[bytes, bytes] | None]:
    """Single-UID FETCH classification: ok / missing / malformed / no_bad."""
    typ, data = server.uid("FETCH", str(uid), items)
    if typ != "OK":
        return "no_bad", None
    parsed, unattributed, _ = _parse_uid_response(data)
    entry = parsed.get(uid)
    if entry is None:
        return ("malformed" if unattributed else "missing"), None
    if entry[1] is None:
        return "malformed", None
    return "ok", (entry[0], entry[1])


def _plan_batches(items: list[tuple[str, int, int]]) -> list[list[tuple[str, int, int]]]:
    """Split UID-sorted (Message-Id, UID, size) into <=8 messages / <=8 MiB batches.

    Unknown (size <= 0) and oversized messages take a batch of their own, so a
    single huge message never drags neighbours over the byte cap.
    """
    batches: list[list[tuple[str, int, int]]] = []
    current: list[tuple[str, int, int]] = []
    current_bytes = 0
    for item in items:
        size = item[2]
        if size <= 0 or size > FETCH_BATCH_MAX_BYTES:
            if current:
                batches.append(current)
                current = []
                current_bytes = 0
            batches.append([item])
            continue
        if len(current) == FETCH_BATCH_MAX_MESSAGES or current_bytes + size > FETCH_BATCH_MAX_BYTES:
            batches.append(current)
            current = []
            current_bytes = 0
        current.append(item)
        current_bytes += size
    if current:
        batches.append(current)
    return batches


def _body_fetch_attempt(server: imaplib.IMAP4, uids: list[int]) -> tuple[dict[int, tuple[bytes, bytes | None]], int, set[int], str | None]:
    typ, data = server.uid("FETCH", ",".join(str(uid) for uid in uids), BODY_FETCH_ITEMS)
    if typ != "OK":
        return {}, 0, set(), str(data)
    parsed, unattributed, duplicates = _parse_uid_response(data)
    return parsed, unattributed, duplicates, None


def _isolate_body(server: imaplib.IMAP4, uids: list[int], log: logging.Logger,
                  display: str) -> tuple[dict[int, tuple[bytes, bytes]], dict[int, str]]:
    """Retry every UID of a rejected batch one by one; all NO/BAD is a folder failure."""
    fetched: dict[int, tuple[bytes, bytes]] = {}
    failures: dict[int, str] = {}
    for uid in uids:
        status, entry = _fetch_one(server, uid, BODY_FETCH_ITEMS)
        if status == "ok" and entry is not None:
            fetched[uid] = entry
        else:
            failures[uid] = status if status != "ok" else "malformed"
    if failures and len(failures) == len(uids) and all(bucket == "no_bad" for bucket in failures.values()):
        raise FolderFetchError("every UID in a FETCH batch was rejected by the server")
    log.info("%s: isolated batch → %d fetched, %d failed", display, len(fetched), len(failures))
    return fetched, failures


def _fetch_batch(server: imaplib.IMAP4, batch: list[tuple[str, int, int]], log: logging.Logger,
                 display: str) -> tuple[dict[int, tuple[bytes, bytes]], dict[int, str]]:
    """Fetch one batch and classify per-UID failures without writing anything."""
    uids = [uid for _, uid, _ in batch]
    parsed, unattributed, duplicates, error = _body_fetch_attempt(server, uids)
    if error is not None:
        log.warning("%s: FETCH rejected, retrying batch", display)
        parsed, unattributed, duplicates, error = _body_fetch_attempt(server, uids)
        if error is not None:
            log.warning("%s: FETCH rejected after retry, isolating UIDs", display)
            return _isolate_body(server, uids, log, display)
    if unattributed:
        log.warning("%s: %d FETCH response(s) had no UID", display, unattributed)
    if duplicates:
        log.warning("%s: duplicate UID(s) in FETCH response, keeping the first: %s",
                    display, sorted(duplicates))
    unknown = set(parsed) - set(uids)
    if unknown:
        log.warning("%s: FETCH returned unrequested UIDs %s", display, sorted(unknown))
    fetched: dict[int, tuple[bytes, bytes]] = {}
    failures: dict[int, str] = {}
    for uid in uids:
        entry = parsed.get(uid)
        if entry is not None and entry[1] is not None:
            fetched[uid] = (entry[0], entry[1])
            continue
        status, retry = _fetch_one(server, uid, BODY_FETCH_ITEMS)
        if status == "ok" and retry is not None:
            fetched[uid] = retry
        else:
            failures[uid] = status if status != "ok" else "malformed"
    return fetched, failures


@dataclass(frozen=True)
class _MessageOutput:
    timestamp: datetime
    from_value: str
    subject_value: str
    size: int
    wrote: bool
    wrote_mbox: bool
    eml_relpath: str | None


def _write_message(raw_bytes: bytes, meta_text: str, msg_id: str, *,
                   mbox: IO[bytes] | None, eml_dir: Path | None,
                   skip_mbox: Container[str], skip_eml: Container[str],
                   thunderbird: bool, filename: str) -> _MessageOutput:
    timestamp = message_timestamp(meta_text, raw_bytes)
    head = raw_bytes[:8192]
    from_value = folded_header(head, "From")
    subject_value = folded_header(head, "Subject")
    size = 0
    wrote = False
    wrote_mbox = False
    eml_relpath: str | None = None
    try:
        if mbox is not None and msg_id not in skip_mbox:
            buf = f"From nobody {time.ctime()}\n"
            if UUID in msg_id:
                buf += f"Message-Id: {msg_id}\n"
            mbox.write(buf.encode("utf-8"))
            text_bytes = raw_bytes.strip().replace(b"\r", b"")
            text_bytes = (text_bytes.replace(b"\nFrom ", b"\n From ") if thunderbird
                          else re.sub(br"\n(>*)From ", b"\n>\\1From ", text_bytes))
            mbox.write(text_bytes + b"\n\n")
            size = len(text_bytes)
            wrote = True
            wrote_mbox = True
        if eml_dir is not None and msg_id not in skip_eml:
            payload = raw_bytes.strip() + b"\r\n"
            if UUID in msg_id and b"message-id:" not in head.lower():
                payload = f"Message-Id: {msg_id}\r\n".encode() + payload
            bucket_dir = eml_dir / timestamp.strftime("%Y-%m")
            target = unique_eml_path(build_eml_target(bucket_dir, timestamp, msg_id, decode_mime_words(subject_value)))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            size = max(size, len(payload))
            wrote = True
            eml_relpath = target.relative_to(eml_dir).as_posix()
    except OSError as exc:
        raise BackupWriteError(f"Failed writing message {msg_id} to {filename}: {exc}") from exc
    return _MessageOutput(timestamp, from_value, subject_value, size, wrote, wrote_mbox, eml_relpath)


def download_messages(server: imaplib.IMAP4, filename: str, messages: dict[str, tuple[int, int]],
                      overwrite: bool, nospinner: bool, thunderbird: bool,
                      basedir: Path | None, icloud: bool, quiet: bool, log: logging.Logger,
                      eml_dir: Path | None = None, skip_mbox: Container[str] | None = None,
                      skip_eml: Container[str] | None = None, foldername: str = "",
                      verbose: int = 0, compact: bool = False,
                      report: Callable[[dict], None] | None = None,
                      should_stop: Callable[[], bool] | None = None,
                      *, written: set[str] | None = None,
                      progress: DownloadProgress | None = None,
                      on_batch: Callable[[BatchStats], None] | None = None,
                      index_mbox: IndexWriter | None = None,
                      index_eml: IndexWriter | None = None) -> DownloadProgress:
    """Fetch pending messages in UID batches, writing only the formats each one lacks.

    Messages are fetched once each as `(UID, RFC822.SIZE)` pairs; a batch is fully
    parsed before any byte is written, failed UIDs are counted per bucket and never
    counted as backed, and a stop discards the rest of the in-flight batch.
    """
    if skip_mbox is None:
        skip_mbox = frozenset()
    if skip_eml is None:
        skip_eml = frozenset()
    if progress is None:
        progress = DownloadProgress()
    if written is None:
        written = set()
    display = foldername or filename
    if basedir is not None:
        fullname = basedir / filename
        if overwrite and fullname.exists():
            if not quiet:
                log.info("Deleting mbox %s at %s", filename, fullname)
            try:
                fullname.unlink()
            except OSError as exc:
                raise BackupWriteError(f"Failed deleting {fullname}: {exc}") from exc
    if eml_dir is not None and overwrite and eml_dir.exists():
        if not quiet:
            log.info("Deleting eml folder %s", eml_dir)
        try:
            shutil.rmtree(eml_dir)
        except OSError as exc:
            raise BackupWriteError(f"Failed deleting {eml_dir}: {exc}") from exc
    if not messages:
        for writer in (index_mbox, index_eml):
            if writer is not None:
                writer.finish()
        if not quiet:
            log.info("%s: New messages: 0", filename)
        return progress

    count = len(messages)
    ordered = sorted(((msg_id, uid, size) for msg_id, (uid, size) in messages.items()),
                     key=lambda item: item[1])
    index_of = {msg_id: idx for idx, msg_id in enumerate(messages, start=1)}
    batches = _plan_batches(ordered)
    spinner = Spinner(f"Downloading {count} new messages to {display}", nospinner, quiet=quiet)
    biggest = 0
    output_bytes = 0
    t0 = time.monotonic()
    mbox: IO[bytes] | None = None
    completed = False
    try:
        if basedir is not None:
            try:
                (basedir / filename).parent.mkdir(parents=True, exist_ok=True)
                mbox = (basedir / filename).open("ab")
            except OSError as exc:
                raise BackupWriteError(f"Failed opening {basedir / filename}: {exc}") from exc
        for batch in batches:
            if should_stop is not None and should_stop():
                progress.stopped = True
                if not quiet:
                    log.info("%s: stopped by request before next batch", display)
                break
            batch_t0 = time.monotonic()
            fetched, failures = _fetch_batch(server, batch, log, display)
            fetch_ms = (time.monotonic() - batch_t0) * 1000.0
            if should_stop is not None and should_stop():
                progress.stopped = True
                if not quiet:
                    log.info("%s: stopped by request; discarding fetched batch", display)
                break
            backed_before = progress.backed
            failed_before = progress.fetch_failed
            for msg_id, uid, size in batch:
                if msg_id in written:
                    continue
                failure = failures.get(uid)
                if failure is not None:
                    progress.fetch_failed += 1
                    if failure == "missing":
                        progress.fetch_failed_missing += 1
                    elif failure == "no_bad":
                        progress.fetch_failed_no_bad += 1
                    else:
                        progress.fetch_failed_malformed += 1
                    log.debug("%s: could not fetch %s (%s)", display, msg_id, failure)
                    continue
                entry = fetched.get(uid)
                if entry is None:
                    continue
                meta, raw_bytes = entry
                output = _write_message(raw_bytes, str(meta, "utf-8", "replace"), msg_id,
                                        mbox=mbox, eml_dir=eml_dir, skip_mbox=skip_mbox,
                                        skip_eml=skip_eml, thunderbird=thunderbird, filename=filename)
                if not output.wrote:
                    continue
                if output.wrote_mbox and index_mbox is not None:
                    index_mbox.mbox_written(msg_id)
                if output.eml_relpath is not None and index_eml is not None:
                    index_eml.eml_written(output.eml_relpath, msg_id)
                written.add(msg_id)
                progress.backed += 1
                progress.done_bytes += size
                output_bytes += output.size
                if output.size > biggest:
                    biggest = output.size
                if not compact:
                    report_message(display, index_of[msg_id], count, output.timestamp,
                                   output.from_value, output.subject_value, output.size, msg_id,
                                   quiet, verbose, report, erase=spinner.erase)
                elapsed = time.monotonic() - t0
                rate = f" · {pretty_byte_count(int(output_bytes / elapsed))}/s" if elapsed >= 1.0 and output_bytes else ""
                idx = index_of[msg_id]
                status_text = (f"Downloading {display} [{progress_bar(idx, count)}] "
                               f"{idx}/{count} · {pretty_byte_count(output_bytes)}{rate}")
                if compact:
                    status_text += f" | {_subject_label(output.subject_value, 40)}"
                spinner.status(status_text)
                spinner.spin()
                if should_stop is not None and should_stop():
                    progress.stopped = True
                    if not quiet:
                        log.info("%s: stopped by request after %d/%d messages", display, idx, count)
                    break
            stats = BatchStats(msgs=len(batch), bytes=sum(size for _, _, size in batch),
                               fetch_ms=fetch_ms,
                               backed=progress.backed - backed_before,
                               fetch_failed=progress.fetch_failed - failed_before)
            progress.batches += 1
            progress.last_batch = stats
            if mbox is not None:
                try:
                    mbox.flush()
                except OSError as exc:
                    raise BackupWriteError(f"Failed flushing {filename}: {exc}") from exc
            if index_mbox is not None:
                index_mbox.batch_end(mbox.fileno() if mbox is not None else None)
            if index_eml is not None:
                index_eml.batch_end()
            if on_batch is not None:
                on_batch(stats)
            if progress.stopped:
                break
        completed = True
    finally:
        if mbox is not None:
            try:
                mbox.close()
            except OSError as exc:
                if sys.exc_info()[0] is None:
                    raise BackupWriteError(f"Failed closing {filename}: {exc}") from exc
        for writer in (index_mbox, index_eml):
            if writer is not None:
                if completed:
                    writer.finish()
                else:
                    writer.abort()
        spinner.stop()
    if not quiet:
        log.info("%s: %s total, %s largest", filename, pretty_byte_count(output_bytes), pretty_byte_count(biggest))
        if progress.fetch_failed:
            log.warning("%s: %d message(s) could not be fetched (missing=%d, no_bad=%d, malformed=%d)",
                        display, progress.fetch_failed, progress.fetch_failed_missing,
                        progress.fetch_failed_no_bad, progress.fetch_failed_malformed)
    return progress


def scan_file(filename: str, overwrite: bool, nospinner: bool, basedir: Path, quiet: bool, log: logging.Logger) -> dict[str, str]:
    if overwrite:
        return {}
    fullname = basedir / filename
    if not fullname.exists():
        if not quiet:
            log.info("File %s: not found", filename)
        return {}
    spinner = Spinner(f"File {filename}", nospinner, quiet=quiet)
    messages: dict[str, str] = {}
    header = 'Message-Id'
    mbox = mailbox.mbox(fullname)
    try:
        for idx, msg in enumerate(mbox):
            raw_val = msg.get(header)
            if not raw_val:
                if not quiet:
                    log.warning("Message #%d in %s has no %s header", idx, filename, header)
                spinner.spin()
                continue
            line = f"{header}: {raw_val}".strip()
            line = BLANKS_RE.sub(' ', line)
            match = MSGID_RE.match(line)
            if match:
                messages.setdefault(match.group(1), match.group(1))
            else:
                if not quiet:
                    log.warning("Message #%d in %s has malformed %s header", idx, filename, header)
            spinner.spin()
    finally:
        mbox.close()
    spinner.stop()
    if not quiet:
        log.info("%s: %d messages", filename, len(messages))
    return messages


@dataclass(frozen=True)
class _EmlFile:
    """One .eml file's fingerprint and Message-Id; empty msg_id marks a file without the header."""

    relpath: str
    size: int
    mtime_ns: int
    msg_id: str


def _scan_eml_files(folder_dir: Path, quiet: bool, log: logging.Logger,
                    spinner: Spinner | None = None) -> dict[str, _EmlFile]:
    """Read each .eml header once, returning relpath -> fingerprint + Message-Id."""
    files: dict[str, _EmlFile] = {}
    for path in sorted(folder_dir.rglob("*" + EML_SUFFIX)):
        if not path.is_file():
            continue
        try:
            with path.open("rb") as f:
                head = f.read(65536)
            st = path.stat()
        except OSError as exc:
            if not quiet:
                log.warning("Cannot read %s: %s", path, exc)
            continue
        value = folded_header(head, "Message-Id")
        if not value and not quiet:
            log.warning("%s has no Message-Id header", path)
        relpath = path.relative_to(folder_dir).as_posix()
        files[relpath] = _EmlFile(relpath, st.st_size, st.st_mtime_ns, value)
        if spinner is not None:
            spinner.spin()
    return files


def _stat_eml_files(folder_dir: Path) -> dict[str, tuple[int, int]] | None:
    """Fingerprints only; None means the tree could not be fully enumerated."""
    files: dict[str, tuple[int, int]] = {}
    for path in sorted(folder_dir.rglob("*" + EML_SUFFIX)):
        if not path.is_file():
            continue
        try:
            st = path.stat()
        except OSError:
            return None
        files[path.relative_to(folder_dir).as_posix()] = (st.st_size, st.st_mtime_ns)
    return files


def scan_eml_dir(folder_dir: Path, overwrite: bool, nospinner: bool, quiet: bool, log: logging.Logger) -> dict[str, str]:
    """Content-level scan mirroring scan_file(): recover Message-Ids from each .eml header block."""
    if overwrite:
        return {}
    if not folder_dir.is_dir():
        if not quiet:
            log.info("eml folder %s: not found", folder_dir)
        return {}
    spinner = Spinner(f"Scanning eml {folder_dir.name}", nospinner, quiet=quiet)
    try:
        records = _scan_eml_files(folder_dir, quiet, log, spinner)
    finally:
        spinner.stop()
    messages: dict[str, str] = {}
    for record in records.values():
        if record.msg_id:
            messages.setdefault(record.msg_id, record.msg_id)
    if not quiet:
        log.info("%s: %d messages", folder_dir, len(messages))
    return messages


def pending_messages[T](remote: dict[str, T], *scans: dict[str, str] | None) -> dict[str, T]:
    """Server messages that still need downloading, for the selected output formats.

    Each entry in `scans` is one selected format's local scan result (scan_file /
    scan_eml_dir); pass None for a format that is not selected so it cannot make
    every message look pending. A message is pending iff at least one selected
    format lacks it; download_messages' skip sets then gate per-sink writes.
    """
    selected = [scan for scan in scans if scan is not None]
    return {mid: remote[mid] for mid in remote if any(mid not in scan for scan in selected)}


_INDEX_SCHEMA = """
CREATE TABLE IF NOT EXISTS sink (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('mbox','eml')),
    root TEXT NOT NULL,
    account TEXT NOT NULL,
    folder TEXT NOT NULL,
    relpath TEXT NOT NULL,
    size INTEGER,
    mtime_ns INTEGER,
    UNIQUE (kind, root, account, folder)
);

CREATE TABLE IF NOT EXISTS message (
    sink_id INTEGER NOT NULL REFERENCES sink(id) ON DELETE CASCADE,
    msg_id TEXT NOT NULL,
    PRIMARY KEY (sink_id, msg_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS eml_file (
    sink_id INTEGER NOT NULL REFERENCES sink(id) ON DELETE CASCADE,
    relpath TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    msg_id TEXT NOT NULL,
    PRIMARY KEY (sink_id, relpath)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS eml_file_msg ON eml_file (sink_id, msg_id);
"""


class _IndexSchemaMismatch(Exception):
    """The DB exists but belongs to another scan-index schema; safe to delete and rebuild."""


@dataclass(frozen=True)
class _IndexSink:
    id: int
    relpath: str
    size: int | None
    mtime_ns: int | None


def _normalized_index_root(root: str | Path) -> str:
    """Cache key for an output root: absolute and case-normalized (Windows)."""
    return os.path.normcase(os.path.abspath(os.fspath(root)))


class IndexWriter:
    """Register written messages in the scan index; every failure disables the index only.

    Rows are registered as messages are written but only committed at batch_end(),
    so a process killed mid-batch leaves a fingerprint mismatch for the next run to
    detect and rebuild from content (never an incorrect skip).
    """

    def __init__(self, index: ScanIndex, kind: str, root: str | Path, account: str, folder: str,
                 relpath: str, overwrite: bool = False):
        self._index = index
        self._kind = kind
        self._root = Path(root)
        self._account = account
        self._folder = folder
        self._relpath = relpath
        self._overwrite = overwrite
        self._sink_id: int | None = None
        self._closed = False

    def _conn(self) -> sqlite3.Connection | None:
        if self._closed or self._index._disabled:
            return None
        return self._index._conn

    def _ensure_sink(self) -> int | None:
        if self._sink_id is not None:
            return self._sink_id
        conn = self._conn()
        if conn is None:
            return None
        root_key = _normalized_index_root(self._root)
        try:
            row = conn.execute(
                "SELECT id, relpath FROM sink WHERE kind = ? AND root = ? AND account = ? AND folder = ?",
                (self._kind, root_key, self._account, self._folder)).fetchone()
            if row is None:
                cur = conn.execute(
                    "INSERT INTO sink(kind, root, account, folder, relpath, size, mtime_ns) "
                    "VALUES(?,?,?,?,?,NULL,NULL)",
                    (self._kind, root_key, self._account, self._folder, self._relpath))
                if cur.lastrowid is None:
                    raise sqlite3.IntegrityError("scan index sink insert returned no id")
                self._sink_id = int(cur.lastrowid)
                return self._sink_id
            sink_id = int(row[0])
            if self._overwrite or str(row[1]) != self._relpath:
                # 覆盖写或换了输出文件：旧 Message-Id 不能留在索引里冒充本地已有
                conn.execute("DELETE FROM message WHERE sink_id = ?", (sink_id,))
                if self._kind == "eml":
                    conn.execute("DELETE FROM eml_file WHERE sink_id = ?", (sink_id,))
                conn.execute("UPDATE sink SET relpath = ? WHERE id = ?", (self._relpath, sink_id))
            self._sink_id = sink_id
            return sink_id
        except (sqlite3.Error, OSError) as exc:
            self._index._disable(exc)
            return None

    def mbox_written(self, msg_id: str) -> None:
        sink_id = self._ensure_sink()
        if sink_id is None:
            return
        conn = self._conn()
        if conn is None:
            return
        try:
            conn.execute("INSERT OR IGNORE INTO message(sink_id, msg_id) VALUES(?,?)", (sink_id, msg_id))
        except sqlite3.Error as exc:
            self._index._disable(exc)

    def eml_written(self, relpath: str, msg_id: str) -> None:
        sink_id = self._ensure_sink()
        if sink_id is None:
            return
        conn = self._conn()
        if conn is None:
            return
        try:
            st = (self._root / self._relpath / relpath).stat()
            conn.execute(
                "INSERT OR REPLACE INTO eml_file(sink_id, relpath, size, mtime_ns, msg_id) VALUES(?,?,?,?,?)",
                (sink_id, relpath, st.st_size, st.st_mtime_ns, msg_id))
            conn.execute("INSERT OR IGNORE INTO message(sink_id, msg_id) VALUES(?,?)", (sink_id, msg_id))
        except (sqlite3.Error, OSError) as exc:
            self._index._disable(exc)

    def batch_end(self, mbox_fd: int | None = None) -> None:
        if self._closed or self._index._disabled:
            return
        conn = self._index._conn
        try:
            if self._kind == "mbox" and mbox_fd is not None:
                sink_id = self._ensure_sink()
                if sink_id is None:
                    return
                st = os.fstat(mbox_fd)
                conn.execute("UPDATE sink SET size = ?, mtime_ns = ? WHERE id = ?",
                             (st.st_size, st.st_mtime_ns, sink_id))
            conn.commit()
        except (sqlite3.Error, OSError) as exc:
            self._index._disable(exc)

    def finish(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._index._disabled:
            return
        try:
            self._index._conn.commit()
        except sqlite3.Error as exc:
            self._index._disable(exc)

    def abort(self) -> None:
        """Drop the uncommitted batch; the written files stay and force a rebuild next run."""
        if self._closed:
            return
        self._closed = True
        if self._index._disabled:
            return
        with contextlib.suppress(sqlite3.Error):
            self._index._conn.rollback()


class ScanIndex:
    """Rebuildable SQLite cache of local Message-Ids (spec §5.6).

    The index answers "which Message-Ids are already on disk for this sink" while
    a file/directory fingerprint proves the answer is still current. It is never
    the dedupe source of truth: any failure disables it for the run and the caller
    falls back to the existing content scan.
    """

    def __init__(self, conn: sqlite3.Connection, stats: IndexStats, log: logging.Logger,
                 db_path: Path):
        self._conn = conn
        self._stats = stats
        self._log = log
        self._db_path = db_path
        self._disabled = False

    @classmethod
    def open(cls, log: logging.Logger, stats: IndexStats | None = None) -> ScanIndex | None:
        """Open (or create) the index for this thread; None means disabled for this run."""
        stats = stats if stats is not None else IndexStats()
        # 首次建库时多账户线程会同时切换 WAL；journal_mode 不遵守 busy_timeout，
        # 串行化打开过程，避免刚启动就互相判成锁超时。
        with _INDEX_OPEN_LOCK:
            return cls._open(log, stats)

    @classmethod
    def _open(cls, log: logging.Logger, stats: IndexStats) -> ScanIndex | None:
        db_path = INDEX_DIR / DB_NAME
        try:
            INDEX_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            stats.disabled = True
            log.warning("Local scan index disabled; falling back to content scans: %s", exc)
            return None
        for attempt in (1, 2):
            conn: sqlite3.Connection | None = None
            try:
                conn = sqlite3.connect(db_path, timeout=INDEX_BUSY_TIMEOUT_MS / 1000.0)
                index = cls(conn, stats, log, db_path)
                index._prepare()
                return index
            except _IndexSchemaMismatch as exc:
                if conn is not None:
                    conn.close()
                if attempt == 2:
                    stats.disabled = True
                    log.warning("Local scan index disabled; falling back to content scans: %s", exc)
                    return None
                cls._delete_db_files(db_path)
            except sqlite3.OperationalError as exc:
                # 锁超时/目录不可写：数据库可能正被其他账户线程使用，不能删
                if conn is not None:
                    conn.close()
                stats.disabled = True
                log.warning("Local scan index disabled; falling back to content scans: %s", exc)
                return None
            except sqlite3.DatabaseError as exc:
                if conn is not None:
                    conn.close()
                if attempt == 2:
                    stats.disabled = True
                    log.warning("Local scan index disabled; falling back to content scans: %s", exc)
                    return None
                log.warning("Local scan index is unusable, rebuilding: %s", exc)
                cls._delete_db_files(db_path)
            except OSError as exc:
                if conn is not None:
                    conn.close()
                stats.disabled = True
                log.warning("Local scan index disabled; falling back to content scans: %s", exc)
                return None
        stats.disabled = True
        log.warning("Local scan index disabled; falling back to content scans")
        return None

    def _prepare(self) -> None:
        self._conn.execute(f"PRAGMA busy_timeout = {INDEX_BUSY_TIMEOUT_MS}")
        mode = self._conn.execute("PRAGMA journal_mode").fetchone()
        if mode is None or str(mode[0]).lower() != "wal":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        row = self._conn.execute("PRAGMA user_version").fetchone()
        version = int(row[0]) if row is not None else 0
        if version not in (0, INDEX_SCHEMA_VERSION):
            raise _IndexSchemaMismatch(f"unsupported scan index schema version {version}")
        self._conn.executescript(_INDEX_SCHEMA)
        if version != INDEX_SCHEMA_VERSION:
            self._conn.execute(f"PRAGMA user_version = {INDEX_SCHEMA_VERSION}")
            self._conn.commit()

    @staticmethod
    def _delete_db_files(db_path: Path) -> None:
        for suffix in ("", "-wal", "-shm"):
            with contextlib.suppress(OSError):
                Path(f"{db_path}{suffix}").unlink()

    def _disable(self, exc: BaseException) -> None:
        if self._disabled:
            return
        self._disabled = True
        self._stats.disabled = True
        self._log.warning("Local scan index disabled for this run; falling back to content scans: %s", exc)
        with contextlib.suppress(sqlite3.Error):
            self._conn.close()

    def _discard_corrupt(self, exc: BaseException) -> None:
        """Drop a malformed DB so the next run rebuilds instead of disabling forever."""
        self._disable(exc)
        self._delete_db_files(self._db_path)

    def _handle_query_error(self, exc: BaseException) -> bool:
        """Disable this run's index; True means a corrupt DB was dropped and must be rebuilt."""
        corrupt = isinstance(exc, sqlite3.DatabaseError) and not isinstance(
            exc, (sqlite3.OperationalError, sqlite3.ProgrammingError))
        if corrupt:
            self._discard_corrupt(exc)
        else:
            self._disable(exc)
        return corrupt

    def _get_sink(self, kind: str, root: str | Path, account: str, folder: str) -> _IndexSink | None:
        row = self._conn.execute(
            "SELECT id, relpath, size, mtime_ns FROM sink "
            "WHERE kind = ? AND root = ? AND account = ? AND folder = ?",
            (kind, _normalized_index_root(root), account, folder)).fetchone()
        if row is None:
            return None
        return _IndexSink(int(row[0]), str(row[1]),
                          int(row[2]) if row[2] is not None else None,
                          int(row[3]) if row[3] is not None else None)

    def _upsert_sink(self, kind: str, root: str | Path, account: str, folder: str, relpath: str,
                     size: int | None, mtime_ns: int | None) -> int:
        root_key = _normalized_index_root(root)
        row = self._conn.execute(
            "SELECT id FROM sink WHERE kind = ? AND root = ? AND account = ? AND folder = ?",
            (kind, root_key, account, folder)).fetchone()
        if row is None:
            cur = self._conn.execute(
                "INSERT INTO sink(kind, root, account, folder, relpath, size, mtime_ns) VALUES(?,?,?,?,?,?,?)",
                (kind, root_key, account, folder, relpath, size, mtime_ns))
            if cur.lastrowid is None:
                raise sqlite3.IntegrityError("scan index sink insert returned no id")
            return int(cur.lastrowid)
        sink_id = int(row[0])
        self._conn.execute("UPDATE sink SET relpath = ?, size = ?, mtime_ns = ? WHERE id = ?",
                           (relpath, size, mtime_ns, sink_id))
        return sink_id

    def _delete_sink(self, kind: str, root: str | Path, account: str, folder: str) -> None:
        with self._conn:
            self._conn.execute(
                "DELETE FROM sink WHERE kind = ? AND root = ? AND account = ? AND folder = ?",
                (kind, _normalized_index_root(root), account, folder))

    def _write_mbox_snapshot(self, root: str | Path, account: str, folder: str, relpath: str,
                             messages: dict[str, str]) -> None:
        st = (Path(root) / relpath).stat()
        with self._conn:
            sink_id = self._upsert_sink("mbox", root, account, folder, relpath, st.st_size, st.st_mtime_ns)
            self._conn.execute("DELETE FROM message WHERE sink_id = ?", (sink_id,))
            self._conn.executemany("INSERT OR IGNORE INTO message(sink_id, msg_id) VALUES(?,?)",
                                   [(sink_id, msg_id) for msg_id in messages])

    def _write_eml_snapshot(self, root: str | Path, account: str, folder: str, relpath: str,
                            records: dict[str, _EmlFile]) -> None:
        with self._conn:
            sink_id = self._upsert_sink("eml", root, account, folder, relpath, None, None)
            self._conn.execute("DELETE FROM eml_file WHERE sink_id = ?", (sink_id,))
            self._conn.executemany(
                "INSERT OR REPLACE INTO eml_file(sink_id, relpath, size, mtime_ns, msg_id) VALUES(?,?,?,?,?)",
                [(sink_id, record.relpath, record.size, record.mtime_ns, record.msg_id)
                 for record in records.values()])
            self._conn.execute("DELETE FROM message WHERE sink_id = ?", (sink_id,))
            msg_ids = sorted({record.msg_id for record in records.values() if record.msg_id})
            self._conn.executemany("INSERT OR IGNORE INTO message(sink_id, msg_id) VALUES(?,?)",
                                   [(sink_id, msg_id) for msg_id in msg_ids])

    def _load_mbox_messages(self, sink_id: int) -> dict[str, str] | None:
        rows = self._conn.execute("SELECT msg_id FROM message WHERE sink_id = ?", (sink_id,)).fetchall()
        messages: dict[str, str] = {}
        for (msg_id,) in rows:
            if not isinstance(msg_id, str) or not msg_id:
                return None
            messages[msg_id] = msg_id
        return messages

    def _load_eml_messages(self, sink_id: int,
                           fingerprints: dict[str, tuple[int, int]]) -> dict[str, str] | None:
        rows = self._conn.execute(
            "SELECT relpath, size, mtime_ns, msg_id FROM eml_file WHERE sink_id = ?", (sink_id,)).fetchall()
        if len(rows) != len(fingerprints):
            return None
        messages: dict[str, str] = {}
        for relpath, size, mtime_ns, msg_id in rows:
            if fingerprints.get(relpath) != (size, mtime_ns) or not isinstance(msg_id, str):
                return None
            if msg_id:
                messages.setdefault(msg_id, msg_id)
        stored: set[str] = set()
        for (msg_id,) in self._conn.execute("SELECT msg_id FROM message WHERE sink_id = ?", (sink_id,)):
            if not isinstance(msg_id, str) or not msg_id:
                return None
            stored.add(msg_id)
        if stored != set(messages):
            return None
        return messages

    def local_mbox_ids(self, root: str | Path, account: str, folder: str, relpath: str,
                       overwrite: bool, nospinner: bool, quiet: bool, log: logging.Logger) -> dict[str, str] | None:
        """Local mbox Message-Ids, or None when overwrite makes the local set empty."""
        t0 = time.perf_counter()
        try:
            if overwrite:
                if not self._disabled:
                    try:
                        self._delete_sink("mbox", root, account, folder)
                    except (sqlite3.Error, OSError) as exc:
                        self._handle_query_error(exc)
                return None
            if self._disabled:
                return scan_file(relpath, False, nospinner, Path(root), quiet=quiet, log=log)
            try:
                sink = self._get_sink("mbox", root, account, folder)
                full = Path(root) / relpath
                if not full.exists():
                    if sink is not None:
                        self._delete_sink("mbox", root, account, folder)
                    self._stats.misses += 1
                    log.info("Local index miss: %s (mbox not found)", relpath)
                    return {}
                if sink is not None and sink.relpath == relpath:
                    st = full.stat()
                    if sink.size == st.st_size and sink.mtime_ns == st.st_mtime_ns:
                        loaded = self._load_mbox_messages(sink.id)
                        if loaded is not None:
                            self._stats.hits += 1
                            log.info("Local index hit: %s (%d messages)", relpath, len(loaded))
                            return loaded
            except (sqlite3.Error, OSError) as exc:
                if self._handle_query_error(exc):
                    self._stats.misses += 1
                    self._stats.rebuilds += 1
                return scan_file(relpath, False, nospinner, Path(root), quiet=quiet, log=log)
            messages = scan_file(relpath, False, nospinner, Path(root), quiet=quiet, log=log)
            self._stats.misses += 1
            self._stats.rebuilds += 1
            try:
                self._write_mbox_snapshot(root, account, folder, relpath, messages)
            except (sqlite3.Error, OSError) as exc:
                self._disable(exc)
            else:
                log.info("Local index rebuild: %s (%d messages)", relpath, len(messages))
            return messages
        finally:
            self._stats.local_ms += (time.perf_counter() - t0) * 1000.0

    def local_eml_ids(self, root: str | Path, account: str, folder: str, relpath: str,
                      overwrite: bool, nospinner: bool, quiet: bool, log: logging.Logger) -> dict[str, str] | None:
        """Local eml Message-Ids, or None when overwrite makes the local set empty."""
        t0 = time.perf_counter()
        folder_dir = Path(root) / relpath
        try:
            if overwrite:
                if not self._disabled:
                    try:
                        self._delete_sink("eml", root, account, folder)
                    except (sqlite3.Error, OSError) as exc:
                        self._handle_query_error(exc)
                return None
            if self._disabled:
                return scan_eml_dir(folder_dir, False, nospinner, quiet=quiet, log=log)
            try:
                sink = self._get_sink("eml", root, account, folder)
                if not folder_dir.is_dir():
                    if sink is not None:
                        self._delete_sink("eml", root, account, folder)
                    self._stats.misses += 1
                    log.info("Local index miss: %s (eml folder not found)", relpath)
                    return {}
                if sink is not None and sink.relpath == relpath:
                    fingerprints = _stat_eml_files(folder_dir)
                    if fingerprints is not None:
                        loaded = self._load_eml_messages(sink.id, fingerprints)
                        if loaded is not None:
                            self._stats.hits += 1
                            log.info("Local index hit: %s (%d messages)", relpath, len(loaded))
                            return loaded
            except (sqlite3.Error, OSError) as exc:
                if self._handle_query_error(exc):
                    self._stats.misses += 1
                    self._stats.rebuilds += 1
                return scan_eml_dir(folder_dir, False, nospinner, quiet=quiet, log=log)
            spinner = Spinner(f"Scanning eml {folder_dir.name}", nospinner, quiet=quiet)
            try:
                records = _scan_eml_files(folder_dir, quiet, log, spinner)
            finally:
                spinner.stop()
            messages: dict[str, str] = {}
            for record in records.values():
                if record.msg_id:
                    messages.setdefault(record.msg_id, record.msg_id)
            self._stats.misses += 1
            self._stats.rebuilds += 1
            try:
                self._write_eml_snapshot(root, account, folder, relpath, records)
            except (sqlite3.Error, OSError) as exc:
                self._disable(exc)
            else:
                log.info("Local index rebuild: %s (%d messages)", relpath, len(messages))
            return messages
        finally:
            self._stats.local_ms += (time.perf_counter() - t0) * 1000.0

    def writer(self, kind: str, root: str | Path, account: str, folder: str, relpath: str,
               overwrite: bool = False) -> IndexWriter:
        return IndexWriter(self, kind, root, account, folder, relpath, overwrite)

    def close(self) -> None:
        if self._disabled:
            return
        with contextlib.suppress(sqlite3.Error):
            self._conn.rollback()
        with contextlib.suppress(sqlite3.Error):
            self._conn.close()


def local_snapshots(scan_index: ScanIndex | None, basedir: Path | None, eml_root: Path | None,
                    account: str, folder: str, mbox_relpath: str, eml_relpath: str, *,
                    overwrite: bool, nospinner: bool, quiet: bool,
                    log: logging.Logger) -> tuple[dict[str, str] | None, dict[str, str] | None]:
    """Local Message-Ids for each selected output format, from the index or a content scan.

    A None entry means that format is not selected. When the index reports overwrite,
    the selected sink is deliberately empty, so it becomes an empty set here.
    """
    local_mbox: dict[str, str] | None = None
    if basedir is not None:
        if scan_index is not None:
            local_mbox = scan_index.local_mbox_ids(basedir, account, folder, mbox_relpath,
                                                   overwrite, nospinner, quiet, log) or {}
        else:
            local_mbox = scan_file(mbox_relpath, overwrite, nospinner, basedir,
                                   quiet=quiet, log=log)
    local_eml: dict[str, str] | None = None
    if eml_root is not None:
        if scan_index is not None:
            local_eml = scan_index.local_eml_ids(eml_root, account, folder, eml_relpath,
                                                 overwrite, nospinner, quiet, log) or {}
        else:
            local_eml = scan_eml_dir(eml_root / eml_relpath, overwrite, nospinner,
                                     quiet=quiet, log=log)
    return local_mbox, local_eml


def _read_uidvalidity(server: imaplib.IMAP4, display: str, log: logging.Logger) -> int | None:
    raw = server.untagged_responses.get("UIDVALIDITY")
    if not raw:
        log.warning("%s: server did not report UIDVALIDITY", display)
        return None
    try:
        value = raw[0]
        if isinstance(value, tuple):
            raise TypeError(value)
        return int(value)
    except (TypeError, ValueError):
        log.warning("%s: malformed UIDVALIDITY response", display)
        return None


def _parse_search_uids(data: list[object]) -> list[int] | None:
    tokens = b" ".join(item for item in data if isinstance(item, bytes)).split()
    try:
        return sorted({int(token) for token in tokens})
    except ValueError:
        return None


def _fetch_header_page(server: imaplib.IMAP4, uids: list[int], items: str,
                       display: str, log: logging.Logger) -> dict[int, tuple[bytes, bytes]]:
    """One page of header FETCHes, retried as a page; a bad UID alone is retried once."""
    requested = list(uids)
    wanted = set(requested)
    for attempt in (1, 2):
        typ, data = server.uid("FETCH", ",".join(str(uid) for uid in requested), items)
        if typ != "OK":
            if attempt == 1:
                log.warning("%s: header FETCH failed, retrying page", display)
                continue
            raise SkipFolderException(f"HEADER FETCH failed: {data}")
        parsed, unattributed, _ = _parse_uid_response(data)
        unknown = set(parsed) - wanted
        if unattributed or unknown:
            if attempt == 1:
                log.warning("%s: malformed header response, retrying page", display)
                continue
            raise SkipFolderException(f"Malformed HEADER FETCH response: {data!r}")
        resolved: dict[int, tuple[bytes, bytes]] = {}
        for uid in requested:
            entry = parsed.get(uid)
            if entry is not None and entry[1] is not None:
                resolved[uid] = (entry[0], entry[1])
                continue
            status, retry = _fetch_one(server, uid, items)
            if status != "ok" or retry is None:
                raise SkipFolderException(f"HEADER FETCH for UID {uid} failed ({status})")
            resolved[uid] = retry
        return resolved
    raise SkipFolderException("HEADER FETCH failed")


def _store_remote_message(messages: dict[str, tuple[int, int]], msg_id: str, uid: int, size: int,
                          display: str, log: logging.Logger) -> None:
    if msg_id in messages:
        log.warning("%s: duplicate Message-Id %s; using UID %d", display, msg_id, uid)
    messages[msg_id] = (uid, size)


def scan_folder(server: imaplib.IMAP4, foldername: str, nospinner: bool, quiet: bool, log: logging.Logger,
                display: str | None = None) -> FolderScan:
    """Take one complete UID snapshot of a folder: SEARCH ALL plus 500-UID header pages.

    A page that cannot be parsed is retried once, then the whole folder is skipped;
    a partial snapshot is never returned, because a missing entry would turn into a
    skipped message. Connection errors propagate to the caller for reconnect.
    """
    messages: dict[str, tuple[int, int]] = {}
    shown = display or foldername
    quoted = f'"{foldername}"'
    spinner = Spinner(f'Folder "{shown}"', nospinner, quiet=quiet)
    try:
        typ, data = server.select(quoted, readonly=True)
        if typ != "OK":
            raise SkipFolderException(f"SELECT failed: {data}")
        uidvalidity = _read_uidvalidity(server, shown, log)
        if uidvalidity is None:
            # 没有 UIDVALIDITY 就无法确认 SEARCH 落在了刚选的邮箱上：QQ「其他文件夹」
            # 这类分组容器接受 SELECT 却不切换选中态，SEARCH 会返回上一个邮箱的 UID。
            # 宁可跳过，也不把别的文件夹的信记到这个文件夹名下。
            raise SkipFolderException("server did not report a usable UIDVALIDITY")
        typ, data = server.uid("SEARCH", "ALL")
        if typ != "OK":
            log.warning("%s: UID SEARCH failed, retrying", shown)
            typ, data = server.uid("SEARCH", "ALL")
            if typ != "OK":
                raise SkipFolderException(f"UID SEARCH failed: {data}")
        uids = _parse_search_uids(data)
        if uids is None:
            raise SkipFolderException(f"Malformed UID SEARCH response: {data!r}")
        sizes: dict[int, int] = {}
        fallback: list[int] = []
        for start in range(0, len(uids), SCAN_PAGE_UIDS):
            page = uids[start:start + SCAN_PAGE_UIDS]
            fetched = _fetch_header_page(server, page, SCAN_MSGID_FETCH_ITEMS, shown, log)
            for uid in page:
                meta, literal = fetched[uid]
                sizes[uid] = _parse_fetch_size(meta)
                normalized = BLANKS_RE.sub(" ", str(literal, "utf-8", "replace").strip())
                match = MSGID_RE.match(normalized)
                if match is None:
                    fallback.append(uid)
                    continue
                _store_remote_message(messages, match.group(1), uid, sizes[uid], shown, log)
            spinner.spin()
        for start in range(0, len(fallback), SCAN_PAGE_UIDS):
            page = fallback[start:start + SCAN_PAGE_UIDS]
            fetched = _fetch_header_page(server, page, SCAN_FALLBACK_FETCH_ITEMS, shown, log)
            for uid in page:
                _, literal = fetched[uid]
                synthetic = "<" + UUID + "." + hashlib.sha1(literal.replace(b"\r\n", b"\t")).hexdigest() + ">"
                _store_remote_message(messages, synthetic, uid, sizes[uid], shown, log)
            spinner.spin()
    finally:
        spinner.stop()
    if not quiet:
        log.info("%s: %d messages", shown, len(messages))
    return FolderScan(messages, uidvalidity)


@dataclass
class FolderDownloadState:
    """One folder's remote snapshot across reconnect attempts (spec §5.7.6)."""

    scan: FolderScan
    pending: dict[str, tuple[int, int]]
    uidvalidity_checked: bool = False


def download_folder_with_reconnect(
    server: imaplib.IMAP4,
    connect: Callable[[], imaplib.IMAP4],
    state: FolderDownloadState,
    *,
    foldername: str,
    display: str,
    local_mbox: dict[str, str] | None,
    local_eml: dict[str, str] | None,
    written: set[str],
    download: Callable[[imaplib.IMAP4, dict[str, tuple[int, int]], bool], None],
    rescan: Callable[[imaplib.IMAP4], FolderScan],
    should_stop: Callable[[], bool] | None,
    log: logging.Logger,
    overwrite: bool = False,
    reuse_selection: bool = False,
    on_scan_changed: Callable[[FolderScan, dict[str, tuple[int, int]]], None] | None = None,
    stats: ReconnectStats | None = None,
) -> tuple[imaplib.IMAP4, FolderDownloadState]:
    """Download one folder with bounded reconnect and UIDVALIDITY-aware resume.

    Each attempt SELECTs the folder and compares UIDVALIDITY with the snapshot
    (except a CLI first attempt on the selection its scan just made). A change
    rescans the remote side once, reuses the local snapshots and subtracts
    ``written`` before fetching; a second change is a folder failure. ``download``
    receives the live connection, the messages still missing, and whether this is
    the first attempt's overwrite, so the caller can rebuild per-attempt sinks.
    """
    stats = stats if stats is not None else ReconnectStats()
    attempt = 0

    def operation(conn: imaplib.IMAP4) -> None:
        nonlocal attempt
        reuse = attempt == 0 and reuse_selection
        attempt += 1
        if not reuse:
            typ, data = conn.select(f'"{foldername}"', readonly=True)
            if typ != "OK":
                raise SkipFolderException(f"SELECT failed: {data}")
            uidvalidity = _read_uidvalidity(conn, display, log)
            if uidvalidity is None:
                raise SkipFolderException("server did not report a usable UIDVALIDITY")
            if uidvalidity != state.scan.uidvalidity:
                if state.uidvalidity_checked:
                    raise FolderFetchError("UIDVALIDITY changed twice during one folder run")
                state.scan = rescan(conn)
                state.uidvalidity_checked = True
                stats.uidvalidity_changes += 1
                state.pending = {msg_id: item for msg_id, item in
                                 pending_messages(state.scan.messages, local_mbox, local_eml).items()
                                 if msg_id not in written}
                log.warning("%s: UIDVALIDITY changed; rescanned the folder", display)
                if on_scan_changed is not None:
                    on_scan_changed(state.scan, state.pending)
        remaining = {msg_id: item for msg_id, item in state.pending.items()
                     if msg_id not in written}
        download(conn, remaining, attempt == 1 and overwrite)

    _, live = run_with_reconnect(server, connect, operation, should_stop=should_stop,
                                 log=log, stats=stats)
    return live, state


type ParenItem = str | list[ParenItem]


def parse_paren_list(row: str) -> tuple[list[ParenItem], str]:
    if not row or row[0] != '(':
        raise ValueError("Expected '('")
    row = row[1:]
    result: list[ParenItem] = []
    name_attr_re = re.compile(r"^\s*(\\[a-zA-Z0-9_]+)\s*")
    while row and row[0] != ')':
        if row[0] == '(':
            sub, row = parse_paren_list(row)
            result.append(sub)
        else:
            m = name_attr_re.search(row)
            if not m:
                raise ValueError("Malformed attribute list")
            result.append(m.group(1))
            row = row[m.end():]
    if not row or row[0] != ')':
        raise ValueError("Unterminated attribute list")
    return result, row[1:]


def parse_string_list(row: str) -> list[str]:
    slist = re.compile(r'\s*"([^"]+)"\s*|\s*(\S+)\s*').split(row)
    return [s for s in slist if s]


def parse_list(row: str) -> list[ParenItem]:
    row = row.strip()
    paren_list, rest = parse_paren_list(row)
    string_list = parse_string_list(rest)
    if len(string_list) != 2:
        raise ValueError("Unexpected LIST response format")
    return [paren_list, *string_list]


def folder_to_eml_relpath(foldername: str, delim: str) -> str:
    """Map an IMAP folder name to nested eml directories (one sanitized segment per hierarchy level)."""
    return "/".join(sanitize_segment(s, EML_SEGMENT_MAX) for s in foldername.split(delim))


def split_folder_display(display_name: str) -> tuple[str, ...]:
    """Hierarchy segments of a decoded folder name.

    The delimiter is the server's choice, not the client's (QQ uses "/", Outlook
    "."), so pick the first one actually present; a name with none is one segment.
    """
    delim = next((d for d in FOLDER_DELIMS if d in display_name), None)
    return tuple(display_name.split(delim)) if delim else (display_name,)


def split_folder_patterns(value: str | None) -> list[str]:
    """Comma-separated --folders/--exclude-folders value as trimmed patterns; empty items dropped."""
    if not value:
        return []
    return [p.strip().rstrip(FOLDER_DELIMS) for p in value.split(",") if p.strip()]


def folder_matches(display_name: str, raw_name: str, pattern: str) -> bool:
    """One --folders/--exclude-folders pattern against one folder.

    An exact decoded or raw IMAP name, or a proper segment prefix of the decoded
    name: "Archive" takes Archive plus everything nested under it, while "INBOX"
    misses INBOX/archive because the comparison stops at delimiter boundaries.
    """
    if pattern in (display_name, raw_name):
        return True
    pattern_segs = split_folder_display(pattern)
    folder_segs = split_folder_display(display_name)
    return len(pattern_segs) < len(folder_segs) and folder_segs[:len(pattern_segs)] == pattern_segs


def folder_listing(names: list[tuple[str, str, str, str]]) -> str:
    """Every folder as one markdown-style bullet per line, names sorted parents-first.

    Input must already be sorted so a parent precedes the folders nested under it.
    """
    return "\n".join(f"- {n[3]}" for n in names)


def get_names(server: imaplib.IMAP4, thunderbird: bool, nospinner: bool, quiet: bool,
              log: logging.Logger) -> list[tuple[str, str, str, str]]:
    spinner = Spinner("Finding Folders", nospinner, quiet=quiet)
    typ, data = server.list()
    if typ != 'OK':
        log.error("LIST failed: %s", data)
        raise RuntimeError(f"LIST failed: {data}")
    spinner.spin()
    entries: list[tuple[tuple[str, ...], tuple[str, str, str, str]]] = []
    for raw in data:
        if not isinstance(raw, bytes):
            continue
        row_str = str(raw, 'utf-8', 'replace')
        try:
            lst = parse_list(row_str)
        except (ValueError, IndexError):
            continue
        delim, foldername = lst[1], lst[2]
        if not isinstance(delim, str) or not isinstance(foldername, str):
            continue
        display_name = imap_utf7_decode(foldername)
        attrs = lst[0]
        if isinstance(attrs, list) and any(
                isinstance(attr, str) and attr.lower() == "\\noselect" for attr in attrs):
            # 分组容器（QQ「其他文件夹」）：本身没有邮件，不能 SELECT 备份
            log.info("Skipping non-selectable folder %s", display_name)
            continue
        if thunderbird:
            filename = '.sbd/'.join(display_name.split(delim))
            if filename.startswith("INBOX"):
                filename = filename.replace("INBOX", "Inbox")
        else:
            filename = '.'.join(display_name.split(delim)) + '.mbox'
        # key by segments so a parent always precedes its descendants
        entries.append((tuple(display_name.split(delim)),
                        (foldername, filename, folder_to_eml_relpath(display_name, delim), display_name)))
    spinner.stop()
    names = [entry for _, entry in sorted(entries, key=lambda e: e[0])]
    if not quiet:
        listing = folder_listing(names)
        log.info("Found %d folders:%s", len(names), "" if not listing else "\n" + listing)
    return names


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="imapbackup", description="Incremental IMAP backup (Py3.12)")
    add = p.add_argument
    add('-s','--server', required=True)
    add('-u','--user', required=True)
    add('-p','--pass', dest='password')
    add('-d','--mbox-dir', default=None, help='mbox output directory (default: "." unless --eml-dir is given)')
    add('--eml-dir', help='enable per-message .eml output under this directory')
    add('-a','--append-to-mboxes', action='store_true')
    add('-y','--yes-overwrite-mboxes', action='store_true')
    add('-f','--folders', help='comma-separated folder names to back up: an exact name or a path prefix, '
                               'so "Archive" takes Archive and every folder nested under it. '
                               'Chinese display names as printed after connecting work')
    add('--exclude-folders', help='comma-separated folder names to skip; same exact-or-prefix matching as --folders')
    add('-e','--ssl', action='store_true')
    add('-k','--keyfile')
    add('-c','--certfile')
    add('-t','--timeout', type=int, default=60)
    add('--thunderbird', action='store_true')
    add('--nospinner', action='store_true')
    add('--compact', action='store_true', help='hide per-message lines; show progress only in the status line')
    add('--icloud', action='store_true')
    add('--quiet', action='store_true')
    add('-v','--verbose', action='count', default=0, help='Increase verbosity (repeatable)')
    add('--version', action='version', version=f"imapbackup {__version__}",
        help='print the version and exit')
    return p


@dataclass
class Config:
    # 每字段一行：分号打包的单行多字段曾被个别语言服务器解析漏字段（如 overwrite），分写更稳也更可读
    overwrite: bool
    usessl: bool
    thunderbird: bool
    nospinner: bool
    basedir: Path | None
    icloud: bool
    quiet: bool
    user: str
    server: str
    password: str
    timeout: int = 60
    folders: str | None = None
    exclude_folders: str | None = None
    keyfilename: str | None = None
    certfilename: str | None = None
    port: int = field(default=0)
    verbose: int = 0
    eml_dir: Path | None = None
    compact: bool = False
    def parsed_folders(self) -> list[str]: return split_folder_patterns(self.folders)
    def parsed_excludes(self) -> list[str]: return split_folder_patterns(self.exclude_folders)


def parse_args_to_config(argv: list[str]) -> Config:
    args = build_arg_parser().parse_args(argv)
    overwrite = args.yes_overwrite_mboxes and not args.append_to_mboxes
    eml_dir = Path(os.path.expanduser(args.eml_dir)).resolve() if args.eml_dir else None
    if args.mbox_dir:
        basedir: Path | None = Path(os.path.expanduser(args.mbox_dir)).resolve()
    elif eml_dir is None:
        basedir = Path(".").resolve()
    else:
        basedir = None
    password = string_from_file(args.password) if args.password else getpass.getpass()
    server = args.server
    port = 993 if args.ssl else 143
    if ':' in server:
        host, p = server.split(':',1)
        server = host
        try:
            p_int = int(p)
            if not (0 < p_int < 65536):
                raise ValueError
            port = p_int
        except Exception as exc:
            raise SystemExit(f"Invalid port in --server: {p}") from exc
    if (args.keyfile and not args.certfile) or (args.certfile and not args.keyfile):
        raise SystemExit("Specify both --keyfile and --certfile or neither")
    if args.keyfile and not args.ssl:
        raise SystemExit("--keyfile requires --ssl")
    if args.certfile and not args.ssl:
        raise SystemExit("--certfile requires --ssl")
    if args.exclude_folders and args.folders:
        raise SystemExit("Cannot use both --folders and --exclude-folders")
    if args.timeout <= 0:
        raise SystemExit("--timeout must be > 0")
    return Config(overwrite, bool(args.ssl), bool(args.thunderbird), bool(args.nospinner),
                  basedir, bool(args.icloud), bool(args.quiet), args.user, server, password,
                  int(args.timeout), args.folders, args.exclude_folders, args.keyfile, args.certfile, port,
                  verbose=int(args.verbose), eml_dir=eml_dir, compact=bool(args.compact))


def configure_logging(cfg: Config) -> logging.Logger:
    level = (logging.WARNING if cfg.quiet and cfg.verbose == 0
             else logging.INFO if cfg.verbose == 0 else logging.DEBUG)
    # Root stays permissive so records are not dropped before the handlers see
    # them; the console level is applied to the StreamHandler itself. That is
    # what lets the run log keep INFO records while --quiet silences stderr.
    logging.basicConfig(level=logging.DEBUG,
                        format='%(asctime)s %(levelname)s %(message)s',
                        datefmt='%H:%M:%S')
    root = logging.getLogger()
    for handler in root.handlers:
        if isinstance(handler, logging.StreamHandler) and not isinstance(handler, logging.FileHandler):
            handler.setLevel(level)
    file_handler = make_log_file_handler()
    if file_handler is None:
        print(f"imapbackup: cannot write run log to {LOG_DIR}", file=sys.stderr)
    else:
        file_handler.setLevel(logging.INFO)
        file_handler.setFormatter(LOG_FORMATTER)
        root.addHandler(file_handler)
        runlog = logging.getLogger(RUN_LOG_LOGGER)
        runlog.setLevel(logging.INFO)
        runlog.propagate = False
        runlog.addHandler(file_handler)
    log = logging.getLogger('imapbackup')
    log.debug('Logger initialized (level=%s quiet=%s verbose=%s)', logging.getLevelName(level), cfg.quiet, cfg.verbose)
    return log


def get_config(argv: list[str] | None = None) -> Config:
    return parse_args_to_config(argv if argv is not None else sys.argv[1:])


def connect_and_login(cfg: Config, log: logging.Logger) -> imaplib.IMAP4:
    socket.setdefaulttimeout(cfg.timeout)
    try:
        if cfg.usessl:
            mode = "SSL (key/cert)" if (cfg.keyfilename and cfg.certfilename) else "SSL"
            log.info("Connecting to '%s' TCP %s, %s", cfg.server, cfg.port, mode)
            if cfg.keyfilename and cfg.certfilename:
                context = ssl.create_default_context()
                try:
                    context.load_cert_chain(certfile=cfg.certfilename, keyfile=cfg.keyfilename)
                except Exception as exc:
                    raise SystemExit(f"Failed loading certificate/key: {exc}") from exc
                server = imaplib.IMAP4_SSL(cfg.server, cfg.port, ssl_context=context)
            else:
                server = imaplib.IMAP4_SSL(cfg.server, cfg.port)
        else:
            log.info("Connecting to '%s' TCP %s", cfg.server, cfg.port)
            server = imaplib.IMAP4(cfg.server, cfg.port)
        with contextlib.suppress(OSError, AttributeError):
            server.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        log.info("Logging in as '%s'", cfg.user)
        server.login(cfg.user, cfg.password)
        return server
    except socket.gaierror as e:
        raise SystemExit(f"DNS lookup failed for '{cfg.server}': {e}") from e
    except OSError as e:
        raise SystemExit(f"Connection to '{cfg.server}' failed: {e}") from e
    except imaplib.IMAP4.error as e:
        raise SystemExit(f"IMAP authentication failed: {e}") from e


def connect_for_reconnect(cfg: Config, log: logging.Logger) -> imaplib.IMAP4:
    """Fresh connection for a reconnect attempt, with auth failures separated out.

    ``connect_and_login`` reports every failure as SystemExit; only DNS/connection
    failures are worth retrying, and a bad password must stop the budget at once
    instead of burning five attempts.
    """
    try:
        return connect_and_login(cfg, log)
    except SystemExit as exc:
        message = str(exc)
        if message.startswith("IMAP authentication failed"):
            raise ReconnectAuthError(message) from exc
        if message.startswith(("DNS lookup failed", "Connection to '")):
            raise ConnectionError(message) from exc
        raise


def _connection_error_reason(exc: BaseException) -> str | None:
    """Classify one exception as retryable connection loss (spec §5.7.3)."""
    if isinstance(exc, BackupWriteError):
        return None
    if isinstance(exc, imaplib.IMAP4.abort):
        return "abort"
    if isinstance(exc, OSError):
        return "oserror"
    if isinstance(exc, imaplib.IMAP4.error) and "bye" in str(exc).lower():
        return "bye"
    return None


def _close_connection(server: imaplib.IMAP4) -> None:
    with contextlib.suppress(Exception):
        server.shutdown()


def _wait_before_reconnect(delay: float, should_stop: Callable[[], bool] | None) -> bool:
    """Sleep in 0.2 s slices; True means the user asked to stop."""
    deadline = time.monotonic() + delay
    while True:
        if should_stop is not None and should_stop():
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(RECONNECT_STOP_POLL_S, remaining))


def run_with_reconnect[T](
    server: imaplib.IMAP4,
    connect: Callable[[], imaplib.IMAP4],
    operation: Callable[[imaplib.IMAP4], T],
    *,
    should_stop: Callable[[], bool] | None,
    log: logging.Logger,
    stats: ReconnectStats | None = None,
) -> tuple[T, imaplib.IMAP4]:
    """Run one operation, rebuilding the connection after a dropped link (spec §5.7).

    ``operation`` receives a usable connection and owns its SELECT, UIDVALIDITY
    check and resume. Only abort/OSError/BYE are retried; every other exception
    (write errors, folder failures, auth failures, stop) propagates unchanged.
    A successful operation returns the live connection so the caller can keep
    using it; the consecutive-failure budget belongs to this call, so the next
    operation starts fresh. Stop wins over the next retry and over exhaustion.
    """
    stats = stats if stats is not None else ReconnectStats()
    current = server
    failures = 0
    last_error: Exception | None = None
    while True:
        if should_stop is not None and should_stop():
            _close_connection(current)
            raise ReconnectStopped("stopped before the next request")
        try:
            if getattr(current, "state", None) == "LOGOUT":
                # 上一次调用把新连接也关掉后，调用方手里只剩已注销的旧连接；
                # 直接发命令只会得到不可重试的 IMAP4.error("illegal in state LOGOUT")
                raise imaplib.IMAP4.abort("connection is already closed")
            result = operation(current)
        except ReconnectStopped:
            _close_connection(current)
            raise
        except Exception as exc:
            reason = _connection_error_reason(exc)
            if reason is None:
                if current is not server:
                    _close_connection(current)   # do not leak a replacement link
                raise
            last_error = exc
            stats.last_reason = reason
            log.warning("Connection lost (%s); reconnecting", reason)
            _close_connection(current)
        else:
            if failures:
                log.info("Reconnected; resuming after %d failed attempt(s)", failures)
            return result, current

        while True:
            # 停止优先于耗尽：最后一跳失败后用户已点停止时，按停止收口而不是记一次失败
            if should_stop is not None and should_stop():
                raise ReconnectStopped("stopped before the next reconnect attempt")
            failures += 1
            if failures > RECONNECT_MAX_ATTEMPTS:
                stats.exhausted = True
                log.error("Reconnect exhausted after %d attempts (%s)",
                          RECONNECT_MAX_ATTEMPTS, stats.last_reason)
                assert last_error is not None
                raise last_error
            stats.attempts += 1
            cap = min(RECONNECT_BACKOFF_CAP_S, RECONNECT_BACKOFF_BASE_S * 4 ** (failures - 1))
            delay = random.uniform(0.0, cap)
            log.info("Reconnect attempt %d/%d in %.1fs (%s)", failures,
                     RECONNECT_MAX_ATTEMPTS, delay, stats.last_reason)
            if _wait_before_reconnect(delay, should_stop):
                raise ReconnectStopped("stopped while waiting to reconnect")
            try:
                fresh = connect()
            except ReconnectAuthError:
                raise
            except Exception as exc:
                reason = _connection_error_reason(exc)
                if reason is None:
                    raise
                last_error = exc
                stats.last_reason = reason
                log.warning("Reconnect attempt %d failed (%s)", failures, reason)
                continue
            if should_stop is not None and should_stop():
                _close_connection(fresh)
                raise ReconnectStopped("stopped after reconnecting")
            stats.successes += 1
            current = fresh
            break


def ensure_basedir(path: Path) -> None: path.mkdir(parents=True, exist_ok=True)


def create_folder_structure(names: Iterable[tuple[str, str, str, str]], basedir: Path, account: str = "") -> None:
    for _, filename, _, _ in sorted(names, key=lambda n: n[1]):
        folder = Path(f"{account}/{filename}" if account else filename).parent
        if folder and str(folder) != '.':
            (basedir / folder).mkdir(parents=True, exist_ok=True)


def main(argv: list[str] | None = None) -> int:
    cfg = get_config(argv)
    log = configure_logging(cfg)
    runlog = logging.getLogger(RUN_LOG_LOGGER)
    # First line of every run: the version, and a delimiter in the shared daily
    # log, which now holds both CLI and GUI runs.
    log.info("imapbackup v%s starting", __version__)
    scan_index: ScanIndex | None = None
    reconnect_stats = ReconnectStats()

    def reconnect() -> imaplib.IMAP4:
        return connect_for_reconnect(cfg, log)

    # Starts before connect_and_login so connection and authentication
    # failures reach the run log too; argparse failures still happen earlier,
    # before any handler exists (see ADR 0005).
    try:
        server = connect_and_login(cfg, log)
        names, server = run_with_reconnect(
            server, reconnect,
            lambda s: get_names(s, cfg.thunderbird, cfg.nospinner, quiet=cfg.quiet, log=log),
            should_stop=None, log=log, stats=reconnect_stats)
        # `--folders ""` must fail loudly, not be read as "no filter"
        include = cfg.parsed_folders() if cfg.folders is not None else None
        exclude = cfg.parsed_excludes() if cfg.exclude_folders else []
        # thunderbird output writes an "Inbox" folder, so map that spelling onto the server's INBOX
        if include is not None and cfg.thunderbird:
            include = [f.replace("Inbox", "INBOX", 1) if f.startswith("Inbox") else f for f in include]
        if include is not None:
            selected = [n for n in names if any(folder_matches(n[3], n[0], p) for p in include)]
            if not selected:
                raise SystemExit(f"No folder matches --folders '{cfg.folders}'. Available folders:\n{folder_listing(names)}")
            names = selected
        if exclude:
            unmatched = [p for p in exclude if not any(folder_matches(n[3], n[0], p) for n in names)]
            names = [n for n in names if not any(folder_matches(n[3], n[0], p) for p in exclude)]
            if unmatched:
                log.warning("No folder matches --exclude-folders '%s'", ", ".join(unmatched))
        account = sanitize_segment(cfg.user, EML_SEGMENT_MAX)
        scan_index = ScanIndex.open(log)
        if cfg.basedir is not None:
            ensure_basedir(cfg.basedir)
            create_folder_structure(names, cfg.basedir, account)
        total_folders = len(names)
        total_backed = total_skipped = total_fetch_failed = folder_failures = 0
        run_t0 = time.monotonic()

        def backup_folder(folder: str, filename: str, eml_relpath: str, display: str,
                          progress: DownloadProgress,
                          current: imaplib.IMAP4) -> tuple[imaplib.IMAP4, FolderScan, dict[str, tuple[int, int]]]:
            remote, live = run_with_reconnect(
                current, reconnect,
                lambda s: scan_folder(s, folder, cfg.nospinner, quiet=cfg.quiet, log=log, display=display),
                should_stop=None, log=log, stats=reconnect_stats)
            mbox_relpath = f"{account}/{filename}"
            eml_rel = f"{account}/{eml_relpath}"
            # per-format incremental: pending iff any selected format lacks the message;
            # download_messages then writes only the sinks that still lack it
            local_mbox, local_eml = local_snapshots(
                scan_index, cfg.basedir, cfg.eml_dir, account, display,
                mbox_relpath, eml_rel, overwrite=cfg.overwrite,
                nospinner=cfg.nospinner, quiet=cfg.quiet, log=log)
            eml_folder_dir = cfg.eml_dir / eml_rel if cfg.eml_dir is not None else None
            new_messages = pending_messages(remote.messages, local_mbox, local_eml)
            label = mbox_relpath if cfg.basedir is not None else eml_rel
            written: set[str] = set()

            def do_download(conn: imaplib.IMAP4, messages: dict[str, tuple[int, int]],
                            overwrite_now: bool) -> None:
                # a new writer per attempt: abort() closes the previous one
                index_mbox = (scan_index.writer("mbox", cfg.basedir, account, display,
                                                mbox_relpath, overwrite_now)
                              if scan_index is not None and cfg.basedir is not None else None)
                index_eml = (scan_index.writer("eml", cfg.eml_dir, account, display,
                                               eml_rel, overwrite_now)
                             if scan_index is not None and cfg.eml_dir is not None else None)
                download_messages(conn, label, messages, overwrite_now, cfg.nospinner,
                                  cfg.thunderbird, cfg.basedir, cfg.icloud, quiet=cfg.quiet, log=log,
                                  eml_dir=eml_folder_dir, skip_mbox=local_mbox, skip_eml=local_eml,
                                  foldername=display, verbose=cfg.verbose, compact=cfg.compact,
                                  written=written, progress=progress,
                                  index_mbox=index_mbox, index_eml=index_eml)

            def rescan(conn: imaplib.IMAP4) -> FolderScan:
                return scan_folder(conn, folder, cfg.nospinner, quiet=cfg.quiet, log=log, display=display)

            state = FolderDownloadState(remote, new_messages)
            live, state = download_folder_with_reconnect(
                live, reconnect, state, foldername=folder, display=display,
                local_mbox=local_mbox, local_eml=local_eml, written=written,
                download=do_download, rescan=rescan, should_stop=None, log=log,
                overwrite=cfg.overwrite, reuse_selection=True, stats=reconnect_stats)
            return live, state.scan, state.pending

        for folder_idx, (foldername, filename, eml_relpath, display_name) in enumerate(names, start=1):
            if not cfg.quiet:
                folder_separator(folder_idx, total_folders, display_name)
            progress = DownloadProgress()
            remote: FolderScan | None = None
            new_messages: dict[str, tuple[int, int]] = {}
            try:
                server, remote, new_messages = backup_folder(foldername, filename, eml_relpath,
                                                             display_name, progress, server)
            except SkipFolderException as e:
                folder_failures += 1
                if not cfg.quiet:
                    log.warning("%s: folder failed (scan): %s", display_name, e)
                continue
            except FolderFetchError as e:
                folder_failures += 1
                if not cfg.quiet:
                    log.warning("%s: folder failed (fetch): %s", display_name, e)
                continue
            finally:
                total_backed += progress.backed
                total_fetch_failed += progress.fetch_failed
                if remote is not None:
                    total_skipped += len(remote.messages) - len(new_messages)
        elapsed = int(time.monotonic() - run_t0)
        log.info("Account %s: %d backed, %d skipped, %d fetch failed, %d folder failure(s), elapsed %d:%02d",
                 account, total_backed, total_skipped, total_fetch_failed, folder_failures,
                 elapsed // 60, elapsed % 60)
        if not cfg.quiet:
            log.info("Disconnecting")
        with contextlib.suppress(imaplib.IMAP4.error):
            server.logout()
        return 0
    except KeyboardInterrupt:
        runlog.warning("Run interrupted by user")
        raise
    except SystemExit as exc:
        runlog.warning("Run aborted: %s", exc)
        raise
    except Exception:
        runlog.exception("Run failed")
        raise
    finally:
        if scan_index is not None:
            scan_index.close()


def cli_exception(typ, value, traceback):
    if not issubclass(typ, KeyboardInterrupt):
        sys.__excepthook__(typ, value, traceback)
    elif sys.stdout is not None:
        sys.stdout.write("\n")
        sys.stdout.flush()

if sys.stdin is not None and sys.stdin.isatty():  # pragma: no cover
    sys.excepthook = cli_exception

if __name__ == '__main__':
    sys.exit(main())
