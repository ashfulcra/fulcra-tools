"""Tests for the event-log retention pass (_prune_event_log, _eventlog_keep).

Root cause B: the event-sourcing dual-write appends an immutable shard at
events/tasks/<id>/<event_id>.json on every task mutation, but retention.py
never pruned the events/ family — so the log grew without bound (B1) and every
archived task orphaned its whole shard tree (B2).

Mock idioms mirror the continuity-checkpoint tests (_ContTree /
TestPruneContinuityCheckpoints): patch remote.list_files / remote.list_json /
remote.delete / remote.stat on the retention module with side_effects driven by
a {prefix: [...]} tree; set FULCRA_COORD_REMOTE_ROOT=/coordination in setUp;
collect deletes in self.deleted.
"""

import os
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from fulcra_coord import retention
from fulcra_coord.events import fold_task

_ROOT = "/coordination"
_EVENTS_ROOT = f"{_ROOT}/events/tasks/"


def _ev(task_id, *, at, suffix="0000", payload):
    """Build a minimal event record with a deterministic, fold-sortable
    (at, event_id). The event_id suffix breaks ties at the same `at`."""
    return {
        "schema_version": "fulcra.coordination.event.v1",
        "event_id": f"{at.replace(':', '').replace('-', '').replace('.', '').replace('Z', '')}-{suffix}",
        "task_id": task_id,
        "kind": "updated",
        "actor": "role:host:proj",
        "at": at,
        "idempotency_key": None,
        "payload": payload,
    }


def _snapshot_payload(task_id, **fields):
    """A full-task snapshot payload (truthy schema + id) — replaces fold state."""
    p = {"schema": "fulcra.coordination.task.v1", "id": task_id}
    p.update(fields)
    return p


def _delta_payload(**fields):
    """A legacy Phase-1 delta payload (no schema/id) — shallow-merges in fold."""
    return dict(fields)


class _EventTree(unittest.TestCase):
    """Base: models NON-RECURSIVE remote.list_files as a {prefix: [children]}
    map (dirs trail a slash, files don't), and remote.list_json as a
    {prefix: [(path, record)]} map. Patches list_files / list_json / delete /
    stat on the retention module."""

    def setUp(self):
        os.environ["FULCRA_COORD_REMOTE_ROOT"] = "/coordination"
        self._prev_keep = os.environ.pop("FULCRA_COORD_EVENTLOG_KEEP", None)
        self.now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)
        self.deleted: list[str] = []

    def tearDown(self):
        os.environ.pop("FULCRA_COORD_REMOTE_ROOT", None)
        if self._prev_keep is None:
            os.environ.pop("FULCRA_COORD_EVENTLOG_KEEP", None)
        else:
            os.environ["FULCRA_COORD_EVENTLOG_KEEP"] = self._prev_keep

    def _list_files_factory(self, files_tree):
        def _list(prefix, *, backend=None, timeout=None):
            return list(files_tree.get(prefix, []))
        return _list

    def _list_json_factory(self, json_tree):
        def _list(prefix, *, backend=None):
            return list(json_tree.get(prefix, []))
        return _list

    def _delete_factory(self, raise_on=()):
        def _delete(path, *, backend=None):
            if path in raise_on:
                raise RuntimeError("boom")
            self.deleted.append(path)
            return True
        return _delete

    def _stat_factory(self, present_paths):
        """stat -> truthy for paths in present_paths, else None."""
        def _stat(path, *, backend=None):
            return {"size": 1} if path in present_paths else None
        return _stat

    def _run(self, *, files_tree, json_tree, all_tasks, present_paths=(),
             raise_on=(), deadline=None, max_per_run=None):
        cm = patch("fulcra_coord.retention._retention_max_per_run",
                   return_value=max_per_run) if max_per_run is not None \
            else _NullCM()
        with patch("fulcra_coord.retention.remote.list_files",
                   side_effect=self._list_files_factory(files_tree)), \
             patch("fulcra_coord.retention.remote.list_json",
                   side_effect=self._list_json_factory(json_tree)), \
             patch("fulcra_coord.retention.remote.delete",
                   side_effect=self._delete_factory(raise_on)), \
             patch("fulcra_coord.retention.remote.stat",
                   side_effect=self._stat_factory(set(present_paths))), \
             cm:
            return retention._prune_event_log(
                all_tasks, self.now, backend=["false"], deadline=deadline)


class _NullCM:
    def __enter__(self):
        return None

    def __exit__(self, *a):
        return False


def _events_prefix(task_id):
    return f"{_EVENTS_ROOT}{task_id}/"


