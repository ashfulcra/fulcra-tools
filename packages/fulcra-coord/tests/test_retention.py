import contextlib
import io
import json
import os
import shutil
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

    # --- restored_at (F7, 2026-06-11 wave): a restore stamps restored_at and
    # --- archive eligibility ages from max(done_at, restored_at), so a restored
    # --- task gets a FRESH retention window instead of an instant re-archive.

    def test_fresh_restore_is_not_archivable(self):
        # done 40d ago (aged) but restored 1d ago: the restore opens a fresh
        # window — NOT archivable, else the next daily pass silently undoes it.
        t = {"status": "done", "done_at": _dt(40, self.now),
             "updated_at": _dt(40, self.now), "restored_at": _dt(1, self.now)}
        self.assertFalse(views.is_archivable_task(t, self.now, 30))

    def test_aged_restore_is_archivable_again(self):
        # restored 31d ago: the fresh window has itself expired — archivable.
        t = {"status": "done", "done_at": _dt(90, self.now),
             "updated_at": _dt(90, self.now), "restored_at": _dt(31, self.now)}
        self.assertTrue(views.is_archivable_task(t, self.now, 30))

    def test_unparseable_restored_at_is_kept(self):
        # A restore demonstrably happened but can't be dated: fail toward
        # KEEPING (archiving is a destructive move — never move what we can't date).
        t = {"status": "done", "done_at": _dt(40, self.now),
             "updated_at": _dt(40, self.now), "restored_at": "garbage"}
        self.assertFalse(views.is_archivable_task(t, self.now, 30))

    def test_older_restored_at_does_not_shrink_done_age(self):
        # restored BEFORE the done stamp (weird but possible after manual edits):
        # max() keeps aging from done_at — restore never makes a task MORE archivable.
        t = {"status": "done", "done_at": _dt(10, self.now),
             "updated_at": _dt(10, self.now), "restored_at": _dt(50, self.now)}
        self.assertFalse(views.is_archivable_task(t, self.now, 30))


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


class TestIsPrunableEscalationMarker(unittest.TestCase):
    """Role vacancy-escalation daily markers (roles/<name>/escalations/<DAY>.json)
    are minted per vacant role per day and were never pruned — they accumulated
    forever AND every roles listing paid to download them (2026-06-11 wave).
    Same parse-don't-compare + fail-toward-keeping discipline as the digest
    markers, on the same MARKER retention window."""

    def setUp(self):
        self.now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)

    def test_old_escalation_marker_prunable(self):
        path = "/coordination/roles/triage-lead/escalations/2026-05-20.json"  # 16d
        self.assertTrue(views.is_prunable_escalation_marker(path, self.now, 7))

    def test_recent_escalation_marker_kept(self):
        path = "/coordination/roles/triage-lead/escalations/2026-06-03.json"  # 2d
        self.assertFalse(views.is_prunable_escalation_marker(path, self.now, 7))

    def test_exactly_at_cutoff_prunable(self):
        path = "/coordination/roles/triage-lead/escalations/2026-05-29.json"  # 7d
        self.assertTrue(views.is_prunable_escalation_marker(path, self.now, 7))

    def test_undatable_kept(self):
        # Never delete what we can't date — destructive-op safe direction.
        for path in ("/coordination/roles/r/escalations/garbage.json",
                     "/coordination/roles/r/escalations/.json",
                     "/coordination/roles/r/escalations/2026-13-99-extra.json"):
            self.assertFalse(views.is_prunable_escalation_marker(path, self.now, 7), path)

    def test_non_escalation_paths_never_match(self):
        # The role record and lease files share the roles/ prefix but are NOT
        # markers — they must never be prunable, however old their names look.
        self.assertFalse(views.is_prunable_escalation_marker(
            "/coordination/roles/2026-05-01.json", self.now, 7))
        self.assertFalse(views.is_prunable_escalation_marker(
            "/coordination/roles/r/leases/2026-05-01.json", self.now, 7))


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


