"""Tests for fulcra-coord — no live Fulcra writes required.

All remote I/O is mocked via a fake backend (command that always exits 1).
Tests use a temporary XDG_CACHE_HOME to avoid polluting the real cache.

Run:
  pytest tests/test_fulcra_coord.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

# Ensure the package root is importable when running from project dir
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fulcra_coord import schema, cache, views
from fulcra_coord.schema import (
    make_task,
    make_task_id,
    apply_transition,
    apply_update,
    validate_task,
    build_tags,
    CoordError,
    TransitionError,
    SchemaError,
)
from fulcra_coord.views import (
    build_index,
    build_active,
    build_next,
    build_recently_done,
    build_search_index,
    build_workstream_view,
    build_agent_view,
    search_tasks,
    build_all_views,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sample_task(**overrides) -> dict:
    t = make_task(
        title="Fix the widget pipeline",
        workstream="devops",
        agent="claude-code",
        kind="ops",
        priority="P2",
    )
    t.update(overrides)
    return t


def _with_status(task: dict, status: str) -> dict:
    """Force task status (bypass transition, for test setup only)."""
    import copy
    t = copy.deepcopy(task)
    t["status"] = status
    return t


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------

class TestTaskIdGeneration(unittest.TestCase):
    def test_format(self):
        from datetime import datetime, timezone
        dt = datetime(2026, 5, 31, 14, 0, 0, tzinfo=timezone.utc)
        tid = make_task_id("Fix the widget", dt)
        self.assertTrue(tid.startswith("TASK-20260531-"))
        self.assertRegex(tid, r"^TASK-\d{8}-[a-z0-9-]+-[0-9a-f]{8}$")

    def test_unique(self):
        t1 = make_task_id("same title")
        t2 = make_task_id("same title")
        self.assertNotEqual(t1, t2)

    def test_slug_special_chars(self):
        tid = make_task_id("Fix: the $$ widget!!!")
        slug_part = "-".join(tid.split("-")[2:-1])
        self.assertRegex(slug_part, r"^[a-z0-9-]+$")


class TestMakeTask(unittest.TestCase):
    def test_required_fields(self):
        t = _sample_task()
        for field in ["schema", "id", "title", "status", "workstream", "owner_agent",
                      "created_at", "updated_at", "events", "tags"]:
            self.assertIn(field, t, f"Missing field: {field}")

    def test_initial_status(self):
        t = _sample_task()
        self.assertEqual(t["status"], "proposed")

    def test_initial_event(self):
        t = _sample_task()
        self.assertEqual(len(t["events"]), 1)
        self.assertEqual(t["events"][0]["type"], "created")

    def test_tags_contain_workstream(self):
        t = _sample_task()
        self.assertIn("workstream:devops", t["tags"])

    def test_validate_passes(self):
        t = _sample_task()
        errs = validate_task(t)
        self.assertEqual(errs, [], f"Unexpected errors: {errs}")

    def test_no_hardcoded_openclaw_paths(self):
        t = _sample_task()
        task_json = json.dumps(t)
        self.assertNotIn("/arc/coordination", task_json)
        self.assertNotIn("openclaw", task_json.lower())

    def test_default_surface_is_generic(self):
        t = _sample_task()
        self.assertEqual(t["surface"], "local:agent")


class TestStatusTransitions(unittest.TestCase):
    def test_proposed_to_active(self):
        t = _sample_task()
        t2 = apply_transition(t, "active", by="agent-a")
        self.assertEqual(t2["status"], "active")

    def test_active_to_done_requires_evidence(self):
        t = _with_status(_sample_task(), "active")
        with self.assertRaises(SchemaError):
            apply_transition(t, "done", by="agent-a")

    def test_active_to_done_with_evidence(self):
        t = _with_status(_sample_task(), "active")
        t2 = apply_transition(
            t, "done", by="agent-a",
            evidence="PR merged and deployed",
            verification_level="agent-verified",
        )
        self.assertEqual(t2["status"], "done")
        self.assertEqual(t2["done"]["evidence"], "PR merged and deployed")
        self.assertEqual(t2["done"]["verification_level"], "agent-verified")

    def test_invalid_transition_raises(self):
        t = _sample_task()  # status = proposed
        with self.assertRaises(TransitionError):
            apply_transition(t, "done", by="agent-a",
                             evidence="x", verification_level="agent-verified")

    def test_terminal_status_blocks_transition(self):
        t = _with_status(_sample_task(), "done")
        with self.assertRaises(TransitionError):
            apply_transition(t, "active", by="agent-a")

    def test_blocked_sets_blocked_on(self):
        t = _with_status(_sample_task(), "active")
        t2 = apply_transition(t, "blocked", by="agent-a", blocked_on="Waiting on deploy key")
        self.assertEqual(t2["status"], "blocked")
        self.assertEqual(t2["blocked_on"], "Waiting on deploy key")

    def test_waiting_clears_blocked_on(self):
        t = _with_status(_sample_task(), "active")
        t["blocked_on"] = "something"
        t2 = apply_transition(t, "waiting", by="agent-a", next_action="Resume tomorrow")
        self.assertEqual(t2["status"], "waiting")
        self.assertIsNone(t2["blocked_on"])

    def test_events_appended(self):
        t = _sample_task()
        t2 = apply_transition(t, "active", by="agent-a")
        self.assertEqual(len(t2["events"]), 2)

    def test_events_bounded(self):
        t = _with_status(_sample_task(), "active")
        t["events"] = [{"at": f"2026-01-{i:02d}T00:00:00Z", "type": "updated",
                        "by": "agent-a", "summary": f"update {i}", "evidence": None}
                       for i in range(1, 26)]
        t2 = apply_transition(t, "waiting", by="agent-a", next_action="later")
        self.assertLessEqual(len(t2["events"]), schema.MAX_EVENTS_INLINE)

    def test_unknown_status_raises(self):
        t = _sample_task()
        with self.assertRaises(TransitionError):
            apply_transition(t, "foobar", by="agent-a")

    def test_invalid_verification_level_raises(self):
        t = _with_status(_sample_task(), "active")
        with self.assertRaises(SchemaError):
            apply_transition(t, "done", by="agent-a",
                             evidence="x", verification_level="maybe")


class TestApplyUpdate(unittest.TestCase):
    def test_update_summary(self):
        t = _sample_task()
        t2 = apply_update(t, by="agent-a", summary="New summary here")
        self.assertEqual(t2["current_summary"], "New summary here")
        self.assertEqual(t2["status"], "proposed")  # status unchanged

    def test_update_next_action(self):
        t = _sample_task()
        t2 = apply_update(t, by="agent-a", next_action="Deploy to staging")
        self.assertEqual(t2["next_action"], "Deploy to staging")

    def test_update_does_not_change_status(self):
        t = _with_status(_sample_task(), "active")
        t2 = apply_update(t, by="agent-a", summary="Updated")
        self.assertEqual(t2["status"], "active")

    def test_update_appends_event(self):
        t = _sample_task()
        t2 = apply_update(t, by="agent-a", summary="test")
        self.assertEqual(t2["events"][-1]["type"], "updated")


class TestValidateTask(unittest.TestCase):
    def test_valid_task(self):
        t = _sample_task()
        self.assertEqual(validate_task(t), [])

    def test_missing_status(self):
        t = _sample_task()
        del t["status"]
        errs = validate_task(t)
        self.assertTrue(any("status" in e for e in errs))

    def test_invalid_status(self):
        t = _sample_task()
        t["status"] = "limbo"
        errs = validate_task(t)
        self.assertTrue(any("Invalid status" in e for e in errs))

    def test_bad_task_id(self):
        t = _sample_task()
        t["id"] = "not-a-valid-id"
        errs = validate_task(t)
        self.assertTrue(any("id" in e.lower() for e in errs))


# ---------------------------------------------------------------------------
# View tests
# ---------------------------------------------------------------------------

def _make_tasks_set() -> list[dict]:
    """Return a diverse set of tasks for view tests."""
    base = _sample_task()
    active = apply_transition(base, "active", by="agent-a")

    blocked_base = _sample_task()
    blocked_base["workstream"] = "fulcra"
    blocked_base["owner_agent"] = "agent-b"
    blocked = _with_status(blocked_base, "blocked")
    blocked["blocked_on"] = "Waiting on API key"

    waiting_base = _sample_task()
    waiting_base["title"] = "Deploy to prod"
    waiting_base["workstream"] = "devops"
    waiting = _with_status(waiting_base, "waiting")

    proposed_base = _sample_task()
    proposed_base["title"] = "New proposed task"
    proposed = proposed_base

    done_base = _sample_task()
    done_base["title"] = "Already shipped"
    done_base["workstream"] = "general"
    done_base["owner_agent"] = "agent-c"
    done = _with_status(done_base, "done")
    done["done"] = {
        "done_at": "2026-05-30T10:00:00Z",
        "done_by": "agent-c",
        "evidence": "Shipped",
        "verification_level": "agent-verified",
        "confidence": None,
    }
    done["updated_at"] = "2026-05-30T10:00:00Z"

    return [active, blocked, waiting, proposed, done]


class TestBuildIndex(unittest.TestCase):
    def test_structure(self):
        tasks = _make_tasks_set()
        idx = build_index(tasks)
        self.assertIn("schema", idx)
        self.assertIn("counts", idx)
        self.assertIn("active", idx)
        self.assertIn("recent_done", idx)
        self.assertEqual(idx["schema"], "fulcra.coordination.index.v1")

    def test_counts_by_status(self):
        tasks = _make_tasks_set()
        idx = build_index(tasks)
        by_status = idx["counts"]["by_status"]
        self.assertGreater(by_status.get("active", 0), 0)

    def test_active_list_excludes_done(self):
        tasks = _make_tasks_set()
        idx = build_index(tasks)
        for item in idx["active"]:
            self.assertNotIn(item["status"], ("done", "abandoned"))


class TestBuildActiveView(unittest.TestCase):
    def test_only_active_statuses(self):
        tasks = _make_tasks_set()
        view = build_active(tasks)
        for t in view["tasks"]:
            self.assertIn(t["status"], ("active", "waiting", "blocked"))

    def test_schema(self):
        tasks = _make_tasks_set()
        view = build_active(tasks)
        self.assertEqual(view["schema"], "fulcra.coordination.view.v1")
        self.assertEqual(view["view"], "active")


class TestBuildNextView(unittest.TestCase):
    def test_proposed_and_waiting_included(self):
        tasks = _make_tasks_set()
        view = build_next(tasks)
        for t in view["tasks"]:
            self.assertIn(t["status"], ("proposed", "waiting"))

    def test_active_excluded(self):
        tasks = _make_tasks_set()
        view = build_next(tasks)
        for t in view["tasks"]:
            self.assertNotEqual(t["status"], "active")


class TestBuildRecentlyDone(unittest.TestCase):
    def test_done_within_window(self):
        tasks = _make_tasks_set()
        view = build_recently_done(tasks, days=7)
        for t in view["tasks"]:
            self.assertIn(t["status"], ("done", "abandoned"))

    def test_old_done_excluded(self):
        tasks = _make_tasks_set()
        old_done = _sample_task()
        old_done["status"] = "done"
        old_done["updated_at"] = "2025-01-01T00:00:00Z"
        old_done["done"] = {
            "done_at": "2025-01-01T00:00:00Z",
            "done_by": "agent-a", "evidence": "old", "verification_level": None, "confidence": None,
        }
        view = build_recently_done(tasks + [old_done], days=7)
        for t in view["tasks"]:
            self.assertNotEqual(t["id"], old_done["id"])


class TestBuildSearchIndex(unittest.TestCase):
    def test_all_current_tasks_included(self):
        tasks = _make_tasks_set()
        idx = build_search_index(tasks)
        self.assertIn("records", idx)
        current_ids = {t["id"] for t in tasks if t["status"] not in ("done", "abandoned")}
        record_ids = {r["id"] for r in idx["records"]}
        self.assertTrue(current_ids.issubset(record_ids))

    def test_each_record_has_required_fields(self):
        tasks = _make_tasks_set()
        idx = build_search_index(tasks)
        for r in idx["records"]:
            for field in ("id", "title", "status", "priority", "workstream", "tags", "task_file"):
                self.assertIn(field, r, f"Missing {field} in search record")

    def test_priority_field_preserved_in_search_index(self):
        """Regression: build_search_index must carry priority so table display works."""
        tasks = _make_tasks_set()
        idx = build_search_index(tasks)
        for r in idx["records"]:
            self.assertNotEqual(r.get("priority", ""), "",
                                "priority must not be empty in search-index record")

    def test_task_file_uses_remote_root(self):
        tasks = _make_tasks_set()
        idx = build_search_index(tasks)
        for r in idx["records"]:
            # Should not contain hardcoded openclaw-specific paths
            self.assertNotIn("/arc/coordination", r["task_file"])


class TestBuildWorkstreamView(unittest.TestCase):
    def test_filters_by_workstream(self):
        tasks = _make_tasks_set()
        view = build_workstream_view("devops", tasks)
        for t in view["active"]:
            self.assertEqual(t["workstream"], "devops")

    def test_schema(self):
        tasks = _make_tasks_set()
        view = build_workstream_view("devops", tasks)
        self.assertEqual(view["schema"], "fulcra.coordination.workstream_view.v1")
        self.assertEqual(view["workstream"], "devops")


class TestBuildAgentView(unittest.TestCase):
    def test_no_error(self):
        tasks = _make_tasks_set()
        view = build_agent_view("agent-b", tasks)
        self.assertIn("active", view)

    def test_schema(self):
        tasks = _make_tasks_set()
        view = build_agent_view("claude-code", tasks)
        self.assertEqual(view["schema"], "fulcra.coordination.agent_view.v1")


class TestSearchTasks(unittest.TestCase):
    def test_finds_by_title(self):
        tasks = _make_tasks_set()
        results = search_tasks("widget", tasks)
        self.assertTrue(any("widget" in r["title"].lower() for r in results))

    def test_finds_by_workstream(self):
        tasks = _make_tasks_set()
        results = search_tasks("fulcra", tasks)
        self.assertTrue(any(r["workstream"] == "fulcra" for r in results))

    def test_finds_by_tag(self):
        tasks = _make_tasks_set()
        results = search_tasks("kind:ops", tasks)
        self.assertGreater(len(results), 0)

    def test_no_match_returns_empty(self):
        tasks = _make_tasks_set()
        results = search_tasks("xyzzy-not-a-real-thing-9999", tasks)
        self.assertEqual(results, [])


class TestBuildAllViews(unittest.TestCase):
    def test_all_standard_views_present(self):
        tasks = _make_tasks_set()
        all_v = build_all_views(tasks)
        for name in ("index", "active", "next", "recently-done", "search-index"):
            self.assertIn(name, all_v, f"Missing view: {name}")

    def test_workstream_views_generated(self):
        tasks = _make_tasks_set()
        all_v = build_all_views(tasks)
        self.assertIn("workstreams/devops", all_v)

    def test_agent_views_generated(self):
        tasks = _make_tasks_set()
        all_v = build_all_views(tasks)
        self.assertIn("agents/claude-code", all_v)

    def test_agent_views_generated_for_recent_touchers(self):
        tasks = _make_tasks_set()
        handed_off = _with_status(_sample_task(), "done")
        handed_off["owner_agent"] = "agent-a"
        handed_off["last_touched_by"] = "agent-b"
        handed_off["done"] = {
            "done_at": "2026-05-30T10:00:00Z",
            "done_by": "agent-b",
            "evidence": "Smoke test passed",
            "verification_level": "agent-verified",
            "confidence": None,
        }
        handed_off["updated_at"] = "2026-05-30T10:00:00Z"

        all_v = build_all_views([*tasks, handed_off])

        self.assertIn("agents/agent-b", all_v)
        self.assertTrue(
            any(t["id"] == handed_off["id"] for t in all_v["agents/agent-b"]["recent_done"])
        )


# ---------------------------------------------------------------------------
# Cache tests
# ---------------------------------------------------------------------------

class TestCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp

    def tearDown(self):
        del os.environ["XDG_CACHE_HOME"]

    def test_write_and_read_task(self):
        t = _sample_task()
        cache.write_cached_task(t)
        read = cache.read_cached_task(t["id"])
        self.assertIsNotNone(read)
        self.assertEqual(read["id"], t["id"])

    def test_read_missing_returns_none(self):
        result = cache.read_cached_task("TASK-20260101-nonexistent-00000000")
        self.assertIsNone(result)

    def test_list_cached_tasks(self):
        t1 = _sample_task()
        t2 = _sample_task()
        t2["title"] = "Another task"
        cache.write_cached_task(t1)
        cache.write_cached_task(t2)
        listed = cache.list_cached_tasks()
        ids = {t["id"] for t in listed}
        self.assertIn(t1["id"], ids)
        self.assertIn(t2["id"], ids)

    def test_write_and_read_view(self):
        data = {"schema": "test", "tasks": []}
        cache.write_cached_view("active", data)
        read = cache.read_cached_view("active")
        self.assertEqual(read["schema"], "test")

    def test_write_and_read_nested_view(self):
        data = {"schema": "test", "workstream": "devops", "active": []}
        cache.write_cached_view("workstreams/devops", data)
        read = cache.read_cached_view("workstreams/devops")
        self.assertEqual(read["workstream"], "devops")

    def test_write_and_clear_op_marker(self):
        cache.ensure_dirs()
        cache.write_op_marker("abc123", {"op_id": "abc123", "needs_reconcile": True})
        markers = cache.list_op_markers()
        self.assertTrue(any(m["op_id"] == "abc123" for m in markers))
        cache.clear_op_marker("abc123")
        markers2 = cache.list_op_markers()
        self.assertFalse(any(m["op_id"] == "abc123" for m in markers2))

    def test_ops_log_append(self):
        cache.append_ops_log({"command": "test", "status": "ok"})
        log_path = cache.ops_log_path()
        self.assertTrue(log_path.exists())
        lines = log_path.read_text().strip().splitlines()
        self.assertTrue(any(json.loads(l)["command"] == "test" for l in lines))

    def test_meta_read_write(self):
        stat = {"version_id": "v42", "size": 100}
        cache.write_meta("/coordination/tasks/TASK-x.json", stat)
        read = cache.read_meta("/coordination/tasks/TASK-x.json")
        self.assertEqual(read["version_id"], "v42")


# ---------------------------------------------------------------------------
# Remote stat_changed tests
# ---------------------------------------------------------------------------

class TestStatChanged(unittest.TestCase):
    def setUp(self):
        from fulcra_coord import remote
        self.remote = remote

    def test_both_none_is_unchanged(self):
        self.assertFalse(self.remote.stat_changed(None, None))

    def test_one_none_is_changed(self):
        self.assertTrue(self.remote.stat_changed(None, {"version_id": "v1"}))
        self.assertTrue(self.remote.stat_changed({"version_id": "v1"}, None))

    def test_same_version_unchanged(self):
        a = {"version_id": "v1", "size": 100}
        b = {"version_id": "v1", "size": 100}
        self.assertFalse(self.remote.stat_changed(a, b))

    def test_different_version_changed(self):
        a = {"version_id": "v1"}
        b = {"version_id": "v2"}
        self.assertTrue(self.remote.stat_changed(a, b))

    def test_size_fallback(self):
        a = {"size": 100}
        b = {"size": 200}
        self.assertTrue(self.remote.stat_changed(a, b))

    def test_same_size_different_uploaded_at_is_changed(self):
        """Regression: same size but different uploaded_at must return True.

        The old code returned on the first matching key ('size') and declared
        the file unchanged when sizes matched, even if uploaded_at differed —
        i.e., a same-size re-upload was silently missed.
        """
        a = {"size": 65, "uploaded_at": "2026-05-31T10:00:00Z"}
        b = {"size": 65, "uploaded_at": "2026-05-31T11:00:00Z"}
        self.assertTrue(self.remote.stat_changed(a, b))

    def test_same_size_different_previous_versions_is_changed(self):
        """Regression: previous_versions incrementing signals a re-upload."""
        a = {"size": 65, "previous_versions": 1}
        b = {"size": 65, "previous_versions": 2}
        self.assertTrue(self.remote.stat_changed(a, b))

    def test_same_size_and_same_timestamp_unchanged(self):
        """Equal weak keys together: should still return False."""
        a = {"size": 65, "uploaded_at": "2026-05-31T10:00:00Z"}
        b = {"size": 65, "uploaded_at": "2026-05-31T10:00:00Z"}
        self.assertFalse(self.remote.stat_changed(a, b))

    def test_strong_key_wins_over_weak_difference(self):
        """If version_id matches, equal version takes precedence over size difference."""
        a = {"version_id": "v1", "size": 100}
        b = {"version_id": "v1", "size": 200}
        self.assertFalse(self.remote.stat_changed(a, b))

    def test_parse_live_text_stat_shape(self):
        text = """/coordination/tasks/TASK-20260531-example-abc12345.json (65 bytes)
Uploaded: 2026-05-31T17:50:10.725882Z
Version: ae726cf0-1351-4491-93a7-996e632ee8e8
Previous Versions: 1
- 48ef4d2c-b7e4-4bb7-96cb-49b63ad84e3f 2026-05-31T17:50:07.753359Z (65 bytes)
"""
        parsed = self.remote._parse_stat(text)
        self.assertEqual(parsed["size"], 65)
        self.assertEqual(parsed["version_id"], "ae726cf0-1351-4491-93a7-996e632ee8e8")
        self.assertEqual(parsed["uploaded_at"], "2026-05-31T17:50:10.725882Z")
        self.assertEqual(parsed["previous_versions"], 1)

    def test_remote_root_env_override(self):
        os.environ["FULCRA_COORD_REMOTE_ROOT"] = "/myteam/coordination"
        try:
            self.assertEqual(
                self.remote.task_remote_path("TASK-20260531-example-12345678"),
                "/myteam/coordination/tasks/TASK-20260531-example-12345678.json",
            )
        finally:
            del os.environ["FULCRA_COORD_REMOTE_ROOT"]

    def test_default_remote_root(self):
        # Ensure default doesn't contain openclaw-specific paths
        os.environ.pop("FULCRA_COORD_REMOTE_ROOT", None)
        path = self.remote.task_remote_path("TASK-20260531-example-12345678")
        self.assertNotIn("/arc/", path)
        self.assertTrue(path.startswith("/"))

    def test_check_cli_available_requires_file_subcommand(self):
        result = types.SimpleNamespace(
            returncode=2,
            stdout="",
            stderr="Error: No such command 'file'.",
        )
        with patch("fulcra_coord.remote.subprocess.run", return_value=result):
            ok, msg = self.remote.check_cli_available(["uv", "tool", "run", "fulcra-api", "file"])
        self.assertFalse(ok)
        self.assertIn("file command", msg)

    def test_check_cli_available_accepts_file_help(self):
        result = types.SimpleNamespace(returncode=0, stdout="Usage: fulcra file", stderr="")
        with patch("fulcra_coord.remote.subprocess.run", return_value=result):
            ok, msg = self.remote.check_cli_available(["fulcra-api", "file"])
        self.assertTrue(ok)
        self.assertEqual(msg, "fulcra-api file")

    def test_check_file_commands_ok_when_file_help_succeeds(self):
        """`<cli> file --help` exits 0 → file command group present."""
        result = types.SimpleNamespace(
            returncode=0,
            stdout="Usage: fulcra file [OPTIONS]\n  upload\n  download\n  stat",
            stderr="",
        )
        with patch("fulcra_coord.remote.subprocess.run", return_value=result):
            ok, msg = self.remote.check_file_commands(["fulcra-api"])
        self.assertTrue(ok)
        # Message should name the probed base command for transparency.
        self.assertIn("fulcra-api", msg)

    def test_check_file_commands_fail_when_no_file_subcommand(self):
        """Public PyPI build lacking `file` → non-zero exit → FAIL."""
        result = types.SimpleNamespace(
            returncode=2,
            stdout="",
            stderr="Error: No such command 'file'.",
        )
        with patch("fulcra_coord.remote.subprocess.run", return_value=result):
            ok, msg = self.remote.check_file_commands(["fulcra-api"])
        self.assertFalse(ok)
        self.assertIn("file", msg)

    def test_check_file_commands_fail_when_cli_missing(self):
        """CLI binary not installed at all → FileNotFoundError → FAIL, no crash."""
        with patch("fulcra_coord.remote.subprocess.run", side_effect=FileNotFoundError()):
            ok, msg = self.remote.check_file_commands(["nonexistent-cli"])
        self.assertFalse(ok)

    def test_check_file_commands_fail_on_timeout(self):
        """A hung probe must degrade to FAIL, never propagate the timeout."""
        with patch(
            "fulcra_coord.remote.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="fulcra-api file --help", timeout=5),
        ):
            ok, msg = self.remote.check_file_commands(["fulcra-api"])
        self.assertFalse(ok)

    def test_check_file_commands_uses_real_cli_base_not_fake_backend(self):
        """The probe must target the *resolved real CLI* base + `file`, never the
        file-ops fake backend. A fake backend speaks the `file` subcommand protocol
        (e.g. ``stat``/``download`` directly) and has no top-level `file` group, so
        probing it would give a misleading FAIL. With FULCRA_CLI_COMMAND set, the
        probe must shell ``<that base> file --help``."""
        os.environ["FULCRA_CLI_COMMAND"] = "my-fulcra"
        try:
            captured = {}

            def fake_run(cmd, *a, **kw):
                captured["cmd"] = cmd
                return types.SimpleNamespace(returncode=0, stdout="file", stderr="")

            with patch("fulcra_coord.remote.subprocess.run", side_effect=fake_run):
                ok, msg = self.remote.check_file_commands()
            self.assertTrue(ok)
            self.assertEqual(captured["cmd"][:3], ["my-fulcra", "file", "--help"])
        finally:
            del os.environ["FULCRA_CLI_COMMAND"]


# ---------------------------------------------------------------------------
# CLI integration (dry-run via fake backend)
# ---------------------------------------------------------------------------

class TestCLIWithFakeBackend(unittest.TestCase):
    """Exercises CLI commands with a fake backend that never touches Fulcra."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp
        # Fake backend: `false` always exits 1 — no remote access
        self.fake_backend = ["false"]

    def tearDown(self):
        del os.environ["XDG_CACHE_HOME"]

    def _args(self, **kwargs) -> types.SimpleNamespace:
        return types.SimpleNamespace(**kwargs)

    def test_status_empty_cache(self):
        from fulcra_coord.cli import cmd_status
        args = self._args(workstream=None, agent=None, format="table")
        rc = cmd_status(args, backend=self.fake_backend)
        self.assertEqual(rc, 0)

    def test_status_json_empty(self):
        from fulcra_coord.cli import cmd_status
        args = self._args(workstream=None, agent=None, format="json")
        rc = cmd_status(args, backend=self.fake_backend)
        self.assertEqual(rc, 0)

    def test_start_creates_cached_task(self):
        from fulcra_coord.cli import cmd_start
        args = self._args(
            title="Test task from CLI",
            workstream="devops",
            agent="claude-code",
            kind="ops",
            priority="P2",
            summary="Test summary",
            next="Deploy it",
            surface=None,
        )
        rc = cmd_start(args, backend=self.fake_backend)
        # Upload fails with fake backend — returns 1
        self.assertEqual(rc, 1)
        # Task must be cached regardless
        tasks = cache.list_cached_tasks()
        self.assertTrue(
            any(t["title"] == "Test task from CLI" for t in tasks),
            "Task not found in cache after start"
        )

    def test_update_missing_task(self):
        from fulcra_coord.cli import cmd_update
        args = self._args(
            task_id="TASK-20260101-notreal-00000000",
            summary="update",
            next=None,
            blocked_on=None,
            status=None,
            agent="claude-code",
        )
        rc = cmd_update(args, backend=self.fake_backend)
        self.assertEqual(rc, 1)

    def test_done_missing_task(self):
        from fulcra_coord.cli import cmd_done
        args = self._args(
            task_id="TASK-20260101-notreal-00000000",
            evidence="did it",
            verification_level="agent-verified",
            confidence=None,
            agent="claude-code",
        )
        rc = cmd_done(args, backend=self.fake_backend)
        self.assertEqual(rc, 1)

    def test_search_empty(self):
        from fulcra_coord.cli import cmd_search
        args = self._args(query="widget", format="table")
        rc = cmd_search(args, backend=self.fake_backend)
        self.assertEqual(rc, 0)

    def test_search_finds_cached_task(self):
        from fulcra_coord.cli import cmd_start, cmd_search
        start_args = self._args(
            title="Deploy the fuzzy widget",
            workstream="devops",
            agent="claude-code",
            kind="ops",
            priority="P2",
            summary="widget deployment",
            next="run deploy script",
            surface=None,
        )
        cmd_start(start_args, backend=self.fake_backend)

        args = self._args(query="fuzzy widget", format="table")
        rc = cmd_search(args, backend=self.fake_backend)
        self.assertEqual(rc, 0)

    def test_done_full_flow(self):
        """Create task in cache, transition to active, then mark done."""
        from fulcra_coord.cli import cmd_start, cmd_done

        start_args = self._args(
            title="Full flow task",
            workstream="devops",
            agent="claude-code",
            kind="ops",
            priority="P1",
            summary="Testing done flow",
            next="mark done",
            surface=None,
        )
        cmd_start(start_args, backend=self.fake_backend)

        tasks = cache.list_cached_tasks()
        task = next(t for t in tasks if t["title"] == "Full flow task")
        task_id = task["id"]

        # Force to active in cache so done transition is allowed
        active_task = apply_transition(task, "active", by="claude-code")
        cache.write_cached_task(active_task)

        done_args = self._args(
            task_id=task_id,
            evidence="Tests passed, PR merged",
            verification_level="agent-verified",
            confidence="high",
            agent="claude-code",
        )
        rc = cmd_done(done_args, backend=self.fake_backend)
        self.assertIn(rc, (0, 1))  # 0=success, 1=upload-fail (ok in offline test)

        # Task in cache should be done
        cached = cache.read_cached_task(task_id)
        if cached:
            self.assertEqual(cached.get("status"), "done")

    def test_block_flow(self):
        """Create task, force to active, block it."""
        from fulcra_coord.cli import cmd_start, cmd_block

        start_args = self._args(
            title="Block test task",
            workstream="devops",
            agent="claude-code",
            kind="ops",
            priority="P2",
            summary="",
            next="",
            surface=None,
        )
        cmd_start(start_args, backend=self.fake_backend)

        tasks = cache.list_cached_tasks()
        task = next(t for t in tasks if t["title"] == "Block test task")
        task_id = task["id"]

        active_task = apply_transition(task, "active", by="claude-code")
        cache.write_cached_task(active_task)

        block_args = self._args(
            task_id=task_id,
            blocked_on="Waiting for external API key",
            agent="claude-code",
        )
        rc = cmd_block(block_args, backend=self.fake_backend)
        self.assertIn(rc, (0, 1, 2))

        cached = cache.read_cached_task(task_id)
        if cached:
            self.assertEqual(cached.get("status"), "blocked")
            self.assertEqual(cached.get("blocked_on"), "Waiting for external API key")

    def test_doctor_offline(self):
        """Doctor should run without error even offline."""
        from fulcra_coord.cli import cmd_doctor
        args = self._args()
        rc = cmd_doctor(args, backend=self.fake_backend)
        # Returns 1 because CLI not reachable — that's expected
        self.assertIn(rc, (0, 1))

    def test_reconcile_empty(self):
        from fulcra_coord.cli import cmd_reconcile
        args = self._args()
        rc = cmd_reconcile(args, backend=self.fake_backend)
        self.assertIn(rc, (0, 1))


# ---------------------------------------------------------------------------
# _try_merge conflict detection
# ---------------------------------------------------------------------------

class TestTryMerge(unittest.TestCase):
    """Tests for the safe-merge logic in cli._try_merge."""

    def test_conflict_when_both_changed_status(self):
        """Two agents independently transitioning to different statuses = conflict."""
        from fulcra_coord.cli import _try_merge
        base = _sample_task()
        local_active = apply_transition(base, "active", by="agent-a")
        local_done = apply_transition(
            local_active, "done", by="agent-a",
            evidence="tests passed", verification_level="agent-verified",
        )
        remote_abandoned = apply_transition(
            local_active, "abandoned", by="agent-b", reason="scope cut"
        )
        # Both changed status from active → different terminals
        result = _try_merge(local_done, remote_abandoned)
        self.assertIsNone(result, "Should return None when both sides changed status")

    def test_safe_merge_when_remote_only_updated(self):
        """Remote non-status update + local status change should merge cleanly."""
        from fulcra_coord.cli import _try_merge
        base = _sample_task()
        local_active = apply_transition(base, "active", by="agent-a")
        local_done = apply_transition(
            local_active, "done", by="agent-a",
            evidence="shipped", verification_level="agent-verified",
        )
        remote_updated = apply_update(local_active, by="agent-b", summary="Added notes")
        # remote status is still "active"; local is "done"
        result = _try_merge(local_done, remote_updated)
        self.assertIsNotNone(result, "Should merge when remote only updated fields")
        self.assertEqual(result["status"], "done")
        # remote's update event should be included
        event_summaries = [e.get("summary") for e in result.get("events", [])]
        self.assertIn("Added notes", event_summaries)

    def test_same_status_merges_events(self):
        """Same status on both sides: just union the events."""
        from fulcra_coord.cli import _try_merge
        base = _sample_task()
        local_v = apply_update(base, by="agent-a", summary="local note")
        remote_v = apply_update(base, by="agent-b", summary="remote note")
        result = _try_merge(local_v, remote_v)
        self.assertIsNotNone(result)
        summaries = [e.get("summary") for e in result.get("events", [])]
        self.assertIn("local note", summaries)
        self.assertIn("remote note", summaries)

    def test_merge_when_only_remote_changed_status(self):
        """Regression: no ConflictError when only REMOTE changed status.

        Scenario: local agent updates summary (non-status), remote agent
        concurrently activates the task (status change).  The merge must
        succeed — local's non-status update should layer on top of remote's
        authoritative status.

        Before the fix, _try_merge checked `remote_has_new_status_change`
        alone and returned None (ConflictError) even though local never
        changed status.  The correct check requires BOTH sides to have new
        independent status events.
        """
        from fulcra_coord.cli import _try_merge
        base = _sample_task()
        # Local: summary update only (status stays proposed)
        local_updated = apply_update(base, by="agent-a", summary="Local progress note")
        # Remote: independent status activation (proposed → active)
        remote_active = apply_transition(base, "active", by="agent-b")

        result = _try_merge(local_updated, remote_active)
        self.assertIsNotNone(
            result,
            "Should not conflict when only remote changed status and local only updated fields",
        )
        # Remote's status must win
        self.assertEqual(result["status"], "active")
        # Local's update event must be present
        summaries = [e.get("summary") for e in result.get("events", [])]
        self.assertIn("Local progress note", summaries,
                      "Local's update event must survive merge into remote's state")

    def test_merge_only_remote_status_applies_newer_local_fields(self):
        """When local fields are newer than remote's status-change, they should be applied."""
        from fulcra_coord.cli import _try_merge
        import copy
        from datetime import datetime, timezone, timedelta
        base = _sample_task()
        # Remote changes status at T_remote
        t_remote = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
        remote_active = apply_transition(base, "active", by="agent-b", dt=t_remote)

        # Local updates summary AFTER the remote status change
        t_local = t_remote + timedelta(seconds=30)
        local_updated = apply_update(base, by="agent-a", summary="Later local note", dt=t_local)

        result = _try_merge(local_updated, remote_active)
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "active")
        # Local's summary (newer) should override
        self.assertEqual(result.get("current_summary"), "Later local note",
                         "Newer local summary must win over older remote summary")

    def test_merge_only_remote_status_keeps_remote_fields_when_older_local(self):
        """When remote's status change is more recent than local's update, remote fields win."""
        from fulcra_coord.cli import _try_merge
        from datetime import datetime, timezone, timedelta
        base = _sample_task()
        # Local updates summary first
        t_local = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
        local_updated = apply_update(base, by="agent-a", summary="Older local note", dt=t_local)

        # Remote activates AFTER the local update, also setting a summary
        t_remote = t_local + timedelta(seconds=60)
        remote_active = apply_transition(
            base, "active", by="agent-b", summary="Remote activation note", dt=t_remote
        )

        result = _try_merge(local_updated, remote_active)
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "active")
        # Remote is more recent → remote's summary wins
        self.assertEqual(result.get("current_summary"), "Remote activation note",
                         "Remote summary must win when remote update is more recent")


