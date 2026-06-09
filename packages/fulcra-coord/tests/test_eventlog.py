"""Tests for the append-only event log I/O layer (eventlog.py).

Written test-first (TDD): these must FAIL with ModuleNotFoundError before
``eventlog.py`` and the path helpers in ``remote.py`` are created, and PASS
once they are in place.

Two core properties under test:

1. ``append_event`` / ``read_events`` round-trip: a written shard is readable
   back at the correct path and carries the same event_id.

2. ``read_events`` + ``fold_task`` compose correctly: multiple events for the
   same task, written independently, reduce to the expected final state.
"""

from unittest import mock

from fulcra_coord import events, eventlog, remote


def test_append_then_read_roundtrips(coord_backend):
    """A single appended event is returned by read_events with the same event_id."""
    e = events.make_event(
        family="tasks",
        task_id="TASK-9",
        kind="created",
        actor="a",
        payload={"title": "t", "status": "active"},
    )
    assert eventlog.append_event(e, backend=coord_backend) is True
    got = eventlog.read_events("TASK-9", backend=coord_backend)
    assert len(got) == 1
    assert got[0]["event_id"] == e["event_id"]


def test_read_then_fold_matches_written_state(coord_backend):
    """Three events appended in sequence reduce to the correct final state."""
    for kind, payload in [
        ("created", {"title": "t", "status": "active", "summary": "s0"}),
        ("updated", {"summary": "s1"}),
        ("done", {"status": "done"}),
    ]:
        eventlog.append_event(
            events.make_event(
                family="tasks",
                task_id="TASK-9",
                kind=kind,
                actor="a",
                payload=payload,
            ),
            backend=coord_backend,
        )
    state = events.fold_task(eventlog.read_events("TASK-9", backend=coord_backend))
    assert state["status"] == "done"
    assert state["summary"] == "s1"


def test_read_events_returns_plain_dicts_not_tuples(coord_backend):
    """read_events output must be plain dicts so fold_task composes directly.

    Pins the spec: read_events must strip shard paths and return event dicts,
    not (path, dict) tuples. Without this, fold_task(read_events(...)) crashes
    with 'tuple' object has no attribute 'get'.
    """
    eventlog.append_event(
        events.make_event(
            family="tasks",
            task_id="TASK-R",
            kind="created",
            actor="a",
            payload={"status": "active"},
        ),
        backend=coord_backend,
    )
    got = eventlog.read_events("TASK-R", backend=coord_backend)
    assert all(isinstance(e, dict) for e in got)
    # fold_task must consume read_events output DIRECTLY
    assert events.fold_task(got)["status"] == "active"


def test_read_events_for_unknown_task_returns_empty(coord_backend):
    """A task with no event shards yields []. The reconcile parity check (T6)
    relies on this empty-list path to mean 'not yet dual-written', so pin it."""
    assert eventlog.read_events("TASK-NOPE", backend=coord_backend) == []


def _shard(task_id, eid):
    """Build a minimal valid event dict whose JSON would parse to a dict."""
    return {"task_id": task_id, "event_id": eid, "payload": {"status": "active"}}


def test_read_events_warns_when_a_shard_fails_to_parse():
    """D3: a shard that fails to parse to a dict is silently dropped by
    list_json. A dropped *snapshot* shard could make the fold reconstruct
    STALE state while fold_is_complete still returns True — a silent
    correctness hazard. read_events must emit a best-effort warning naming the
    task and the drop count when fewer shards parse than the store lists, while
    STILL returning the good records unchanged."""
    listed = [
        "events/tasks/TASK-D3/aaa.json",
        "events/tasks/TASK-D3/bbb.json",
        "events/tasks/TASK-D3/ccc.json",  # this one fails to parse -> dropped
    ]
    parsed = [
        ("events/tasks/TASK-D3/aaa.json", _shard("TASK-D3", "aaa")),
        ("events/tasks/TASK-D3/bbb.json", _shard("TASK-D3", "bbb")),
    ]
    with mock.patch.object(remote, "list_files", return_value=listed), \
         mock.patch.object(remote, "list_json", return_value=parsed), \
         mock.patch.object(eventlog.log, "warning") as warn:
        got = eventlog.read_events("TASK-D3")

    # The good records are returned unchanged — the signal never blocks the read.
    assert [r["event_id"] for r in got] == ["aaa", "bbb"]
    assert warn.called, "expected a warning when listed > parsed"
    msg = " ".join(str(a) for a in warn.call_args[0]) + " " + str(warn.call_args)
    assert "TASK-D3" in msg, f"warning must name the task; got {warn.call_args!r}"
    assert "1" in msg, f"warning must report the drop count (1); got {warn.call_args!r}"


def test_read_events_no_warning_when_all_shards_parse():
    """When listed == parsed there is nothing dropped — no warning fires."""
    listed = [
        "events/tasks/TASK-OK/aaa.json",
        "events/tasks/TASK-OK/bbb.json",
    ]
    parsed = [
        ("events/tasks/TASK-OK/aaa.json", _shard("TASK-OK", "aaa")),
        ("events/tasks/TASK-OK/bbb.json", _shard("TASK-OK", "bbb")),
    ]
    with mock.patch.object(remote, "list_files", return_value=listed), \
         mock.patch.object(remote, "list_json", return_value=parsed), \
         mock.patch.object(eventlog.log, "warning") as warn:
        got = eventlog.read_events("TASK-OK")

    assert [r["event_id"] for r in got] == ["aaa", "bbb"]
    assert not warn.called, "no shard was dropped; warning must not fire"


def test_read_events_warning_failure_never_breaks_the_read():
    """The drop-detection is best-effort: if the extra list_files probe raises,
    read_events must still return the parsed records (signal is never load-
    bearing)."""
    parsed = [("events/tasks/TASK-B/aaa.json", _shard("TASK-B", "aaa"))]
    with mock.patch.object(remote, "list_files", side_effect=RuntimeError("boom")), \
         mock.patch.object(remote, "list_json", return_value=parsed):
        got = eventlog.read_events("TASK-B")
    assert [r["event_id"] for r in got] == ["aaa"]
