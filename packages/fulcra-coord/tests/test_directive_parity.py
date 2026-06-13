"""Reconcile directive-parity check tests (Phase 3b Task 4).

Task 4 adds ``cli._directive_parity_check`` — a REPORT-ONLY reconcile sub-pass
that folds each first-class ``directives/<id>.json`` record against its back-ref
task and reports drift as health debt, mirroring ``_event_parity_check``'s
best-effort posture. The directive store is NOT authoritative (nothing reads it
for correctness), so drift is REPORTED, never acted on, and a failure here can
NEVER change reconcile's exit code or mutate anything.

Properties under test:

1. TOP-LEVEL-ONLY filter (load-bearing). The directives prefix now contains
   SUB-LOG SUBTREES — ``directives/<id>/acks/<agent>.json`` and
   ``directives/<id>/routing/<event_id>.json`` shards live UNDER the same prefix
   as the top-level ``directives/<id>.json`` records. The parity check MUST count
   ONLY the top-level records (a path that, after stripping the prefix, has NO
   further ``/`` AND ends in ``.json``). Without the filter the ack/route shards
   would be mis-counted as directive records and report massive false drift.

2. Consistent: a directive matching its back-ref task -> 0 drift.

3. Field drift: a stored directive whose audience/status disagrees with the
   task's current assignee/status -> flagged in ``drift_task_ids``.

4. Ack drift: a stored directive MISSING an acker the task / ack sub-log has ->
   flagged (the meaningful drift — the snapshot must never be missing an acker).

5. Orphan/gate: a directive with no ``task_id``, or whose back-ref task is gone,
   is skipped (not crashed) and doesn't inflate drift.

6. Wiring: a reconcile populates ``record["directive_parity"]``; a
   ``_directive_parity_check`` exception NEVER changes the reconcile exit code.

Written test-first (TDD): these FAIL before ``_directive_parity_check`` is added
to cli.py, and PASS once the function and its wiring are in place.
"""

from unittest import mock

from fulcra_coord import cli, directives, remote, schema


def _seed_directive_task(
    backend,
    *,
    task_id="TASK-DP",
    owner="alice",
    assignee="bob",
    status="proposed",
    workstream="ws",
    acked_by=None,
):
    """Create a directive-task, store it as the authoritative task body, and
    dual-write its first-class directive mirror. Returns (task, directive)."""
    task = schema.make_task(
        title="do the thing", workstream=workstream, agent=owner,
        owner_agent=owner, assignee=assignee, task_id=task_id,
    )
    task["status"] = status
    if acked_by:
        task["acked_by"] = list(acked_by)
    remote.upload_json(task, remote.task_remote_path(task_id), backend=backend)
    directive = directives.directive_from_task(task)
    remote.upload_json(
        directive, remote.directive_remote_path(directive["id"]), backend=backend
    )
    return task, directive


# ---------------------------------------------------------------------------
# 1. TOP-LEVEL-ONLY filter (load-bearing — would over-count without it)
# ---------------------------------------------------------------------------

def test_directive_parity_counts_top_level_records_only(coord_backend):
    """A directive record with ack + routing SUB-LOG shards under the same prefix
    must be counted as ONE record, not three. The check enumerates only top-level
    ``directives/<id>.json`` records (no further '/' after the prefix), so the
    ack/route shards are NEVER mis-counted as directive records.

    This is the load-bearing filter from the Task 2 carry-forward: without it,
    every ack/route shard would inflate ``checked`` and produce massive false
    drift (a shard is not a directive and has no task_id back-ref shape)."""
    task, directive = _seed_directive_task(
        coord_backend, task_id="TASK-FILTER", acked_by=["bob"],
    )
    # Seed sub-log shards UNDER the same directives prefix. The ack shard mirrors
    # the task's own ack (bob), so the stored record (which carried bob's ack via
    # the task) stays consistent with the expected mirror — the point of THIS test
    # is the top-level filter, not ack drift.
    directives.write_directive_ack(directive["id"], "bob", backend=coord_backend)
    directives.append_directive_route(
        directive["id"], {"event_id": "e1", "at": "2026-01-01T00:00:00Z"},
        backend=coord_backend,
    )
    # Sanity: list_files returns the record AND both shards under the prefix.
    all_files = remote.list_files(remote.directives_prefix(), backend=coord_backend)
    assert len(all_files) == 3  # record + ack shard + route shard

    report = cli._directive_parity_check(backend=coord_backend)
    # Exactly ONE directive record counted (the shards are filtered out).
    assert report["checked"] == 1
    # A consistent record (acked_by includes bob via the sub-log) -> zero drift.
    assert report["drift"] == 0
    assert report["drift_task_ids"] == []


