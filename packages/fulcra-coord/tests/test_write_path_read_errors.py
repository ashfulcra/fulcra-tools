"""Write path: transport READ FAILURE must never be conflated with ABSENCE.

The 2026-06-11 blind adversarial audit found a systemic class across the write
pipeline: the transport primitives (``fulcra_coord_files.store``) intentionally
collapse "remote says the file is absent" and "the read failed" into the same
None/[] — and three write-path call sites treated that None as ABSENCE, turning
one transient 504 into a destructive write:

  * F1 ``writepipe._write_task_and_views``: a pre-stat None was read as "new
    task, skip the merge check" — so an agent holding a stale body whose
    pre-stat 504'd would blind-LWW over a peer's just-landed ``done``.
    The fold-sourced branch had the same hole (download None == "file gone").
  * F2 ``io._load_summaries_for_rebuild``: an unreadable summaries aggregate
    was read as "older bus without the aggregate" -> fall back to
    ``_load_all_tasks`` -> which itself degrades to LOCAL CACHE ONLY when the
    index read fails -> a cold-cache host then uploads ALL views rebuilt from
    its partial cache with a fresh ``generated_at`` (the stale-view guard
    can't catch fresh-but-truncated), silently blanking the bus's read surface.
  * F3 ``cli.cmd_reconcile``: the same ``_load_all_tasks`` cache-only degrade
    fed the reconcile view rebuild — a thin-cache host's heartbeat tick could
    truncate the global views every 90s, forever.

The correct idiom already exists in one place (role_ops.read_role's READ_ERROR
sentinel, bug hunt C1): a failed read is disambiguated by a stat probe and
``store.probe_reachable`` before anyone is allowed to act on "absent". These
tests pin the same discipline at the three write-path sites.
"""

from __future__ import annotations

import json
import types
from unittest import mock

import pytest

from fulcra_coord import cache, cli, io as coord_io, remote, schema, writepipe


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _store_file(tmp_path, remote_path: str):
    """The fake store's local file for a remote path (FULCRA_FAKE_ROOT layout)."""
    return tmp_path / remote_path.lstrip("/")


def _read_store_json(tmp_path, remote_path: str):
    return json.loads(_store_file(tmp_path, remote_path).read_text())


def _make_task(title="t"):
    t = schema.make_task(title=title, workstream="ws", agent="agent-a")
    t["status"] = "active"
    return t


def _patch_download_none_for(monkeypatch, predicate):
    """remote.download_json -> None for paths matching ``predicate``; real
    otherwise. Models a targeted transport read failure (504 weather)."""
    real = remote.download_json

    def fake(path, **kw):
        if predicate(path):
            return None
        return real(path, **kw)

    monkeypatch.setattr(remote, "download_json", fake)


def _failed_marker_for(task_id):
    return [
        m for m in cache.list_op_markers()
        if m.get("task_id") == task_id and m.get("needs_reconcile")
    ]


# ===========================================================================
# F1 — writepipe pre-read: stat None is not proof of absence
# ===========================================================================

