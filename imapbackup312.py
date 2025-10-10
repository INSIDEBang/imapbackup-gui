#!/usr/bin/env python3
"""IMAP Incremental Backup Script (Python 3.12+ edition)

Modernized for Python 3.12:
 - argparse + dataclass config
 - pathlib usage
 - type hints
 - removed legacy socket monkey patch
 - quiet mode & safer error handling

Original contributors (abridged): jwagnerhki, Bob Ippolito, Michael Leonhard,
Giuseppe Scrivano, Ronan Sheth, Brandon Long, Christian Schanz, A. Bovett,
Mark Feit, Marco Machicao, and Rui Carmo.
"""
from __future__ import annotations

__version__ = "1.5.2"
__author__ = "Rui Carmo (http://taoofmac.com)"
__copyright__ = "(C) 2006-2025 Rui Carmo. Code under MIT License.(C)"
__contributors__ = "jwagnerhki, Bob Ippolito, Michael Leonhard, Giuseppe Scrivano <gscrivano@gnu.org>, Ronan Sheth, Brandon Long, Christian Schanz, A. Bovett, Mark Feit, Marco Machicao"

import argparse
import ssl
import logging
import getpass
import hashlib
import imaplib
import mailbox
import os
import re
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


class SkipFolderException(Exception):
    """Indicates aborting processing of current folder, continue with next folder."""


class Spinner:
    glyphs = "|/-\\"
    def __init__(self, message: str, disabled: bool, quiet: bool = False):
        self.message = message
        self.disabled = disabled or quiet or (not sys.stdin.isatty())
        self.pos = 0
        if not self.disabled:
            sys.stdout.write(message)
            sys.stdout.flush()
    def spin(self) -> None:
        if self.disabled: return
        sys.stdout.write(f"\r{self.message} {self.glyphs[self.pos]}")
        sys.stdout.flush()
        self.pos = (self.pos + 1) % len(self.glyphs)
    def stop(self) -> None:
        if self.disabled: return
        sys.stdout.write(f"\r{self.message}  \r{self.message}\n")
        sys.stdout.flush()


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


def string_from_file(value: str) -> str:
    if not value or value[0] not in ("\\", "@"): return value
    if value[0] == "\\": return value[1:]
    path = Path(os.path.expanduser(value[1:]))
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:  # pragma: no cover
        raise SystemExit(f"Unable to read password file '{path}': {exc}") from exc


def download_messages(server: imaplib.IMAP4, filename: str, messages: Dict[str, int],
                      overwrite: bool, nospinner: bool, thunderbird: bool,
                      basedir: Path, icloud: bool, quiet: bool, log: logging.Logger) -> None:
    fullname = basedir / filename
    if overwrite and fullname.exists():
        if not quiet: log.info("Deleting mbox %s at %s", filename, fullname)
        fullname.unlink()
    if not messages:
        if not quiet: log.info("%s: New messages: 0", filename)
        return
    fullname.parent.mkdir(parents=True, exist_ok=True)
    spinner = Spinner(f"Downloading {len(messages)} new messages to {filename}", nospinner, quiet=quiet)
    total = biggest = 0
    from_re = re.compile(br"\n(>*)From ")
    with fullname.open("ab") as mbox:
        for msg_id, seq in messages.items():
            buf = f"From nobody {time.ctime()}\n"
            if UUID in msg_id: buf += f"Message-Id: {msg_id}\n"
            mbox.write(buf.encode("utf-8"))
            if icloud:
                typ, data = server.fetch(str(seq), "(BODY.PEEK[])")
            else:
                typ, data = server.fetch(str(seq), "(RFC822)")
            if typ != 'OK':
                raise RuntimeError(f"FETCH failed for UID {seq}: {data}")
            raw_bytes = data[0][1]
            text_bytes = raw_bytes.strip().replace(b"\r", b"")
            if thunderbird:
                text_bytes = text_bytes.replace(b"\nFrom ", b"\n From ")
            else:
                text_bytes = from_re.sub(b"\n>\\1From ", text_bytes)
            mbox.write(text_bytes + b"\n\n")
            size = len(text_bytes)
            if size > biggest: biggest = size
            total += size
            spinner.spin()
    spinner.stop()
    if not quiet:
        log.info("%s: %s total, %s largest", filename, pretty_byte_count(total), pretty_byte_count(biggest))


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


