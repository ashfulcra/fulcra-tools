"""Tests for the coordination-system health surface (spec v2).

Covers the pure judgment (views.assess_infra_health + env-knob readers) and the
remote path helpers. All datetime gates go through views._parse_dt; no live bus
I/O — the hermetic conftest defaults FULCRA_COORD_BACKEND=false.
"""

from __future__ import annotations

import io
import json
import os
import sys
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fulcra_coord import views, heartbeat, remote, cli


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

    def test_freshest_record_per_host_supersedes_dead_worktree_orphan(self):
        # A machine accrues several health records under the SAME host: live
        # worktrees plus orphans from deleted ones (health was keyed per-cwd).
        # The freshest reconcile_at per host decides that host's status, so a
        # live machine's current reconcile supersedes its own dead-worktree
        # orphans — instead of one stale orphan pinning the whole fleet to
        # "outage" until the 30-day prune (the false-alarm bug).
        recs = [
            _rec("mac", "claude-code-mac-deleted-worktree", 20000, self.now),  # orphan, outage-stale
            _rec("mac", "claude-code-mac-fulcra-tools", 60, self.now),         # live machine, fresh
        ]
        out = views.assess_infra_health(recs, now=self.now,
                                        degraded_after_s=3600, outage_after_s=10800)
        self.assertEqual(out["worst_status"], "healthy")
        mac = [h for h in out["hosts"] if h["host"] == "mac"]
        self.assertEqual(len(mac), 1)            # one row per host, not per record
        self.assertEqual(mac[0]["status"], "healthy")

    def test_host_with_only_stale_records_still_reads_outage(self):
        # The real signal is preserved: a machine whose EVERY record is stale
        # (genuinely not reconciling) still reads outage — the freshest is stale.
        recs = [
            _rec("srv", "a", 20000, self.now),
            _rec("srv", "b", 30000, self.now),
        ]
        out = views.assess_infra_health(recs, now=self.now,
                                        degraded_after_s=3600, outage_after_s=10800)
        self.assertEqual(out["worst_status"], "outage")

    def test_datable_record_beats_undatable_for_same_host(self):
        # A host with a fresh datable record AND an undatable one is healthy —
        # the datable (freshest) wins; the undatable doesn't drag it to
        # not_reporting.
        good = _rec("mac", "a", 60, self.now)
        bad = _rec("mac", "b", 60, self.now)
        bad["reconcile_at"] = "garbage"
        out = views.assess_infra_health([bad, good], now=self.now,
                                        degraded_after_s=3600, outage_after_s=10800)
        mac = [h for h in out["hosts"] if h["host"] == "mac"]
        self.assertEqual(len(mac), 1)
        self.assertEqual(mac[0]["status"], "healthy")

    def test_bus_missed_digest_only_on_true_miss(self):
        recs = [_rec("a", "a", 60, self.now)]
        # Today's marker (date == now's date) -> midnight is 12h before noon ->
        # well within the slack window. Not a miss.
        recent = self.now.strftime("%Y-%m-%d")
        out = views.assess_infra_health(
            recs, now=self.now, degraded_after_s=3600, outage_after_s=10800,
            digest_last_emit=recent)
        self.assertFalse(out["bus"]["missed_digest_window"])
        # A TRUE miss: a whole day's BOTH windows skipped. The freshest marker is
        # then 2 days back; its midnight is ~60h before noon -> beyond the 44h
        # threshold. (One full day skipped, observed the next day.)
        old = (self.now - timedelta(days=2, hours=12)).strftime("%Y-%m-%d")
        out2 = views.assess_infra_health(
            recs, now=self.now, degraded_after_s=3600, outage_after_s=10800,
            digest_last_emit=old)
        self.assertTrue(out2["bus"]["missed_digest_window"])

    def test_bus_morning_run_with_yesterday_marker_is_not_a_miss(self):
        """Regression: the 08:00 morning digest, on a HEALTHY fleet, must NOT
        report a missed window. `_assess_fleet` runs before the morning marker is
        claimed, so the freshest marker is YESTERDAY's date; against a date-only
        (midnight-normalized) marker that is 32h stale at 08:00. The old 20h
        threshold flagged this every single morning (a daily false alarm). The
        44h threshold clears the 32h healthy worst-case while still catching a
        56h full-day-skipped miss."""
        morning = datetime(2026, 6, 5, 8, 0, 0, tzinfo=timezone.utc)
        recs = [_rec("a", "a", 60, morning)]
        yesterday = (morning - timedelta(days=1)).strftime("%Y-%m-%d")  # 32h old
        out = views.assess_infra_health(
            recs, now=morning, degraded_after_s=3600, outage_after_s=10800,
            digest_last_emit=yesterday)
        self.assertFalse(out["bus"]["missed_digest_window"])

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