def test_f1_failed_pre_stat_with_readable_body_still_merges(coord_backend,
                                                            monkeypatch,
                                                            tmp_path):
    """THE F1 FAILURE SEQUENCE: agent A holds a stale ``active`` body; agent B
    lands ``done``; A runs ``update`` and A's pre-stat 504s (None). The old
    code read that None as "new task, skip merge check" and A's upload
    blind-reverted B's transition. The body IS readable — the write must
    re-download and merge, so B's ``done`` survives."""
    base = _make_task()
    done_by_b = schema.apply_transition(
        base, "done", by="agent-b",
        evidence="shipped", verification_level="agent-verified")
    remote.upload_json(done_by_b, remote.task_remote_path(base["id"]),
                       backend=coord_backend)

    # A's stale read-modify-write: a field edit on the pre-done body, with a
    # NEWER updated_at than B's write (the LWW trap).
    a_local = dict(base)
    a_local["current_summary"] = "A-progress-note"
    a_local["updated_at"] = "2099-01-01T00:00:00.000000Z"
    cache.write_cached_task(a_local)

    # Script ONLY the pre-stat to fail (one 504); every later stat is real.
    real_stat = remote.stat
    task_path = remote.task_remote_path(base["id"])
    calls = {"n": 0}

    def flaky_pre_stat(path, **kw):
        if path == task_path:
            calls["n"] += 1
            if calls["n"] == 1:
                return None  # the 504'd pre-stat
        return real_stat(path, **kw)

    monkeypatch.setattr(remote, "stat", flaky_pre_stat)

    ok = writepipe._write_task_and_views(
        a_local, backend=coord_backend, command="update")
    assert ok is True

    written = _read_store_json(tmp_path, task_path)
    assert written["status"] == "done", \
        "a failed pre-stat must not skip the merge check — B's done was reverted"
    assert written["current_summary"] == "A-progress-note", \
        "the merge must still carry A's newer field edit"


def test_f1_unreadable_remote_fails_write_instead_of_blind_overwrite(
        coord_backend, monkeypatch, tmp_path):
    """Stat None + download None + bus NOT reachable: absence is unconfirmed
    and reads are failing — the write must FAIL (cached + needs_reconcile for
    the standard repair), never upload blind."""
    base = _make_task()
    done_by_b = schema.apply_transition(
        base, "done", by="agent-b",
        evidence="shipped", verification_level="agent-verified")
    task_path = remote.task_remote_path(base["id"])
    remote.upload_json(done_by_b, task_path, backend=coord_backend)
    before = _store_file(tmp_path, task_path).read_text()

    a_local = dict(base)
    a_local["current_summary"] = "A-progress-note"
    cache.write_cached_task(a_local)

    monkeypatch.setattr(remote, "stat", lambda path, **kw: None)
    _patch_download_none_for(monkeypatch, lambda p: p == task_path)
    monkeypatch.setattr(remote, "probe_reachable", lambda backend=None: False)

    ok = writepipe._write_task_and_views(
        a_local, backend=coord_backend, command="update")
    assert ok is False, "an unconfirmable pre-read must fail the write"
    assert _store_file(tmp_path, task_path).read_text() == before, \
        "the write blind-overwrote B's done while reads were failing"
    markers = _failed_marker_for(base["id"])
    assert markers and markers[0].get("status") == "failed", \
        f"expected a failed/needs_reconcile marker, got {cache.list_op_markers()}"
    assert cache.read_cached_task(base["id"]) is not None


def test_f1_reachable_bus_alone_does_not_confirm_absent_body(
        coord_backend, monkeypatch, tmp_path):
    """A reachable bus is not enough to prove a task body is absent when the
    task-path reads are failing. Re-stat the task path before taking the
    genuinely-new-task path; otherwise a transient stat/download failure while
    the bus is reachable still blind-overwrites an existing task."""
    base = _make_task()
    done_by_b = schema.apply_transition(
        base, "done", by="agent-b",
        evidence="shipped", verification_level="agent-verified")
    task_path = remote.task_remote_path(base["id"])
    remote.upload_json(done_by_b, task_path, backend=coord_backend)
    before = _store_file(tmp_path, task_path).read_text()

    a_local = dict(base)
    a_local["current_summary"] = "A-progress-note"
    cache.write_cached_task(a_local)

    real_stat = remote.stat
    calls = {"task_stat": 0}

    def flaky_initial_stat(path, **kw):
        if path == task_path:
            calls["task_stat"] += 1
            if calls["task_stat"] == 1:
                return None
        return real_stat(path, **kw)

    monkeypatch.setattr(remote, "stat", flaky_initial_stat)
    _patch_download_none_for(monkeypatch, lambda p: p == task_path)
    monkeypatch.setattr(remote, "probe_reachable", lambda backend=None: True)

    ok = writepipe._write_task_and_views(
        a_local, backend=coord_backend, command="update")
    assert ok is False
    assert calls["task_stat"] >= 2, \
        "absence must be confirmed by re-statting the task path"
    assert _store_file(tmp_path, task_path).read_text() == before, \
        "bus reachability alone was treated as absent and overwrote B's done"
    assert _failed_marker_for(base["id"])


