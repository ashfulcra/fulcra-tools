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


# ---------------------------------------------------------------------------
# Root cause C1 — ack-divergence detection (report-only)
#
# The AUTHORITATIVE ack set for a task is summaries.acked_by — io.py unions
# prior acks into each summary because the in-task event log is truncated to
# MAX_EVENTS_INLINE, and inbox._ack_summary_only records an ack with NO event
# shard at all. So the FOLD can be MISSING durable acks the summaries view has.
# Parity must SURFACE that loss (otherwise a flip to events-as-source would
# re-notify already-acked directives).
# ---------------------------------------------------------------------------

def test_parity_flags_ack_drift_when_summaries_has_extra_acker(coord_backend):
    """C1a: fold is complete and matches the file, but the summaries view carries
    an acker the fold never saw -> ack drift must be surfaced AND fold the task
    into drift_task_ids so the flip-readiness gate trips."""
    task = schema.make_task(title="p", workstream="ws", agent="a")
    task["status"] = "active"
    remote.upload_json(task, remote.task_remote_path(task["id"]), backend=coord_backend)
    # Snapshot event matches the file exactly (no field drift), and carries no acks.
    eventlog.append_event(events.make_event(family="tasks", task_id=task["id"],
                          kind="start", actor="a", payload=dict(task)), backend=coord_backend)
    # Authoritative summaries view records an ack the fold is missing.
    remote.upload_json({"summaries": [{"id": task["id"], "acked_by": ["agent-x"]}]},
                       remote.view_remote_path("summaries"), backend=coord_backend)
    report = cli._event_parity_check(backend=coord_backend)
    assert report["ack_drift"] >= 1
    assert task["id"] in report["ack_drift_task_ids"]
    assert task["id"] in report["drift_task_ids"]
    assert report["drift"] >= 1


def test_parity_no_ack_drift_when_fold_has_all_durable_acks(coord_backend):
    """C1b: the fold's acked_by is a superset of the summaries acked_by -> the fold
    has every durable ack, so no ack drift is contributed."""
    task = schema.make_task(title="p", workstream="ws", agent="a")
    task["status"] = "active"
    task["acked_by"] = ["agent-x", "agent-y"]
    remote.upload_json(task, remote.task_remote_path(task["id"]), backend=coord_backend)
    eventlog.append_event(events.make_event(family="tasks", task_id=task["id"],
                          kind="start", actor="a", payload=dict(task)), backend=coord_backend)
    remote.upload_json({"summaries": [{"id": task["id"], "acked_by": ["agent-x"]}]},
                       remote.view_remote_path("summaries"), backend=coord_backend)
    report = cli._event_parity_check(backend=coord_backend)
    assert report["ack_drift"] == 0
    assert task["id"] not in report["ack_drift_task_ids"]


def test_parity_no_crash_when_summaries_view_absent(coord_backend):
    """C1c: no summaries view present (download returns None) -> no crash, no ack
    drift flagged. The check must degrade gracefully on an old / missing bus view."""
    task = schema.make_task(title="p", workstream="ws", agent="a")
    task["status"] = "active"
    remote.upload_json(task, remote.task_remote_path(task["id"]), backend=coord_backend)
    eventlog.append_event(events.make_event(family="tasks", task_id=task["id"],
                          kind="start", actor="a", payload=dict(task)), backend=coord_backend)
    # No summaries view uploaded.
    report = cli._event_parity_check(backend=coord_backend)
    assert report["ack_drift"] == 0
    assert report["ack_drift_task_ids"] == []


# ---------------------------------------------------------------------------
# Root cause C2 — safe delta-only field broadening
#
# When the fold is NOT complete (legacy delta-only task, no snapshot), parity
# used to compare ONLY status, missing drift in every other delta-carried
# field. We broaden to compare all fields the fold ACTUALLY carries, but ONLY
# those — a field the fold never saw can't false-positive.
# ---------------------------------------------------------------------------

def test_parity_delta_only_flags_non_status_field_drift(coord_backend):
    """C2a: delta-only task (events but NO snapshot) where a NON-status field the
    fold carries differs from the file -> now flagged as drift (status-only missed it)."""
    task = {"id": "TASK-C2A", "title": "t", "status": "active", "current_summary": "FILE-VALUE"}
    remote.upload_json(task, remote.task_remote_path("TASK-C2A"), backend=coord_backend)
    # Delta events only (no snapshot -> fold_is_complete False). status agrees,
    # but current_summary disagrees with the file.
    for kind, p in [("start", {"status": "active"}),
                    ("update", {"current_summary": "FOLD-VALUE"})]:
        eventlog.append_event(events.make_event(family="tasks", task_id="TASK-C2A",
                              kind=kind, actor="a", payload=p), backend=coord_backend)
    report = cli._event_parity_check(backend=coord_backend)
    assert report["drift"] >= 1
    assert "TASK-C2A" in report["drift_task_ids"]


def test_parity_delta_only_no_false_positive_on_unseen_field(coord_backend):
    """C2b: delta-only task where the file has an EXTRA field the fold never saw,
    but all fold-carried fields match -> NO drift (broadening must not over-flag)."""
    task = {"id": "TASK-C2B", "title": "t", "status": "active",
            "current_summary": "same", "extra_only_in_file": "ignored"}
    remote.upload_json(task, remote.task_remote_path("TASK-C2B"), backend=coord_backend)
    for kind, p in [("start", {"status": "active"}),
                    ("update", {"current_summary": "same"})]:
        eventlog.append_event(events.make_event(family="tasks", task_id="TASK-C2B",
                              kind=kind, actor="a", payload=p), backend=coord_backend)
    report = cli._event_parity_check(backend=coord_backend)
    assert "TASK-C2B" not in report["drift_task_ids"]


def test_parity_dedups_task_drifting_on_both_field_and_ack(coord_backend):
    """C1+drift dedup: a task that drifts on BOTH a field AND an ack appears
    EXACTLY ONCE in drift_task_ids, and drift == len(drift_task_ids)."""
    task = schema.make_task(title="p", workstream="ws", agent="a")
    task["status"] = "active"
    task["current_summary"] = "FILE-VALUE"
    remote.upload_json(task, remote.task_remote_path(task["id"]), backend=coord_backend)
    # Snapshot event disagrees with the file on current_summary (field drift).
    snap = dict(task)
    snap["current_summary"] = "FOLD-VALUE"
    eventlog.append_event(events.make_event(family="tasks", task_id=task["id"],
                          kind="start", actor="a", payload=snap), backend=coord_backend)
    # And summaries carries an ack the fold is missing (ack drift).
    remote.upload_json({"summaries": [{"id": task["id"], "acked_by": ["agent-x"]}]},
                       remote.view_remote_path("summaries"), backend=coord_backend)
    report = cli._event_parity_check(backend=coord_backend)
    assert report["drift_task_ids"].count(task["id"]) == 1
    assert report["drift"] == len(report["drift_task_ids"])
