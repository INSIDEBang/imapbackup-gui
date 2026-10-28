"""诊断采样写入器 Diagnostics 的回归（规格 §7、§12.2 I 组中与文件相关的断言）。

不起 Tk 窗口：写入器只依赖 core.LOG_DIR（用 isolate_log_dir 重定向到临时目录）。「事件队列 →
JSONL」的整条链路（含 worker 统计、隐私白名单、run/节奏）在 test_gui_smoke.py 的诊断小节里，
与其他 GUI 用例共用那一个真实例——本进程里再多建一个 Tk 根窗口会让整套 GUI 用例不稳定。
"""
import json
import logging
import re
from datetime import datetime
from pathlib import Path

import pytest

from diag_helpers import FIELDS, read_rows

pytest.importorskip("tkinter")   # imapbackup_gui 在模块级 import tkinter（不起窗口）

import imapbackup_gui as g

pytestmark = pytest.mark.gui

FIXED_NOW = datetime(2026, 10, 28, 9, 30).astimezone()


def test_every_row_has_the_full_schema_and_identity_fields(isolate_log_dir):
    """所有行共用规格 §7.3 的字段表与顺序；不适用的字段是 null；中文原样写（不转义）。"""
    diag = g.Diagnostics(logging.getLogger("test.diag.schema"))
    diag.write("phase", account="tracer@example.com", host="imap.example.com", folder="收件箱", phase="scanning")

    (path,) = isolate_log_dir.glob("diag-*.jsonl")
    assert path.name == f"diag-{datetime.now():%Y-%m-%d}.jsonl"
    text = path.read_text(encoding="utf-8")
    assert text.endswith("\n") and text.count("\n") == 1 and "收件箱" in text
    (row,) = read_rows(isolate_log_dir)
    assert list(row) == FIELDS
    assert (row["schema"], row["app"], row["event"]) == (1, "imapbackup-gui", "phase")
    assert (row["account"], row["host"], row["folder"], row["phase"]) == (
        "tracer@example.com", "imap.example.com", "收件箱", "scanning")
    assert re.fullmatch(r"[0-9a-f]{8}", row["run"]), row["run"]
    assert re.fullmatch(r".*T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2}", row["ts"]), row["ts"]
    assert datetime.fromisoformat(row["ts"]).tzinfo is not None
    untouched = set(FIELDS) - {"ts", "schema", "app", "run", "event", "account", "host", "folder", "phase"}
    assert {name: row[name] for name in untouched} == dict.fromkeys(untouched)


def test_the_run_id_is_stable_until_the_run_ends(isolate_log_dir):
    diag = g.Diagnostics(logging.getLogger("test.diag.run"))
    diag.write("phase")
    diag.write("sample")
    first, second = (row["run"] for row in read_rows(isolate_log_dir))
    assert first == second
    diag.end_run()
    diag.write("phase")
    runs = [row["run"] for row in read_rows(isolate_log_dir)]
    assert runs[2] != first and re.fullmatch(r"[0-9a-f]{8}", runs[2])


def test_a_new_day_starts_a_new_file(isolate_log_dir):
    """每次写入前按当天日期选文件：跨天自动换文件，已写的行不动。"""
    moments = iter([datetime(2026, 10, 27, 23, 59, 59, 900_000).astimezone(),
                    datetime(2026, 10, 28, 0, 0, 0, 100_000).astimezone()])
    diag = g.Diagnostics(logging.getLogger("test.diag.days"), clock=lambda: next(moments))
    diag.write("run_end", result="completed")
    diag.write("run_end", result="failed")
    assert sorted(p.name for p in isolate_log_dir.glob("diag-*")) == ["diag-2026-10-27.jsonl", "diag-2026-10-28.jsonl"]
    first = json.loads((isolate_log_dir / "diag-2026-10-27.jsonl").read_text(encoding="utf-8"))
    second = json.loads((isolate_log_dir / "diag-2026-10-28.jsonl").read_text(encoding="utf-8"))
    assert (first["result"], second["result"]) == ("completed", "failed")
    assert first["ts"].startswith("2026-10-27T23:59:59.900") and second["ts"].startswith("2026-10-28T00:00:00.100")


def test_each_account_is_due_every_ten_seconds_without_drift_and_phases_reset_the_timer():
    """10 秒节拍：按固定间隔顺延（tick 晚到不累积漂移）；phase 重新计时；停摆太久从现在重算。"""
    diag = g.Diagnostics(logging.getLogger("test.diag.timer"))
    assert not diag.is_due("a", 100.0)                    # 没开跑的账户永远不到点
    diag.begin_account("a", 100.0)
    assert [diag.is_due("a", at) for at in (100.0, 109.9, 110.0)] == [False, False, True]
    diag.advance_timer("a", 110.4)                        # 晚到 0.4 秒：下一个节拍仍是 120
    assert [diag.is_due("a", at) for at in (119.9, 120.0)] == [False, True]
    diag.reset_timer("a", 115.0)                          # 写了 phase：从 115 起重新算
    assert [diag.is_due("a", at) for at in (124.9, 125.0)] == [False, True]
    diag.advance_timer("a", 200.0)                        # 停摆很久：不补发一串，从现在起重算
    assert [diag.is_due("a", at) for at in (209.9, 210.0)] == [False, True]
    assert diag.elapsed("a", 207.0) == 107.0
    diag.end_account("a")
    assert not diag.is_due("a", 1000.0) and diag.elapsed("a", 1000.0) is None


