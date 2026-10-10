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
  --folders INBOX,Archive \
  --quiet
```

Key options (3.12 script):

* `--folders` and `--exclude-folders` are mutually exclusive. Each takes a comma-separated list of folder display names, matched either exactly or as a path prefix, so `Archive` also covers every folder nested under it (`INBOX` does not catch `INBOX/archive`). Chinese names as printed after connecting work as-is; a value that matches nothing exits with the account's folder list instead of backing up an empty selection.
* Full-message downloads always use read-only `BODY.PEEK[]`. `--icloud` remains accepted for command-line compatibility but no longer changes the fetch command.
* `--thunderbird` creates Thunderbird-style directory layout (.sbd folders instead of dotted names).
* `--eml-dir DIR` additionally writes each message as an individual `.eml` file (see Fork Notes below); combine with `--mbox-dir` to write both formats from a single fetch.
* `--yes-overwrite-mboxes` will delete existing local mbox files before writing (default is incremental append logic).
* `--nospinner` disables the TTY spinner (always disabled if stdout is not a TTY or in quiet mode).

To view all options:

```bash
python imapbackup312.py --help
```

## Quick Start — GUI (Python 3.12+)

```bash
python imapbackup_gui.py
```

A single-window front-end for people who would rather not use a terminal: manage your accounts (name, address, server, SSL, port, password) in the Settings page, pick one in the list on the left of the backup page and connect to load that account's folder tree (collapsible, with cascade-selectable checkboxes), choose the mbox/eml output format, then start the backup and watch each message appear in the table with live server/pending/backed counts. "备份所选" runs every checked account in list order (accounts are checked by default; the row checkboxes plus 全选/反选/清空 pick the set); each account keeps its own folders, message list and progress, and one failing account does not stop the others. Stop is cooperative (it finishes the current message), reruns are incremental, and the app data goes to `%LOCALAPPDATA%\imapbackup-gui\`: daily logs under `log\` plus per-user UI settings. Passwords are stored in the settings file encrypted with the Windows DPAPI (current user, current machine only), never in plain text. It runs the same backup code as the CLI above, so the two never drift apart. The interface text is Chinese.

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

## Fork Notes (imapbackup-gui)

This fork adds capabilities on top of upstream and ships as a per-user Windows installer for non-technical colleagues. The CLI remains fully usable on its own.

## What Changed in 1.6.0 (fork)

**Output formats and incremental scanning**

* Added `--eml-dir` for per-message `.eml` output (directory layout and file naming in ADR 0001).
* Backups now live under `<backup-root>/<account>/…` for both output formats — see the one-time migration notes below.
* IMAP modified UTF-7 folder names are decoded (e.g. `&UXZO1mWHTvZZOQ-` → `其他文件夹`) for display, mbox filenames, eml directories, and `--folders` / `--exclude-folders` matching.
* `--folders` and `--exclude-folders` match on a delimiter-aware segment boundary, so `系统文件夹` selects that folder and its whole subtree while `Archived` is not caught by the prefix `Archive`. Previously only an exact full path matched, which made a Chinese leaf name silently select nothing; a selection that still matches nothing now exits non-zero and prints the folder list.

**Terminal output**

* Right after connecting, the account's folders are listed as a bulleted list — one `- ` per line, parents before the folders nested under them — and the backup then runs in that same order, so the names can be read off the list and quoted in `--folders`. `--quiet` suppresses the listing.
* Added per-message progress lines with RFC 2047-decoded senders and subjects.
* The progress lines no longer clobber the spinner: a status line with a count progress bar, downloaded bytes and transfer rate stays pinned at the bottom of the terminal; `--compact` hides the scrolling lines and shows the latest subject in the status line.
* Each folder's sync starts with a full-width separator (e.g. `── [3/8] Sent Messages ──`) so it is easy to see where one folder ends and the next begins.
* Log level names are spelled out (`INFO` instead of `I`).
* Each run opens with a version banner (`imapbackup v1.6.0 starting`) so the version is visible before the first network attempt. `--quiet` keeps it out of the terminal but not out of the run log.

**Run log**

* The CLI now writes a daily run log to `%LOCALAPPDATA%\imapbackup-gui\log\run-YYYY-MM-DD.log` — the same file and format as the GUI (ADR 0005). Connection, folder enumeration, per-folder sync and disconnect events are recorded; failures, unexpected exceptions and user interrupts are recorded too. An unopenable log directory degrades to a warning instead of aborting the backup, so logging is never a precondition for the backup itself.
* Because the daily file holds both CLI and GUI runs, each run's version banner doubles as a delimiter marking where one run ends and the next begins.
* Added `--version` for reporting the version without starting a backup. It answers before any required flag is checked and before logging is configured, so it leaves no run log entry.

**GUI entry point and the core seams it relies on**

* Added `imapbackup_gui.py`, the Tkinter GUI entry point (see Quick Start — GUI above).
* The per-message `report` callback seam now passes a structured dict (folder, index, total, timestamp, From, Subject, size, Message-Id) instead of a preformatted line; the refactor itself leaves terminal output byte-identical (ADR 0003).
* `download_messages` gained an optional `should_stop` callback for cooperative cancellation between messages.
* Synthetic-ID fallback header fetch now uses `BODY.PEEK` (read-only invariant).
* Console-stream guards so a future `--windowed` build cannot crash on missing stdin/stdout.

**GUI settings and recovery contract**

* UI settings are global to the Windows user, not keyed by account, folder or backup root. The versioned settings file stores normal window geometry, maximized state, mail-list column widths, the close-button preference, the account list, and the (single, shared) backup root; it restores normal geometry before maximization, keeps the last normal geometry while maximized, and falls back to the default position if a saved position is entirely off the current displays.
* A missing, corrupt, incompatible or unwritable settings file must not block the GUI, connection or backup: recover with defaults or the last successfully saved values, keep the current process state, and report a diagnostic warning through the run log (or the existing console/status fallback when logging is unavailable). Settings written by an older build (version 1, single account) are migrated on read: the old account becomes the first entry of the account list and its backup folder becomes the shared backup root.

**Multiple accounts**

* Accounts are managed in the Settings page ("账户"): add, edit and delete them there, with the password encrypted through DPAPI (current user, current machine) — the file never holds plain-text credentials. The password box shows a fixed mask once a password is stored, never the secret or its length: saving it untouched keeps the stored password, clearing it removes the password, and typing anything else stores the new one. The "备份" category holds the one backup root shared by every account; each account's output stays under `<backup-root>/<email address>/`, so backups never mix (ADR 0002).
* The backup page is a two-pane layout: the account list on the left, the selected account's detail on the right. The detail shows the connection information and the backup root as a read-only card — no text fields — and the only editable controls are the mbox/eml format checkboxes and the folder selection tree, both stored per account.
* The account list scrolls on its own once the accounts no longer fit the pane; the scrollbar only appears then, and the mouse wheel moves it one row per notch.
* Each account keeps its own folder selection (defaults to INBOX for a new account), message list, counts, progress and status; switching accounts does not interrupt a connection or a run in progress.
* "备份所选" backs up every checked account; each row carries a checkbox and 全选/反选/清空 switch the whole set; a new or restored account starts checked, and the set is UI state that is not written to the settings file. A failing account is logged and reported without stopping the rest; Stop finishes the current message and stops all accounts.
* The account name is a display-only label: renaming it never moves existing backups or causes messages to be downloaded again.

**Connection folded into the backup run**

* There is no separate "connect" button any more. Folder lists load silently in the background (on startup, on switching accounts, and after saving an account), so the folder tree simply fills in on its own; a failure only writes to the run log.
* Starting a backup first opens a connection-check dialog that probes each account in turn (sign in, list folders, sign out) and shows per-account status. Only after every account checks out does the dialog close on its own and the accounts sync **concurrently** — each with its own IMAP connection.
* A wrong (or missing) password opens a password prompt: type the right one and retry that account, or skip it and carry on with the rest. A "save password" checkbox (checked by default) stores it encrypted; leaving it unchecked uses the password only for this run. Closing the check dialog cancels the whole backup.
* Run log lines from concurrent accounts carry an account-name prefix so interleaved output stays readable; the mail list shows only the selected account's messages and switches with the selection.

**Resizable sidebars and capped layout**

* The backup page's account list and the settings page's left bar can each be dragged at their right edge to resize (120–320 px, default 160); each remembers its own width across sessions.
* Card values and the account form no longer stretch to fill a maximized window: values cap at 620px and the form block at 640px, long values truncate with a hover tooltip, and the two upper cards line up on a shared bottom edge.

**Background backup and the tray**

* Closing the window no longer kills a running backup. The close button follows a three-way preference — ask me every time (default), hide to the tray and keep running, or quit the program — stored next to the other UI settings. The dialog has "hide to background" / "quit" plus a "remember my choice" checkbox; the Settings page can switch back to asking at any time, and it is the same stored option either way.
* The tray icon lives in the notification area for the whole session (starting with the program, removed only when it exits), so the app's presence and its right-click menu — show / stop backup / quit — are always reachable; it is also the way back in while the window is hidden. A tooltip and the greying of "stop backup" follow whether a run is in progress.
* When a run finishes, stops or fails while the window is hidden, the window stays hidden and only a tray balloon reports the result; clicking the balloon or the icon brings the results back. Quitting from the tray while a run is in progress asks for confirmation first.
* If the tray is unavailable (or the icon cannot be created), "hide to background" degrades to minimising to the taskbar so there is always a way back to the window.
* A second launch of an already-running (possibly hidden) instance now brings that instance's window to the front instead of silently exiting.
* The window has a native menu bar with "备份" (backup) and "设置" (settings) entries that switch pages in place; the Settings page uses the usual two-pane layout — a category list on the left, the settings of the selected category on the right.

**Windows installer**

* `packaging\build.bat` runs PyInstaller (GUI and CLI executables sharing one runtime directory) and then Inno Setup, producing `dist\installer\imapbackup-gui-setup-<version>.exe`. The wizard is in Chinese, installs per-user (no administrator), and creates Start-menu entries for both executables; the desktop icon is an optional task.
* The GUI is single-instance — a second launch brings the existing instance's window to the front instead of opening a second window; the installer and uninstaller detect a running instance through the same mutex.
* The uninstaller removes program files, shortcuts and registry entries by itself. When launched from Control Panel or Windows "Settings → Apps", it runs as a small wizard: the first page carries separate, pre-checked checkboxes for deleting run logs and UI settings under `%LOCALAPPDATA%\imapbackup-gui\` (the logs hold server addresses and account names, never passwords) — uncheck either one to keep it, or cancel the wizard to abort the uninstall entirely; a final dialog confirms completion. (The stock confirmation and completion message boxes are suppressed by launching the uninstaller silently through a rewritten `UninstallString`; running `unins000.exe` directly keeps them.) Silent uninstalls with plain `/SILENT` (automation) stay fully unattended and delete both by default. Backups are never touched. The CLI executable is `imapbackup-gui-cli.exe`; adding it to `PATH` is deliberately not done — it is reachable from the Start menu.

### One-time migration for backups made before the account layer

Incremental scanning reads the `Message-Id` header inside each message (not the path), so moving existing backups into the account directory is seamless — nothing gets re-downloaded. Modified UTF-7 folder names decode to real names now (e.g. `&UXZO1mWHTvZZOQ-` was 其他文件夹), so the old garbled directories can simply be deleted if empty:

```powershell
New-Item "D:\data\Downloads\mail_backup\your.name@example.com" -ItemType Directory
Move-Item "D:\data\Downloads\mail_backup\INBOX", "D:\data\Downloads\mail_backup\Sent Messages" "D:\data\Downloads\mail_backup\your.name@example.com\"
Remove-Item "D:\data\Downloads\mail_backup\&UXZO1mWHTvZZOQ-"
```

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

The version number lives in `imapbackup312.py` (`__version__`) and is duplicated verbatim in `imapbackup_gui.py`: the CLI and the GUI are one project and carry one version — currently 1.6.0. Optimizations stay on 1.6.x; swapping the GUI framework would move the project to 1.7.0 or 2.0.0. `imapbackup.py` and `imapbackup38.py` are frozen/transitional and still report `1.4h`, outside this numbering. Bump the two literals together, and only when intended — never as a side effect of a feature commit. The installer carries the same number: `packaging/imapbackup-gui.iss` (`#define MyAppVersion`, which also names the setup file and the registered `AppVersion`) and the completion message in `packaging/build.bat` must move in the same change.