# ---------------------------------------------------------------------------
# Remote refresh semantics
# ---------------------------------------------------------------------------

class TestRemoteRefreshSemantics(unittest.TestCase):
    """Remote-indexed task reads should refresh stale cache and stat baselines."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp

    def tearDown(self):
        del os.environ["XDG_CACHE_HOME"]

    def test_load_all_tasks_refreshes_existing_cached_task(self):
        from fulcra_coord.cli import _load_all_tasks
        from fulcra_coord import remote

        base = _sample_task()
        cache.write_cached_task(base)
        remote_waiting = apply_transition(base, "waiting", by="agent-b", next_action="hand back")
        task_path = remote.task_remote_path(base["id"])

        def fake_download(path, *, backend=None, timeout=None):
            if path.endswith("/index.json"):
                return {
                    "active": [{"id": base["id"]}],
                    "recent_done": [],
                }
            if path.endswith("/search-index.json"):
                return {"records": [{"id": base["id"]}]}
            if path == task_path:
                return remote_waiting
            return None

        with patch("fulcra_coord.cli.remote.download_json", side_effect=fake_download), \
             patch("fulcra_coord.cli.remote.stat", return_value={"version_id": "remote-v2"}):
            tasks = _load_all_tasks(backend=["false"])

        loaded = next(t for t in tasks if t["id"] == base["id"])
        self.assertEqual(loaded["status"], "waiting")
        self.assertEqual(cache.read_cached_task(base["id"])["status"], "waiting")
        self.assertEqual(cache.read_meta(task_path)["version_id"], "remote-v2")

    def test_load_task_caches_stat_when_downloaded_from_remote(self):
        from fulcra_coord.cli import _load_task
        from fulcra_coord import remote

        task = _sample_task()
        task_path = remote.task_remote_path(task["id"])

        with patch("fulcra_coord.cli.remote.download_json", return_value=task), \
             patch("fulcra_coord.cli.remote.stat", return_value={"version_id": "remote-v1"}):
            loaded = _load_task(task["id"], backend=["false"])

        self.assertEqual(loaded["id"], task["id"])
        self.assertEqual(cache.read_meta(task_path)["version_id"], "remote-v1")


# ---------------------------------------------------------------------------
# Upload-failure propagation
# ---------------------------------------------------------------------------

class TestUploadFailurePropagation(unittest.TestCase):
    """Transition commands must return 1 (not 0) when remote upload fails."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp
        self.fake_backend = ["false"]

    def tearDown(self):
        del os.environ["XDG_CACHE_HOME"]

    def _args(self, **kwargs):
        return types.SimpleNamespace(**kwargs)

    def _make_cached_active_task(self):
        t = _sample_task()
        active = apply_transition(t, "active", by="claude-code")
        cache.write_cached_task(active)
        return active

    def test_update_returns_1_on_upload_fail(self):
        from fulcra_coord.cli import cmd_update
        task = self._make_cached_active_task()
        args = self._args(
            task_id=task["id"], summary="new summary",
            next=None, blocked_on=None, status=None, agent="claude-code",
        )
        rc = cmd_update(args, backend=self.fake_backend)
        self.assertEqual(rc, 1, "cmd_update should return 1 when upload fails")

    def test_block_returns_1_on_upload_fail(self):
        from fulcra_coord.cli import cmd_block
        task = self._make_cached_active_task()
        args = self._args(
            task_id=task["id"], blocked_on="waiting on key", agent="claude-code",
        )
        rc = cmd_block(args, backend=self.fake_backend)
        self.assertEqual(rc, 1, "cmd_block should return 1 when upload fails")

    def test_pause_returns_1_on_upload_fail(self):
        from fulcra_coord.cli import cmd_pause
        task = self._make_cached_active_task()
        args = self._args(
            task_id=task["id"], next="resume tomorrow", agent="claude-code",
        )
        rc = cmd_pause(args, backend=self.fake_backend)
        self.assertEqual(rc, 1, "cmd_pause should return 1 when upload fails")

    def test_done_returns_1_on_upload_fail(self):
        from fulcra_coord.cli import cmd_done
        task = self._make_cached_active_task()
        args = self._args(
            task_id=task["id"], evidence="PR merged",
            verification_level="agent-verified", confidence=None, agent="claude-code",
        )
        rc = cmd_done(args, backend=self.fake_backend)
        self.assertEqual(rc, 1, "cmd_done should return 1 when upload fails")
        # But the task should still be locally marked done
        cached = cache.read_cached_task(task["id"])
        self.assertEqual(cached["status"], "done")

    def test_abandon_returns_1_on_upload_fail(self):
        from fulcra_coord.cli import cmd_abandon
        task = self._make_cached_active_task()
        args = self._args(
            task_id=task["id"], reason="scope cut", agent="claude-code",
        )
        rc = cmd_abandon(args, backend=self.fake_backend)
        self.assertEqual(rc, 1, "cmd_abandon should return 1 when upload fails")


# ---------------------------------------------------------------------------
# Search field consistency
# ---------------------------------------------------------------------------

class TestSearchFieldConsistency(unittest.TestCase):
    """Cached search-index path must search the same fields as the task path."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp

    def tearDown(self):
        del os.environ["XDG_CACHE_HOME"]

    def _args(self, **kwargs):
        return types.SimpleNamespace(**kwargs)

    def _seed_search_index(self):
        """Write a fake search-index to cache with two distinct tasks."""
        records = [
            {
                "id": "TASK-20260101-alpha-aaaaaaaa",
                "title": "Deploy alpha service",
                "status": "active",
                "workstream": "devops",
                "owner_agent": "claude-code",
                "tags": ["workstream:devops", "kind:ops"],
                "summary": "alpha deployment in progress",
                "task_file": "/coordination/tasks/TASK-20260101-alpha-aaaaaaaa.json",
                "updated_at": "2026-01-01T00:00:00Z",
            },
            {
                "id": "TASK-20260101-beta-bbbbbbbb",
                "title": "Research beta feature",
                "status": "proposed",
                "workstream": "research",
                "owner_agent": "codex",
                "tags": ["workstream:research", "kind:research"],
                "summary": "investigating options",
                "task_file": "/coordination/tasks/TASK-20260101-beta-bbbbbbbb.json",
                "updated_at": "2026-01-02T00:00:00Z",
            },
        ]
        cache.write_cached_view("search-index", {
            "schema": "fulcra.coordination.search_index.v1",
            "updated_at": "2026-01-02T00:00:00Z",
            "records": records,
        })

    def test_search_by_title_hits_correct_record(self):
        from fulcra_coord.cli import cmd_search
        self._seed_search_index()
        args = self._args(query="alpha", format="json")
        with patch("builtins.print") as mock_print:
            cmd_search(args, backend=["false"])
        # Capture printed JSON
        printed = "".join(str(c) for call in mock_print.call_args_list
                          for c in call.args)
        data = json.loads(printed)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["id"], "TASK-20260101-alpha-aaaaaaaa")

    def test_search_does_not_match_json_structural_keys(self):
        """Searching for 'title' (a JSON key in every record) must not match all records."""
        from fulcra_coord.cli import cmd_search
        self._seed_search_index()
        args = self._args(query="title", format="json")
        with patch("builtins.print") as mock_print:
            cmd_search(args, backend=["false"])
        printed = "".join(str(c) for call in mock_print.call_args_list
                          for c in call.args)
        data = json.loads(printed)
        # "title" is a JSON key but must not match any record's meaningful fields
        self.assertEqual(data["count"], 0,
                         "'title' is a JSON key and must not match any record's content fields")

    def test_search_by_tag_hits_record(self):
        from fulcra_coord.cli import cmd_search
        self._seed_search_index()
        args = self._args(query="kind:research", format="json")
        with patch("builtins.print") as mock_print:
            cmd_search(args, backend=["false"])
        printed = "".join(str(c) for call in mock_print.call_args_list
                          for c in call.args)
        data = json.loads(printed)
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["id"], "TASK-20260101-beta-bbbbbbbb")

    def test_table_display_shows_summary_from_cached_index(self):
        """Regression: table output must show 'summary' from cached search-index records.

        Cached records use 'summary'; task_summary() dicts use 'current_summary'.
        The display code must handle both so summaries don't silently disappear
        when the cached search-index path is taken.
        """
        from fulcra_coord.cli import cmd_search
        self._seed_search_index()
        args = self._args(query="deployment", format="table")
        output_lines = []
        with patch("builtins.print", side_effect=lambda *a, **kw: output_lines.append(" ".join(str(x) for x in a))):
            cmd_search(args, backend=["false"])
        combined = "\n".join(output_lines)
        # The alpha record has summary "alpha deployment in progress"
        self.assertIn("alpha deployment in progress", combined,
                      "Summary from cached search-index must appear in table output")


# ---------------------------------------------------------------------------
# Pass-2 regression tests
# ---------------------------------------------------------------------------

class TestUpdateStatusDoneRejected(unittest.TestCase):
    """Regression: 'update --status done' must be rejected at the CLI parser level.

    apply_transition('done') always requires --evidence and --verification-level,
    which cmd_update never passes. Rather than surfacing a cryptic SchemaError,
    'done' must be removed from update's --status choices so argparse rejects it
    with a clear usage error before any schema code runs.
    """

    def test_done_not_in_update_status_choices(self):
        from fulcra_coord.entry import build_parser
        parser = build_parser()
        # Find the 'update' subparser
        update_sub = None
        for action in parser._subparsers._group_actions:
            if hasattr(action, '_name_parser_map'):
                update_sub = action._name_parser_map.get("update")
                break
        self.assertIsNotNone(update_sub, "Could not find 'update' subparser")
        # Find the --status action
        status_action = next(
            (a for a in update_sub._actions if getattr(a, 'dest', '') == 'status'),
            None,
        )
        self.assertIsNotNone(status_action, "Could not find --status in update subparser")
        self.assertNotIn(
            "done", status_action.choices or [],
            "'done' must not be a valid choice for 'update --status': "
            "use the dedicated 'done' command which enforces --evidence",
        )

    def test_update_status_done_raises_via_argparse(self):
        """Argparse must reject --status done on 'update' before any schema code runs."""
        from fulcra_coord.entry import build_parser
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args(["update", "TASK-20260101-test-00000000", "--status", "done"])


class TestExtraTagsPreservedOnTransition(unittest.TestCase):
    """Regression: apply_transition must not drop non-standard tags.

    Before the fix, rebuilding tags from the 5 standard prefixes only caused
    any programmatically-added extra tags (e.g. 'sprint:2') to be silently
    discarded after the first status transition.
    """

    def test_extra_tags_survive_transition(self):
        t = _sample_task()
        # Inject a non-standard tag
        t["tags"].append("sprint:42")
        t["tags"] = sorted(set(t["tags"]))
        t2 = apply_transition(t, "active", by="agent-a")
        self.assertIn("sprint:42", t2["tags"],
                      "Non-standard tag 'sprint:42' must survive apply_transition")

    def test_standard_tags_still_updated(self):
        t = _sample_task()  # status = proposed
        t["tags"].append("sprint:42")
        t2 = apply_transition(t, "active", by="agent-a")
        self.assertIn("status:active", t2["tags"],
                      "status tag must be updated to new status after transition")
        self.assertNotIn("status:proposed", t2["tags"],
                         "old status tag must be removed after transition")

    def test_extra_tags_survive_done_transition(self):
        t = _with_status(_sample_task(), "active")
        t["tags"].append("sprint:42")
        t2 = apply_transition(
            t, "done", by="agent-a",
            evidence="shipped", verification_level="agent-verified",
        )
        self.assertIn("sprint:42", t2["tags"],
                      "Non-standard tag must survive done transition")
        self.assertIn("status:done", t2["tags"])

    def test_no_extra_tags_is_unchanged(self):
        """Transition without extras must still produce exactly the 5 standard tags."""
        t = _sample_task()
        # Standard task has exactly 5 standard tags
        t2 = apply_transition(t, "active", by="agent-a")
        standard_count = sum(
            1 for tag in t2["tags"]
            if any(tag.startswith(p) for p in
                   ("workstream:", "agent:", "kind:", "status:", "priority:"))
        )
        self.assertEqual(len(t2["tags"]), standard_count,
                         "With no extra tags, all tags must be standard prefixed tags")


# ---------------------------------------------------------------------------
# Transition table completeness
# ---------------------------------------------------------------------------

class TestTransitionTable(unittest.TestCase):
    def test_all_valid_statuses_have_transitions(self):
        from fulcra_coord.schema import STATUS_TRANSITIONS, VALID_STATUSES
        for s in VALID_STATUSES:
            self.assertIn(s, STATUS_TRANSITIONS,
                          f"Status {s!r} missing from STATUS_TRANSITIONS")

    def test_transition_targets_are_valid(self):
        from fulcra_coord.schema import STATUS_TRANSITIONS, VALID_STATUSES
        for src, targets in STATUS_TRANSITIONS.items():
            for tgt in targets:
                self.assertIn(tgt, VALID_STATUSES,
                              f"Transition target {tgt!r} not in VALID_STATUSES")


# ---------------------------------------------------------------------------
# Remote root isolation
# ---------------------------------------------------------------------------

class TestRemoteRootIsolation(unittest.TestCase):
    """Verify the package doesn't embed OpenClaw-specific paths."""

    def tearDown(self):
        os.environ.pop("FULCRA_COORD_REMOTE_ROOT", None)

    def test_default_root_is_not_arc_specific(self):
        from fulcra_coord import remote_root
        os.environ.pop("FULCRA_COORD_REMOTE_ROOT", None)
        r = remote_root()
        self.assertNotIn("/arc/", r)
        self.assertTrue(r.startswith("/"))

    def test_custom_root_via_env(self):
        from fulcra_coord import remote_root
        os.environ["FULCRA_COORD_REMOTE_ROOT"] = "/myorg/coord"
        self.assertEqual(remote_root(), "/myorg/coord")

    def test_custom_root_leading_slash_normalized(self):
        from fulcra_coord import remote_root
        os.environ["FULCRA_COORD_REMOTE_ROOT"] = "myorg/coord"
        self.assertEqual(remote_root(), "/myorg/coord")


# ---------------------------------------------------------------------------
# Reconcile op-marker preservation on partial failure (regression)
# ---------------------------------------------------------------------------

class TestReconcilePreservesMarkersOnFailure(unittest.TestCase):
    """Regression: op markers must survive a partially failed reconcile run.

    Before the fix, clear_op_marker() was called before checking view upload
    failures, so a partial reconcile silently discarded needs_reconcile markers
    and the subsequent `status` command showed no pending-repair warning.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp

    def tearDown(self):
        del os.environ["XDG_CACHE_HOME"]

    def test_markers_survive_partial_reconcile(self):
        """If any view upload fails during reconcile, needs_reconcile markers must persist."""
        from fulcra_coord.cli import cmd_reconcile

        cache.ensure_dirs()
        cache.write_op_marker("repair01", {
            "op_id": "repair01",
            "command": "update",
            "task_id": "TASK-20260101-test-aaaaaaaa",
            "status": "partial",
            "needs_reconcile": True,
            "started_at": "2026-01-01T00:00:00Z",
        })

        # Fake backend that always fails — all view uploads return non-zero
        args = types.SimpleNamespace()
        rc = cmd_reconcile(args, backend=["false"])

        # Reconcile should fail (return 1)
        self.assertEqual(rc, 1)

        # The marker must still be present — reconcile did not fully succeed
        remaining = cache.list_op_markers()
        self.assertTrue(
            any(m["op_id"] == "repair01" for m in remaining),
            "needs_reconcile marker must not be cleared when view uploads fail",
        )

    def test_markers_cleared_on_full_success(self):
        """When reconcile fully succeeds, needs_reconcile markers should be cleared."""
        from fulcra_coord.cli import cmd_reconcile

        cache.ensure_dirs()
        cache.write_op_marker("repair02", {
            "op_id": "repair02",
            "command": "start",
            "task_id": "TASK-20260101-test-bbbbbbbb",
            "status": "partial",
            "needs_reconcile": True,
            "started_at": "2026-01-01T00:00:00Z",
        })

        # Patch remote.upload_json to always succeed so reconcile completes cleanly
        with patch("fulcra_coord.cli.remote.upload_json", return_value=True), \
             patch("fulcra_coord.cli.remote.download_json", return_value=None):
            args = types.SimpleNamespace()
            rc = cmd_reconcile(args, backend=["false"])

        self.assertEqual(rc, 0)

        remaining = cache.list_op_markers()
        self.assertFalse(
            any(m["op_id"] == "repair02" for m in remaining),
            "needs_reconcile marker must be cleared after successful reconcile",
        )


# ---------------------------------------------------------------------------
# Doctor remote-access exit code (regression)
# ---------------------------------------------------------------------------

class TestDoctorRemoteAccessExitCode(unittest.TestCase):
    """Regression: cmd_doctor must return 1 when remote access fails.

    Before the fix, a working CLI + broken remote (e.g. auth failure) would
    cause doctor to exit 0, misleading agents into thinking the system was
    healthy and able to read/write coordination data.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp

    def tearDown(self):
        del os.environ["XDG_CACHE_HOME"]

    def test_doctor_fails_when_remote_unreachable(self):
        """CLI reachable but remote stat fails → exit 1."""
        from fulcra_coord.cli import cmd_doctor

        with patch("fulcra_coord.cli.remote.check_cli_available", return_value=(True, "fulcra-api file")), \
             patch("fulcra_coord.cli.remote.check_file_commands", return_value=(True, "fulcra-api file")), \
             patch("fulcra_coord.cli.remote.check_remote_access",
                   return_value=(False, "Could not stat /coordination/index.json")):
            args = types.SimpleNamespace()
            rc = cmd_doctor(args, backend=["false"])

        self.assertEqual(rc, 1, "doctor must return 1 when remote access fails")

    def test_doctor_succeeds_when_both_ok(self):
        """CLI reachable and remote accessible → exit 0."""
        from fulcra_coord.cli import cmd_doctor

        with patch("fulcra_coord.cli.remote.check_cli_available", return_value=(True, "fulcra-api file")), \
             patch("fulcra_coord.cli.remote.check_file_commands", return_value=(True, "fulcra-api file")), \
             patch("fulcra_coord.cli.remote.check_remote_access",
                   return_value=(True, "Remote accessible (/coordination/index.json)")):
            args = types.SimpleNamespace()
            rc = cmd_doctor(args, backend=["false"])

        self.assertEqual(rc, 0, "doctor must return 0 when CLI and remote are both healthy")


class TestDoctorFileCapabilityCheck(unittest.TestCase):
    """Doctor must explicitly probe the `file` command group.

    The #1 fresh-agent onboarding failure: the public PyPI `fulcra-api` build
    lacks the `file` command group that drives the coordination bus. Without a
    dedicated probe, the agent installs fulcra-api, runs fulcra-coord, and every
    bus op fails silently with no clear signal why. Doctor surfaces this with a
    FAIL + the fix (install the file-capable build per docs/fulcra-cli-branch.md)
    and marks the overall result not-ok.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp

    def tearDown(self):
        del os.environ["XDG_CACHE_HOME"]

    def _run_doctor(self, file_ok, file_msg):
        from fulcra_coord.cli import cmd_doctor

        lines = []
        with patch("fulcra_coord.cli.remote.check_cli_available", return_value=(True, "fulcra-api file")), \
             patch("fulcra_coord.cli.remote.check_file_commands", return_value=(file_ok, file_msg)), \
             patch("fulcra_coord.cli.remote.check_remote_access",
                   return_value=(True, "Remote accessible")), \
             patch("fulcra_coord.cli._info",
                   side_effect=lambda *a, **kw: lines.append(" ".join(str(x) for x in a))):
            rc = cmd_doctor(types.SimpleNamespace(), backend=["false"])
        return rc, "\n".join(lines)

    def test_doctor_reports_file_commands_ok(self):
        """Probe succeeds → `File commands: OK` printed, exit stays 0."""
        rc, out = self._run_doctor(True, "fulcra-api file")
        self.assertIn("File commands: OK", out)
        self.assertEqual(rc, 0)

    def test_doctor_reports_file_commands_fail_with_fix(self):
        """Probe fails → FAIL line + fix hint pointing at the file-capable build,
        and the overall doctor result is not-ok (exit 1)."""
        rc, out = self._run_doctor(False, "No such command 'file'.")
        self.assertIn("File commands: FAIL", out)
        self.assertIn("file-management", out)
        self.assertIn("docs/fulcra-cli-branch.md", out)
        self.assertEqual(rc, 1, "missing file command group must mark doctor not-ok")

    def test_doctor_file_probe_never_crashes(self):
        """If the underlying probe raises, doctor must still complete (the helper
        swallows exceptions → FAIL), never propagate the exception."""
        from fulcra_coord.cli import cmd_doctor

        with patch("fulcra_coord.cli.remote.check_cli_available", return_value=(True, "fulcra-api file")), \
             patch("fulcra_coord.cli.remote.check_file_commands",
                   side_effect=RuntimeError("unexpected")), \
             patch("fulcra_coord.cli.remote.check_remote_access",
                   return_value=(True, "Remote accessible")):
            # Even if the helper itself were to raise, cmd_doctor must not crash.
            # In practice check_file_commands swallows its own errors; this guards
            # the call site too.
            try:
                rc = cmd_doctor(types.SimpleNamespace(), backend=["false"])
            except Exception as e:  # pragma: no cover - this is the assertion
                self.fail(f"cmd_doctor crashed on file-probe error: {e}")
        self.assertEqual(rc, 1)


# ---------------------------------------------------------------------------
# Pass-3 regression tests
# ---------------------------------------------------------------------------

class TestConflictDetectionWithNoCachedMeta(unittest.TestCase):
    """Regression: _write_task_and_views must perform conflict detection even when
    cached_meta is None (first write from a remote/cloud machine that loaded the task
    via _load_task or _load_all_tasks but never previously wrote it here).

    Before the fix, the condition was:
      if pre_stat and cached_meta and remote.stat_changed(...)
    which is always False when cached_meta is None, silently overwriting concurrent
    remote changes from other agents.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp

    def tearDown(self):
        del os.environ["XDG_CACHE_HOME"]

    def test_remote_events_merged_when_no_cached_meta(self):
        """Remote non-status update is merged into local task when no cached meta exists.

        Scenario: both machines have the same proposed task. The remote agent added a
        summary note (non-status update). The local agent is about to write a summary
        update too. With no cached meta, the merge check must fire and incorporate
        the remote's extra event rather than silently overwriting it.
        """
        import copy
        from fulcra_coord.cli import _write_task_and_views

        base = make_task(title="Fresh machine merge test", workstream="devops", agent="agent-a")
        # Remote: a non-status update was made by another agent (still proposed)
        remote_version = apply_update(base, by="agent-b", summary="Remote progress note")

        # Local: a different non-status update (still proposed), no meta stored
        local_version = apply_update(base, by="agent-a", summary="Local progress note")
        cache.write_cached_task(local_version)
        # No cache.write_meta() — simulating first load on a remote machine

        pre_stat = {"version_id": "v999", "size": 200}
        captured = {}

        def _fake_upload(data, path, *, backend=None, timeout=None):
            # Only capture the task file (not view files)
            if "tasks/" in path:
                captured["task"] = data
            return True

        with patch("fulcra_coord.cli.remote.stat", return_value=pre_stat), \
             patch("fulcra_coord.cli.remote.download_json", return_value=remote_version), \
             patch("fulcra_coord.cli.remote.upload_json", side_effect=_fake_upload):
            _write_task_and_views(local_version, backend=["false"])

        uploaded = captured.get("task", {})
        event_summaries = [e.get("summary") for e in uploaded.get("events", [])]
        self.assertIn("Remote progress note", event_summaries,
                      "Remote agent's event must be merged in when cached_meta is None")

    def test_conflict_raised_when_both_sides_changed_status_no_meta(self):
        """ConflictError must fire for conflicting status changes even with no cached meta."""
        import copy
        from fulcra_coord.cli import _write_task_and_views

        base = make_task(title="Conflict no-meta test", workstream="devops", agent="agent-a")
        local_active = apply_transition(base, "active", by="agent-a")
        local_done = apply_transition(
            local_active, "done", by="agent-a",
            evidence="shipped", verification_level="agent-verified",
        )
        # Remote: another agent independently abandoned the task
        remote_abandoned = apply_transition(
            local_active, "abandoned", by="agent-b", reason="scope cut"
        )

        cache.write_cached_task(local_done)
        # No meta stored — fresh machine

        pre_stat = {"version_id": "v111", "size": 300}

        with patch("fulcra_coord.cli.remote.stat", return_value=pre_stat), \
             patch("fulcra_coord.cli.remote.download_json", return_value=remote_abandoned):
            from fulcra_coord import schema
            with self.assertRaises(schema.ConflictError,
                                   msg="ConflictError must raise on conflicting status changes even with no cached meta"):
                _write_task_and_views(local_done, backend=["false"])

    def test_no_merge_when_remote_file_absent(self):
        """No merge check when pre_stat is None (new task — file not yet on remote)."""
        import copy
        from fulcra_coord.cli import _write_task_and_views

        base = make_task(title="Brand new task", workstream="devops", agent="agent-a")
        cache.write_cached_task(base)

        download_called = []

        def _fake_upload(data, path, *, backend=None, timeout=None):
            return True

        with patch("fulcra_coord.cli.remote.stat", return_value=None), \
             patch("fulcra_coord.cli.remote.download_json",
                   side_effect=lambda *a, **kw: download_called.append(a) or None), \
             patch("fulcra_coord.cli.remote.upload_json", side_effect=_fake_upload):
            _write_task_and_views(base, backend=["false"])

        # download_json may be called for view building (index etc.) but NOT for conflict check
        # The task-path download for conflict detection must not occur when pre_stat is None
        task_path = __import__("fulcra_coord.remote", fromlist=["task_remote_path"]).task_remote_path(base["id"])
        task_downloads = [a for a in download_called if a and task_path in str(a)]
        self.assertEqual(len(task_downloads), 0,
                         "No merge-check download should happen when remote file does not exist (pre_stat is None)")


class TestVerificationLevelHelpText(unittest.TestCase):
    """Regression: --verification-level valid choices must appear in help output.

    Before the fix, metavar="LEVEL" was set without a help= string, causing
    argparse to hide the valid choices. A remote agent running
    'fulcra-coord done --help' would see '--verification-level LEVEL' with no
    indication of what values are accepted.
    """

    def test_verification_level_help_lists_choices(self):
        from fulcra_coord.entry import build_parser
        parser = build_parser()
        done_sub = None
        for action in parser._subparsers._group_actions:
            if hasattr(action, "_name_parser_map"):
                done_sub = action._name_parser_map.get("done")
                break
        self.assertIsNotNone(done_sub, "Could not find 'done' subparser")

        vl_action = next(
            (a for a in done_sub._actions if getattr(a, "dest", "") == "verification_level"),
            None,
        )
        self.assertIsNotNone(vl_action, "Could not find --verification-level action")
        help_text = vl_action.help or ""
        has_choice = any(
            c in help_text
            for c in ("agent-verified", "human-verified", "automated", "unverified")
        )
        self.assertTrue(
            has_choice,
            f"--verification-level help must list at least one valid choice; got: {help_text!r}",
        )


# ---------------------------------------------------------------------------
# Pass-4 regression tests
# ---------------------------------------------------------------------------

class TestInstallShimNoSelfReference(unittest.TestCase):
    """Regression: install-shim must not write a shim that calls itself.

    When the package entry point is already at ~/.local/bin/fulcra-coord
    (e.g. `pip install --user`), sys.argv[0] resolves to the same path as the
    shim destination.  Before the fix, the shim would contain
    `exec "/home/user/.local/bin/fulcra-coord" "$@"` — an infinite loop.
    The fix: if src.resolve() == shim_path.resolve(), fall back to the
    `python3 -m fulcra_coord` invocation instead.
    """

    def test_shim_uses_fallback_when_src_is_shim_target(self):
        import tempfile, stat as stat_mod
        from pathlib import Path
        from fulcra_coord import cli as _cli_mod

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            # Simulate: the installed script IS already at the shim destination
            fake_bin = tmp / ".local" / "bin"
            fake_bin.mkdir(parents=True)
            fake_entry = fake_bin / "fulcra-coord"
            fake_entry.write_text("#!/usr/bin/env bash\nexec python3 -m fulcra_coord \"$@\"\n")
            fake_entry.chmod(fake_entry.stat().st_mode | stat_mod.S_IEXEC)

            # Patch sys.argv[0] to point to the would-be shim destination
            with patch("sys.argv", [str(fake_entry), "install-shim"]), \
                 patch.object(Path, "home", return_value=tmp):
                args = types.SimpleNamespace()
                rc = _cli_mod.cmd_install_shim(args)

            self.assertEqual(rc, 0)
            shim = fake_bin / "fulcra-coord"
            content = shim.read_text()
            # Must NOT exec itself — must use python3 -m fallback
            self.assertNotIn(str(fake_entry), content,
                             "Shim must not exec its own path (infinite loop)")
            self.assertIn("python3 -m fulcra_coord", content,
                          "Shim must fall back to python3 -m when src == shim_path")

    def test_shim_uses_src_when_different_from_shim_target(self):
        """Normal install: entry point at a different path → exec that path."""
        import tempfile, stat as stat_mod
        from pathlib import Path
        from fulcra_coord import cli as _cli_mod

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            # Entry point in a venv, not in ~/.local/bin
            venv_bin = tmp / ".venv" / "bin"
            venv_bin.mkdir(parents=True)
            venv_entry = venv_bin / "fulcra-coord"
            venv_entry.write_text("#!/usr/bin/env python3\n# venv entry\n")
            venv_entry.chmod(venv_entry.stat().st_mode | stat_mod.S_IEXEC)

            fake_home_bin = tmp / ".local" / "bin"
            fake_home_bin.mkdir(parents=True)

            with patch("sys.argv", [str(venv_entry), "install-shim"]), \
                 patch.object(Path, "home", return_value=tmp):
                args = types.SimpleNamespace()
                rc = _cli_mod.cmd_install_shim(args)

            self.assertEqual(rc, 0)
            shim = fake_home_bin / "fulcra-coord"
            content = shim.read_text()
            # Must exec the venv entry point
            self.assertIn(str(venv_entry), content,
                          "Shim must exec the venv entry point when it differs from shim path")
            self.assertNotIn("python3 -m fulcra_coord", content)


class TestTryMergeSymmetryAndEdgeCases(unittest.TestCase):
    """Additional edge-case tests for _try_merge after pass-4 fix."""

    def test_both_status_changes_still_conflict(self):
        """Verify existing conflict detection still works after fix."""
        from fulcra_coord.cli import _try_merge
        base = _sample_task()
        local_active = apply_transition(base, "active", by="agent-a")
        local_done = apply_transition(
            local_active, "done", by="agent-a",
            evidence="tests passed", verification_level="agent-verified",
        )
        remote_abandoned = apply_transition(
            local_active, "abandoned", by="agent-b", reason="scope cut"
        )
        result = _try_merge(local_done, remote_abandoned)
        self.assertIsNone(result,
                          "Conflict must still be detected when both sides changed status")

    def test_local_status_change_remote_update_still_merges(self):
        """Existing pass-3 test: local status change + remote non-status update still merges."""
        from fulcra_coord.cli import _try_merge
        base = _sample_task()
        local_active = apply_transition(base, "active", by="agent-a")
        local_done = apply_transition(
            local_active, "done", by="agent-a",
            evidence="shipped", verification_level="agent-verified",
        )
        remote_updated = apply_update(local_active, by="agent-b", summary="Added notes")
        result = _try_merge(local_done, remote_updated)
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "done")
        event_summaries = [e.get("summary") for e in result.get("events", [])]
        self.assertIn("Added notes", event_summaries)


# ---------------------------------------------------------------------------
# Pass-4 regression tests (continued)
# ---------------------------------------------------------------------------