def test_rotation_deletes_only_diagnostic_files_older_than_fourteen_days(isolate_log_dir):
    """14 天轮转只删文件名完全匹配、日期早于保留期的 diag-*.jsonl。"""
    isolate_log_dir.mkdir()
    expected_gone = ["diag-2026-10-13.jsonl", "diag-2020-01-01.jsonl"]          # 15 天前、很久以前
    expected_kept = ["diag-2026-10-14.jsonl", "diag-2026-10-28.jsonl",           # 正好 14 天前、今天
                     "run-2020-01-01.log", "treeview-columns.json",              # 别的文件一概不碰
                     "diag-2020-01-01.jsonl.bak", "diag-notes.txt", "Diag-2020-01-01.JSONL.txt",
                     "diag-2026-13-99.jsonl"]                                    # 日期非法：不是我们写的
    for name in [*expected_gone, *expected_kept]:
        (isolate_log_dir / name).write_text("x\n", encoding="utf-8")
    (isolate_log_dir / "diag-2020-02-02.jsonl").mkdir()                          # 同名目录不是文件
    diag = g.Diagnostics(logging.getLogger("test.diag.rotate"), clock=lambda: FIXED_NOW)

    diag.rotate()

    assert sorted(p.name for p in isolate_log_dir.iterdir()) == sorted([*expected_kept, "diag-2020-02-02.jsonl"])


def test_rotation_without_a_log_dir_is_a_no_op(isolate_log_dir):
    g.Diagnostics(logging.getLogger("test.diag.rotate"), clock=lambda: FIXED_NOW).rotate()
    assert not isolate_log_dir.exists()


def test_a_failed_deletion_only_warns_and_the_rest_are_still_cleaned(isolate_log_dir, monkeypatch, caplog):
    isolate_log_dir.mkdir()
    for name in ("diag-2020-01-01.jsonl", "diag-2020-01-02.jsonl", "diag-2020-01-03.jsonl"):
        (isolate_log_dir / name).write_text("x\n", encoding="utf-8")
    original = Path.unlink

    def unlink(self, *args, **kwargs):
        if self.name == "diag-2020-01-02.jsonl":
            raise PermissionError("locked by another process")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    diag = g.Diagnostics(logging.getLogger("test.diag.rotate"), clock=lambda: FIXED_NOW)
    with caplog.at_level(logging.WARNING, logger="test.diag.rotate"):
        diag.rotate()

    assert [p.name for p in isolate_log_dir.iterdir()] == ["diag-2020-01-02.jsonl"]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "diag-2020-01-02.jsonl" in warnings[0].getMessage()
    assert not diag.disabled                # 清理失败不影响诊断写入


def test_a_write_failure_disables_diagnostics_with_a_single_warning(isolate_log_dir, caplog):
    """日志目录建不出来（路径被一个文件占了）：不抛异常，禁用后不再尝试，只记一次 WARNING。"""
    isolate_log_dir.write_text("not a directory", encoding="utf-8")
    diag = g.Diagnostics(logging.getLogger("test.diag.fail"))
    with caplog.at_level(logging.WARNING, logger="test.diag.fail"):
        diag.write("sample")
        diag.write("sample")
        diag.write("run_end", result="completed")
    assert diag.disabled
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    assert isolate_log_dir.read_text(encoding="utf-8") == "not a directory"


def test_an_injected_exception_also_only_disables_diagnostics(isolate_log_dir, caplog):
    def broken_clock():
        raise RuntimeError("injected")

    diag = g.Diagnostics(logging.getLogger("test.diag.inject"), clock=broken_clock)
    with caplog.at_level(logging.WARNING, logger="test.diag.inject"):
        diag.write("sample")
    assert diag.disabled and len(caplog.records) == 1


def test_fields_outside_the_whitelist_are_refused(isolate_log_dir, caplog):
    """隐私白名单由构造保证：写入器拒收字段表之外的键（主题、显示名…），一行都不落盘。"""
    diag = g.Diagnostics(logging.getLogger("test.diag.whitelist"))
    with caplog.at_level(logging.WARNING, logger="test.diag.whitelist"):
        diag.write("sample", subject="机密主题")
    assert diag.disabled
    assert list(isolate_log_dir.glob("diag-*")) == []
