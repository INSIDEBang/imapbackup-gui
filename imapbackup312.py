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
 - per-message progress lines; structured report callback and cooperative
   stop hook consumed by the Tkinter GUI (imapbackup_gui.py)

Original contributors (abridged): jwagnerhki, Bob Ippolito, Michael Leonhard,
Giuseppe Scrivano, Ronan Sheth, Brandon Long, Christian Schanz, A. Bovett,
Mark Feit, Marco Machicao, and Rui Carmo.
"""
from __future__ import annotations

__version__ = "1.7.0"
__author__ = "Rui Carmo (http://taoofmac.com)"
__copyright__ = "(C) 2006-2025 Rui Carmo. Code under MIT License.(C)"
__contributors__ = "jwagnerhki, Bob Ippolito, Michael Leonhard, Giuseppe Scrivano <gscrivano@gnu.org>, Ronan Sheth, Brandon Long, Christian Schanz, A. Bovett, Mark Feit, Marco Machicao"

import argparse
import base64
import email.utils
import ssl
import logging
import getpass
import hashlib
import imaplib
import mailbox
import os
import re
import shutil
import socket
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.header import decode_header
from functools import lru_cache
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple


class SkipFolderException(Exception):
    """Indicates aborting processing of current folder, continue with next folder."""


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
    out: List[str] = []
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
        if self.disabled: return
        self.message = text
        self._render(text)
    def erase(self) -> None:
        """Blank the status line so a report line can be printed above it."""
        if self.disabled: return
        sys.stdout.write("\r" + " " * self._width + "\r")
        sys.stdout.flush()
        self._width = 0
    def spin(self) -> None:
        if self.disabled: return
        self.pos = (self.pos + 1) % len(self.glyphs)
        self._render(self.message)
    def stop(self) -> None:
        if self.disabled: return
        self.erase()


def pretty_byte_count(num: int) -> str:
    if num == 1: return "1 byte"
    if num < 1024: return f"{num} bytes"
    if num < 1 << 20: return f"{num/1024.0:.2f} KB"
    if num < 1 << 30: return f"{num/1048576.0:.3f} MB"
    if num < 1 << 40: return f"{num/1073741824.0:.3f} GB"
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


def string_from_file(value: str) -> str:
    if not value or value[0] not in ("\\", "@"): return value
    if value[0] == "\\": return value[1:]
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


def build_eml_target(bucket_dir: Path, timestamp: datetime, msg_id: str, subject: Optional[str]) -> Path:
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


def parse_internaldate(value: str) -> Optional[datetime]:
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
    out: List[str] = []
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


@lru_cache(maxsize=None)
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
    out: List[str] = []
    i = 0
    while i < len(name):
        if name[i] != "&":
            out.append(name[i]); i += 1
            continue
        j = name.find("-", i + 1)
        if j < 0:
            out.append(name[i:]); break
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
                   quiet: bool, verbose: int, report: Optional[Callable[[dict], None]] = None,
                   erase: Optional[Callable[[], None]] = None) -> None:
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


def download_messages(server: imaplib.IMAP4, filename: str, messages: Dict[str, int],
                      overwrite: bool, nospinner: bool, thunderbird: bool,
                      basedir: Optional[Path], icloud: bool, quiet: bool, log: logging.Logger,
                      eml_dir: Optional[Path] = None, foldername: str = "",
                      verbose: int = 0, compact: bool = False,
                      report: Optional[Callable[[dict], None]] = None,
                      should_stop: Optional[Callable[[], bool]] = None) -> None:
    if basedir is not None:
        fullname = basedir / filename
        if overwrite and fullname.exists():
            if not quiet: log.info("Deleting mbox %s at %s", filename, fullname)
            fullname.unlink()
    if eml_dir is not None and overwrite and eml_dir.exists():
        if not quiet: log.info("Deleting eml folder %s", eml_dir)
        shutil.rmtree(eml_dir)
    if not messages:
        if not quiet: log.info("%s: New messages: 0", filename)
        return
    if basedir is not None:
        fullname.parent.mkdir(parents=True, exist_ok=True)
    count = len(messages)
    display = foldername or filename
    spinner = Spinner(f"Downloading {count} new messages to {display}", nospinner, quiet=quiet)
    biggest = 0
    done_bytes = 0
    t0 = time.monotonic()
    from_re = re.compile(br"\n(>*)From ")
    mbox = fullname.open("ab") if basedir is not None else None
    try:
        for idx, (msg_id, seq) in enumerate(messages.items(), start=1):
            if should_stop is not None and should_stop():
                if not quiet: log.info("%s: stopped by request after %d/%d messages", display, idx - 1, count)
                break
            fetch_cmd = "(INTERNALDATE BODY.PEEK[])" if icloud else "(INTERNALDATE RFC822)"
            typ, data = server.fetch(str(seq), fetch_cmd)
            if typ != 'OK':
                raise RuntimeError(f"FETCH failed for UID {seq}: {data}")
            if not data or not isinstance(data[0], tuple):
                raise RuntimeError(f"Malformed FETCH response for UID {seq}: {data}")
            fetch_meta = str(data[0][0], 'utf-8', 'replace')
            raw_bytes = data[0][1]
            timestamp = message_timestamp(fetch_meta, raw_bytes)
            head = raw_bytes[:8192]
            from_value = folded_header(head, "From")
            subject_value = folded_header(head, "Subject")
            size = 0
            if mbox is not None:
                buf = f"From nobody {time.ctime()}\n"
                if UUID in msg_id: buf += f"Message-Id: {msg_id}\n"
                mbox.write(buf.encode("utf-8"))
                text_bytes = raw_bytes.strip().replace(b"\r", b"")
                if thunderbird:
                    text_bytes = text_bytes.replace(b"\nFrom ", b"\n From ")
                else:
                    text_bytes = from_re.sub(b"\n>\\1From ", text_bytes)
                mbox.write(text_bytes + b"\n\n")
                size = len(text_bytes)
            if eml_dir is not None:
                payload = raw_bytes.strip() + b"\r\n"
                if UUID in msg_id and b"message-id:" not in head.lower():
                    payload = f"Message-Id: {msg_id}\r\n".encode("utf-8") + payload
                bucket_dir = eml_dir / timestamp.strftime("%Y-%m")
                target = unique_eml_path(build_eml_target(bucket_dir, timestamp, msg_id, decode_mime_words(subject_value)))
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                size = max(size, len(payload))
            if size > biggest: biggest = size
            if not compact:
                report_message(display, idx, count, timestamp, from_value, subject_value, size,
                               msg_id, quiet, verbose, report, erase=spinner.erase)
            done_bytes += size
            elapsed = time.monotonic() - t0
            rate = f" · {pretty_byte_count(int(done_bytes / elapsed))}/s" if elapsed >= 1.0 and done_bytes else ""
            status_text = (f"Downloading {display} [{progress_bar(idx, count)}] "
                           f"{idx}/{count} · {pretty_byte_count(done_bytes)}{rate}")
            if compact:
                status_text += f" | {_subject_label(subject_value, 40)}"
            spinner.status(status_text)
            spinner.spin()
    finally:
        if mbox is not None:
            mbox.close()
    spinner.stop()
    if not quiet:
        log.info("%s: %s total, %s largest", filename, pretty_byte_count(done_bytes), pretty_byte_count(biggest))


def scan_file(filename: str, overwrite: bool, nospinner: bool, basedir: Path, quiet: bool, log: logging.Logger) -> Dict[str, str]:
    if overwrite: return {}
    fullname = basedir / filename
    if not fullname.exists():
        if not quiet: log.info("File %s: not found", filename)
        return {}
    spinner = Spinner(f"File {filename}", nospinner, quiet=quiet)
    messages: Dict[str, str] = {}
    header = 'Message-Id'
    mbox = mailbox.mbox(fullname)
    try:
        for idx, msg in enumerate(mbox):
            raw_val = msg.get(header)
            if not raw_val:
                if not quiet: log.warning("Message #%d in %s has no %s header", idx, filename, header)
                spinner.spin(); continue
            line = f"{header}: {raw_val}".strip()
            line = BLANKS_RE.sub(' ', line)
            match = MSGID_RE.match(line)
            if match:
                messages.setdefault(match.group(1), match.group(1))
            else:
                if not quiet: log.warning("Message #%d in %s has malformed %s header", idx, filename, header)
            spinner.spin()
    finally:
        mbox.close()
    spinner.stop()
    if not quiet: log.info("%s: %d messages", filename, len(messages))
    return messages


def scan_eml_dir(folder_dir: Path, overwrite: bool, nospinner: bool, quiet: bool, log: logging.Logger) -> Dict[str, str]:
    """Content-level scan mirroring scan_file(): recover Message-Ids from each .eml header block."""
    if overwrite: return {}
    if not folder_dir.is_dir():
        if not quiet: log.info("eml folder %s: not found", folder_dir)
        return {}
    spinner = Spinner(f"Scanning eml {folder_dir.name}", nospinner, quiet=quiet)
    messages: Dict[str, str] = {}
    try:
        for path in sorted(folder_dir.rglob("*" + EML_SUFFIX)):
            try:
                with path.open("rb") as f: head = f.read(65536)
            except OSError as exc:
                if not quiet: log.warning("Cannot read %s: %s", path, exc)
                continue
            value = folded_header(head, "Message-Id")
            if value:
                messages.setdefault(value, value)
            else:
                if not quiet: log.warning("%s has no Message-Id header", path)
            spinner.spin()
    finally:
        spinner.stop()
    if not quiet: log.info("%s: %d messages", folder_dir, len(messages))
    return messages


def scan_folder(server: imaplib.IMAP4, foldername: str, nospinner: bool, quiet: bool, log: logging.Logger, display: Optional[str] = None) -> Dict[str, int]:
    messages: Dict[str, int] = {}
    shown = display or foldername
    quoted = f'"{foldername}"'
    spinner = Spinner(f'Folder "{shown}"', nospinner, quiet=quiet)
    try:
        typ, data = server.select(quoted, readonly=True)
        if typ != 'OK':
            raise SkipFolderException(f"SELECT failed: {data}")
        try:
            num_msgs = int(data[0])
        except (ValueError, TypeError):
            num_msgs = 0
        if num_msgs > 0:
            typ, data = server.fetch(f"1:{num_msgs}", '(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])')
            if typ != 'OK':
                raise SkipFolderException(f"FETCH failed: {data}")
            pairs = [t for t in data if isinstance(t, tuple)]
            for idx, tup in enumerate(pairs):
                normalized = BLANKS_RE.sub(' ', str(tup[1], 'utf-8', 'replace').strip())
                match = MSGID_RE.match(normalized)
                seq = idx + 1
                if match:
                    messages.setdefault(match.group(1), seq)
                else:
                    msg_typ, msg_data = server.fetch(str(seq), '(BODY.PEEK[HEADER.FIELDS (FROM TO CC DATE SUBJECT)])')
                    if msg_typ != 'OK':
                        raise SkipFolderException(f"HEADER FETCH {seq} failed: {msg_data}")
                    hdr_bytes = msg_data[0][1].replace(b'\r\n', b'\t')
                    synthetic = '<' + UUID + '.' + hashlib.sha1(hdr_bytes).hexdigest() + '>'
                    messages.setdefault(synthetic, seq)
                spinner.spin()
    finally:
        spinner.stop()
    if not quiet: log.info("%s: %d messages", shown, len(messages))
    return messages


def parse_paren_list(row: str):
    if not row or row[0] != '(': raise ValueError("Expected '('")
    row = row[1:]
    result: List[str | List[str]] = []
    name_attr_re = re.compile(r"^\s*(\\[a-zA-Z0-9_]+)\s*")
    while row and row[0] != ')':
        if row[0] == '(':
            sub, row = parse_paren_list(row); result.append(sub)
        else:
            m = name_attr_re.search(row)
            if not m: raise ValueError("Malformed attribute list")
            result.append(m.group(1)); row = row[m.end():]
    if not row or row[0] != ')': raise ValueError("Unterminated attribute list")
    return result, row[1:]


def parse_string_list(row: str) -> List[str]:
    slist = re.compile(r'\s*"([^"]+)"\s*|\s*(\S+)\s*').split(row)
    return [s for s in slist if s]


def parse_list(row: str) -> List[str | List[str]]:
    row = row.strip()
    paren_list, rest = parse_paren_list(row)
    string_list = parse_string_list(rest)
    if len(string_list) != 2: raise ValueError("Unexpected LIST response format")
    return [paren_list] + string_list


def folder_to_eml_relpath(foldername: str, delim: str) -> str:
    """Map an IMAP folder name to nested eml directories (one sanitized segment per hierarchy level)."""
    return "/".join(sanitize_segment(s, EML_SEGMENT_MAX) for s in foldername.split(delim))


def get_names(server: imaplib.IMAP4, thunderbird: bool, nospinner: bool, quiet: bool, log: logging.Logger) -> List[Tuple[str, str, str, str]]:
    spinner = Spinner("Finding Folders", nospinner, quiet=quiet)
    typ, data = server.list()
    if typ != 'OK':
        log.error("LIST failed: %s", data)
        raise RuntimeError(f"LIST failed: {data}")
    spinner.spin()
    names: List[Tuple[str, str, str, str]] = []
    for raw in data:
        row_str = str(raw, 'utf-8', 'replace')
        try:
            lst = parse_list(row_str)
        except (ValueError, IndexError):
            continue
        delim = lst[1]; foldername = lst[2]  # type: ignore[index]
        display_name = imap_utf7_decode(foldername)
        if thunderbird:
            filename = '.sbd/'.join(display_name.split(delim))
            if filename.startswith("INBOX"): filename = filename.replace("INBOX", "Inbox")
        else:
            filename = '.'.join(display_name.split(delim)) + '.mbox'
        names.append((foldername, filename, folder_to_eml_relpath(display_name, delim), display_name))
    spinner.stop()
    if not quiet: log.info("Found %d folders", len(names))
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
    add('-f','--folders')
    add('--exclude-folders')
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
    return p


@dataclass
class Config:
    overwrite: bool; usessl: bool; thunderbird: bool; nospinner: bool
    basedir: Optional[Path]; icloud: bool; quiet: bool; user: str; server: str; password: str
    timeout: int = 60; folders: Optional[str] = None; exclude_folders: Optional[str] = None
    keyfilename: Optional[str] = None; certfilename: Optional[str] = None; port: int = field(default=0)
    verbose: int = 0
    eml_dir: Optional[Path] = None
    compact: bool = False
    def parsed_folders(self) -> List[str]: return [f.strip() for f in self.folders.split(',')] if self.folders else []
    def parsed_excludes(self) -> List[str]: return [f.strip() for f in self.exclude_folders.split(',')] if self.exclude_folders else []


def parse_args_to_config(argv: List[str]) -> Config:
    args = build_arg_parser().parse_args(argv)
    overwrite = args.yes_overwrite_mboxes and not args.append_to_mboxes
    eml_dir = Path(os.path.expanduser(args.eml_dir)).resolve() if args.eml_dir else None
    if args.mbox_dir:
        basedir: Optional[Path] = Path(os.path.expanduser(args.mbox_dir)).resolve()
    elif eml_dir is None:
        basedir = Path(".").resolve()
    else:
        basedir = None
    password = string_from_file(args.password) if args.password else getpass.getpass()
    server = args.server; port = 993 if args.ssl else 143
    if ':' in server:
        host, p = server.split(':',1); server = host
        try:
            p_int = int(p)
            if not (0 < p_int < 65536):
                raise ValueError
            port = p_int
        except Exception as exc:
            raise SystemExit(f"Invalid port in --server: {p}") from exc
    if (args.keyfile and not args.certfile) or (args.certfile and not args.keyfile):
        raise SystemExit("Specify both --keyfile and --certfile or neither")
    if args.keyfile and not args.ssl: raise SystemExit("--keyfile requires --ssl")
    if args.certfile and not args.ssl: raise SystemExit("--certfile requires --ssl")
    if args.exclude_folders and args.folders: raise SystemExit("Cannot use both --folders and --exclude-folders")
    if args.timeout <= 0: raise SystemExit("--timeout must be > 0")
    return Config(overwrite, bool(args.ssl), bool(args.thunderbird), bool(args.nospinner),
                  basedir, bool(args.icloud), bool(args.quiet), args.user, server, password,
                  int(args.timeout), args.folders, args.exclude_folders, args.keyfile, args.certfile, port,
                  verbose=int(args.verbose), eml_dir=eml_dir, compact=bool(args.compact))


def configure_logging(cfg: Config) -> logging.Logger:
    if cfg.quiet and cfg.verbose == 0:
        level = logging.WARNING
    else:
        level = logging.INFO if cfg.verbose == 0 else logging.DEBUG
    logging.basicConfig(level=level,
                        format='%(asctime)s %(levelname)s %(message)s',
                        datefmt='%H:%M:%S')
    log = logging.getLogger('imapbackup')
    log.debug('Logger initialized (level=%s quiet=%s verbose=%s)', logging.getLevelName(level), cfg.quiet, cfg.verbose)
    return log


def get_config(argv: Optional[List[str]] = None) -> Config:
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
        try:
            server.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except (OSError, AttributeError):
            pass
        log.info("Logging in as '%s'", cfg.user)
        server.login(cfg.user, cfg.password)
        return server
    except socket.gaierror as e:
        raise SystemExit(f"DNS lookup failed for '{cfg.server}': {e}") from e
    except socket.error as e:
        raise SystemExit(f"Connection to '{cfg.server}' failed: {e}") from e
    except imaplib.IMAP4.error as e:
        raise SystemExit(f"IMAP authentication failed: {e}") from e


def ensure_basedir(path: Path) -> None: path.mkdir(parents=True, exist_ok=True)


def create_folder_structure(names: Iterable[Tuple[str, str, str, str]], basedir: Path, account: str = "") -> None:
    for _, filename, _, _ in sorted(names, key=lambda n: n[1]):
        folder = Path(f"{account}/{filename}" if account else filename).parent
        if folder and str(folder) != '.': (basedir / folder).mkdir(parents=True, exist_ok=True)


def main(argv: Optional[List[str]] = None) -> int:
    cfg = get_config(argv)
    log = configure_logging(cfg)
    server = connect_and_login(cfg, log)
    try:
        names = get_names(server, cfg.thunderbird, cfg.nospinner, quiet=cfg.quiet, log=log)
        include = set(cfg.parsed_folders()) if cfg.folders else None
        exclude = set(cfg.parsed_excludes()) if cfg.exclude_folders else set()
        if include is not None and cfg.thunderbird:
            assert isinstance(include, set)
            thunder_include: set[str] = set()
            for f in list(include):
                thunder_include.add(f.replace("Inbox","INBOX",1) if f.startswith("Inbox") else f)
            include = thunder_include
        if include is not None: names = [n for n in names if n[0] in include or n[3] in include]
        if exclude: names = [n for n in names if n[0] not in exclude and n[3] not in exclude]
        account = sanitize_segment(cfg.user, EML_SEGMENT_MAX)
        if cfg.basedir is not None:
            ensure_basedir(cfg.basedir); create_folder_structure(names, cfg.basedir, account)
        total_folders = len(names)
        for folder_idx, (foldername, filename, eml_relpath, display_name) in enumerate(names, start=1):
            if not cfg.quiet: folder_separator(folder_idx, total_folders, display_name)
            try:
                remote_msgs = scan_folder(server, foldername, cfg.nospinner, quiet=cfg.quiet, log=log, display=display_name)
                local_msgs: Dict[str, str] = {}
                if cfg.basedir is not None:
                    mbox_name = f"{account}/{filename}"
                    local_msgs.update(scan_file(mbox_name, cfg.overwrite, cfg.nospinner, cfg.basedir, quiet=cfg.quiet, log=log))
                eml_folder_dir = None
                if cfg.eml_dir is not None:
                    eml_folder_dir = cfg.eml_dir / account / eml_relpath
                    local_msgs.update(scan_eml_dir(eml_folder_dir, cfg.overwrite, cfg.nospinner, quiet=cfg.quiet, log=log))
                new_messages = {mid: remote_msgs[mid] for mid in remote_msgs if mid not in local_msgs}
                label = f"{account}/{filename}" if cfg.basedir is not None else f"{account}/{eml_relpath}"
                download_messages(server, label, new_messages, cfg.overwrite, cfg.nospinner,
                                  cfg.thunderbird, cfg.basedir, cfg.icloud, quiet=cfg.quiet, log=log,
                                  eml_dir=eml_folder_dir, foldername=display_name,
                                  verbose=cfg.verbose, compact=cfg.compact)
            except SkipFolderException as e:
                if not cfg.quiet: log.warning("%s", e); continue
        if not cfg.quiet: log.info("Disconnecting")
        try: server.logout()
        except imaplib.IMAP4.error: pass
        return 0
    finally:
        pass


def cli_exception(typ, value, traceback):
    if not issubclass(typ, KeyboardInterrupt):
        sys.__excepthook__(typ, value, traceback)
    elif sys.stdout is not None:
        sys.stdout.write("\n"); sys.stdout.flush()

if sys.stdin is not None and sys.stdin.isatty():  # pragma: no cover
    sys.excepthook = cli_exception

if __name__ == '__main__':
    sys.exit(main())