class TestLoadAllTasksCachesRemoteSearchIndex(unittest.TestCase):
    """Regression: _load_all_tasks must write the remote search-index to local cache.

    Before the fix, the remote search-index was downloaded only to extract task IDs —
    it was never written to local cache.  A subsequent cmd_search would therefore use
    a stale (or absent) local search-index view, missing remotely-updated task fields
    (title, summary) even after status had already refreshed individual task files.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp

    def tearDown(self):
        del os.environ["XDG_CACHE_HOME"]

    def test_load_all_tasks_writes_remote_search_index_to_local_cache(self):
        """After _load_all_tasks, the local search-index cache must reflect remote content."""
        from fulcra_coord.cli import _load_all_tasks
        from fulcra_coord import remote as _remote

        # No local cache initially
        self.assertIsNone(cache.read_cached_view("search-index"))

        remote_search_index = {
            "schema": "fulcra.coordination.search_index.v1",
            "updated_at": "2026-06-01T10:00:00Z",
            "records": [
                {
                    "id": "TASK-20260601-remote-only-aabbccdd",
                    "title": "Remote only task",
                    "status": "active",
                    "priority": "P1",
                    "workstream": "devops",
                    "owner_agent": "remote-agent",
                    "tags": ["workstream:devops"],
                    "summary": "remote summary",
                    "task_file": "/coordination/tasks/TASK-20260601-remote-only-aabbccdd.json",
                    "updated_at": "2026-06-01T09:00:00Z",
                }
            ],
        }

        def fake_download(path, *, backend=None, timeout=None):
            if path.endswith("/index.json"):
                return {"active": [], "recent_done": []}
            if path.endswith("/search-index.json"):
                return remote_search_index
            return None

        with patch("fulcra_coord.cli.remote.download_json", side_effect=fake_download), \
             patch("fulcra_coord.cli.remote.stat", return_value=None):
            _load_all_tasks(backend=["false"])

        cached = cache.read_cached_view("search-index")
        self.assertIsNotNone(cached,
                             "_load_all_tasks must write remote search-index to local cache")
        records = cached.get("records", [])
        ids = [r["id"] for r in records]
        self.assertIn("TASK-20260601-remote-only-aabbccdd", ids,
                      "Remote search-index records must be preserved in the local cache")

    def test_search_sees_remote_updates_after_status_run(self):
        """cmd_search must find remotely-updated task fields after cmd_status is run.

        Scenario: local search-index has task Z with old title. Remote updates Z's title.
        Running cmd_status calls _load_all_tasks which now caches the remote search-index.
        A subsequent cmd_search must find Z by its new title (from the remote search-index).
        """
        from fulcra_coord.cli import cmd_status, cmd_search

        # Seed a stale local search-index with old title
        stale_record = {
            "id": "TASK-20260601-updated-task-deadbeef",
            "title": "Old title",
            "status": "active",
            "priority": "P2",
            "workstream": "devops",
            "owner_agent": "agent-a",
            "tags": ["workstream:devops"],
            "summary": "old summary",
            "task_file": "/coordination/tasks/TASK-20260601-updated-task-deadbeef.json",
            "updated_at": "2026-06-01T08:00:00Z",
        }
        cache.write_cached_view("search-index", {
            "schema": "fulcra.coordination.search_index.v1",
            "updated_at": "2026-06-01T08:00:00Z",
            "records": [stale_record],
        })

        # Remote has the updated search-index with new title
        updated_record = dict(stale_record, title="New unique title XYZ99", updated_at="2026-06-01T10:00:00Z")
        fresh_remote_index = {
            "schema": "fulcra.coordination.search_index.v1",
            "updated_at": "2026-06-01T10:00:00Z",
            "records": [updated_record],
        }

        def fake_download(path, *, backend=None, timeout=None):
            if path.endswith("/index.json"):
                return {"active": [{"id": stale_record["id"]}], "recent_done": []}
            if path.endswith("/search-index.json"):
                return fresh_remote_index
            return None

        with patch("fulcra_coord.cli.remote.download_json", side_effect=fake_download), \
             patch("fulcra_coord.cli.remote.stat", return_value=None):
            status_args = types.SimpleNamespace(workstream=None, agent=None, format="table")
            cmd_status(status_args, backend=["false"])

        # After status, search by new title must succeed
        results_found = []
        with patch("builtins.print", side_effect=lambda *a, **kw: None):
            search_args = types.SimpleNamespace(query="XYZ99", format="json")
            with patch("builtins.print") as mock_print:
                cmd_search(search_args, backend=["false"])
            printed = "".join(str(c) for call in mock_print.call_args_list for c in call.args)
        try:
            data = json.loads(printed)
            results_found = data.get("results", [])
        except (json.JSONDecodeError, UnboundLocalError):
            pass

        self.assertTrue(
            any(r["id"] == stale_record["id"] for r in results_found),
            "search must find task by its remotely-updated title after cmd_status refreshes the search-index cache",
        )


class TestTryMergeLocalStatusChangeRemoteNewerFields(unittest.TestCase):
    """Regression: _try_merge must apply remote's newer field values when only local
    changed status and remote concurrently updated non-status fields with a later timestamp.

    Before the fix, the 'same status / only local changed status' path took local as
    base unconditionally — remote's concurrent summary/next_action updates were silently
    discarded even when remote's updated_at was later than local's.

    The fix makes the path symmetric with the 'only remote changed status' path, which
    already applied the newer side's non-status fields.
    """

    def test_remote_newer_fields_applied_when_only_local_changed_status(self):
        """Local changed status; remote updated fields with a later timestamp → remote fields win."""
        from fulcra_coord.cli import _try_merge
        from datetime import datetime, timezone, timedelta

        base = _sample_task()  # proposed
        t_local = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
        t_remote = t_local + timedelta(seconds=30)

        # Local: proposed → active at T_local (status change, older timestamp)
        local_active = apply_transition(base, "active", by="agent-a", dt=t_local)

        # Remote: non-status update with newer summary at T_remote (no status change)
        remote_updated = apply_update(base, by="agent-b", summary="Remote newer summary", dt=t_remote)
        # remote_updated.status == "proposed" (unchanged), remote_updated.updated_at > local_active.updated_at

        result = _try_merge(local_active, remote_updated)
        self.assertIsNotNone(result, "Merge must succeed: only local changed status")
        self.assertEqual(result["status"], "active",
                         "Local's status transition must be preserved")
        self.assertEqual(result.get("current_summary"), "Remote newer summary",
                         "Remote's newer summary must override local's older summary")

    def test_local_newer_fields_kept_when_local_is_more_recent(self):
        """Local changed status with a later timestamp → local fields win (no remote override)."""
        from fulcra_coord.cli import _try_merge
        from datetime import datetime, timezone, timedelta

        base = _sample_task()
        t_remote = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
        t_local = t_remote + timedelta(seconds=60)

        # Remote: non-status update with older summary
        remote_updated = apply_update(base, by="agent-b", summary="Older remote summary", dt=t_remote)

        # Local: proposed → active at T_local (status change, newer timestamp), with its own summary
        local_active = apply_transition(base, "active", by="agent-a",
                                        summary="Newer local summary", dt=t_local)

        result = _try_merge(local_active, remote_updated)
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "active")
        self.assertEqual(result.get("current_summary"), "Newer local summary",
                         "Local's newer summary must win when local is more recent")

    def test_same_status_remote_newer_fields_applied(self):
        """Same status on both sides; remote has newer field update → remote fields win."""
        from fulcra_coord.cli import _try_merge
        from datetime import datetime, timezone, timedelta

        base = _sample_task()
        t_local = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
        t_remote = t_local + timedelta(seconds=15)

        local_v = apply_update(base, by="agent-a", summary="Local note", dt=t_local)
        remote_v = apply_update(base, by="agent-b", summary="Remote newer note", dt=t_remote)

        result = _try_merge(local_v, remote_v)
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "proposed")
        self.assertEqual(result.get("current_summary"), "Remote newer note",
                         "Remote's newer summary must win when remote updated_at is later")


# ---------------------------------------------------------------------------
# Pass-5 regression tests
# ---------------------------------------------------------------------------

class TestViewFanOutTaskSetFreshness(unittest.TestCase):
    """Regression: _write_task_and_views must build views from the full remote task
    set, not just the locally-cached subset.

    Bug: commands that update a single task (update, block, pause, done, abandon)
    call _load_task() which puts only ONE task in the local cache. The original
    code then called cache.list_cached_tasks() to build views, producing views
    that dropped every task the machine never individually fetched. On a fresh
    machine with an incomplete cache this silently corrupted the global views
    seen by all agents.

    Fix: replace cache.list_cached_tasks() with _load_all_tasks(backend=backend)
    so that the remote index is consulted and all known tasks are fetched into
    cache before view generation.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp

    def tearDown(self):
        del os.environ["XDG_CACHE_HOME"]

    def test_views_include_remote_tasks_not_in_local_cache(self):
        """Views built during a single-task write must include tasks only on remote.

        Scenario:
          - Remote has TASK-A (active) and TASK-B (active), both in the remote index.
          - Fresh machine only has TASK-A in its local cache (just ran _load_task).
          - Machine updates TASK-A and calls _write_task_and_views.
          - Before the fix: views built from cache.list_cached_tasks() → only TASK-A.
          - After the fix: views built from _load_all_tasks() → TASK-A + TASK-B.
        """
        from fulcra_coord.cli import _write_task_and_views

        task_a = make_task(title="Task A", workstream="devops", agent="agent-a")
        task_b = make_task(title="Task B", workstream="infra", agent="agent-b")
        task_a = apply_transition(task_a, "active", by="agent-a")
        task_b = apply_transition(task_b, "active", by="agent-b")

        # Only TASK-A is in the local cache (incomplete local cache scenario)
        cache.write_cached_task(task_a)
        # TASK-B is intentionally absent from local cache

        # Remote index knows about both tasks
        remote_index = {
            "schema": "fulcra.coordination.index.v1",
            "active": [
                {"id": task_a["id"]},
                {"id": task_b["id"]},
            ],
            "recent_done": [],
        }

        uploaded_views: dict = {}

        def fake_download(path, *, backend=None, timeout=None):
            if path.endswith("/index.json"):
                return remote_index
            if path.endswith("/search-index.json"):
                return {"schema": "v1", "records": []}
            # Individual task downloads
            if task_b["id"] in path:
                return task_b
            if task_a["id"] in path:
                return task_a
            return None

        def fake_stat(path, *, backend=None, timeout=None):
            return None  # no pre-existing stat — avoids merge-check branch

        def fake_upload(data, path, *, backend=None, timeout=None):
            uploaded_views[path] = data
            return True

        with patch("fulcra_coord.cli.remote.download_json", side_effect=fake_download), \
             patch("fulcra_coord.cli.remote.stat", side_effect=fake_stat), \
             patch("fulcra_coord.cli.remote.upload_json", side_effect=fake_upload):
            _write_task_and_views(task_a, backend=["false"])

        # Find the uploaded index view
        index_path = next(
            (p for p in uploaded_views if p.endswith("/index.json")), None
        )
        self.assertIsNotNone(index_path, "An index view must have been uploaded")
        uploaded_index = uploaded_views[index_path]

        active_ids = [t["id"] for t in uploaded_index.get("active", [])]
        self.assertIn(task_a["id"], active_ids,
                      "TASK-A (locally cached) must appear in uploaded index view")
        self.assertIn(task_b["id"], active_ids,
                      "TASK-B (remote-only) must appear in uploaded index view — "
                      "incomplete local cache must not truncate views")

    def test_views_include_locally_cached_new_task_not_yet_in_remote_index(self):
        """A brand-new task (not yet in the remote index) must still appear in views.

        Scenario: cmd_start creates TASK-NEW and calls _write_task_and_views.
        The remote index has TASK-OLD from a previous write. TASK-NEW is in
        local cache (written by cmd_start before calling _write_task_and_views)
        but is absent from the remote index (index hasn't been updated yet).
        Both tasks must appear in the uploaded views.
        """
        from fulcra_coord.cli import _write_task_and_views

        task_old = make_task(title="Old Task", workstream="devops", agent="agent-a")
        task_old = apply_transition(task_old, "active", by="agent-a")
        task_new = make_task(title="New Task", workstream="infra", agent="agent-b")

        # Both tasks in local cache (task_new just written by cmd_start)
        cache.write_cached_task(task_old)
        cache.write_cached_task(task_new)

        # Remote index only knows about TASK-OLD
        remote_index = {
            "schema": "fulcra.coordination.index.v1",
            "active": [{"id": task_old["id"]}],
            "recent_done": [],
        }

        uploaded_views: dict = {}

        def fake_download(path, *, backend=None, timeout=None):
            if path.endswith("/index.json"):
                return remote_index
            if path.endswith("/search-index.json"):
                return {"schema": "v1", "records": []}
            if task_old["id"] in path:
                return task_old
            return None

        def fake_stat(path, *, backend=None, timeout=None):
            return None

        def fake_upload(data, path, *, backend=None, timeout=None):
            uploaded_views[path] = data
            return True

        with patch("fulcra_coord.cli.remote.download_json", side_effect=fake_download), \
             patch("fulcra_coord.cli.remote.stat", side_effect=fake_stat), \
             patch("fulcra_coord.cli.remote.upload_json", side_effect=fake_upload):
            _write_task_and_views(task_new, backend=["false"])

        index_path = next(
            (p for p in uploaded_views if p.endswith("/index.json")), None
        )
        self.assertIsNotNone(index_path, "An index view must have been uploaded")
        uploaded_index = uploaded_views[index_path]

        # TASK-OLD is active → should appear in uploaded_index["active"]
        active_ids = [t["id"] for t in uploaded_index.get("active", [])]
        self.assertIn(task_old["id"], active_ids,
                      "TASK-OLD (active, from remote index) must appear in index active list")

        # TASK-NEW is proposed → it appears only in counts, not the active list.
        # Verify the count reflects both tasks (1 active + 1 proposed = 2 non-terminal).
        counts = uploaded_index.get("counts", {}).get("by_status", {})
        self.assertEqual(counts.get("active", 0), 1,
                         "counts.by_status.active must be 1 (TASK-OLD)")
        self.assertEqual(counts.get("proposed", 0), 1,
                         "counts.by_status.proposed must be 1 (TASK-NEW from local cache)")


# ---------------------------------------------------------------------------
# Claude Code auto-integration
# ---------------------------------------------------------------------------

class TestSessionLink(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = patch.dict(os.environ, {"XDG_CACHE_HOME": self.tmp}, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_write_then_read_roundtrip(self):
        from fulcra_coord import session_link
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sess-1"}):
            session_link.write_pointer("TASK-abc", agent="claude-code:h:r", root="/coordination")
            ptr = session_link.read_pointer("sess-1")
        self.assertEqual(ptr["task_id"], "TASK-abc")
        self.assertEqual(ptr["agent"], "claude-code:h:r")
        self.assertEqual(ptr["root"], "/coordination")

    def test_no_session_id_writes_nothing(self):
        from fulcra_coord import session_link
        with patch.dict(os.environ, {}, clear=True):
            os.environ["XDG_CACHE_HOME"] = self.tmp
            wrote = session_link.write_pointer("TASK-x", agent="a", root="/r")
        self.assertFalse(wrote)

    def test_read_missing_returns_none(self):
        from fulcra_coord import session_link
        self.assertIsNone(session_link.read_pointer("nope"))

    def test_generic_session_key_fallback(self):
        # OpenClaw handlers know the stable sessionKey, not CLAUDE_CODE_SESSION_ID.
        # The generic FULCRA_COORD_SESSION_KEY env var lets them stamp the pointer.
        from fulcra_coord import session_link
        with patch.dict(os.environ, {}, clear=True):
            os.environ["XDG_CACHE_HOME"] = self.tmp
            os.environ["FULCRA_COORD_SESSION_KEY"] = "agent:fulcra-agent:main"
            wrote = session_link.write_pointer(
                "TASK-oc", agent="openclaw:host:fulcra-agent", root="/coordination")
            self.assertTrue(wrote)
            ptr = session_link.read_pointer("agent:fulcra-agent:main")
        self.assertEqual(ptr["task_id"], "TASK-oc")
        self.assertEqual(ptr["agent"], "openclaw:host:fulcra-agent")

    def test_claude_session_id_takes_precedence(self):
        # When both are set, CLAUDE_CODE_SESSION_ID wins (Claude Code is the
        # native, finer-grained identifier; the generic key is a fallback only).
        from fulcra_coord import session_link
        with patch.dict(os.environ, {}, clear=True):
            os.environ["XDG_CACHE_HOME"] = self.tmp
            os.environ["CLAUDE_CODE_SESSION_ID"] = "cc-sess"
            os.environ["FULCRA_COORD_SESSION_KEY"] = "oc-key"
            session_link.write_pointer("TASK-p", agent="a", root="/r")
        self.assertIsNotNone(session_link.read_pointer("cc-sess"))
        self.assertIsNone(session_link.read_pointer("oc-key"))


class TestWriteStampsSessionPointer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        patch.dict(os.environ, {"XDG_CACHE_HOME": self.tmp}).start()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        patch.stopall()

    def test_active_write_stamps_pointer(self):
        from fulcra_coord import cli as climod, session_link, remote
        task = {"id": "TASK-z", "status": "active", "owner_agent": "claude-code:h:r"}
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sess-9"}), \
             patch.object(climod, "_write_task_and_views_core", return_value=True, create=True), \
             patch.object(remote, "stat", return_value=None), \
             patch.object(remote, "upload_json", return_value=True), \
             patch.object(remote, "download_json", return_value=None):
            climod._stamp_session_pointer(task)
        ptr = session_link.read_pointer("sess-9")
        self.assertEqual(ptr["task_id"], "TASK-z")

    def test_terminal_write_does_not_stamp(self):
        from fulcra_coord import cli as climod, session_link
        task = {"id": "TASK-done", "status": "done", "owner_agent": "a"}
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sess-10"}):
            climod._stamp_session_pointer(task)
        self.assertIsNone(session_link.read_pointer("sess-10"))

    def test_terminal_transition_clears_existing_pointer(self):
        # I4 regression: a task going terminal must clear its session pointer, so
        # PreCompact/SessionEnd hooks don't later checkpoint a finished task.
        from fulcra_coord import cli as climod, session_link
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "sess-11"}):
            climod._stamp_session_pointer(
                {"id": "TASK-x", "status": "active", "owner_agent": "a"})
            self.assertEqual(session_link.read_pointer("sess-11")["task_id"], "TASK-x")
            # same task transitions to done -> pointer cleared
            climod._stamp_session_pointer(
                {"id": "TASK-x", "status": "done", "owner_agent": "a"})
            self.assertIsNone(session_link.read_pointer("sess-11"))

    def test_clear_for_task_only_removes_matching(self):
        from fulcra_coord import session_link
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "s-keep"}):
            session_link.write_pointer("TASK-keep", agent="a", root="/r")
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "s-drop"}):
            session_link.write_pointer("TASK-drop", agent="a", root="/r")
        removed = session_link.clear_for_task("TASK-drop")
        self.assertEqual(removed, 1)
        self.assertIsNone(session_link.read_pointer("s-drop"))
        self.assertEqual(session_link.read_pointer("s-keep")["task_id"], "TASK-keep")


class TestHookTemplates(unittest.TestCase):
    def test_templates_present_and_failsafe(self):
        from fulcra_coord import claude_code as cc
        for name in ("SESSION_START_SH", "PRE_COMPACT_SH", "SESSION_END_SH"):
            body = getattr(cc, name)
            self.assertTrue(body.startswith("#!/usr/bin/env bash"))
            self.assertIn("exit 0", body)  # always exits clean
        self.assertIn("status", cc.SESSION_START_SH)
        self.assertIn("update", cc.PRE_COMPACT_SH)
        self.assertIn("pause", cc.SESSION_END_SH)


class TestSessionTaskCmd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        patch.dict(os.environ, {"XDG_CACHE_HOME": self.tmp}).start()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True); patch.stopall()

    def test_prints_task_id_for_known_session(self):
        from fulcra_coord import cli as climod, session_link
        from types import SimpleNamespace
        import io
        from contextlib import redirect_stdout
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "s1"}):
            session_link.write_pointer("TASK-q", agent="a", root="/r")
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = climod.cmd_session_task(SimpleNamespace(session_id="s1"))
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().strip(), "TASK-q")

    def test_unknown_session_prints_nothing_rc1(self):
        from fulcra_coord import cli as climod
        from types import SimpleNamespace
        rc = climod.cmd_session_task(SimpleNamespace(session_id="nope"))
        self.assertEqual(rc, 1)


class TestInstallClaudeCode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home"); os.makedirs(self.home)
        patch.dict(os.environ, {"HOME": self.home}).start()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True); patch.stopall()

    def _settings(self):
        import json
        p = os.path.join(self.home, ".claude", "settings.json")
        return json.load(open(p)) if os.path.exists(p) else None

    def test_install_writes_three_hooks_and_scripts(self):
        from fulcra_coord import claude_code as cc
        cc.install_claude_code(scope="global")
        s = self._settings()
        self.assertIn("SessionStart", s["hooks"])
        self.assertIn("PreCompact", s["hooks"])
        self.assertIn("SessionEnd", s["hooks"])
        hooks_dir = os.path.join(self.home, ".claude", "fulcra-coord-hooks")
        for f in ("session-start.sh", "pre-compact.sh", "session-end.sh"):
            self.assertTrue(os.access(os.path.join(hooks_dir, f), os.X_OK))

    def test_install_is_idempotent(self):
        from fulcra_coord import claude_code as cc
        cc.install_claude_code(scope="global")
        cc.install_claude_code(scope="global")
        s = self._settings()
        self.assertEqual(len(s["hooks"]["SessionStart"]), 1)

    def test_install_preserves_existing_unrelated_hook(self):
        import json
        os.makedirs(os.path.join(self.home, ".claude"))
        pre = {"hooks": {"SessionStart": [{"matcher": "*", "hooks": [
            {"type": "command", "command": "/usr/bin/true"}]}]}}
        json.dump(pre, open(os.path.join(self.home, ".claude", "settings.json"), "w"))
        from fulcra_coord import claude_code as cc
        cc.install_claude_code(scope="global")
        s = self._settings()
        cmds = [h["command"] for e in s["hooks"]["SessionStart"] for h in e["hooks"]]
        self.assertIn("/usr/bin/true", cmds)
        self.assertEqual(len(cmds), 2)

    def test_uninstall_removes_only_managed(self):
        import json
        os.makedirs(os.path.join(self.home, ".claude"))
        pre = {"hooks": {"SessionStart": [{"matcher": "*", "hooks": [
            {"type": "command", "command": "/usr/bin/true"}]}]}}
        json.dump(pre, open(os.path.join(self.home, ".claude", "settings.json"), "w"))
        from fulcra_coord import claude_code as cc
        cc.install_claude_code(scope="global")
        cc.install_claude_code(scope="global", uninstall=True)
        s = self._settings()
        cmds = [h["command"] for e in s["hooks"].get("SessionStart", []) for h in e["hooks"]]
        self.assertEqual(cmds, ["/usr/bin/true"])

    def test_dry_run_writes_nothing(self):
        from fulcra_coord import claude_code as cc
        cc.install_claude_code(scope="global", dry_run=True)
        self.assertIsNone(self._settings())

    def test_dry_run_would_write_includes_managed_and_preserves_unrelated(self):
        import json
        os.makedirs(os.path.join(self.home, ".claude"))
        pre = {"model": "x", "hooks": {"SessionStart": [{"matcher": "*", "hooks": [
            {"type": "command", "command": "/usr/bin/true"}]}]}}
        json.dump(pre, open(os.path.join(self.home, ".claude", "settings.json"), "w"))
        from fulcra_coord import claude_code as cc
        plan = cc.install_claude_code(scope="global", dry_run=True)
        ww = plan["would_write"]
        self.assertEqual(ww.get("model"), "x")  # unrelated key preserved
        cmds = [h["command"] for e in ww["hooks"]["SessionStart"] for h in e["hooks"]]
        self.assertIn("/usr/bin/true", cmds)  # unrelated hook preserved
        self.assertTrue(any("fulcra-coord-hooks" in c for c in cmds))  # managed added
        self.assertIn("PreCompact", ww["hooks"])
        self.assertIn("SessionEnd", ww["hooks"])
        # dry-run must not touch disk: on-disk settings stay unmodified (no managed hooks)
        on_disk = self._settings()
        disk_cmds = [h["command"] for e in on_disk["hooks"]["SessionStart"] for h in e["hooks"]]
        self.assertEqual(disk_cmds, ["/usr/bin/true"])
        self.assertNotIn("PreCompact", on_disk["hooks"])


    def test_install_handles_malformed_hooks_structure(self):
        # I-1: valid JSON but structurally wrong (hooks is a list, or an event
        # maps to a dict instead of a list of entries) must not raise and must
        # still produce a working install with the three managed events.
        import json
        from fulcra_coord import claude_code as cc
        for bad in ({"hooks": ["weird"]},
                    {"hooks": {"SessionStart": {"oops": 1}}}):
            os.makedirs(os.path.join(self.home, ".claude"), exist_ok=True)
            sp = os.path.join(self.home, ".claude", "settings.json")
            json.dump(bad, open(sp, "w"))
            cc.install_claude_code(scope="global")  # must not raise
            s = self._settings()
            for ev in ("SessionStart", "PreCompact", "SessionEnd"):
                self.assertIn(ev, s["hooks"])
                cmds = [h["command"] for e in s["hooks"][ev] for h in e["hooks"]]
                self.assertTrue(any("fulcra-coord-hooks" in c for c in cmds))
            os.remove(sp)

    def test_install_backs_up_unparseable_settings(self):
        # M-2: invalid JSON must be backed up to settings.json.bak (original
        # bytes preserved) before being overwritten with the managed config.
        from fulcra_coord import claude_code as cc
        os.makedirs(os.path.join(self.home, ".claude"), exist_ok=True)
        sp = os.path.join(self.home, ".claude", "settings.json")
        original = "{not valid json"
        with open(sp, "w") as f:
            f.write(original)
        cc.install_claude_code(scope="global")
        bak = sp + ".bak"
        self.assertTrue(os.path.exists(bak))
        self.assertEqual(open(bak).read(), original)
        s = self._settings()
        self.assertIn("SessionStart", s["hooks"])
        cmds = [h["command"] for e in s["hooks"]["SessionStart"] for h in e["hooks"]]
        self.assertTrue(any("fulcra-coord-hooks" in c for c in cmds))

    def test_project_install_is_self_contained(self):
        # M-1: a project-scoped install must materialize its scripts under the
        # project's own ./.claude/fulcra-coord-hooks/ and the settings.json hook
        # commands must point there, not into the operator's HOME.
        from fulcra_coord import claude_code as cc
        import json
        proj = os.path.join(self.tmp, "proj"); os.makedirs(proj)
        cwd = os.getcwd()
        try:
            os.chdir(proj)
            cc.install_claude_code(scope="project")
            hooks_dir = os.path.join(proj, ".claude", "fulcra-coord-hooks")
            for f in ("session-start.sh", "pre-compact.sh", "session-end.sh"):
                self.assertTrue(os.access(os.path.join(hooks_dir, f), os.X_OK))
            s = json.load(open(os.path.join(proj, ".claude", "settings.json")))
            cmds = [h["command"] for ev in s["hooks"].values()
                    for e in ev for h in e["hooks"]]
            self.assertTrue(cmds)
            for c in cmds:
                self.assertIn(hooks_dir, c)
                self.assertNotIn(self.home, c)
        finally:
            os.chdir(cwd)


class TestInstallClaudeCodeCmd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); self.home = os.path.join(self.tmp, "h"); os.makedirs(self.home)
        patch.dict(os.environ, {"HOME": self.home}).start()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True); patch.stopall()

    def test_cmd_installs_global(self):
        from fulcra_coord import cli as climod
        from types import SimpleNamespace
        rc = climod.cmd_install_claude_code(
            SimpleNamespace(scope="global", uninstall=False, dry_run=False))
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(os.path.join(self.home, ".claude", "settings.json")))


class TestHookParity(unittest.TestCase):
    def test_committed_scripts_match_templates(self):
        from fulcra_coord import claude_code as cc
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1] / "adapters" / "claude-code" / "hooks"
        mapping = {"session-start.sh": cc.SESSION_START_SH,
                   "pre-compact.sh": cc.PRE_COMPACT_SH,
                   "session-end.sh": cc.SESSION_END_SH}
        for fname, body in mapping.items():
            self.assertEqual((root / fname).read_text(), body,
                             f"{fname} drifted from template; regenerate it")


class TestHookScriptsE2E(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bin = os.path.join(self.tmp, "bin"); os.makedirs(self.bin)
        self.calls = os.path.join(self.tmp, "calls.log")
        # fake fulcra-coord: status returns canned JSON; other subcommands log args
        fake = os.path.join(self.bin, "fulcra-coord")
        with open(fake, "w") as f:
            f.write("#!/usr/bin/env bash\n"
                    'if [ "$1" = "status" ]; then cat "%s"; exit 0; fi\n'
                    'if [ "$1" = "__session-task" ]; then echo "TASK-live"; exit 0; fi\n'
                    'echo "$@" >> "%s"\n' % (os.path.join(self.tmp, "status.json"), self.calls))
        os.chmod(fake, 0o755)
        # Materialize the committed templates into a temp dir with the Gap-1
        # argv placeholder substituted by the fake CLI's absolute path (as a bash
        # array body) — this is the form that actually lands on disk at install
        # time. Running the raw committed copies (which carry the literal
        # __FULCRA_COORD_ARGV__ placeholder) would invoke a nonexistent command.
        from fulcra_coord.cli_invocation import PLACEHOLDER_ARGV, materialize_argv
        committed = os.path.join(os.path.dirname(__file__), "..", "adapters",
                                 "claude-code", "hooks")
        self.hooks = os.path.join(self.tmp, "hooks"); os.makedirs(self.hooks)
        for fname in ("session-start.sh", "pre-compact.sh", "session-end.sh"):
            body = open(os.path.join(committed, fname)).read()
            out = os.path.join(self.hooks, fname)
            with open(out, "w") as f:
                f.write(body.replace(PLACEHOLDER_ARGV, materialize_argv([fake])))
            os.chmod(out, 0o755)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, script, stdin, statusjson="{}"):
        with open(os.path.join(self.tmp, "status.json"), "w") as f:
            f.write(statusjson)
        env = dict(os.environ); env["PATH"] = self.bin + os.pathsep + env["PATH"]
        return subprocess.run(["bash", os.path.join(self.hooks, script)],
                              input=stdin, capture_output=True, text=True, env=env)

    def test_session_start_emits_context_for_my_active_task(self):
        sj = json.dumps({"active": [
            {"id": "TASK-live", "title": "Deploy", "status": "active",
             "owner_agent": "claude-code:%s:t" % os.uname().nodename.split('.')[0],
             "updated_at": "2026-06-01T00:00:00Z", "next_action": "do X"}]})
        r = self._run("session-start.sh", json.dumps({"cwd": self.tmp}), sj)
        self.assertEqual(r.returncode, 0)
        self.assertIn("TASK-live", r.stdout)
        self.assertIn("additionalContext", r.stdout)

    def test_session_start_silent_on_clean_bus(self):
        r = self._run("session-start.sh", json.dumps({"cwd": self.tmp}),
                      json.dumps({"active": []}))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")

    def test_pre_compact_calls_update(self):
        r = self._run("pre-compact.sh", json.dumps({"session_id": "s", "transcript_path": "/t.json"}))
        self.assertEqual(r.returncode, 0)
        self.assertIn("update TASK-live", open(self.calls).read())

    def test_session_start_flags_active_task_with_missing_timestamp(self):
        # M-4: an active task NOT owned by this agent whose updated_at is missing
        # must be surfaced as possibly-forgotten. A missing timestamp used to be
        # treated as age 0 (fresh) and silently dropped; now age is +inf so it
        # is reliably stale regardless of FULCRA_COORD_STALE_HOURS.
        sj = json.dumps({"active": [
            {"id": "TASK-orphan", "title": "Stranded", "status": "active",
             "owner_agent": "claude-code:other-host:other-repo"}]})
        r = self._run("session-start.sh", json.dumps({"cwd": self.tmp}), sj)
        self.assertEqual(r.returncode, 0)
        self.assertIn("TASK-orphan", r.stdout)
        self.assertIn("Possibly-forgotten", r.stdout)

    def test_session_end_pauses_active_task(self):
        sj = json.dumps({"active": [{"id": "TASK-live", "status": "active"}]})
        r = self._run("session-end.sh", json.dumps({"session_id": "s"}), sj)
        self.assertEqual(r.returncode, 0)
        self.assertIn("pause TASK-live", open(self.calls).read())

    def test_session_end_noop_when_not_active(self):
        sj = json.dumps({"active": [{"id": "TASK-live", "status": "waiting"}]})
        r = self._run("session-end.sh", json.dumps({"session_id": "s"}), sj)
        self.assertEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(self.calls) and "pause" in open(self.calls).read())


class TestSessionStartBlockedOnYouBanner(unittest.TestCase):
    """SessionStart leads with a ⛔ BLOCKED ON YOU section (from needs-me) before
    the in-flight / directives / stale sections; silent when needs-me is empty."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bin = os.path.join(self.tmp, "bin"); os.makedirs(self.bin)
        # Fake CLI: status -> canned; needs-me -> canned; inbox -> empty; others log.
        fake = os.path.join(self.bin, "fulcra-coord")
        with open(fake, "w") as f:
            f.write("#!/usr/bin/env bash\n"
                    'if [ "$1" = "status" ]; then cat "%s"; exit 0; fi\n'
                    'if [ "$1" = "needs-me" ]; then cat "%s"; exit 0; fi\n'
                    'if [ "$1" = "inbox" ]; then echo "{\\"inbox\\": []}"; exit 0; fi\n'
                    'exit 0\n'
                    % (os.path.join(self.tmp, "status.json"),
                       os.path.join(self.tmp, "needsme.json")))
        os.chmod(fake, 0o755)
        from fulcra_coord.cli_invocation import PLACEHOLDER_ARGV, materialize_argv
        from fulcra_coord import claude_code as cc
        self.hooks = os.path.join(self.tmp, "hooks"); os.makedirs(self.hooks)
        out = os.path.join(self.hooks, "session-start.sh")
        with open(out, "w") as f:
            f.write(cc.SESSION_START_SH.replace(
                PLACEHOLDER_ARGV, materialize_argv([fake])))
        os.chmod(out, 0o755)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, statusjson="{}", needsme="{}"):
        with open(os.path.join(self.tmp, "status.json"), "w") as f:
            f.write(statusjson)
        with open(os.path.join(self.tmp, "needsme.json"), "w") as f:
            f.write(needsme)
        env = dict(os.environ); env["PATH"] = self.bin + os.pathsep + env["PATH"]
        return subprocess.run(["bash", os.path.join(self.hooks, "session-start.sh")],
                              input=json.dumps({"cwd": self.tmp}),
                              capture_output=True, text=True, env=env)

    def test_blocked_on_you_section_appears_first(self):
        needsme = json.dumps({"human": "ash", "count": 1, "items": [
            {"id": "TASK-x", "title": "approve deploy", "status": "blocked",
             "owner_agent": "claude-code:h:vercel", "blocked_on": "approve the deploy",
             "next_action": "", "updated_at": "2026-06-01T00:00:00Z"}]})
        r = self._run(json.dumps({"active": []}), needsme)
        self.assertEqual(r.returncode, 0)
        self.assertIn("BLOCKED ON YOU", r.stdout)
        self.assertIn("approve the deploy", r.stdout)
        self.assertIn("claude-code:h:vercel", r.stdout)
        # The blocked-on-you banner leads the injected context (it is the very
        # first line — no bus work in this case, so it stands alone at the top).
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertTrue(ctx.startswith("⛔ BLOCKED ON YOU"))

    def test_silent_when_nothing_blocked_and_clean(self):
        r = self._run(json.dumps({"active": []}),
                      json.dumps({"human": "ash", "count": 0, "items": []}))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")

    def test_blocked_on_you_with_inflight_both_present(self):
        host = os.uname().nodename.split(".")[0]
        sj = json.dumps({"active": [
            {"id": "TASK-mine", "title": "my work", "status": "active",
             "owner_agent": "claude-code:%s:%s" % (host, os.path.basename(self.tmp)),
             "updated_at": "2026-06-01T00:00:00Z", "next_action": "do X"}]})
        needsme = json.dumps({"human": "ash", "count": 1, "items": [
            {"id": "TASK-x", "title": "approve deploy", "status": "blocked",
             "owner_agent": "claude-code:h:vercel", "blocked_on": "approve it",
             "next_action": "", "updated_at": "2026-06-01T00:00:00Z"}]})
        r = self._run(sj, needsme)
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("BLOCKED ON YOU", ctx)
        self.assertIn("TASK-mine", ctx)
        self.assertLess(ctx.index("BLOCKED ON YOU"), ctx.index("TASK-mine"))


class TestSessionStartAgentResolution(unittest.TestCase):
    """I-2: the banner resolves AGENT through the CLI (`identity --format json`)
    so the "mine" filter, title, and resume hint agree with inbox/needs-me. A
    declared per-cwd identity (not the shell-derived claude-code:<host>:<repo>)
    must drive which tasks count as "mine"."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bin = os.path.join(self.tmp, "bin"); os.makedirs(self.bin)
        # Fake CLI: identity -> a DECLARED id different from the derived shape;
        # status -> canned; needs-me/inbox -> empty.
        fake = os.path.join(self.bin, "fulcra-coord")
        with open(fake, "w") as f:
            f.write("#!/usr/bin/env bash\n"
                    'if [ "$1" = "identity" ]; then echo "{\\"agent\\": \\"declared:custom:id\\"}"; exit 0; fi\n'
                    'if [ "$1" = "status" ]; then cat "%s"; exit 0; fi\n'
                    'if [ "$1" = "needs-me" ]; then echo "{\\"items\\": []}"; exit 0; fi\n'
                    'if [ "$1" = "inbox" ]; then echo "{\\"inbox\\": []}"; exit 0; fi\n'
                    'exit 0\n' % (os.path.join(self.tmp, "status.json"),))
        os.chmod(fake, 0o755)
        from fulcra_coord.cli_invocation import PLACEHOLDER_ARGV, materialize_argv
        from fulcra_coord import claude_code as cc
        self.hooks = os.path.join(self.tmp, "hooks"); os.makedirs(self.hooks)
        out = os.path.join(self.hooks, "session-start.sh")
        with open(out, "w") as f:
            f.write(cc.SESSION_START_SH.replace(
                PLACEHOLDER_ARGV, materialize_argv([fake])))
        os.chmod(out, 0o755)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, statusjson):
        with open(os.path.join(self.tmp, "status.json"), "w") as f:
            f.write(statusjson)
        env = dict(os.environ); env["PATH"] = self.bin + os.pathsep + env["PATH"]
        return subprocess.run(["bash", os.path.join(self.hooks, "session-start.sh")],
                              input=json.dumps({"cwd": self.tmp}),
                              capture_output=True, text=True, env=env)

    def test_declared_identity_drives_mine_filter(self):
        # A task owned by the DECLARED id is "mine"; the shell-derived id is not
        # used. The resume hint also carries the declared id.
        sj = json.dumps({"active": [
            {"id": "TASK-declared", "title": "my declared work", "status": "active",
             "owner_agent": "declared:custom:id",
             "updated_at": "2026-06-01T00:00:00Z", "next_action": "do X"}]})
        r = self._run(sj)
        self.assertEqual(r.returncode, 0)
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("TASK-declared", ctx)
        self.assertIn("--agent declared:custom:id", ctx)

    def test_derived_owner_not_mine_under_declared_identity(self):
        # A task owned by the shell-derived id is NOT mine once a different id is
        # declared (it can only surface via the stale path, not the mine path).
        host = os.uname().nodename.split(".")[0]
        derived = "claude-code:%s:%s" % (host, os.path.basename(self.tmp))
        sj = json.dumps({"active": [
            {"id": "TASK-derived", "title": "derived work", "status": "active",
             "owner_agent": derived,
             "updated_at": "2026-06-01T00:00:00Z", "next_action": "do Y"}]})
        r = self._run(sj)
        self.assertEqual(r.returncode, 0)
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        # Title (first active task owned by AGENT) must be empty -> no sessionTitle
        # since the derived-owned task is not owned by the declared AGENT.
        self.assertNotIn("sessionTitle", r.stdout)