def _shard_path(task_id, record):
    return f"{_events_prefix(task_id)}{record['event_id']}.json"


class TestEventlogKeep(_EventTree):
    def test_default_is_twenty(self):
        self.assertEqual(retention._eventlog_keep(), 20)

    def test_floor_is_one(self):
        os.environ["FULCRA_COORD_EVENTLOG_KEEP"] = "0"
        self.assertEqual(retention._eventlog_keep(), 1)
        os.environ["FULCRA_COORD_EVENTLOG_KEEP"] = "-5"
        self.assertEqual(retention._eventlog_keep(), 1)


class TestPruneEventLogLive(_EventTree):
    def test_keep_snapshot_and_recent_delete_older(self):
        # A live task: old deltas -> old snapshot -> more deltas -> NEWER snapshot
        # -> a couple trailing deltas. Everything strictly before the LATEST
        # snapshot (beyond the keep window) is stale and deletable; the latest
        # snapshot and everything after are load-bearing.
        tid = "task-1"
        recs = [
            _ev(tid, at="2026-01-01T00:00:00Z", suffix="01",
                payload=_delta_payload(title="t0", note="early")),
            _ev(tid, at="2026-01-02T00:00:00Z", suffix="01",
                payload=_delta_payload(current_summary="s1")),
            _ev(tid, at="2026-01-03T00:00:00Z", suffix="01",
                payload=_snapshot_payload(tid, title="snap-old", status="active")),
            _ev(tid, at="2026-01-04T00:00:00Z", suffix="01",
                payload=_delta_payload(current_summary="s2")),
            # latest snapshot:
            _ev(tid, at="2026-01-05T00:00:00Z", suffix="01",
                payload=_snapshot_payload(tid, title="snap-new", status="active",
                                          owner_agent="a")),
            _ev(tid, at="2026-01-06T00:00:00Z", suffix="01",
                payload=_delta_payload(current_summary="s3")),
            _ev(tid, at="2026-01-07T00:00:00Z", suffix="01",
                payload=_delta_payload(status="done")),
        ]
        pairs = [(_shard_path(tid, r), r) for r in recs]
        files_tree = {_EVENTS_ROOT: [_events_prefix(tid)]}
        json_tree = {_events_prefix(tid): pairs}
        # keep=2: len=7, snap_idx=4 -> len-keep=5 >= snap_idx, so the SNAPSHOT
        # floor binds: keep_from = min(4, 5) = 4. Indices 0..3 (strictly before
        # the latest snapshot) delete; the snapshot + everything after survive.
        os.environ["FULCRA_COORD_EVENTLOG_KEEP"] = "2"
        n = self._run(files_tree=files_tree, json_tree=json_tree,
                      all_tasks=[{"id": tid}])
        self.assertEqual(n, 4)
        deleted_set = set(self.deleted)
        for r in recs[:4]:
            self.assertIn(_shard_path(tid, r), deleted_set)
        for r in recs[4:]:
            self.assertNotIn(_shard_path(tid, r), deleted_set)

        # LOAD-BEARING CORRECTNESS INVARIANT: the surviving shards must fold to
        # the SAME task STATE as folding the full event set. A snapshot is
        # self-complete, so dropping everything before the latest snapshot leaves
        # every task field byte-identical. We exclude only ``_applied_event_count``
        # — that's an internal bookkeeping counter of how many events were applied,
        # NOT task state, and it legitimately drops from 7 to 3 once 4 stale
        # pre-snapshot shards are pruned; nothing a fold consumer reads changes.
        survivors = [r for r in recs if _shard_path(tid, r) not in deleted_set]

        def _state(s):
            return {k: v for k, v in s.items() if k != "_applied_event_count"}

        self.assertEqual(_state(fold_task(survivors)), _state(fold_task(recs)))

    def test_keep_floor_never_below_latest_snapshot(self):
        # KEEP=0 -> _eventlog_keep()==1. Even with a tiny keep window, the latest
        # snapshot is preserved: keep_from = min(snap_idx, len-keep) never exceeds
        # snap_idx, so deletions stay strictly before the latest snapshot.
        tid = "task-floor"
        recs = [
            _ev(tid, at="2026-01-01T00:00:00Z", suffix="01",
                payload=_delta_payload(title="d0")),
            _ev(tid, at="2026-01-02T00:00:00Z", suffix="01",
                payload=_snapshot_payload(tid, title="the-snap", status="active")),
            _ev(tid, at="2026-01-03T00:00:00Z", suffix="01",
                payload=_delta_payload(status="done")),
        ]
        pairs = [(_shard_path(tid, r), r) for r in recs]
        os.environ["FULCRA_COORD_EVENTLOG_KEEP"] = "0"  # floored to 1
        n = self._run(files_tree={_EVENTS_ROOT: [_events_prefix(tid)]},
                      json_tree={_events_prefix(tid): pairs},
                      all_tasks=[{"id": tid}])
        # snap_idx=1, len=3, keep=1 -> keep_from=min(1, 3-1=2)=1 -> delete recs[:1].
        self.assertEqual(n, 1)
        self.assertEqual(self.deleted, [_shard_path(tid, recs[0])])
        # The snapshot itself is never deleted.
        self.assertNotIn(_shard_path(tid, recs[1]), self.deleted)

    def test_keep_window_caps_below_snapshot(self):
        # When the keep window is SMALLER than the events-after-snapshot, the
        # keep window (not the snapshot) is the binding constraint, but never
        # below the snapshot. snap at index 0, then 4 deltas, keep=2:
        # keep_from = min(0, 5-2=3) = 0 -> nothing deleted (snapshot is oldest).
        tid = "task-cap"
        recs = [
            _ev(tid, at="2026-01-01T00:00:00Z", suffix="01",
                payload=_snapshot_payload(tid, title="snap", status="active")),
        ] + [
            _ev(tid, at=f"2026-01-0{d}T00:00:00Z", suffix="01",
                payload=_delta_payload(current_summary=f"s{d}"))
            for d in range(2, 6)
        ]
        pairs = [(_shard_path(tid, r), r) for r in recs]
        os.environ["FULCRA_COORD_EVENTLOG_KEEP"] = "2"
        n = self._run(files_tree={_EVENTS_ROOT: [_events_prefix(tid)]},
                      json_tree={_events_prefix(tid): pairs},
                      all_tasks=[{"id": tid}])
        self.assertEqual(n, 0)
        self.assertEqual(self.deleted, [])


