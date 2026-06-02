"""Tests for scripts/demo_seed.py — the three-agent coordination demo seeder.

These run the seed against a STATEFUL FAKE BACKEND (a local fulcra-api-file
emulator wired in via FULCRA_COORD_BACKEND), so the real upload + view-rebuild
path executes end-to-end without touching a live Fulcra account.

Asserted:
  * exactly 6 tasks are seeded;
  * TASK-DEMO-backfill is flagged stale in views/needs-attention.json;
  * the `agents` digest groups the 4 distinct owner_agents and marks the
    backfill task stale (the ⚠ in the rendered output).
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

# Make repo root (package) and scripts/ importable.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

FAKE_BACKEND = REPO_ROOT / "tests" / "fake_fulcra_backend.py"
DEMO_ROOT = "/coordination-demo"


class DemoSeedTest(unittest.TestCase):
    def setUp(self):
        # Isolated remote state + cache so the test never hits a live account
        # and never pollutes the developer's real cache.
        self.fake_state = tempfile.mkdtemp()
        self.cache_dir = tempfile.mkdtemp()

        self._saved_env = {
            k: os.environ.get(k)
            for k in (
                "FULCRA_COORD_BACKEND",
                "FULCRA_FAKE_ROOT",
                "FULCRA_COORD_REMOTE_ROOT",
                "XDG_CACHE_HOME",
                "FULCRA_COORD_STALE_HOURS",
            )
        }
        os.environ["FULCRA_COORD_BACKEND"] = f"{sys.executable} {FAKE_BACKEND}"
        os.environ["FULCRA_FAKE_ROOT"] = self.fake_state
        os.environ["FULCRA_COORD_REMOTE_ROOT"] = DEMO_ROOT
        os.environ["XDG_CACHE_HOME"] = self.cache_dir
        os.environ.pop("FULCRA_COORD_STALE_HOURS", None)  # use the 2h default

        # Import (or reimport) the seed module under the configured env.
        import demo_seed
        self.demo_seed = importlib.reload(demo_seed)

        # Base time = real "now". The package's view layer computes staleness
        # against the real wall clock (views.is_stale -> datetime.now), so the
        # seeded offsets must be measured from the same real now for the ~4h-old
        # backfill task to actually read as stale during the test. (The live seed
        # uses real now by default for exactly this reason; pinning a fixed past
        # or future base here would desync the offsets from the staleness check.)
        self.now = datetime.now(timezone.utc)

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _seed(self):
        tasks = self.demo_seed.build_scenario_tasks(self.now)
        tasks_ok, views_ok, failures = self.demo_seed.upload_tasks_and_views(tasks)
        self.assertEqual(failures, [], f"seed uploads failed: {failures}")
        return tasks, tasks_ok, views_ok

    # ------------------------------------------------------------------
    # Scenario shape
    # ------------------------------------------------------------------

    def test_seeds_six_tasks(self):
        tasks, tasks_ok, _ = self._seed()
        self.assertEqual(len(tasks), 6)
        self.assertEqual(tasks_ok, 6)

        statuses = sorted(t["status"] for t in tasks)
        # active x3 (search-api, infra-cluster, backfill), waiting, blocked, done
        self.assertEqual(
            statuses,
            sorted(["active", "active", "active", "waiting", "blocked", "done"]),
        )

        # Four distinct owner_agents across the scenario.
        owners = {t["owner_agent"] for t in tasks}
        self.assertEqual(
            owners,
            {
                "claude-code:DeskbookPro:search",
                "openclaw:macmini:infra",
                "codex:DeskbookPro:search",
                "claude-code:DeskbookPro:backfill",
            },
        )

    def test_main_entrypoint_seeds_and_succeeds(self):
        """The actual CLI entrypoint writes all tasks + views and exits 0."""
        # No --now: use real now so offsets line up with the live default path.
        rc = self.demo_seed.main(["--reset"])
        self.assertEqual(rc, 0)

        from fulcra_coord import remote
        idx = remote.download_json(remote.view_remote_path("index"))
        self.assertIsNotNone(idx, "index.json must be written by the seed")
        self.assertEqual(idx["counts"]["by_status"].get("active"), 3)
        self.assertEqual(idx["counts"]["by_status"].get("done"), 1)

    # ------------------------------------------------------------------
    # Stale flagging (the forgotten backfill task)
    # ------------------------------------------------------------------

    def test_backfill_flagged_stale_in_needs_attention(self):
        self._seed()
        from fulcra_coord import remote
        na = remote.download_json(remote.view_remote_path("needs-attention"))
        self.assertIsNotNone(na, "needs-attention.json must be written")
        stale_ids = [t["id"] for t in na["tasks"]]
        self.assertIn(
            "TASK-DEMO-backfill", stale_ids,
            "the ~4h-old active backfill task must be flagged stale",
        )
        # Only the backfill task should be stale: the other actives are recent.
        self.assertEqual(stale_ids, ["TASK-DEMO-backfill"])
        for t in na["tasks"]:
            self.assertTrue(t.get("stale") is True)

    def test_summary_reports_backfill_stale(self):
        tasks, _, _ = self._seed()
        summary = self.demo_seed.summarize(tasks, self.now)
        self.assertEqual(summary["total"], 6)
        self.assertEqual(summary["stale_ids"], ["TASK-DEMO-backfill"])
        self.assertEqual(summary["by_status"]["active"], 3)

    # ------------------------------------------------------------------
    # The agents digest: 4-agent grouping + stale mark
    # ------------------------------------------------------------------

    def test_agents_digest_groups_four_owners_and_marks_stale(self):
        self._seed()
        from fulcra_coord.cli import cmd_agents

        backend = os.environ["FULCRA_COORD_BACKEND"].split()

        class Args:
            format = "json"
            mine = None

        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_agents(Args(), backend=backend)
        self.assertEqual(rc, 0)

        out = json.loads(buf.getvalue())
        agents = {blk["agent"] for blk in out["agents"]}
        # Four owner_agents have open (active/waiting/blocked) work. The done
        # staging-cluster task's owner (openclaw) is already represented by its
        # active infra-cluster task, so exactly 4 groups appear.
        self.assertEqual(
            agents,
            {
                "claude-code:DeskbookPro:search",
                "openclaw:macmini:infra",
                "codex:DeskbookPro:search",
                "claude-code:DeskbookPro:backfill",
            },
        )

        # The backfill task must be the one carrying the stale flag (the ⚠).
        stale_marked = [
            t["id"]
            for blk in out["agents"]
            for t in blk["tasks"]
            if t["stale"]
        ]
        self.assertEqual(stale_marked, ["TASK-DEMO-backfill"])

    def test_agents_digest_renders_warning_glyph(self):
        """The human-readable table marks the stale task with ⚠."""
        self._seed()
        from fulcra_coord.cli import cmd_agents

        backend = os.environ["FULCRA_COORD_BACKEND"].split()

        class Args:
            format = "table"
            mine = None

        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            cmd_agents(Args(), backend=backend)
        rendered = buf.getvalue()
        self.assertIn("⚠", rendered)
        # The glyph must sit on the backfill line, not elsewhere.
        backfill_line = next(
            ln for ln in rendered.splitlines() if "TASK-DEMO-backfill" in ln
        )
        self.assertIn("⚠", backfill_line)


class DemoSetupScriptTest(unittest.TestCase):
    """scripts/demo-setup.sh — the per-host one-command demo readiness wrapper
    (#19). We don't execute its install steps here (that would touch the real
    launchd/crontab and a live account); we assert the script is well-formed:
    valid bash, executable, and carries all three agent-type branches so a typo
    in any branch is caught by CI rather than at demo time."""

    SCRIPT = REPO_ROOT / "scripts" / "demo-setup.sh"

    def test_script_exists_and_is_executable(self):
        self.assertTrue(self.SCRIPT.is_file(), "demo-setup.sh missing")
        self.assertTrue(os.access(self.SCRIPT, os.X_OK),
                        "demo-setup.sh is not chmod +x")

    def test_script_is_valid_bash(self):
        import subprocess
        res = subprocess.run(["bash", "-n", str(self.SCRIPT)],
                             capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_has_three_agent_type_branches(self):
        body = self.SCRIPT.read_text()
        # Each agent type must map to its installer subcommand.
        self.assertIn("install-claude-code", body)
        self.assertIn("install-codex", body)
        self.assertIn("install-openclaw", body)
        # And the listener step is always wired in.
        self.assertIn("install-listener", body)


if __name__ == "__main__":
    unittest.main()