class TestPruneMarkersSweepsEscalations(_FakeBus):
    """retention._prune_markers must also sweep aged role vacancy-escalation
    markers (2026-06-11 wave): they are daily first-writer-wins dedup guards
    exactly like the digest markers — regenerable, no history value — but lived
    outside the digest/markers/ prefix and so leaked forever."""

    def _seed(self):
        # An aged digest marker (the pre-existing sweep), plus the new family:
        self._put("/coordination/digest/markers/2026-05-01-morning.json", {"x": 1})
        # aged escalation marker (35d before `now` below) -> pruned
        self._put("/coordination/roles/triage-lead/escalations/2026-05-01.json",
                  {"role": "triage-lead", "date": "2026-05-01"})
        # fresh escalation marker (same day) -> kept
        self._put("/coordination/roles/triage-lead/escalations/2026-06-05.json",
                  {"role": "triage-lead", "date": "2026-06-05"})
        # undatable file in the escalations dir -> kept (never delete undatable)
        self._put("/coordination/roles/triage-lead/escalations/notes.json",
                  {"free": "form"})
        # role record + lease share the roles/ prefix -> never touched
        self._put("/coordination/roles/triage-lead.json",
                  {"name": "triage-lead", "maintainer": "ash"})
        self._put("/coordination/roles/triage-lead/leases/agent-a.json",
                  {"agent": "agent-a"})

    def test_aged_escalation_pruned_fresh_and_undatable_kept(self):
        self._seed()
        now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)
        n = retention._prune_markers(now, backend=self.backend)
        # both aged markers (digest + escalation) deleted
        self.assertEqual(n, 2)
        self.assertFalse(self._exists(
            "/coordination/roles/triage-lead/escalations/2026-05-01.json"))
        self.assertFalse(self._exists(
            "/coordination/digest/markers/2026-05-01-morning.json"))
        # fresh + undatable + non-marker files survive
        self.assertTrue(self._exists(
            "/coordination/roles/triage-lead/escalations/2026-06-05.json"))
        self.assertTrue(self._exists(
            "/coordination/roles/triage-lead/escalations/notes.json"))
        self.assertTrue(self._exists("/coordination/roles/triage-lead.json"))
        self.assertTrue(self._exists(
            "/coordination/roles/triage-lead/leases/agent-a.json"))

    def test_roles_listing_failure_still_prunes_digest_markers(self):
        # The two sweeps are independently best-effort: a roles-prefix listing
        # failure must not abort the digest-marker sweep (and vice versa).
        self._seed()
        now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)
        real_list = remote.list_files

        def _flaky(prefix, *a, **k):
            if prefix.startswith("/coordination/roles"):
                raise RuntimeError("roles listing 504")
            return real_list(prefix, *a, **k)

        with patch("fulcra_coord.retention.remote.list_files", side_effect=_flaky):
            n = retention._prune_markers(now, backend=self.backend)
        self.assertEqual(n, 1)  # the digest marker still pruned
        self.assertTrue(self._exists(
            "/coordination/roles/triage-lead/escalations/2026-05-01.json"))


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


