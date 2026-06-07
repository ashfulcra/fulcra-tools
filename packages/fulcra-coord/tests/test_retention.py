import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fulcra_coord import cli, remote, views, retention

_FAKE = str(Path(__file__).parent / "fake_fulcra_backend.py")


def _dt(days_ago, now):
    return (now - timedelta(days=days_ago)).isoformat(timespec="microseconds").replace("+00:00", "Z")


class TestIsArchivableTask(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)

    def test_terminal_and_aged_is_archivable(self):
        for status in ("done", "abandoned"):
            t = {"status": status, "done_at": _dt(31, self.now), "updated_at": _dt(31, self.now)}
            self.assertTrue(views.is_archivable_task(t, self.now, 30), status)

    def test_terminal_but_recent_is_not(self):
        t = {"status": "done", "done_at": _dt(29, self.now), "updated_at": _dt(29, self.now)}
        self.assertFalse(views.is_archivable_task(t, self.now, 30))

    def test_exactly_at_cutoff_is_archivable(self):
        # cutoff is "older than N days" => age >= N days qualifies (>= boundary).
        t = {"status": "done", "done_at": _dt(30, self.now), "updated_at": _dt(30, self.now)}
        self.assertTrue(views.is_archivable_task(t, self.now, 30))

    def test_non_terminal_never_archivable_even_if_ancient(self):
        for status in ("active", "waiting", "blocked", "proposed"):
            t = {"status": status, "updated_at": _dt(999, self.now)}
            self.assertFalse(views.is_archivable_task(t, self.now, 30), status)

    def test_uses_done_at_over_updated_at(self):
        # done long ago but updated recently (e.g. a late annotation) => not aged.
        t = {"status": "done", "done_at": _dt(5, self.now), "updated_at": _dt(5, self.now)}
        self.assertFalse(views.is_archivable_task(t, self.now, 30))

    def test_missing_timestamps_not_archivable(self):
        # a clockless terminal task: +inf age would archive it, but we choose the
        # SAFE direction for a destructive move — don't archive without a parseable
        # done/updated timestamp (opposite of is_stale's fail-toward-surfacing).
        t = {"status": "done"}
        self.assertFalse(views.is_archivable_task(t, self.now, 30))

    def test_env_default_is_30(self):
        self.assertEqual(views._retention_days(), 30.0)
        os.environ["FULCRA_COORD_RETENTION_DAYS"] = "7"
        try:
            self.assertEqual(views._retention_days(), 7.0)
        finally:
            del os.environ["FULCRA_COORD_RETENTION_DAYS"]


class TestIsPrunableMarker(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)

    def test_old_marker_prunable(self):
        path = "/coordination/digest/markers/2026-05-20-morning.json"  # 16d old
        self.assertTrue(views.is_prunable_marker(path, self.now, 7))

    def test_recent_marker_kept(self):
        path = "/coordination/digest/markers/2026-06-02-evening.json"  # 3d old
        self.assertFalse(views.is_prunable_marker(path, self.now, 7))

    def test_exactly_at_cutoff_prunable(self):
        path = "/coordination/digest/markers/2026-05-29-morning.json"  # 7d old
        self.assertTrue(views.is_prunable_marker(path, self.now, 7))

    def test_unparseable_date_kept(self):
        # never delete something we can't date — safe direction for a destructive op.
        self.assertFalse(views.is_prunable_marker("/x/digest/markers/garbage.json", self.now, 7))
        self.assertFalse(views.is_prunable_marker("/x/digest/markers/.json", self.now, 7))


class TestIsPrunablePresence(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)

    def _rec(self, days_ago):
        ls = (self.now - timedelta(days=days_ago)).isoformat(timespec="microseconds").replace("+00:00", "Z")
        return {"agent": "claude-code:h:r", "last_seen": ls}

    def test_long_dead_prunable(self):
        self.assertTrue(views.is_prunable_presence(self._rec(31), self.now, 30))

    def test_recently_seen_kept(self):
        self.assertFalse(views.is_prunable_presence(self._rec(2), self.now, 30))

    def test_exactly_at_cutoff_prunable(self):
        self.assertTrue(views.is_prunable_presence(self._rec(30), self.now, 30))

    def test_missing_last_seen_kept(self):
        self.assertFalse(views.is_prunable_presence({"agent": "x"}, self.now, 30))