class TestPruneEventLogDeltaOnly(_EventTree):
    def test_delta_only_never_pruned(self):
        # No snapshot anywhere -> each delta may carry a unique field -> FAIL-SAFE:
        # never prune. 0 deletions even with a tiny keep window.
        tid = "task-deltas"
        recs = [
            _ev(tid, at=f"2026-01-0{d}T00:00:00Z", suffix="01",
                payload=_delta_payload(**{f"field_{d}": d}))
            for d in range(1, 7)
        ]
        pairs = [(_shard_path(tid, r), r) for r in recs]
        os.environ["FULCRA_COORD_EVENTLOG_KEEP"] = "1"
        n = self._run(files_tree={_EVENTS_ROOT: [_events_prefix(tid)]},
                      json_tree={_events_prefix(tid): pairs},
                      all_tasks=[{"id": tid}])
        self.assertEqual(n, 0)
        self.assertEqual(self.deleted, [])


class TestPruneEventLogOrphans(_EventTree):
    def test_orphan_all_shards_deleted(self):
        # B2: task dir whose id is NOT in all_tasks AND whose hot file is confirmed
        # absent (stat -> None) is an archived/deleted task: delete ALL its shards.
        # The orphan branch enumerates via list_files (files_tree), NOT list_json:
        # it never inspects payloads, and list_json would silently drop any shard
        # whose JSON can't parse to a dict (Fix 1 — closes the corrupt-shard hole).
        tid = "task-archived"
        recs = [
            _ev(tid, at=f"2026-01-0{d}T00:00:00Z", suffix="01",
                payload=_delta_payload(x=d))
            for d in range(1, 5)
        ]
        shards = [_shard_path(tid, r) for r in recs]
        n = self._run(files_tree={_EVENTS_ROOT: [_events_prefix(tid)],
                                  _events_prefix(tid): shards},
                      json_tree={},
                      all_tasks=[],          # not live
                      present_paths=())      # hot file absent
        self.assertEqual(n, 4)
        self.assertEqual(sorted(self.deleted), sorted(shards))

    def test_orphan_corrupt_shard_still_deleted(self):
        # Fix 1 regression: an orphan tree contains a shard that is INVISIBLE to
        # list_json (its JSON doesn't parse to a dict, so list_json drops it) but
        # IS present in the directory listing (files_tree). Under the OLD list_json
        # enumeration this shard would leak forever — incomplete GC. The orphan
        # branch must enumerate via list_files so the corrupt shard IS deleted.
        tid = "task-archived-corrupt"
        good = _ev(tid, at="2026-01-01T00:00:00Z", suffix="01",
                   payload=_delta_payload(x=1))
        good_path = _shard_path(tid, good)
        corrupt_path = f"{_events_prefix(tid)}corrupt-half-written.json"
        # files_tree (list_files) sees BOTH shards; json_tree (list_json) sees only
        # the parseable one — the corrupt shard is absent there.
        n = self._run(files_tree={_EVENTS_ROOT: [_events_prefix(tid)],
                                  _events_prefix(tid): [good_path, corrupt_path]},
                      json_tree={_events_prefix(tid): [(good_path, good)]},
                      all_tasks=[],          # not live
                      present_paths=())      # hot file absent
        self.assertEqual(n, 2)
        self.assertIn(corrupt_path, self.deleted)
        self.assertIn(good_path, self.deleted)

    def test_orphan_enumeration_ignores_non_json(self):
        # The orphan list_files enumeration is filtered to .json: a stray non-JSON
        # file in the tree (e.g. a leftover lock/tmp) is NOT deleted.
        tid = "task-archived-stray"
        good = _ev(tid, at="2026-01-01T00:00:00Z", suffix="01",
                   payload=_delta_payload(x=1))
        good_path = _shard_path(tid, good)
        stray = f"{_events_prefix(tid)}.tmp-lock"
        n = self._run(files_tree={_EVENTS_ROOT: [_events_prefix(tid)],
                                  _events_prefix(tid): [good_path, stray]},
                      json_tree={},
                      all_tasks=[], present_paths=())
        self.assertEqual(n, 1)
        self.assertEqual(self.deleted, [good_path])

    def test_orphan_failsafe_hot_file_present_skips(self):
        # B2 fail-safe: not in all_tasks BUT hot file stat -> present => possibly
        # live (partial all_tasks). SKIP — never prune a possibly-live tree.
        tid = "task-maybe-live"
        recs = [
            _ev(tid, at="2026-01-01T00:00:00Z", suffix="01",
                payload=_delta_payload(x=1)),
        ]
        shards = [_shard_path(tid, r) for r in recs]
        hot = f"{_ROOT}/tasks/{tid}.json"
        n = self._run(files_tree={_EVENTS_ROOT: [_events_prefix(tid)],
                                  _events_prefix(tid): shards},
                      json_tree={},
                      all_tasks=[],
                      present_paths=(hot,))  # hot file present -> skip
        self.assertEqual(n, 0)
        self.assertEqual(self.deleted, [])


