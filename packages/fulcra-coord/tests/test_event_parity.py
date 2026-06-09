"""Reconcile event-parity check tests (Task 6).

Two properties under test:

1. Zero drift when the event fold's status matches the mutable snapshot —
   confirms the parity check does NOT false-positive on a consistent bus.

2. Drift is flagged when the event fold disagrees with the snapshot status —
   confirms the parity check actually catches inconsistencies.

Written test-first (TDD): both tests FAIL before _event_parity_check is added
to cli.py, and PASS once the function and its wiring are in place.
"""

from fulcra_coord import cli, events, eventlog, remote


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