class TestSessionStartSelfBlockedDedup(unittest.TestCase):
    """M-1: a self-filed `block --on-user` task is owned by the agent AND appears
    in needs-me. It must show ONCE (in the ⛔ BLOCKED ON YOU banner), not again
    under the "open work" mine/possibly-forgotten sections."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bin = os.path.join(self.tmp, "bin"); os.makedirs(self.bin)
        self.host = os.uname().nodename.split(".")[0]
        self.repo = os.path.basename(self.tmp)
        self.agent = "claude-code:%s:%s" % (self.host, self.repo)
        fake = os.path.join(self.bin, "fulcra-coord")
        with open(fake, "w") as f:
            # identity -> empty (exercise the derived fallback so AGENT == the
            # owner_agent of the self-filed task below).
            f.write("#!/usr/bin/env bash\n"
                    'if [ "$1" = "identity" ]; then exit 0; fi\n'
                    'if [ "$1" = "status" ]; then cat "%s"; exit 0; fi\n'
                    'if [ "$1" = "needs-me" ]; then cat "%s"; exit 0; fi\n'
                    'if [ "$1" = "inbox" ]; then echo "{\\"inbox\\": []}"; exit 0; fi\n'
                    'exit 0\n'
                    % (os.path.join(self.tmp, "status.json"),
                       os.path.join(self.tmp, "needsme.json")))
        os.chmod(fake, 0o755)
        from fulcra_coord.cli_invocation import PLACEHOLDER_ARGV, materialize_argv
        from fulcra_coord import claude_code as cc
        self.hooks = os.path.join(self.tmp, "hooks"); os.makedirs(self.hooks)
        out = os.path.join(self.hooks, "session-start.sh")
        with open(out, "w") as f:
            f.write(cc.SESSION_START_SH.replace(
                PLACEHOLDER_ARGV, materialize_argv([fake])))
        os.chmod(out, 0o755)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, statusjson, needsme):
        with open(os.path.join(self.tmp, "status.json"), "w") as f:
            f.write(statusjson)
        with open(os.path.join(self.tmp, "needsme.json"), "w") as f:
            f.write(needsme)
        env = dict(os.environ); env["PATH"] = self.bin + os.pathsep + env["PATH"]
        return subprocess.run(["bash", os.path.join(self.hooks, "session-start.sh")],
                              input=json.dumps({"cwd": self.tmp}),
                              capture_output=True, text=True, env=env)

    def test_self_filed_on_user_task_shows_once(self):
        # The task is owned by THIS agent (so it would land in "mine") AND is in
        # needs-me (the blocked-on-you banner). It must appear exactly once.
        sj = json.dumps({"active": [
            {"id": "TASK-self", "title": "self-blocked", "status": "blocked",
             "owner_agent": self.agent, "updated_at": "2026-06-01T00:00:00Z",
             "next_action": "wait on human"}]})
        needsme = json.dumps({"human": "ash", "count": 1, "items": [
            {"id": "TASK-self", "title": "self-blocked", "status": "blocked",
             "owner_agent": self.agent, "blocked_on": "need a decision",
             "next_action": "", "updated_at": "2026-06-01T00:00:00Z"}]})
        r = self._run(sj, needsme)
        self.assertEqual(r.returncode, 0)
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(ctx.count("TASK-self"), 1)
        self.assertIn("BLOCKED ON YOU", ctx)
        # It must NOT spawn an empty "open work" header (no other bus content).
        self.assertNotIn("open work on the shared bus", ctx)


class TestOpenClawTemplates(unittest.TestCase):
    def test_prompt_and_hook_templates_present(self):
        from fulcra_coord import openclaw as oc
        # Prompt files mention the CLI status surfacing.
        self.assertIn("fulcra-coord status", oc.BOOT_MD_BODY)
        self.assertIn("fulcra-coord status", oc.HEARTBEAT_MD_BODY)
        # Shutdown handler pauses; bootstrap handler injects via bootstrapFiles.
        self.assertIn("pause", oc.SHUTDOWN_HANDLER_TS)
        self.assertIn("bootstrapFiles", oc.BOOTSTRAP_HANDLER_TS)
        # HOOK.md frontmatter declares the right events + the bin requirement.
        self.assertIn("gateway:shutdown", oc.SHUTDOWN_HOOK_MD)
        self.assertIn("agent:bootstrap", oc.BOOTSTRAP_HOOK_MD)
        self.assertIn("fulcra-coord", oc.SHUTDOWN_HOOK_MD)

    def test_handlers_aligned_with_real_sdk(self):
        # The handlers are now corrected to the real OpenClaw automation-hook
        # API (no more guessed shapes). Assert the load-bearing facts hold:
        from fulcra_coord import openclaw as oc
        # Bootstrap reads the mutable array off event.context (NOT top-level),
        # and folds into the recognized MEMORY.md basename via inline content —
        # never the rejected "push an arbitrary temp path" shape.
        self.assertIn("event?.context", oc.BOOTSTRAP_HANDLER_TS)
        self.assertIn("ctx?.bootstrapFiles", oc.BOOTSTRAP_HANDLER_TS)
        self.assertIn("MEMORY.md", oc.BOOTSTRAP_HANDLER_TS)
        # Shutdown keys on the top-level event.sessionKey (correct per SDK).
        self.assertIn("event?.sessionKey", oc.SHUTDOWN_HANDLER_TS)
        # Both cite the source they were validated against (not a guess caveat).
        self.assertIn("workspace.ts", oc.BOOTSTRAP_HANDLER_TS)
        self.assertIn("hooks.md", oc.SHUTDOWN_HANDLER_TS)

    def test_handlers_are_failsafe(self):
        from fulcra_coord import openclaw as oc
        for ts in (oc.SHUTDOWN_HANDLER_TS, oc.BOOTSTRAP_HANDLER_TS):
            self.assertIn("catch", ts)  # swallows errors, never blocks


class TestInstallOpenClaw(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "openclaw-hooks")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _exists(self, *parts):
        return os.path.exists(os.path.join(self.root, *parts))

    def test_install_materializes_expected_files(self):
        from fulcra_coord import openclaw as oc
        oc.install_openclaw(hooks_root=self.root)
        self.assertTrue(self._exists("BOOT.md"))
        self.assertTrue(self._exists("HEARTBEAT.md"))
        self.assertTrue(self._exists("fulcra-coord-shutdown", "HOOK.md"))
        self.assertTrue(self._exists("fulcra-coord-shutdown", "handler.ts"))
        self.assertTrue(self._exists("fulcra-coord-bootstrap", "HOOK.md"))
        self.assertTrue(self._exists("fulcra-coord-bootstrap", "handler.ts"))
        self.assertTrue(self._exists("fulcra-coord-compact", "HOOK.md"))
        self.assertTrue(self._exists("fulcra-coord-compact", "handler.ts"))

    def test_compact_hook_declares_session_compact_before_event(self):
        # The compaction checkpoint hook must register against the file-based
        # session:compact:before event (the corrected fact: it IS file-based,
        # not Plugin-SDK-only) and always issue an `update` checkpoint.
        from fulcra_coord import openclaw as oc
        oc.install_openclaw(hooks_root=self.root)
        hook = open(os.path.join(self.root, "fulcra-coord-compact", "HOOK.md")).read()
        self.assertIn("session:compact:before", hook)
        handler = open(os.path.join(self.root, "fulcra-coord-compact", "handler.ts")).read()
        self.assertIn("compact:before", handler)
        self.assertIn("__session-task", handler)
        self.assertIn("update", handler)

    def test_boot_md_carries_marker_block(self):
        from fulcra_coord import openclaw as oc
        oc.install_openclaw(hooks_root=self.root)
        text = open(os.path.join(self.root, "BOOT.md")).read()
        self.assertIn(oc._BEGIN, text)
        self.assertIn(oc._END, text)
        self.assertIn("fulcra-coord status", text)

    def test_install_is_idempotent(self):
        from fulcra_coord import openclaw as oc
        oc.install_openclaw(hooks_root=self.root)
        oc.install_openclaw(hooks_root=self.root)
        text = open(os.path.join(self.root, "BOOT.md")).read()
        # Exactly one managed block, not two.
        self.assertEqual(text.count(oc._BEGIN), 1)
        self.assertEqual(text.count(oc._END), 1)

    def test_install_preserves_user_boot_content(self):
        from fulcra_coord import openclaw as oc
        os.makedirs(self.root)
        with open(os.path.join(self.root, "BOOT.md"), "w") as f:
            f.write("# My own boot prompt\nDo my custom thing.\n")
        oc.install_openclaw(hooks_root=self.root)
        text = open(os.path.join(self.root, "BOOT.md")).read()
        self.assertIn("My own boot prompt", text)  # user content kept
        self.assertIn(oc._BEGIN, text)             # our block appended

    def test_dry_run_writes_nothing_but_reports(self):
        from fulcra_coord import openclaw as oc
        plan = oc.install_openclaw(hooks_root=self.root, dry_run=True)
        self.assertFalse(os.path.exists(self.root))
        self.assertTrue(any("BOOT.md" in w for w in plan["writes"]))
        self.assertTrue(any("handler.ts" in w for w in plan["writes"]))
        self.assertTrue(any("fulcra-coord-shutdown" in d for d in plan["hook_dirs"]))
        self.assertTrue(any("fulcra-coord-compact" in d for d in plan["hook_dirs"]))

    def test_uninstall_is_surgical(self):
        from fulcra_coord import openclaw as oc
        # Pre-seed user content alongside what we'll install.
        os.makedirs(self.root)
        with open(os.path.join(self.root, "BOOT.md"), "w") as f:
            f.write("# My own boot prompt\nKeep me.\n")
        oc.install_openclaw(hooks_root=self.root)
        oc.install_openclaw(hooks_root=self.root, uninstall=True)
        # Hook dirs gone.
        self.assertFalse(self._exists("fulcra-coord-shutdown"))
        self.assertFalse(self._exists("fulcra-coord-bootstrap"))
        self.assertFalse(self._exists("fulcra-coord-compact"))
        # User's BOOT.md content survives; our block is stripped.
        text = open(os.path.join(self.root, "BOOT.md")).read()
        self.assertIn("Keep me.", text)
        self.assertNotIn(oc._BEGIN, text)

    def test_uninstall_deletes_husk_when_only_our_block(self):
        # If BOOT.md held ONLY our managed block, uninstall should remove the
        # file entirely rather than leaving an empty husk.
        from fulcra_coord import openclaw as oc
        oc.install_openclaw(hooks_root=self.root)
        oc.install_openclaw(hooks_root=self.root, uninstall=True)
        self.assertFalse(self._exists("BOOT.md"))
        self.assertFalse(self._exists("HEARTBEAT.md"))

    def test_env_override_hooks_root(self):
        from fulcra_coord import openclaw as oc
        with patch.dict(os.environ, {"FULCRA_OPENCLAW_HOOKS_ROOT": self.root}):
            oc.install_openclaw()
        self.assertTrue(self._exists("BOOT.md"))


class TestInstallOpenClawCmd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "ocroot")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cmd_installs(self):
        from fulcra_coord import cli as climod
        from types import SimpleNamespace
        rc = climod.cmd_install_openclaw(
            SimpleNamespace(hooks_root=self.root, uninstall=False, dry_run=False))
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(os.path.join(self.root, "BOOT.md")))

    def test_cmd_dry_run_writes_nothing(self):
        from fulcra_coord import cli as climod
        from types import SimpleNamespace
        rc = climod.cmd_install_openclaw(
            SimpleNamespace(hooks_root=self.root, uninstall=False, dry_run=True))
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.root))

    def test_cmd_uninstall(self):
        from fulcra_coord import cli as climod
        from types import SimpleNamespace
        climod.cmd_install_openclaw(
            SimpleNamespace(hooks_root=self.root, uninstall=False, dry_run=False))
        rc = climod.cmd_install_openclaw(
            SimpleNamespace(hooks_root=self.root, uninstall=True, dry_run=False))
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(os.path.join(self.root, "fulcra-coord-shutdown")))


class TestOpenClawPluginSource(unittest.TestCase):
    """The Track B plugin source tree is well-formed and on the real SDK API."""

    def _src_root(self):
        from fulcra_coord import openclaw_plugin as ocp
        return ocp._plugin_src_root()

    def test_plugin_source_tree_present(self):
        root = self._src_root()
        self.assertTrue(root.is_dir(), f"plugin src root missing: {root}")
        for rel in ("package.json", "openclaw.plugin.json", "tsconfig.json",
                    "README.md", "src/index.ts",
                    # Build-correctness files (arc live findings #e8096836/#f0e6511a):
                    # the ambient SDK shim and the `.npmrc`/`.npmignore` that omit
                    # the `openclaw` peer must all ship in the materialized tree.
                    "src/openclaw-sdk.d.ts", ".npmrc", ".npmignore"):
            self.assertTrue((root / rel).is_file(), f"missing plugin file: {rel}")

    def test_build_correctness_files_wellformed(self):
        """The two arc live defects are fixed at the source: @types/node is a
        devDependency (TS2688) and the peer install is suppressed (.npmrc
        omit=peer) with an ambient shim so tsc still resolves the openclaw
        subpath import."""
        import json
        root = self._src_root()
        pkg = json.loads((root / "package.json").read_text())
        # Defect 1: @types/node present so `types:["node"]` resolves.
        self.assertIn("@types/node", pkg.get("devDependencies", {}))
        # Defect 2a: `.npmrc` omits the auto-installed peer.
        self.assertIn("omit=peer", (root / ".npmrc").read_text())
        # Defect 2b: ambient shim declares exactly the imported subpath so tsc
        # compiles without the real `openclaw` peer in node_modules.
        shim = (root / "src" / "openclaw-sdk.d.ts").read_text()
        self.assertIn('declare module "openclaw/plugin-sdk/plugin-entry"', shim)
        self.assertIn("definePluginEntry", shim)
        # Defect 2c: node_modules backstop.
        self.assertIn("node_modules", (root / ".npmignore").read_text())

    def test_manifest_declares_plugin_id(self):
        import json
        root = self._src_root()
        manifest = json.loads((root / "openclaw.plugin.json").read_text())
        self.assertEqual(manifest["id"], "fulcra-coord")
        pkg = json.loads((root / "package.json").read_text())
        # The wheel ships the built entry under dist/ via the openclaw manifest.
        self.assertIn("openclaw", pkg)
        self.assertIn("extensions", pkg["openclaw"])

    def test_index_ts_registers_the_three_checkpoints(self):
        ts = (self._src_root() / "src" / "index.ts").read_text()
        # The three deterministic lifecycle hooks, by their real SDK names.
        self.assertIn('api.on("session_start"', ts)
        self.assertIn('api.on("before_compaction"', ts)
        self.assertIn('api.on("session_end"', ts)
        # before_compaction ALWAYS checkpoints via `update` (the Track A gap).
        self.assertIn("update", ts)
        # session_end parks via `pause`, and must skip `compaction` (continues).
        self.assertIn("pause", ts)
        self.assertNotIn('"compaction"', _park_reasons_line(ts))
        # Uses the FULCRA_COORD_SESSION_KEY pointer fallback Track A added.
        self.assertIn("FULCRA_COORD_SESSION_KEY", ts)
        # Entry point is the real definePluginEntry from the SDK subpath.
        self.assertIn("definePluginEntry", ts)
        self.assertIn("openclaw/plugin-sdk/plugin-entry", ts)


class TestInstallOpenClawPlugin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dir = os.path.join(self.tmp, "fulcra-coord-plugin")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _exists(self, *parts):
        return os.path.exists(os.path.join(self.dir, *parts))

    def test_materializes_full_tree(self):
        from fulcra_coord import openclaw_plugin as ocp
        ocp.install_openclaw_plugin(plugin_dir=self.dir)
        self.assertTrue(self._exists("package.json"))
        self.assertTrue(self._exists("openclaw.plugin.json"))
        self.assertTrue(self._exists("tsconfig.json"))
        self.assertTrue(self._exists("README.md"))
        self.assertTrue(self._exists("src", "index.ts"))
        # Build-correctness files materialize too (arc live findings).
        self.assertTrue(self._exists("src", "openclaw-sdk.d.ts"))
        self.assertTrue(self._exists(".npmrc"))
        self.assertTrue(self._exists(".npmignore"))

    def test_dry_run_writes_nothing_but_plans(self):
        from fulcra_coord import openclaw_plugin as ocp
        plan = ocp.install_openclaw_plugin(plugin_dir=self.dir, dry_run=True)
        self.assertFalse(os.path.exists(self.dir))
        self.assertTrue(any("index.ts" in w for w in plan["writes"]))
        # The build steps the CLI can't run are surfaced for the user.
        self.assertTrue(any("npm run build" in s for s in plan["build_steps"]))
        self.assertTrue(any("openclaw plugins install" in s for s in plan["build_steps"]))

    def test_idempotent_reinstall(self):
        from fulcra_coord import openclaw_plugin as ocp
        ocp.install_openclaw_plugin(plugin_dir=self.dir)
        ocp.install_openclaw_plugin(plugin_dir=self.dir)
        self.assertTrue(self._exists("src", "index.ts"))

    def test_uninstall_removes_tree(self):
        from fulcra_coord import openclaw_plugin as ocp
        ocp.install_openclaw_plugin(plugin_dir=self.dir)
        ocp.install_openclaw_plugin(plugin_dir=self.dir, uninstall=True)
        self.assertFalse(os.path.exists(self.dir))

    def test_env_override_plugin_dir(self):
        from fulcra_coord import openclaw_plugin as ocp
        with patch.dict(os.environ, {"FULCRA_OPENCLAW_PLUGIN_DIR": self.dir}):
            ocp.install_openclaw_plugin()
        self.assertTrue(self._exists("src", "index.ts"))


class TestInstallOpenClawWithPluginCmd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "ocroot")
        self.pdir = os.path.join(self.tmp, "ocplugin")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_with_plugin_materializes_alongside_track_a(self):
        from fulcra_coord import cli as climod
        from types import SimpleNamespace
        rc = climod.cmd_install_openclaw(SimpleNamespace(
            hooks_root=self.root, uninstall=False, dry_run=False,
            with_plugin=True, plugin_dir=self.pdir))
        self.assertEqual(rc, 0)
        # Track A artifacts AND the Track B plugin sources both landed.
        self.assertTrue(os.path.exists(os.path.join(self.root, "BOOT.md")))
        self.assertTrue(os.path.exists(os.path.join(self.pdir, "src", "index.ts")))

    def test_without_flag_leaves_plugin_untouched(self):
        from fulcra_coord import cli as climod
        from types import SimpleNamespace
        rc = climod.cmd_install_openclaw(SimpleNamespace(
            hooks_root=self.root, uninstall=False, dry_run=False,
            with_plugin=False, plugin_dir=self.pdir))
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.pdir))

    def test_with_plugin_dry_run_writes_nothing(self):
        from fulcra_coord import cli as climod
        from types import SimpleNamespace
        rc = climod.cmd_install_openclaw(SimpleNamespace(
            hooks_root=self.root, uninstall=False, dry_run=True,
            with_plugin=True, plugin_dir=self.pdir))
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.exists(self.root))
        self.assertFalse(os.path.exists(self.pdir))


def _park_reasons_line(ts: str) -> str:
    """Return the PARK_REASONS set literal so we can assert `compaction` is
    NOT among the reasons that park a task (compaction continues the session)."""
    for line in ts.splitlines():
        if "PARK_REASONS" in line and "new Set" in line:
            return line
    return ""


# ---------------------------------------------------------------------------
# Gap 1 — resolvable CLI command baked into materialized hooks
# ---------------------------------------------------------------------------

class TestResolveCliCommand(unittest.TestCase):
    """resolve_cli_command() prefers an on-PATH `fulcra-coord` absolute path,
    else falls back to `<python> -m fulcra_coord` (always importable)."""

    def test_prefers_which_when_on_path(self):
        from fulcra_coord import cli_invocation
        with patch("shutil.which", return_value="/opt/bin/fulcra-coord"):
            self.assertEqual(cli_invocation.resolve_cli_command(),
                             "/opt/bin/fulcra-coord")

    def test_falls_back_to_python_m_when_not_on_path(self):
        from fulcra_coord import cli_invocation
        with patch("shutil.which", return_value=None):
            cmd = cli_invocation.resolve_cli_command()
        self.assertIn("-m fulcra_coord", cmd)
        self.assertIn(sys.executable, cmd)

    def test_resolved_command_is_never_the_bare_name(self):
        # The whole point of Gap 1: never emit a bare `fulcra-coord` that a
        # uv-tool / source install would fail to find on PATH.
        from fulcra_coord import cli_invocation
        with patch("shutil.which", return_value=None):
            cmd = cli_invocation.resolve_cli_command()
        self.assertNotEqual(cmd.strip(), "fulcra-coord")


class TestClaudeHookCommandSubstitution(unittest.TestCase):
    """Templates carry the __FULCRA_COORD_ARGV__ array placeholder; materialized
    hooks on disk carry the resolved argv as a bash array, never the placeholder
    and never a word-splittable string (C1)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home"); os.makedirs(self.home)
        patch.dict(os.environ, {"HOME": self.home}).start()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True); patch.stopall()

    def test_templates_carry_array_placeholder_and_define_array(self):
        from fulcra_coord import claude_code as cc
        for body in (cc.SESSION_START_SH, cc.PRE_COMPACT_SH, cc.SESSION_END_SH):
            self.assertIn("__FULCRA_COORD_ARGV__", body)
            self.assertIn("FULCRA_COORD=(__FULCRA_COORD_ARGV__)", body)
            # The old word-splittable string form must be gone.
            self.assertNotIn('FULCRA_COORD="__', body)
        # No bare `fulcra-coord ` invocations remain (only the array expansion).
        for body in (cc.SESSION_START_SH, cc.PRE_COMPACT_SH, cc.SESSION_END_SH):
            for line in body.splitlines():
                if "fulcra-coord " in line and "FULCRA_COORD" not in line \
                        and not line.lstrip().startswith("#"):
                    self.fail(f"bare invocation in template: {line!r}")

    def test_materialized_hook_has_resolved_argv_array_not_placeholder(self):
        from fulcra_coord import claude_code as cc
        with patch("fulcra_coord.cli_invocation.resolve_cli_argv",
                   return_value=["/opt/bin/fulcra-coord"]):
            cc.install_claude_code(scope="global")
        hooks_dir = os.path.join(self.home, ".claude", "fulcra-coord-hooks")
        body = open(os.path.join(hooks_dir, "session-start.sh")).read()
        self.assertNotIn("__FULCRA_COORD_ARGV__", body)
        self.assertIn("FULCRA_COORD=(/opt/bin/fulcra-coord)", body)


class TestOpenClawCommandSubstitution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "openclaw-hooks")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_handlers_carry_argv_json_placeholder(self):
        from fulcra_coord import openclaw as oc
        for ts in (oc.SHUTDOWN_HANDLER_TS, oc.BOOTSTRAP_HANDLER_TS,
                   oc.COMPACT_HANDLER_TS):
            self.assertIn("__FULCRA_COORD_ARGV_JSON__", ts)
            # The old word-splittable string + .split() form must be gone (C1).
            self.assertNotIn(".split(/", ts)
            self.assertNotIn('"__FULCRA_COORD_CMD__"', ts)
        # No bare `fulcra-coord` string-literal invocations in the handlers
        # (the resolved argv flows through the JSON-array placeholder).
        for ts in (oc.SHUTDOWN_HANDLER_TS, oc.BOOTSTRAP_HANDLER_TS,
                   oc.COMPACT_HANDLER_TS):
            self.assertNotIn('"fulcra-coord"', ts)

    def test_materialized_handler_has_resolved_argv_json(self):
        from fulcra_coord import openclaw as oc
        with patch("fulcra_coord.cli_invocation.resolve_cli_argv",
                   return_value=["/opt/bin/fulcra-coord"]):
            oc.install_openclaw(hooks_root=self.root)
        body = open(os.path.join(self.root, oc.SHUTDOWN_DIRNAME,
                                 "handler.ts")).read()
        self.assertNotIn("__FULCRA_COORD_ARGV_JSON__", body)
        self.assertIn('["/opt/bin/fulcra-coord"]', body)


# ---------------------------------------------------------------------------
# Gap 2 — staleness in views/reconcile + needs-attention view + heartbeat
# ---------------------------------------------------------------------------

def _stale_task(hours_old: float, **overrides) -> dict:
    """An active task whose updated_at is `hours_old` hours in the past."""
    from datetime import datetime, timedelta, timezone
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours_old)) \
        .isoformat().replace("+00:00", "Z")
    t = _with_status(_sample_task(), "active")
    t["updated_at"] = ts
    t.update(overrides)
    return t


class TestStalenessFlag(unittest.TestCase):
    def test_old_active_task_flagged_stale(self):
        from fulcra_coord import views
        t = _stale_task(5)  # default threshold is 2h
        na = views.build_needs_attention([t], stale_hours=2)
        self.assertEqual(len(na["tasks"]), 1)
        self.assertTrue(na["tasks"][0].get("stale"))

    def test_fresh_active_task_not_flagged(self):
        from fulcra_coord import views
        t = _stale_task(0.1)
        na = views.build_needs_attention([t], stale_hours=2)
        self.assertEqual(na["tasks"], [])

    def test_missing_updated_at_treated_as_stale(self):
        # Consistent with the I4/M4 fix: an unparseable/missing timestamp on an
        # active task is the "lost its clock, possibly forgotten" case.
        from fulcra_coord import views
        t = _with_status(_sample_task(), "active")
        t["updated_at"] = ""
        na = views.build_needs_attention([t], stale_hours=2)
        self.assertEqual(len(na["tasks"]), 1)
        self.assertTrue(na["tasks"][0].get("stale"))

    def test_done_task_never_in_needs_attention(self):
        from fulcra_coord import views
        t = _stale_task(99)
        t["status"] = "done"
        na = views.build_needs_attention([t], stale_hours=2)
        self.assertEqual(na["tasks"], [])

    def test_active_view_summaries_carry_stale_flag(self):
        from fulcra_coord import views
        old, fresh = _stale_task(5), _stale_task(0.1)
        fresh["id"] = "TASK-fresh-xyz"
        av = views.build_active([old, fresh], stale_hours=2)
        by_id = {t["id"]: t for t in av["tasks"]}
        self.assertTrue(by_id[old["id"]].get("stale"))
        self.assertFalse(by_id["TASK-fresh-xyz"].get("stale"))

    def test_build_all_views_includes_needs_attention(self):
        from fulcra_coord import views
        out = views.build_all_views([_stale_task(5)])
        self.assertIn("needs-attention", out)


