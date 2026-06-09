"""Phase 2b — Task 1: flag-gated read-from-fold cutover (io._cache_remote_task).

The read cutover changes what a READ reconstructs a task BODY from. It MUST be
default-off (zero behaviour change unless an operator sets
``FULCRA_COORD_READ_SOURCE=events``) and fall back to the mutable
``tasks/<id>.json`` file on ANY incompleteness or error in the event fold.

``_cache_remote_task`` is the single funnel every body read passes through
(both ``_load_task`` and the bulk ``_load_all_tasks``). These tests pin:

1. default (no env / ``file``) reads the mutable file — byte-identical to the
   pre-cutover behaviour;
2. ``events`` source returns the event FOLD for a task whose events include a
   full-task snapshot (``fold_is_complete`` is True);
3. ``events`` source FALLS BACK to the file for a delta-only event stream
   (fold incomplete);
4. ``events`` source FALLS BACK to the file when there are NO events at all;
5. ``events`` source STILL stats the mutable file for the cache meta — the
   write path's optimistic-concurrency pre-stat depends on this, so even when
   the body comes from the fold the file stat must be cached.
"""

import os

from fulcra_coord import cache, eventlog, events, io, remote, schema


def _write_file_task(task, *, backend):
    """Upload a task body to the mutable ``tasks/<id>.json`` remote path."""
    remote.upload_json(task, remote.task_remote_path(task["id"]), backend=backend)


def _append_snapshot(task, *, backend):
    """Append a full-task snapshot event (payload IS the task → fold complete)."""
    eventlog.append_event(
        events.make_event(
            family="tasks", task_id=task["id"], kind="start",
            actor="a", payload=dict(task),
        ),
        backend=backend,
    )


def _append_delta(task_id, payload, *, backend):
    """Append a Phase-1 delta event (field subset → fold INcomplete)."""
    eventlog.append_event(
        events.make_event(
            family="tasks", task_id=task_id, kind="update",
            actor="a", payload=payload,
        ),
        backend=backend,
    )


def test_default_read_source_reads_the_file(monkeypatch, coord_backend):
    """No env knob set → body comes from the mutable file (default = 'file')."""
    monkeypatch.delenv("FULCRA_COORD_READ_SOURCE", raising=False)
    task = schema.make_task(title="from-file", workstream="ws", agent="a")
    task["current_summary"] = "FILE-BODY"
    _write_file_task(task, backend=coord_backend)
    # An event fold exists too, but in default mode it must be ignored entirely.
    _append_snapshot({**task, "current_summary": "FOLD-BODY"}, backend=coord_backend)

    got = io._cache_remote_task(task["id"], backend=coord_backend)
    assert got is not None
    assert got["current_summary"] == "FILE-BODY"


def test_events_source_returns_fold_for_complete_task(monkeypatch, coord_backend):
    """events mode + a snapshot event → body is reconstructed from the FOLD."""
    monkeypatch.setenv("FULCRA_COORD_READ_SOURCE", "events")
    task = schema.make_task(title="folded", workstream="ws", agent="a")
    # File body and fold body intentionally DIFFER so we can tell which won.
    _write_file_task({**task, "current_summary": "FILE-BODY"}, backend=coord_backend)
    _append_snapshot({**task, "current_summary": "FOLD-BODY"}, backend=coord_backend)

    got = io._cache_remote_task(task["id"], backend=coord_backend)
    assert got is not None
    assert got["current_summary"] == "FOLD-BODY"


def test_events_source_falls_back_to_file_for_delta_only(monkeypatch, coord_backend):
    """events mode + a DELTA-only stream (fold incomplete) → fall back to file."""
    monkeypatch.setenv("FULCRA_COORD_READ_SOURCE", "events")
    task = schema.make_task(title="delta-only", workstream="ws", agent="a")
    task["current_summary"] = "FILE-BODY"
    _write_file_task(task, backend=coord_backend)
    # Delta payload carries neither schema nor id → fold_is_complete is False.
    _append_delta(task["id"], {"current_summary": "DELTA-ONLY"}, backend=coord_backend)

    got = io._cache_remote_task(task["id"], backend=coord_backend)
    assert got is not None
    assert got["current_summary"] == "FILE-BODY"


def test_events_source_no_events_falls_back_to_file(monkeypatch, coord_backend):
    """events mode + NO events at all → fall back to the mutable file."""
    monkeypatch.setenv("FULCRA_COORD_READ_SOURCE", "events")
    task = schema.make_task(title="no-events", workstream="ws", agent="a")
    task["current_summary"] = "FILE-BODY"
    _write_file_task(task, backend=coord_backend)
    # No events appended for this task.

    got = io._cache_remote_task(task["id"], backend=coord_backend)
    assert got is not None
    assert got["current_summary"] == "FILE-BODY"


def test_events_source_still_stats_file_for_write_meta(monkeypatch, coord_backend):
    """Even when the body comes from the fold, the FILE stat must be cached.

    The write path (writepipe._write_task_and_views) reads cache.read_meta on
    the mutable file path for optimistic-concurrency. If events-mode skipped the
    file stat, the very next write would see no baseline and mis-handle the merge
    check. So _cache_remote_task must ALWAYS stat tasks/<id>.json → write_meta,
    regardless of where the body came from.
    """
    monkeypatch.setenv("FULCRA_COORD_READ_SOURCE", "events")
    task = schema.make_task(title="meta", workstream="ws", agent="a")
    _write_file_task({**task, "current_summary": "FILE-BODY"}, backend=coord_backend)
    _append_snapshot({**task, "current_summary": "FOLD-BODY"}, backend=coord_backend)

    task_path = remote.task_remote_path(task["id"])
    # Sanity: no cached meta before the read.
    assert cache.read_meta(task_path) is None

    got = io._cache_remote_task(task["id"], backend=coord_backend)
    # Body came from the fold ...
    assert got["current_summary"] == "FOLD-BODY"
    # ... and yet the FILE stat is cached for the write-path baseline.
    meta = cache.read_meta(task_path)
    assert meta is not None
    assert meta.get("version") == remote.stat(task_path, backend=coord_backend)["version"]


def test_events_source_body_has_no_fold_bookkeeping(coord_backend, monkeypatch):
    # The fold's internal _applied_event_count must NOT leak into the returned
    # task body — else a read-modify-write in events mode would persist it into
    # the durable tasks/<id>.json (apply_event deep-copies all keys) and parity
    # would be blind to it (its ignore-set hides _applied_event_count).
    monkeypatch.setenv("FULCRA_COORD_READ_SOURCE", "events")
    task = schema.make_task(title="bookkeeping", workstream="ws", agent="a")
    _write_file_task({**task, "current_summary": "FILE-BODY"}, backend=coord_backend)
    _append_snapshot({**task, "current_summary": "FOLD-BODY"}, backend=coord_backend)
    got = io._cache_remote_task(task["id"], backend=coord_backend)
    assert got["current_summary"] == "FOLD-BODY"   # confirm it IS the fold
    assert "_applied_event_count" not in got       # but bookkeeping stripped