def test_f1_stat_sees_file_but_body_unreadable_fails_write(coord_backend,
                                                           monkeypatch,
                                                           tmp_path):
    """Pre-stat SEES the file (it demonstrably exists) but the merge-check
    download fails: that is a READ_ERROR, not 'nothing to merge against' —
    the old code silently skipped the merge and uploaded blind."""
    base = _make_task()
    done_by_b = schema.apply_transition(
        base, "done", by="agent-b",
        evidence="shipped", verification_level="agent-verified")
    task_path = remote.task_remote_path(base["id"])
    remote.upload_json(done_by_b, task_path, backend=coord_backend)
    before = _store_file(tmp_path, task_path).read_text()

    a_local = dict(base)
    a_local["current_summary"] = "A-progress-note"
    cache.write_cached_task(a_local)

    # stat is real (sees the file); only the body download fails.
    _patch_download_none_for(monkeypatch, lambda p: p == task_path)

    ok = writepipe._write_task_and_views(
        a_local, backend=coord_backend, command="update")
    assert ok is False
    assert _store_file(tmp_path, task_path).read_text() == before, \
        "stat saw the file; an unreadable body must never be overwritten blind"
    assert _failed_marker_for(base["id"])


def test_f1_fold_sourced_write_with_unreadable_file_fails(coord_backend,
                                                          monkeypatch,
                                                          tmp_path):
    """The fold-sourced branch had the same conflation: download None was read
    as 'file gone, nothing to merge against' and the fold body was uploaded
    as-is — over a file that pre-stat proves EXISTS but could not be read."""
    base = _make_task()
    done_by_b = schema.apply_transition(
        base, "done", by="agent-b",
        evidence="shipped", verification_level="agent-verified")
    task_path = remote.task_remote_path(base["id"])
    remote.upload_json(done_by_b, task_path, backend=coord_backend)
    before = _store_file(tmp_path, task_path).read_text()

    a_local = dict(base)
    a_local["current_summary"] = "A-progress-note"
    cache.write_cached_task(a_local)
    cache.write_provenance(base["id"], {
        "source": "fold", "fold_complete": True,
        "fold_base": dict(base), "file_stat_at_read": None,
    })

    _patch_download_none_for(monkeypatch, lambda p: p == task_path)

    ok = writepipe._write_task_and_views(
        a_local, backend=coord_backend, command="update")
    assert ok is False
    assert _store_file(tmp_path, task_path).read_text() == before, \
        "fold-sourced write must not blind-LWW over an unreadable file"
    assert _failed_marker_for(base["id"])


def test_f1_confirmed_absent_new_task_writes_without_merge(coord_backend,
                                                           tmp_path):
    """The genuinely-new-task path stays a clean write: stat None + download
    None + bus reachable (all real against an empty fake store) == CONFIRMED
    absent — no merge check, no failure, the body lands."""
    t = _make_task()
    cache.write_cached_task(t)
    ok = writepipe._write_task_and_views(
        t, backend=coord_backend, command="update")
    assert ok is True
    written = _read_store_json(tmp_path, remote.task_remote_path(t["id"]))
    assert written["id"] == t["id"]
    assert written["status"] == "active"


# ===========================================================================
# F2 — _load_summaries_for_rebuild: unreadable aggregate != legacy bus
# ===========================================================================

def _seed_bus_with_victim(coord_backend):
    """A bus carrying a 'victim' task this cold-cache host has never seen:
    present in the durable task file, the summaries aggregate, and the index
    view. Any truncated rebuild drops it from the read surface."""
    victim = _make_task(title="victim")
    remote.upload_json(victim, remote.task_remote_path(victim["id"]),
                       backend=coord_backend)
    remote.upload_json(
        {"generated_at": "2099-01-01T00:00:00Z",
         "summaries": [schema.task_summary(victim)]},
        remote.view_remote_path("summaries"), backend=coord_backend)
    remote.upload_json(
        {"active": [schema.task_summary(victim)], "recent_done": []},
        remote.view_remote_path("index"), backend=coord_backend)
    return victim