# ---------------------------------------------------------------------------
# 2. Consistent: directive matches its back-ref task -> 0 drift
# ---------------------------------------------------------------------------

def test_directive_parity_zero_drift_when_consistent(coord_backend):
    """A directive whose stored fields match the recomputed mirror of its back-ref
    task contributes zero drift."""
    _seed_directive_task(coord_backend, task_id="TASK-OK", status="proposed")
    report = cli._directive_parity_check(backend=coord_backend)
    assert report["checked"] == 1
    assert report["drift"] == 0
    assert report["drift_task_ids"] == []


# ---------------------------------------------------------------------------
# 3. Field drift: stored audience/status disagrees with the task
# ---------------------------------------------------------------------------

def test_directive_parity_flags_audience_drift(coord_backend):
    """A stored directive whose audience disagrees with the task's current
    assignee is flagged in drift_task_ids."""
    task, directive = _seed_directive_task(
        coord_backend, task_id="TASK-AUD", assignee="bob",
    )
    # The task is later re-assigned to a different agent, but the stored directive
    # record was never re-mirrored -> stored audience ("bob") drifts from the
    # task's current assignee ("carol").
    task["assignee"] = "carol"
    remote.upload_json(task, remote.task_remote_path("TASK-AUD"), backend=coord_backend)
    report = cli._directive_parity_check(backend=coord_backend)
    assert report["drift"] >= 1
    assert "TASK-AUD" in report["drift_task_ids"]


def test_directive_parity_flags_status_drift(coord_backend):
    """A stored directive whose mapped status disagrees with the task's current
    status (proposed-stored vs active-task -> acted) is flagged."""
    task, directive = _seed_directive_task(
        coord_backend, task_id="TASK-ST", status="proposed",
    )
    # The task moved to active (-> directive status "acted") but the stored record
    # still reads "proposed".
    task["status"] = "active"
    remote.upload_json(task, remote.task_remote_path("TASK-ST"), backend=coord_backend)
    report = cli._directive_parity_check(backend=coord_backend)
    assert report["drift"] >= 1
    assert "TASK-ST" in report["drift_task_ids"]


# ---------------------------------------------------------------------------
# 4. Ack drift: stored record MISSING an acker the task / sub-log has
# ---------------------------------------------------------------------------

def test_directive_parity_flags_ack_drift_from_task(coord_backend):
    """A stored directive whose acked_by is MISSING an acker present in the task's
    inbox_ack (task.acked_by) is flagged. The directive snapshot must never be
    missing an acker the task/sub-log holds — that's the meaningful drift."""
    # Seed a stored directive with NO acks, then the task gains an ack later.
    task, directive = _seed_directive_task(
        coord_backend, task_id="TASK-ACKT", status="proposed",
    )
    assert directive["acked_by"] == []  # stored record has no acks
    task["acked_by"] = ["bob"]
    remote.upload_json(task, remote.task_remote_path("TASK-ACKT"), backend=coord_backend)
    report = cli._directive_parity_check(backend=coord_backend)
    assert report["drift"] >= 1
    assert "TASK-ACKT" in report["drift_task_ids"]


def test_directive_parity_flags_ack_drift_from_sublog(coord_backend):
    """A stored directive whose acked_by is MISSING an acker present in the durable
    ack SUB-LOG is flagged (the union dual_write folds in)."""
    task, directive = _seed_directive_task(
        coord_backend, task_id="TASK-ACKS", status="proposed",
    )
    assert directive["acked_by"] == []
    # An ack lands in the durable sub-log but the stored snapshot was never
    # re-written to fold it in.
    directives.write_directive_ack(directive["id"], "bob", backend=coord_backend)
    report = cli._directive_parity_check(backend=coord_backend)
    assert report["drift"] >= 1
    assert "TASK-ACKS" in report["drift_task_ids"]


def test_directive_parity_no_ack_drift_when_record_has_acker(coord_backend):
    """A stored directive whose acked_by already CONTAINS the task's acker is not
    flagged for ack drift (the snapshot is a superset, which is fine)."""
    task, directive = _seed_directive_task(
        coord_backend, task_id="TASK-ACKOK", status="proposed", acked_by=["bob"],
    )
    assert directive["acked_by"] == ["bob"]  # task ack carried onto the mirror
    report = cli._directive_parity_check(backend=coord_backend)
    assert "TASK-ACKOK" not in report["drift_task_ids"]