class TestArchiveTombstoneVerify(_FakeBus):
    """_archive_task must require a READABLE cold body, never a bare stat.

    THE BUG (the F7-UNDOING hazard, 2026-06-11 — same stat-dishonesty class as
    #177, opposite direction): the platform delete is SOFT, so after
    cmd_restore deletes the archive copy, stat on the archive path STILL
    answers (version history). The old stat-based gates therefore read the
    tombstoned cold copy as "already archived": the next retention pass on a
    re-aged restored task (reachable since #172 ages from restored_at) skipped
    the fresh upload, its stat-based verify passed, and it DELETED the hot
    copy — body GONE from the hot path with only a tombstone in the archive,
    silently undoing #172's restore-sticks fix.

    Discipline pinned here (the #177 tombstone signature applied to the
    presence question): readable download ⇒ present; not-found-class download
    failure on a reachable bus ⇒ tombstone ⇒ NOT present (fresh upload);
    transient/unknown failure ⇒ unconfirmable ⇒ DEFER, hot copy kept."""

    def _task(self, tid="t-1"):
        return {"id": tid, "title": "old work", "status": "done",
                "workstream": "ws", "owner_agent": "claude-code:h:r",
                "done_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z"}

    def _tombstone(self, remote_path, prior_content="{}"):
        """Model the platform's SOFT delete at ``remote_path``: no live body,
        but a ``.tombstone`` sibling the fake backend answers stat from while
        download fails with the not-found-class stderr."""
        local = Path(self.tmp) / remote_path.lstrip("/")
        local.parent.mkdir(parents=True, exist_ok=True)
        if local.exists():
            local.unlink()
        Path(str(local) + ".tombstone").write_text(prior_content)

    def _restore_args(self, tid):
        ns = type("A", (), {})()
        ns.task_id, ns.format = tid, "table"
        return ns

    def test_re_aged_restore_uploads_fresh_body_over_tombstone(self):
        # (a) restore -> soft-deleted cold copy -> re-age -> retention pass:
        # the tombstoned cold copy must read as NOT present, so a FRESH upload
        # lands and is READABLE before the hot copy may go. Before the fix the
        # tombstone stat read as "already archived": no upload, hot deleted,
        # body lost.
        t = self._task()
        ap = "/coordination/archive/tasks/2026-01/t-1.json"
        self._put(ap, t)
        self._put("/coordination/archive/index/t-1.json",
                  {"id": "t-1", "archive_path": ap})
        self.assertEqual(cli.cmd_restore(self._restore_args("t-1"),
                                         backend=self.backend), 0)
        # The fake's delete is a hard unlink; production's is SOFT. Recreate
        # what production leaves behind: tombstones (prior versions) where
        # restore deleted the archive body AND the index shard.
        self._tombstone(ap, json.dumps(t))
        self._tombstone("/coordination/archive/index/t-1.json",
                        json.dumps({"id": "t-1", "archive_path": ap}))
        # Re-age: the restored task drifts past the retention window again.
        hot = self._read("/coordination/tasks/t-1.json")
        hot["restored_at"] = "2026-02-01T00:00:00Z"
        self._put("/coordination/tasks/t-1.json", hot)
        self.assertTrue(views.is_archivable_task(
            hot, datetime.now(timezone.utc)))  # the scenario is reachable

        ok = cli._archive_task(hot, backend=self.backend)

        self.assertTrue(ok, "a legitimate re-archive must complete")
        archived = remote.download_json(ap, backend=self.backend)
        self.assertIsNotNone(
            archived,
            "the FRESH body must be READABLE in the archive — a tombstone "
            "stat must never count as 'already archived' (no-loss)")
        self.assertEqual(archived.get("restored_at"), "2026-02-01T00:00:00Z",
                         "the archived body must be the fresh hot body, not "
                         "the stale pre-restore version")
        # The shard gate has the same hole: restore tombstoned the shard, and
        # a stat-gate would skip the rewrite, leaving the re-archived task
        # invisible to `search --archived` and unrestorable.
        shard = remote.download_json("/coordination/archive/index/t-1.json",
                                     backend=self.backend)
        self.assertIsNotNone(shard, "a READABLE index shard must be rewritten "
                                    "over the tombstoned one")
        self.assertEqual(shard.get("id"), "t-1")
        # No loss: the hot copy may only be gone because the readable fresh
        # cold copy above exists.
        self.assertFalse(self._exists("/coordination/tasks/t-1.json"))

    def test_tombstoned_archive_transient_probe_failure_defers(self):
        # (b) tombstoned cold copy + TRANSIENT download failure: the presence
        # verdict is unconfirmable — the task must be DEFERRED this pass and
        # the hot copy kept. Before the fix the tombstone stat read as
        # present and the hot copy was deleted on a guess.
        from fulcra_coord_files import store
        t = self._task()
        self._put("/coordination/tasks/t-1.json", t)
        ap = "/coordination/archive/tasks/2026-01/t-1.json"
        self._tombstone(ap, json.dumps(t))

        wrapper = Path(self.tmp) / "transient_download_backend.py"
        wrapper.write_text(
            f"""
import os, sys
if sys.argv[1:2] == ["download"]:
    sys.stderr.write("Error: HTTP Error 504: Gateway Timeout\\n")
    sys.exit(1)
os.execv({sys.executable!r}, [{sys.executable!r}, {_FAKE!r}] + sys.argv[1:])
"""
        )
        backend = [sys.executable, str(wrapper)]
        with patch.object(store, "_RETRY_BACKOFF_SECONDS", 0.0):
            ok = cli._archive_task(t, backend=backend)
        self.assertFalse(ok, "an unconfirmable cold-copy state must defer")
        self.assertTrue(self._exists("/coordination/tasks/t-1.json"),
                        "the hot copy must be kept on an unconfirmable pass")
        self.assertFalse(self._exists(ap),
                         "no upload may land on an unconfirmable pass")

    def test_tombstoned_archive_unreachable_bus_defers(self):
        # (b') tombstone-shaped failure but the bus probe fails: nothing is
        # confirmable on an unreachable bus — defer, keep the hot copy.
        t = self._task()
        self._put("/coordination/tasks/t-1.json", t)
        ap = "/coordination/archive/tasks/2026-01/t-1.json"
        self._tombstone(ap, json.dumps(t))

        wrapper = Path(self.tmp) / "unreachable_backend.py"
        wrapper.write_text(
            f"""
import os, sys
if sys.argv[1:2] == ["list"]:
    sys.stderr.write("Connection refused\\n")
    sys.exit(1)
os.execv({sys.executable!r}, [{sys.executable!r}, {_FAKE!r}] + sys.argv[1:])
"""
        )
        backend = [sys.executable, str(wrapper)]
        ok = cli._archive_task(t, backend=backend)
        self.assertFalse(ok, "tombstone on an unreachable bus is unconfirmable")
        self.assertTrue(self._exists("/coordination/tasks/t-1.json"),
                        "the hot copy must be kept on an unconfirmable pass")

    def test_post_upload_verify_rejects_tombstone_stat_only_state(self):
        # (c) the no-loss gate itself: an upload that CLAIMS success while the
        # archive path stays tombstone-only (stat answers, body unreadable)
        # must NOT verify — the hot delete below it would destroy the only
        # readable copy. Stat passed vacuously before the fix.
        t = self._task()
        self._put("/coordination/tasks/t-1.json", t)
        ap = "/coordination/archive/tasks/2026-01/t-1.json"
        self._tombstone(ap, json.dumps(t))

        with patch("fulcra_coord.retention.remote.upload_json",
                   return_value=True):  # lies: writes nothing
            ok = cli._archive_task(t, backend=self.backend)
        self.assertFalse(ok, "a tombstone-stat-only archive state must not "
                             "pass the post-upload verify")
        self.assertTrue(self._exists("/coordination/tasks/t-1.json"),
                        "the hot copy survives an unverifiable upload")

    def test_restore_then_immediate_pass_still_sticks(self):
        # The #172 pin, replayed against PRODUCTION's soft delete: a fresh
        # restore (not yet re-aged) whose archive body/shard deletes left
        # tombstones must STILL survive the very next retention pass.
        t = self._task()
        ap = "/coordination/archive/tasks/2026-01/t-1.json"
        self._put(ap, t)
        self._put("/coordination/archive/index/t-1.json",
                  {"id": "t-1", "archive_path": ap})
        self.assertEqual(cli.cmd_restore(self._restore_args("t-1"),
                                         backend=self.backend), 0)
        self._tombstone(ap, json.dumps(t))
        self._tombstone("/coordination/archive/index/t-1.json",
                        json.dumps({"id": "t-1", "archive_path": ap}))
        restored = self._read("/coordination/tasks/t-1.json")
        with patch("fulcra_coord.retention._claim_retention_marker",
                   return_value=True):
            res = cli._run_retention([restored], now=datetime.now(timezone.utc),
                                     deadline=time.monotonic() + 60,
                                     backend=self.backend)
        self.assertEqual(res.get("archived", 0), 0)
        self.assertTrue(self._exists("/coordination/tasks/t-1.json"),
                        "restore must stick even with tombstoned cold paths")


