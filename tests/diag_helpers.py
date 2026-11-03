"""诊断采样 JSONL 测试的共用件（test_diagnostics.py 与 test_gui_smoke.py 共用）。"""
import json
from pathlib import Path

# 规格 §7.3 的字段表，逐字抄写：用例拿它当 schema 的独立真相来源，而不是回读实现里的常量。
FIELDS = [
    "ts", "schema", "app", "run", "event", "account", "host", "folder", "phase",
    "done_msgs", "done_bytes", "pending_msgs", "pending_bytes",
    "rate_msgs_per_min", "rate_bytes_per_s", "eta_s",
    "batch_msgs", "batch_bytes", "fetch_ms_avg", "fetch_ms_p95", "fetch_ms_max",
    "inflight_msgs", "inflight_bytes", "inflight_elapsed_s",
    "fetch_failed", "fetch_failed_missing", "fetch_failed_no_bad", "fetch_failed_malformed",
    "fetch_failed_timeout",
    "folder_failures",
    "index_hits", "index_misses", "index_rebuilds", "index_disabled", "scan_local_ms", "scan_remote_ms",
    "reconnect_attempts", "reconnect_successes", "reconnect_exhausted", "uidvalidity_changes",
    "reconnect_reason",
    "result", "duration_s",
]


def read_rows(log_dir: Path) -> list[dict]:
    """日志目录里全部 diag-*.jsonl 的行（按文件名即日期排序）。"""
    rows: list[dict] = []
    for path in sorted(log_dir.glob("diag-*.jsonl")):
        rows += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return rows
