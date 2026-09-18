# -*- coding: utf-8 -*-
"""测试用的假 IMAP 服务器与替身。

全部是进程内的 imaplib.IMAP4 替身：不起 socket、不联网、不需要凭证。每个类只实现
被测代码真正调用的方法，返回值照抄 imaplib 的形状（`("OK", [bytes, ...])`）。

这里集中放这些替身，是因为它们在 .scratch/ 的一次性脚本里被逐字复制过 7 份
（4 种 FakeServer 形态、3 份 NullLog、2 份 modified UTF-7 编码器）；迁移后只留一份。
"""
import base64
import logging
from logging import NullHandler


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


class ListServer:
    """只有 LIST 的服务器：枚举文件夹用的最小替身。

    display_names 是已解码的显示名，list() 会编码回 modified UTF-7 再返回，
    和真实服务器一致。delim 控制层级分隔符（"." 用于 Outlook 风格账号）。
    """
    def __init__(self, display_names, delim="/"):
        self.display_names = list(display_names)
        self.delim = delim

    def list(self):
        rows = [f'(\\HasNoChildren) "{self.delim}" "{imap_utf7_encode(n)}"' for n in self.display_names]
        return "OK", [r.encode("utf-8") for r in rows]

    def select(self, quoted, readonly=False):
        return "OK", [b"0"]

    def logout(self):
        return "OK", [b"bye"]


class MainServer:
    """能驱动 main() 跑完全流程的服务器：LIST / SELECT / FETCH / LOGOUT。

    fail_fetch / interrupt_fetch 用来注入失败路径，验证 main() 的顶层捕获。
    """
    def __init__(self, messages=0, fail_fetch=None, interrupt_fetch=None):
        self.messages = messages
        self.fail_fetch = fail_fetch
        self.interrupt_fetch = interrupt_fetch

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"']

    def select(self, quoted, readonly=False):
        return "OK", [str(self.messages).encode()]

    def fetch(self, seq, items):
        if self.interrupt_fetch:
            raise KeyboardInterrupt
        if self.fail_fetch:
            raise RuntimeError(f"FETCH failed for UID {seq}: {self.fail_fetch}")
        raw = b"Message-Id: <aaa@x>\r\n\r\nbody\r\n"
        return "OK", [(f"{seq} (RFC822 {len(raw)})".encode(), raw)]

    def logout(self):
        return "OK", [b"bye"]


class FetchServer:
    """按序号返回预置响应的服务器，记录每次 FETCH 请求的 items。

    responses 是 {seq: (metadata_with_length_prefix, raw_bytes)}；fetch() 把它拼成
    imaplib 的两段式返回。fetches 记下 (seq, items)，用来断言取的是什么字段、取了几次。
    """
    def __init__(self, responses):
        self.responses = dict(responses)
        self.fetches = []

    def fetch(self, seq, items):
        self.fetches.append((seq, items))
        return "OK", [self.responses[int(seq)], b")"]


class HeaderServer:
    """SELECT 出一封消息，FETCH 只回 MESSAGE-ID header。

    给 scan_folder 的合成 ID 兜底路径（缺 Message-Id 时抓 header）用。
    """
    def select(self, quoted, readonly=False):
        return "OK", [b"1"]

    def fetch(self, seq, items):
        return "OK", [(b'1 (BODY[HEADER.FIELDS (MESSAGE-ID)] {20}', b'Message-Id: <r1@x>\r\n'), b")"]


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