class TestRestoreTombstoneVerify(_FakeBus):
    """cmd_restore's mirror case: its post-upload verify of the HOT copy must
    require a READABLE body. The hot path tasks/<id>.json is tombstoned in
    production (the original archive move soft-deleted it), so a stat-based
    verify passes vacuously even when the upload never landed — and the
    archive-body delete right after it would destroy the ONLY readable copy."""

    def _task(self):
        return {"id": "t-1", "title": "old", "status": "done",
                "workstream": "ws", "owner_agent": "a",
                "done_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z"}

    def _tombstone(self, remote_path, prior_content="{}"):
        local = Path(self.tmp) / remote_path.lstrip("/")
        local.parent.mkdir(parents=True, exist_ok=True)
        if local.exists():
            local.unlink()
        Path(str(local) + ".tombstone").write_text(prior_content)

    def _args(self, tid):
        ns = type("A", (), {})()
        ns.task_id, ns.format = tid, "table"
        return ns

    def test_restore_verify_rejects_tombstoned_hot_path(self):
        # (d) lying upload over a tombstoned hot path: the verify must FAIL
        # and the archive body + shard must be KEPT. Before the fix the
        # tombstone's stat verified the restore, the archive body was deleted,
        # and the task body was gone from BOTH sides.
        t = self._task()
        ap = "/coordination/archive/tasks/2026-01/t-1.json"
        self._put(ap, t)
        self._put("/coordination/archive/index/t-1.json",
                  {"id": "t-1", "archive_path": ap})
        self._tombstone("/coordination/tasks/t-1.json", json.dumps(t))

        with patch("fulcra_coord.retention.remote.upload_json",
                   return_value=True):  # lies: writes nothing
            rc = cli.cmd_restore(self._args("t-1"), backend=self.backend)
        self.assertEqual(rc, 1, "an unreadable hot body must not verify")
        self.assertTrue(self._exists(ap),
                        "the archive body — the only readable copy — is kept")
        self.assertTrue(self._exists("/coordination/archive/index/t-1.json"),
                        "the index shard is kept for the retry")

    def test_restore_succeeds_over_tombstoned_hot_path(self):
        # Positive control: a REAL upload over the tombstoned hot path (the
        # production-normal restore) verifies readable and completes — the
        # strengthened verify must not spuriously fail on the tombstone
        # sibling left by the original archive move.
        t = self._task()
        ap = "/coordination/archive/tasks/2026-01/t-1.json"
        self._put(ap, t)
        self._put("/coordination/archive/index/t-1.json",
                  {"id": "t-1", "archive_path": ap})
        self._tombstone("/coordination/tasks/t-1.json", json.dumps(t))

        rc = cli.cmd_restore(self._args("t-1"), backend=self.backend)
        self.assertEqual(rc, 0)
        hot = remote.download_json("/coordination/tasks/t-1.json",
                                   backend=self.backend)
        self.assertIsNotNone(hot, "the restored hot body must be readable")
        self.assertTrue(hot.get("restored_at"))


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

    def test_restore_deletes_archive_body(self):
        # F7 (2026-06-11 wave): restore used to leave the ARCHIVE BODY in
        # place — the next daily retention pass then saw archive_exists=True,
        # skipped the upload, and deleted the hot copy again: a restore not
        # followed by a status transition within ~24h was silently undone.
        # The cold copy must be deleted once the hot body verifiably landed.
        body = {"id": "t-1", "title": "old", "status": "done",
                "done_at": "2026-05-01T00:00:00Z", "updated_at": "2026-05-01T00:00:00Z"}
        ap = "/coordination/archive/tasks/2026-05/t-1.json"
        self._put(ap, body)
        self._put("/coordination/archive/index/t-1.json",
                  {"id": "t-1", "archive_path": ap})
        rc = cli.cmd_restore(self._args("t-1"), backend=self.backend)
        self.assertEqual(rc, 0)
        self.assertTrue(self._exists("/coordination/tasks/t-1.json"))
        self.assertFalse(self._exists(ap),
                         "the archive body must be deleted or the next retention "
                         "pass re-archives via the idempotent archive_exists branch")
        self.assertFalse(self._exists("/coordination/archive/index/t-1.json"))

    def test_restore_stamps_restored_at(self):
        # The restored hot body must carry restored_at so is_archivable_task
        # ages from the restore, not the original done stamp.
        body = {"id": "t-1", "title": "old", "status": "done",
                "done_at": "2026-05-01T00:00:00Z", "updated_at": "2026-05-01T00:00:00Z"}
        ap = "/coordination/archive/tasks/2026-05/t-1.json"
        self._put(ap, body)
        self._put("/coordination/archive/index/t-1.json",
                  {"id": "t-1", "archive_path": ap})
        self.assertEqual(cli.cmd_restore(self._args("t-1"), backend=self.backend), 0)
        hot = self._read("/coordination/tasks/t-1.json")
        self.assertTrue(hot.get("restored_at"),
                        "restore must stamp restored_at on the hot body")
        self.assertIsNotNone(views._parse_dt(hot["restored_at"]))

    def test_restore_archive_delete_failure_keeps_shard_and_errors(self):
        # If the cold-copy delete fails the restore must KEEP the index shard
        # and return non-zero so the operator retries — deleting the shard while
        # the stale archive body lingers would let a later idempotent archive
        # pass resurrect the stale body over post-restore edits.
        body = {"id": "t-1", "title": "old", "status": "done",
                "done_at": "2026-05-01T00:00:00Z", "updated_at": "2026-05-01T00:00:00Z"}
        ap = "/coordination/archive/tasks/2026-05/t-1.json"
        self._put(ap, body)
        self._put("/coordination/archive/index/t-1.json",
                  {"id": "t-1", "archive_path": ap})
        real_delete = remote.delete

        def _flaky_delete(path, *, backend=None):
            if path == ap:
                return False  # the cold-copy delete transiently fails
            return real_delete(path, backend=backend)

        with patch("fulcra_coord.retention.remote.delete", side_effect=_flaky_delete):
            rc = cli.cmd_restore(self._args("t-1"), backend=self.backend)
        self.assertEqual(rc, 1)
        self.assertTrue(self._exists("/coordination/tasks/t-1.json"))  # hot landed
        self.assertTrue(self._exists("/coordination/archive/index/t-1.json"),
                        "shard kept so a retry can finish the move")

    def test_restore_sticks_across_next_retention_pass(self):
        # END-TO-END F7 pin: restore an aged terminal task, then run the very
        # next retention pass — the task must REMAIN hot. Before the fix the
        # pass re-archived it instantly (still terminal + aged from done_at,
        # archive body still present), silently undoing the operator's restore.
        body = {"id": "t-1", "title": "old", "status": "done",
                "workstream": "ws", "owner_agent": "a",
                "done_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"}
        ap = "/coordination/archive/tasks/2026-01/t-1.json"
        self._put(ap, body)
        self._put("/coordination/archive/index/t-1.json",
                  {"id": "t-1", "archive_path": ap})
        self.assertEqual(cli.cmd_restore(self._args("t-1"), backend=self.backend), 0)
        restored = self._read("/coordination/tasks/t-1.json")
        with patch("fulcra_coord.retention._claim_retention_marker", return_value=True):
            res = cli._run_retention([restored], now=datetime.now(timezone.utc),
                                     deadline=time.monotonic() + 60,
                                     backend=self.backend)
        self.assertEqual(res.get("archived", 0), 0,
                         "a just-restored task must not be re-archived next pass")
        self.assertTrue(self._exists("/coordination/tasks/t-1.json"),
                        "restore must stick: the hot body survives the next retention pass")

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

    # _claim_retention_marker returns (claimed, marker) since the loop-2 perf
    # pass (#6): the marker rides up to the health record so the tick never
    # re-downloads retention/last-run.json.

    def test_absent_marker_is_claimed(self):
        with patch("fulcra_coord.cli.remote.download_json", return_value=None), \
             patch("fulcra_coord.cli.remote.upload_json", return_value=True):
            claimed, marker = cli._claim_retention_marker(self.now, backend=["false"])
        self.assertTrue(claimed)
        self.assertEqual(marker["date"], "2026-06-05")  # our own fresh stamp

    def test_today_marker_blocks_second_host(self):
        today = {"date": "2026-06-05", "by": "other-host"}
        with patch("fulcra_coord.cli.remote.download_json", return_value=today), \
             patch("fulcra_coord.cli.remote.upload_json") as up:
            claimed, marker = cli._claim_retention_marker(self.now, backend=["false"])
        self.assertFalse(claimed)
        self.assertEqual(marker, today)  # the observed marker is threaded up
        up.assert_not_called()

    def test_yesterday_marker_allows_new_claim(self):
        yest = {"date": "2026-06-04", "by": "x"}
        with patch("fulcra_coord.cli.remote.download_json", return_value=yest), \
             patch("fulcra_coord.cli.remote.upload_json", return_value=True):
            claimed, marker = cli._claim_retention_marker(self.now, backend=["false"])
        self.assertTrue(claimed)
        self.assertEqual(marker["date"], "2026-06-05")

    def test_claim_error_skips(self):
        with patch("fulcra_coord.cli.remote.download_json", side_effect=RuntimeError):
            claimed, marker = cli._claim_retention_marker(self.now, backend=["false"])
        self.assertFalse(claimed)
        self.assertIsNone(marker)


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
                   return_value=True) as wtv:
            wtv.side_effect = lambda task, **kw: written.append(task) or True
            n = retention._expire_stale_broadcasts(
                [stale, fresh, concrete], self.now, backend=["false"])
        self.assertEqual(n, 1)
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0]["id"], "TASK-stale")
        self.assertEqual(written[0]["status"], "abandoned")
        wtv.assert_called_once()
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
        # ConflictError => the body was NOT written; skip and don't count/cache.
        from fulcra_coord import schema
        stale = _broadcast(20, self.now, tid="TASK-cf")
        with patch("fulcra_coord.retention._write_task_and_views",
                   side_effect=schema.ConflictError("racing writer")), \
             patch("fulcra_coord.retention.cache.write_cached_task") as wct:
            n = retention._expire_stale_broadcasts([stale], self.now, backend=["false"])
        self.assertEqual(n, 0)
        wct.assert_not_called()

    def test_failed_write_return_does_not_count_or_cache(self):
        # A plain False return means the task upload failed, so no expiration landed.
        stale = _broadcast(20, self.now, tid="TASK-fail")
        with patch("fulcra_coord.retention._write_task_and_views", return_value=False), \
             patch("fulcra_coord.retention.cache.write_cached_task") as wct:
            n = retention._expire_stale_broadcasts([stale], self.now, backend=["false"])
        self.assertEqual(n, 0)
        wct.assert_not_called()


