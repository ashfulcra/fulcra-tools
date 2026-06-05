import os
import unittest
from datetime import datetime, timedelta, timezone

from fulcra_coord import views


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
