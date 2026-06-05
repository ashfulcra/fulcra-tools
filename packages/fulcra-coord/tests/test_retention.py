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
        self.backend = [sys.executable, _FAKE]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        for k in ("FULCRA_FAKE_ROOT", "FULCRA_COORD_REMOTE_ROOT", "FULCRA_COORD_BACKEND"):
            os.environ.pop(k, None)

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
