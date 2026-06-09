"""Tests for the provenance-sidecar retention pass (_prune_provenance_sidecars).

Root cause A leftover: ``cache.write_provenance`` drops a ``<key>.prov.json``
sidecar (key = ``cache._prov_key(task_id)``) under ``cache.meta_dir()`` for every
task body ever read in events-mode — each holding a full ``fold_base`` task body.
``clear_provenance`` removes one after a successful upload, but a task that simply
ages out (archived / deleted remotely) leaves its sidecar behind forever, so on a
long-lived host the ``.prov.json`` family grows without bound.

This prune deletes the orphans: any ``*.prov.json`` whose key isn't a LIVE task's
``_prov_key`` belongs to a task no longer in the set, so it's safe to unlink.

SAFETY (load-bearing): these are LOCAL files under ``meta_dir()`` — deleted with
``Path.unlink``, never ``remote.delete``. Only ``*.prov.json`` orphans are
touched; ``*.stat.json`` (the hash-keyed meta sidecars, intentionally out of
scope) and anything else in ``meta_dir()`` are NEVER deleted. Live tasks' prov
sidecars survive. The whole pass is wrapped so it never raises into reconcile.

These are local files, so the autouse hermetic fixture (which redirects
XDG_CACHE_HOME, and thus ``meta_dir()``, to a per-test tmp dir) gives the
isolation — no fake remote backend is needed here.
"""

from __future__ import annotations

from fulcra_coord import cache, retention


def test_prunes_orphan_keeps_live_and_ignores_stat_json():
    live_id = "TASK-20260609-live-aaaaaaaa"
    dead_id = "TASK-20260609-dead-bbbbbbbb"

    # A prov sidecar for a live task and one for a task no longer in the set.
    cache.write_provenance(live_id, {"source": "fold", "fold_base": {"id": live_id}})
    cache.write_provenance(dead_id, {"source": "fold", "fold_base": {"id": dead_id}})

    # A .stat.json sidecar in the SAME dir — must be left untouched.
    cache.write_meta("/coordination/tasks/SOMETHING.json", {"version": "v1"})

    live_path = cache.meta_dir() / f"{cache._prov_key(live_id)}.prov.json"
    dead_path = cache.meta_dir() / f"{cache._prov_key(dead_id)}.prov.json"
    stat_files_before = list(cache.meta_dir().glob("*.stat.json"))
    assert live_path.exists() and dead_path.exists()
    assert stat_files_before, "test setup should have written a .stat.json sidecar"

    deleted = retention._prune_provenance_sidecars([{"id": live_id}])

    assert deleted == 1, "exactly the one orphan should be deleted"
    assert live_path.exists(), "live task's prov sidecar must survive"
    assert not dead_path.exists(), "orphaned prov sidecar must be deleted"
    # The .stat.json files are out of scope and must be untouched.
    assert list(cache.meta_dir().glob("*.stat.json")) == stat_files_before


def test_no_orphans_deletes_nothing():
    live_id = "TASK-20260609-only-cccccccc"
    cache.write_provenance(live_id, {"source": "file"})
    deleted = retention._prune_provenance_sidecars([{"id": live_id}])
    assert deleted == 0
    assert (cache.meta_dir() / f"{cache._prov_key(live_id)}.prov.json").exists()


def test_missing_meta_dir_never_raises():
    # No sidecars written at all -> nothing to prune, no crash, returns 0.
    deleted = retention._prune_provenance_sidecars([{"id": "TASK-X"}])
    assert deleted == 0


def test_tasks_without_id_are_ignored_when_building_live_set():
    # A task dict lacking "id" must not crash the live-set build, and must not
    # cause a real orphan to be spared (a None/missing key isn't a live key).
    dead_id = "TASK-20260609-dead-dddddddd"
    cache.write_provenance(dead_id, {"source": "fold"})
    deleted = retention._prune_provenance_sidecars([{"title": "no id here"}, {}])
    assert deleted == 1
    assert not (cache.meta_dir() / f"{cache._prov_key(dead_id)}.prov.json").exists()
