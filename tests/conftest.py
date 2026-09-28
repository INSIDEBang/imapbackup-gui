"""Shared pytest fixtures.

这里只放需要 pytest 管道支持的东西（专属临时目录、进程级日志全局态复位、
stderr 控制台 handler 补位）。假 IMAP 服务器在 fakes.py —— 那是纯 Python，不需要
fixture 机制。
"""
import io
import logging
import tempfile
from pathlib import Path

import pytest

import imapbackup312 as core

BASETEMP = Path(tempfile.gettempdir()) / "imapbackup-gui-tests"

# configure_logging 里 basicConfig 用的同一套格式，见 imapbackup312.configure_logging
CONSOLE_FORMAT = '%(asctime)s %(levelname)s %(message)s'
CONSOLE_DATEFMT = '%H:%M:%S'


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    """给套件一个专属临时根，不共享 pytest 默认的 %TEMP%\\pytest-of-<user>。

    那个默认目录是跨项目共享的。这台机器上里面有一个别的账号留下的 pytest-current
    符号链接，连 os.stat 都拒绝访问（父目录只授了 SYSTEM / Administrators /
    OWNER RIGHTS，当前用户不是它的 owner）；pytest 收尾时 cleanup_dead_symlinks()
    对它 resolve() 直接抛 PermissionError，整个套件退出码变红——测试一条都没挂。
    Windows 上 get_user_id() 返回 None，pytest 那道「临时目录必须属于当前用户」的
    守卫整个被跳过，所以共享目录里有别人留下的东西它照样用，直到收尾才炸。

    basetemp 在 pytest 9 里不是 ini 选项（只认命令行），所以在这里注入。必须落在
    pytest_configure 而不是 pytest_sessionstart：TempPathFactory 在 sessionstart 时
    已被构造并把 config.option.basetemp 抓走，再改就晚了。
    """
    config.option.basetemp = str(BASETEMP)


def _drop_file_handlers(logger):
    """Close and detach every FileHandler.

    当日运行日志的 handler 必须在测试之间拆掉：不拆的话「root 上恰好一个 FileHandler」
    这类断言会被上一个测试的残留污染，文件句柄也不会释放。
    """
    for handler in [h for h in logger.handlers if isinstance(h, logging.FileHandler)]:
        handler.close()
        logger.removeHandler(handler)


@pytest.fixture
def clean_logging():
    """根 logger 回到「没有当日运行日志」的状态，像新起一个进程。

    configure_logging 会往 root 挂 FileHandler、把 StreamHandler 的级别压到 WARNING，
    还把 runlog 的 propagate 关成 False。不复位的话后一个测试看到的是上一个测试留下的
    状态。

    只清 FileHandler，保留 pytest 自己挂在 root 上的捕获 handler —— 那是 pytest 在测试
    失败时展示被捕获日志的来源，清掉它会让失败输出少一截。
    """
    root = logging.getLogger()
    runlog = logging.getLogger(core.RUN_LOG_LOGGER)
    saved = {"level": runlog.level, "propagate": runlog.propagate, "handlers": list(runlog.handlers)}

    _drop_file_handlers(root)
    _drop_file_handlers(runlog)
    root.setLevel(logging.NOTSET)
    runlog.setLevel(logging.NOTSET)
    runlog.propagate = True

    yield

    _drop_file_handlers(root)
    # 关掉的文件 handler 不能还挂在 runlog 上；只还回非文件 handler
    runlog.handlers[:] = [h for h in saved["handlers"] if not isinstance(h, logging.FileHandler)]
    runlog.setLevel(saved["level"])
    runlog.propagate = saved["propagate"]


@pytest.fixture
def console_capture(clean_logging):
    """一个 stderr StreamHandler，替 logging.basicConfig 该装却没装的那个。

    独立进程里 root 一开始没有 handler，configure_logging 调 basicConfig 就会装一个写
    stderr 的 StreamHandler。pytest 会在 root 上挂自己的捕获 handler，basicConfig 因此
    变成 no-op，CLI 的 INFO 记录就全部只进 pytest 的日志缓冲、不进 stderr——断言终端
    输出的测试会拿到空串。这里显式补上一个，格式与 basicConfig 一致。

    返回那个缓冲的 StringIO；用 redirect_stderr 把 print(..., file=sys.stderr) 也汇进去。
    """
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter(CONSOLE_FORMAT, datefmt=CONSOLE_DATEFMT))
    logging.getLogger().addHandler(handler)
    try:
        yield buf
    finally:
        logging.getLogger().removeHandler(handler)
