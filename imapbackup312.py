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
import re
import shutil
import socket
import ssl
import sys
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


@dataclass
class FolderScan:
    """One folder's message scan: Message-Id -> (UID, RFC822.SIZE)."""

    messages: dict[str, tuple[int, int]]
    uidvalidity: int | None


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
SCAN_MSGID_FETCH_ITEMS = "(UID RFC822.SIZE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])"
SCAN_FALLBACK_FETCH_ITEMS = "(UID BODY.PEEK[HEADER.FIELDS (FROM TO CC DATE SUBJECT)])"
BODY_FETCH_ITEMS = "(UID INTERNALDATE BODY.PEEK[])"
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
    except OSError as exc:
        raise BackupWriteError(f"Failed writing message {msg_id} to {filename}: {exc}") from exc
    return _MessageOutput(timestamp, from_value, subject_value, size, wrote)


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
                      on_batch: Callable[[BatchStats], None] | None = None) -> DownloadProgress:
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
            if on_batch is not None:
                on_batch(stats)
            if progress.stopped:
                break
    finally:
        if mbox is not None:
            try:
                mbox.close()
            except OSError as exc:
                if sys.exc_info()[0] is None:
                    raise BackupWriteError(f"Failed closing {filename}: {exc}") from exc
        spinner.stop()
    if not quiet:
        log.info("%s: %s total, %s largest", filename, pretty_byte_count(output_bytes), pretty_byte_count(biggest))
        if progress.fetch_failed:
            log.warning("%s: %d message(s) could not be fetched", display, progress.fetch_failed)
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


def scan_eml_dir(folder_dir: Path, overwrite: bool, nospinner: bool, quiet: bool, log: logging.Logger) -> dict[str, str]:
    """Content-level scan mirroring scan_file(): recover Message-Ids from each .eml header block."""
    if overwrite:
        return {}
    if not folder_dir.is_dir():
        if not quiet:
            log.info("eml folder %s: not found", folder_dir)
        return {}
    spinner = Spinner(f"Scanning eml {folder_dir.name}", nospinner, quiet=quiet)
    messages: dict[str, str] = {}
    try:
        for path in sorted(folder_dir.rglob("*" + EML_SUFFIX)):
            try:
                with path.open("rb") as f:
                    head = f.read(65536)
            except OSError as exc:
                if not quiet:
                    log.warning("Cannot read %s: %s", path, exc)
                continue
            value = folded_header(head, "Message-Id")
            if value:
                messages.setdefault(value, value)
            else:
                if not quiet:
                    log.warning("%s has no Message-Id header", path)
            spinner.spin()
    finally:
        spinner.stop()
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
    # Starts before connect_and_login so connection and authentication
    # failures reach the run log too; argparse failures still happen earlier,
    # before any handler exists (see ADR 0005).
    try:
        server = connect_and_login(cfg, log)
        names = get_names(server, cfg.thunderbird, cfg.nospinner, quiet=cfg.quiet, log=log)
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
        if cfg.basedir is not None:
            ensure_basedir(cfg.basedir)
            create_folder_structure(names, cfg.basedir, account)
        total_folders = len(names)
        total_backed = total_skipped = total_fetch_failed = folder_failures = 0
        run_t0 = time.monotonic()
        for folder_idx, (foldername, filename, eml_relpath, display_name) in enumerate(names, start=1):
            if not cfg.quiet:
                folder_separator(folder_idx, total_folders, display_name)
            progress = DownloadProgress()
            remote: FolderScan | None = None
            new_messages: dict[str, tuple[int, int]] = {}
            try:
                remote = scan_folder(server, foldername, cfg.nospinner, quiet=cfg.quiet, log=log, display=display_name)
                # per-format incremental: pending iff any selected format lacks the message;
                # download_messages then writes only the sinks that still lack it
                local_mbox: dict[str, str] | None = None
                if cfg.basedir is not None:
                    local_mbox = scan_file(f"{account}/{filename}", cfg.overwrite, cfg.nospinner,
                                           cfg.basedir, quiet=cfg.quiet, log=log)
                local_eml: dict[str, str] | None = None
                eml_folder_dir = None
                if cfg.eml_dir is not None:
                    eml_folder_dir = cfg.eml_dir / account / eml_relpath
                    local_eml = scan_eml_dir(eml_folder_dir, cfg.overwrite, cfg.nospinner, quiet=cfg.quiet, log=log)
                new_messages = pending_messages(remote.messages, local_mbox, local_eml)
                label = f"{account}/{filename}" if cfg.basedir is not None else f"{account}/{eml_relpath}"
                download_messages(server, label, new_messages, cfg.overwrite, cfg.nospinner,
                                  cfg.thunderbird, cfg.basedir, cfg.icloud, quiet=cfg.quiet, log=log,
                                  eml_dir=eml_folder_dir, skip_mbox=local_mbox, skip_eml=local_eml,
                                  foldername=display_name, verbose=cfg.verbose, compact=cfg.compact,
                                  progress=progress)
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