def test_directive_parity_deadline_bounds_ack_reads():
    """A reconcile-supplied deadline must bound durable ack sub-log reads."""
    import time

    task = schema.make_task(
        title="bounded", workstream="ws", agent="alice",
        owner_agent="alice", assignee="bob", task_id="TASK-DEADLINE",
    )
    stored = directives.directive_from_task(task)
    seen_timeouts = []

    def fake_read_acks(directive_id, *, backend=None, timeout=None):
        seen_timeouts.append(timeout)
        return []

    with mock.patch.object(directives, "read_directive_acks",
                           side_effect=fake_read_acks):
        report = cli._directive_parity_check(
            records=[stored], all_tasks=[task],
            deadline=time.monotonic() + 3.0,
        )

    assert report["checked"] == 1
    assert report["deferred"] == 0
    assert seen_timeouts
    assert 1.0 <= seen_timeouts[0] <= 2.0


def test_directive_parity_deadline_defers_before_unbounded_ack_read():
    """Near deadline, parity reports deferral instead of starting ack I/O."""
    import time

    task = schema.make_task(
        title="deferred", workstream="ws", agent="alice",
        owner_agent="alice", assignee="bob", task_id="TASK-DEFER",
    )
    stored = directives.directive_from_task(task)

    with mock.patch.object(directives, "read_directive_acks") as read_acks:
        report = cli._directive_parity_check(
            records=[stored], all_tasks=[task],
            deadline=time.monotonic() + 1.0,
        )

    assert report["checked"] == 0
    assert report["deferred"] == 1
    read_acks.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Orphan / gate: no task_id, or back-ref task gone -> skipped, not crashed
# ---------------------------------------------------------------------------

def test_directive_parity_skips_directive_with_no_task_id(coord_backend):
    """A directive record with no task_id back-ref can't be compared to a task ->
    skipped (gate), not counted as drift, not crashed."""
    # A directly-authored directive (no task_id back-ref).
    directive = schema.make_directive(
        directive_type="tell", from_agent="alice", audience="bob",
        title="orphan", workstream="ws",
    )
    assert directive.get("task_id") is None
    remote.upload_json(
        directive, remote.directive_remote_path(directive["id"]), backend=coord_backend
    )
    report = cli._directive_parity_check(backend=coord_backend)
    assert report["drift"] == 0
    assert report["drift_task_ids"] == []
    # The orphan is not compared (gated on missing task_id).
    assert report["checked"] == 0


def test_directive_parity_skips_when_backref_task_gone(coord_backend):
    """A directive whose back-ref task no longer exists is skipped (not crashed)
    and does not inflate drift."""
    _seed_directive_task(coord_backend, task_id="TASK-GONE", status="proposed")
    # Delete the authoritative task body so the back-ref dangles.
    remote.delete(remote.task_remote_path("TASK-GONE"), backend=coord_backend)
    report = cli._directive_parity_check(backend=coord_backend)
    # Either skipped or counted as an orphan, but never drift and never a crash.
    assert report["drift"] == 0
    assert "TASK-GONE" not in report["drift_task_ids"]


# ---------------------------------------------------------------------------
# 6. Wiring: reconcile populates record["directive_parity"]; an exception in the
#    check never changes the reconcile exit code.
# ---------------------------------------------------------------------------

def test_reconcile_populates_directive_parity(coord_backend):
    """A reconcile run folds the directive-parity block into the health record.

    Captures every health-record upload (the path under the health prefix) and
    asserts the persisted record carries the ``directive_parity`` block."""
    captured = {}
    real_upload = remote.upload_json

    def _capture(record, path, **kwargs):
        if "/health/" in path and isinstance(record, dict):
            captured["record"] = record
        return real_upload(record, path, **kwargs)

    _seed_directive_task(coord_backend, task_id="TASK-RECON", status="proposed")
    with mock.patch.object(remote, "upload_json", side_effect=_capture):
        rc = cli.cmd_reconcile(mock.Mock(), backend=coord_backend)
    assert rc == 0
    assert "record" in captured, "no health record was uploaded"
    assert "directive_parity" in captured["record"]
    assert "checked" in captured["record"]["directive_parity"]


def test_reconcile_exit_code_unaffected_by_directive_parity_exception(coord_backend):
    """If _directive_parity_check raises, reconcile must STILL return 0 and the
    directive_parity block is simply absent (best-effort / report-only)."""
    _seed_directive_task(coord_backend, task_id="TASK-RAISE", status="proposed")
    with mock.patch.object(
        cli, "_directive_parity_check", side_effect=RuntimeError("boom")
    ):
        rc = cli.cmd_reconcile(mock.Mock(), backend=coord_backend)
    assert rc == 0
