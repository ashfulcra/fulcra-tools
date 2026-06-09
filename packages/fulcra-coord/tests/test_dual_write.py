"""Strangler-fig dual-write tests for the LIVE task write path (Task 5).

These guard the single most safety-critical property of Phase 1: the additive
event dual-write bolted onto ``_write_task_and_views`` must NEVER be able to
fail or alter an existing task write. The mutable ``tasks/<id>.json`` is still
authoritative this phase; the event append is purely best-effort.

Two properties under test:

1. A normal-completion task write ALSO appends exactly one immutable event that
   mirrors the task's mutable fields (here: ``status``).

2. If the event append blows up, the task write still returns success — the
   dual-write is wrapped and swallows all exceptions, so the live fleet write
   path is unaffected by any event-log fault.

Written test-first: property (1) FAILS before the dual-write block is added
(no event appended); both PASS once it lands.
"""

from fulcra_coord import cli, eventlog


def test_start_also_appends_an_event(coord_backend, monkeypatch):
    monkeypatch.setenv("FULCRA_COORD_BACKEND", " ".join(coord_backend))
    task = {"id": "TASK-DW1", "title": "dual write", "status": "active",
            "current_summary": "s", "workstream": "ws", "owner_agent": "a"}
    assert cli._write_task_and_views(task, backend=coord_backend, command="start") is True
    evs = eventlog.read_events("TASK-DW1", backend=coord_backend)
    assert len(evs) == 1
    assert evs[0]["kind"] == "start"
    assert evs[0]["payload"]["status"] == "active"
    # idempotency_key (the op_id) must be present + truthy — fold_task dedup
    # of retried writes depends on it; a regression dropping it would silently
    # disable retry-collapse.
    assert evs[0]["idempotency_key"]


def test_event_write_failure_does_not_fail_the_task_write(coord_backend, monkeypatch):
    import fulcra_coord.writepipe as wp
    monkeypatch.setattr(wp.eventlog, "append_event",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    task = {"id": "TASK-DW2", "title": "t", "status": "active", "current_summary": "s",
            "workstream": "ws", "owner_agent": "a"}
    assert cli._write_task_and_views(task, backend=coord_backend, command="start") is True


def test_event_write_false_return_logs_failure_without_failing_task_write(coord_backend, monkeypatch):
    import fulcra_coord.writepipe as wp

    seen = []
    monkeypatch.setattr(wp.eventlog, "append_event", lambda *a, **k: False)
    monkeypatch.setattr(wp.ops_log, "log_op",
                        lambda *args, **kwargs: seen.append((args, kwargs)))

    task = {"id": "TASK-DW3", "title": "t", "status": "active", "current_summary": "s",
            "workstream": "ws", "owner_agent": "a"}
    assert cli._write_task_and_views(task, backend=coord_backend, command="start") is True
    assert any(
        args == ("start", "TASK-DW3") and kwargs["status"] == "event_append_failed"
        for args, kwargs in seen
    )
