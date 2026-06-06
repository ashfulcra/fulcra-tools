"""Tests for the coordination-system health surface (spec v2).

Covers the pure judgment (views.assess_infra_health + env-knob readers) and the
remote path helpers. All datetime gates go through views._parse_dt; no live bus
I/O — the hermetic conftest defaults FULCRA_COORD_BACKEND=false.
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fulcra_coord import views, heartbeat, remote


class TestHealthKnobs(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("FULCRA_COORD_HEALTH_DEGRADED_SECONDS", None)
        os.environ.pop("FULCRA_COORD_HEALTH_OUTAGE_SECONDS", None)

    def test_degraded_default_is_interval_times_three(self):
        os.environ.pop("FULCRA_COORD_HEALTH_DEGRADED_SECONDS", None)
        self.assertEqual(views._health_degraded_seconds(),
                         heartbeat.INTERVAL_MIN_DEFAULT * 60 * 3)

    def test_degraded_env_override(self):
        os.environ["FULCRA_COORD_HEALTH_DEGRADED_SECONDS"] = "300"
        self.assertEqual(views._health_degraded_seconds(), 300.0)

    def test_degraded_garbage_env_falls_back(self):
        os.environ["FULCRA_COORD_HEALTH_DEGRADED_SECONDS"] = "not-a-number"
        self.assertEqual(views._health_degraded_seconds(),
                         heartbeat.INTERVAL_MIN_DEFAULT * 60 * 3)

    def test_outage_default_is_three_hours(self):
        os.environ.pop("FULCRA_COORD_HEALTH_OUTAGE_SECONDS", None)
        self.assertEqual(views._health_outage_seconds(), 3 * 3600.0)

    def test_outage_env_override(self):
        os.environ["FULCRA_COORD_HEALTH_OUTAGE_SECONDS"] = "7200"
        self.assertEqual(views._health_outage_seconds(), 7200.0)


def _rec(host, slug, ago_s, now):
    return {
        "schema": "fulcra.coordination.health.v1",
        "host": host, "agent": f"claude-code:{host}:repo", "version": "0.9.0",
        "reconcile_at": (now - timedelta(seconds=ago_s)).isoformat(
            timespec="microseconds").replace("+00:00", "Z"),
        "duration_s": 1.2, "tasks_loaded": 5, "views_refreshed": 7,
        "repair_backlog": 0, "retention_last_run": None,
        "listener_last_fire": None, "bus_task_count": 5,
    }


class TestAssessInfraHealth(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)

    def test_fresh_record_is_healthy(self):
        recs = [_rec("mac", "claude-code-mac-repo", 60, self.now)]
        out = views.assess_infra_health(recs, now=self.now,
                                        degraded_after_s=3600, outage_after_s=10800)
        self.assertEqual(out["worst_status"], "healthy")
        self.assertEqual(out["hosts"][0]["status"], "healthy")

    def test_record_past_degraded_is_degraded(self):
        recs = [_rec("mac", "claude-code-mac-repo", 4000, self.now)]  # >3600, <10800
        out = views.assess_infra_health(recs, now=self.now,
                                        degraded_after_s=3600, outage_after_s=10800)
        self.assertEqual(out["hosts"][0]["status"], "degraded")
        self.assertEqual(out["worst_status"], "degraded")
        self.assertTrue(any("stale" in r for r in out["hosts"][0]["reasons"]))

    def test_record_past_outage_is_outage(self):
        recs = [_rec("mac", "claude-code-mac-repo", 20000, self.now)]  # >10800
        out = views.assess_infra_health(recs, now=self.now,
                                        degraded_after_s=3600, outage_after_s=10800)
        self.assertEqual(out["hosts"][0]["status"], "outage")
        self.assertEqual(out["worst_status"], "outage")

    def test_no_health_records_is_not_a_degraded_status(self):
        out = views.assess_infra_health([], now=self.now,
                                        degraded_after_s=3600, outage_after_s=10800)
        self.assertEqual(out["hosts"], [])
        self.assertEqual(out["worst_status"], "healthy")  # nothing reporting != degraded

    def test_undatable_reconcile_at_is_not_reporting(self):
        bad = _rec("mac", "claude-code-mac-repo", 60, self.now)
        bad["reconcile_at"] = "not-a-timestamp"
        out = views.assess_infra_health([bad], now=self.now,
                                        degraded_after_s=3600, outage_after_s=10800)
        self.assertEqual(out["hosts"][0]["status"], "not_reporting")
        # not_reporting is informational — never escalates worst_status
        self.assertEqual(out["worst_status"], "healthy")

    def test_metrics_surfaced_but_not_gated(self):
        rec = _rec("mac", "claude-code-mac-repo", 60, self.now)
        rec["duration_s"] = 88.0  # absurd duration must NOT change status
        rec["repair_backlog"] = 50
        out = views.assess_infra_health([rec], now=self.now,
                                        degraded_after_s=3600, outage_after_s=10800)
        self.assertEqual(out["hosts"][0]["status"], "healthy")
        self.assertEqual(out["hosts"][0]["metrics"]["duration_s"], 88.0)
        self.assertEqual(out["hosts"][0]["metrics"]["repair_backlog"], 50)

    def test_worst_status_is_the_worst_of_many(self):
        recs = [
            _rec("a", "a", 60, self.now),      # healthy
            _rec("b", "b", 4000, self.now),    # degraded
            _rec("c", "c", 20000, self.now),   # outage
        ]
        out = views.assess_infra_health(recs, now=self.now,
                                        degraded_after_s=3600, outage_after_s=10800)
        self.assertEqual(out["worst_status"], "outage")

    def test_bus_missed_digest_only_on_true_miss(self):
        recs = [_rec("a", "a", 60, self.now)]
        # last emit ~9h ago (same UTC day as noon `now`) -> a normal overnight
        # gap, NOT a miss. A date-only marker parses to that date's midnight,
        # which is 12h before noon -> < the 20h slack window.
        recent = (self.now - timedelta(hours=9)).strftime("%Y-%m-%d")
        out = views.assess_infra_health(
            recs, now=self.now, degraded_after_s=3600, outage_after_s=10800,
            digest_last_emit=recent)
        self.assertFalse(out["bus"]["missed_digest_window"])
        # last emit 30h ago -> the prior day's midnight is 36h before noon ->
        # beyond the ~20h max inter-window gap -> a true miss
        old = (self.now - timedelta(hours=30)).strftime("%Y-%m-%d")
        out2 = views.assess_infra_health(
            recs, now=self.now, degraded_after_s=3600, outage_after_s=10800,
            digest_last_emit=old)
        self.assertTrue(out2["bus"]["missed_digest_window"])

    def test_bus_no_digest_marker_is_missed(self):
        recs = [_rec("a", "a", 60, self.now)]
        out = views.assess_infra_health(
            recs, now=self.now, degraded_after_s=3600, outage_after_s=10800,
            digest_last_emit=None)
        self.assertTrue(out["bus"]["missed_digest_window"])


class TestHealthPaths(unittest.TestCase):
    def test_health_remote_path(self):
        p = remote.health_remote_path("claude-code-mac-repo")
        self.assertTrue(p.endswith("/health/claude-code-mac-repo.json"))
        self.assertIn(remote.remote_root(), p)

    def test_health_prefix(self):
        self.assertTrue(remote.health_prefix().endswith("/health/"))


if __name__ == "__main__":
    unittest.main()
