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