def test_f2_unreadable_summaries_aggregate_skips_view_rebuild(coord_backend,
                                                              monkeypatch,
                                                              tmp_path):
    """THE F2 FAILURE SEQUENCE: cold-cache host writes a task while every
    view read 504s. The old chain (summaries None -> 'older bus' ->
    _load_all_tasks -> index None -> LOCAL CACHE ONLY) rebuilt + uploaded all
    views from this host's one cached task with a fresh generated_at —
    silently blanking the bus's read surface. The aggregate demonstrably
    EXISTS (stat sees it): the write must upload the TASK BODY only and defer
    the views to reconcile (NeedsReconcile + marker)."""
    victim = _seed_bus_with_victim(coord_backend)
    index_path = remote.view_remote_path("index")
    index_before = _store_file(tmp_path, index_path).read_text()

    # Every VIEW read fails (the throttled-reads weather); task files readable.
    _patch_download_none_for(monkeypatch, lambda p: "/views/" in p)

    t = _make_task(title="new-on-cold-host")
    cache.write_cached_task(t)
    with pytest.raises(schema.NeedsReconcile):
        writepipe._write_task_and_views(
            t, backend=coord_backend, command="update")

    # The authoritative body landed…
    written = _read_store_json(tmp_path, remote.task_remote_path(t["id"]))
    assert written["id"] == t["id"]
    # …but no view was rebuilt from the partial source: the victim's read
    # surface is intact.
    assert _store_file(tmp_path, index_path).read_text() == index_before, \
        "views were rebuilt+uploaded from a cache-only source — the bus's " \
        "index was truncated (victim dropped: %s)" % victim["id"]
    assert _failed_marker_for(t["id"]), \
        "the skipped view rebuild must leave a needs_reconcile marker"


def test_f2_confirmed_absent_aggregate_keeps_legacy_fallback(coord_backend,
                                                             tmp_path):
    """A bus that genuinely predates the aggregate (no summaries.json, but a
    readable index) keeps the existing fallback: full _load_all_tasks rebuild,
    views uploaded, no spurious failure."""
    victim = _make_task(title="victim")
    remote.upload_json(victim, remote.task_remote_path(victim["id"]),
                       backend=coord_backend)
    remote.upload_json(
        {"active": [schema.task_summary(victim)], "recent_done": []},
        remote.view_remote_path("index"), backend=coord_backend)

    t = _make_task(title="new-task")
    cache.write_cached_task(t)
    ok = writepipe._write_task_and_views(
        t, backend=coord_backend, command="update")
    assert ok is True

    rebuilt = _read_store_json(tmp_path, remote.view_remote_path("index"))
    ids = {s["id"] for s in rebuilt.get("active", [])}
    assert victim["id"] in ids and t["id"] in ids, \
        "legacy fallback must rebuild views from the FULL task set"


def test_f2_degraded_fallback_load_also_skips_view_rebuild(coord_backend,
                                                           monkeypatch,
                                                           tmp_path):
    """Aggregate confirmed absent (legacy bus) BUT the fallback full load
    itself degrades to cache-only (index exists, unreadable): same truncation
    hazard, same answer — body only, views deferred."""
    victim = _make_task(title="victim")
    remote.upload_json(victim, remote.task_remote_path(victim["id"]),
                       backend=coord_backend)
    index_path = remote.view_remote_path("index")
    remote.upload_json(
        {"active": [schema.task_summary(victim)], "recent_done": []},
        index_path, backend=coord_backend)
    index_before = _store_file(tmp_path, index_path).read_text()

    # No summaries.json on the bus (legacy); ONLY the index read fails.
    _patch_download_none_for(monkeypatch, lambda p: p == index_path)

    t = _make_task(title="new-task")
    cache.write_cached_task(t)
    with pytest.raises(schema.NeedsReconcile):
        writepipe._write_task_and_views(
            t, backend=coord_backend, command="update")
    assert _store_file(tmp_path, index_path).read_text() == index_before
    assert _failed_marker_for(t["id"])


