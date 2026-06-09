"""Size-rotation for the local JSONL ops log (cache.append_ops_log).

WHY THIS EXISTS — an unbounded-growth fix, not a nicety:

v0.13.0's event-liveness work made the ops log a load-bearing read surface
(Signal C: ``read_ops_log`` is consulted on every reconcile). But
``append_ops_log`` appends on EVERY task write and nothing ever trims the file,
so on a long-lived host (the DeskbookPro canary running 0.13.0) ``ops.log`` grows
without bound — and each reconcile then reads the WHOLE file (O(all-ops-ever)).

The fix is a single-segment size rotation: once the current ops log exceeds
``FULCRA_COORD_OPSLOG_MAX_BYTES``, rename it to a ``.1`` sibling (overwriting any
prior ``.1``). Because ``log_op`` opens the path fresh in append mode on every
call, the next append simply re-creates the current file — so NO append is lost
across the rotation (there is no shared open handle straddling the rename). The
reader (``read_ops_log``) then reads BOTH segments so the recent window isn't
truncated the instant a rotation happens.

These tests pin: rotation fires past the threshold, the current file is reset
small, no entry is lost (read spans both segments), a subsequent append lands in
a fresh current file, and rotation is a best-effort no-op when disabled / under
the default threshold.
"""

from __future__ import annotations

import os

from fulcra_coord import cache


def _rotated_path():
    p = cache.ops_log_path()
    return p.with_name(p.name + ".1")


# The cap is floored at 4096 (a tiny override can't rotate after every line), so
# tests use the floor as the threshold and write enough to exceed it.
_THRESHOLD = "4096"


def test_append_rotates_past_threshold(monkeypatch):
    """Once the current ops log exceeds the byte threshold, it rotates to ``.1``
    and the current file is reset to a small size — bounding unbounded growth."""
    monkeypatch.setenv("FULCRA_COORD_OPSLOG_MAX_BYTES", _THRESHOLD)

    # Each entry is ~50 bytes; 300 entries (~15KB) comfortably exceeds the 4096 cap.
    for i in range(300):
        cache.append_ops_log({"command": "update", "task_id": f"TASK-{i}",
                              "status": "ok"})

    rotated = _rotated_path()
    assert rotated.exists(), "expected a .1 rotated segment after exceeding the cap"
    # The current file was reset by the rotation; it must be far smaller than the
    # full history we wrote (which spans both segments).
    cur_size = cache.ops_log_path().stat().st_size
    assert cur_size <= int(_THRESHOLD), \
        f"current ops log not reset by rotation (size={cur_size})"


def test_rotation_loses_no_entries_and_read_spans_both_segments(monkeypatch):
    """Across a SINGLE rotation no append is dropped, and ``read_ops_log`` returns
    entries from BOTH the current file and the ``.1`` rotated segment.

    (Rotation is single-segment by design: a SECOND rotation overwrites ``.1`` and
    drops the oldest — that's the bound. The no-loss guarantee is specifically the
    recent window straddling one rotation, which is what Signal C reads.)"""
    monkeypatch.setenv("FULCRA_COORD_OPSLOG_MAX_BYTES", _THRESHOLD)

    # Write just past the cap so exactly ONE rotation fires: each entry is ~105
    # bytes, so a 4096-byte segment holds ~39. 60 entries trips the cap once
    # (~39 rotated into .1) and leaves the rest in a fresh current file, WITHOUT
    # reaching a second rotation (which would overwrite .1 and drop the oldest).
    n = 60
    for i in range(n):
        cache.append_ops_log({"command": "update", "task_id": f"TASK-{i}",
                              "status": "ok"})

    assert _rotated_path().exists(), "a single rotation should have occurred"
    # Guard the premise: only one rotation happened, so we did NOT overwrite .1 a
    # second time and drop the oldest. The current file is either absent (rotation
    # fired on the final append, leaving the next write to recreate it) or under
    # the cap — never a second full segment.
    cur = cache.ops_log_path()
    if cur.exists():
        assert cur.stat().st_size <= int(_THRESHOLD)

    entries = cache.read_ops_log()
    seen = {e.get("task_id") for e in entries}
    # Every appended task id survives — split across the ``.1`` and current files.
    for i in range(n):
        assert f"TASK-{i}" in seen, f"TASK-{i} lost across a single rotation"


def test_append_after_rotation_uses_fresh_current_file(monkeypatch):
    """The append immediately following a rotation lands in a fresh current file
    (proving log_op opens the path fresh per write — no stale handle)."""
    monkeypatch.setenv("FULCRA_COORD_OPSLOG_MAX_BYTES", _THRESHOLD)

    for i in range(300):
        cache.append_ops_log({"command": "update", "task_id": f"TASK-{i}",
                              "status": "ok"})
    assert _rotated_path().exists()

    # Append one more; it must be present in the CURRENT file specifically.
    cache.append_ops_log({"command": "done", "task_id": "TASK-FRESH",
                          "status": "ok"})
    cur_text = cache.ops_log_path().read_text()
    assert "TASK-FRESH" in cur_text, "post-rotation append did not hit the fresh current file"


def test_no_rotation_under_default_threshold():
    """With the default (large) threshold, a few appends never rotate — no ``.1``
    segment appears and everything stays in the single current file."""
    # No env override -> sane large default; a handful of entries can't trip it.
    os.environ.pop("FULCRA_COORD_OPSLOG_MAX_BYTES", None)
    for i in range(5):
        cache.append_ops_log({"command": "update", "task_id": f"TASK-{i}",
                              "status": "ok"})
    assert not _rotated_path().exists(), "rotated under the default threshold unexpectedly"
    assert len(cache.read_ops_log()) == 5