class TestCmdHealth(unittest.TestCase):
    def _records(self):
        now = datetime.now(timezone.utc)
        fresh = {"schema": "fulcra.coordination.health.v1", "host": "mac",
                 "agent": "claude-code:mac:repo", "version": "0.9.0",
                 "reconcile_at": now.isoformat().replace("+00:00", "Z"),
                 "duration_s": 1.0, "tasks_loaded": 3, "views_refreshed": 5,
                 "repair_backlog": 0, "retention_last_run": None,
                 "listener_last_fire": None, "bus_task_count": 3}
        return fresh

    def test_health_json_format(self):
        rec = self._records()
        with mock.patch("fulcra_coord.cli.remote.list_files",
                        return_value=["/coordination/health/claude-code-mac-repo.json"]), \
             mock.patch("fulcra_coord.cli.remote.download_json", return_value=rec), \
             mock.patch("fulcra_coord.digest._load_task_summaries",
                        return_value=[{"id": "TASK-1"}, {"id": "TASK-2"}]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.cmd_health(types.SimpleNamespace(format="json"), backend=["false"])
        self.assertEqual(rc, 0)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["worst_status"], "healthy")
        self.assertEqual(len(out["hosts"]), 1)
        self.assertEqual(out["bus"]["task_count"], 2)

    def test_health_table_format_runs(self):
        rec = self._records()
        with mock.patch("fulcra_coord.cli.remote.list_files",
                        return_value=["/coordination/health/claude-code-mac-repo.json"]), \
             mock.patch("fulcra_coord.cli.remote.download_json", return_value=rec):
            rc = cli.cmd_health(types.SimpleNamespace(format="table"), backend=["false"])
        self.assertEqual(rc, 0)

    def test_health_tolerates_missing_and_garbage(self):
        # One path lists but download returns None (garbage/missing) -> no crash.
        with mock.patch("fulcra_coord.cli.remote.list_files",
                        return_value=["/coordination/health/x.json", "/coordination/health/dir-not-json"]), \
             mock.patch("fulcra_coord.cli.remote.download_json", return_value=None):
            rc = cli.cmd_health(types.SimpleNamespace(format="json"), backend=["false"])
        self.assertEqual(rc, 0)

    def test_health_empty_bus_is_healthy(self):
        with mock.patch("fulcra_coord.cli.remote.list_files", return_value=[]), \
             mock.patch("fulcra_coord.cli.remote.download_json", return_value=None):
            rc = cli.cmd_health(types.SimpleNamespace(format="json"), backend=["false"])
        self.assertEqual(rc, 0)


class TestHealthWiring(unittest.TestCase):
    def test_health_in_command_map(self):
        from fulcra_coord.entry import COMMAND_MAP
        self.assertIs(COMMAND_MAP["health"], cli.cmd_health)

    def test_health_parses_format(self):
        from fulcra_coord.entry import build_parser
        args = build_parser().parse_args(["health", "--format", "json"])
        self.assertEqual(args.command, "health")
        self.assertEqual(args.format, "json")


class TestDoctorHealthFold(unittest.TestCase):
    def test_doctor_includes_fleet_health(self):
        buf = io.StringIO()
        with mock.patch("fulcra_coord.cli.remote.list_files", return_value=[]), \
             mock.patch("fulcra_coord.cli.remote.download_json", return_value=None), \
             mock.patch("fulcra_coord.doctor._assess_fleet",
                        return_value={"hosts": [{"host": "mac", "status": "healthy",
                                                 "reasons": [], "metrics": {}}],
                                      "bus": {"missed_digest_window": False,
                                              "digest_last_emit": None,
                                              "retention_last_run": None,
                                              "task_count": 1},
                                      "worst_status": "healthy"}), \
             redirect_stdout(buf):
            cli.cmd_doctor(types.SimpleNamespace(), backend=["false"])
        self.assertIn("Fleet health", buf.getvalue())

    def test_doctor_fleet_health_never_crashes_doctor(self):
        buf = io.StringIO()
        with mock.patch("fulcra_coord.doctor._assess_fleet",
                        side_effect=RuntimeError("boom")), \
             mock.patch("fulcra_coord.cli.remote.check_cli_available",
                        return_value=(True, "ok")), \
             mock.patch("fulcra_coord.cli.remote.check_file_commands",
                        return_value=(True, "ok")), \
             mock.patch("fulcra_coord.cli.remote.check_remote_access",
                        return_value=(True, "ok")), \
             redirect_stdout(buf):
            rc = cli.cmd_doctor(types.SimpleNamespace(), backend=["false"])
        # doctor must still return its own verdict; a fleet-health error degrades
        # to a noted line, never a crash.
        self.assertIn(rc, (0, 1))


class TestIsPrunableHealth(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)

    def test_aged_record_is_prunable(self):
        rec = {"reconcile_at": (self.now - timedelta(days=40)).isoformat().replace("+00:00", "Z")}
        self.assertTrue(views.is_prunable_health(rec, self.now))  # > 30d default

    def test_fresh_record_is_kept(self):
        rec = {"reconcile_at": (self.now - timedelta(days=1)).isoformat().replace("+00:00", "Z")}
        self.assertFalse(views.is_prunable_health(rec, self.now))

    def test_undatable_record_is_kept(self):
        self.assertFalse(views.is_prunable_health({"reconcile_at": "nope"}, self.now))
        self.assertFalse(views.is_prunable_health({}, self.now))


class TestPruneDeadHealth(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)

    def _rec(self, days_ago):
        return {"reconcile_at": (self.now - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")}

    def test_prunes_aged_keeps_fresh_keeps_undatable(self):
        paths = ["/coordination/health/old.json",
                 "/coordination/health/fresh.json",
                 "/coordination/health/bad.json"]
        bodies = {"/coordination/health/old.json": self._rec(40),
                  "/coordination/health/fresh.json": self._rec(1),
                  "/coordination/health/bad.json": {"reconcile_at": "nope"}}
        deleted = []
        with mock.patch("fulcra_coord.cli.remote.list_files", return_value=paths), \
             mock.patch("fulcra_coord.cli.remote.download_json",
                        side_effect=lambda p, **k: bodies.get(p)), \
             mock.patch("fulcra_coord.cli.remote.delete",
                        side_effect=lambda p, **k: deleted.append(p) or True):
            n = cli._prune_dead_health(self.now, backend=["false"])
        self.assertEqual(n, 1)
        self.assertEqual(deleted, ["/coordination/health/old.json"])

    def test_failsafe_on_list_error(self):
        with mock.patch("fulcra_coord.cli.remote.list_files",
                        side_effect=RuntimeError("boom")):
            n = cli._prune_dead_health(self.now, backend=["false"])
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
