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

from fulcra_coord import events, eventlog


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
    # read_events returns list of (path, record) tuples — unwrap the records.
    records = [rec for _, rec in got]
    assert len(records) == 1
    assert records[0]["event_id"] == e["event_id"]


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
    raw = eventlog.read_events("TASK-9", backend=coord_backend)
    # fold_task expects a plain list of event dicts, not (path, rec) pairs.
    evs = [rec for _, rec in raw]
    state = events.fold_task(evs)
    assert state["status"] == "done"
    assert state["summary"] == "s1"
