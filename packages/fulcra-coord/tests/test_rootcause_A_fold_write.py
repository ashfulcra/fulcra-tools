"""End-to-end fold-write soundness in events-mode (root cause A, Step 5).

These pin the integrated write path: in events-mode (FULCRA_COORD_READ_SOURCE=
events), a command edits a body reconstructed from a possibly-LAGGING fold; the
write must NOT clobber newer file fields the fold doesn't carry (root cause A2),
must surface a genuine status conflict instead of silently picking, must never
shrink acked_by, and must NEVER leak the provenance hand-off into any persisted
artifact.

The flow each test sets up:
  1. seed the mutable file (theirs) and an event log whose fold is the read body;
  2. read via io._cache_remote_task under events mode -> records provenance
     (source=fold, fold_base=<fold>);
  3. mutate the returned body (the command's edit) and write it;
  4. assert the merged, uploaded body.

Plus an A1 immutability test: event_id is minted once at append and never
re-minted during read/fold/retention.
"""

import copy

from fulcra_coord import cache, eventlog, events, io, remote, schema, writepipe
import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _seed_file(task, *, backend):
    remote.upload_json(task, remote.task_remote_path(task["id"]), backend=backend)


def _seed_snapshot(task, *, backend):
    """Append a full-task snapshot event (the fold body)."""
    eventlog.append_event(
        events.make_event(family="tasks", task_id=task["id"], kind="start",
                          actor="a", payload=copy.deepcopy(task)),
        backend=backend,
    )


def _read_in_events_mode(task_id, monkeypatch, backend):
    monkeypatch.setenv("FULCRA_COORD_READ_SOURCE", "events")
    return io._cache_remote_task(task_id, backend=backend)


# ---------------------------------------------------------------------------
# 1. newer file scalar the stale fold lacks + a different local edit -> BOTH survive
# ---------------------------------------------------------------------------

def test_stale_fold_does_not_clobber_newer_file_field(coord_backend, monkeypatch):
    t = schema.make_task(title="t", workstream="ws", agent="a")
    t["status"] = "active"
    t["current_summary"] = "ORIG-SUMMARY"
    t["next_action"] = "ORIG-NA"
    # The FOLD lags: snapshot reflects the ORIGINAL state.
    _seed_snapshot(t, backend=coord_backend)
    # The FILE is NEWER on a field the fold doesn't carry the new value for.
    file_body = copy.deepcopy(t)
    file_body["next_action"] = "FILE-NEW-NA"   # remote advanced next_action
    file_body["updated_at"] = "2099-01-01T00:00:00.000000Z"
    _seed_file(file_body, backend=coord_backend)

    # Read folds the (stale) snapshot; the command edits a DIFFERENT scalar.
    body = _read_in_events_mode(t["id"], monkeypatch, coord_backend)
    body["current_summary"] = "MY-EDIT"

    assert writepipe._write_task_and_views(
        body, backend=coord_backend, command="update") is True

    written = remote.download_json(remote.task_remote_path(t["id"]), backend=coord_backend)
    # BOTH survive: the recovered newer file field AND my edit.
    assert written["next_action"] == "FILE-NEW-NA"
    assert written["current_summary"] == "MY-EDIT"


# ---------------------------------------------------------------------------
# 2. remote-only status change (file) + local edits summary -> remote status wins
# ---------------------------------------------------------------------------

def test_remote_status_change_survives_stale_fold_write(coord_backend, monkeypatch):
    t = schema.make_task(title="t", workstream="ws", agent="a")
    t["status"] = "active"
    t["current_summary"] = "ORIG"
    _seed_snapshot(t, backend=coord_backend)   # fold says active
    file_body = copy.deepcopy(t)
    file_body["status"] = "done"               # remote moved to done
    file_body["updated_at"] = "2099-01-01T00:00:00.000000Z"
    _seed_file(file_body, backend=coord_backend)

    body = _read_in_events_mode(t["id"], monkeypatch, coord_backend)
    body["current_summary"] = "MY-NOTE"        # local only edits summary

    assert writepipe._write_task_and_views(
        body, backend=coord_backend, command="update") is True

    written = remote.download_json(remote.task_remote_path(t["id"]), backend=coord_backend)
    assert written["status"] == "done"          # remote transition preserved
    assert written["current_summary"] == "MY-NOTE"