class TestReconcileStaleness(unittest.TestCase):
    """The reconcile path stamps staleness and materializes needs-attention."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp
        self.fake_backend = ["false"]

    def tearDown(self):
        os.environ.pop("XDG_CACHE_HOME", None)
        os.environ.pop("FULCRA_COORD_STALE_HOURS", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reconcile_writes_needs_attention_view(self):
        from fulcra_coord import cli as climod, cache
        cache.write_cached_task(_stale_task(9))
        rc = climod.cmd_reconcile(types.SimpleNamespace(),
                                  backend=self.fake_backend)
        self.assertEqual(rc, 1)  # fake backend upload fails, but views build
        na = cache.read_cached_view("needs-attention")
        self.assertIsNotNone(na)
        self.assertEqual(len(na["tasks"]), 1)
        self.assertTrue(na["tasks"][0].get("stale"))

    def test_reconcile_respects_stale_hours_env(self):
        from fulcra_coord import cli as climod, cache
        os.environ["FULCRA_COORD_STALE_HOURS"] = "24"
        cache.write_cached_task(_stale_task(9))  # 9h < 24h -> not stale
        climod.cmd_reconcile(types.SimpleNamespace(), backend=self.fake_backend)
        na = cache.read_cached_view("needs-attention")
        self.assertEqual(na["tasks"], [])


class TestInstallHeartbeat(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.target = os.path.join(self.tmp, "LaunchAgents")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_install_writes_launchd_plist(self):
        from fulcra_coord import heartbeat
        with patch("fulcra_coord.cli_invocation.resolve_cli_argv",
                   return_value=["/opt/bin/fulcra-coord"]), \
             patch("sys.platform", "darwin"):
            plan = heartbeat.install_heartbeat(target_dir=self.target,
                                               interval_min=20)
        plist = os.path.join(self.target, "com.fulcra.coord.heartbeat.plist")
        self.assertTrue(os.path.exists(plist))
        body = open(plist).read()
        self.assertIn("/opt/bin/fulcra-coord", body)
        self.assertIn("reconcile", body)
        self.assertIn("<integer>1200</integer>", body)  # 20 min

    def test_install_is_idempotent(self):
        from fulcra_coord import heartbeat
        with patch("sys.platform", "darwin"):
            heartbeat.install_heartbeat(target_dir=self.target)
            heartbeat.install_heartbeat(target_dir=self.target)
        files = os.listdir(self.target)
        self.assertEqual(files.count("com.fulcra.coord.heartbeat.plist"), 1)

    def test_dry_run_writes_nothing(self):
        from fulcra_coord import heartbeat
        with patch("sys.platform", "darwin"):
            plan = heartbeat.install_heartbeat(target_dir=self.target,
                                               dry_run=True)
        self.assertFalse(os.path.exists(self.target) and os.listdir(self.target))
        self.assertTrue(plan.get("writes"))

    def test_uninstall_removes_plist(self):
        from fulcra_coord import heartbeat
        with patch("sys.platform", "darwin"):
            heartbeat.install_heartbeat(target_dir=self.target)
            heartbeat.install_heartbeat(target_dir=self.target, uninstall=True)
        plist = os.path.join(self.target, "com.fulcra.coord.heartbeat.plist")
        self.assertFalse(os.path.exists(plist))

    def test_crontab_fallback_on_non_macos(self):
        from fulcra_coord import heartbeat
        crontab = os.path.join(self.tmp, "crontab.txt")
        with patch("sys.platform", "linux"), \
             patch("fulcra_coord.cli_invocation.resolve_cli_argv",
                   return_value=["/opt/bin/fulcra-coord"]):
            plan = heartbeat.install_heartbeat(
                target_dir=self.tmp, interval_min=15,
                crontab_path=crontab)
        body = open(crontab).read()
        self.assertIn("fulcra-coord-heartbeat", body)  # managed marker
        self.assertIn("/opt/bin/fulcra-coord", body)
        self.assertIn("reconcile", body)

    def test_crontab_uninstall_is_surgical(self):
        from fulcra_coord import heartbeat
        crontab = os.path.join(self.tmp, "crontab.txt")
        with open(crontab, "w") as f:
            f.write("0 0 * * * /usr/bin/other-job\n")
        with patch("sys.platform", "linux"):
            heartbeat.install_heartbeat(target_dir=self.tmp, crontab_path=crontab)
            heartbeat.install_heartbeat(target_dir=self.tmp, crontab_path=crontab,
                                        uninstall=True)
        body = open(crontab).read()
        self.assertIn("/usr/bin/other-job", body)  # user's line preserved
        self.assertNotIn("fulcra-coord-heartbeat", body)


class TestInstallHeartbeatCmd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.target = os.path.join(self.tmp, "LaunchAgents")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cmd_installs(self):
        from fulcra_coord import cli as climod
        with patch("sys.platform", "darwin"):
            rc = climod.cmd_install_heartbeat(types.SimpleNamespace(
                interval_min=20, uninstall=False, dry_run=False,
                target_dir=self.target))
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(
            os.path.join(self.target, "com.fulcra.coord.heartbeat.plist")))


class TestInstallerHardening(unittest.TestCase):
    """#25 — launchd jobs run with a bare PATH and no log files, so they need
    hand-patching to run. Both installers must bake an EnvironmentVariables.PATH
    (homebrew + ~/.local/bin + ~/.cargo/bin + the resolved CLI's own dir) and
    StandardOut/ErrorPath under ~/Library/Logs/fulcra-coord, creating that Logs
    dir on install; the cron line must carry a PATH= prefix. All test-isolated:
    LaunchAgents writes go to target_dir, the Logs dir to logs_dir, cron to
    crontab_path — nothing touches the real ~/Library or the live crontab."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.target = os.path.join(self.tmp, "LaunchAgents")
        self.logs = os.path.join(self.tmp, "Logs", "fulcra-coord")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- launchd plist: EnvironmentVariables.PATH ---------------------------

    def _install_heartbeat_plist(self):
        import plistlib
        from fulcra_coord import heartbeat
        with patch("fulcra_coord.cli_invocation.resolve_cli_argv",
                   return_value=["/opt/cli-home/bin/fulcra-coord"]), \
             patch("sys.platform", "darwin"):
            heartbeat.install_heartbeat(target_dir=self.target,
                                        logs_dir=self.logs, interval_min=20)
        plist = os.path.join(self.target, "com.fulcra.coord.heartbeat.plist")
        with open(plist, "rb") as f:
            return plistlib.load(f)

    def _install_listener_plist(self):
        import plistlib
        from fulcra_coord import listener
        with patch("fulcra_coord.cli_invocation.resolve_cli_argv",
                   return_value=["/opt/cli-home/bin/fulcra-coord"]), \
             patch("sys.platform", "darwin"):
            listener.install_listener(agent="codex:h:r", target_dir=self.target,
                                      logs_dir=self.logs, interval_min=10)
        plist = os.path.join(self.target, "com.fulcra.coord.listener.plist")
        with open(plist, "rb") as f:
            return plistlib.load(f)

    def test_heartbeat_plist_has_path_with_homebrew_and_local_and_cli_dir(self):
        data = self._install_heartbeat_plist()
        path = data["EnvironmentVariables"]["PATH"]
        entries = path.split(":")
        self.assertIn("/opt/homebrew/bin", entries)
        self.assertIn("/usr/local/bin", entries)
        self.assertIn(str(Path.home() / ".local" / "bin"), entries)
        self.assertIn(str(Path.home() / ".cargo" / "bin"), entries)
        # the dir of the resolved CLI binary so `uv`/`fulcra-coord` resolve
        self.assertIn("/opt/cli-home/bin", entries)

    def test_listener_plist_has_path_with_homebrew_and_local_and_cli_dir(self):
        data = self._install_listener_plist()
        path = data["EnvironmentVariables"]["PATH"]
        entries = path.split(":")
        self.assertIn("/opt/homebrew/bin", entries)
        self.assertIn(str(Path.home() / ".local" / "bin"), entries)
        self.assertIn("/opt/cli-home/bin", entries)

    # --- launchd plist: StandardOut/ErrorPath + Logs dir creation -----------

    def test_heartbeat_plist_has_log_paths(self):
        data = self._install_heartbeat_plist()
        self.assertEqual(data["StandardOutPath"],
                         os.path.join(self.logs, "heartbeat.out.log"))
        self.assertEqual(data["StandardErrorPath"],
                         os.path.join(self.logs, "heartbeat.err.log"))

    def test_listener_plist_has_log_paths(self):
        data = self._install_listener_plist()
        self.assertEqual(data["StandardOutPath"],
                         os.path.join(self.logs, "listener.out.log"))
        self.assertEqual(data["StandardErrorPath"],
                         os.path.join(self.logs, "listener.err.log"))

    def test_heartbeat_creates_logs_dir(self):
        self._install_heartbeat_plist()
        self.assertTrue(os.path.isdir(self.logs))

    def test_listener_creates_logs_dir(self):
        self._install_listener_plist()
        self.assertTrue(os.path.isdir(self.logs))

    def test_dry_run_does_not_create_logs_dir(self):
        from fulcra_coord import heartbeat
        with patch("sys.platform", "darwin"):
            heartbeat.install_heartbeat(target_dir=self.target,
                                        logs_dir=self.logs, dry_run=True)
        self.assertFalse(os.path.exists(self.logs))

    def test_heartbeat_and_listener_coexist_with_distinct_labels(self):
        import plistlib
        from fulcra_coord import heartbeat, listener
        with patch("sys.platform", "darwin"):
            heartbeat.install_heartbeat(target_dir=self.target, logs_dir=self.logs)
            listener.install_listener(agent="codex:h:r", target_dir=self.target,
                                      logs_dir=self.logs)
        hb = os.path.join(self.target, "com.fulcra.coord.heartbeat.plist")
        ln = os.path.join(self.target, "com.fulcra.coord.listener.plist")
        self.assertTrue(os.path.exists(hb) and os.path.exists(ln))
        with open(hb, "rb") as f:
            self.assertEqual(plistlib.load(f)["Label"],
                             "com.fulcra.coord.heartbeat")
        with open(ln, "rb") as f:
            self.assertEqual(plistlib.load(f)["Label"],
                             "com.fulcra.coord.listener")

    # --- crontab: PATH= prefix ----------------------------------------------

    def test_heartbeat_cron_line_carries_path(self):
        from fulcra_coord import heartbeat
        crontab = os.path.join(self.tmp, "crontab.txt")
        with patch("sys.platform", "linux"), \
             patch("fulcra_coord.cli_invocation.resolve_cli_argv",
                   return_value=["/opt/cli-home/bin/fulcra-coord"]):
            heartbeat.install_heartbeat(target_dir=self.tmp, interval_min=15,
                                        crontab_path=crontab)
        body = open(crontab).read()
        self.assertIn("PATH=", body)
        self.assertIn("/opt/homebrew/bin", body)
        self.assertIn("/opt/cli-home/bin", body)

    def test_listener_cron_line_carries_path(self):
        from fulcra_coord import listener
        crontab = os.path.join(self.tmp, "crontab.txt")
        with patch("sys.platform", "linux"), \
             patch("fulcra_coord.cli_invocation.resolve_cli_argv",
                   return_value=["/opt/cli-home/bin/fulcra-coord"]):
            listener.install_listener(agent="codex:h:r", target_dir=self.tmp,
                                      interval_min=5, crontab_path=crontab)
        body = open(crontab).read()
        self.assertIn("PATH=", body)
        self.assertIn("/opt/homebrew/bin", body)

    def test_heartbeat_cron_with_path_still_idempotent(self):
        from fulcra_coord import heartbeat
        crontab = os.path.join(self.tmp, "crontab.txt")
        with patch("sys.platform", "linux"):
            heartbeat.install_heartbeat(target_dir=self.tmp, crontab_path=crontab)
            heartbeat.install_heartbeat(target_dir=self.tmp, crontab_path=crontab)
        body = open(crontab).read()
        self.assertEqual(body.count(heartbeat.CRON_MARKER), 1)
        self.assertEqual(body.count("reconcile"), 1)

    def test_listener_cron_with_path_uninstall_is_surgical(self):
        from fulcra_coord import listener
        crontab = os.path.join(self.tmp, "crontab.txt")
        with open(crontab, "w") as f:
            f.write("0 0 * * * /usr/bin/other-job\n")
        with patch("sys.platform", "linux"):
            listener.install_listener(agent="codex:h:r", target_dir=self.tmp,
                                      crontab_path=crontab)
            listener.install_listener(agent="codex:h:r", target_dir=self.tmp,
                                      crontab_path=crontab, uninstall=True)
        body = open(crontab).read()
        self.assertIn("/usr/bin/other-job", body)
        self.assertNotIn("fulcra-coord-listener", body)
        self.assertNotIn("notify-inbox", body)


# ---------------------------------------------------------------------------
# Gap 3 — cross-agent "what are all my agents doing" digest
# ---------------------------------------------------------------------------

class TestAgentsDigest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp
        self.fake_backend = ["false"]

    def tearDown(self):
        os.environ.pop("XDG_CACHE_HOME", None)
        os.environ.pop("FULCRA_COORD_STALE_HOURS", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cache(self, task):
        from fulcra_coord import cache
        cache.write_cached_task(task)

    def _run(self, **kw):
        from fulcra_coord.cli import cmd_agents
        import io, contextlib
        args = types.SimpleNamespace(mine=kw.get("mine"),
                                     format=kw.get("format", "table"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cmd_agents(args, backend=self.fake_backend)
        return rc, buf.getvalue()

    def test_empty_bus_clean_render(self):
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("No active", out)

    def test_groups_by_owner_agent(self):
        a = _with_status(_sample_task(), "active"); a["owner_agent"] = "agent-A"
        b = _with_status(_sample_task(), "active"); b["owner_agent"] = "agent-B"
        b["id"] = "TASK-bbb-zzz"; b["title"] = "Second task"
        self._cache(a); self._cache(b)
        rc, out = self._run()
        self.assertEqual(rc, 0)
        self.assertIn("agent-A", out)
        self.assertIn("agent-B", out)

    def test_json_format_groups(self):
        a = _with_status(_sample_task(), "active"); a["owner_agent"] = "agent-A"
        self._cache(a)
        rc, out = self._run(format="json")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertIn("agents", data)
        names = [g["agent"] for g in data["agents"]]
        self.assertIn("agent-A", names)
        grp = next(g for g in data["agents"] if g["agent"] == "agent-A")
        self.assertEqual(grp["counts"]["active"], 1)

    def test_stale_marker_surfaces(self):
        old = _stale_task(9); old["owner_agent"] = "agent-A"
        self._cache(old)
        rc, out = self._run()
        self.assertIn("⚠", out)  # ⚠

    def test_stale_flag_in_json(self):
        old = _stale_task(9); old["owner_agent"] = "agent-A"
        self._cache(old)
        rc, out = self._run(format="json")
        data = json.loads(out)
        grp = next(g for g in data["agents"] if g["agent"] == "agent-A")
        self.assertTrue(grp["tasks"][0]["stale"])

    def test_mine_filters_to_one_agent(self):
        a = _with_status(_sample_task(), "active"); a["owner_agent"] = "agent-A"
        b = _with_status(_sample_task(), "active"); b["owner_agent"] = "agent-B"
        b["id"] = "TASK-bbb-zzz"
        self._cache(a); self._cache(b)
        rc, out = self._run(mine="agent-A", format="json")
        data = json.loads(out)
        names = [g["agent"] for g in data["agents"]]
        self.assertEqual(names, ["agent-A"])

    def test_counts_split_by_status(self):
        act = _with_status(_sample_task(), "active"); act["owner_agent"] = "agent-A"
        wait = _with_status(_sample_task(), "waiting"); wait["owner_agent"] = "agent-A"
        wait["id"] = "TASK-wait-001"
        self._cache(act); self._cache(wait)
        rc, out = self._run(mine="agent-A", format="json")
        data = json.loads(out)
        grp = data["agents"][0]
        self.assertEqual(grp["counts"]["active"], 1)
        self.assertEqual(grp["counts"]["waiting"], 1)


# ---------------------------------------------------------------------------
# Gap 4 — install-codex
# ---------------------------------------------------------------------------

class TestCodexTemplates(unittest.TestCase):
    def test_reuses_claude_code_script_bodies(self):
        from fulcra_coord import codex, claude_code as cc
        # SessionStart body is shared verbatim with Claude Code (same stdin shape).
        self.assertEqual(codex.SESSION_START_SH, cc.SESSION_START_SH)
        # PreCompact reuses the CC body but keys the session-id env fallback on
        # FULCRA_COORD_SESSION_KEY (Codex's session id env differs).
        self.assertIn("FULCRA_COORD_SESSION_KEY", codex.PRE_COMPACT_SH)
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", codex.PRE_COMPACT_SH)
        # Gap 1 argv placeholder present so it gets a resolved argv at install.
        self.assertIn("__FULCRA_COORD_ARGV__", codex.PRE_COMPACT_SH)

    def test_no_stop_hook(self):
        # Codex Stop fires every turn and would thrash; end-parking is delegated
        # to the heartbeat. We must NOT ship a Stop hook.
        from fulcra_coord import codex
        self.assertNotIn("Stop", codex._EVENTS)


class TestInstallCodex(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "home"); os.makedirs(self.home)
        patch.dict(os.environ, {"HOME": self.home}).start()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True); patch.stopall()

    def _hooks(self):
        p = os.path.join(self.home, ".codex", "hooks.json")
        return json.load(open(p)) if os.path.exists(p) else None

    def test_install_materializes_scripts_and_merges_two_events(self):
        from fulcra_coord import codex
        with patch("fulcra_coord.cli_invocation.resolve_cli_argv",
                   return_value=["/opt/bin/fulcra-coord"]):
            codex.install_codex()
        h = self._hooks()
        self.assertIn("SessionStart", h["hooks"])
        self.assertIn("PreCompact", h["hooks"])
        self.assertNotIn("Stop", h["hooks"])
        hooks_dir = os.path.join(self.home, ".codex", "fulcra-coord-hooks")
        for f in ("session-start.sh", "pre-compact.sh"):
            self.assertTrue(os.access(os.path.join(hooks_dir, f), os.X_OK))
        # Materialized script carries the resolved argv array, not the placeholder.
        body = open(os.path.join(hooks_dir, "pre-compact.sh")).read()
        self.assertNotIn("__FULCRA_COORD_ARGV__", body)
        self.assertIn("FULCRA_COORD=(/opt/bin/fulcra-coord)", body)

    def test_install_is_idempotent(self):
        from fulcra_coord import codex
        codex.install_codex()
        codex.install_codex()
        h = self._hooks()
        self.assertEqual(len(h["hooks"]["SessionStart"]), 1)
        self.assertEqual(len(h["hooks"]["PreCompact"]), 1)

    def test_preserves_unrelated_codex_hooks(self):
        os.makedirs(os.path.join(self.home, ".codex"))
        pre = {"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": "/usr/bin/true"}]}],
            "Notification": [{"hooks": [{"type": "command", "command": "/bin/echo"}]}]}}
        json.dump(pre, open(os.path.join(self.home, ".codex", "hooks.json"), "w"))
        from fulcra_coord import codex
        codex.install_codex()
        h = self._hooks()
        cmds = [hh["command"] for e in h["hooks"]["SessionStart"] for hh in e["hooks"]]
        self.assertIn("/usr/bin/true", cmds)
        self.assertIn("Notification", h["hooks"])  # unrelated event preserved

    def test_uninstall_is_surgical(self):
        os.makedirs(os.path.join(self.home, ".codex"))
        pre = {"hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": "/usr/bin/true"}]}]}}
        json.dump(pre, open(os.path.join(self.home, ".codex", "hooks.json"), "w"))
        from fulcra_coord import codex
        codex.install_codex()
        codex.install_codex(uninstall=True)
        h = self._hooks()
        cmds = [hh["command"] for e in h["hooks"].get("SessionStart", []) for hh in e["hooks"]]
        self.assertEqual(cmds, ["/usr/bin/true"])
        self.assertNotIn("PreCompact", h["hooks"])

    def test_dry_run_writes_nothing(self):
        from fulcra_coord import codex
        plan = codex.install_codex(dry_run=True)
        self.assertIsNone(self._hooks())
        self.assertIn("PreCompact", plan["events"])

    def test_target_dir_override(self):
        from fulcra_coord import codex
        target = os.path.join(self.tmp, "elsewhere")
        codex.install_codex(target_dir=target)
        self.assertTrue(os.path.exists(os.path.join(target, "hooks.json")))


class TestInstallCodexCmd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.home = os.path.join(self.tmp, "h"); os.makedirs(self.home)
        patch.dict(os.environ, {"HOME": self.home}).start()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True); patch.stopall()

    def test_cmd_installs(self):
        from fulcra_coord import cli as climod
        rc = climod.cmd_install_codex(types.SimpleNamespace(
            uninstall=False, dry_run=False, target_dir=None))
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(
            os.path.join(self.home, ".codex", "hooks.json")))


class TestCodexHookParity(unittest.TestCase):
    def test_committed_scripts_match_templates(self):
        from fulcra_coord import codex
        import pathlib
        root = pathlib.Path(__file__).resolve().parents[1] / "adapters" / "codex" / "hooks"
        mapping = {"session-start.sh": codex.SESSION_START_SH,
                   "pre-compact.sh": codex.PRE_COMPACT_SH}
        for fname, body in mapping.items():
            self.assertEqual((root / fname).read_text(), body,
                             f"{fname} drifted from template; regenerate it")


# ---------------------------------------------------------------------------
# C1 — resolved CLI invocation survives paths containing spaces (argv, no
# word-splitting). These materialize each surface with a SPACED resolved argv
# (e.g. sys.executable under "~/Library/Application Support/...") and assert the
# CLI is invoked with the exact argv rather than split on the embedded space.
# ---------------------------------------------------------------------------

# A resolved argv whose interpreter path contains a space — the exact shape that
# breaks unquoted word-splitting (the Gap-1 failure mode C1 fixes).
SPACED_ARGV = ["/a b/python", "-m", "fulcra_coord"]


class TestResolveCliArgv(unittest.TestCase):
    """resolve_cli_argv() returns an explicit argv list; resolve_cli_command()
    is its shlex-joined display form (kept only for human-readable output)."""

    def test_argv_prefers_which_when_on_path(self):
        from fulcra_coord import cli_invocation
        with patch("shutil.which", return_value="/opt/bin/fulcra-coord"):
            self.assertEqual(cli_invocation.resolve_cli_argv(),
                             ["/opt/bin/fulcra-coord"])

    def test_argv_falls_back_to_python_m(self):
        from fulcra_coord import cli_invocation
        with patch("shutil.which", return_value=None):
            argv = cli_invocation.resolve_cli_argv()
        self.assertEqual(argv, [sys.executable, "-m", "fulcra_coord"])

    def test_command_is_shlex_join_of_argv(self):
        import shlex
        from fulcra_coord import cli_invocation
        with patch("fulcra_coord.cli_invocation.resolve_cli_argv",
                   return_value=SPACED_ARGV):
            self.assertEqual(cli_invocation.resolve_cli_command(),
                             shlex.join(SPACED_ARGV))


class TestClaudeHookSpacedArgvE2E(unittest.TestCase):
    """The materialized bash hooks must invoke the CLI with the exact argv even
    when argv[0] contains a space. A bash array preserves the tokens; the old
    unquoted `$FULCRA_COORD` word-split argv[0] into two broken tokens."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Build a fake CLI at a path that CONTAINS A SPACE.
        self.bindir = os.path.join(self.tmp, "a b")
        os.makedirs(self.bindir)
        self.calls = os.path.join(self.tmp, "calls.log")
        self.fake = os.path.join(self.bindir, "fulcra-coord")
        with open(self.fake, "w") as f:
            f.write("#!/usr/bin/env bash\n"
                    'if [ "$1" = "status" ]; then cat "%s"; exit 0; fi\n'
                    'if [ "$1" = "__session-task" ]; then echo "TASK-live"; exit 0; fi\n'
                    'echo "$@" >> "%s"\n'
                    % (os.path.join(self.tmp, "status.json"), self.calls))
        os.chmod(self.fake, 0o755)
        # Resolved argv with a spaced argv[0] — the fake CLI's real path.
        self.argv = [self.fake]
        from fulcra_coord import claude_code as cc
        from fulcra_coord.cli_invocation import materialize_argv
        self.hooks = os.path.join(self.tmp, "hooks"); os.makedirs(self.hooks)
        for fname, body in (("session-start.sh", cc.SESSION_START_SH),
                            ("pre-compact.sh", cc.PRE_COMPACT_SH),
                            ("session-end.sh", cc.SESSION_END_SH)):
            out = os.path.join(self.hooks, fname)
            with open(out, "w") as f:
                f.write(body.replace(cc.PLACEHOLDER_ARGV, materialize_argv(self.argv)))
            os.chmod(out, 0o755)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, script, stdin, statusjson="{}"):
        with open(os.path.join(self.tmp, "status.json"), "w") as f:
            f.write(statusjson)
        return subprocess.run(["bash", os.path.join(self.hooks, script)],
                              input=stdin, capture_output=True, text=True)

    def test_pre_compact_invokes_cli_on_spaced_path(self):
        # The fake CLI only logs (and thus exists to be called) if argv[0] is the
        # spaced path INTACT. Word-splitting would try to exec "/a" → no-op.
        r = self._run("pre-compact.sh",
                      json.dumps({"session_id": "s", "transcript_path": "/t.json"}))
        self.assertEqual(r.returncode, 0)
        self.assertTrue(os.path.exists(self.calls),
                        "CLI on spaced path was never invoked (argv word-split)")
        self.assertIn("update TASK-live", open(self.calls).read())

    def test_session_start_surfaces_task_via_spaced_path(self):
        sj = json.dumps({"active": [
            {"id": "TASK-live", "title": "Deploy", "status": "active",
             "owner_agent": "claude-code:%s:t" % os.uname().nodename.split('.')[0],
             "updated_at": "2026-06-01T00:00:00Z", "next_action": "do X"}]})
        r = self._run("session-start.sh", json.dumps({"cwd": self.tmp}), sj)
        self.assertEqual(r.returncode, 0)
        self.assertIn("TASK-live", r.stdout)


class TestHeartbeatSpacedArgv(unittest.TestCase):
    """plist ProgramArguments and the cron line must carry argv tokens, not a
    word-split of a string — so a spaced argv[0] survives intact."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.target = os.path.join(self.tmp, "LaunchAgents")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_plist_program_arguments_round_trip(self):
        import plistlib
        from fulcra_coord import heartbeat
        with patch("fulcra_coord.cli_invocation.resolve_cli_argv",
                   return_value=SPACED_ARGV), \
             patch("sys.platform", "darwin"):
            heartbeat.install_heartbeat(target_dir=self.target, interval_min=20)
        plist = os.path.join(self.target, "com.fulcra.coord.heartbeat.plist")
        with open(plist, "rb") as f:
            data = plistlib.load(f)
        self.assertEqual(data["ProgramArguments"], SPACED_ARGV + ["reconcile"])
        self.assertEqual(data["StartInterval"], 1200)

    def test_cron_line_shlex_splits_back_to_argv(self):
        import shlex
        from fulcra_coord import heartbeat
        crontab = os.path.join(self.tmp, "crontab.txt")
        with patch("sys.platform", "linux"), \
             patch("fulcra_coord.cli_invocation.resolve_cli_argv",
                   return_value=SPACED_ARGV):
            heartbeat.install_heartbeat(target_dir=self.tmp, interval_min=15,
                                        crontab_path=crontab)
        body = open(crontab).read()
        cmd_line = [ln for ln in body.splitlines()
                    if ln and not ln.startswith("#") and "reconcile" in ln][0]
        # Drop the 5 cron schedule fields; the rest must shlex-split to the
        # PATH= hardening prefix (#25) followed by the argv + subcommand.
        rest = cmd_line.split(None, 5)[5]
        # Strip the redirection suffix the cron line appends.
        rest = rest.split(" >/dev/null")[0]
        tokens = shlex.split(rest)
        # First token is the PATH= assignment; the remainder is the spaced argv.
        self.assertTrue(tokens[0].startswith("PATH="))
        self.assertEqual(tokens[1:], SPACED_ARGV + ["reconcile"])


class TestOpenClawSpacedArgv(unittest.TestCase):
    """The TS handlers must materialize a real JSON array literal so a spaced
    argv[0] is preserved (no `.split(/\\s+/)` on a string)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "openclaw-hooks")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_handler_json_array_parses_to_argv(self):
        import re
        from fulcra_coord import openclaw as oc
        with patch("fulcra_coord.cli_invocation.resolve_cli_argv",
                   return_value=SPACED_ARGV):
            oc.install_openclaw(hooks_root=self.root)
        body = open(os.path.join(self.root, oc.SHUTDOWN_DIRNAME,
                                 "handler.ts")).read()
        m = re.search(r"const FULCRA_COORD_CMD(?:\s*:\s*string\[\])?\s*=\s*(\[.*?\]);", body)
        self.assertIsNotNone(m, "expected a JSON array literal for FULCRA_COORD_CMD")
        self.assertEqual(json.loads(m.group(1)), SPACED_ARGV)
        # The split-based string approach must be gone.
        self.assertNotIn(".split(/", body)


class TestStripManagedCronSurgical(unittest.TestCase):
    """M2: uninstall removes the marker line AND the next line only when the
    next line is OUR managed reconcile command — never an unrelated user job."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_unrelated_line_after_marker_is_preserved(self):
        from fulcra_coord import heartbeat
        # A crontab where a managed marker is immediately followed by an
        # UNRELATED user job (e.g. the managed command line was hand-deleted but
        # the marker stayed, or lines got reordered). Stripping must not eat it.
        text = (heartbeat.CRON_MARKER + "\n"
                "0 3 * * * /usr/bin/backup-db\n"
                "*/5 * * * * /usr/bin/other\n")
        stripped = heartbeat._strip_managed_cron(text)
        self.assertNotIn(heartbeat.CRON_MARKER, stripped)
        self.assertIn("/usr/bin/backup-db", stripped)
        self.assertIn("/usr/bin/other", stripped)

    def test_managed_command_after_marker_is_removed(self):
        from fulcra_coord import heartbeat
        managed = "*/20 * * * * /opt/bin/fulcra-coord reconcile >/dev/null 2>&1"
        text = (heartbeat.CRON_MARKER + "\n" + managed + "\n"
                "0 3 * * * /usr/bin/backup-db\n")
        stripped = heartbeat._strip_managed_cron(text)
        self.assertNotIn(heartbeat.CRON_MARKER, stripped)
        self.assertNotIn("reconcile", stripped)
        self.assertIn("/usr/bin/backup-db", stripped)


class TestNeedsAttentionMissingFields(unittest.TestCase):
    """M3: task_summary on the needs-attention/reconcile path must tolerate a
    task missing updated_at / priority (a missing updated_at is treated stale
    and reaches task_summary; hard-indexing there raised KeyError)."""

    def test_build_needs_attention_missing_updated_at_does_not_raise(self):
        from fulcra_coord import views
        t = _with_status(_sample_task(), "active")
        t.pop("updated_at", None)
        t.pop("priority", None)
        # Must not raise KeyError; the task is stale (no timestamp) so it surfaces.
        na = views.build_needs_attention([t])
        ids = [x["id"] for x in na["tasks"]]
        self.assertIn(t["id"], ids)


class TestRootScopedCache(unittest.TestCase):
    """Regression: the local cache must be isolated per remote root, or tasks/
    views from one root (e.g. /coordination-demo) bleed into another's
    status/reconcile and get uploaded into the wrong root's views."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        patch.dict(os.environ, {"XDG_CACHE_HOME": self.tmp}).start()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        patch.stopall()

    def test_cached_tasks_isolated_by_root(self):
        from fulcra_coord import cache
        with patch.dict(os.environ, {"FULCRA_COORD_REMOTE_ROOT": "/coordination"}):
            cache.write_cached_task({"id": "TASK-A", "status": "active"})
            self.assertEqual([t["id"] for t in cache.list_cached_tasks()], ["TASK-A"])
        with patch.dict(os.environ, {"FULCRA_COORD_REMOTE_ROOT": "/coordination-demo"}):
            cache.write_cached_task({"id": "TASK-B", "status": "active"})
            # demo root must NOT see TASK-A
            self.assertEqual([t["id"] for t in cache.list_cached_tasks()], ["TASK-B"])
        with patch.dict(os.environ, {"FULCRA_COORD_REMOTE_ROOT": "/coordination"}):
            # production root still sees only its own task
            self.assertEqual([t["id"] for t in cache.list_cached_tasks()], ["TASK-A"])

    def test_cached_views_isolated_by_root(self):
        from fulcra_coord import cache
        with patch.dict(os.environ, {"FULCRA_COORD_REMOTE_ROOT": "/coordination"}):
            cache.write_cached_view("index", {"counts": {"active": 1}})
        with patch.dict(os.environ, {"FULCRA_COORD_REMOTE_ROOT": "/coordination-demo"}):
            self.assertIsNone(cache.read_cached_view("index"))

    def test_root_slug_sanitized(self):
        from fulcra_coord import cache
        with patch.dict(os.environ, {"FULCRA_COORD_REMOTE_ROOT": "/team/coordination"}):
            self.assertIn("team-coordination", str(cache.tasks_dir()))
        with patch.dict(os.environ, {"FULCRA_COORD_REMOTE_ROOT": "/coordination"}):
            self.assertNotIn("team-coordination", str(cache.tasks_dir()))

    def test_sessions_remain_global(self):
        # session pointers are keyed by globally-unique session id and store
        # their own root, so they intentionally stay outside the per-root scope.
        from fulcra_coord import cache
        with patch.dict(os.environ, {"FULCRA_COORD_REMOTE_ROOT": "/coordination"}):
            a = cache.sessions_dir()
        with patch.dict(os.environ, {"FULCRA_COORD_REMOTE_ROOT": "/coordination-demo"}):
            b = cache.sessions_dir()
        self.assertEqual(a, b)


# ---------------------------------------------------------------------------
# Coordination inbox — addressing model (Part 1)
# ---------------------------------------------------------------------------

class TestAssigneeSchema(unittest.TestCase):
    def test_make_task_accepts_assignee(self):
        t = make_task(title="Do a thing", workstream="devops",
                      agent="claude-code:host:repo", assignee="codex:host:repo")
        self.assertEqual(t["assignee"], "codex:host:repo")

    def test_make_task_assignee_defaults_none(self):
        t = make_task(title="Do a thing", workstream="devops", agent="a")
        self.assertIsNone(t.get("assignee"))

    def test_validate_passes_with_assignee(self):
        t = make_task(title="Do a thing", workstream="devops",
                      agent="a", assignee="b")
        self.assertEqual(validate_task(t), [])

    def test_validate_passes_without_assignee(self):
        t = make_task(title="Do a thing", workstream="devops", agent="a")
        self.assertEqual(validate_task(t), [])

    def test_task_summary_includes_assignee(self):
        t = make_task(title="Do a thing", workstream="devops",
                      agent="a", assignee="b")
        self.assertEqual(schema.task_summary(t)["assignee"], "b")

    def test_task_summary_assignee_none_when_absent(self):
        t = make_task(title="Do a thing", workstream="devops", agent="a")
        self.assertIsNone(schema.task_summary(t).get("assignee"))


class TestInboxAckEvent(unittest.TestCase):
    def test_apply_event_appends_inbox_ack_without_status_change(self):
        t = _with_status(_sample_task(), "proposed")
        before = t["status"]
        out = schema.apply_event(t, "inbox_ack", by="codex:me")
        self.assertEqual(out["status"], before)  # status unchanged
        types_ = [e["type"] for e in out["events"]]
        self.assertIn("inbox_ack", types_)
        ack = [e for e in out["events"] if e["type"] == "inbox_ack"][0]
        self.assertEqual(ack["by"], "codex:me")
        self.assertIn("at", ack)


# ---------------------------------------------------------------------------
# build_inbox view
# ---------------------------------------------------------------------------

def _directive(assignee, owner="agent-1", status="proposed", **ov):
    """A directive task: assigned to `assignee`, owned by someone else."""
    t = make_task(title="Please do X", workstream="general",
                  agent=owner, owner_agent=owner, assignee=assignee)
    t = _with_status(t, status)
    t.update(ov)
    return t


class TestBuildInbox(unittest.TestCase):
    def test_open_directive_grouped_by_assignee(self):
        from fulcra_coord.views import build_inbox
        d = _directive("codex:h:r")
        inbox = build_inbox([d])
        self.assertIn("codex-h-r", inbox)
        ids = [s["id"] for s in inbox["codex-h-r"]]
        self.assertIn(d["id"], ids)

    def test_excludes_tasks_without_assignee(self):
        from fulcra_coord.views import build_inbox
        t = _with_status(_sample_task(), "proposed")
        self.assertEqual(build_inbox([t]), {})

    def test_excludes_done_directives(self):
        from fulcra_coord.views import build_inbox
        d = _directive("codex:h:r", status="active")
        # active is not an "open inbox" status (proposed/waiting only)
        self.assertEqual(build_inbox([d]), {})

    def test_isolated_per_assignee(self):
        from fulcra_coord.views import build_inbox
        a = _directive("codex:h:r")
        b = _directive("gemini:h:r")
        inbox = build_inbox([a, b])
        self.assertIn("codex-h-r", inbox)
        self.assertIn("gemini-h-r", inbox)
        self.assertEqual(len(inbox["codex-h-r"]), 1)

    def test_excludes_acked_directive(self):
        from fulcra_coord.views import build_inbox
        d = _directive("codex:h:r")
        d["events"].append({"at": "2026-06-02T00:00:00Z", "type": "inbox_ack",
                            "by": "codex:h:r", "summary": "seen", "evidence": None})
        self.assertEqual(build_inbox([d]), {})

    def test_excludes_self_owned(self):
        # If owner_agent == assignee, it's my own task, not a directive to me.
        from fulcra_coord.views import build_inbox
        d = _directive("codex:h:r", owner="codex:h:r")
        self.assertEqual(build_inbox([d]), {})


class TestBuildAllViewsInbox(unittest.TestCase):
    def test_inbox_views_emitted_per_assignee(self):
        d = _directive("codex:h:r")
        views_out = build_all_views([d])
        self.assertIn("inbox/codex-h-r", views_out)

    def test_index_folds_inbox_count(self):
        d = _directive("codex:h:r")
        idx = build_index([d])
        self.assertIn("inbox", idx.get("counts", {}))
        self.assertEqual(idx["counts"]["inbox"].get("codex-h-r"), 1)


# ---------------------------------------------------------------------------
# tell / assign / inbox commands
# ---------------------------------------------------------------------------

class TestTellAssignInbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp
        self.fake_backend = ["false"]

    def tearDown(self):
        os.environ.pop("XDG_CACHE_HOME", None)
        os.environ.pop("FULCRA_COORD_AGENT", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ns(self, **kw):
        return types.SimpleNamespace(**kw)

    def test_tell_creates_directive_with_assignee(self):
        from fulcra_coord.cli import cmd_tell
        args = self._ns(assignee="codex:h:r", title="Run the migration",
                        workstream="general", priority="P1", next="kick it off",
                        summary="needs doing", **{"from": "claude-code:h:r"})
        cmd_tell(args, backend=self.fake_backend)
        tasks = cache.list_cached_tasks()
        d = [t for t in tasks if t["title"] == "Run the migration"][0]
        self.assertEqual(d["assignee"], "codex:h:r")
        self.assertEqual(d["owner_agent"], "claude-code:h:r")
        self.assertEqual(d["status"], "proposed")

    def test_assign_sets_assignee_on_existing_task(self):
        from fulcra_coord.cli import cmd_assign
        t = _with_status(_sample_task(), "proposed")
        cache.write_cached_task(t)
        args = self._ns(task_id=t["id"], assignee="codex:h:r", agent="claude-code")
        cmd_assign(args, backend=self.fake_backend)
        reloaded = cache.read_cached_task(t["id"])
        self.assertEqual(reloaded["assignee"], "codex:h:r")

    def test_inbox_lists_open_directive_for_me(self):
        from fulcra_coord.cli import cmd_inbox
        d = _directive("codex:h:r")
        cache.write_cached_task(d)
        import io, contextlib
        args = self._ns(agent="codex:h:r", format="json", ack=None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_inbox(args, backend=self.fake_backend)
        out = json.loads(buf.getvalue())
        ids = [i["id"] for i in out["inbox"]]
        self.assertIn(d["id"], ids)

    def test_inbox_excludes_directive_for_other_agent(self):
        from fulcra_coord.cli import cmd_inbox
        d = _directive("gemini:h:r")
        cache.write_cached_task(d)
        import io, contextlib
        args = self._ns(agent="codex:h:r", format="json", ack=None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_inbox(args, backend=self.fake_backend)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["inbox"], [])

    def test_inbox_ack_removes_from_inbox(self):
        from fulcra_coord.cli import cmd_inbox
        d = _directive("codex:h:r")
        cache.write_cached_task(d)
        # Ack it
        ack_args = self._ns(agent="codex:h:r", format="json", ack=d["id"])
        import io, contextlib
        with contextlib.redirect_stdout(io.StringIO()):
            cmd_inbox(ack_args, backend=self.fake_backend)
        reloaded = cache.read_cached_task(d["id"])
        self.assertTrue(any(e["type"] == "inbox_ack" and e["by"] == "codex:h:r"
                            for e in reloaded["events"]))
        # Now inbox is empty
        list_args = self._ns(agent="codex:h:r", format="json", ack=None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_inbox(list_args, backend=self.fake_backend)
        self.assertEqual(json.loads(buf.getvalue())["inbox"], [])

    def test_inbox_derives_agent_from_env(self):
        from fulcra_coord.cli import cmd_inbox
        os.environ["FULCRA_COORD_AGENT"] = "codex:h:r"
        d = _directive("codex:h:r")
        cache.write_cached_task(d)
        import io, contextlib
        args = self._ns(agent=None, format="json", ack=None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_inbox(args, backend=self.fake_backend)
        out = json.loads(buf.getvalue())
        self.assertEqual([i["id"] for i in out["inbox"]], [d["id"]])

    def test_inbox_table_format(self):
        from fulcra_coord.cli import cmd_inbox
        d = _directive("codex:h:r")
        cache.write_cached_task(d)
        import io, contextlib
        args = self._ns(agent="codex:h:r", format="table", ack=None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cmd_inbox(args, backend=self.fake_backend)
        self.assertEqual(rc, 0)
        self.assertIn(d["id"], buf.getvalue())


# ---------------------------------------------------------------------------
# Part 2 — SessionStart surfaces directives
# ---------------------------------------------------------------------------

class TestSessionStartInbox(unittest.TestCase):
    """The SessionStart hook surfaces a 📥 Directives section for the derived agent."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bin = os.path.join(self.tmp, "bin"); os.makedirs(self.bin)
        self.host = os.uname().nodename.split('.')[0]
        self.repo = os.path.basename(self.tmp)
        self.agent = f"claude-code:{self.host}:{self.repo}"
        self.status_json = os.path.join(self.tmp, "status.json")
        self.inbox_json = os.path.join(self.tmp, "inbox.json")
        self.inbox_args = os.path.join(self.tmp, "inbox_args.log")
        fake = os.path.join(self.bin, "fulcra-coord")
        with open(fake, "w") as f:
            f.write("#!/usr/bin/env bash\n"
                    'if [ "$1" = "status" ]; then cat "%s"; exit 0; fi\n'
                    'if [ "$1" = "inbox" ]; then echo "$@" > "%s"; cat "%s" 2>/dev/null; exit 0; fi\n'
                    'if [ "$1" = "__session-task" ]; then echo "TASK-live"; exit 0; fi\n'
                    'exit 0\n' % (self.status_json, self.inbox_args, self.inbox_json))
        os.chmod(fake, 0o755)
        from fulcra_coord.cli_invocation import PLACEHOLDER_ARGV, materialize_argv
        from fulcra_coord import claude_code as cc
        self.hook = os.path.join(self.tmp, "session-start.sh")
        with open(self.hook, "w") as f:
            f.write(cc.SESSION_START_SH.replace(
                PLACEHOLDER_ARGV, materialize_argv([fake])))
        os.chmod(self.hook, 0o755)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, status="{}", inbox="[]"):
        with open(self.status_json, "w") as f:
            f.write(status)
        with open(self.inbox_json, "w") as f:
            f.write(inbox)
        env = dict(os.environ); env["PATH"] = self.bin + os.pathsep + env["PATH"]
        return subprocess.run(["bash", self.hook],
                              input=json.dumps({"cwd": self.tmp}),
                              capture_output=True, text=True, env=env)

    def test_directives_section_emitted_when_inbox_has_item(self):
        inbox = json.dumps({"inbox": [
            {"id": "TASK-directive", "from": "claude-code:other:repo",
             "next_action": "do the migration", "title": "Migrate"}]})
        r = self._run(status=json.dumps({"active": []}), inbox=inbox)
        self.assertEqual(r.returncode, 0)
        self.assertIn("Directives for you", r.stdout)
        self.assertIn("TASK-directive", r.stdout)

    def test_silent_when_no_directives_and_no_work(self):
        r = self._run(status=json.dumps({"active": []}),
                      inbox=json.dumps({"inbox": []}))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")

    def test_inbox_call_does_not_pin_agent(self):
        # I1: the hook must NOT pass --agent to the inbox call. Passing it is
        # highest-precedence in resolve_agent and would override a persisted
        # (`identity set`) or $FULCRA_COORD_AGENT identity, so directives
        # addressed to a declared id would be missed. The inbox command must
        # resolve its own identity. (The status call may still derive/filter on
        # the auto id — only the inbox call is asserted here.)
        self._run(status=json.dumps({"active": []}),
                  inbox=json.dumps({"inbox": []}))
        with open(self.inbox_args) as f:
            recorded = f.read()
        self.assertNotIn("--agent", recorded,
                         "SessionStart inbox call must not pin --agent (I1)")


