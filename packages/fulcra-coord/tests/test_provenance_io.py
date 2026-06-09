"""io._cache_remote_task records read provenance (root cause A, Step 2).

The read funnel must record, per task_id, where the body it returned came from
so the write path can recover from a lagging fold (A2). These tests pin:

* events-mode complete-fold read writes source=fold with a deep-copied
  fold_base (the CLEAN fold body, no _applied_event_count) and the FILE stat;
* file-mode read writes source=file with the file stat and no fold_base;
* the returned body is unchanged from prior behaviour;
* a provenance-write failure NEVER fails the read.
"""

from fulcra_coord import cache, eventlog, events, io, remote, schema


def _write_file_task(task, *, backend):
    remote.upload_json(task, remote.task_remote_path(task["id"]), backend=backend)


def _append_snapshot(task, *, backend):
    eventlog.append_event(
        events.make_event(family="tasks", task_id=task["id"], kind="start",
                          actor="a", payload=dict(task)),
        backend=backend,
    )


def test_events_complete_fold_records_source_fold(monkeypatch, coord_backend):
    monkeypatch.setenv("FULCRA_COORD_READ_SOURCE", "events")
    task = schema.make_task(title="folded", workstream="ws", agent="a")
    _write_file_task({**task, "current_summary": "FILE-BODY"}, backend=coord_backend)
    _append_snapshot({**task, "current_summary": "FOLD-BODY"}, backend=coord_backend)

    got = io._cache_remote_task(task["id"], backend=coord_backend)
    assert got["current_summary"] == "FOLD-BODY"

    prov = cache.read_provenance(task["id"])
    assert prov is not None
    assert prov["source"] == "fold"
    assert prov["fold_complete"] is True
    # The fold_base is the CLEAN fold body, no bookkeeping leak.
    assert prov["fold_base"]["current_summary"] == "FOLD-BODY"
    assert "_applied_event_count" not in prov["fold_base"]
    # The FILE stat (not the fold) is recorded for the concurrency baseline.
    assert prov["file_stat_at_read"] is not None


def test_events_fold_base_is_deep_copy(monkeypatch, coord_backend):
    """Mutating the returned body must not retro-alter the stored fold_base."""
    monkeypatch.setenv("FULCRA_COORD_READ_SOURCE", "events")
    task = schema.make_task(title="deepcopy", workstream="ws", agent="a")
    _write_file_task({**task, "current_summary": "FILE-BODY"}, backend=coord_backend)
    _append_snapshot({**task, "current_summary": "FOLD-BODY"}, backend=coord_backend)

    got = io._cache_remote_task(task["id"], backend=coord_backend)
    got["current_summary"] = "MUTATED-AFTER-READ"

    prov = cache.read_provenance(task["id"])
    assert prov["fold_base"]["current_summary"] == "FOLD-BODY"


def test_file_mode_records_source_file(monkeypatch, coord_backend):
    monkeypatch.delenv("FULCRA_COORD_READ_SOURCE", raising=False)
    task = schema.make_task(title="from-file", workstream="ws", agent="a")
    _write_file_task({**task, "current_summary": "FILE-BODY"}, backend=coord_backend)

    got = io._cache_remote_task(task["id"], backend=coord_backend)
    assert got["current_summary"] == "FILE-BODY"

    prov = cache.read_provenance(task["id"])
    assert prov is not None
    assert prov["source"] == "file"
    assert prov["fold_base"] is None
    assert prov["fold_complete"] is False
    assert prov["file_stat_at_read"] is not None


def test_events_fallback_to_file_records_source_file(monkeypatch, coord_backend):
    """Events mode, but fold incomplete (delta-only) -> file fallback => source=file."""
    monkeypatch.setenv("FULCRA_COORD_READ_SOURCE", "events")
    task = schema.make_task(title="delta-only", workstream="ws", agent="a")
    _write_file_task({**task, "current_summary": "FILE-BODY"}, backend=coord_backend)
    eventlog.append_event(
        events.make_event(family="tasks", task_id=task["id"], kind="update",
                          actor="a", payload={"current_summary": "DELTA"}),
        backend=coord_backend,
    )
    got = io._cache_remote_task(task["id"], backend=coord_backend)
    assert got["current_summary"] == "FILE-BODY"
    prov = cache.read_provenance(task["id"])
    assert prov["source"] == "file"
    assert prov["fold_base"] is None


def test_provenance_write_failure_does_not_fail_read(monkeypatch, coord_backend):
    monkeypatch.setenv("FULCRA_COORD_READ_SOURCE", "events")
    task = schema.make_task(title="provfail", workstream="ws", agent="a")
    _write_file_task({**task, "current_summary": "FILE-BODY"}, backend=coord_backend)
    _append_snapshot({**task, "current_summary": "FOLD-BODY"}, backend=coord_backend)

    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(cache, "write_provenance", _boom)
    # The read must still succeed and return the fold body.
    got = io._cache_remote_task(task["id"], backend=coord_backend)
    assert got["current_summary"] == "FOLD-BODY"