# ---------------------------------------------------------------------------
# 3. both local + remote independently change status -> ConflictError (no silent pick)
# ---------------------------------------------------------------------------

def test_both_change_status_raises_conflict(coord_backend, monkeypatch):
    t = schema.make_task(title="t", workstream="ws", agent="a")
    t["status"] = "active"
    _seed_snapshot(t, backend=coord_backend)   # fold says active
    file_body = copy.deepcopy(t)
    file_body["status"] = "blocked"            # remote moved to blocked
    file_body["updated_at"] = "2099-01-01T00:00:00.000000Z"
    _seed_file(file_body, backend=coord_backend)

    body = _read_in_events_mode(t["id"], monkeypatch, coord_backend)
    body["status"] = "done"                    # local moved to done

    with pytest.raises((schema.ConflictError, schema.NeedsReconcile)):
        writepipe._write_task_and_views(body, backend=coord_backend, command="done")


# ---------------------------------------------------------------------------
# 4. fold missing a file ack -> acked_by is the UNION (never shrinks)
# ---------------------------------------------------------------------------

def test_acked_by_union_preserves_file_ack(coord_backend, monkeypatch):
    t = schema.make_task(title="t", workstream="ws", agent="a")
    t["status"] = "active"
    t["acked_by"] = ["agent-1"]
    _seed_snapshot(t, backend=coord_backend)   # fold ack set: {agent-1}
    file_body = copy.deepcopy(t)
    file_body["acked_by"] = ["agent-1", "agent-2"]   # file also has agent-2
    file_body["updated_at"] = "2099-01-01T00:00:00.000000Z"
    _seed_file(file_body, backend=coord_backend)

    body = _read_in_events_mode(t["id"], monkeypatch, coord_backend)
    body["current_summary"] = "touch"

    assert writepipe._write_task_and_views(
        body, backend=coord_backend, command="update") is True

    written = remote.download_json(remote.task_remote_path(t["id"]), backend=coord_backend)
    assert set(written["acked_by"]) == {"agent-1", "agent-2"}


# ---------------------------------------------------------------------------
# 5. file-sourced fallback under events mode -> existing stat-change path, NOT 3-way
# ---------------------------------------------------------------------------

def test_file_sourced_write_uses_stat_path_not_three_way(coord_backend, monkeypatch):
    monkeypatch.setenv("FULCRA_COORD_READ_SOURCE", "events")
    t = schema.make_task(title="t", workstream="ws", agent="a")
    t["status"] = "active"
    t["current_summary"] = "FILE-BODY"
    _seed_file(t, backend=coord_backend)
    # Delta-only event stream -> fold incomplete -> read FALLS BACK to file.
    eventlog.append_event(
        events.make_event(family="tasks", task_id=t["id"], kind="update",
                          actor="a", payload={"current_summary": "DELTA"}),
        backend=coord_backend,
    )
    body = io._cache_remote_task(t["id"], backend=coord_backend)
    assert body["current_summary"] == "FILE-BODY"

    # Provenance must record source=file (the fallback path), so the write uses
    # the existing stat-change merge check, not the forced 3-way.
    prov = cache.read_provenance(t["id"])
    assert prov["source"] == "file"

    # A no-op write (nothing changed remotely since read) must NOT spuriously
    # conflict — the stat is unchanged so the merge check is skipped entirely.
    assert writepipe._write_task_and_views(
        body, backend=coord_backend, command="update") is True


# ---------------------------------------------------------------------------
# 6. provenance is NOT persisted into any remote artifact
# ---------------------------------------------------------------------------

# The provenance-ONLY keys. NOTE: ``source`` is deliberately NOT in this set —
# a task legitimately carries a ``source{}`` dict (channel/message_id/...), so a
# bare key-name check would false-positive on every real task. The provenance
# leak we guard against is the sidecar dict's OWN shape, so we additionally
# assert no dict ever carries the full provenance signature.
_PROV_ONLY_KEYS = {"fold_base", "file_stat_at_read", "fold_complete"}