# ===========================================================================
# F3 — cmd_reconcile: a reconcile that can't see the bus must not rewrite it
# ===========================================================================

def test_f3_load_all_tasks_flags_cache_only_degrade(coord_backend, monkeypatch,
                                                    tmp_path):
    """_load_all_tasks must EXPOSE whether it fell back to local cache so
    callers can refuse to treat the partial set as the bus."""
    t = _make_task()
    cache.write_cached_task(t)

    # Index exists on the bus but its read fails -> degraded.
    index_path = remote.view_remote_path("index")
    remote.upload_json({"active": [], "recent_done": []}, index_path,
                       backend=coord_backend)
    _patch_download_none_for(monkeypatch, lambda p: p == index_path)
    out = coord_io._load_all_tasks(backend=coord_backend)
    assert getattr(out, "load_degraded", False) is True

    # Bus unreachable entirely -> degraded too.
    monkeypatch.setattr(remote, "stat", lambda path, **kw: None)
    monkeypatch.setattr(remote, "probe_reachable", lambda backend=None: False)
    out = coord_io._load_all_tasks(backend=coord_backend)
    assert getattr(out, "load_degraded", False) is True


def test_f3_load_all_tasks_confirmed_absent_index_is_not_degraded(
        coord_backend):
    """A fresh/legacy bus with NO index at all (reachable, confirmed absent)
    is not an outage: the cached set is the best truth and callers may act."""
    t = _make_task()
    cache.write_cached_task(t)
    out = coord_io._load_all_tasks(backend=coord_backend)
    assert getattr(out, "load_degraded", True) is False
    assert {x["id"] for x in out} == {t["id"]}


def test_f3_reconcile_skips_view_rebuild_when_load_degraded(coord_backend,
                                                            monkeypatch,
                                                            tmp_path):
    """THE F3 FAILURE SEQUENCE: a thin-cache host's heartbeat reconcile cannot
    enumerate task files AND hits an index read failure; _load_all_tasks silently
    degrades to its local cache and the tick rebuilds + uploads ~all views from
    the truncated set — and reconcile can never re-discover the dropped tasks.
    The degraded tick must SKIP the view rebuild/upload phase and fail loudly
    instead."""
    victim = _seed_bus_with_victim(coord_backend)
    index_path = remote.view_remote_path("index")
    index_before = _store_file(tmp_path, index_path).read_text()

    # Thin local cache: one unrelated task.
    t = _make_task(title="only-local")
    cache.write_cached_task(t)

    _patch_download_none_for(monkeypatch, lambda p: p == index_path)
    monkeypatch.setattr(remote, "list_files", lambda *a, **k: [])

    rc = cli.cmd_reconcile(types.SimpleNamespace(), backend=coord_backend)
    assert rc == 1, "a reconcile that can't see the bus must not report success"
    assert _store_file(tmp_path, index_path).read_text() == index_before, \
        "degraded reconcile rewrote the bus's views from a truncated set " \
        "(victim dropped: %s)" % victim["id"]


def test_f3_reconcile_on_fresh_bus_still_rebuilds(coord_backend, tmp_path):
    """Counter-case: a genuinely fresh bus (no index anywhere, reachable) is
    NOT degraded — reconcile keeps rebuilding views from what it has."""
    t = _make_task()
    cache.write_cached_task(t)
    rc = cli.cmd_reconcile(types.SimpleNamespace(), backend=coord_backend)
    assert rc == 0
    rebuilt = _read_store_json(tmp_path, remote.view_remote_path("index"))
    assert t["id"] in {s["id"] for s in rebuilt.get("active", [])}