# ---------------------------------------------------------------------------
# Part 3 — durable listener (install-listener) + notify-inbox
# ---------------------------------------------------------------------------

class TestInstallListener(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.target = os.path.join(self.tmp, "LaunchAgents")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_install_writes_launchd_plist(self):
        from fulcra_coord import listener
        with patch("fulcra_coord.cli_invocation.resolve_cli_argv",
                   return_value=["/opt/bin/fulcra-coord"]), \
             patch("sys.platform", "darwin"):
            plan = listener.install_listener(agent="codex:h:r",
                                             target_dir=self.target,
                                             interval_min=10)
        plist = os.path.join(self.target, "com.fulcra.coord.listener.plist")
        self.assertTrue(os.path.exists(plist))
        body = open(plist).read()
        self.assertIn("/opt/bin/fulcra-coord", body)
        self.assertIn("notify-inbox", body)
        self.assertIn("codex:h:r", body)
        self.assertIn("<integer>600</integer>", body)  # 10 min

    def test_install_is_idempotent(self):
        from fulcra_coord import listener
        with patch("sys.platform", "darwin"):
            listener.install_listener(agent="codex:h:r", target_dir=self.target)
            listener.install_listener(agent="codex:h:r", target_dir=self.target)
        files = os.listdir(self.target)
        self.assertEqual(files.count("com.fulcra.coord.listener.plist"), 1)

    def test_dry_run_writes_nothing(self):
        from fulcra_coord import listener
        with patch("sys.platform", "darwin"):
            plan = listener.install_listener(agent="codex:h:r",
                                             target_dir=self.target, dry_run=True)
        self.assertFalse(os.path.exists(self.target) and os.listdir(self.target))
        self.assertTrue(plan.get("writes"))

    def test_uninstall_removes_plist(self):
        from fulcra_coord import listener
        with patch("sys.platform", "darwin"):
            listener.install_listener(agent="codex:h:r", target_dir=self.target)
            listener.install_listener(agent="codex:h:r", target_dir=self.target,
                                      uninstall=True)
        plist = os.path.join(self.target, "com.fulcra.coord.listener.plist")
        self.assertFalse(os.path.exists(plist))

    def test_crontab_fallback_on_non_macos(self):
        from fulcra_coord import listener
        crontab = os.path.join(self.tmp, "crontab.txt")
        with patch("sys.platform", "linux"), \
             patch("fulcra_coord.cli_invocation.resolve_cli_argv",
                   return_value=["/opt/bin/fulcra-coord"]):
            listener.install_listener(agent="codex:h:r", target_dir=self.tmp,
                                      interval_min=5, crontab_path=crontab)
        body = open(crontab).read()
        self.assertIn("fulcra-coord-listener", body)  # managed marker
        self.assertIn("notify-inbox", body)
        self.assertIn("codex:h:r", body)

    def test_crontab_uninstall_is_surgical(self):
        from fulcra_coord import listener
        crontab = os.path.join(self.tmp, "crontab.txt")
        with open(crontab, "w") as f:
            f.write("0 0 * * * /usr/bin/other-job\n")
        with patch("sys.platform", "linux"):
            listener.install_listener(agent="codex:h:r", target_dir=self.tmp,
                                      crontab_path=crontab)
            listener.install_listener(agent="codex:h:r", target_dir=self.tmp,
                                      crontab_path=crontab, uninstall=True)
        body = open(crontab).read()
        self.assertIn("/usr/bin/other-job", body)
        self.assertNotIn("fulcra-coord-listener", body)


class TestInstallListenerCmd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.target = os.path.join(self.tmp, "LaunchAgents")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cmd_installs(self):
        from fulcra_coord import cli as climod
        with patch("sys.platform", "darwin"):
            rc = climod.cmd_install_listener(types.SimpleNamespace(
                agent="codex:h:r", interval_min=10, uninstall=False,
                dry_run=False, target_dir=self.target))
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(
            os.path.join(self.target, "com.fulcra.coord.listener.plist")))


class TestNotifyInbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp
        self.fake_backend = ["false"]

    def tearDown(self):
        os.environ.pop("XDG_CACHE_HOME", None)
        os.environ.pop("FULCRA_COORD_AGENT", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _surface_path(self, agent):
        from fulcra_coord import cache, listener
        return cache.cache_root() / f"inbox-pending-{listener.agent_slug(agent)}.json"

    def test_writes_surface_file_when_inbox_nonempty(self):
        from fulcra_coord.cli import cmd_notify_inbox
        d = _directive("codex:h:r")
        cache.write_cached_task(d)
        with patch("fulcra_coord.listener.emit_notification") as emit:
            rc = cmd_notify_inbox(types.SimpleNamespace(agent="codex:h:r"),
                                  backend=self.fake_backend)
        self.assertEqual(rc, 0)
        sf = self._surface_path("codex:h:r")
        self.assertTrue(sf.exists())
        data = json.loads(sf.read_text())
        self.assertIn(d["id"], [i["id"] for i in data["inbox"]])
        emit.assert_called_once()

    def test_noop_when_inbox_empty(self):
        from fulcra_coord.cli import cmd_notify_inbox
        with patch("fulcra_coord.listener.emit_notification") as emit:
            rc = cmd_notify_inbox(types.SimpleNamespace(agent="codex:h:r"),
                                  backend=self.fake_backend)
        self.assertEqual(rc, 0)
        emit.assert_not_called()
        sf = self._surface_path("codex:h:r")
        # No directives -> surface file is empty/absent (no stale notification)
        if sf.exists():
            self.assertEqual(json.loads(sf.read_text())["inbox"], [])


class TestNotifyBlockedOnYou(unittest.TestCase):
    """notify-inbox ALSO notifies on NEW blocked-on-you items for the resolved
    human, once per item (idempotent seen-set), no-op when empty."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp, "config")
        os.environ["FULCRA_COORD_HUMAN"] = "ash"
        os.environ.pop("FULCRA_COORD_AGENT", None)
        self.fake_backend = ["false"]

    def tearDown(self):
        for k in ("XDG_CACHE_HOME", "XDG_CONFIG_HOME", "FULCRA_COORD_HUMAN",
                  "FULCRA_COORD_AGENT"):
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _human_item(self, tid):
        # A blocked-on-human task: assignee=ash, status blocked, needs:human.
        d = _directive("ash", owner="claude-code:h:vercel", status="blocked",
                       blocked_on="approve the deploy")
        d["id"] = tid
        d["tags"] = sorted(set(d["tags"] + ["needs:human"]))
        return d

    def test_new_blocked_item_notifies_once(self):
        from fulcra_coord.cli import cmd_notify_inbox
        cache.write_cached_task(self._human_item("TASK-20260603-x-aaaaaaaa"))
        with patch("fulcra_coord.listener.emit_notification"), \
             patch("fulcra_coord.listener.emit_message") as emsg:
            rc = cmd_notify_inbox(types.SimpleNamespace(agent="codex:h:r"),
                                  backend=self.fake_backend)
        self.assertEqual(rc, 0)
        emsg.assert_called_once()
        # The message names the requesting agent + the ask.
        msg = emsg.call_args[0][0]
        self.assertIn("claude-code:h:vercel", msg)
        self.assertIn("approve the deploy", msg)

    def test_repeat_does_not_renotify(self):
        from fulcra_coord.cli import cmd_notify_inbox
        cache.write_cached_task(self._human_item("TASK-20260603-x-bbbbbbbb"))
        with patch("fulcra_coord.listener.emit_notification"), \
             patch("fulcra_coord.listener.emit_message") as emsg:
            cmd_notify_inbox(types.SimpleNamespace(agent="codex:h:r"),
                             backend=self.fake_backend)
            cmd_notify_inbox(types.SimpleNamespace(agent="codex:h:r"),
                             backend=self.fake_backend)
        self.assertEqual(emsg.call_count, 1)

    def test_empty_no_notification(self):
        from fulcra_coord.cli import cmd_notify_inbox
        with patch("fulcra_coord.listener.emit_notification"), \
             patch("fulcra_coord.listener.emit_message") as emsg:
            cmd_notify_inbox(types.SimpleNamespace(agent="codex:h:r"),
                             backend=self.fake_backend)
        emsg.assert_not_called()

    def test_second_new_item_notifies_again(self):
        from fulcra_coord.cli import cmd_notify_inbox
        cache.write_cached_task(self._human_item("TASK-20260603-x-cccccccc"))
        with patch("fulcra_coord.listener.emit_notification"), \
             patch("fulcra_coord.listener.emit_message") as emsg:
            cmd_notify_inbox(types.SimpleNamespace(agent="codex:h:r"),
                             backend=self.fake_backend)
            cache.write_cached_task(self._human_item("TASK-20260603-x-dddddddd"))
            cmd_notify_inbox(types.SimpleNamespace(agent="codex:h:r"),
                             backend=self.fake_backend)
        self.assertEqual(emsg.call_count, 2)


class TestListenerParity(unittest.TestCase):
    def test_openclaw_heartbeat_runs_notify_inbox(self):
        from fulcra_coord import openclaw as oc
        self.assertIn("notify-inbox", oc.HEARTBEAT_MD_BODY)

    def test_listener_doc_present(self):
        import pathlib
        p = (pathlib.Path(__file__).resolve().parents[1] / "adapters"
             / "claude-code" / "LISTENER.md")
        self.assertTrue(p.exists())
        text = p.read_text()
        self.assertIn("notify-inbox", text)
        self.assertIn("install-listener", text)


# ---------------------------------------------------------------------------
# C1 regression — acking/claiming a directive must clear it from the inbox.
#
# Root cause: build_all_views emits an inbox/<slug> view ONLY for assignees who
# still have open directives. When an inbox empties (the last directive is acked
# or claimed), the stale inbox/<slug>.json is never overwritten — and _load_inbox
# PREFERRED that cached view, so it kept returning the phantom directive. The fix
# makes _load_inbox authoritative from the task set (matching cmd_agents). These
# tests use the real stateful fake backend so the full write + view-cache path
# runs end-to-end, faithfully reproducing the stale-cached-view condition.
# ---------------------------------------------------------------------------

class TestInboxClearsAfterAckOrClaim(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fake_root = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp
        os.environ["FULCRA_FAKE_ROOT"] = self.fake_root
        backend_script = str(Path(__file__).resolve().parent / "fake_fulcra_backend.py")
        # Stateful backend: writes/reads against FULCRA_FAKE_ROOT so views are
        # actually uploaded and re-cached, exactly as in production.
        self.fake_backend = [sys.executable, backend_script]

    def tearDown(self):
        os.environ.pop("XDG_CACHE_HOME", None)
        os.environ.pop("FULCRA_FAKE_ROOT", None)
        os.environ.pop("FULCRA_COORD_AGENT", None)
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.fake_root, ignore_errors=True)

    def _ns(self, **kw):
        return types.SimpleNamespace(**kw)

    def _inbox_json(self, me):
        from fulcra_coord.cli import cmd_inbox
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_inbox(self._ns(agent=me, format="json", ack=None),
                      backend=self.fake_backend)
        return json.loads(buf.getvalue())["inbox"]

    def test_ack_clears_inbox_even_with_stale_cached_view(self):
        """tell -> (views built+cached) -> inbox --ack -> inbox is EMPTY.

        Before the fix the acked directive lingered because _load_inbox returned
        the stale cached inbox view that build_all_views never overwrote.
        """
        from fulcra_coord.cli import cmd_tell, cmd_inbox
        import io, contextlib
        me = "codex:h:r"
        # Boss directs work at me; this builds + uploads + caches all views,
        # including inbox/codex-h-r.json.
        tell_args = self._ns(assignee=me, title="do x", workstream="general",
                              priority="P2", next="", summary="",
                              **{"from": "boss:h:r"})
        with contextlib.redirect_stdout(io.StringIO()):
            cmd_tell(tell_args, backend=self.fake_backend)
        # Sanity: it is in the inbox now.
        self.assertEqual(len(self._inbox_json(me)), 1)
        d = self._inbox_json(me)[0]

        # Ack it (full-success write rebuilds views).
        with contextlib.redirect_stdout(io.StringIO()):
            rc = cmd_inbox(self._ns(agent=me, format="json", ack=d["id"]),
                           backend=self.fake_backend)
        self.assertEqual(rc, 0)

        # Inbox must now be EMPTY, and the authoritative recompute must agree.
        self.assertEqual(self._inbox_json(me), [],
                         "Acked directive must not linger in the inbox")
        recomputed = views.build_inbox(cache.list_cached_tasks()).get(
            views.agent_slug(me), [])
        self.assertEqual(recomputed, [],
                         "build_inbox over the task set must also be empty")

    def test_claim_clears_inbox_even_with_stale_cached_view(self):
        """tell -> update --status active --agent me (claim) -> inbox EMPTY."""
        from fulcra_coord.cli import cmd_tell, cmd_update
        import io, contextlib
        me = "codex:h:r"
        tell_args = self._ns(assignee=me, title="do y", workstream="general",
                             priority="P2", next="", summary="",
                             **{"from": "boss:h:r"})
        with contextlib.redirect_stdout(io.StringIO()):
            cmd_tell(tell_args, backend=self.fake_backend)
        d = self._inbox_json(me)[0]

        # Claim it: status -> active, owner becomes me.
        upd = self._ns(task_id=d["id"], status="active", agent=me,
                       summary="claiming", next=None, blocked_on=None)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = cmd_update(upd, backend=self.fake_backend)
        self.assertEqual(rc, 0)

        self.assertEqual(self._inbox_json(me), [],
                         "Claimed directive must not linger in the inbox")


# ---------------------------------------------------------------------------
# I1 regression — a concurrent assign (reassignment) racing a status change must
# not lose the new assignee/owner_agent. _try_merge previously reconciled only
# events/current_summary/next_action; assignee/owner_agent were taken wholesale
# from the merge base, so the concurrent reassignment was silently dropped.
# ---------------------------------------------------------------------------

class TestTryMergePreservesAssigneeOwner(unittest.TestCase):
    def test_local_assign_newer_wins_over_remote_status_change(self):
        """(a) base proposed/assignee=alice; local assign->bob (newer);
        remote proposed->active. Merged must keep assignee=bob, status=active."""
        from fulcra_coord.cli import _try_merge
        from datetime import datetime, timezone, timedelta
        base = _directive("alice", owner="boss")  # proposed, assignee=alice

        # Remote: status change proposed -> active (older).
        t_remote = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
        remote_active = apply_transition(base, "active", by="boss", dt=t_remote)

        # Local: reassign to bob (newer than the remote status change). Mirrors
        # cmd_assign: an `updated` event + the field set on the returned copy.
        t_local = t_remote + timedelta(seconds=30)
        local_assign = apply_update(base, by="boss",
                                    summary="Assigned to bob by boss.", dt=t_local)
        local_assign["assignee"] = "bob"

        result = _try_merge(local_assign, remote_active)
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "active",
                         "Remote's authoritative status must win")
        self.assertEqual(result["assignee"], "bob",
                         "Newer local reassignment must survive the merge")

    def test_remote_assign_newer_wins_over_local_status_change(self):
        """(b) symmetric: local status change; remote assign alice->bob (newer).
        Merged must keep assignee=bob."""
        from fulcra_coord.cli import _try_merge
        from datetime import datetime, timezone, timedelta
        base = _directive("alice", owner="boss")  # proposed, assignee=alice

        # Local: status change proposed -> active (older).
        t_local = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
        local_active = apply_transition(base, "active", by="me", dt=t_local)

        # Remote: reassign to bob (newer).
        t_remote = t_local + timedelta(seconds=30)
        remote_assign = apply_update(base, by="boss",
                                     summary="Assigned to bob by boss.", dt=t_remote)
        remote_assign["assignee"] = "bob"

        result = _try_merge(local_active, remote_assign)
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "active",
                         "Local's status change must win (only local changed status)")
        self.assertEqual(result["assignee"], "bob",
                         "Newer remote reassignment must survive the merge")

    def test_owner_agent_carried_from_newer_side(self):
        """owner_agent must follow the more-recently-updated side too."""
        from fulcra_coord.cli import _try_merge
        from datetime import datetime, timezone, timedelta
        base = _directive("alice", owner="boss")

        t_remote = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
        remote_active = apply_transition(base, "active", by="boss", dt=t_remote)

        t_local = t_remote + timedelta(seconds=30)
        local_reown = apply_update(base, by="boss", summary="reowned", dt=t_local)
        local_reown["owner_agent"] = "carol"

        result = _try_merge(local_reown, remote_active)
        self.assertIsNotNone(result)
        self.assertEqual(result["owner_agent"], "carol",
                         "Newer local owner_agent must survive the merge")


# ---------------------------------------------------------------------------
# Feature #1 — agent identity handshake (resolve_agent + agent_matches)
# ---------------------------------------------------------------------------

class TestAgentMatches(unittest.TestCase):
    """Prefix-aware inbox membership: a directive addressed to a short id must
    reach the full-id agent that the short id is a prefix of (the arc bug)."""

    def test_exact_match(self):
        from fulcra_coord.views import agent_matches
        self.assertTrue(agent_matches("claude-code:host:repo", "claude-code:host:repo"))

    def test_arc_case_short_assignee_matches_full_me(self):
        # THE EXACT arc case: a directive addressed to the short id "claude-code"
        # must land in the inbox of "claude-code:DeskbookPro:fulcra-coord".
        from fulcra_coord.views import agent_matches
        me = "claude-code:DeskbookPro:fulcra-coord"
        self.assertTrue(agent_matches(me, "claude-code"))

    def test_two_segment_prefix_matches(self):
        from fulcra_coord.views import agent_matches
        me = "claude-code:DeskbookPro:fulcra-coord"
        self.assertTrue(agent_matches(me, "claude-code:DeskbookPro"))

    def test_different_kind_does_not_match(self):
        # "openclaw" is NOT a prefix of a claude-code id.
        from fulcra_coord.views import agent_matches
        me = "claude-code:DeskbookPro:fulcra-coord"
        self.assertFalse(agent_matches(me, "openclaw"))

    def test_divergent_segment_does_not_match(self):
        # "claude-code:other" diverges at the second segment.
        from fulcra_coord.views import agent_matches
        me = "claude-code:DeskbookPro:fulcra-coord"
        self.assertFalse(agent_matches(me, "claude-code:other"))

    def test_longer_assignee_does_not_match_shorter_me(self):
        # A more-specific assignee must NOT match a less-specific me — only a
        # strict prefix (or equal) of MY segments counts.
        from fulcra_coord.views import agent_matches
        self.assertFalse(agent_matches("claude-code", "claude-code:host:repo"))

    def test_partial_segment_is_not_a_prefix(self):
        # Prefix is per-colon-segment, not per-character: "claude" is not a
        # segment-prefix of "claude-code:...".
        from fulcra_coord.views import agent_matches
        me = "claude-code:DeskbookPro:fulcra-coord"
        self.assertFalse(agent_matches(me, "claude"))

    def test_none_or_empty_args_return_false_not_raise(self):
        # M2: a falsy me/assignee can never be a real match and None.split(":")
        # would raise into a best-effort read path — early-return False instead.
        from fulcra_coord.views import agent_matches
        self.assertFalse(agent_matches(None, "claude-code:h:r"))
        self.assertFalse(agent_matches("claude-code:h:r", None))
        self.assertFalse(agent_matches("", "claude-code:h:r"))
        self.assertFalse(agent_matches("claude-code:h:r", ""))
        self.assertFalse(agent_matches(None, None))


class TestBuildInboxPrefixMatch(unittest.TestCase):
    """build_inbox keeps keying by the assignee's own slug, but membership for a
    querying agent is decided by agent_matches over the live task set."""

    def test_short_assignee_directive_visible_to_full_id(self):
        from fulcra_coord.views import build_inbox, agent_matches, agent_slug
        d = _directive("claude-code", owner="boss")
        inbox = build_inbox([d])
        # Keyed by the assignee's own slug (unchanged behavior).
        self.assertIn(agent_slug("claude-code"), inbox)
        # The full-id agent finds it via prefix match across all buckets.
        me = "claude-code:DeskbookPro:fulcra-coord"
        found = [s for slug, items in inbox.items()
                 for s in items
                 if agent_matches(me, _assignee_of(d))]
        self.assertTrue(any(s["id"] == d["id"] for s in found))


def _assignee_of(task):
    return task.get("assignee")


class TestResolveAgent(unittest.TestCase):
    """resolve_agent order: explicit > FULCRA_COORD_AGENT env > persisted file >
    derived default. Identity file lives under XDG_CONFIG_HOME (test-isolated)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, "config")
        os.environ["XDG_CONFIG_HOME"] = self.cfg
        os.environ.pop("FULCRA_COORD_AGENT", None)

    def tearDown(self):
        os.environ.pop("XDG_CONFIG_HOME", None)
        os.environ.pop("FULCRA_COORD_AGENT", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_explicit_wins(self):
        from fulcra_coord import identity
        os.environ["FULCRA_COORD_AGENT"] = "env:agent"
        identity.set_identity("file:agent")
        self.assertEqual(identity.resolve_agent("explicit:agent"), "explicit:agent")

    def test_env_beats_file_and_derived(self):
        from fulcra_coord import identity
        os.environ["FULCRA_COORD_AGENT"] = "env:agent"
        identity.set_identity("file:agent")
        self.assertEqual(identity.resolve_agent(), "env:agent")

    def test_file_beats_derived(self):
        from fulcra_coord import identity
        identity.set_identity("file:agent")
        self.assertEqual(identity.resolve_agent(), "file:agent")

    def test_derived_default_when_nothing_set(self):
        from fulcra_coord import identity
        me = identity.resolve_agent()
        self.assertTrue(me.startswith("claude-code:"))
        self.assertEqual(len(me.split(":")), 3)

    def test_persisted_then_reused(self):
        from fulcra_coord import identity
        identity.set_identity("claude-code:Desk:proj")
        # A fresh resolve (no explicit/env) reads the persisted file.
        self.assertEqual(identity.resolve_agent(), "claude-code:Desk:proj")

    def test_clear_falls_back_to_derived(self):
        from fulcra_coord import identity
        identity.set_identity("file:agent")
        identity.clear_identity()
        me = identity.resolve_agent()
        self.assertTrue(me.startswith("claude-code:"))

    def test_resolve_source_reports_origin(self):
        from fulcra_coord import identity
        self.assertEqual(identity.resolve_agent_source()[1], "derived")
        identity.set_identity("file:agent")
        self.assertEqual(identity.resolve_agent_source(), ("file:agent", "config"))
        os.environ["FULCRA_COORD_AGENT"] = "env:agent"
        self.assertEqual(identity.resolve_agent_source(), ("env:agent", "env"))
        self.assertEqual(identity.resolve_agent_source("x:y"), ("x:y", "explicit"))

    def test_identity_file_under_xdg_config_home(self):
        from fulcra_coord import identity
        identity.set_identity("file:agent")
        self.assertTrue(os.path.exists(identity.identity_path()))
        self.assertTrue(str(identity.identity_path()).startswith(self.cfg))


class TestIdentityCommand(unittest.TestCase):
    """`fulcra-coord identity` show/set/clear."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp, "config")
        os.environ.pop("FULCRA_COORD_AGENT", None)

    def tearDown(self):
        os.environ.pop("XDG_CONFIG_HOME", None)
        os.environ.pop("FULCRA_COORD_AGENT", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, **kw):
        import io, contextlib
        from fulcra_coord.cli import cmd_identity
        args = types.SimpleNamespace(format="json", **kw)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cmd_identity(args)
        return rc, buf.getvalue()

    def test_set_persists_and_show_reports(self):
        rc, _ = self._run(identity_action="set", agent_id="claude-code:Desk:proj")
        self.assertEqual(rc, 0)
        rc, out = self._run(identity_action=None, agent_id=None)
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["agent"], "claude-code:Desk:proj")
        self.assertEqual(data["source"], "config")

    def test_clear_reverts_to_derived(self):
        self._run(identity_action="set", agent_id="claude-code:Desk:proj")
        rc, _ = self._run(identity_action="clear", agent_id=None)
        self.assertEqual(rc, 0)
        rc, out = self._run(identity_action=None, agent_id=None)
        data = json.loads(out)
        self.assertEqual(data["source"], "derived")
        self.assertTrue(data["agent"].startswith("claude-code:"))

    def _run_text(self, **kw):
        import io, contextlib
        from fulcra_coord.cli import cmd_identity
        args = types.SimpleNamespace(format="table", **kw)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cmd_identity(args)
        return rc, buf.getvalue()

    def test_show_surfaces_legacy_hint_when_unset(self):
        # I-1: a legacy global exists and this cwd has no per-cwd entry -> show a
        # one-line note that it's no longer used and how to set the repo identity.
        from fulcra_coord import identity
        legacy = identity.config_root() / "identity.json"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(json.dumps({"agent": "codex:Mac:main"}))
        rc, out = self._run_text(identity_action=None, agent_id=None)
        self.assertEqual(rc, 0)
        self.assertIn("legacy global identity", out)
        self.assertIn("codex:Mac:main", out)
        self.assertIn("identity set", out)

    def test_show_no_legacy_hint_when_per_cwd_set(self):
        # Once this cwd declares its own identity the legacy hint is suppressed.
        from fulcra_coord import identity
        legacy = identity.config_root() / "identity.json"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(json.dumps({"agent": "codex:Mac:main"}))
        self._run(identity_action="set", agent_id="claude-code:Desk:proj")
        rc, out = self._run_text(identity_action=None, agent_id=None)
        self.assertNotIn("legacy global identity", out)

    def test_migrate_copies_legacy_into_per_cwd(self):
        # `identity migrate` copies the legacy global into this cwd's entry so it
        # resolves with source "config" afterward.
        from fulcra_coord import identity
        legacy = identity.config_root() / "identity.json"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(json.dumps({"agent": "codex:Mac:main"}))
        rc, _ = self._run(identity_action="migrate", agent_id=None)
        self.assertEqual(rc, 0)
        rc, out = self._run(identity_action=None, agent_id=None)
        data = json.loads(out)
        self.assertEqual(data["agent"], "codex:Mac:main")
        self.assertEqual(data["source"], "config")


class TestInboxPrefixRegression(unittest.TestCase):
    """The real arc bug, end-to-end through the CLI: a directive `tell`-ed to the
    SHORT id `claude-code` must appear in the inbox of the full-id agent."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp, "config")
        os.environ.pop("FULCRA_COORD_AGENT", None)
        self.fake_backend = ["false"]

    def tearDown(self):
        os.environ.pop("XDG_CACHE_HOME", None)
        os.environ.pop("XDG_CONFIG_HOME", None)
        os.environ.pop("FULCRA_COORD_AGENT", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ns(self, **kw):
        return types.SimpleNamespace(**kw)

    def test_tell_short_id_reaches_full_id_inbox(self):
        from fulcra_coord.cli import cmd_tell, cmd_inbox
        import io, contextlib
        tell_args = self._ns(assignee="claude-code", title="do x",
                              workstream="general", priority="P2",
                              next="", summary="", **{"from": "boss"})
        cmd_tell(tell_args, backend=self.fake_backend)

        inbox_args = self._ns(agent="claude-code:DeskbookPro:fulcra-coord",
                              format="json", ack=None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_inbox(inbox_args, backend=self.fake_backend)
        out = json.loads(buf.getvalue())
        titles = [i["title"] for i in out["inbox"]]
        self.assertIn("do x", titles)

    def test_unrelated_kind_does_not_receive_short_directive(self):
        from fulcra_coord.cli import cmd_tell, cmd_inbox
        import io, contextlib
        tell_args = self._ns(assignee="claude-code", title="do x",
                              workstream="general", priority="P2",
                              next="", summary="", **{"from": "boss"})
        cmd_tell(tell_args, backend=self.fake_backend)
        # An openclaw agent must NOT see a claude-code directive.
        inbox_args = self._ns(agent="openclaw:host:repo", format="json", ack=None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_inbox(inbox_args, backend=self.fake_backend)
        self.assertEqual(json.loads(buf.getvalue())["inbox"], [])

    def test_identity_set_then_inbox_uses_it(self):
        from fulcra_coord import identity
        from fulcra_coord.cli import cmd_tell, cmd_inbox
        import io, contextlib
        identity.set_identity("claude-code:DeskbookPro:fulcra-coord")
        tell_args = self._ns(assignee="claude-code", title="do y",
                              workstream="general", priority="P2",
                              next="", summary="", **{"from": "boss"})
        cmd_tell(tell_args, backend=self.fake_backend)
        # No --agent: inbox must resolve "me" from the persisted identity.
        inbox_args = self._ns(agent=None, format="json", ack=None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_inbox(inbox_args, backend=self.fake_backend)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["agent"], "claude-code:DeskbookPro:fulcra-coord")
        self.assertIn("do y", [i["title"] for i in out["inbox"]])


class TestLifecycleAgentResolution(unittest.TestCase):
    """B-1 regression: block/pause/done/abandon/update with NO --agent must record
    the RESOLVED identity (persisted/derived) as last_touched_by, not the literal
    string "agent". The other bus ops already resolve via identity.resolve_agent;
    these four diverged because their parser default was the literal "agent"."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp, "config")
        os.environ.pop("FULCRA_COORD_AGENT", None)
        self.fake_backend = ["false"]
        # Persist a concrete identity so the resolved value is deterministic and
        # clearly distinguishable from the old "agent" literal.
        from fulcra_coord import identity
        identity.set_identity("claude-code:TestHost:fulcra-coord")
        self.expected = "claude-code:TestHost:fulcra-coord"

    def tearDown(self):
        os.environ.pop("XDG_CACHE_HOME", None)
        os.environ.pop("XDG_CONFIG_HOME", None)
        os.environ.pop("FULCRA_COORD_AGENT", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ns(self, **kw):
        return types.SimpleNamespace(**kw)

    def _make_active_task(self, title):
        from fulcra_coord.cli import cmd_start
        cmd_start(self._ns(title=title, workstream="devops", agent="someone-else",
                           kind="ops", priority="P2", summary="", next="",
                           surface=None), backend=self.fake_backend)
        task = next(t for t in cache.list_cached_tasks() if t["title"] == title)
        active = apply_transition(task, "active", by="someone-else")
        cache.write_cached_task(active)
        return task["id"]

    def test_block_without_agent_records_resolved_identity(self):
        from fulcra_coord.cli import cmd_block
        tid = self._make_active_task("block-noagent")
        # agent=None mirrors the parser default after B-1.
        cmd_block(self._ns(task_id=tid, blocked_on="waiting", agent=None),
                  backend=self.fake_backend)
        cached = cache.read_cached_task(tid)
        self.assertEqual(cached["last_touched_by"], self.expected)
        self.assertNotEqual(cached["last_touched_by"], "agent")

    def test_pause_without_agent_records_resolved_identity(self):
        from fulcra_coord.cli import cmd_pause
        tid = self._make_active_task("pause-noagent")
        cmd_pause(self._ns(task_id=tid, next="later", agent=None),
                  backend=self.fake_backend)
        cached = cache.read_cached_task(tid)
        self.assertEqual(cached["last_touched_by"], self.expected)

    def test_done_without_agent_records_resolved_identity(self):
        from fulcra_coord.cli import cmd_done
        tid = self._make_active_task("done-noagent")
        cmd_done(self._ns(task_id=tid, evidence="shipped",
                          verification_level="agent-verified", confidence=None,
                          agent=None), backend=self.fake_backend)
        cached = cache.read_cached_task(tid)
        self.assertEqual(cached["last_touched_by"], self.expected)

    def test_abandon_without_agent_records_resolved_identity(self):
        from fulcra_coord.cli import cmd_abandon
        tid = self._make_active_task("abandon-noagent")
        cmd_abandon(self._ns(task_id=tid, reason="dropped", agent=None),
                    backend=self.fake_backend)
        cached = cache.read_cached_task(tid)
        self.assertEqual(cached["last_touched_by"], self.expected)

    def test_update_without_agent_records_resolved_identity(self):
        from fulcra_coord.cli import cmd_update
        tid = self._make_active_task("update-noagent")
        cmd_update(self._ns(task_id=tid, summary="progress", next=None,
                            blocked_on=None, status=None, agent=None),
                   backend=self.fake_backend)
        cached = cache.read_cached_task(tid)
        self.assertEqual(cached["last_touched_by"], self.expected)
        self.assertNotEqual(cached["last_touched_by"], "agent")


class TestInstallLogsDirArg(unittest.TestCase):
    """B-2 regression: --logs-dir must be accepted by install-heartbeat and
    install-listener and thread through to the installer (parallel to --target-dir)."""

    def test_install_listener_accepts_logs_dir(self):
        from fulcra_coord.entry import build_parser
        p = build_parser()
        args = p.parse_args([
            "install-listener", "--logs-dir", "/tmp/x", "--target-dir", "/tmp/t",
            "--dry-run",
        ])
        self.assertEqual(args.logs_dir, "/tmp/x")
        self.assertTrue(args.dry_run)

    def test_install_heartbeat_accepts_logs_dir(self):
        from fulcra_coord.entry import build_parser
        p = build_parser()
        args = p.parse_args([
            "install-heartbeat", "--logs-dir", "/tmp/x", "--dry-run",
        ])
        self.assertEqual(args.logs_dir, "/tmp/x")

    def test_update_status_choices_exclude_proposed(self):
        """B-3 regression: 'proposed' is not a reachable transition target."""
        from fulcra_coord.entry import build_parser
        p = build_parser()
        with self.assertRaises(SystemExit):
            p.parse_args(["update", "T-1", "--status", "proposed"])


# ---------------------------------------------------------------------------
# Feature #2 — all-agents broadcast messaging (arc's proposal)
#
# A broadcast is a directive whose assignee is the wildcard sentinel "*". It must
# reach EVERY agent's inbox, and each agent acks it INDEPENDENTLY: one agent's
# inbox_ack must not remove it from another agent's inbox. This is the durable
# "tell every agent X" primitive (e.g. "update fulcra-coord when main changes").
# ---------------------------------------------------------------------------

class TestBroadcastSentinel(unittest.TestCase):
    """The wildcard assignee "*" matches every agent id via agent_matches."""

    def test_broadcast_constant_is_star(self):
        from fulcra_coord.views import BROADCAST
        self.assertEqual(BROADCAST, "*")

    def test_star_matches_claude_code(self):
        from fulcra_coord.views import agent_matches
        self.assertTrue(agent_matches("claude-code:DeskbookPro:fulcra-coord", "*"))

    def test_star_matches_openclaw(self):
        from fulcra_coord.views import agent_matches
        self.assertTrue(agent_matches("openclaw:host:repo", "*"))

    def test_star_matches_chatgpt(self):
        from fulcra_coord.views import agent_matches
        self.assertTrue(agent_matches("chatgpt:host:repo", "*"))

    def test_star_matches_bare_id(self):
        from fulcra_coord.views import agent_matches
        self.assertTrue(agent_matches("anything", "*"))

    def test_normal_id_still_prefix_only(self):
        # The wildcard must not loosen normal matching: a concrete assignee still
        # matches only by colon-segment prefix, never every agent.
        from fulcra_coord.views import agent_matches
        me = "claude-code:DeskbookPro:fulcra-coord"
        self.assertTrue(agent_matches(me, "claude-code"))
        self.assertFalse(agent_matches(me, "openclaw"))
        self.assertFalse(agent_matches("openclaw:host:repo", "claude-code"))


class TestBroadcastInboxForEveryAgent(unittest.TestCase):
    """A broadcast directive appears in inbox_for for every distinct identity."""

    THREE = ("claude-code:Desk:proj", "openclaw:host:repo", "chatgpt:host:repo")

    def test_broadcast_visible_to_all_three(self):
        from fulcra_coord.views import inbox_for, BROADCAST
        d = _directive(BROADCAST, owner="boss")
        for me in self.THREE:
            ids = [s["id"] for s in inbox_for(me, [d])]
            self.assertIn(d["id"], ids, f"broadcast not in {me} inbox")

    def test_per_agent_independent_ack(self):
        # claude acks -> still open for openclaw and chatgpt. Acks are per-`by`,
        # so one agent acking must not clear the broadcast for the others.
        from fulcra_coord.views import inbox_for, BROADCAST
        d = _directive(BROADCAST, owner="boss")
        d["events"].append({"at": "2026-06-02T00:00:00Z", "type": "inbox_ack",
                            "by": "claude-code:Desk:proj", "summary": "seen",
                            "evidence": None})
        self.assertEqual(inbox_for("claude-code:Desk:proj", [d]), [],
                         "acking agent must no longer see the broadcast")
        for me in ("openclaw:host:repo", "chatgpt:host:repo"):
            ids = [s["id"] for s in inbox_for(me, [d])]
            self.assertIn(d["id"], ids,
                          f"{me} must still see the broadcast after another agent acked")

    def test_broadcast_slug_is_human_legible(self):
        # M3: agent_slug("*") would strip to empty and fall back to the opaque
        # "agent"; special-case it to "broadcast" so views/inbox/broadcast.json
        # and index.counts.inbox are legible. The literal "*" never hits a path.
        from fulcra_coord.views import agent_slug, BROADCAST
        self.assertEqual(agent_slug(BROADCAST), "broadcast")

    def test_build_inbox_buckets_broadcast_under_broadcast_slug(self):
        from fulcra_coord.views import build_inbox, BROADCAST
        d = _directive(BROADCAST, owner="boss")
        inbox = build_inbox([d])
        self.assertIn("broadcast", inbox)
        self.assertNotIn("agent", inbox)
        ids = [s["id"] for s in inbox["broadcast"]]
        self.assertIn(d["id"], ids)

    def test_is_open_directive_per_agent_for_broadcast(self):
        # is_open_directive checks "open for THIS querying agent": acked-by-me
        # clears it for me only. (assignee here is the querying agent we test for.)
        from fulcra_coord.views import is_open_directive, BROADCAST
        d = _directive(BROADCAST, owner="boss")
        # Open for the broadcast assignee itself (no ack yet).
        self.assertTrue(is_open_directive(d, BROADCAST))

    def test_broadcast_not_cleared_for_nonowner(self):
        # owner!=me logic must not wrongly clear a broadcast for non-owners. The
        # broadcast is owned by "boss"; openclaw is neither owner nor assignee-equal.
        from fulcra_coord.views import inbox_for, BROADCAST
        d = _directive(BROADCAST, owner="boss")
        ids = [s["id"] for s in inbox_for("openclaw:host:repo", [d])]
        self.assertIn(d["id"], ids)

    def test_owner_of_broadcast_does_not_see_own(self):
        # The directing agent (owner) should not see its own broadcast as an
        # inbound directive — that's its own work, mirroring the tell semantics.
        from fulcra_coord.views import inbox_for, BROADCAST
        d = _directive(BROADCAST, owner="boss:h:r")
        ids = [s["id"] for s in inbox_for("boss:h:r", [d])]
        self.assertNotIn(d["id"], ids)


class TestBroadcastCommand(unittest.TestCase):
    """`fulcra-coord broadcast <title>` creates an assignee="*" proposed directive."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp, "config")
        os.environ.pop("FULCRA_COORD_AGENT", None)
        self.fake_backend = ["false"]

    def tearDown(self):
        os.environ.pop("XDG_CACHE_HOME", None)
        os.environ.pop("XDG_CONFIG_HOME", None)
        os.environ.pop("FULCRA_COORD_AGENT", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ns(self, **kw):
        return types.SimpleNamespace(**kw)

    def test_broadcast_creates_star_directive(self):
        from fulcra_coord.cli import cmd_broadcast
        import io, contextlib
        args = self._ns(title="Update fulcra-coord when main changes",
                        workstream="general", priority="P1",
                        next="rebase + retest", summary="durable team rule",
                        **{"from": "claude-code:h:r"})
        # The ["false"] backend always fails the upload (rc=1), but the directive
        # is created and cached locally — assert on the cached task, not rc, exactly
        # as TestTellAssignInbox does for the one-agent `tell`.
        with contextlib.redirect_stdout(io.StringIO()):
            cmd_broadcast(args, backend=self.fake_backend)
        tasks = cache.list_cached_tasks()
        d = [t for t in tasks
             if t["title"] == "Update fulcra-coord when main changes"][0]
        self.assertEqual(d["assignee"], "*")
        self.assertEqual(d["owner_agent"], "claude-code:h:r")
        self.assertEqual(d["status"], "proposed")

    def test_broadcast_in_command_map(self):
        from fulcra_coord.entry import COMMAND_MAP, build_parser
        self.assertIn("broadcast", COMMAND_MAP)
        # Parser must accept `broadcast <title>` with the optional flags.
        p = build_parser()
        ns = p.parse_args(["broadcast", "do the thing", "--from", "boss",
                           "--priority", "P0", "--next", "now", "--workstream",
                           "ops", "--summary", "why"])
        self.assertEqual(ns.command, "broadcast")
        self.assertEqual(ns.title, "do the thing")
        self.assertEqual(getattr(ns, "from"), "boss")


class TestBroadcastEndToEnd(unittest.TestCase):
    """Full loop over a stateful fake backend: broadcast -> 3 identities each see
    it via `inbox` -> each acks -> each sees empty while the others still see it.
    A normal one-agent `tell` is unaffected by the broadcast machinery."""

    THREE = ("claude-code:Desk:proj", "openclaw:host:repo", "chatgpt:host:repo")

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fake_root = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp, "config")
        os.environ["FULCRA_FAKE_ROOT"] = self.fake_root
        os.environ.pop("FULCRA_COORD_AGENT", None)
        backend_script = str(Path(__file__).resolve().parent / "fake_fulcra_backend.py")
        self.fake_backend = [sys.executable, backend_script]

    def tearDown(self):
        for k in ("XDG_CACHE_HOME", "XDG_CONFIG_HOME", "FULCRA_FAKE_ROOT",
                  "FULCRA_COORD_AGENT"):
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.fake_root, ignore_errors=True)

    def _ns(self, **kw):
        return types.SimpleNamespace(**kw)

    def _inbox_ids(self, me):
        from fulcra_coord.cli import cmd_inbox
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_inbox(self._ns(agent=me, format="json", ack=None),
                      backend=self.fake_backend)
        return [i["id"] for i in json.loads(buf.getvalue())["inbox"]]

    def test_broadcast_seen_then_independently_acked(self):
        from fulcra_coord.cli import cmd_broadcast, cmd_inbox
        import io, contextlib
        b = self._ns(title="team rule", workstream="general", priority="P1",
                     next="", summary="", **{"from": "boss:h:r"})
        with contextlib.redirect_stdout(io.StringIO()):
            cmd_broadcast(b, backend=self.fake_backend)

        # All three see the same broadcast.
        bid = self._inbox_ids(self.THREE[0])[0]
        for me in self.THREE:
            self.assertIn(bid, self._inbox_ids(me), f"{me} should see broadcast")

        # claude acks -> gone for claude, still present for the other two.
        with contextlib.redirect_stdout(io.StringIO()):
            cmd_inbox(self._ns(agent=self.THREE[0], format="json", ack=bid),
                      backend=self.fake_backend)
        self.assertNotIn(bid, self._inbox_ids(self.THREE[0]))
        self.assertIn(bid, self._inbox_ids(self.THREE[1]))
        self.assertIn(bid, self._inbox_ids(self.THREE[2]))

        # openclaw acks -> gone for openclaw, still present for chatgpt.
        with contextlib.redirect_stdout(io.StringIO()):
            cmd_inbox(self._ns(agent=self.THREE[1], format="json", ack=bid),
                      backend=self.fake_backend)
        self.assertNotIn(bid, self._inbox_ids(self.THREE[1]))
        self.assertIn(bid, self._inbox_ids(self.THREE[2]))

        # chatgpt acks -> gone for everyone.
        with contextlib.redirect_stdout(io.StringIO()):
            cmd_inbox(self._ns(agent=self.THREE[2], format="json", ack=bid),
                      backend=self.fake_backend)
        for me in self.THREE:
            self.assertNotIn(bid, self._inbox_ids(me))

    def test_normal_tell_unaffected_by_broadcast(self):
        from fulcra_coord.cli import cmd_broadcast, cmd_tell
        import io, contextlib
        # A broadcast to all + a targeted tell to openclaw only.
        with contextlib.redirect_stdout(io.StringIO()):
            cmd_broadcast(self._ns(title="all-rule", workstream="general",
                                   priority="P2", next="", summary="",
                                   **{"from": "boss:h:r"}),
                          backend=self.fake_backend)
            cmd_tell(self._ns(assignee="openclaw:host:repo", title="just-you",
                              workstream="general", priority="P2", next="",
                              summary="", **{"from": "boss:h:r"}),
                     backend=self.fake_backend)

        # openclaw sees both; chatgpt sees only the broadcast.
        oc_titles = self._titles(self.THREE[1])
        self.assertIn("all-rule", oc_titles)
        self.assertIn("just-you", oc_titles)
        cg_titles = self._titles(self.THREE[2])
        self.assertIn("all-rule", cg_titles)
        self.assertNotIn("just-you", cg_titles)

    def _titles(self, me):
        from fulcra_coord.cli import cmd_inbox
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_inbox(self._ns(agent=me, format="json", ack=None),
                      backend=self.fake_backend)
        return [i["title"] for i in json.loads(buf.getvalue())["inbox"]]


# ---------------------------------------------------------------------------
# Situational awareness — Piece 3: blocked-on-you (block --on-user + needs-me)
# ---------------------------------------------------------------------------

class TestNeedsHumanView(unittest.TestCase):
    """views.needs_human: every OPEN task (proposed/waiting/blocked) whose
    assignee matches the human (prefix-aware), across all owners."""

    def _t(self, tid, assignee, status="blocked", owner="claude-code:h:r",
           blocked_on=None, next_action="", updated="2026-06-01T00:00:00Z"):
        from fulcra_coord import schema
        t = schema.make_task(title=tid, workstream="general", agent=owner,
                             owner_agent=owner, assignee=assignee)
        t["status"] = status
        t["blocked_on"] = blocked_on
        t["next_action"] = next_action
        t["updated_at"] = updated
        return t

    def test_collects_blocked_on_human(self):
        from fulcra_coord.views import needs_human
        tasks = [
            self._t("a", "human", blocked_on="review the PR"),
            self._t("b", "claude-code:h:r", status="active"),  # not for human
        ]
        out = needs_human(tasks, "human")
        self.assertEqual([s["title"] for s in out], ["a"])

    def test_ash_matches_human_via_prefix_when_assignee_is_ash(self):
        # assignee "ash" reached by querying human "ash".
        from fulcra_coord.views import needs_human
        tasks = [self._t("a", "ash", blocked_on="x")]
        self.assertEqual(len(needs_human(tasks, "ash")), 1)

    def test_broadcast_is_not_blocked_on_human(self):
        # A broadcast (assignee="*") reaches every agent's inbox, but an
        # all-agent announcement is NOT a personal ask blocked on the human.
        # The "blocked on YOU" plate must be precise — only tasks SPECIFICALLY
        # directed at the human (concrete assignee) or tagged needs:human count.
        # Otherwise every join-announcement broadcast floods the SessionStart
        # ⛔ banner and buries the real asks (defeats the whole feature).
        from fulcra_coord.views import needs_human
        tasks = [
            self._t("broadcast-noise", "*", blocked_on="fyi to everyone"),
            self._t("real-ask", "human", blocked_on="review the PR"),
        ]
        out = needs_human(tasks, "human")
        self.assertEqual([s["title"] for s in out], ["real-ask"])

    def test_needs_human_tag_counts_even_if_assignee_drifts(self):
        # A task carrying the needs:human tag (set by `block --on-user`) is on
        # the human's plate regardless of how assignee was later edited.
        from fulcra_coord.views import needs_human
        t = self._t("tagged", "*", blocked_on="do the thing")
        t["tags"] = ["needs:human", "agent-tasks"]
        self.assertEqual([s["title"] for s in needs_human([t], "human")],
                         ["tagged"])

    def test_only_open_statuses(self):
        from fulcra_coord.views import needs_human
        tasks = [
            self._t("done-one", "human", status="done"),
            self._t("active-one", "human", status="active"),
            self._t("blocked-one", "human", status="blocked"),
            self._t("waiting-one", "human", status="waiting"),
            self._t("proposed-one", "human", status="proposed"),
        ]
        titles = {s["title"] for s in needs_human(tasks, "human")}
        self.assertEqual(titles, {"blocked-one", "waiting-one", "proposed-one"})

    def test_sorted_by_age_oldest_first(self):
        from fulcra_coord.views import needs_human
        tasks = [
            self._t("newer", "human", updated="2026-06-02T00:00:00Z"),
            self._t("older", "human", updated="2026-06-01T00:00:00Z"),
        ]
        self.assertEqual([s["title"] for s in needs_human(tasks, "human")],
                         ["older", "newer"])

    def test_aggregates_across_multiple_owners(self):
        from fulcra_coord.views import needs_human
        tasks = [
            self._t("from-a", "human", owner="claude-code:h:a"),
            self._t("from-b", "human", owner="claude-code:h:b"),
        ]
        owners = {s["owner_agent"] for s in needs_human(tasks, "human")}
        self.assertEqual(owners, {"claude-code:h:a", "claude-code:h:b"})


class TestBlockOnUser(unittest.TestCase):
    """`block --on-user` marks blocked, sets blocked_on + assignee=human +
    needs:human tag; `needs-me` then lists it; a non-human inbox excludes it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp, "config")
        os.environ.pop("FULCRA_COORD_AGENT", None)
        os.environ.pop("FULCRA_COORD_HUMAN", None)
        os.environ.pop("FULCRA_COORD_ANNOTATIONS", None)
        self.fake_backend = ["false"]

    def tearDown(self):
        for k in ("XDG_CACHE_HOME", "XDG_CONFIG_HOME", "FULCRA_COORD_AGENT",
                  "FULCRA_COORD_HUMAN", "FULCRA_COORD_ANNOTATIONS"):
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ns(self, **kw):
        return types.SimpleNamespace(**kw)

    def _make_active(self, title, owner="claude-code:host:repo"):
        from fulcra_coord.cli import cmd_start
        cmd_start(self._ns(title=title, workstream="devops", agent=owner,
                           kind="ops", priority="P2", summary="", next="",
                           surface=None), backend=self.fake_backend)
        task = next(t for t in cache.list_cached_tasks() if t["title"] == title)
        active = apply_transition(task, "active", by=owner)
        cache.write_cached_task(active)
        return task["id"]

    def test_block_on_user_sets_assignee_and_tag(self):
        from fulcra_coord.cli import cmd_block
        tid = self._make_active("needs-review")
        cmd_block(self._ns(task_id=tid, blocked_on=None, on_user="approve the deploy",
                           agent=None), backend=self.fake_backend)
        t = cache.read_cached_task(tid)
        self.assertEqual(t["status"], "blocked")
        self.assertEqual(t["blocked_on"], "approve the deploy")
        self.assertEqual(t["assignee"], "human")
        self.assertIn("needs:human", t["tags"])

    def test_block_on_user_honors_resolved_human(self):
        from fulcra_coord.cli import cmd_block
        os.environ["FULCRA_COORD_HUMAN"] = "ash"
        tid = self._make_active("needs-ash")
        cmd_block(self._ns(task_id=tid, blocked_on=None, on_user="decide X",
                           agent=None), backend=self.fake_backend)
        self.assertEqual(cache.read_cached_task(tid)["assignee"], "ash")

    def test_needs_me_lists_block_on_user_item(self):
        from fulcra_coord.cli import cmd_block, cmd_needs_me
        import io, contextlib
        tid = self._make_active("review-me")
        cmd_block(self._ns(task_id=tid, blocked_on=None, on_user="look at it",
                           agent=None), backend=self.fake_backend)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_needs_me(self._ns(human=None, format="json"),
                         backend=self.fake_backend)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["human"], "human")
        titles = [i["title"] for i in out["items"]]
        self.assertIn("review-me", titles)

    def test_needs_me_resolves_ash_and_human_both(self):
        from fulcra_coord.cli import cmd_block, cmd_needs_me
        import io, contextlib
        os.environ["FULCRA_COORD_HUMAN"] = "ash"
        tid = self._make_active("ash-item")
        cmd_block(self._ns(task_id=tid, blocked_on=None, on_user="ash decides",
                           agent=None), backend=self.fake_backend)
        # Query with explicit --human ash
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_needs_me(self._ns(human="ash", format="json"),
                         backend=self.fake_backend)
        out = json.loads(buf.getvalue())
        self.assertIn("ash-item", [i["title"] for i in out["items"]])

    def test_non_human_inbox_excludes_block_on_user(self):
        from fulcra_coord.cli import cmd_block, cmd_inbox
        import io, contextlib
        tid = self._make_active("human-only")
        cmd_block(self._ns(task_id=tid, blocked_on=None, on_user="human does it",
                           agent=None), backend=self.fake_backend)
        # A regular agent's inbox must not show a needs:human directive
        # (it's blocked, not proposed/waiting, and owned by the agent itself).
        inbox_args = self._ns(agent="claude-code:other:repo", format="json", ack=None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_inbox(inbox_args, backend=self.fake_backend)
        self.assertEqual(json.loads(buf.getvalue())["inbox"], [])

    def test_block_blocked_on_still_works(self):
        # The existing agent-blocker path is preserved.
        from fulcra_coord.cli import cmd_block
        tid = self._make_active("agent-blocked")
        cmd_block(self._ns(task_id=tid, blocked_on="waiting on CI", on_user=None,
                           agent=None), backend=self.fake_backend)
        t = cache.read_cached_task(tid)
        self.assertEqual(t["blocked_on"], "waiting on CI")
        self.assertIsNone(t.get("assignee"))
        self.assertNotIn("needs:human", t["tags"])

    def test_block_on_user_emits_needs_user_annotation_when_gated(self):
        # Uses the STATEFUL fake backend so the block fully succeeds and the
        # post-success annotation hook fires (the ["false"] backend would fail
        # the upload and short-circuit before the annotation).
        from fulcra_coord import cli
        from unittest.mock import patch
        backend = [sys.executable, str(Path(__file__).resolve().parent
                                       / "fake_fulcra_backend.py")]
        fake_root = tempfile.mkdtemp()
        os.environ["FULCRA_FAKE_ROOT"] = fake_root
        try:
            cli.cmd_start(self._ns(title="annotate-me", workstream="devops",
                                   agent="claude-code:host:repo", kind="ops",
                                   priority="P2", summary="", next="", surface=None),
                          backend=backend)
            tid = next(t for t in cache.list_cached_tasks()
                       if t["title"] == "annotate-me")["id"]
            cli.cmd_update(self._ns(task_id=tid, summary=None, next=None,
                                    blocked_on=None, status="active", agent=None),
                           backend=backend)
            with patch.object(cli.lifecycle_annotations,
                              "emit_needs_user_annotation") as emit:
                cli.cmd_block(self._ns(task_id=tid, blocked_on=None,
                                       on_user="approve it", agent=None),
                              backend=backend)
            emit.assert_called_once()
            self.assertEqual(emit.call_args.kwargs["task"]["blocked_on"], "approve it")
        finally:
            os.environ.pop("FULCRA_FAKE_ROOT", None)
            shutil.rmtree(fake_root, ignore_errors=True)

    def test_block_without_on_user_does_not_emit_needs_user(self):
        from fulcra_coord import cli
        from unittest.mock import patch
        backend = [sys.executable, str(Path(__file__).resolve().parent
                                       / "fake_fulcra_backend.py")]
        fake_root = tempfile.mkdtemp()
        os.environ["FULCRA_FAKE_ROOT"] = fake_root
        try:
            cli.cmd_start(self._ns(title="no-annotate", workstream="devops",
                                   agent="claude-code:host:repo", kind="ops",
                                   priority="P2", summary="", next="", surface=None),
                          backend=backend)
            tid = next(t for t in cache.list_cached_tasks()
                       if t["title"] == "no-annotate")["id"]
            cli.cmd_update(self._ns(task_id=tid, summary=None, next=None,
                                    blocked_on=None, status="active", agent=None),
                           backend=backend)
            with patch.object(cli.lifecycle_annotations,
                              "emit_needs_user_annotation") as emit:
                cli.cmd_block(self._ns(task_id=tid, blocked_on="CI", on_user=None,
                                       agent=None), backend=backend)
            emit.assert_not_called()
        finally:
            os.environ.pop("FULCRA_FAKE_ROOT", None)
            shutil.rmtree(fake_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Situational awareness — Piece 2: per-cwd identity (fix the global clobber)
# ---------------------------------------------------------------------------

class TestPerCwdIdentity(unittest.TestCase):
    """The persisted identity is scoped PER WORKING DIRECTORY, fixing the bug
    where a sibling session's `identity set` in one repo clobbered another's.
    Two cwds hold distinct identities; setting in one never affects the other."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp, "config")
        os.environ.pop("FULCRA_COORD_AGENT", None)
        self.cwd_a = os.path.join(self.tmp, "repo-a")
        self.cwd_b = os.path.join(self.tmp, "repo-b")
        os.makedirs(self.cwd_a)
        os.makedirs(self.cwd_b)
        self._orig_cwd = os.getcwd()

    def tearDown(self):
        os.chdir(self._orig_cwd)
        os.environ.pop("XDG_CONFIG_HOME", None)
        os.environ.pop("FULCRA_COORD_AGENT", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_two_cwds_hold_distinct_identities(self):
        from fulcra_coord import identity
        os.chdir(self.cwd_a)
        identity.set_identity("claude-code:host:repo-a")
        os.chdir(self.cwd_b)
        identity.set_identity("claude-code:host:repo-b")
        os.chdir(self.cwd_a)
        self.assertEqual(identity.resolve_agent(), "claude-code:host:repo-a")
        os.chdir(self.cwd_b)
        self.assertEqual(identity.resolve_agent(), "claude-code:host:repo-b")

    def test_set_in_a_does_not_change_b(self):
        from fulcra_coord import identity
        os.chdir(self.cwd_b)
        identity.set_identity("claude-code:host:repo-b")
        os.chdir(self.cwd_a)
        identity.set_identity("claude-code:host:repo-a")
        # B is untouched.
        os.chdir(self.cwd_b)
        self.assertEqual(identity.read_identity(), "claude-code:host:repo-b")

    def test_clear_is_per_cwd(self):
        from fulcra_coord import identity
        os.chdir(self.cwd_a)
        identity.set_identity("a:agent")
        os.chdir(self.cwd_b)
        identity.set_identity("b:agent")
        os.chdir(self.cwd_a)
        identity.clear_identity()
        self.assertIsNone(identity.read_identity())
        # B still resolves.
        os.chdir(self.cwd_b)
        self.assertEqual(identity.read_identity(), "b:agent")

    def test_identity_path_is_per_cwd_distinct(self):
        from fulcra_coord import identity
        os.chdir(self.cwd_a)
        pa = identity.identity_path()
        os.chdir(self.cwd_b)
        pb = identity.identity_path()
        self.assertNotEqual(pa, pb)
        self.assertIn("identities", str(pa))

    def test_legacy_global_does_not_resolve(self):
        from fulcra_coord import identity
        # Simulate an existing pre-per-cwd setup: a legacy global identity.json.
        # I-1: the legacy global must NOT silently resolve for an un-set cwd —
        # a stale/clobbered global (e.g. another tool's id) would otherwise leak
        # in as the identity for EVERY repo. The safe derived id is used instead.
        legacy = identity.config_root() / "identity.json"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(json.dumps({"agent": "legacy:agent"}))
        os.chdir(self.cwd_a)
        # No per-cwd entry -> the legacy global is IGNORED for resolution.
        self.assertIsNone(identity.read_identity())
        agent, source = identity.resolve_agent_source()
        self.assertNotEqual(agent, "legacy:agent")
        self.assertEqual(source, "derived")

    def test_legacy_global_still_readable_for_hint(self):
        from fulcra_coord import identity
        # The legacy file is kept readable ONLY to surface a migration hint; it
        # never feeds resolution. read_legacy_identity exposes it for that note.
        legacy = identity.config_root() / "identity.json"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(json.dumps({"agent": "legacy:agent"}))
        os.chdir(self.cwd_a)
        self.assertEqual(identity.read_legacy_identity(), "legacy:agent")

    def test_per_cwd_resolves_over_legacy_global(self):
        from fulcra_coord import identity
        legacy = identity.config_root() / "identity.json"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(json.dumps({"agent": "legacy:agent"}))
        os.chdir(self.cwd_a)
        identity.set_identity("percwd:agent")
        self.assertEqual(identity.read_identity(), "percwd:agent")
        self.assertEqual(identity.resolve_agent(), "percwd:agent")

    def test_precedence_env_beats_per_cwd(self):
        from fulcra_coord import identity
        os.chdir(self.cwd_a)
        identity.set_identity("percwd:agent")
        os.environ["FULCRA_COORD_AGENT"] = "env:agent"
        self.assertEqual(identity.resolve_agent(), "env:agent")

    def test_symlinked_path_shares_identity_with_realpath(self):
        # M-3: _cwd_hash uses realpath, so entering a repo via a symlink resolves
        # the SAME identity entry as the canonical path. Set under the real path,
        # read back via the symlink.
        from fulcra_coord import identity
        link = os.path.join(self.tmp, "repo-a-link")
        os.symlink(self.cwd_a, link)
        os.chdir(self.cwd_a)
        identity.set_identity("real:agent")
        os.chdir(link)
        self.assertEqual(identity.read_identity(), "real:agent")
        self.assertEqual(identity.identity_path(self.cwd_a),
                         identity.identity_path(link))


# ---------------------------------------------------------------------------
# Situational awareness — Piece 7: resume (pick-up-where-you-left-off briefing)
# ---------------------------------------------------------------------------

class TestResume(unittest.TestCase):
    """`resume` builds a read-only four-section briefing for an agent: your
    active/waiting work, what's blocked on you, what you owe others, and what's
    blocked on the human."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp, "config")
        os.environ["FULCRA_COORD_HUMAN"] = "ash"
        os.environ.pop("FULCRA_COORD_AGENT", None)
        self.fake_backend = ["false"]
        self.me = "claude-code:host:repo"
        self._seed()

    def tearDown(self):
        for k in ("XDG_CACHE_HOME", "XDG_CONFIG_HOME", "FULCRA_COORD_HUMAN",
                  "FULCRA_COORD_AGENT"):
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ns(self, **kw):
        return types.SimpleNamespace(**kw)

    def _task(self, tid, *, owner, assignee=None, status="active", na=""):
        from fulcra_coord import schema
        t = schema.make_task(title=tid, workstream="general", agent=owner,
                             owner_agent=owner, assignee=assignee)
        t["id"] = tid
        t["status"] = status
        t["next_action"] = na
        return t

    def _seed(self):
        # (a) my active task
        cache.write_cached_task(self._task("TASK-mine-active", owner=self.me,
                                           status="active", na="finish X"))
        # (b) blocked on me: someone else owns it, assigned to me, open
        cache.write_cached_task(self._task("TASK-blocked-on-me", owner="other:a:b",
                                           assignee=self.me, status="waiting",
                                           na="need your input"))
        # (c) I owe others: I own/created it, assigned to someone else, open
        cache.write_cached_task(self._task("TASK-i-owe", owner=self.me,
                                           assignee="other:c:d", status="proposed",
                                           na="do the thing"))
        # (d) blocked on the human (ash)
        cache.write_cached_task(self._task("TASK-on-human", owner="other:e:f",
                                           assignee="ash", status="blocked",
                                           na="approve"))

    def _run(self, **kw):
        import io, contextlib
        from fulcra_coord.cli import cmd_resume
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_resume(self._ns(format="json", **kw), backend=self.fake_backend)
        return json.loads(buf.getvalue())

    def test_my_active_section(self):
        out = self._run(agent=self.me)
        self.assertEqual(out["agent"], self.me)
        ids = [t["id"] for t in out["active"]]
        self.assertIn("TASK-mine-active", ids)

    def test_blocked_on_me_section(self):
        out = self._run(agent=self.me)
        ids = [t["id"] for t in out["blocked_on_me"]]
        self.assertIn("TASK-blocked-on-me", ids)
        self.assertNotIn("TASK-mine-active", ids)

    def test_i_owe_others_section(self):
        out = self._run(agent=self.me)
        ids = [t["id"] for t in out["owed_to_others"]]
        self.assertIn("TASK-i-owe", ids)
        # Not my own active work, not blocked-on-me.
        self.assertNotIn("TASK-mine-active", ids)
        self.assertNotIn("TASK-blocked-on-me", ids)

    def test_broadcast_not_in_blocked_on_me(self):
        # A broadcast (assignee="*") owned by another agent reaches my inbox but
        # is an all-agent announcement, not work PARKED on me. It must NOT pad
        # the resume "blocked on me" section (parity with needs_human; visible
        # via `inbox` instead). Otherwise join-announcements bury real asks.
        cache.write_cached_task(self._task("TASK-broadcast-noise", owner="other:x:y",
                                            assignee="*", status="proposed",
                                            na="fyi everyone"))
        out = self._run(agent=self.me)
        ids = [t["id"] for t in out["blocked_on_me"]]
        self.assertNotIn("TASK-broadcast-noise", ids)
        self.assertIn("TASK-blocked-on-me", ids)  # concrete directive still shows

    def test_blocked_on_human_section(self):
        out = self._run(agent=self.me)
        ids = [t["id"] for t in out["blocked_on_human"]]
        self.assertIn("TASK-on-human", ids)

    def test_resolves_explicit_agent(self):
        # A different agent sees a different briefing.
        out = self._run(agent="other:a:b")
        # other:a:b owns TASK-blocked-on-me (assigned to me) -> it's in owed_to_others.
        self.assertIn("TASK-blocked-on-me", [t["id"] for t in out["owed_to_others"]])

    def test_self_filed_on_human_task_not_double_listed(self):
        # M-2: a task I own that is assigned to the human is "blocked on human";
        # it must NOT also appear under "owed to others" (it's the same task, and
        # listing it twice double-counts what I owe).
        cache.write_cached_task(self._task("TASK-i-owe-human", owner=self.me,
                                            assignee="ash", status="blocked",
                                            na="ping ash"))
        out = self._run(agent=self.me)
        owed = [t["id"] for t in out["owed_to_others"]]
        on_human = [t["id"] for t in out["blocked_on_human"]]
        self.assertIn("TASK-i-owe-human", on_human)
        self.assertNotIn("TASK-i-owe-human", owed)


# ---------------------------------------------------------------------------
# Situational awareness — Piece 1: human handle (resolve_human + `human` cmd)
# ---------------------------------------------------------------------------

class TestResolveHuman(unittest.TestCase):
    """resolve_human order: FULCRA_COORD_HUMAN env > persisted human file >
    default 'human'. Config isolated via XDG_CONFIG_HOME."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp, "config")
        os.environ.pop("FULCRA_COORD_HUMAN", None)

    def tearDown(self):
        os.environ.pop("XDG_CONFIG_HOME", None)
        os.environ.pop("FULCRA_COORD_HUMAN", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_is_human(self):
        from fulcra_coord import identity
        self.assertEqual(identity.resolve_human(), "human")
        self.assertEqual(identity.resolve_human_source(), ("human", "default"))

    def test_env_wins_over_default(self):
        from fulcra_coord import identity
        os.environ["FULCRA_COORD_HUMAN"] = "ash"
        self.assertEqual(identity.resolve_human(), "ash")
        self.assertEqual(identity.resolve_human_source()[1], "env")

    def test_env_wins_over_config(self):
        from fulcra_coord import identity
        identity.set_human("file-ash")
        os.environ["FULCRA_COORD_HUMAN"] = "ash"
        self.assertEqual(identity.resolve_human(), "ash")

    def test_config_wins_over_default(self):
        from fulcra_coord import identity
        identity.set_human("ash")
        self.assertEqual(identity.resolve_human(), "ash")
        self.assertEqual(identity.resolve_human_source(), ("ash", "config"))

    def test_set_then_clear_reverts_to_default(self):
        from fulcra_coord import identity
        identity.set_human("ash")
        self.assertTrue(identity.clear_human())
        self.assertEqual(identity.resolve_human(), "human")

    def test_human_path_under_xdg(self):
        from fulcra_coord import identity
        identity.set_human("ash")
        self.assertTrue(str(identity.human_path()).startswith(
            os.path.join(self.tmp, "config")))


class TestHumanCommand(unittest.TestCase):
    """`fulcra-coord human` show/set/clear."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp, "config")
        os.environ.pop("FULCRA_COORD_HUMAN", None)

    def tearDown(self):
        os.environ.pop("XDG_CONFIG_HOME", None)
        os.environ.pop("FULCRA_COORD_HUMAN", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, **kw):
        import io, contextlib
        from fulcra_coord.cli import cmd_human
        args = types.SimpleNamespace(format="json", **kw)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cmd_human(args)
        return rc, buf.getvalue()

    def test_default_show(self):
        rc, out = self._run(human_action=None, handle=None)
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["human"], "human")
        self.assertEqual(data["source"], "default")

    def test_set_then_show(self):
        rc, _ = self._run(human_action="set", handle="ash")
        self.assertEqual(rc, 0)
        rc, out = self._run(human_action=None, handle=None)
        data = json.loads(out)
        self.assertEqual(data["human"], "ash")
        self.assertEqual(data["source"], "config")

    def test_clear_reverts_to_default(self):
        self._run(human_action="set", handle="ash")
        rc, _ = self._run(human_action="clear", handle=None)
        self.assertEqual(rc, 0)
        rc, out = self._run(human_action=None, handle=None)
        self.assertEqual(json.loads(out)["source"], "default")


# ---------------------------------------------------------------------------
# Versioning + capabilities probe (ArcBot-2 feedback)
# ---------------------------------------------------------------------------

class TestVersionFlag(unittest.TestCase):
    """`--version` prints the real package version and exits 0 — even though a
    subcommand is otherwise required (argparse's version action fires first)."""

    def test_version_flag_prints_version_and_exits_zero(self):
        import io, contextlib
        from fulcra_coord.entry import build_parser
        from fulcra_coord import __version__
        buf = io.StringIO()
        with self.assertRaises(SystemExit) as cm, contextlib.redirect_stdout(buf):
            build_parser().parse_args(["--version"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn(__version__, buf.getvalue())
        self.assertIn("fulcra-coord", buf.getvalue())

    def test_version_is_not_the_frozen_placeholder(self):
        # ArcBot-2: the CLI was "stuck at v0.1.0 across breaking subcommand
        # additions." Guard that the surface has actually moved past it.
        from fulcra_coord import __version__
        self.assertNotEqual(__version__, "0.1.0")


class TestCapabilitiesProbe(unittest.TestCase):
    """`capabilities` reports the version + supported commands so onboarding can
    detect a stale install before invoking a missing subcommand."""

    def _run(self, fmt="json"):
        import io, contextlib
        from fulcra_coord.cli import cmd_capabilities
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_capabilities(types.SimpleNamespace(format=fmt))
        return buf.getvalue()

    def test_json_lists_situational_awareness_commands(self):
        from fulcra_coord import __version__
        data = json.loads(self._run("json"))
        self.assertEqual(data["version"], __version__)
        self.assertEqual(data["name"], "fulcra-coord")
        # The commands the onboarding docs rely on must be advertised.
        for cmd in ("needs-me", "resume", "human", "block", "capabilities"):
            self.assertIn(cmd, data["commands"])

    def test_hidden_hook_command_is_not_advertised(self):
        # __session-task is a hook-only internal, not part of the public probe.
        data = json.loads(self._run("json"))
        self.assertNotIn("__session-task", data["commands"])

    def test_commands_match_dispatch_table(self):
        # The probe must never claim a command main can't actually route.
        from fulcra_coord.entry import COMMAND_MAP
        data = json.loads(self._run("json"))
        expected = sorted(k for k in COMMAND_MAP if not k.startswith("__"))
        self.assertEqual(data["commands"], expected)

    def test_table_format_human_readable(self):
        from fulcra_coord import __version__
        out = self._run("table")
        self.assertIn(__version__, out)
        self.assertIn("needs-me", out)


# ---------------------------------------------------------------------------
# Summaries aggregate + summary-sourced views (performance refactor)
# ---------------------------------------------------------------------------

def _make_representative_tasks() -> list[dict]:
    """Tasks spanning every status, with the fields that distinguish full
    bodies from summaries: last_touched_by != owner_agent, done.done_at both
    inside and outside the search/recently-done cutoffs, assignees, tags.

    Used by the equivalence test (the linchpin proving build_all_views gives
    identical output from full bodies vs task_summary dicts) and by
    build_summaries shape tests.
    """
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    recent_iso = (now - timedelta(days=2)).isoformat().replace("+00:00", "Z")
    old_iso = (now - timedelta(days=40)).isoformat().replace("+00:00", "Z")

    # active, touched by a different agent than the owner (exercises build_agent_view)
    active = apply_transition(_sample_task(), "active", by="agent-x")
    active["owner_agent"] = "agent-a"
    active["last_touched_by"] = "agent-x"

    # waiting
    waiting = _with_status(_sample_task(), "waiting")
    waiting["title"] = "Parked task"
    waiting["workstream"] = "research"

    # blocked, assigned to a human (needs:human path)
    blocked = _with_status(_sample_task(), "blocked")
    blocked["title"] = "Blocked on human"
    blocked["owner_agent"] = "agent-b"
    blocked["assignee"] = "ash"
    blocked["blocked_on"] = "need a decision"
    blocked["tags"] = sorted(set(blocked.get("tags", []) + ["needs:human"]))

    # proposed directive addressed to another agent (inbox path)
    proposed = _sample_task()
    proposed["title"] = "Directive to codex"
    proposed["owner_agent"] = "agent-a"
    proposed["assignee"] = "codex"

    # proposed directive that the assignee has ALREADY acked — exercises the
    # acked_by flattening (events live only on a full body; a summary carries the
    # ack set). Without acked_by on the summary, the rebuilt inbox view would
    # re-surface this and break the full-body-vs-summary equivalence.
    acked = _sample_task()
    acked["title"] = "Acked directive"
    acked["owner_agent"] = "agent-a"
    acked["assignee"] = "agent-z"
    acked = schema.apply_event(acked, "inbox_ack", by="agent-z",
                               summary="seen", dt=now)

    # done INSIDE the search-index cutoff (30d) and recently-done (7d) cutoff
    done_recent = _with_status(_sample_task(), "done")
    done_recent["title"] = "Recently shipped"
    done_recent["owner_agent"] = "agent-c"
    done_recent["last_touched_by"] = "agent-d"
    done_recent["updated_at"] = recent_iso
    done_recent["done"] = {
        "done_at": recent_iso, "done_by": "agent-d",
        "evidence": "shipped", "verification_level": "agent-verified",
        "confidence": None,
    }

    # done OUTSIDE both cutoffs (should be excluded from recently-done + search)
    done_old = _with_status(_sample_task(), "done")
    done_old["title"] = "Ancient task"
    done_old["owner_agent"] = "agent-c"
    done_old["updated_at"] = old_iso
    done_old["done"] = {
        "done_at": old_iso, "done_by": "agent-c",
        "evidence": "old", "verification_level": "agent-verified",
        "confidence": None,
    }

    # abandoned recently
    abandoned = _with_status(_sample_task(), "abandoned")
    abandoned["title"] = "Dropped task"
    abandoned["updated_at"] = recent_iso
    abandoned["done"] = {
        "done_at": recent_iso, "done_by": "agent-a",
        "evidence": None, "verification_level": None, "confidence": None,
    }

    return [active, waiting, blocked, proposed, acked, done_recent, done_old, abandoned]


class TestTaskSummaryIdempotence(unittest.TestCase):
    """task_summary must be a fixpoint: feeding a summary back in returns it
    unchanged. This is what makes summaries a faithful re-buildable view source
    (build_all_views over summaries == over full bodies)."""

    def test_idempotent(self):
        from datetime import datetime, timezone, timedelta
        done_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        t = _with_status(_sample_task(), "done")
        t["owner_agent"] = "agent-a"
        t["last_touched_by"] = "agent-b"
        t["assignee"] = "codex"
        t["tags"] = sorted(set(t.get("tags", []) + ["needs:human"]))
        t["updated_at"] = done_at
        t["done"] = {
            "done_at": done_at, "done_by": "agent-b",
            "evidence": "x", "verification_level": "agent-verified", "confidence": None,
        }
        s1 = schema.task_summary(t)
        s2 = schema.task_summary(s1)
        self.assertEqual(s1, s2)

    def test_carries_last_touched_by_and_done_at(self):
        # Pick the done_recent task by identity rather than a brittle index.
        tasks = _make_representative_tasks()
        t = next(x for x in tasks if x["title"] == "Recently shipped")
        s = schema.task_summary(t)
        self.assertEqual(s["last_touched_by"], "agent-d")
        self.assertEqual(s["done_at"], t["done"]["done_at"])


class TestBuildAllViewsEquivalence(unittest.TestCase):
    """THE linchpin: build_all_views must produce IDENTICAL output whether fed
    full task bodies or task_summary dicts. Proves the write path can rebuild
    views from the summaries aggregate without re-fetching task bodies."""

    def test_full_bodies_equal_summaries(self):
        tasks = _make_representative_tasks()
        summaries = [schema.task_summary(t) for t in tasks]
        # Freeze "now" inside build_all_views so the updated_at stamps match
        # across the two calls (they would otherwise differ by microseconds).
        fixed = "2026-06-03T00:00:00Z"
        with patch("fulcra_coord.views._now") as mock_now:
            from datetime import datetime, timezone
            mock_now.return_value = datetime(2026, 6, 3, tzinfo=timezone.utc)
            from_full = build_all_views(tasks)
            from_summaries = build_all_views(summaries)
        self.assertEqual(set(from_full), set(from_summaries),
                         "view name sets differ")
        for name in from_full:
            self.assertEqual(from_full[name], from_summaries[name],
                             f"view {name!r} differs between full bodies and summaries")


class TestBuildSummaries(unittest.TestCase):
    """build_summaries: the read-side aggregate so reads don't fetch bodies."""

    def test_shape_and_membership(self):
        tasks = _make_representative_tasks()
        v = views.build_summaries(tasks)
        self.assertEqual(v["schema"], "fulcra.coordination.summaries.v1")
        self.assertEqual(v["view"], "summaries")
        self.assertIn("updated_at", v)
        self.assertIsInstance(v["summaries"], list)
        # Includes the full passed-in set, one summary per task.
        self.assertEqual(
            {s["id"] for s in v["summaries"]},
            {t["id"] for t in tasks},
        )
        # Each entry is exactly task_summary(t).
        by_id = {s["id"]: s for s in v["summaries"]}
        for t in tasks:
            self.assertEqual(by_id[t["id"]], schema.task_summary(t))

    def test_in_build_all_views(self):
        tasks = _make_representative_tasks()
        all_v = build_all_views(tasks)
        self.assertIn("summaries", all_v)
        self.assertEqual(all_v["summaries"]["view"], "summaries")


class TestLoadTaskSummaries(unittest.TestCase):
    """_load_task_summaries reads views/summaries.json when present, else falls
    back to a full task load (older bus that never wrote the aggregate)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp

    def tearDown(self):
        del os.environ["XDG_CACHE_HOME"]

    def test_reads_present_summaries_view(self):
        from fulcra_coord.cli import _load_task_summaries
        from fulcra_coord import remote
        tasks = _make_representative_tasks()
        summaries_view = views.build_summaries(tasks)
        sum_path = remote.view_remote_path("summaries")

        def fake_download(path, *, backend=None, timeout=None):
            if path == sum_path:
                return summaries_view
            return None

        with patch("fulcra_coord.cli.remote.download_json", side_effect=fake_download):
            got = _load_task_summaries(backend=["false"])
        self.assertEqual({s["id"] for s in got}, {t["id"] for t in tasks})

    def test_falls_back_to_full_load_when_absent(self):
        from fulcra_coord.cli import _load_task_summaries
        tasks = _make_representative_tasks()
        for t in tasks:
            cache.write_cached_task(t)

        # No summaries.json remotely (download returns None for everything).
        with patch("fulcra_coord.cli.remote.download_json", return_value=None):
            got = _load_task_summaries(backend=["false"])
        # Fallback returns task_summary per cached task.
        self.assertEqual({s["id"] for s in got}, {t["id"] for t in tasks})


class TestReadsSourcedFromSummaries(unittest.TestCase):
    """Read commands must answer correctly from a summaries.json without ever
    fetching task bodies. We patch _cache_remote_task to blow up so the test
    fails loudly if a read path tries to fetch a body."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp

    def tearDown(self):
        del os.environ["XDG_CACHE_HOME"]

    def test_needs_me_from_summaries_no_body_fetch(self):
        from fulcra_coord.cli import cmd_needs_me
        from fulcra_coord import remote
        tasks = _make_representative_tasks()  # 'blocked' is assigned to ash
        summaries_view = views.build_summaries(tasks)
        sum_path = remote.view_remote_path("summaries")

        def fake_download(path, *, backend=None, timeout=None):
            if path == sum_path:
                return summaries_view
            return None

        def boom(*a, **k):
            raise AssertionError("read path fetched a task body — should use summaries")

        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with patch("fulcra_coord.cli.remote.download_json", side_effect=fake_download), \
             patch("fulcra_coord.cli._cache_remote_task", side_effect=boom), \
             redirect_stdout(buf):
            args = types.SimpleNamespace(human="ash", format="json")
            rc = cmd_needs_me(args, backend=["false"])
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        ids = {i["id"] for i in data["items"]}
        self.assertIn(tasks[2]["id"], ids)  # the blocked-on-ash task


class TestParallelViewUpload(unittest.TestCase):
    """P1: the parallel view-upload fan-out must preserve exact semantics —
    raise NeedsReconcile when any single view fails, upload all on success."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp

    def tearDown(self):
        del os.environ["XDG_CACHE_HOME"]

    def _setup_remote(self):
        """Make the optimistic-concurrency pre-stat see no remote file (fresh
        write) and the task upload succeed, so we isolate the view fan-out."""
        from fulcra_coord import remote
        task = apply_transition(_sample_task(), "active", by="claude-code")
        cache.write_cached_task(task)
        return task

    def test_raises_needs_reconcile_when_one_view_fails(self):
        from fulcra_coord.cli import _write_task_and_views
        task = self._setup_remote()
        index_path = "/".join([__import__("fulcra_coord").remote.view_remote_path("index")])

        def upload_json(data, path, *, backend=None, timeout=None):
            # Fail exactly one view upload (index); task + every other view ok.
            if path.endswith("/index.json"):
                return False
            return True

        with patch("fulcra_coord.cli.remote.stat", return_value=None), \
             patch("fulcra_coord.cli.remote.upload_json", side_effect=upload_json), \
             patch("fulcra_coord.cli._load_task_summaries",
                   return_value=[schema.task_summary(task)]), \
             patch("fulcra_coord.cli._load_all_tasks", return_value=[task]):
            with self.assertRaises(schema.NeedsReconcile):
                _write_task_and_views(task, backend=["false"], command="update")

    def test_uploads_all_views_on_success(self):
        from fulcra_coord.cli import _write_task_and_views
        task = self._setup_remote()
        uploaded_paths = []

        def upload_json(data, path, *, backend=None, timeout=None):
            uploaded_paths.append(path)
            return True

        with patch("fulcra_coord.cli.remote.stat", return_value=None), \
             patch("fulcra_coord.cli.remote.upload_json", side_effect=upload_json), \
             patch("fulcra_coord.cli._load_task_summaries",
                   return_value=[schema.task_summary(task)]), \
             patch("fulcra_coord.cli._load_all_tasks", return_value=[task]):
            ok = _write_task_and_views(task, backend=["false"], command="update")
        self.assertTrue(ok)
        # Every standard view path was uploaded (index + summaries + active ...).
        self.assertTrue(any(p.endswith("/index.json") for p in uploaded_paths))
        self.assertTrue(any(p.endswith("/views/summaries.json") for p in uploaded_paths))
        self.assertTrue(any(p.endswith("/views/active.json") for p in uploaded_paths))


class TestRebuildSourceRobustness(unittest.TestCase):
    """`_load_summaries_for_rebuild` must (B1) preserve acks the truncated event
    log can no longer prove, and (S2) recover a task this agent knows about that
    a raced aggregate dropped — without resurrecting a stale local copy."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp

    def tearDown(self):
        del os.environ["XDG_CACHE_HOME"]

    def _agg(self, summaries):
        from fulcra_coord import remote
        sum_path = remote.view_remote_path("summaries")
        view = {"schema": "fulcra.coordination.summaries.v1", "view": "summaries",
                "updated_at": "2026-06-03T00:00:00Z", "summaries": summaries}

        def fake_download(path, *, backend=None, timeout=None):
            return view if path == sum_path else None
        return fake_download

    def test_b1_preserves_acks_lost_to_event_truncation(self):
        # The written task body shows no ack (event scrolled out of the inline
        # window); the durable aggregate still records it. Rebuild must keep it.
        from fulcra_coord.cli import _load_summaries_for_rebuild
        task = apply_transition(_sample_task(), "active", by="claude-code")
        self.assertEqual(schema.task_summary(task).get("acked_by") or [], [])
        prior = schema.task_summary(task)
        prior["acked_by"] = ["openclaw:macmini:infra"]

        with patch("fulcra_coord.cli.remote.download_json",
                   side_effect=self._agg([prior])):
            out = _load_summaries_for_rebuild(task, backend=["false"])
        entry = next(s for s in out if s["id"] == task["id"])
        self.assertIn("openclaw:macmini:infra", entry["acked_by"])

    def test_s2_recovers_locally_known_task_missing_from_aggregate(self):
        # A peer's raced write left the downloaded aggregate without taskB, but
        # this agent has taskB cached → it must survive the rebuild.
        from fulcra_coord.cli import _load_summaries_for_rebuild
        task_a = apply_transition(_sample_task(), "active", by="claude-code")
        task_b = _sample_task()
        task_b["id"] = "TASK-20260603-peer-task-aaaaaaaa"
        task_b["updated_at"] = "2026-06-03T10:00:00Z"
        cache.write_cached_task(task_b)

        with patch("fulcra_coord.cli.remote.download_json",
                   side_effect=self._agg([schema.task_summary(task_a)])):
            out = _load_summaries_for_rebuild(task_a, backend=["false"])
        ids = {s["id"] for s in out}
        self.assertIn(task_b["id"], ids)   # recovered from local cache
        self.assertIn(task_a["id"], ids)

    def test_s2_does_not_resurrect_stale_local_over_fresh_aggregate(self):
        # Aggregate has a NEWER taskB; local cache has a STALE taskB. Freshest
        # (aggregate) must win — no stale resurrection.
        from fulcra_coord.cli import _load_summaries_for_rebuild
        task_a = apply_transition(_sample_task(), "active", by="claude-code")
        stale_b = _sample_task()
        stale_b["id"] = "TASK-20260603-peer-task-bbbbbbbb"
        stale_b["title"] = "STALE"
        stale_b["updated_at"] = "2026-06-01T00:00:00Z"
        cache.write_cached_task(stale_b)
        fresh_b = dict(schema.task_summary(stale_b))
        fresh_b["title"] = "FRESH"
        fresh_b["updated_at"] = "2026-06-03T00:00:00Z"

        with patch("fulcra_coord.cli.remote.download_json",
                   side_effect=self._agg([schema.task_summary(task_a), fresh_b])):
            out = _load_summaries_for_rebuild(task_a, backend=["false"])
        entry = next(s for s in out if s["id"] == stale_b["id"])
        self.assertEqual(entry["title"], "FRESH")


class TestParallelUploadExceptionSafety(unittest.TestCase):
    """S3: a view upload that RAISES (not just returns False) must still be
    treated as a failed view → NeedsReconcile, never escaping uncaught."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp

    def tearDown(self):
        del os.environ["XDG_CACHE_HOME"]

    def test_raising_upload_becomes_needs_reconcile(self):
        from fulcra_coord.cli import _write_task_and_views
        task = apply_transition(_sample_task(), "active", by="claude-code")
        cache.write_cached_task(task)

        def upload_json(data, path, *, backend=None, timeout=None):
            if path.endswith("/index.json"):
                raise RuntimeError("simulated network blowup")
            return True

        with patch("fulcra_coord.cli.remote.stat", return_value=None), \
             patch("fulcra_coord.cli.remote.upload_json", side_effect=upload_json), \
             patch("fulcra_coord.cli._load_summaries_for_rebuild",
                   return_value=[schema.task_summary(task)]):
            with self.assertRaises(schema.NeedsReconcile):
                _write_task_and_views(task, backend=["false"], command="update")


# ===========================================================================
# AGENT PRESENCE — workstream-on-connect (situational awareness)
# ===========================================================================

class TestMakePresence(unittest.TestCase):
    """schema.make_presence: validated per-agent presence record."""

    def test_required_fields_and_schema(self):
        rec = schema.make_presence("claude-code:h:r", workstreams=["fulcra"])
        self.assertEqual(rec["schema"], "fulcra.coordination.presence.v1")
        self.assertEqual(rec["agent"], "claude-code:h:r")
        self.assertEqual(rec["workstreams"], ["fulcra"])
        self.assertIn("last_seen", rec)
        # default last_seen is an ISO-Z timestamp
        self.assertTrue(rec["last_seen"].endswith("Z"))
        self.assertEqual(rec["summary"], "")
        self.assertIsNone(rec["session"])

    def test_workstreams_normalized_sorted_unique_nonempty(self):
        rec = schema.make_presence(
            "a", workstreams=["zeta", "alpha", "alpha", "", "  ", " beta "])
        # sorted, deduped, stripped, empties dropped
        self.assertEqual(rec["workstreams"], ["alpha", "beta", "zeta"])

    def test_workstreams_none_becomes_empty_list(self):
        rec = schema.make_presence("a", workstreams=None)
        self.assertEqual(rec["workstreams"], [])

    def test_explicit_last_seen_and_session_preserved(self):
        rec = schema.make_presence(
            "a", workstreams=["x"], summary="on it",
            last_seen="2026-06-03T00:00:00Z", session="sess-1")
        self.assertEqual(rec["last_seen"], "2026-06-03T00:00:00Z")
        self.assertEqual(rec["session"], "sess-1")
        self.assertEqual(rec["summary"], "on it")


class TestBuildPresence(unittest.TestCase):
    """views.build_presence: aggregate roster with liveness + sort order."""

    def _rec(self, agent, hours_ago, workstreams=("x",)):
        from datetime import datetime, timezone, timedelta
        ts = (datetime.now(timezone.utc) - timedelta(hours=hours_ago)) \
            .isoformat().replace("+00:00", "Z")
        return schema.make_presence(agent, workstreams=list(workstreams),
                                    last_seen=ts)

    def tearDown(self):
        os.environ.pop("FULCRA_COORD_STALE_HOURS", None)

    def test_schema_and_shape(self):
        out = views.build_presence([self._rec("a", 0)])
        self.assertEqual(out["view"], "presence")
        self.assertEqual(out["schema"], "fulcra.coordination.presence_view.v1")
        self.assertIn("updated_at", out)
        self.assertEqual(len(out["agents"]), 1)
        self.assertIn("liveness", out["agents"][0])

    def test_liveness_bands(self):
        # threshold = 2h: live < 1h (0.5x), idle < 2h, else stale
        os.environ["FULCRA_COORD_STALE_HOURS"] = "2"
        recs = [self._rec("live-agent", 0.1),
                self._rec("idle-agent", 1.5),
                self._rec("stale-agent", 5)]
        out = views.build_presence(recs)
        by_agent = {a["agent"]: a["liveness"] for a in out["agents"]}
        self.assertEqual(by_agent["live-agent"], "live")
        self.assertEqual(by_agent["idle-agent"], "idle")
        self.assertEqual(by_agent["stale-agent"], "stale")

    def test_sorted_by_last_seen_desc(self):
        recs = [self._rec("old", 5), self._rec("fresh", 0.1),
                self._rec("mid", 2)]
        out = views.build_presence(recs)
        order = [a["agent"] for a in out["agents"]]
        self.assertEqual(order, ["fresh", "mid", "old"])


class TestPresenceRemotePaths(unittest.TestCase):
    def test_paths(self):
        from fulcra_coord import remote
        self.assertTrue(
            remote.presence_remote_path("claude-code-h-r").endswith(
                "/presence/claude-code-h-r.json"))
        self.assertTrue(
            remote.presence_view_path().endswith("/views/presence.json"))


class _PresenceBackendCase(unittest.TestCase):
    """Base: stateful fake backend so connect/workstream/presence run E2E."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fake_root = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp
        os.environ["FULCRA_FAKE_ROOT"] = self.fake_root
        backend_script = str(Path(__file__).resolve().parent / "fake_fulcra_backend.py")
        self.fake_backend = [sys.executable, backend_script]

    def tearDown(self):
        os.environ.pop("XDG_CACHE_HOME", None)
        os.environ.pop("FULCRA_FAKE_ROOT", None)
        os.environ.pop("FULCRA_COORD_AGENT", None)
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.fake_root, ignore_errors=True)

    def _ns(self, **kw):
        return types.SimpleNamespace(**kw)

    def _run(self, fn, args):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fn(args, backend=self.fake_backend)
        return rc, buf.getvalue()


class TestConnectCommand(_PresenceBackendCase):
    def test_connect_writes_presence_record(self):
        from fulcra_coord.cli import cmd_connect
        from fulcra_coord import remote
        rc, _ = self._run(cmd_connect, self._ns(
            agent="claude-code:h:r", workstream="fulcra", summary="boot",
            format="table"))
        self.assertEqual(rc, 0)
        rec = remote.download_json(
            remote.presence_remote_path(views.agent_slug("claude-code:h:r")),
            backend=self.fake_backend)
        self.assertIsNotNone(rec)
        self.assertEqual(rec["agent"], "claude-code:h:r")
        self.assertIn("fulcra", rec["workstreams"])

    def test_connect_derives_workstreams_from_open_tasks_union_explicit(self):
        from fulcra_coord.cli import cmd_start, cmd_connect
        from fulcra_coord import remote
        me = "claude-code:h:r"
        # Two open tasks owned by me in distinct workstreams.
        self._run(cmd_start, self._ns(
            title="t1", workstream="devops", agent=me, kind="ops",
            priority="P2", summary="", next="", surface=None))
        self._run(cmd_start, self._ns(
            title="t2", workstream="insights", agent=me, kind="ops",
            priority="P2", summary="", next="", surface=None))
        # Connect with an explicit extra workstream.
        rc, _ = self._run(cmd_connect, self._ns(
            agent=me, workstream="research", summary="", format="table"))
        self.assertEqual(rc, 0)
        rec = remote.download_json(
            remote.presence_remote_path(views.agent_slug(me)),
            backend=self.fake_backend)
        # Union of explicit (research) + derived (devops, insights).
        self.assertEqual(set(rec["workstreams"]),
                         {"research", "devops", "insights"})

    def test_connect_upserts_aggregate(self):
        from fulcra_coord.cli import cmd_connect
        from fulcra_coord import remote
        me = "claude-code:h:r"
        self._run(cmd_connect, self._ns(
            agent=me, workstream="fulcra", summary="", format="table"))
        agg = remote.download_json(remote.presence_view_path(),
                                   backend=self.fake_backend)
        self.assertIsNotNone(agg)
        agents = [a["agent"] for a in agg["agents"]]
        self.assertIn(me, agents)
        # second connect by another agent must not clobber the first
        self._run(cmd_connect, self._ns(
            agent="codex:h:r", workstream="ops", summary="", format="table"))
        agg = remote.download_json(remote.presence_view_path(),
                                   backend=self.fake_backend)
        agents = [a["agent"] for a in agg["agents"]]
        self.assertIn(me, agents)
        self.assertIn("codex:h:r", agents)


class TestWorkstreamCommand(_PresenceBackendCase):
    def _record(self, me):
        from fulcra_coord import remote
        return remote.download_json(
            remote.presence_remote_path(views.agent_slug(me)),
            backend=self.fake_backend)

    def test_set_replaces(self):
        from fulcra_coord.cli import cmd_connect, cmd_workstream
        me = "claude-code:h:r"
        self._run(cmd_connect, self._ns(
            agent=me, workstream="fulcra", summary="", format="table"))
        self._run(cmd_workstream, self._ns(
            agent=me, ws_action="set", workstreams="a,b", summary=None,
            format="table"))
        self.assertEqual(set(self._record(me)["workstreams"]), {"a", "b"})

    def test_add_appends(self):
        from fulcra_coord.cli import cmd_connect, cmd_workstream
        me = "claude-code:h:r"
        self._run(cmd_connect, self._ns(
            agent=me, workstream="fulcra", summary="", format="table"))
        self._run(cmd_workstream, self._ns(
            agent=me, ws_action="add", workstreams="extra", summary=None,
            format="table"))
        self.assertEqual(set(self._record(me)["workstreams"]),
                         {"fulcra", "extra"})

    def test_clear_empties(self):
        from fulcra_coord.cli import cmd_connect, cmd_workstream
        me = "claude-code:h:r"
        self._run(cmd_connect, self._ns(
            agent=me, workstream="fulcra", summary="", format="table"))
        self._run(cmd_workstream, self._ns(
            agent=me, ws_action="clear", workstreams=None, summary=None,
            format="table"))
        self.assertEqual(self._record(me)["workstreams"], [])

    def test_set_updates_summary(self):
        from fulcra_coord.cli import cmd_connect, cmd_workstream
        me = "claude-code:h:r"
        self._run(cmd_connect, self._ns(
            agent=me, workstream="fulcra", summary="", format="table"))
        self._run(cmd_workstream, self._ns(
            agent=me, ws_action="set", workstreams="x", summary="new note",
            format="table"))
        self.assertEqual(self._record(me)["summary"], "new note")


class TestPresenceCommand(_PresenceBackendCase):
    def test_empty_roster_message(self):
        from fulcra_coord.cli import cmd_presence
        rc, out = self._run(cmd_presence, self._ns(format="table"))
        self.assertEqual(rc, 0)
        self.assertIn("No agent presence recorded yet", out)

    def test_renders_agent_with_no_tasks(self):
        """The key requirement: an agent with a presence record but NO tasks
        still shows in the roster."""
        from fulcra_coord.cli import cmd_connect, cmd_presence
        me = "lonely-agent:h:r"
        # connect with no open tasks -> presence record exists, no tasks.
        self._run(cmd_connect, self._ns(
            agent=me, workstream="solo-stream", summary="working alone",
            format="table"))
        rc, out = self._run(cmd_presence, self._ns(format="table"))
        self.assertEqual(rc, 0)
        self.assertIn(me, out)
        self.assertIn("solo-stream", out)

    def test_json_roster(self):
        from fulcra_coord.cli import cmd_connect, cmd_presence
        me = "claude-code:h:r"
        self._run(cmd_connect, self._ns(
            agent=me, workstream="fulcra", summary="", format="table"))
        rc, out = self._run(cmd_presence, self._ns(format="json"))
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["view"], "presence")
        self.assertIn(me, [a["agent"] for a in data["agents"]])


class TestAgentsIncludesPresenceOnly(_PresenceBackendCase):
    def test_presence_only_agent_surfaces_in_agents(self):
        """cmd_agents must include an agent that has a presence record but no
        active task (the situational-awareness requirement)."""
        from fulcra_coord.cli import cmd_connect, cmd_agents
        ghost = "presence-only:h:r"
        self._run(cmd_connect, self._ns(
            agent=ghost, workstream="ambient-work", summary="just lurking",
            format="table"))
        rc, out = self._run(cmd_agents, self._ns(mine=None, format="table"))
        self.assertEqual(rc, 0)
        self.assertIn(ghost, out)
        self.assertIn("ambient-work", out)


class TestPresenceReconcile(_PresenceBackendCase):
    def test_reconcile_rebuilds_presence_aggregate_from_records(self):
        """reconcile lists presence/*.json and rebuilds views/presence.json."""
        from fulcra_coord.cli import cmd_connect, cmd_reconcile
        from fulcra_coord import remote
        a, b = "agent-a:h:r", "agent-b:h:r"
        self._run(cmd_connect, self._ns(
            agent=a, workstream="wa", summary="", format="table"))
        self._run(cmd_connect, self._ns(
            agent=b, workstream="wb", summary="", format="table"))
        # Nuke the aggregate; reconcile must rebuild it from per-agent records.
        agg_local = remote.presence_view_path()
        rel = agg_local.lstrip("/")
        (Path(self.fake_root) / rel).unlink()
        self._run(cmd_reconcile, self._ns())
        agg = remote.download_json(remote.presence_view_path(),
                                   backend=self.fake_backend)
        self.assertIsNotNone(agg)
        agents = {x["agent"] for x in agg["agents"]}
        self.assertEqual(agents, {a, b})


class TestStartAgentOptional(_PresenceBackendCase):
    def test_start_without_agent_resolves_via_resolve_agent(self):
        from fulcra_coord.cli import cmd_start
        from fulcra_coord import remote
        os.environ["FULCRA_COORD_AGENT"] = "env-agent:h:r"
        # No --agent attribute set on the namespace at all (omitted).
        rc, out = self._run(cmd_start, self._ns(
            title="resolve me", workstream="devops", kind="ops",
            priority="P2", summary="", next="", surface=None, agent=None))
        self.assertEqual(rc, 0)
        # The created task's owner must be the resolved env agent.
        self.assertIn("env-agent:h:r", out)

    def test_start_with_agent_overrides(self):
        from fulcra_coord.cli import cmd_start
        os.environ["FULCRA_COORD_AGENT"] = "env-agent:h:r"
        rc, out = self._run(cmd_start, self._ns(
            title="explicit", workstream="devops", kind="ops",
            priority="P2", summary="", next="", surface=None,
            agent="explicit-agent:h:r"))
        self.assertEqual(rc, 0)
        self.assertIn("explicit-agent:h:r", out)


# ---------------------------------------------------------------------------
# Onboarding UX hints (Task C): non-blocking STDERR nudges.
#   1. `start` with a TASK-id-shaped title -> "you probably meant to claim".
#   2. `connect`/`start` with a DERIVED identity + a legacy global identity.json
#      present -> "migrate your legacy identity".
# Both are warnings only; behavior (task creation, presence write) is unchanged.
# ---------------------------------------------------------------------------

class TestOnboardingHints(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fake_root = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp, "config")
        os.environ["FULCRA_FAKE_ROOT"] = self.fake_root
        os.environ.pop("FULCRA_COORD_AGENT", None)
        backend_script = str(Path(__file__).resolve().parent / "fake_fulcra_backend.py")
        self.fake_backend = [sys.executable, backend_script]

    def tearDown(self):
        for k in ("XDG_CACHE_HOME", "XDG_CONFIG_HOME", "FULCRA_FAKE_ROOT",
                  "FULCRA_COORD_AGENT"):
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.fake_root, ignore_errors=True)

    def _ns(self, **kw):
        return types.SimpleNamespace(**kw)

    def _run_capturing_stderr(self, fn, args):
        """Run a command and capture BOTH streams (the hints go to STDERR so the
        backgrounded `connect` hook discards them; tests must read stderr)."""
        import io, contextlib
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fn(args, backend=self.fake_backend)
        return rc, out.getvalue(), err.getvalue()

    def _write_legacy_identity(self, agent="codex:Mac:main"):
        from fulcra_coord import identity
        legacy = identity.config_root() / "identity.json"
        legacy.parent.mkdir(parents=True, exist_ok=True)
        legacy.write_text(json.dumps({"agent": agent}))

    # ---- start-vs-claim hint ----

    def test_start_with_task_id_title_emits_claim_hint_but_still_creates(self):
        from fulcra_coord.cli import cmd_start
        title = "TASK-20260101-deploy-the-thing-ab12cd34"
        rc, out, err = self._run_capturing_stderr(cmd_start, self._ns(
            title=title, workstream="devops", agent="claude-code:h:r",
            kind="ops", priority="P2", summary="", next="", surface=None))
        # Non-blocking: the task is still created (cached) regardless of rc.
        tasks = cache.list_cached_tasks()
        self.assertTrue(any(t["title"] == title for t in tasks),
                        "start must still create the task despite the hint")
        self.assertIn("update", err)
        self.assertIn("--status active", err)

    def test_start_with_normal_title_no_claim_hint(self):
        from fulcra_coord.cli import cmd_start
        rc, out, err = self._run_capturing_stderr(cmd_start, self._ns(
            title="Deploy the widget", workstream="devops",
            agent="claude-code:h:r", kind="ops", priority="P2",
            summary="", next="", surface=None))
        self.assertNotIn("--status active", err)

    # ---- legacy-identity migrate hint ----

    def test_connect_emits_legacy_hint_when_derived_and_legacy_present(self):
        # No explicit/env/per-cwd identity -> source is "derived"; a legacy
        # global exists -> hint fires (on STDERR).
        from fulcra_coord.cli import cmd_connect
        self._write_legacy_identity()
        rc, out, err = self._run_capturing_stderr(cmd_connect, self._ns(
            agent=None, workstream=None, summary="", format="table"))
        self.assertEqual(rc, 0)
        self.assertIn("identity.json", err)
        self.assertIn("identity migrate", err)

    def test_start_emits_legacy_hint_when_derived_and_legacy_present(self):
        from fulcra_coord.cli import cmd_start
        self._write_legacy_identity()
        rc, out, err = self._run_capturing_stderr(cmd_start, self._ns(
            title="A normal task", workstream="devops", agent=None,
            kind="ops", priority="P2", summary="", next="", surface=None))
        self.assertIn("identity migrate", err)

    def test_no_legacy_hint_when_per_cwd_identity_set(self):
        # A declared per-cwd identity -> source "config", not "derived" -> no hint
        # even though a legacy global is present.
        from fulcra_coord.cli import cmd_connect
        from fulcra_coord import identity
        self._write_legacy_identity()
        identity.set_identity("claude-code:host:thisrepo")
        rc, out, err = self._run_capturing_stderr(cmd_connect, self._ns(
            agent=None, workstream=None, summary="", format="table"))
        self.assertNotIn("identity migrate", err)

    def test_no_legacy_hint_when_no_legacy_file(self):
        # Derived identity but NO legacy file -> nothing to migrate -> no hint.
        from fulcra_coord.cli import cmd_connect
        rc, out, err = self._run_capturing_stderr(cmd_connect, self._ns(
            agent=None, workstream=None, summary="", format="table"))
        self.assertNotIn("identity migrate", err)

    def test_no_legacy_hint_when_explicit_agent(self):
        # An explicit --agent -> source "explicit", not "derived" -> no hint.
        from fulcra_coord.cli import cmd_connect
        self._write_legacy_identity()
        rc, out, err = self._run_capturing_stderr(cmd_connect, self._ns(
            agent="explicit:h:r", workstream=None, summary="", format="table"))
        self.assertNotIn("identity migrate", err)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