The Windows installer is built from `packaging/` (see Fork Notes); release tags are cut when a version line wraps up.

## Contributing

I am accepting pull requests, but bear in mind that one of the original goals of this script was to run on _older_ Python versions, so as to save sysadmins stuck in the Dark Ages the trouble of installing a newer Python (much to my own amazement, this was originally written in Python 2.3).

I would be delighted to bring it fully up to date with Python 2.7.x/3.x, etc., and have considered adding multi-threading/multiprocessing to speed it up, but time is short enough as it is. If you feel up to the task just send me a pull request with a "new" main script with the target Python version as part of its name - something like `imapbackup27.py`, `imapbackup39.py`, etc.

If you want to contribute to the current modernized branch, focus on `imapbackup312.py` (e.g. structured logging, optional compression post‑processing, test harness, packaging). Please keep backwards compatibility for existing flags unless there is a compelling reason to change them.

## Disclaimer

For tradition's sake, here goes:

IN NO EVENT WILL I BE LIABLE FOR ANY DAMAGES WHATSOEVER (INCLUDING, WITHOUT LIMITATION, THOSE RESULTING FROM LOST PROFITS, LOST DATA, LOST REVENUE OR BUSINESS INTERRUPTION) ARISING OUT OF THE USE, INABILITY TO USE, OR THE RESULTS OF USE OF, THIS PROGRAM. WITHOUT LIMITING THE FOREGOING, I SHALL NOT BE LIABLE FOR ANY SPECIAL, INDIRECT, INCIDENTAL, OR CONSEQUENTIAL DAMAGES THAT MAY RESULT FROM THE USE OF THIS SCRIPT OR ANY PORTION THEREOF WHETHER ARISING UNDER CONTRACT, NEGLIGENCE, TORT OR ANY OTHER LAW OR CAUSE OF ACTION. I WILL ALSO PROVIDE NO SUPPORT WHATSOEVER, OTHER THAN ACCEPTING FIXES AND UPDATING THE SCRIPT AS IS DEEMED NECESSARY.

[tao]: http://taoofmac.com/space/projects/imapbackup