# ---------------------------------------------------------------------------
# Continuity-checkpoint retention: recursive prune of checkpoints/ archives
# ---------------------------------------------------------------------------

# Continuity tree root used by the pruner: remote_root() + "/continuity".
# The conftest defaults FULCRA_COORD_REMOTE_ROOT to "/coordination" only inside
# _FakeBus subclasses; the classes below set it explicitly so the mocked
# list_files keys are deterministic regardless of the ambient env.
_CONT_ROOT = "/coordination/continuity"


def _chk(stamp, *, task="task-1", hex_="abcdef0123"):
    """Build a CHK-<stamp>-<task>-<hex>.json filename. <stamp> is the
    zero-padded lexically-sortable timestamp so filename sort == chrono sort."""
    return f"CHK-{stamp}-{task}-{hex_}.json"


class _ContTree(unittest.TestCase):
    """Base for the continuity pruner tests. Models the LIVE ``fulcra file
    list`` contract (the 2026-06-10 measured pass, the same contract
    load_loop_records' top-level path filter is built on, and the shape
    tests/fake_fulcra_backend.py's rglob produces): ONE listing of a prefix
    returns the RECURSIVE set of FILE paths under it, with NO directory
    entries. Tests hand `_run` a flat file list; the pruner must partition it
    by path segments — never descend per-directory (the old walker did, found
    zero children in production, and the checkpoints/ GC silently never ran)."""

    def setUp(self):
        os.environ["FULCRA_COORD_REMOTE_ROOT"] = "/coordination"
        self._prev_keep = os.environ.pop("FULCRA_COORD_CONTINUITY_KEEP", None)
        self.now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)
        self.deleted: list[str] = []
        self.list_calls: list[str] = []

    def tearDown(self):
        os.environ.pop("FULCRA_COORD_REMOTE_ROOT", None)
        if self._prev_keep is None:
            os.environ.pop("FULCRA_COORD_CONTINUITY_KEEP", None)
        else:
            os.environ["FULCRA_COORD_CONTINUITY_KEEP"] = self._prev_keep

    def _list_factory(self, entries):
        """side_effect for list_files: every entry under the asked prefix, the
        recursive live contract. Entries may include trailing-slash dir entries
        for the tolerance tests; real backends return files only."""
        def _list(prefix, *, backend=None, timeout=None):
            self.list_calls.append(prefix)
            norm = prefix if prefix.endswith("/") else prefix + "/"
            return [e for e in entries if e.startswith(norm)]
        return _list

    def _delete_factory(self, raise_on=()):
        def _delete(path, *, backend=None):
            if path in raise_on:
                raise RuntimeError("boom")
            self.deleted.append(path)
            return True
        return _delete

    def _run(self, entries, *, raise_on=(), list_side_effect=None, deadline=None):
        list_se = list_side_effect or self._list_factory(entries)
        with patch("fulcra_coord.retention.remote.list_files", side_effect=list_se), \
             patch("fulcra_coord.retention.remote.delete",
                   side_effect=self._delete_factory(raise_on)):
            return retention._prune_continuity_checkpoints(
                self.now, backend=["false"], deadline=deadline)


