[![Language grade: Python](https://img.shields.io/lgtm/grade/python/g/rcarmo/imapbackup.svg?logo=lgtm&logoWidth=18)](https://lgtm.com/projects/g/rcarmo/imapbackup/context:python)

# imapbackup

A Python script for creating full backups of IMAP mailboxes

## Background

This was first published around 2007 (probably earlier) [on my personal site][tao], and it was originally developed to work around the then rather limited (ok, inconsistent) Mac OS X Mail.app functionality and allow me to back up my old mailboxes in a fully standard `mbox` format (well, at least as much as `mbox` can be considered a standard...).

Somewhat to my surprise it was considered useful by quite a few people throughout the years, and contributions started coming in. Given that there seems to be renewed interest in this as a systems administration tool, I'm posting the source code here and re-licensing it under the MIT license.

## Features

* ZERO dependencies.
* Copies every single message from every single folder (or a subset of folders) in your IMAP server to your disk.
* Does _incremental_ copying (i.e., tries very hard to not copy messages twice).
* Tries to do everything as safely as possible (only performs read operations on IMAP).
* Generates `mbox` formatted files that can be imported into Mail.app (just choose "Other" on the import dialog).
* Optionally compresses the result files on the fly (and can append to them). Only available in Python 2.x
* Is completely and utterly free (distributed under the MIT license).

## Requirements / Versions

| Script | Target Python | Status | Notes |
|--------|---------------|--------|-------|
| `imapbackup.py` | 2.5+ (Python 2.x) | Legacy/Frozen | Includes optional gzip compression logic. No longer updated. |
| `imapbackup38.py` | 3.8+ | Maintenance | Transitional Python 3 port retaining original structure. |
| `imapbackup312.py` | 3.12+ | Experimental | Modernized: structured configuration, improved error handling, quiet mode support, enhanced SSL handling. |

The new Python 3.12+ script drops on‑the‑fly compression (you can compress the resulting `mbox` files afterward with your tool of choice) and focuses on clean, dependency‑free operation using only the standard library.

## Quick Start (Python 3.12+)

```bash
python imapbackup312.py \
  --server imap.example.com:993 \
  --ssl \
  --user your.name@example.com \
  --pass @/path/to/secret.txt \
  --mbox-dir ./mail_backup \
  --folders INBOX,Sent,Archive \
  --quiet
```

Key options (3.12 script):

* `--folders` and `--exclude-folders` are mutually exclusive.
* `--icloud` forces BODY.PEEK fetches (works around certain providers that otherwise re-flag messages).
* `--thunderbird` creates Thunderbird-style directory layout (.sbd folders instead of dotted names).
* `--yes-overwrite-mboxes` will delete existing local mbox files before writing (default is incremental append logic).
* `--nospinner` disables the TTY spinner (always disabled if stdout is not a TTY or in quiet mode).

To view all options:

```bash
python imapbackup312.py --help
```

## What Changed in 1.5.2 (Experimental Python 3.12 modernization)

* Replaced legacy option parsing with `argparse` (adds `--help`, clearer errors).
* Introduced a dataclass-based configuration object for clarity and future extensibility.
* Switched to `pathlib` for path handling.
* Added `--quiet` mode to suppress non-essential progress output.
* Added robust SSL context creation; if both `--keyfile` and `--certfile` are supplied they are loaded explicitly.
* Removed obsolete socket monkey patch and Python 2 compatibility code.
* Normalized message download logic; added safer handling of iCloud servers via `--icloud` (uses `BODY.PEEK[]`).
* Improved folder include/exclude handling and Thunderbird naming translation.
* Added graceful KeyboardInterrupt handling (prints a clean newline instead of a traceback).
* Cleaned up and tightened error messages with consistent `SystemExit` usage and exception chaining.

## Migrating From Older Scripts

* Command-line flags are largely compatible; the biggest difference is the explicit `--mbox-dir` defaulting to the current directory.
* The incremental logic (scanning local mbox files for existing `Message-Id`) is unchanged in behavior.
* Compression is no longer built in—run `gzip` or `xz` afterward if desired.
* For Thunderbird layouts, remember to include `--thunderbird` and manually create/point to an existing profile import location if needed.

## Integrity & Safety Notes

* The script only uses read-only IMAP operations (`SELECT` in read-only mode, `FETCH` with `BODY.PEEK` when relevant).
* Synthetic `Message-Id` values are generated (with a stable UUID salt) for messages lacking a `Message-Id` header, preserving incremental behavior.
* Always test with a small subset of folders first using `--folders` before running a full backup.

## Versioning

The version number is embedded in `imapbackup312.py` (`__version__`). Tagging or packaging (e.g. as a `pipx` tool) is a possible future enhancement.

## Contributing

I am accepting pull requests, but bear in mind that one of the original goals of this script was to run on _older_ Python versions, so as to save sysadmins stuck in the Dark Ages the trouble of installing a newer Python (much to my own amazement, this was originally written in Python 2.3).

I would be delighted to bring it fully up to date with Python 2.7.x/3.x, etc., and have considered adding multi-threading/multiprocessing to speed it up, but time is short enough as it is. If you feel up to the task just send me a pull request with a "new" main script with the target Python version as part of its name - something like `imapbackup27.py`, `imapbackup39.py`, etc.

If you want to contribute to the current modernized branch, focus on `imapbackup312.py` (e.g. structured logging, optional compression post‑processing, test harness, packaging). Please keep backwards compatibility for existing flags unless there is a compelling reason to change them.

## Disclaimer

For tradition's sake, here goes:

IN NO EVENT WILL I BE LIABLE FOR ANY DAMAGES WHATSOEVER (INCLUDING, WITHOUT LIMITATION, THOSE RESULTING FROM LOST PROFITS, LOST DATA, LOST REVENUE OR BUSINESS INTERRUPTION) ARISING OUT OF THE USE, INABILITY TO USE, OR THE RESULTS OF USE OF, THIS PROGRAM. WITHOUT LIMITING THE FOREGOING, I SHALL NOT BE LIABLE FOR ANY SPECIAL, INDIRECT, INCIDENTAL, OR CONSEQUENTIAL DAMAGES THAT MAY RESULT FROM THE USE OF THIS SCRIPT OR ANY PORTION THEREOF WHETHER ARISING UNDER CONTRACT, NEGLIGENCE, TORT OR ANY OTHER LAW OR CAUSE OF ACTION. I WILL ALSO PROVIDE NO SUPPORT WHATSOEVER, OTHER THAN ACCEPTING FIXES AND UPDATING THE SCRIPT AS IS DEEMED NECESSARY.

[tao]: http://taoofmac.com/space/projects/imapbackup
