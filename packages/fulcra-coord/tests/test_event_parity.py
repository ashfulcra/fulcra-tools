"""Reconcile event-parity check tests (Task 6).

Two properties under test:

1. Zero drift when the event fold's status matches the mutable snapshot —
   confirms the parity check does NOT false-positive on a consistent bus.

2. Drift is flagged when the event fold disagrees with the snapshot status —
   confirms the parity check actually catches inconsistencies.

Written test-first (TDD): both tests FAIL before _event_parity_check is added
to cli.py, and PASS once the function and its wiring are in place.

Phase 2a (Task 3) extends the suite with three additional tests covering
full-task comparison when a snapshot event is present (fold_is_complete),
while keeping the Phase-1 status-only tests to confirm the delta-only branch
is unaffected.
"""

from fulcra_coord import cli, events, eventlog, remote, schema


def test_parity_reports_zero_drift_when_event_fold_matches_snapshot(coord_backend):
    """A task whose event-fold status matches the snapshot must contribute zero drift."""
    task = {"id": "TASK-P1", "title": "t", "status": "done", "current_summary": "s1"}
    remote.upload_json(task, remote.task_remote_path("TASK-P1"), backend=coord_backend)
    for kind, p in [("start", {"title": "t", "status": "active", "current_summary": "s0"}),
                    ("update", {"current_summary": "s1"}), ("done", {"status": "done"})]:
        eventlog.append_event(events.make_event(family="tasks", task_id="TASK-P1",
                              kind=kind, actor="a", payload=p), backend=coord_backend)
    report = cli._event_parity_check(backend=coord_backend)
    assert report["checked"] >= 1
    assert report["drift"] == 0


def test_parity_flags_drift_when_event_fold_disagrees(coord_backend):
    """A task whose event-fold status differs from the snapshot must appear in drift_task_ids."""
    task = {"id": "TASK-P2", "title": "t", "status": "active", "current_summary": "snapshot-only"}
    remote.upload_json(task, remote.task_remote_path("TASK-P2"), backend=coord_backend)
    eventlog.append_event(events.make_event(family="tasks", task_id="TASK-P2",
                          kind="done", actor="a", payload={"status": "done"}), backend=coord_backend)
    report = cli._event_parity_check(backend=coord_backend)
    assert report["drift"] >= 1
    assert "TASK-P2" in report["drift_task_ids"]


# ---------------------------------------------------------------------------
# Phase 2a — Task 3: full-task comparison when fold_is_complete
# ---------------------------------------------------------------------------

def test_parity_full_task_match_when_snapshot(coord_backend):
    """No drift when the full-task snapshot event agrees with the live file.

    The snapshot event payload IS the task (carries schema + id), so
    fold_is_complete will be True and the full-task comparison path is taken.
    All durable fields match, so drift must be 0.
    """
    task = schema.make_task(title="p", workstream="ws", agent="a")
    task["status"] = "active"
    remote.upload_json(task, remote.task_remote_path(task["id"]), backend=coord_backend)
    eventlog.append_event(events.make_event(family="tasks", task_id=task["id"],
                          kind="start", actor="a", payload=dict(task)), backend=coord_backend)
    report = cli._event_parity_check(backend=coord_backend)
    assert report["drift"] == 0


def test_parity_flags_full_task_drift_when_snapshot_disagrees(coord_backend):
    """Drift is flagged when a snapshot event disagrees with the live file on a
    non-status field (current_summary), even though status is identical.

    The Phase-1 status-only check would miss this; the Phase-2a full-task
    comparison must catch it.
    """
    task = schema.make_task(title="p", workstream="ws", agent="a")
    task["status"] = "active"
    remote.upload_json(task, remote.task_remote_path(task["id"]), backend=coord_backend)
    # Snapshot event carries a DIFFERENT current_summary, but the SAME status.
    snap = dict(task)
    snap["current_summary"] = "DIFFERENT"
    eventlog.append_event(events.make_event(family="tasks", task_id=task["id"],
                          kind="start", actor="a", payload=snap), backend=coord_backend)
    report = cli._event_parity_check(backend=coord_backend)
    assert report["drift"] >= 1
    assert task["id"] in report["drift_task_ids"]


def test_parity_full_task_ignores_volatile_fields(coord_backend):
    """Volatile fields (updated_at, last_touched_*) must NOT trigger drift.

    These fields legitimately diverge between a point-in-time snapshot and the
    live file (every write updates them), so excluding them from the comparison
    is correct.  The in-task events[] log also grows independently and is
    excluded for the same reason.
    """
    task = schema.make_task(title="p", workstream="ws", agent="a")
    task["status"] = "active"
    remote.upload_json(task, remote.task_remote_path(task["id"]), backend=coord_backend)
    # Snapshot event is identical EXCEPT for volatile fields — must NOT be drift.
    snap = dict(task)
    snap["updated_at"] = "2099-01-01T00:00:00.000000Z"
    snap["last_touched_by"] = "someone-else"
    eventlog.append_event(events.make_event(family="tasks", task_id=task["id"],
                          kind="update", actor="a", payload=snap), backend=coord_backend)
    report = cli._event_parity_check(backend=coord_backend)
    assert report["drift"] == 0