class TestPruneContinuityCheckpoints(_ContTree):
    def test_partitions_one_recursive_listing(self):
        # ws -> agent -> task -> checkpoints, 4 path levels deep — but the
        # listing is ONE flat recursive file set (no dir entries anywhere).
        # The pruner must find the checkpoints dir purely by path segments.
        chk = f"{_CONT_ROOT}/ws-hash/agent-a/task-1/checkpoints"
        files = [f"{chk}/{_chk(f'2026010{i}T000000z')}" for i in range(1, 5)]  # 4 files
        entries = files + [f"{_CONT_ROOT}/ws-hash/agent-a/task-1/latest.json"]
        os.environ["FULCRA_COORD_CONTINUITY_KEEP"] = "2"
        n = self._run(entries)
        self.assertEqual(n, 2)
        # The two OLDEST (lexically smallest) are deleted.
        self.assertEqual(sorted(self.deleted), sorted(files[:2]))
        # PERF half of the fix: exactly ONE listing — no per-directory walk.
        self.assertEqual(self.list_calls, [f"{_CONT_ROOT}"])

    def test_keep_newest_n_deletes_oldest(self):
        chk = f"{_CONT_ROOT}/ws/a/t/checkpoints"
        # 15 files, stamps chosen so lexical sort == chronological order.
        stamps = [f"20260101T{h:02d}0000z" for h in range(15)]
        files = [f"{chk}/{_chk(s)}" for s in stamps]
        entries = files + [f"{_CONT_ROOT}/ws/a/t/latest.json"]
        os.environ["FULCRA_COORD_CONTINUITY_KEEP"] = "10"
        n = self._run(entries)
        self.assertEqual(n, 5)
        oldest_5 = files[:5]   # smallest stamps
        newest_10 = files[5:]
        self.assertEqual(sorted(self.deleted), sorted(oldest_5))
        for keep in newest_10:
            self.assertNotIn(keep, self.deleted)
        # latest.json is never in the delete set.
        self.assertNotIn(f"{_CONT_ROOT}/ws/a/t/latest.json", self.deleted)

    def test_per_task_dirs_pruned_independently(self):
        # Two tasks' checkpoints dirs in ONE listing: the keep window applies
        # PER DIR, not across the union (else a chatty task would evict a quiet
        # task's history).
        chk_a = f"{_CONT_ROOT}/ws/a/t1/checkpoints"
        chk_b = f"{_CONT_ROOT}/ws/a/t2/checkpoints"
        files_a = [f"{chk_a}/{_chk(f'2026010{i}T000000z')}" for i in range(1, 5)]  # 4
        files_b = [f"{chk_b}/{_chk(f'2026010{i}T000000z')}" for i in range(1, 3)]  # 2
        os.environ["FULCRA_COORD_CONTINUITY_KEEP"] = "2"
        n = self._run(files_a + files_b)
        self.assertEqual(n, 2)   # only t1 has more than `keep`
        self.assertEqual(sorted(self.deleted), sorted(files_a[:2]))

    def test_latest_json_never_deleted(self):
        chk = f"{_CONT_ROOT}/ws/a/t/checkpoints"
        files = [f"{chk}/{_chk(f'202601{d:02d}T000000z')}" for d in range(1, 13)]
        # A latest.json INSIDE the checkpoints dir as well — must survive.
        latest_in_chk = f"{chk}/latest.json"
        entries = files + [latest_in_chk, f"{_CONT_ROOT}/ws/a/t/latest.json"]
        os.environ["FULCRA_COORD_CONTINUITY_KEEP"] = "10"
        self._run(entries)
        self.assertNotIn(latest_in_chk, self.deleted)
        self.assertNotIn(f"{_CONT_ROOT}/ws/a/t/latest.json", self.deleted)

    def test_keep_ge_count_no_deletions(self):
        chk = f"{_CONT_ROOT}/ws/a/t/checkpoints"
        files = [f"{chk}/{_chk(f'2026010{i}T000000z')}" for i in range(1, 4)]  # 3
        os.environ["FULCRA_COORD_CONTINUITY_KEEP"] = "10"
        n = self._run(files)
        self.assertEqual(n, 0)
        self.assertEqual(self.deleted, [])

    def test_keep_floor_is_one(self):
        # _continuity_keep clamps a 0/negative env to 1: never delete the only/newest.
        os.environ["FULCRA_COORD_CONTINUITY_KEEP"] = "0"
        self.assertEqual(retention._continuity_keep(), 1)
        chk = f"{_CONT_ROOT}/ws/a/t/checkpoints"
        older = f"{chk}/{_chk('20260101T000000z')}"
        newer = f"{chk}/{_chk('20260102T000000z')}"
        n = self._run([older, newer])
        self.assertEqual(n, 1)
        self.assertEqual(self.deleted, [older])  # oldest deleted, newest kept

    def test_keep_default_is_ten(self):
        self.assertEqual(retention._continuity_keep(), 10)

    def test_per_run_cap_limits_deletions(self):
        chk = f"{_CONT_ROOT}/ws/a/t/checkpoints"
        files = [f"{chk}/{_chk(f'202601{d:02d}T000000z')}" for d in range(1, 12)]  # 11
        os.environ["FULCRA_COORD_CONTINUITY_KEEP"] = "1"  # 10 deletable
        with patch("fulcra_coord.retention._retention_max_per_run", return_value=2):
            n = self._run(files)
        self.assertEqual(n, 2)
        self.assertEqual(len(self.deleted), 2)

    def test_list_files_raises_returns_zero(self):
        def _boom(prefix, *, backend=None, timeout=None):
            raise RuntimeError("list blew up")
        n = self._run([], list_side_effect=_boom)
        self.assertEqual(n, 0)
        self.assertEqual(self.deleted, [])

    def test_one_delete_raises_others_still_deleted(self):
        chk = f"{_CONT_ROOT}/ws/a/t/checkpoints"
        files = [f"{chk}/{_chk(f'2026010{i}T000000z')}" for i in range(1, 6)]  # 5
        os.environ["FULCRA_COORD_CONTINUITY_KEEP"] = "1"  # 4 deletable (files[:4])
        # Make the SECOND-oldest deletion raise; the others must still proceed.
        n = self._run(files, raise_on=(files[1],))
        # 4 deletable, one raised -> 3 counted, no exception escapes.
        self.assertEqual(n, 3)
        self.assertNotIn(files[1], self.deleted)
        self.assertIn(files[0], self.deleted)
        self.assertIn(files[2], self.deleted)
        self.assertIn(files[3], self.deleted)

    def test_empty_tree_zero_deletions(self):
        n = self._run([])
        self.assertEqual(n, 0)
        self.assertEqual(self.deleted, [])

    def test_deadline_stops_walk(self):
        # A deadline already past -> the budget gate stops before any delete.
        chk = f"{_CONT_ROOT}/ws/a/t/checkpoints"
        files = [f"{chk}/{_chk(f'2026010{i}T000000z')}" for i in range(1, 6)]
        os.environ["FULCRA_COORD_CONTINUITY_KEEP"] = "1"
        n = self._run(files, deadline=time.monotonic() - 1)
        self.assertEqual(n, 0)
        self.assertEqual(self.deleted, [])

    def test_expired_deadline_skips_tree_listing(self):
        # The walk itself must respect the budget, not just the delete loop.
        # Otherwise a giant continuity tree could burn reconcile's deadline before
        # the pruner ever reaches its first delete.
        with patch("fulcra_coord.retention.remote.list_files") as lf, \
             patch("fulcra_coord.retention.remote.delete") as delete:
            n = retention._prune_continuity_checkpoints(
                self.now, backend=["false"], deadline=time.monotonic() - 1)
        self.assertEqual(n, 0)
        lf.assert_not_called()
        delete.assert_not_called()

    def test_dir_entries_tolerated_same_result(self):
        # TOLERANCE pin: a backend that emits trailing-slash DIRECTORY entries
        # alongside the recursive file paths must partition to the SAME result
        # as a files-only listing — dir entries register the checkpoints dir but
        # are never themselves delete candidates.
        chk = f"{_CONT_ROOT}/ws/a/t/checkpoints"
        files = [f"{chk}/{_chk(f'2026010{i}T000000z')}" for i in range(1, 5)]  # 4
        entries = [f"{_CONT_ROOT}/ws/",
                   f"{_CONT_ROOT}/ws/a/",
                   f"{_CONT_ROOT}/ws/a/t/",
                   f"{chk}/"] + files
        os.environ["FULCRA_COORD_CONTINUITY_KEEP"] = "2"
        n = self._run(entries)
        self.assertEqual(n, 2)
        self.assertEqual(sorted(self.deleted), sorted(files[:2]))


