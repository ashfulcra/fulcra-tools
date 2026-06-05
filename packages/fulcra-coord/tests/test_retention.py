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

from fulcra_coord import cli, remote, views

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
             patch("fulcra_coord.cli._load_task_summaries", return_value=[]), \
             patch("fulcra_coord.cli._list_index_shards") as shards:
            rc = cli.cmd_search(self._args("anything"), backend=["false"])
        self.assertEqual(rc, 0)
        shards.assert_not_called()

    def test_archived_search_finds_shard_match(self):
        shard = {"id": "t-1", "title": "migrate the widget", "status": "done",
                 "workstream": "ws", "owner_agent": "a", "done_at": "x",
                 "archive_path": "/coordination/archive/tasks/2026-05/t-1.json"}
        out = io.StringIO()
        with patch("fulcra_coord.cli.cache.read_cached_view", return_value=None), \
             patch("fulcra_coord.cli._load_task_summaries", return_value=[]), \
             patch("fulcra_coord.cli._list_index_shards", return_value=[shard]), \
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
             patch("fulcra_coord.cli._load_task_summaries", return_value=[]), \
             patch("fulcra_coord.cli._list_index_shards", return_value=[shard]), \
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
             patch("fulcra_coord.cli._load_task_summaries", return_value=[]), \
             patch("fulcra_coord.cli._list_index_shards", return_value=[shard]), \
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
        with patch("fulcra_coord.cli._claim_retention_marker", return_value=True):
            res = cli._run_retention(tasks, now=now, deadline=time.monotonic() + 60, backend=self.backend)
        self.assertEqual(res["archived"], 2)
        self.assertFalse(self._exists("/coordination/tasks/old-1.json"))
        self.assertTrue(self._exists("/coordination/tasks/live-1.json"))

    def test_throttle_skips_when_already_ran(self):
        tasks = [self._terminal("old-1")]
        self._put("/coordination/tasks/old-1.json", tasks[0])
        with patch("fulcra_coord.cli._claim_retention_marker", return_value=False):
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
            with patch("fulcra_coord.cli._claim_retention_marker", return_value=True):
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
        with patch("fulcra_coord.cli._claim_retention_marker", return_value=True) as claim:
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
        real = cli._archive_task
        def flaky(task, *, backend=None):
            calls["n"] += 1
            if task["id"] == "good-1":
                return False  # simulate a transient failure on one item
            return real(task, backend=backend)
        with patch("fulcra_coord.cli._claim_retention_marker", return_value=True), \
             patch("fulcra_coord.cli._archive_task", side_effect=flaky):
            res = cli._run_retention(tasks, now=datetime.now(timezone.utc),
                                     deadline=time.monotonic() + 60, backend=self.backend)
        self.assertEqual(res["archived"], 1)  # good-2 still archived

    def test_never_raises(self):
        with patch("fulcra_coord.cli._claim_retention_marker", side_effect=RuntimeError):
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

