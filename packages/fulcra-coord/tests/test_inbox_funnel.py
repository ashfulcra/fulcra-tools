"""inbox.py ack-fallback body read must go through the single read funnel.

``inbox --ack`` falls back to ``_load_task_from_summary`` when ``_load_task``
returns nothing (the task isn't in the index but a summary still names a durable
file). That fallback historically read the body via ``remote.download_json``
DIRECTLY — bypassing ``io._cache_remote_task``, the single body-read funnel that
honors the Phase-2b ``FULCRA_COORD_READ_SOURCE`` knob. So once a host opted into
events mode, the rest of the read path read the event-fold while this one path
read the (possibly divergent) mutable file.

These tests pin the funnel routing:

1. events mode + a DIVERGENT full-task snapshot event → the fallback returns the
   FOLD body, not the stale file body (the read-source knob is honored);
2. default file mode → the fallback returns the file body byte-identically (no
   behaviour change for an operator who hasn't opted in).
"""

from fulcra_coord import eventlog, events, inbox, remote, schema


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


def _summary_for(task):
    """A minimal inbox summary naming the task's durable file path."""
    return {"id": task["id"], "task_file": remote.task_remote_path(task["id"])}


def test_ack_fallback_honors_events_read_source(monkeypatch, coord_backend):
    """events mode → the ack fallback returns the event-FOLD body, not the file.

    File body and fold body intentionally DIVERGE (different current_summary).
    Pre-fix, the direct download_json returned the FILE body (RED). Post-fix,
    routing through io._cache_remote_task returns the FOLD body (GREEN).
    """
    monkeypatch.setenv("FULCRA_COORD_READ_SOURCE", "events")
    task = schema.make_task(title="folded-ack", workstream="ws", agent="a")
    _write_file_task({**task, "current_summary": "FILE-BODY"}, backend=coord_backend)
    _append_snapshot({**task, "current_summary": "FOLD-BODY"}, backend=coord_backend)

    got = inbox._load_task_from_summary(_summary_for(task), backend=coord_backend)
    assert got is not None
    assert got["id"] == task["id"]
    assert got["current_summary"] == "FOLD-BODY"


def test_ack_fallback_default_file_mode_unchanged(monkeypatch, coord_backend):
    """default file mode → the ack fallback returns the file body byte-identically."""
    monkeypatch.delenv("FULCRA_COORD_READ_SOURCE", raising=False)
    task = schema.make_task(title="file-ack", workstream="ws", agent="a")
    task["current_summary"] = "FILE-BODY"
    _write_file_task(task, backend=coord_backend)
    # A divergent fold exists too, but default mode must ignore it entirely.
    _append_snapshot({**task, "current_summary": "FOLD-BODY"}, backend=coord_backend)

    got = inbox._load_task_from_summary(_summary_for(task), backend=coord_backend)
    assert got is not None
    assert got["id"] == task["id"]
    assert got["current_summary"] == "FILE-BODY"
