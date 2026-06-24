"""Round-trip discipline for _load_all_tasks_by_listing (Task-2 perf).

The reconcile body-load path is read-only: it never uses the per-body
stat meta for optimistic-concurrency, so it must NOT pay the post-download
``remote.stat`` spawn that ``_cache_remote_task`` records for the write path.

These tests pin the call count: exactly ONE remote round-trip per body
(the download) when the cache starts cold.  The write path's stat
behaviour is already pinned by ``test_write_path_read_errors.py`` and
``test_perf_call_counts.py`` — those must stay green.
"""

from __future__ import annotations

from unittest import mock

from fulcra_coord import io, remote, schema


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _seed_task(backend, title: str) -> dict:
    t = schema.make_task(title=title, workstream="ws", agent="a")
    t["status"] = "active"
    remote.upload_json(t, remote.task_remote_path(t["id"]), backend=backend)
    return t


# ---------------------------------------------------------------------------
# T2a — listing load: ONE download, ZERO stat spawns per body (cold cache)
# ---------------------------------------------------------------------------

def test_listing_load_does_one_roundtrip_per_body(coord_backend):
    """The reconcile body-load path must issue exactly ONE remote call per task
    body (download_json) and zero stat calls.

    ``_cache_remote_task`` records a post-download stat for the write path's
    optimistic-concurrency baseline — that extra spawn is what pushed a
    16-worker, 303-body load from the expected ~20s to ~31s.  The listing path
    is read-only and must skip it.
    """
    t1 = _seed_task(coord_backend, "alpha")
    t2 = _seed_task(coord_backend, "beta")

    calls: dict[str, int] = {"download": 0, "stat": 0}
    real_dl = remote.download_json
    real_stat = remote.stat

    def counting_dl(path, **kw):
        calls["download"] += 1
        return real_dl(path, **kw)

    def counting_stat(path, **kw):
        calls["stat"] += 1
        return real_stat(path, **kw)

    with mock.patch.object(remote, "download_json", counting_dl), \
         mock.patch.object(remote, "stat", counting_stat):
        result = io._load_all_tasks_by_listing(backend=coord_backend)

    assert result is not None
    ids = {t["id"] for t in result}
    assert t1["id"] in ids
    assert t2["id"] in ids
    assert calls["download"] == 2, (
        f"listing load issued {calls['download']} downloads for 2 bodies "
        "— must be exactly 2 (one per body)")
    assert calls["stat"] == 0, (
        f"listing load issued {calls['stat']} stat calls — must be 0; "
        "the post-download stat is write-path only and not needed here")


# ---------------------------------------------------------------------------
# T2b — stat gate fires when meta was written by a full (write-path) load
# ---------------------------------------------------------------------------

def test_listing_load_after_full_load_uses_stat_gate(coord_backend):
    """When tasks were previously loaded via the FULL path (which DOES write
    stat meta), the listing load's stat gate fires: one stat per unchanged
    body, zero downloads.

    The listing load itself deliberately skips writing stat meta (to avoid
    the extra post-download spawn).  But if another path (e.g. ``_load_task``
    or ``_load_all_tasks``) already recorded meta, the gate can still fire.
    This confirms ``_skip_post_stat`` does not break the gate for those tasks.
    """
    from fulcra_coord import io as coord_io

    t1 = _seed_task(coord_backend, "gamma")
    t2 = _seed_task(coord_backend, "delta")

    # Warm via the FULL path (writes stat meta — simulates having run a normal
    # _cache_remote_task for these tasks, which happens on every non-fallback tick).
    coord_io._cache_remote_task(t1["id"], backend=coord_backend)
    coord_io._cache_remote_task(t2["id"], backend=coord_backend)

    calls: dict[str, int] = {"download": 0, "stat": 0}
    real_dl = remote.download_json
    real_stat = remote.stat

    def counting_dl(path, **kw):
        calls["download"] += 1
        return real_dl(path, **kw)

    def counting_stat(path, **kw):
        calls["stat"] += 1
        return real_stat(path, **kw)

    with mock.patch.object(remote, "download_json", counting_dl), \
         mock.patch.object(remote, "stat", counting_stat):
        result2 = io._load_all_tasks_by_listing(backend=coord_backend)

    assert result2 is not None
    ids = {t["id"] for t in result2}
    assert t1["id"] in ids
    assert t2["id"] in ids
    # Gate should fire (prior meta + cached body match) → no downloads.
    assert calls["download"] == 0, (
        f"listing load after full-path warm: downloaded {calls['download']} "
        "bodies — stat gate should have suppressed all downloads")
    assert calls["stat"] == 2, (
        f"listing load after full-path warm: {calls['stat']} stat calls — "
        "should be exactly 2 (one gate-check per body)")