class TestPruneEventLogBudgetCap(_EventTree):
    def test_per_run_cap_limits_deletions(self):
        # Two orphan task dirs with many shards each; cap=3 limits total deletions.
        tids = ["orph-a", "orph-b"]
        files_tree = {_EVENTS_ROOT: [_events_prefix(t) for t in tids]}
        all_shards = []
        for t in tids:
            recs = [
                _ev(t, at=f"2026-01-0{d}T00:00:00Z", suffix="01",
                    payload=_delta_payload(x=d))
                for d in range(1, 5)
            ]
            shards = [_shard_path(t, r) for r in recs]
            files_tree[_events_prefix(t)] = shards
            all_shards += shards
        n = self._run(files_tree=files_tree, json_tree={},
                      all_tasks=[], present_paths=(), max_per_run=3)
        self.assertEqual(n, 3)
        self.assertEqual(len(self.deleted), 3)

    def test_budget_floor_stops_pass_early(self):
        # A deadline already in the past -> budget gate stops before any delete.
        tid = "orph"
        recs = [
            _ev(tid, at="2026-01-01T00:00:00Z", suffix="01",
                payload=_delta_payload(x=1)),
        ]
        n = self._run(files_tree={_EVENTS_ROOT: [_events_prefix(tid)],
                                  _events_prefix(tid): [_shard_path(tid, recs[0])]},
                      json_tree={},
                      all_tasks=[], present_paths=(),
                      deadline=time.monotonic() - 1)
        self.assertEqual(n, 0)
        self.assertEqual(self.deleted, [])


