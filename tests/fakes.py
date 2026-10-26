"""测试用的假 IMAP 服务器与替身。

全部是进程内的 imaplib.IMAP4 替身：不起 socket、不联网、不需要凭证。每个类只实现
被测代码真正调用的方法，返回值照抄 imaplib 的形状（`("OK", [bytes, ...])`）。

这里集中放这些替身，是因为它们在 .scratch/ 的一次性脚本里被逐字复制过 7 份
（4 种 FakeServer 形态、3 份 NullLog、2 份 modified UTF-7 编码器）；迁移后只留一份。
"""
import base64
import imaplib
import logging
import re
from builtins import list as _list  # 类内定义 list 方法会遮蔽内置名，注解里用 _list（同类型桩）
from logging import NullHandler

DEFAULT_INTERNALDATE = "12-May-2024 10:15:30 +0800"
MSGID_RE = re.compile(br"^Message-Id:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


class NullLog(logging.Logger):
    """吃掉全部记录的 Logger，替身 console logger。

    scan_file / scan_eml_dir / download_messages / get_names 都要一个 logger 参数。
    用真 Logger 而不是手写 stub，属性访问才是诚实的；NullHandler 保证没有一条记录
    漏到 stderr 或当日运行日志里去。
    """
    def __init__(self):
        super().__init__("test.null")
        self.propagate = False
        self.addHandler(NullHandler())


def imap_utf7_encode(text: str) -> str:
    """modified UTF-7 (RFC 3501) 编码器，用来构造服务器响应。

    imapbackup312 只有解码器（imap_utf7_decode），测试要构造中文文件夹名的服务器
    响应就得自己编码一份。
    """
    out, buf = [], ""

    def flush(b: str) -> str:
        if not b:
            return ""
        return "&" + base64.b64encode(b.encode("utf-16-be")).decode().replace("/", ",").rstrip("=") + "-"

    for ch in text:
        if ch.isascii() and 33 <= ord(ch) < 127:
            out.append(flush(buf))
            out.append(ch)
            buf = ""
        else:
            buf += ch
    out.append(flush(buf))
    return "".join(out)


def fallback_literal(uid: int) -> bytes:
    """缺 Message-Id 时假服务器返回的 FROM TO CC DATE SUBJECT 头。

    内容只依赖 UID，测试要重算合成消息标识时可直接调用。
    """
    return (f"From: nobody{uid}@example.com\r\nTo: someone@example.com\r\n"
            f"Date: Sun, 12 May 2024 02:15:30 +0000\r\nSubject: no id {uid}\r\n\r\n").encode()


class UidFetchServer(imaplib.IMAP4):
    """按 UID 应答 SEARCH / FETCH 的假服务器，覆盖扫描与批量取信。

    raws 是 {uid: 完整 RFC822 字节}；sizes 可覆盖元数据里的 RFC822.SIZE（测试超大/零字节
    消息时不必真的分配 9 MiB）。所有注入项默认关闭：

    - missing_uids：批次响应里不返回该 UID；单封重试也不返回。
    - malformed_uids：返回带 UID 但缺字面量的元组；单封重试也一样。
    - unattributed_uids：返回缺 UID 的元组（只能按畸形整页处理）。
    - no_bad_uids：含该 UID 的批次一律 NO；单封请求也对它回 NO。
    - no_bad_single_uids：仅单封请求回 NO（配合 missing_uids 验证重试桶优先）。
    - no_bad_batch_times：前 N 次「多封批次」请求回 NO（逐封隔离时恢复正常）。
    - fail_search_times / fail_header_times / fail_body_times：对应命令前 N 次回 NO。
    - duplicate_uids：响应里重复返回该 UID（验证第一份生效）。
    - reverse_responses：故意逆序返回，验证按请求顺序处理。
    - body_hook：正文请求读取后、应答前调用 body_hook(self, uids)，可用来模拟停止。
    - raise_body：正文请求直接抛出该异常（abort/OSError 传播）。
    - select_ok=False / uidvalidity=None：SELECT 失败、缺 UIDVALIDITY。
    """
    def __init__(self, raws, *, sizes=None, uidvalidity: int | str | None = 777, select_ok=True,
                 missing_uids=(), malformed_uids=(), unattributed_uids=(),
                 no_bad_uids=(), no_bad_single_uids=(), no_bad_folders=(), no_bad_batch_times=0,
                 fail_search_times=0, fail_header_times=0, fail_body_times=0,
                 duplicate_uids=(), folders=("INBOX",), select_fail_folders=(),
                 reverse_responses=False, body_hook=None, raise_body=None,
                 internaldate=DEFAULT_INTERNALDATE):
        self.raws = dict(raws)
        self.sizes = dict(sizes or {})
        self.uidvalidity = uidvalidity
        self.select_ok = select_ok
        self.missing_uids = set(missing_uids)
        self.malformed_uids = set(malformed_uids)
        self.unattributed_uids = set(unattributed_uids)
        self.no_bad_uids = set(no_bad_uids)
        self.no_bad_single_uids = set(no_bad_single_uids)
        self.no_bad_folders = set(no_bad_folders)
        self.no_bad_batch_times = no_bad_batch_times
        self.fail_search_times = fail_search_times
        self.fail_header_times = fail_header_times
        self.fail_body_times = fail_body_times
        self.duplicate_uids = set(duplicate_uids)
        self.folders = list(folders)
        self.select_fail_folders = set(select_fail_folders)
        self.reverse_responses = reverse_responses
        self.body_hook = body_hook
        self.raise_body = raise_body
        self.internaldate = internaldate
        self.current_mailbox: str | None = None
        self.untagged_responses: dict[str, _list] = {}
        self.selects: list[str] = []
        self.uid_calls: list[tuple[str, _list]] = []
        self.fetches: list[tuple[str, str]] = []        # (uid set, items)
        self.body_fetches: list[list[int]] = []
        self.header_fetches: list[list[int]] = []
        self.fallback_fetches: list[list[int]] = []
        self.max_in_flight = 0
        self._in_flight = 0

    def size_of(self, uid: int) -> int:
        return self.sizes.get(uid, len(self.raws[uid]))

    def list(self, directory='""', pattern="*") -> tuple[str, _list]:
        rows = [f'(\\HasNoChildren) "/" "{imap_utf7_encode(name)}"' for name in self.folders]
        return "OK", [row.encode("utf-8") for row in rows]

    def select(self, mailbox="INBOX", readonly=False) -> tuple[str, _list]:
        self.selects.append(mailbox)
        self.current_mailbox = mailbox.strip('"')
        if not self.select_ok or self.current_mailbox in self.select_fail_folders:
            return "NO", [b"select failed"]
        self.untagged_responses = {}
        if self.uidvalidity is not None:
            self.untagged_responses["UIDVALIDITY"] = [str(self.uidvalidity).encode()]
        return "OK", [str(len(self.raws)).encode()]

    def uid(self, command, *args) -> tuple[str, _list]:
        self.uid_calls.append((command, list(args)))
        if command == "SEARCH":
            if self.fail_search_times:
                self.fail_search_times -= 1
                return "NO", [b"search failed"]
            return "OK", [" ".join(str(uid) for uid in sorted(self.raws)).encode()]
        if command != "FETCH":
            return "BAD", [b"unsupported command"]
        uidset, items = args
        uids = [int(part) for part in uidset.split(",") if part]
        self.fetches.append((uidset, items))
        body = "BODY.PEEK[]" in items
        (self.body_fetches if body else self.header_fetches).append(uids)
        if not body:
            if "MESSAGE-ID" not in items.upper():
                self.fallback_fetches.append(uids)
            if self.fail_header_times:
                self.fail_header_times -= 1
                return "NO", [b"header failed"]
        else:
            if self.current_mailbox in self.no_bad_folders:
                return "NO", [b"folder rejected"]
            if self.raise_body is not None:
                raise self.raise_body
            if self.fail_body_times:
                self.fail_body_times -= 1
                return "NO", [b"body failed"]
            if self.body_hook is not None:
                self.body_hook(self, uids)
        if len(uids) > 1 and self.no_bad_batch_times:
            self.no_bad_batch_times -= 1
            return "NO", [b"batch rejected"]
        if self.no_bad_uids & set(uids):
            return "NO", [b"uid rejected"]
        if len(uids) == 1 and self.no_bad_single_uids & set(uids):
            return "NO", [b"single uid rejected"]
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            return "OK", self._responses(uids, items, body)
        finally:
            self._in_flight -= 1

    def _responses(self, uids: _list[int], items: str, body: bool) -> _list:
        data: _list = []
        for uid in uids:
            if uid in self.missing_uids or uid not in self.raws:
                continue
            if uid in self.unattributed_uids:
                literal = self._literal(uid, items, body)
                data.append((f'{self.size_of(uid)} (RFC822.SIZE {len(literal)}'.encode(), literal))
                continue
            literal = None if uid in self.malformed_uids else self._literal(uid, items, body)
            meta = self._meta(uid, items, body, literal)
            data.append((meta, literal))
            if uid in self.duplicate_uids:
                data.append((meta, literal))
        if self.reverse_responses:
            data.reverse()
        data.append(b")")
        return data

    def _literal(self, uid: int, items: str, body: bool) -> bytes:
        raw = self.raws[uid]
        if body:
            return raw
        if "MESSAGE-ID" in items.upper():
            match = MSGID_RE.search(raw.split(b"\r\n\r\n", 1)[0])
            if match is not None:
                return b"Message-Id: " + match.group(1) + b"\r\n"
        return fallback_literal(uid)

    def _meta(self, uid: int, items: str, body: bool, literal: bytes | None) -> bytes:
        size = self.size_of(uid)
        length = len(literal or b"")
        if body:
            return (f'{uid} (UID {uid} INTERNALDATE "{self.internaldate}" '
                    f'RFC822.SIZE {size} BODY[] {{{length}}}').encode()
        if "MESSAGE-ID" in items.upper():
            return f'{uid} (UID {uid} RFC822.SIZE {size} BODY[HEADER.FIELDS (MESSAGE-ID)] {{{length}}}'.encode()
        return (f'{uid} (UID {uid} RFC822.SIZE {size} '
                f'BODY[HEADER.FIELDS (FROM TO CC DATE SUBJECT)] {{{length}}}').encode()

    def logout(self) -> tuple[str, _list]:
        return "OK", [b"bye"]


class ListServer(UidFetchServer):
    """只有 LIST 的服务器：枚举文件夹用的最小替身。

    display_names 是已解码的显示名，list() 会编码回 modified UTF-7 再返回，
    和真实服务器一致。delim 控制层级分隔符（"." 用于 Outlook 风格账号）。
    文件夹都是空的，所以 UID SEARCH 回空列表、不会发任何 FETCH。
    """
    def __init__(self, display_names, delim="/"):
        super().__init__({})
        self.display_names = list(display_names)
        self.delim = delim

    def list(self, directory='""', pattern="*") -> tuple[str, _list]:
        rows = [f'(\\HasNoChildren) "{self.delim}" "{imap_utf7_encode(n)}"' for n in self.display_names]
        return "OK", [r.encode("utf-8") for r in rows]


class MainServer(UidFetchServer):
    """能驱动 main() 跑完全流程的服务器：LIST / SELECT / UID SEARCH / UID FETCH / LOGOUT。

    fail_fetch / interrupt_fetch 只注入到正文取信，验证 main() 的顶层捕获。
    """
    def __init__(self, messages=0, fail_fetch=None, interrupt_fetch=None, **kwargs):
        raws = {uid: (f"From: sender{uid}@example.com\r\nSubject: message {uid}\r\n"
                      f"Message-Id: <msg{uid}@x>\r\n\r\ntest body {uid}\r\n").encode()
                for uid in range(1, messages + 1)}
        super().__init__(raws, **kwargs)
        self.fail_fetch = fail_fetch
        self.interrupt_fetch = interrupt_fetch

    def uid(self, command, *args) -> tuple[str, _list]:
        if (command == "FETCH" and args and "BODY.PEEK[]" in args[-1]
                and not self.malformed_uids):
            if self.interrupt_fetch:
                raise KeyboardInterrupt
            if self.fail_fetch:
                raise RuntimeError(f"FETCH failed for UID {args[0]}: {self.fail_fetch}")
        return super().uid(command, *args)


def recording_spinner(target):
    """返回一个把每次调用记进 target 列表的 Spinner 替身类。

    download_messages 内部自己 new Spinner，所以只能替换类而不是传实例。
    target 收 (事件名, 参数) 元组：("init", message) / ("status", text) /
    ("spin",) / ("erase",) / ("stop",)。

    __init__ 的签名照抄真 Spinner：调用方传 quiet=，少一个就会 TypeError。
    """
    class FakeSpinner:
        def __init__(self, message, disabled, quiet=False):
            target.append(("init", message))
            self.disabled = False
            self.message = message

        def status(self, text):
            target.append(("status", text))

        def spin(self):
            target.append(("spin",))

        def erase(self):
            target.append(("erase",))

        def stop(self):
            target.append(("stop",))

    return FakeSpinner
