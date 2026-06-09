"""Signal C (dual-write liveness): surface event_append_failed in health.

The dual-write append path (writepipe) already logs an ``event_append_failed``
op to the local JSONL ops log on every failed event append — but those entries
are written and never read, so a host whose dual-write is silently failing is
invisible to the fleet. This signal reads the ops log back and surfaces a recent
append-failure count on the per-host health record.

Two layers under test:

1. ``cache.read_ops_log(since=...)`` — reads the JSONL ops log line-by-line,
   best-effort (skips malformed/blank lines, never raises), and windows by the
   ``logged_at`` timestamp when ``since`` is given. Missing file -> [].
2. The health-record counting path — counts ``event_append_failed`` entries in a
   recent window and surfaces an ``event_dual_write`` block, never breaking
   reconcile on a bad ops log.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fulcra_coord import cache
from fulcra_coord import log as ops_log


# ---------------------------------------------------------------------------
# Layer 1 — cache.read_ops_log
# ---------------------------------------------------------------------------

def test_read_ops_log_missing_file_returns_empty():
    """No ops log written yet -> [] (never raises)."""
    assert cache.read_ops_log() == []


def test_read_ops_log_reads_all_entries():
    """Every appended entry round-trips through read_ops_log."""
    ops_log.log_op("start", task_id="TASK-A", status="event_append_failed",
                   error="boom1")
    ops_log.log_op("update", task_id="TASK-B", status="event_append_failed",
                   error="boom2")
    ops_log.log_op("done", task_id="TASK-C", status="ok")

    entries = cache.read_ops_log()
    statuses = [e.get("status") for e in entries]
    assert statuses.count("event_append_failed") == 2
    assert statuses.count("ok") == 1


def test_read_ops_log_skips_malformed_lines():
    """A malformed / blank JSONL line is skipped, not fatal."""
    ops_log.log_op("start", task_id="TASK-A", status="ok")
    # Inject garbage + a blank line directly into the file.
    with cache.ops_log_path().open("a") as fh:
        fh.write("this is not json\n")
        fh.write("\n")
    ops_log.log_op("done", task_id="TASK-B", status="ok")

    entries = cache.read_ops_log()
    # Both valid entries survive; the garbage/blank lines are dropped.
    assert len(entries) == 2
    assert {e["task_id"] for e in entries} == {"TASK-A", "TASK-B"}


def test_read_ops_log_windows_by_since():
    """Entries older than `since` are excluded; newer/equal are kept."""
    now = datetime.now(timezone.utc)
    old_iso = (now - timedelta(hours=48)).isoformat(
        timespec="microseconds").replace("+00:00", "Z")
    # Hand-write an old entry (bypassing append_ops_log's auto-stamp).
    cache.ensure_dirs()
    with cache.ops_log_path().open("a") as fh:
        import json
        fh.write(json.dumps({"command": "start", "status": "event_append_failed",
                             "task_id": "OLD", "logged_at": old_iso}) + "\n")
    # And a fresh one (auto-stamped to now).
    ops_log.log_op("start", task_id="NEW", status="event_append_failed")

    since = now - timedelta(hours=24)
    recent = cache.read_ops_log(since=since)
    task_ids = {e.get("task_id") for e in recent}
    assert "NEW" in task_ids
    assert "OLD" not in task_ids


# ---------------------------------------------------------------------------
# Layer 2 — health-record counting path (event_dual_write block)
# ---------------------------------------------------------------------------

def test_count_append_failures_counts_recent():
    """2 event_append_failed + 1 ok -> append_failures_recent == 2."""
    from fulcra_coord import cli
    ops_log.log_op("start", task_id="TASK-A", status="event_append_failed",
                   error="boom1")
    ops_log.log_op("update", task_id="TASK-B", status="event_append_failed",
                   error="boom2")
    ops_log.log_op("done", task_id="TASK-C", status="ok")

    block = cli._event_dual_write_health()
    assert block["append_failures_recent"] == 2
    assert "window_since" in block


def test_count_append_failures_zero_when_none():
    """No failures -> append_failures_recent == 0."""
    from fulcra_coord import cli
    ops_log.log_op("done", task_id="TASK-C", status="ok")
    block = cli._event_dual_write_health()
    assert block["append_failures_recent"] == 0


def test_count_append_failures_excludes_old():
    """A failure outside the window is not counted."""
    from fulcra_coord import cli
    cache.ensure_dirs()
    old_iso = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat(
        timespec="microseconds").replace("+00:00", "Z")
    import json
    with cache.ops_log_path().open("a") as fh:
        fh.write(json.dumps({"command": "start", "status": "event_append_failed",
                             "task_id": "OLD", "logged_at": old_iso}) + "\n")
    ops_log.log_op("start", task_id="NEW", status="event_append_failed")

    block = cli._event_dual_write_health()
    assert block["append_failures_recent"] == 1  # only NEW, OLD excluded


def test_count_append_failures_never_raises_on_bad_log():
    """A corrupt ops log must NOT break the counting path."""
    from fulcra_coord import cli
    cache.ensure_dirs()
    with cache.ops_log_path().open("a") as fh:
        fh.write("totally not json\n")
    block = cli._event_dual_write_health()
    # Best-effort: malformed lines are skipped, count is 0, no raise.
    assert block["append_failures_recent"] == 0