class TestPruneContinuityLiveListingShape(_FakeBus):
    def test_fake_backend_recursive_listing_is_pruned(self):
        # THE F-finding pin (2026-06-11 wave): against the REAL fake backend —
        # whose `list` is recursive files-only (rglob), the measured live
        # contract — the pruner must actually find and bound checkpoints/.
        # The old trailing-slash walker found zero children here and the GC
        # silently never deleted anything.
        chk = "/coordination/continuity/ws/agent/task/checkpoints"
        for i in range(1, 13):   # 12 archives; default keep=10 -> 2 deletable
            self._put(f"{chk}/CHK-202601{i:02d}T000000z-task-abcdef.json", {"i": i})
        self._put("/coordination/continuity/ws/agent/task/latest.json", {"latest": True})
        n = retention._prune_continuity_checkpoints(
            datetime.now(timezone.utc), backend=self.backend)
        self.assertEqual(n, 2)
        self.assertFalse(self._exists(f"{chk}/CHK-20260101T000000z-task-abcdef.json"))
        self.assertFalse(self._exists(f"{chk}/CHK-20260102T000000z-task-abcdef.json"))
        for i in range(3, 13):
            self.assertTrue(self._exists(
                f"{chk}/CHK-202601{i:02d}T000000z-task-abcdef.json"))
        self.assertTrue(self._exists(
            "/coordination/continuity/ws/agent/task/latest.json"))


class TestRunRetentionContinuityWiring(_FakeBus):
    def test_result_includes_pruned_continuity(self):
        # Patch the pruner to a sentinel and assert _run_retention surfaces it in
        # the result dict (the other prune steps run against the empty fake bus).
        with patch("fulcra_coord.retention._claim_retention_marker", return_value=True), \
             patch("fulcra_coord.retention._prune_continuity_checkpoints",
                   return_value=7) as pcc:
            res = cli._run_retention([], now=datetime.now(timezone.utc),
                                     deadline=time.monotonic() + 60, backend=self.backend)
        self.assertEqual(res.get("pruned_continuity"), 7)
        pcc.assert_called_once()
        # deadline must be threaded through so the pruner composes with the budget.
        self.assertIn("deadline", pcc.call_args.kwargs)