class TestPruneEventLogBestEffort(_EventTree):
    def test_one_delete_raises_swallowed(self):
        # B2 orphan with 3 shards; the middle delete raises -> swallowed, the pass
        # continues, and the failed shard is excluded from the count.
        tid = "orph"
        recs = [
            _ev(tid, at=f"2026-01-0{d}T00:00:00Z", suffix="01",
                payload=_delta_payload(x=d))
            for d in range(1, 4)
        ]
        shards = [_shard_path(tid, r) for r in recs]
        boom = _shard_path(tid, recs[1])
        n = self._run(files_tree={_EVENTS_ROOT: [_events_prefix(tid)],
                                  _events_prefix(tid): shards},
                      json_tree={},
                      all_tasks=[], present_paths=(),
                      raise_on=(boom,))
        # 3 shards, one raised -> 2 counted, no exception escapes.
        self.assertEqual(n, 2)
        self.assertNotIn(boom, self.deleted)

    def test_non_trailing_slash_children_skipped(self):
        # Stray files (no trailing slash) directly under events/tasks/ are not task
        # dirs -> skipped, never treated as a task id.
        stray = f"{_EVENTS_ROOT}stray.json"
        n = self._run(files_tree={_EVENTS_ROOT: [stray]},
                      json_tree={}, all_tasks=[], present_paths=())
        self.assertEqual(n, 0)
        self.assertEqual(self.deleted, [])


class TestPruneEventLogEdgeCases(_EventTree):
    def test_empty_events_tasks_dir(self):
        # The events/tasks/ dir lists nothing at all -> the pass is a clean no-op.
        n = self._run(files_tree={_EVENTS_ROOT: []},
                      json_tree={}, all_tasks=[{"id": "whatever"}],
                      present_paths=())
        self.assertEqual(n, 0)
        self.assertEqual(self.deleted, [])

    def test_live_task_with_zero_shards(self):
        # A live task whose events_prefix lists nothing: list_json -> [] -> the
        # `if not pairs: continue` guard fires -> zero deletions, no error.
        tid = "task-empty"
        n = self._run(files_tree={_EVENTS_ROOT: [_events_prefix(tid)]},
                      json_tree={_events_prefix(tid): []},
                      all_tasks=[{"id": tid}],
                      present_paths=())
        self.assertEqual(n, 0)
        self.assertEqual(self.deleted, [])

    def test_keep_larger_than_event_count(self):
        # keep > len on the LIVE branch, with the snapshot NOT at the start AND
        # NOT at the end. This is the case the max(0, len - keep) guard exists
        # for: len - keep is NEGATIVE, so an un-guarded keep_from would go
        # negative and pairs[:keep_from] would slice off the TAIL — wrongly
        # deleting the snapshot itself (and post-snapshot events) instead of the
        # pre-snapshot shards. Existing keep-window tests only hit keep_from=0 via
        # a snapshot at index 0, so a regressed guard would slip past them.
        #
        # len=5, snap_idx=2, keep=6 -> len-keep=-1.
        #   WITHOUT the guard: keep_from = min(2, -1) = -1, pairs[:-1] = indices
        #     0..3 -> deletes the snapshot at idx 2 (a correctness violation).
        #   WITH the guard:    keep_from = min(2, max(0, -1)) = min(2, 0) = 0,
        #     pairs[:0] = [] -> nothing deleted.
        tid = "task-bigkeep"
        recs = [
            _ev(tid, at="2026-01-01T00:00:00Z", suffix="01",
                payload=_delta_payload(title="d0")),
            _ev(tid, at="2026-01-02T00:00:00Z", suffix="01",
                payload=_delta_payload(current_summary="s1")),
            # snapshot at index 2 (middle):
            _ev(tid, at="2026-01-03T00:00:00Z", suffix="01",
                payload=_snapshot_payload(tid, title="snap", status="active")),
            _ev(tid, at="2026-01-04T00:00:00Z", suffix="01",
                payload=_delta_payload(current_summary="s2")),
            _ev(tid, at="2026-01-05T00:00:00Z", suffix="01",
                payload=_delta_payload(status="done")),
        ]
        pairs = [(_shard_path(tid, r), r) for r in recs]
        os.environ["FULCRA_COORD_EVENTLOG_KEEP"] = "6"  # keep > len (5)
        n = self._run(files_tree={_EVENTS_ROOT: [_events_prefix(tid)]},
                      json_tree={_events_prefix(tid): pairs},
                      all_tasks=[{"id": tid}], present_paths=())
        self.assertEqual(n, 0)
        self.assertEqual(self.deleted, [])
        # The snapshot must never be in the delete set.
        self.assertNotIn(_shard_path(tid, recs[2]), self.deleted)


class TestRunRetentionEventlogWiring(_EventTree):
    def test_result_includes_pruned_events(self):
        from fulcra_coord import cli
        with patch("fulcra_coord.retention._claim_retention_marker", return_value=True), \
             patch("fulcra_coord.retention._prune_event_log", return_value=9) as pel:
            res = cli._run_retention([], now=datetime.now(timezone.utc),
                                     deadline=time.monotonic() + 60, backend=["false"])
        self.assertEqual(res.get("pruned_events"), 9)
        pel.assert_called_once()
        self.assertIn("deadline", pel.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