def _assert_no_prov_keys(obj, where):
    if isinstance(obj, dict):
        leaked = _PROV_ONLY_KEYS & set(obj.keys())
        assert not leaked, f"provenance keys {leaked} leaked into {where}"
        # A provenance sidecar dict has source as a STRING plus fold_complete;
        # the task's own source is a DICT — so flag the sidecar signature only.
        assert not (
            isinstance(obj.get("source"), str)
            and "fold_complete" in obj
        ), f"provenance sidecar shape leaked into {where}"
        for v in obj.values():
            _assert_no_prov_keys(v, where)
    elif isinstance(obj, list):
        for v in obj:
            _assert_no_prov_keys(v, where)


def test_provenance_never_persisted_to_remote(coord_backend, monkeypatch):
    t = schema.make_task(title="t", workstream="ws", agent="a")
    t["status"] = "active"
    _seed_snapshot(t, backend=coord_backend)
    file_body = copy.deepcopy(t)
    file_body["next_action"] = "FILE-NEW"
    file_body["updated_at"] = "2099-01-01T00:00:00.000000Z"
    _seed_file(file_body, backend=coord_backend)

    body = _read_in_events_mode(t["id"], monkeypatch, coord_backend)
    body["current_summary"] = "edit"
    assert writepipe._write_task_and_views(
        body, backend=coord_backend, command="update") is True

    # The task file itself.
    written = remote.download_json(remote.task_remote_path(t["id"]), backend=coord_backend)
    _assert_no_prov_keys(written, "tasks/<id>.json")

    # Every view (index, active, next, summaries, search-index, ...).
    for name in ("index", "active", "next", "recently-done", "summaries",
                 "search-index"):
        v = remote.download_json(remote.view_remote_path(name), backend=coord_backend)
        if v is not None:
            _assert_no_prov_keys(v, f"views/{name}.json")

    # Every event shard's payload.
    for ev in eventlog.read_events(t["id"], backend=coord_backend):
        _assert_no_prov_keys(ev.get("payload") or {}, "event payload")


# ---------------------------------------------------------------------------
# 7. A1 immutability — event_id minted once, never re-minted at read/fold
# ---------------------------------------------------------------------------

def test_fold_is_deterministic_same_winner_twice(coord_backend):
    """Folding the same event set twice yields identical order + winner."""
    t = schema.make_task(title="t", workstream="ws", agent="a")
    t["status"] = "active"
    _seed_snapshot(t, backend=coord_backend)
    eventlog.append_event(
        events.make_event(family="tasks", task_id=t["id"], kind="update",
                          actor="a", payload={"current_summary": "later"}),
        backend=coord_backend,
    )
    evs = eventlog.read_events(t["id"], backend=coord_backend)
    f1 = events.fold_task(copy.deepcopy(evs))
    f2 = events.fold_task(copy.deepcopy(evs))
    assert f1 == f2
    assert f1.get("current_summary") == "later"


def test_fold_does_not_remint_event_id(coord_backend, monkeypatch):
    """fold_task must NOT call event_id — ids are immutable, minted at append.

    A1 soundness: the same-µs concurrent-write winner is arbitrary-but-STABLE
    only because event_id is generated once and never re-derived during
    read/fold/replay. If fold re-minted ids, the random suffix would reshuffle
    the tie-break every read and the winner would flap.
    """
    calls = {"n": 0}
    real_event_id = events.event_id

    def _counting_event_id(*a, **k):
        calls["n"] += 1
        return real_event_id(*a, **k)

    monkeypatch.setattr(events, "event_id", _counting_event_id)

    t = schema.make_task(title="t", workstream="ws", agent="a")
    _seed_snapshot(t, backend=coord_backend)
    evs = eventlog.read_events(t["id"], backend=coord_backend)
    calls["n"] = 0                 # reset after the append minted its id
    events.fold_task(evs)
    events.fold_task(evs)
    assert calls["n"] == 0, "fold_task must not mint event_id"
    # And the stored event_id is stable across reads.
    assert evs[0]["event_id"] == eventlog.read_events(t["id"], backend=coord_backend)[0]["event_id"]