class TestArchivePaths(unittest.TestCase):
    def setUp(self):
        os.environ["FULCRA_COORD_REMOTE_ROOT"] = "/coordination"

    def tearDown(self):
        os.environ.pop("FULCRA_COORD_REMOTE_ROOT", None)

    def test_archive_task_path(self):
        self.assertEqual(remote.archive_task_path("t-1", "2026-05"),
                         "/coordination/archive/tasks/2026-05/t-1.json")

    def test_archive_index_path_and_prefix(self):
        self.assertEqual(remote.archive_index_path("t-1"),
                         "/coordination/archive/index/t-1.json")
        self.assertEqual(remote.archive_index_prefix(), "/coordination/archive/index/")

    def test_retention_marker_path(self):
        now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(remote.retention_marker_path(now),
                         "/coordination/retention/last-run.json")

    def test_digest_markers_prefix(self):
        self.assertEqual(remote.digest_markers_prefix(), "/coordination/digest/markers/")

    def test_presence_prefix(self):
        self.assertEqual(remote.presence_prefix(), "/coordination/presence/")


class _FakeBus(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fc-retention-")
        os.environ["FULCRA_FAKE_ROOT"] = self.tmp
        os.environ["FULCRA_COORD_REMOTE_ROOT"] = "/coordination"
        os.environ["FULCRA_COORD_BACKEND"] = f"{sys.executable} {_FAKE}"
        # Isolate the LOCAL cache too: cmd_reconcile falls back to
        # cache.list_cached_tasks() (XDG_CACHE_HOME-rooted) when the remote index
        # is empty, which would otherwise read the developer's real ~/.cache.
        self._xdg_prev = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = str(Path(self.tmp) / "_xdg_cache")
        self.backend = [sys.executable, _FAKE]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k in ("FULCRA_FAKE_ROOT", "FULCRA_COORD_REMOTE_ROOT", "FULCRA_COORD_BACKEND"):
            os.environ.pop(k, None)
        if self._xdg_prev is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = self._xdg_prev

    def _put(self, remote_path, obj):
        p = Path(self.tmp) / remote_path.lstrip("/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj))

    def _exists(self, remote_path):
        return (Path(self.tmp) / remote_path.lstrip("/")).exists()

    def _read(self, remote_path):
        return json.loads((Path(self.tmp) / remote_path.lstrip("/")).read_text())


class TestRemoteDelete(_FakeBus):
    def test_delete_removes_existing_file(self):
        self._put("/coordination/tasks/t-1.json", {"id": "t-1"})
        ok = remote.delete("/coordination/tasks/t-1.json", backend=self.backend)
        self.assertTrue(ok)
        self.assertFalse(self._exists("/coordination/tasks/t-1.json"))

    def test_delete_missing_file_is_false(self):
        ok = remote.delete("/coordination/tasks/nope.json", backend=self.backend)
        self.assertFalse(ok)


class TestArchiveTask(_FakeBus):
    def _task(self, tid="t-1"):
        return {"id": tid, "title": "old work", "status": "done",
                "workstream": "ws", "owner_agent": "claude-code:h:r",
                "done_at": "2026-05-01T00:00:00Z", "updated_at": "2026-05-01T00:00:00Z"}

    def test_move_writes_archive_deletes_original_writes_shard(self):
        t = self._task()
        self._put("/coordination/tasks/t-1.json", t)
        ok = cli._archive_task(t, backend=self.backend)
        self.assertTrue(ok)
        self.assertTrue(self._exists("/coordination/archive/tasks/2026-05/t-1.json"))
        self.assertFalse(self._exists("/coordination/tasks/t-1.json"))
        shard = self._read("/coordination/archive/index/t-1.json")
        self.assertEqual(shard["id"], "t-1")
        self.assertEqual(shard["archive_path"], "/coordination/archive/tasks/2026-05/t-1.json")
        for f in ("title", "status", "workstream", "owner_agent", "done_at", "archived_at"):
            self.assertIn(f, shard)

    def test_idempotent_already_archived_is_noop(self):
        t = self._task()
        # Seed the hot copy so the FIRST call is a legitimate move (not a
        # stale-cache phantom, which the B2 guard now correctly skips).
        self._put("/coordination/tasks/t-1.json", t)
        self.assertTrue(cli._archive_task(t, backend=self.backend))  # first
        self.assertTrue(cli._archive_task(t, backend=self.backend))  # second: no-op, still True
        self.assertTrue(self._exists("/coordination/archive/tasks/2026-05/t-1.json"))
        self.assertTrue(self._exists("/coordination/archive/index/t-1.json"))

    def test_crash_between_archive_and_delete_completes_next_pass(self):
        # Simulate a crash AFTER the body landed in archive but BEFORE delete:
        # both copies present, no shard. Next _archive_task must finish the move.
        t = self._task()
        self._put("/coordination/tasks/t-1.json", t)
        self._put("/coordination/archive/tasks/2026-05/t-1.json", t)  # duplicate from crash
        ok = cli._archive_task(t, backend=self.backend)
        self.assertTrue(ok)
        self.assertFalse(self._exists("/coordination/tasks/t-1.json"))  # original now gone
        self.assertTrue(self._exists("/coordination/archive/index/t-1.json"))  # shard written

    def test_upload_failure_leaves_original_intact(self):
        # If the archive upload fails (verify finds nothing), the original is NOT
        # deleted — no-loss by construction.
        t = self._task()
        self._put("/coordination/tasks/t-1.json", t)
        with patch("fulcra_coord.cli.remote.upload_json", return_value=False):
            ok = cli._archive_task(t, backend=self.backend)
        self.assertFalse(ok)
        self.assertTrue(self._exists("/coordination/tasks/t-1.json"))

    def test_archive_evicts_local_cache(self):
        # A successful move must also drop the LOCAL cache copy, else the
        # archiving host's _load_all_tasks (cache-seeded) would rebuild the task
        # straight back into views on its next reconcile (resurrection).
        from fulcra_coord import cache
        t = self._task()
        self._put("/coordination/tasks/t-1.json", t)
        cache.write_cached_task(t)
        self.assertIsNotNone(cache.read_cached_task("t-1"))
        self.assertTrue(cli._archive_task(t, backend=self.backend))
        self.assertIsNone(cache.read_cached_task("t-1"),
                          "archived task must be evicted from the local cache")

    def test_stale_cache_phantom_not_archived(self):
        # B2: a task that exists ONLY in this host's stale LOCAL cache (deleted
        # remotely by another host, never archived here) must NOT be uploaded as
        # a phantom archive body+shard. _archive_task must require a positive
        # stat of the hot tasks/<id>.json before the move, like the reroute
        # sweep's `if fresh is None: continue` guard. The phantom is skipped and
        # evicted from the local cache so it stops getting reloaded.
        from fulcra_coord import cache
        t = self._task()
        # NO hot remote copy (self._put NOT called) and NO archive copy: pure
        # stale-cache phantom.
        cache.write_cached_task(t)
        ok = cli._archive_task(t, backend=self.backend)
        self.assertFalse(ok, "phantom (no hot copy) must not report a successful move")
        self.assertFalse(self._exists("/coordination/archive/tasks/2026-05/t-1.json"),
                         "no phantom archive body should be written")
        self.assertFalse(self._exists("/coordination/archive/index/t-1.json"),
                         "no phantom shard should be written")
        self.assertIsNone(cache.read_cached_task("t-1"),
                          "the stale-cache phantom must be evicted so it stops reloading")

    def test_already_archived_with_hot_copy_still_finishes(self):
        # B2 regression guard: the idempotent / crash-recovery path (archive body
        # already present) must STILL complete the move when the hot copy exists.
        t = self._task()
        self._put("/coordination/tasks/t-1.json", t)
        self.assertTrue(cli._archive_task(t, backend=self.backend))  # first move
        # Re-archiving an already-archived id (hot copy now gone, archive present)
        # is a no-op that still returns True.
        self.assertTrue(cli._archive_task(t, backend=self.backend))


class TestIndexShards(_FakeBus):
    def _shard(self, tid):
        return {"schema": "fulcra.coordination.archive_index.v1", "id": tid,
                "title": f"task {tid}", "status": "done", "workstream": "ws",
                "owner_agent": "a", "done_at": "2026-05-01T00:00:00Z",
                "archived_at": "2026-06-05T00:00:00Z",
                "archive_path": f"/coordination/archive/tasks/2026-05/{tid}.json"}

    def test_lists_all_shards(self):
        for tid in ("t-1", "t-2", "t-3"):
            self._put(f"/coordination/archive/index/{tid}.json", self._shard(tid))
        shards = cli._list_index_shards(backend=self.backend)
        self.assertEqual({s["id"] for s in shards}, {"t-1", "t-2", "t-3"})

    def test_empty_archive_returns_empty(self):
        self.assertEqual(cli._list_index_shards(backend=self.backend), [])

    def test_read_single_shard(self):
        self._put("/coordination/archive/index/t-9.json", self._shard("t-9"))
        s = cli._read_index_shard("t-9", backend=self.backend)
        self.assertEqual(s["archive_path"], "/coordination/archive/tasks/2026-05/t-9.json")
        self.assertIsNone(cli._read_index_shard("missing", backend=self.backend))


# ----------------------------------------------------------------------------
# Task 4 — search --archived + restore command + wiring
# ----------------------------------------------------------------------------


class TestSearchArchived(unittest.TestCase):
    def _args(self, query, archived=False, fmt="json"):
        ns = type("A", (), {})()
        ns.query, ns.archived, ns.format = query, archived, fmt
        return ns

    def test_default_search_does_not_list_archive(self):
        with patch("fulcra_coord.cli.cache.read_cached_view", return_value=None), \
             patch("fulcra_coord.retention._load_task_summaries", return_value=[]), \
             patch("fulcra_coord.retention._list_index_shards") as shards:
            rc = cli.cmd_search(self._args("anything"), backend=["false"])
        self.assertEqual(rc, 0)
        shards.assert_not_called()

    def test_archived_search_finds_shard_match(self):
        shard = {"id": "t-1", "title": "migrate the widget", "status": "done",
                 "workstream": "ws", "owner_agent": "a", "done_at": "x",
                 "archive_path": "/coordination/archive/tasks/2026-05/t-1.json"}
        out = io.StringIO()
        with patch("fulcra_coord.cli.cache.read_cached_view", return_value=None), \
             patch("fulcra_coord.retention._load_task_summaries", return_value=[]), \
             patch("fulcra_coord.retention._list_index_shards", return_value=[shard]), \
             contextlib.redirect_stdout(out):
            rc = cli.cmd_search(self._args("widget", archived=True), backend=["false"])
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        ids = [r["id"] for r in payload["results"]]
        self.assertIn("t-1", ids)

    def test_archived_search_marks_results_and_carries_path(self):
        shard = {"id": "t-1", "title": "migrate the widget", "status": "done",
                 "workstream": "ws", "owner_agent": "a", "done_at": "x",
                 "archive_path": "/coordination/archive/tasks/2026-05/t-1.json"}
        out = io.StringIO()
        with patch("fulcra_coord.cli.cache.read_cached_view", return_value=None), \
             patch("fulcra_coord.retention._load_task_summaries", return_value=[]), \
             patch("fulcra_coord.retention._list_index_shards", return_value=[shard]), \
             contextlib.redirect_stdout(out):
            cli.cmd_search(self._args("widget", archived=True), backend=["false"])
        rec = json.loads(out.getvalue())["results"][0]
        self.assertTrue(rec["archived"])
        self.assertEqual(rec["archive_path"], shard["archive_path"])

    def test_archived_search_no_match_returns_only_hot(self):
        shard = {"id": "t-1", "title": "migrate the widget", "status": "done",
                 "workstream": "ws", "owner_agent": "a", "done_at": "x",
                 "archive_path": "/coordination/archive/tasks/2026-05/t-1.json"}
        out = io.StringIO()
        with patch("fulcra_coord.cli.cache.read_cached_view", return_value=None), \
             patch("fulcra_coord.retention._load_task_summaries", return_value=[]), \
             patch("fulcra_coord.retention._list_index_shards", return_value=[shard]), \
             contextlib.redirect_stdout(out):
            cli.cmd_search(self._args("nonexistent-term", archived=True), backend=["false"])
        self.assertEqual(json.loads(out.getvalue())["results"], [])


class TestRestore(_FakeBus):
    def _args(self, tid, fmt="table"):
        ns = type("A", (), {})()
        ns.task_id, ns.format = tid, fmt
        return ns

    def test_restore_moves_body_back_and_deletes_shard(self):
        body = {"id": "t-1", "title": "old", "status": "done",
                "done_at": "2026-05-01T00:00:00Z", "updated_at": "2026-05-01T00:00:00Z"}
        ap = "/coordination/archive/tasks/2026-05/t-1.json"
        self._put(ap, body)
        self._put("/coordination/archive/index/t-1.json",
                  {"id": "t-1", "archive_path": ap})
        rc = cli.cmd_restore(self._args("t-1"), backend=self.backend)
        self.assertEqual(rc, 0)
        self.assertTrue(self._exists("/coordination/tasks/t-1.json"))
        self.assertFalse(self._exists("/coordination/archive/index/t-1.json"))

    def test_restore_unknown_id_is_error(self):
        rc = cli.cmd_restore(self._args("nope"), backend=self.backend)
        self.assertEqual(rc, 1)

    def test_restore_missing_body_keeps_shard(self):
        # Shard present but archived body gone (corrupt state): error, keep shard.
        self._put("/coordination/archive/index/t-1.json",
                  {"id": "t-1", "archive_path": "/coordination/archive/tasks/2026-05/t-1.json"})
        rc = cli.cmd_restore(self._args("t-1"), backend=self.backend)
        self.assertEqual(rc, 1)
        self.assertTrue(self._exists("/coordination/archive/index/t-1.json"))


class TestWiring(unittest.TestCase):
    def test_restore_in_command_map(self):
        from fulcra_coord import entry
        self.assertIn("restore", entry.COMMAND_MAP)
        self.assertIs(entry.COMMAND_MAP["restore"], cli.cmd_restore)

    def test_search_parses_archived_flag(self):
        from fulcra_coord import entry
        ns = entry.build_parser().parse_args(["search", "q", "--archived"])
        self.assertTrue(ns.archived)
        ns2 = entry.build_parser().parse_args(["search", "q", "--all"])
        self.assertTrue(ns2.archived)
        ns3 = entry.build_parser().parse_args(["search", "q"])
        self.assertFalse(ns3.archived)

    def test_restore_parses_task_id(self):
        from fulcra_coord import entry
        ns = entry.build_parser().parse_args(["restore", "t-1"])
        self.assertEqual(ns.task_id, "t-1")
        self.assertEqual(ns.command, "restore")


# ----------------------------------------------------------------------------
# Task 5 — _run_retention folded into cmd_reconcile (throttle + bound + loop)
# ----------------------------------------------------------------------------


class TestRetentionMarker(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)
        os.environ["FULCRA_COORD_REMOTE_ROOT"] = "/coordination"

    def tearDown(self):
        os.environ.pop("FULCRA_COORD_REMOTE_ROOT", None)

    def test_absent_marker_is_claimed(self):
        with patch("fulcra_coord.cli.remote.download_json", return_value=None), \
             patch("fulcra_coord.cli.remote.upload_json", return_value=True):
            self.assertTrue(cli._claim_retention_marker(self.now, backend=["false"]))

    def test_today_marker_blocks_second_host(self):
        today = {"date": "2026-06-05", "by": "other-host"}
        with patch("fulcra_coord.cli.remote.download_json", return_value=today), \
             patch("fulcra_coord.cli.remote.upload_json") as up:
            self.assertFalse(cli._claim_retention_marker(self.now, backend=["false"]))
        up.assert_not_called()

    def test_yesterday_marker_allows_new_claim(self):
        yest = {"date": "2026-06-04", "by": "x"}
        with patch("fulcra_coord.cli.remote.download_json", return_value=yest), \
             patch("fulcra_coord.cli.remote.upload_json", return_value=True):
            self.assertTrue(cli._claim_retention_marker(self.now, backend=["false"]))

    def test_claim_error_skips(self):
        with patch("fulcra_coord.cli.remote.download_json", side_effect=RuntimeError):
            self.assertFalse(cli._claim_retention_marker(self.now, backend=["false"]))


class TestRunRetention(_FakeBus):
    def _terminal(self, tid, days_ago=40):
        ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(
            timespec="microseconds").replace("+00:00", "Z")
        return {"id": tid, "title": tid, "status": "done", "workstream": "ws",
                "owner_agent": "a", "done_at": ts, "updated_at": ts}

    def _active(self, tid):
        ts = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        return {"id": tid, "title": tid, "status": "active", "workstream": "ws",
                "owner_agent": "a", "updated_at": ts}

    def test_archives_only_terminal_aged(self):
        tasks = [self._terminal("old-1"), self._terminal("old-2"), self._active("live-1")]
        for t in tasks:
            self._put(f"/coordination/tasks/{t['id']}.json", t)
        now = datetime.now(timezone.utc)
        with patch("fulcra_coord.retention._claim_retention_marker", return_value=True):
            res = cli._run_retention(tasks, now=now, deadline=time.monotonic() + 60, backend=self.backend)
        self.assertEqual(res["archived"], 2)
        self.assertFalse(self._exists("/coordination/tasks/old-1.json"))
        self.assertTrue(self._exists("/coordination/tasks/live-1.json"))

    def test_throttle_skips_when_already_ran(self):
        tasks = [self._terminal("old-1")]
        self._put("/coordination/tasks/old-1.json", tasks[0])
        with patch("fulcra_coord.retention._claim_retention_marker", return_value=False):
            res = cli._run_retention(tasks, now=datetime.now(timezone.utc),
                                     deadline=time.monotonic() + 60, backend=self.backend)
        self.assertEqual(res, {"skipped": True})
        self.assertTrue(self._exists("/coordination/tasks/old-1.json"))  # untouched

    def test_cap_defers_remainder(self):
        tasks = [self._terminal(f"old-{i}") for i in range(5)]
        for t in tasks:
            self._put(f"/coordination/tasks/{t['id']}.json", t)
        os.environ["FULCRA_COORD_RETENTION_MAX_PER_RUN"] = "2"
        try:
            with patch("fulcra_coord.retention._claim_retention_marker", return_value=True):
                res = cli._run_retention(tasks, now=datetime.now(timezone.utc),
                                         deadline=time.monotonic() + 60, backend=self.backend)
        finally:
            del os.environ["FULCRA_COORD_RETENTION_MAX_PER_RUN"]
        self.assertEqual(res["archived"], 2)
        self.assertEqual(res["deferred"], 3)

    def test_time_budget_skips_when_deadline_gone(self):
        # An already-passed deadline skips the WHOLE pass before any I/O (incl. the
        # throttle-marker read/write) so retention never overruns reconcile's
        # ceiling; the next tick with a fresh budget picks it up. Never raises,
        # nothing archived.
        tasks = [self._terminal(f"old-{i}") for i in range(3)]
        for t in tasks:
            self._put(f"/coordination/tasks/{t['id']}.json", t)
        with patch("fulcra_coord.retention._claim_retention_marker", return_value=True) as claim:
            res = cli._run_retention(tasks, now=datetime.now(timezone.utc),
                                     deadline=time.monotonic() - 1, backend=self.backend)
        self.assertEqual(res, {"skipped": True})
        claim.assert_not_called()  # budget gate is BEFORE the marker I/O
        self.assertTrue(self._exists("/coordination/tasks/old-0.json"))  # untouched


    def test_per_item_failure_does_not_block_others(self):
        tasks = [self._terminal("good-1"), self._terminal("good-2")]
        for t in tasks:
            self._put(f"/coordination/tasks/{t['id']}.json", t)
        calls = {"n": 0}
        real = retention._archive_task
        def flaky(task, *, backend=None):
            calls["n"] += 1
            if task["id"] == "good-1":
                return False  # simulate a transient failure on one item
            return real(task, backend=backend)
        with patch("fulcra_coord.retention._claim_retention_marker", return_value=True), \
             patch("fulcra_coord.retention._archive_task", side_effect=flaky):
            res = cli._run_retention(tasks, now=datetime.now(timezone.utc),
                                     deadline=time.monotonic() + 60, backend=self.backend)
        self.assertEqual(res["archived"], 1)  # good-2 still archived

    def test_never_raises(self):
        with patch("fulcra_coord.retention._claim_retention_marker", side_effect=RuntimeError):
            res = cli._run_retention([], now=datetime.now(timezone.utc),
                                     deadline=time.monotonic() + 60, backend=self.backend)
        self.assertEqual(res, {"skipped": True})

    def test_reconcile_calls_run_retention(self):
        with patch("fulcra_coord.cli._run_retention",
                   return_value={"archived": 0, "deferred": 0,
                                 "pruned_markers": 0, "pruned_presence": 0}) as rr:
            ns = type("A", (), {})()
            cli.cmd_reconcile(ns, backend=self.backend)
        rr.assert_called_once()
        # deadline kwarg must be the reconcile deadline (composes, not double-counts).
        self.assertIn("deadline", rr.call_args.kwargs)


class TestPruneMarkers(_FakeBus):
    def test_prunes_old_keeps_recent(self):
        now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)
        self._put("/coordination/digest/markers/2026-05-20-morning.json", {"x": 1})  # old
        self._put("/coordination/digest/markers/2026-06-03-evening.json", {"x": 1})  # recent
        pruned = cli._prune_markers(now, backend=self.backend)
        self.assertEqual(pruned, 1)
        self.assertFalse(self._exists("/coordination/digest/markers/2026-05-20-morning.json"))
        self.assertTrue(self._exists("/coordination/digest/markers/2026-06-03-evening.json"))

    def test_empty_dir_prunes_nothing(self):
        self.assertEqual(cli._prune_markers(datetime.now(timezone.utc), backend=self.backend), 0)


class TestPruneDeadPresence(_FakeBus):
    def _put_presence(self, slug, days_ago):
        ls = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(
            timespec="microseconds").replace("+00:00", "Z")
        self._put(f"/coordination/presence/{slug}.json", {"agent": slug, "last_seen": ls})

    def test_prunes_dead_keeps_live(self):
        self._put_presence("dead-agent", 40)
        self._put_presence("live-agent", 1)
        n = cli._prune_dead_presence(datetime.now(timezone.utc), backend=self.backend)
        self.assertEqual(n, 1)
        self.assertFalse(self._exists("/coordination/presence/dead-agent.json"))
        self.assertTrue(self._exists("/coordination/presence/live-agent.json"))

    def test_skips_presence_aggregate_view(self):
        # The aggregate lives under views/, not presence/, so it's never listed
        # here — but guard that a malformed record without last_seen is kept.
        self._put("/coordination/presence/weird.json", {"agent": "weird"})
        n = cli._prune_dead_presence(datetime.now(timezone.utc), backend=self.backend)
        self.assertEqual(n, 0)
        self.assertTrue(self._exists("/coordination/presence/weird.json"))


class TestHotPathExclusion(_FakeBus):
    def test_archived_task_absent_from_rebuilt_views(self):
        old_ts = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat(
            timespec="microseconds").replace("+00:00", "Z")
        old = {"id": "old-1", "title": "old", "status": "done", "workstream": "ws",
               "owner_agent": "a", "done_at": old_ts, "updated_at": old_ts}
        live = {"id": "live-1", "title": "live", "status": "active", "workstream": "ws",
                "owner_agent": "a", "updated_at": datetime.now(timezone.utc).isoformat(
                    timespec="microseconds").replace("+00:00", "Z")}
        self._put("/coordination/tasks/old-1.json", old)
        self._put("/coordination/tasks/live-1.json", live)
        # Seed an EMPTY summaries aggregate so the rebuild takes the self-heal path
        # (which lists tasks/) rather than the backward-compat _load_all_tasks path
        # (which seeds ids from index/search/next views absent in this fixture).
        # The empty aggregate forces the self-heal listing to repopulate from the
        # durable tasks/ files — exactly the membership a move shrinks.
        self._put("/coordination/views/summaries.json",
                  {"schema": "fulcra.coordination.summaries.v1", "summaries": []})
        # Archive the old one (the move), then rebuild the self-heal source.
        self.assertTrue(cli._archive_task(old, backend=self.backend))
        # The self-heal listing (tasks/) must no longer include old-1.
        listed = remote.list_files("/coordination/tasks/", backend=self.backend)
        ids = {p.rsplit("/", 1)[-1][:-5] for p in listed if p.endswith(".json")}
        self.assertNotIn("old-1", ids)
        self.assertIn("live-1", ids)
        # And a summaries rebuild over the remaining tasks excludes it with NO filter.
        rebuilt = cli._load_summaries_for_rebuild(live, backend=self.backend)
        rebuilt_ids = {s["id"] for s in rebuilt}
        self.assertNotIn("old-1", rebuilt_ids)
        self.assertIn("live-1", rebuilt_ids)

    def test_reconcile_does_not_resurrect_archived_via_local_cache(self):
        # REGRESSION: the host that finished a task has its body in the LOCAL
        # cache. cmd_reconcile -> _load_all_tasks seeds task_map from
        # cache.list_cached_tasks() and only ADDS remote ids (never removes), so
        # before the fix the archived task was rebuilt straight back into the
        # authoritative summaries.json on the archiving host's next reconcile,
        # then propagated fleet-wide. The archive MOVE must evict the local cache.
        from fulcra_coord import cache
        import contextlib, io
        old_ts = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat(
            timespec="microseconds").replace("+00:00", "Z")
        old = {"id": "TASK-old", "title": "old", "status": "done", "workstream": "ws",
               "owner_agent": "a", "done_at": old_ts, "updated_at": old_ts}
        self._put("/coordination/tasks/TASK-old.json", old)
        cache.write_cached_task(old)  # the finishing host's local copy
        self.assertTrue(cli._archive_task(old, backend=self.backend))
        ns = type("A", (), {})()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            cli.cmd_reconcile(ns, backend=self.backend)
        summaries = self._read("/coordination/views/summaries.json")
        ids = {s["id"] for s in summaries.get("summaries", [])}
        self.assertNotIn("TASK-old", ids,
                         "reconcile resurrected the archived task via the local cache")


# ----------------------------------------------------------------------------
# Broadcast auto-expiry — stale never-claimed broadcasts age out of the bus.
# ----------------------------------------------------------------------------

def _broadcast(created_days_ago, now, status="proposed", assignee=views.BROADCAST,
               tid="TASK-bcast"):
    """A broadcast-shaped task created `created_days_ago` before `now`. created_at
    drives expiry (not updated_at), so updated_at is set fresh to prove it's ignored."""
    created = (now - timedelta(days=created_days_ago)).isoformat(
        timespec="microseconds").replace("+00:00", "Z")
    return {"id": tid, "title": tid, "status": status, "assignee": assignee,
            "workstream": "ws", "owner_agent": "",
            "created_at": created,
            # updated_at deliberately FRESH (now): a view rebuild can bump it, but
            # expiry must measure from created_at, so a fresh updated_at must NOT
            # save an old broadcast from expiring.
            "updated_at": now.isoformat(timespec="microseconds").replace("+00:00", "Z")}


class TestIsExpirableBroadcast(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)

    def test_old_proposed_broadcast_is_expirable(self):
        # assignee="*", proposed, created 20d ago, default 14d window => expirable.
        t = _broadcast(20, self.now)
        self.assertTrue(views.is_expirable_broadcast(t, self.now))

    def test_concrete_assignee_never_expirable(self):
        # A directive addressed to a REAL agent is a concrete ask — never expired,
        # however old (mirrors is_aged_out_broadcast's concrete-assignee guarantee).
        t = _broadcast(20, self.now, assignee="claude-code:host:repo")
        self.assertFalse(views.is_expirable_broadcast(t, self.now))

    def test_non_proposed_broadcast_never_expirable(self):
        # A broadcast deliberately parked (waiting) or already picked up (active)
        # is not un-acted-on noise — only `proposed` broadcasts expire.
        for status in ("waiting", "active"):
            t = _broadcast(20, self.now, status=status)
            self.assertFalse(views.is_expirable_broadcast(t, self.now), status)

    def test_missing_or_garbage_created_at_fails_safe(self):
        # SAFETY: expiry drives a DESTRUCTIVE abandon->archive. Unlike
        # is_aged_out_broadcast (a read filter that fails toward aging a clockless
        # broadcast OUT via _age_hours -> +inf), is_expirable_broadcast must FAIL
        # SAFE: a missing/unparseable created_at => False (never expire what we
        # can't date), exactly like is_archivable_task.
        missing = {"id": "x", "status": "proposed", "assignee": views.BROADCAST}
        self.assertFalse(views.is_expirable_broadcast(missing, self.now))
        garbage = {"id": "x", "status": "proposed", "assignee": views.BROADCAST,
                   "created_at": "not-a-timestamp"}
        self.assertFalse(views.is_expirable_broadcast(garbage, self.now))
        # Contrast: the read-only age-out predicate WOULD age the clockless
        # broadcast out (fail-toward-cleanup) — opposite direction, by design.
        self.assertTrue(views.is_aged_out_broadcast(missing, self.now))

    def test_recent_broadcast_within_window_not_expirable(self):
        # created 5d ago, default 14d window => still inside the window.
        t = _broadcast(5, self.now)
        self.assertFalse(views.is_expirable_broadcast(t, self.now))

    def test_exactly_at_cutoff_is_expirable(self):
        # >= boundary matches is_archivable_task: created exactly N days ago expires.
        t = _broadcast(14, self.now)
        self.assertTrue(views.is_expirable_broadcast(t, self.now))

    def test_env_override_shrinks_window(self):
        os.environ["FULCRA_COORD_BROADCAST_EXPIRY_DAYS"] = "2"
        try:
            t = _broadcast(5, self.now)  # 5d > 2d window
            self.assertTrue(views.is_expirable_broadcast(t, self.now))
        finally:
            del os.environ["FULCRA_COORD_BROADCAST_EXPIRY_DAYS"]

    def test_env_default_is_14(self):
        self.assertEqual(views._broadcast_expiry_days(), 14.0)


class TestExpireStaleBroadcasts(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)

    def test_expires_only_stale_broadcasts(self):
        stale = _broadcast(20, self.now, tid="TASK-stale")
        fresh = _broadcast(1, self.now, tid="TASK-fresh")
        concrete = _broadcast(20, self.now, assignee="claude-code:h:r",
                              tid="TASK-concrete")
        written = []
        with patch("fulcra_coord.retention._write_task_and_views",
                   return_value=True) as wtv, \
             patch("fulcra_coord.retention.cache.write_cached_task") as wct:
            wtv.side_effect = lambda task, **kw: written.append(task) or True
            n = retention._expire_stale_broadcasts(
                [stale, fresh, concrete], self.now, backend=["false"])
        self.assertEqual(n, 1)
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0]["id"], "TASK-stale")
        self.assertEqual(written[0]["status"], "abandoned")
        wct.assert_called_once()
        # the fresh broadcast and the concrete ask were never written/abandoned.
        self.assertEqual({t["id"] for t in written}, {"TASK-stale"})

    def test_per_run_cap_honored(self):
        b1 = _broadcast(20, self.now, tid="TASK-b1")
        b2 = _broadcast(20, self.now, tid="TASK-b2")
        with patch("fulcra_coord.retention._write_task_and_views", return_value=True), \
             patch("fulcra_coord.retention.cache.write_cached_task"), \
             patch("fulcra_coord.retention._retention_max_per_run", return_value=1):
            n = retention._expire_stale_broadcasts(
                [b1, b2], self.now, backend=["false"])
        self.assertEqual(n, 1)

    def test_needs_reconcile_counts_as_expired(self):
        # NeedsReconcile means the task BODY was written (views lagged) — count it.
        from fulcra_coord import schema
        stale = _broadcast(20, self.now, tid="TASK-nr")
        with patch("fulcra_coord.retention._write_task_and_views",
                   side_effect=schema.NeedsReconcile("views lagged")), \
             patch("fulcra_coord.retention.cache.write_cached_task"):
            n = retention._expire_stale_broadcasts([stale], self.now, backend=["false"])
        self.assertEqual(n, 1)

    def test_conflict_does_not_count(self):
        # ConflictError => the body was NOT written; skip and don't count.
        from fulcra_coord import schema
        stale = _broadcast(20, self.now, tid="TASK-cf")
        with patch("fulcra_coord.retention._write_task_and_views",
                   side_effect=schema.ConflictError("racing writer")), \
             patch("fulcra_coord.retention.cache.write_cached_task"):
            n = retention._expire_stale_broadcasts([stale], self.now, backend=["false"])
        self.assertEqual(n, 0)