def scan_folder(server: imaplib.IMAP4, foldername: str, nospinner: bool, quiet: bool, log: logging.Logger) -> Dict[str, int]:
    messages: Dict[str, int] = {}
    quoted = f'"{foldername}"'
    spinner = Spinner(f"Folder {quoted}", nospinner, quiet=quiet)
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
                    msg_typ, msg_data = server.fetch(str(seq), '(BODY[HEADER.FIELDS (FROM TO CC DATE SUBJECT)])')
                    if msg_typ != 'OK':
                        raise SkipFolderException(f"HEADER FETCH {seq} failed: {msg_data}")
                    hdr_bytes = msg_data[0][1].replace(b'\r\n', b'\t')
                    synthetic = '<' + UUID + '.' + hashlib.sha1(hdr_bytes).hexdigest() + '>'
                    messages.setdefault(synthetic, seq)
                spinner.spin()
    finally:
        spinner.stop()
    if not quiet: log.info("%s: %d messages", foldername, len(messages))
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


def get_names(server: imaplib.IMAP4, thunderbird: bool, nospinner: bool, quiet: bool, log: logging.Logger) -> List[Tuple[str, str]]:
    spinner = Spinner("Finding Folders", nospinner, quiet=quiet)
    typ, data = server.list()
    if typ != 'OK':
        log.error("LIST failed: %s", data)
        raise RuntimeError(f"LIST failed: {data}")
    spinner.spin()
    names: List[Tuple[str, str]] = []
    for raw in data:
        row_str = str(raw, 'utf-8', 'replace')
        try:
            lst = parse_list(row_str)
        except (ValueError, IndexError):
            continue
        delim = lst[1]; foldername = lst[2]  # type: ignore[index]
        if thunderbird:
            filename = '.sbd/'.join(foldername.split(delim))
            if filename.startswith("INBOX"): filename = filename.replace("INBOX", "Inbox")
        else:
            filename = '.'.join(foldername.split(delim)) + '.mbox'
        names.append((foldername, filename))
    spinner.stop()
    if not quiet: log.info("Found %d folders", len(names))
    return names


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="imapbackup", description="Incremental IMAP backup (Py3.12)")
    add = p.add_argument
    add('-s','--server', required=True)
    add('-u','--user', required=True)
    add('-p','--pass', dest='password')
    add('-d','--mbox-dir', default='.')
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
    add('--icloud', action='store_true')
    add('--quiet', action='store_true')
    add('-v','--verbose', action='count', default=0, help='Increase verbosity (repeatable)')
    return p


@dataclass
class Config:
    overwrite: bool; usessl: bool; thunderbird: bool; nospinner: bool
    basedir: Path; icloud: bool; quiet: bool; user: str; server: str; password: str
    timeout: int = 60; folders: Optional[str] = None; exclude_folders: Optional[str] = None
    keyfilename: Optional[str] = None; certfilename: Optional[str] = None; port: int = field(default=0)
    verbose: int = 0
    def parsed_folders(self) -> List[str]: return [f.strip() for f in self.folders.split(',')] if self.folders else []
    def parsed_excludes(self) -> List[str]: return [f.strip() for f in self.exclude_folders.split(',')] if self.exclude_folders else []


def parse_args_to_config(argv: List[str]) -> Config:
    args = build_arg_parser().parse_args(argv)
    overwrite = args.yes_overwrite_mboxes and not args.append_to_mboxes
    basedir = Path(os.path.expanduser(args.mbox_dir)).resolve()
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
                  verbose=int(args.verbose))


def configure_logging(cfg: Config) -> logging.Logger:
    if cfg.quiet and cfg.verbose == 0:
        level = logging.WARNING
    else:
        level = logging.INFO if cfg.verbose == 0 else logging.DEBUG
    logging.basicConfig(level=level,
                        format='%(asctime)s %(levelname).1s %(message)s',
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


def create_folder_structure(names: Iterable[Tuple[str,str]], basedir: Path) -> None:
    for _, filename in sorted(names):
        folder = Path(filename).parent
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
        if include is not None: names = [n for n in names if n[0] in include]
        if exclude: names = [n for n in names if n[0] not in exclude]
        ensure_basedir(cfg.basedir); create_folder_structure(names, cfg.basedir)
        for foldername, filename in names:
            if foldername in exclude:
                if not cfg.quiet: log.info("Excluding folder '%s'", foldername); continue
            try:
                remote_msgs = scan_folder(server, foldername, cfg.nospinner, quiet=cfg.quiet, log=log)
                local_msgs = scan_file(filename, cfg.overwrite, cfg.nospinner, cfg.basedir, quiet=cfg.quiet, log=log)
                new_messages = {mid: remote_msgs[mid] for mid in remote_msgs if mid not in local_msgs}
                download_messages(server, filename, new_messages, cfg.overwrite, cfg.nospinner,
                                  cfg.thunderbird, cfg.basedir, cfg.icloud, quiet=cfg.quiet, log=log)
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
    else:
        sys.stdout.write("\n"); sys.stdout.flush()

if sys.stdin.isatty():  # pragma: no cover
    sys.excepthook = cli_exception

if __name__ == '__main__':
    sys.exit(main())
