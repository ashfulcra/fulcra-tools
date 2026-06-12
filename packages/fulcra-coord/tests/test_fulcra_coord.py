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
from datetime import datetime, timedelta, timezone
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
        # waiting -> done stays illegal: parked work must be picked up (active)
        # before it can be completed. (proposed -> done became legal with the
        # message-class lifecycle — see the tests below — so the pin moved here.)
        t = _with_status(_sample_task(), "waiting")
        with self.assertRaises(TransitionError):
            apply_transition(t, "done", by="agent-a",
                             evidence="x", verification_level="agent-verified")

    def test_proposed_to_done_with_evidence(self):
        # Message-class lifecycle (2026-06-11): a delivered message's consumer
        # closing the echo is the NORMAL case, and the old two-write dance
        # (update->active, then done) over a high-latency transport silently
        # discouraged cleanup. proposed -> done is legal in ONE write; evidence
        # stays mandatory (next test), so the audit trail is preserved.
        t = _sample_task()  # status = proposed
        t2 = apply_transition(t, "done", by="agent-a",
                              evidence="delivered; echo closed",
                              verification_level="agent-verified")
        self.assertEqual(t2["status"], "done")
        self.assertEqual(t2["done"]["evidence"], "delivered; echo closed")

    def test_proposed_to_done_still_requires_evidence(self):
        # The single-write close must NOT weaken the done contract: evidence
        # (and a verification level) is enforced exactly as from active.
        t = _sample_task()
        with self.assertRaises(SchemaError):
            apply_transition(t, "done", by="agent-a")

    def test_proposed_transitions_otherwise_unchanged(self):
        # Only `done` was added; active/waiting/abandoned stay, blocked stays
        # illegal from proposed (block implies someone picked it up first).
        self.assertEqual(schema.STATUS_TRANSITIONS["proposed"],
                         {"active", "waiting", "abandoned", "done"})
        t = _sample_task()
        with self.assertRaises(TransitionError):
            apply_transition(t, "blocked", by="agent-a", blocked_on="x")

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

    def test_malformed_task_missing_title_does_not_crash_rebuild(self):
        """Regression (live incident): a real task body missing 'title'/'id' must NOT
        KeyError out of build_search_index — that aborts the whole reconcile rebuild
        (views not repaired, retention never runs). Same render-don't-crash contract
        as task_summary: surface the malformed task with empty-string defaults."""
        tasks = [
            {"status": "active", "workstream": "ws", "owner_agent": "a:b:c"},  # no id, no title
            {"id": "TASK-20260101-x-00000000", "status": "waiting"},          # no title
        ]
        idx = build_search_index(tasks)  # must not raise
        recs = idx["records"]
        self.assertEqual(len(recs), 2)
        for r in recs:
            self.assertIn("title", r)
            self.assertIn("id", r)
            self.assertIn("task_file", r)

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

    def test_agent_views_not_materialized(self):
        # 2026-06-11 perf wave item 3: per-agent agents/<id>.json views were
        # rebuilt + uploaded on every write/reconcile and read by NOTHING (no
        # surface downloads agent_remote_path — verified by audit + grep).
        # They are no longer materialized; per-agent reads ride the summaries
        # aggregate (cmd_agents/resume fold it client-side).
        tasks = _make_tasks_set()
        all_v = build_all_views(tasks)
        agent_views = [n for n in all_v if n.startswith("agents/")]
        self.assertEqual(agent_views, [],
                         f"zero-reader per-agent views re-materialized: {agent_views}")


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

    def test_connect_can_review_sets_review_capability(self):
        from fulcra_coord.cli import cmd_connect
        captured = {}

        def fake_write(record, backend=None):
            captured["rec"] = record
            return True

        with patch("fulcra_coord.presence._write_presence", side_effect=fake_write), \
             patch("fulcra_coord.presence._derive_workstreams_from_open_tasks", return_value=[]), \
             patch("fulcra_coord.remote.probe_reachable", return_value=True):
            args = self._args(agent="claude-code:h:r", workstream=None, summary="",
                              format="json", can_review=True, role=None)
            cmd_connect(args, backend=["false"])
        self.assertIn("review", captured["rec"]["capabilities"])

    def test_connect_role_flag_adds_named_capabilities(self):
        from fulcra_coord.cli import cmd_connect
        captured = {}
        with patch("fulcra_coord.presence._write_presence",
                   side_effect=lambda record, backend=None: captured.update(rec=record) or True), \
             patch("fulcra_coord.presence._derive_workstreams_from_open_tasks", return_value=[]), \
             patch("fulcra_coord.remote.probe_reachable", return_value=True):
            args = self._args(agent="a", workstream=None, summary="", format="json",
                              can_review=False, role=["review", "deploy"])
            cmd_connect(args, backend=["false"])
        self.assertEqual(sorted(captured["rec"]["capabilities"]), ["deploy", "review"])

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

    def test_done_directly_from_proposed(self):
        """proposed -> done in ONE write via the done command (message-class
        lifecycle): closing a delivered tell/echo must not require the
        update->active dance. --evidence is still required by the parser and
        enforced by apply_transition."""
        from fulcra_coord.cli import cmd_start, cmd_done

        start_args = self._args(
            title="Proposed close task",
            workstream="devops",
            agent="claude-code",
            kind="ops",
            priority="P2",
            summary="",
            next="",
            surface=None,
        )
        cmd_start(start_args, backend=self.fake_backend)
        task = next(t for t in cache.list_cached_tasks()
                    if t["title"] == "Proposed close task")
        self.assertEqual(task["status"], "proposed")

        done_args = self._args(
            task_id=task["id"],
            evidence="delivered; closing echo",
            verification_level="agent-verified",
            confidence=None,
            agent="claude-code",
        )
        rc = cmd_done(done_args, backend=self.fake_backend)
        self.assertIn(rc, (0, 1))  # 1 = upload-fail offline; transition still applies
        cached = cache.read_cached_task(task["id"])
        self.assertEqual(cached.get("status"), "done")
        self.assertEqual(cached["done"]["evidence"], "delivered; closing echo")

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

    def test_merge_preserves_nonstandard_kind_review_marker(self):
        """kind:review is a membership marker, not the task's schema kind."""
        from fulcra_coord.cli import _try_merge
        base = _sample_task()
        local_v = apply_update(base, by="agent-a", summary="local note")
        local_v["tags"] = sorted(set(local_v["tags"] + ["kind:review"]))
        remote_v = apply_update(base, by="agent-b", summary="remote note")

        result = _try_merge(local_v, remote_v)

        self.assertIsNotNone(result)
        self.assertIn("kind:ops", result["tags"])
        self.assertIn("kind:review", result["tags"])

    def test_merge_preserves_standard_kind_when_marker_kind_sorts_first(self):
        """2026-06-11 bug hunt C7: with tags [kind:idea, kind:ops] the repair
        extracted 'idea' (sorts first, NOT in VALID_KINDS) as the primary kind,
        and kind:ops — a standard tag, so excluded from the extras carry — was
        silently dropped from the merged task. Both must survive."""
        from fulcra_coord.cli import _try_merge
        base = _sample_task()
        remote_v = apply_update(base, by="agent-b", summary="remote note")
        local_v = apply_update(base, by="agent-a", summary="local note")
        local_v["tags"] = sorted(set(local_v["tags"] + ["kind:idea"]))
        # Force local to be the newer side so the merge base carries BOTH kind
        # tags (the hunt's repro shape: merged["tags"] = [kind:idea, kind:ops]).
        local_v["updated_at"] = "2030-01-01T00:00:00.000000Z"

        result = _try_merge(local_v, remote_v)

        self.assertIsNotNone(result)
        self.assertIn("kind:ops", result["tags"])
        self.assertIn("kind:idea", result["tags"])

    def test_merge_preserves_both_standard_kinds(self):
        """2026-06-11 bug hunt C7 (companion): TWO standard kinds on a task —
        the non-primary one is a standard tag (excluded from extras) and used
        to vanish on merge. Mirrors apply_transition's _secondary_kinds carry."""
        from fulcra_coord.cli import _try_merge
        base = _sample_task()
        remote_v = apply_update(base, by="agent-b", summary="remote note")
        local_v = apply_update(base, by="agent-a", summary="local note")
        local_v["tags"] = sorted(set(local_v["tags"] + ["kind:feature"]))
        local_v["updated_at"] = "2030-01-01T00:00:00.000000Z"

        result = _try_merge(local_v, remote_v)

        self.assertIsNotNone(result)
        self.assertIn("kind:ops", result["tags"])
        self.assertIn("kind:feature", result["tags"])

    def test_merge_tolerates_event_missing_at(self):
        """2026-06-11 bug hunt S8: a malformed bus event with no 'at' key
        KeyError-ed _try_merge mid-write (event-time set comprehensions and
        the union's dict-by-at both hard-indexed it). The merge must not
        raise; the at-less event gets SENTINEL ordering (treated as oldest)
        and the result is deterministic."""
        from fulcra_coord.cli import _try_merge
        base = _sample_task()
        local_v = apply_update(base, by="agent-a", summary="local note")
        local_v["events"] = list(local_v["events"]) + [
            {"type": "note", "summary": "no timestamp"}]   # the malformed event
        remote_v = apply_update(base, by="agent-b", summary="remote note")

        result = _try_merge(local_v, remote_v)   # must not raise

        self.assertIsNotNone(result)
        summaries = [e.get("summary") for e in result["events"]]
        self.assertIn("local note", summaries)
        self.assertIn("remote note", summaries)
        # The at-less event is kept, ordered as OLDEST (the sentinel choice).
        self.assertEqual(result["events"][0].get("summary"), "no timestamp")
        # Deterministic: the same inputs always merge to the same result.
        self.assertEqual(result, _try_merge(local_v, remote_v))

    def test_merge_atless_status_event_is_not_a_new_transition(self):
        """S8 companion: an at-less STATUS-shaped event cannot be ordered
        against the other side, so it must read as ancient/shared — never as
        evidence of a NEW transition that manufactures a spurious conflict."""
        from fulcra_coord.cli import _try_merge
        base = _sample_task()
        local_v = apply_update(base, by="agent-a", summary="local note")
        local_v["events"] = list(local_v["events"]) + [{"type": "active"}]
        local_v["status"] = "active"
        remote_v = apply_transition(base, "active", by="agent-b")

        result = _try_merge(local_v, remote_v)   # must not raise, must merge

        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "active")

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


class TestLoadAllTasksTolerantOfIdlessBodies(unittest.TestCase):
    """A2 — _load_all_tasks must not crash on an id-less cached body.

    An older/imperfect bus can leave a cached task file whose JSON lacks an
    ``id`` key. Building ``{t["id"]: t for t in cached}`` (and the remote-merge
    ``task_map[t["id"]] = t``) with bracket access raises KeyError, which is
    uncaught on the no-summaries-aggregate fallback path of
    _load_summaries_for_rebuild -> _write_task_and_views, crashing EVERY write
    command (create/update/done/tell/...). The load must skip the malformed body
    and still surface the well-formed ones."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp

    def tearDown(self):
        os.environ.pop("XDG_CACHE_HOME", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_idless_cache_file(self):
        # list_cached_tasks() globs TASK-*.json and parses each; a file named like
        # a task but whose body lacks "id" is exactly the imperfect-data case.
        cache.ensure_dirs()
        bad = cache.tasks_dir() / "TASK-idless.json"
        bad.write_text(json.dumps({"status": "active", "title": "no id here"}))

    def test_idless_cached_body_no_remote_index_does_not_crash(self):
        from fulcra_coord.cli import _load_all_tasks
        good = _sample_task()
        cache.write_cached_task(good)
        self._write_idless_cache_file()
        # No remote index (download returns None) -> early return of `cached`,
        # which is fine; but the next test exercises the dict-comp path. Here we
        # at least confirm the cached list itself is loadable.
        with patch("fulcra_coord.cli.remote.download_json", return_value=None):
            tasks = _load_all_tasks(backend=["false"])
        self.assertIn(good["id"], {t.get("id") for t in tasks})

    def test_idless_cached_body_with_remote_index_does_not_crash(self):
        from fulcra_coord.cli import _load_all_tasks
        good = _sample_task()
        cache.write_cached_task(good)
        self._write_idless_cache_file()

        def fake_download(path, *, backend=None, timeout=None):
            if path.endswith("/index.json"):
                return {"active": [{"id": good["id"]}], "recent_done": []}
            return None

        with patch("fulcra_coord.cli.remote.download_json", side_effect=fake_download), \
             patch("fulcra_coord.cli.remote.stat", return_value={"version_id": "v1"}):
            tasks = _load_all_tasks(backend=["false"])

        ids = {t.get("id") for t in tasks}
        self.assertIn(good["id"], ids)   # well-formed task survives
        self.assertNotIn(None, ids)      # the id-less body was skipped, not crashed on

    def test_idless_remote_body_merge_does_not_crash(self):
        # The second bracket-access (task_map[t["id"]] = t) on a remote body that
        # came back without an id must also be tolerated.
        from fulcra_coord.cli import _load_all_tasks
        good = _sample_task()

        def fake_download(path, *, backend=None, timeout=None):
            if path.endswith("/index.json"):
                return {"active": [{"id": good["id"]}], "recent_done": []}
            return None

        # _cache_remote_task returns an id-less body for the indexed id.
        # Patched in io's namespace: _load_all_tasks lives in io and calls
        # _cache_remote_task there, so a cli-namespace patch would be bypassed and
        # the id-less body would never reach the A2 merge guard (vacuous pass).
        with patch("fulcra_coord.cli.remote.download_json", side_effect=fake_download), \
             patch("fulcra_coord.io._cache_remote_task",
                   return_value={"status": "active", "title": "no id"}):
            tasks = _load_all_tasks(backend=["false"])
        self.assertNotIn(None, {t.get("id") for t in tasks})


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


class TestSecondaryKindTagsPreservedOnTransition(unittest.TestCase):
    """Regression: apply_transition must not clobber SECONDARY kind: tags.

    Review tasks carry TWO kind: tags — the base schema kind (e.g. kind:ops)
    and the kind:review routing membership marker added by request-review.
    Before the fix, the transition tag-rebuild excluded ALL kind:-prefixed
    tags from the preserved extras, then rebuilt with the single primary from
    _extract_kind_from_tags (kind:ops sorts before kind:review) — so the FIRST
    transition (a reviewer CLAIMING the review, proposed->active) silently
    dropped kind:review, is_review_directive() flipped False, and review-done
    could no longer resolve the request. Live-found 2026-06-10.
    """

    def _review_task(self) -> dict:
        from fulcra_coord import routing
        t = _sample_task()  # kind:ops, status proposed
        t["tags"] = sorted(set(t["tags"] + [routing.REVIEW_TAG]))
        return t

    def test_kind_review_survives_claim_transition(self):
        t = self._review_task()
        t2 = apply_transition(t, "active", by="reviewer:h:r")
        self.assertIn("kind:review", t2["tags"],
                      "kind:review marker must survive a claim (proposed->active)")
        self.assertIn("kind:ops", t2["tags"],
                      "primary kind must survive alongside the review marker")

    def test_is_review_directive_still_true_after_transition(self):
        from fulcra_coord import routing
        t = self._review_task()
        t2 = apply_transition(t, "active", by="reviewer:h:r")
        self.assertTrue(routing.is_review_directive(t2),
                        "claimed review must still read as a review directive")

    def test_plain_task_rebuild_unchanged(self):
        """Regression guard: a single-kind task's rebuild is byte-identical to
        before — exactly one kind: tag, full standard set, no duplicates."""
        t = _sample_task()
        t2 = apply_transition(t, "active", by="agent-a")
        kind_tags = [tag for tag in t2["tags"] if tag.startswith("kind:")]
        self.assertEqual(kind_tags, ["kind:ops"],
                         "plain task must keep exactly one kind: tag")
        for expected in ("workstream:devops", "agent:claude-code",
                         "status:active", "priority:P2"):
            self.assertIn(expected, t2["tags"])
        self.assertEqual(len(t2["tags"]), len(set(t2["tags"])),
                         "no duplicate tags after rebuild")


def test_review_resolves_after_assignee_claims(coord_backend):
    """End-to-end live repro (2026-06-10): request-review creates the review
    task; the assignee CLAIMS it (update --status active, the real cmd path);
    _resolve_review_request must STILL find it — before the fix the claim's
    tag rebuild dropped kind:review and review-done reported
    '<unresolved — pass --to>', so the verdict could not close the loop."""
    from types import SimpleNamespace
    from fulcra_coord import lifecycle, presence, routing, routing_ops, schema

    author = "author:hostA:repo"
    reviewer = "reviewer:hostB:repo"

    # A live, review-capable reviewer in presence (so routing resolves).
    rec = schema.make_presence(reviewer, capabilities=["review"])
    presence._write_presence(rec, backend=coord_backend)

    rr = SimpleNamespace(pr="42", repo="org/repo", agent=author,
                         candidate_list=None, dry_run=False, format="table")
    assert routing_ops.cmd_request_review(rr, backend=coord_backend) == 0

    # Sanity: resolvable before the claim, and tagged kind:review.
    before = routing_ops._resolve_review_request("42", backend=coord_backend)
    assert before is not None and routing.is_review_directive(before)

    # The reviewer claims the review — the real `update --status active` path.
    claim = SimpleNamespace(task_id=before["id"], summary="claiming the review",
                            blocked_on=None, status="active", agent=reviewer)
    setattr(claim, "next", None)
    assert lifecycle.cmd_update(claim, backend=coord_backend) == 0

    # THE BUG: after the claim, the review request must still resolve.
    after = routing_ops._resolve_review_request("42", backend=coord_backend)
    assert after is not None, (
        "claimed review no longer resolves — kind:review was clobbered "
        "by the transition tag rebuild")
    assert after["id"] == before["id"]
    assert after["status"] == "active"
    assert routing.is_review_directive(after)


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

        # Patch remote.upload_json to always succeed so reconcile completes
        # cleanly. probe_reachable=True declares the mocked world a REACHABLE
        # bus whose index is confirmed absent (F3): without it, all-None reads
        # now correctly read as "can't see the bus" and the tick refuses to
        # rebuild views (degraded skip) instead of completing.
        with patch("fulcra_coord.cli.remote.upload_json", return_value=True), \
             patch("fulcra_coord.cli.remote.download_json", return_value=None), \
             patch("fulcra_coord.cli.remote.probe_reachable", return_value=True):
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

    The #1 fresh-agent onboarding failure: the resolved Fulcra CLI lacks the
    `file` command group that drives the coordination bus. Without a dedicated
    probe, the agent runs fulcra-coord and every bus op fails silently with no
    clear signal why. Doctor surfaces this with a FAIL + the fix (reinstall the
    standard CLI or fix FULCRA_CLI_COMMAND per docs/fulcra-cli-branch.md) and
    marks the overall result not-ok.
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
             patch("fulcra_coord.doctor._info",
                   side_effect=lambda *a, **kw: lines.append(" ".join(str(x) for x in a))):
            rc = cmd_doctor(types.SimpleNamespace(), backend=["false"])
        return rc, "\n".join(lines)

    def test_doctor_reports_file_commands_ok(self):
        """Probe succeeds → `File commands: OK` printed, exit stays 0."""
        rc, out = self._run_doctor(True, "fulcra-api file")
        self.assertIn("File commands: OK", out)
        self.assertEqual(rc, 0)

    def test_doctor_reports_file_commands_fail_with_fix(self):
        """Probe fails → FAIL line + current fix hint, and the overall doctor
        result is not-ok (exit 1)."""
        rc, out = self._run_doctor(False, "No such command 'file'.")
        self.assertIn("File commands: FAIL", out)
        self.assertIn("uv tool install --reinstall --force fulcra-api", out)
        self.assertIn("FULCRA_CLI_COMMAND", out)
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
            # Only capture the task BODY file (tasks/<id>.json), not view files
            # and not the additive event shards (events/tasks/<id>/<event_id>.json),
            # whose path also contains the "tasks/" substring. Match the task-body
            # prefix specifically so the best-effort dual-write append can't clobber
            # the captured body.
            if "/tasks/" in path and "/events/" not in path:
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

    def test_confirmed_absent_new_task_skips_merge_and_writes(self):
        """CONFIRMED absence (stat None + download None + bus reachable) is the
        genuinely-new-task case: no merge check, the write proceeds unmodified.

        2026-06-11 write-path read-error audit (F1): this test used to pin the
        OPPOSITE — "no merge check when pre_stat is None" — which conflated a
        504'd stat with absence and let a stale agent blind-overwrite a peer's
        landed transition. Absence now requires a failed download AND a
        positive reachability probe, never a bare stat miss."""
        from fulcra_coord.cli import _write_task_and_views

        base = make_task(title="Brand new task", workstream="devops", agent="agent-a")
        cache.write_cached_task(base)

        uploaded = {}

        def _fake_upload(data, path, *, backend=None, timeout=None):
            if "/tasks/" in path and "/events/" not in path:
                uploaded["task"] = data
            return True

        with patch("fulcra_coord.cli.remote.stat", return_value=None), \
             patch("fulcra_coord.cli.remote.download_json", return_value=None), \
             patch("fulcra_coord.cli.remote.probe_reachable", return_value=True), \
             patch("fulcra_coord.cli.remote.upload_json", side_effect=_fake_upload):
            ok = _write_task_and_views(base, backend=["false"])

        self.assertTrue(ok, "confirmed-absent new task must write cleanly")
        self.assertEqual(uploaded.get("task", {}).get("id"), base["id"])
        self.assertEqual(uploaded["task"].get("status"), base.get("status"),
                         "no merge may rewrite a confirmed-new task's body")

    def test_read_failure_is_not_absence_and_never_blind_overwrites(self):
        """READ FAILURE (stat None + download None + bus NOT reachable) must
        fail the write — cached locally + needs_reconcile marker — instead of
        being mistaken for a new task and uploaded blind (the F1 failure
        sequence: A's pre-stat 504s while B's `done` sits on the bus; A's
        'new task' upload reverts B's transition)."""
        from fulcra_coord.cli import _write_task_and_views

        base = make_task(title="Maybe not new", workstream="devops", agent="agent-a")
        cache.write_cached_task(base)

        task_body_uploads = []

        def _fake_upload(data, path, *, backend=None, timeout=None):
            if "/tasks/" in path and "/events/" not in path:
                task_body_uploads.append(path)
            return True

        with patch("fulcra_coord.cli.remote.stat", return_value=None), \
             patch("fulcra_coord.cli.remote.download_json", return_value=None), \
             patch("fulcra_coord.cli.remote.probe_reachable", return_value=False), \
             patch("fulcra_coord.cli.remote.upload_json", side_effect=_fake_upload):
            ok = _write_task_and_views(base, backend=["false"])

        self.assertFalse(ok, "unconfirmable absence must fail the write")
        self.assertEqual(task_body_uploads, [],
                         "no task body may be uploaded while reads are failing "
                         "(blind overwrite of a possibly-newer remote body)")
        markers = [m for m in cache.list_op_markers()
                   if m.get("task_id") == base["id"]]
        self.assertTrue(
            any(m.get("needs_reconcile") and m.get("status") == "failed"
                for m in markers),
            f"expected a failed/needs_reconcile marker for self-heal, got {markers}")


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
        self.assertIn("status:active", result.get("tags") or [],
                      "Tags must be repaired to match the preserved local status")
        self.assertNotIn("status:proposed", result.get("tags") or [],
                         "Merged tags must not keep the remote base's stale status")

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

    def test_remote_newer_fields_with_local_status_rebuilds_tags_from_merged_fields(self):
        """When only local changed status but remote has a newer priority/extra
        tag update, merged tags must reflect local status + remote priority and
        preserve non-standard tags from both sides."""
        from fulcra_coord.cli import _try_merge
        from datetime import datetime, timezone, timedelta

        base = _sample_task()
        t_local = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
        t_remote = t_local + timedelta(seconds=30)

        local_active = apply_transition(base, "active", by="agent-a", dt=t_local)
        local_active["tags"] = sorted(set(local_active["tags"] + ["needs:human"]))

        remote_updated = apply_update(base, by="agent-b",
                                      summary="Remote newer summary", dt=t_remote)
        remote_updated["priority"] = "P1"
        remote_updated["tags"] = sorted(set(remote_updated["tags"] + ["custom:remote"]))

        result = _try_merge(local_active, remote_updated)
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "active")
        self.assertEqual(result["priority"], "P1")
        self.assertIn("status:active", result["tags"])
        self.assertNotIn("status:proposed", result["tags"])
        self.assertIn("priority:P1", result["tags"])
        self.assertNotIn("priority:P2", result["tags"])
        self.assertIn("needs:human", result["tags"])
        self.assertIn("custom:remote", result["tags"])

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

        # probe_reachable=True: this world's bus is plainly reachable (the
        # index downloads fine); the F1/F2 read-error guards must read the
        # missing summaries aggregate as confirmed-absent, not as an outage.
        with patch("fulcra_coord.cli.remote.download_json", side_effect=fake_download), \
             patch("fulcra_coord.cli.remote.stat", side_effect=fake_stat), \
             patch("fulcra_coord.cli.remote.probe_reachable", return_value=True), \
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

        # probe_reachable=True (F1/F2 guards): reachable bus, the new task and
        # the summaries aggregate are CONFIRMED absent rather than unreadable.
        with patch("fulcra_coord.cli.remote.download_json", side_effect=fake_download), \
             patch("fulcra_coord.cli.remote.stat", side_effect=fake_stat), \
             patch("fulcra_coord.cli.remote.probe_reachable", return_value=True), \
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
        self.assertIn("snapshot", cc.PRE_COMPACT_SH)
        self.assertIn("pause", cc.SESSION_END_SH)
        self.assertIn("--snapshot", cc.SESSION_END_SH)


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
        # fake fulcra-coord: briefing wraps the canned status JSON into the
        # combined one-process payload session-start now consumes; status stays
        # canned for session-end.sh; other subcommands log args.
        status_json = os.path.join(self.tmp, "status.json")
        fake = os.path.join(self.bin, "fulcra-coord")
        with open(fake, "w") as f:
            f.write("#!/usr/bin/env bash\n"
                    'if [ "$1" = "briefing" ]; then STATUS="%s" python3 -c \''
                    'import json,os;print(json.dumps({"agent":"",'
                    '"status":json.load(open(os.environ["STATUS"])),'
                    '"inbox":{"inbox":[]},"needs_me":{"items":[]}}))\'; exit 0; fi\n'
                    'if [ "$1" = "status" ]; then cat "%s"; exit 0; fi\n'
                    'if [ "$1" = "__session-task" ]; then echo "TASK-live"; exit 0; fi\n'
                    'echo "$@" >> "%s"\n' % (status_json, status_json, self.calls))
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
        calls = open(self.calls).read()
        self.assertIn("update TASK-live", calls)
        self.assertIn("snapshot TASK-live", calls)
        self.assertIn("--reason pre-compact", calls)

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
        calls = open(self.calls).read()
        self.assertIn("pause TASK-live", calls)
        self.assertIn("--snapshot", calls)

    def test_session_end_noop_when_not_active(self):
        sj = json.dumps({"active": [{"id": "TASK-live", "status": "waiting"}]})
        r = self._run("session-end.sh", json.dumps({"session_id": "s"}), sj)
        self.assertEqual(r.returncode, 0)
        self.assertFalse(os.path.exists(self.calls) and "pause" in open(self.calls).read())

    def test_session_start_resume_hint_quotes_fulcra_coord(self):
        # BUG 5: the resume-hint line built a command from `$FULCRA_COORD` raw, so
        # a value carrying a shell metacharacter (the joined argv) embedded an
        # injectable / broken command into the surfaced resume hint. The value must
        # be shlex-quoted so the hint is a single safe token. We name the fake CLI
        # with a literal `;` in its filename: invoked via the quoted bash array it
        # still runs correctly (so the hint renders), but `${FULCRA_COORD[*]}` —
        # the value the resume-hint Python reads — carries the raw metacharacter.
        from fulcra_coord.cli_invocation import PLACEHOLDER_ARGV, materialize_argv
        evil_name = "fulcra-coord;evil"
        evil = os.path.join(self.bin, evil_name)
        shutil.copy(os.path.join(self.bin, "fulcra-coord"), evil)
        os.chmod(evil, 0o755)
        committed = os.path.join(os.path.dirname(__file__), "..", "adapters",
                                 "claude-code", "hooks")
        body = open(os.path.join(committed, "session-start.sh")).read()
        out = os.path.join(self.hooks, "session-start.sh")
        with open(out, "w") as f:
            f.write(body.replace(PLACEHOLDER_ARGV, materialize_argv([evil])))
        os.chmod(out, 0o755)
        sj = json.dumps({"active": [
            {"id": "TASK-stale", "title": "Deploy", "status": "active",
             "owner_agent": "claude-code:other-host:other-repo",
             "updated_at": "2026-06-01T00:00:00Z"}]})
        r = self._run("session-start.sh", json.dumps({"cwd": self.tmp}), sj)
        self.assertEqual(r.returncode, 0)
        self.assertIn("To resume:", r.stdout)
        # The rendered command must NOT contain the raw `;evil ` injection; the
        # quoted form keeps the metacharacter inside a single-quoted token.
        self.assertNotIn(evil_name + " update", r.stdout,
                         "raw unquoted metacharacter leaked into the resume hint")


class TestSessionStartBlockedOnYouBanner(unittest.TestCase):
    """SessionStart leads with a ⛔ BLOCKED ON YOU section (from needs-me) before
    the in-flight / directives / stale sections; silent when needs-me is empty."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bin = os.path.join(self.tmp, "bin"); os.makedirs(self.bin)
        # Fake CLI: briefing -> canned status + needs-me sections combined into
        # the one-process payload session-start now consumes (inbox empty).
        fake = os.path.join(self.bin, "fulcra-coord")
        with open(fake, "w") as f:
            f.write("#!/usr/bin/env bash\n"
                    'if [ "$1" = "briefing" ]; then STATUS="%s" NEEDSME="%s" '
                    "python3 -c '"
                    'import json,os;print(json.dumps({"agent":"",'
                    '"status":json.load(open(os.environ["STATUS"])),'
                    '"inbox":{"inbox":[]},'
                    '"needs_me":json.load(open(os.environ["NEEDSME"]))}))'
                    "'; exit 0; fi\n"
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

    def test_future_only_plate_shows_upcoming_without_headline(self):
        # BUG 9: due-now empty but upcoming non-empty. The banner must surface
        # the muted "(+N upcoming)" line (and not exit silently), with NO ⛔
        # BLOCKED ON YOU headline (the headline counts due-now only).
        needsme = json.dumps({"human": "ash", "count": 0, "items": [],
            "upcoming": [
                {"id": "TASK-future", "title": "reauth window",
                 "status": "blocked", "owner_agent": "claude-code:h:r",
                 "not_before": "2099-01-01T00:00:00Z",
                 "updated_at": "2026-06-01T00:00:00Z"}]})
        r = self._run(json.dumps({"active": []}), needsme)
        self.assertEqual(r.returncode, 0)
        self.assertNotEqual(r.stdout.strip(), "",
                            "a future-only plate must still emit the upcoming line")
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("(+1 upcoming)", ctx)
        self.assertNotIn("BLOCKED ON YOU", ctx,
                         "no due-now items -> no ⛔ headline")

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
        # Fake CLI: briefing carries a DECLARED agent id (what
        # identity.resolve_agent resolved inside the one-process command),
        # different from the derived shape; status canned; inbox/needs-me empty.
        fake = os.path.join(self.bin, "fulcra-coord")
        with open(fake, "w") as f:
            f.write("#!/usr/bin/env bash\n"
                    'if [ "$1" = "briefing" ]; then STATUS="%s" '
                    "python3 -c '"
                    'import json,os;print(json.dumps({"agent":"declared:custom:id",'
                    '"status":json.load(open(os.environ["STATUS"])),'
                    '"inbox":{"inbox":[]},"needs_me":{"items":[]}}))'
                    "'; exit 0; fi\n"
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
        json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
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
            # briefing carries an EMPTY agent id (exercise the derived fallback
            # so AGENT == the owner_agent of the self-filed task below).
            f.write("#!/usr/bin/env bash\n"
                    'if [ "$1" = "briefing" ]; then STATUS="%s" NEEDSME="%s" '
                    "python3 -c '"
                    'import json,os;print(json.dumps({"agent":"",'
                    '"status":json.load(open(os.environ["STATUS"])),'
                    '"inbox":{"inbox":[]},'
                    '"needs_me":json.load(open(os.environ["NEEDSME"]))}))'
                    "'; exit 0; fi\n"
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
        self.assertIn("--snapshot", oc.SHUTDOWN_HANDLER_TS)
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
        self.assertIn("snapshot", handler)
        self.assertIn("openclaw-before-compaction", handler)

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
        # before_compaction ALWAYS checkpoints: `update` stamps task freshness
        # and `snapshot` archives a Fulcra Continuity-compatible resume point.
        self.assertIn("update", ts)
        self.assertIn("snapshot", ts)
        self.assertIn("openclaw-before-compaction", ts)
        # session_end parks via `pause`, and must skip `compaction` (continues).
        self.assertIn("pause", ts)
        self.assertIn("--snapshot", ts)
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


class TestInstallOpenClawBundleCmd(unittest.TestCase):
    """install-openclaw can BUNDLE the durable bus-pickup path (heartbeat +
    per-agent listener) in one command — so "OpenClaw installed" means "this
    agent hears directed work" without a separate install-heartbeat /
    install-listener step. The OpenClaw analogue of ensure-codex-watch.

    These patch the hardened installers at their REAL call sites
    (`fulcra_coord.installers.heartbeat.install_heartbeat` /
    `...listener.install_listener`) so the assertions are non-vacuous: if the
    bundle were open-coded or wired to the wrong symbol, the mocks would not
    fire. openclaw.install_openclaw is also patched to a plan dict so no Track A
    files actually land.
    """

    def _patches(self):
        """Patch the three installers cmd_install_openclaw composes, returning
        the MagicMocks for heartbeat / listener so tests can assert on them.
        openclaw.install_openclaw returns a minimal plan dict with the keys the
        command's summary printing reads."""
        from unittest.mock import patch
        hb = patch("fulcra_coord.installers.heartbeat.install_heartbeat",
                   return_value={"mechanism": "launchd", "writes": ["/tmp/hb.plist"],
                                 "removes": ["/tmp/hb.plist"], "interval_min": 20,
                                 "cli_command": "fulcra-coord"})
        ls = patch("fulcra_coord.installers.listener.install_listener",
                   return_value={"mechanism": "launchd", "writes": ["/tmp/ls.plist"],
                                 "removes": ["/tmp/ls.plist"], "interval_min": 10,
                                 "cli_command": "fulcra-coord"})
        oc = patch("fulcra_coord.installers.openclaw.install_openclaw",
                   return_value={"hooks_root": "/tmp/ocroot", "writes": [],
                                 "removes": [], "hook_dirs": [], "prompt_files": []})
        return hb, ls, oc

    def _args(self, **kw):
        from types import SimpleNamespace
        base = dict(hooks_root="/tmp/ocroot", uninstall=False, dry_run=False,
                    with_plugin=False, plugin_dir=None,
                    with_heartbeat=False, with_listener=False, agent=None)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_cmd_can_bundle_heartbeat_and_listener(self):
        from fulcra_coord import installers
        hb, ls, oc = self._patches()
        with hb as m_hb, ls as m_ls, oc:
            rc = installers.cmd_install_openclaw(self._args(
                with_heartbeat=True, with_listener=True,
                agent="openclaw:test:infra"))
        self.assertEqual(rc, 0)
        self.assertEqual(m_hb.call_count, 1)
        self.assertEqual(m_ls.call_count, 1)
        # The listener is per-agent: it must watch the agent we passed.
        self.assertEqual(m_ls.call_args.kwargs.get("agent"), "openclaw:test:infra")
        # Heartbeat is machine-global → never per-agent.
        self.assertNotIn("agent", m_hb.call_args.kwargs)

    def test_cmd_bundle_uninstall_removes_scheduler_jobs(self):
        from fulcra_coord import installers
        hb, ls, oc = self._patches()
        with hb as m_hb, ls as m_ls, oc:
            rc = installers.cmd_install_openclaw(self._args(
                uninstall=True, with_heartbeat=True, with_listener=True,
                agent="openclaw:test:infra"))
        self.assertEqual(rc, 0)
        # The gotcha guard for uninstall: the early `return 0` must not
        # short-circuit the bundle. Both installers are reached with uninstall=True.
        self.assertTrue(m_hb.call_args.kwargs.get("uninstall"))
        self.assertTrue(m_ls.call_args.kwargs.get("uninstall"))

    def test_cmd_bundle_dry_run_no_side_effects_but_previews(self):
        from fulcra_coord import installers
        hb, ls, oc = self._patches()
        with hb as m_hb, ls as m_ls, oc:
            rc = installers.cmd_install_openclaw(self._args(
                dry_run=True, with_heartbeat=True, with_listener=True,
                agent="openclaw:test:infra"))
        self.assertEqual(rc, 0)
        # The core gotcha guard: the early dry-run `return 0` must NOT
        # short-circuit the bundle. Both installers are reached with dry_run=True
        # (they print their own plan and write nothing).
        self.assertTrue(m_hb.call_args.kwargs.get("dry_run"))
        self.assertTrue(m_ls.call_args.kwargs.get("dry_run"))

    def test_cmd_without_bundle_flags_unchanged(self):
        from fulcra_coord import installers
        hb, ls, oc = self._patches()
        with hb as m_hb, ls as m_ls, oc:
            rc = installers.cmd_install_openclaw(self._args())
        self.assertEqual(rc, 0)
        # No with_* flags → base behavior preserved: neither scheduler installs.
        m_hb.assert_not_called()
        m_ls.assert_not_called()

    def test_cmd_listener_derives_agent_when_unset(self):
        """with_listener but no --agent → the per-agent listener still gets a
        concrete agent (derived), never None, or it would watch the wrong inbox."""
        from fulcra_coord import installers
        hb, ls, oc = self._patches()
        with hb, ls as m_ls, oc, \
                patch("fulcra_coord.installers._derive_agent",
                      return_value="derived:agent"):
            rc = installers.cmd_install_openclaw(self._args(with_listener=True))
        self.assertEqual(rc, 0)
        self.assertEqual(m_ls.call_args.kwargs.get("agent"), "derived:agent")


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


class TestNaiveTimestampNoCrash(unittest.TestCase):
    """BUG 8: a naive (tz-less) parsed timestamp must not crash the liveness /
    aging paths. _parse_dt returned a naive datetime; subtracting it from an
    aware `now` in _age_hours raised TypeError, and the +inf fail-safe contract
    was violated. _parse_dt now coerces naive -> UTC so every caller is safe."""

    def setUp(self):
        from datetime import datetime, timezone
        self.now = datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)

    def test_presence_liveness_with_naive_iso(self):
        from fulcra_coord.views import presence_liveness
        # 10 minutes ago, but tz-less -> must classify (assume UTC), not crash.
        out = presence_liveness("2026-06-03T11:50:00", now=self.now,
                                stale_hours=2)
        self.assertEqual(out, "live")

    def test_is_aged_out_broadcast_with_naive_updated_at(self):
        from fulcra_coord.views import is_aged_out_broadcast
        t = {"id": "TASK-NB", "title": "t", "status": "proposed",
             "workstream": "w", "owner_agent": "o", "assignee": "*",
             "priority": "P2", "tags": [], "events": [],
             "updated_at": "2026-06-01T12:00:00"}  # naive, 2 days old
        # Must not raise; with a 1-day cutoff the 2-day-old broadcast ages out.
        self.assertTrue(is_aged_out_broadcast(t, self.now, age_days=1))

    def test_inbox_for_over_task_with_naive_updated_at(self):
        from fulcra_coord.views import inbox_for
        t = {"id": "TASK-NB2", "title": "t", "status": "proposed",
             "workstream": "w", "owner_agent": "boss", "assignee": "claude-code",
             "priority": "P2", "tags": [], "events": [],
             "updated_at": "2026-06-03T11:55:00"}  # naive, recent
        # Must not raise TypeError out of the aging check.
        out = inbox_for("claude-code:h:r", [t], now=self.now)
        self.assertEqual([s["id"] for s in out], ["TASK-NB2"])

    def test_age_hours_naive_does_not_raise(self):
        from fulcra_coord.views import _age_hours, _parse_dt
        # Direct: naive parse must be aware-UTC, subtraction must work.
        self.assertIsNotNone(_parse_dt("2026-06-03T11:00:00").tzinfo)
        self.assertAlmostEqual(_age_hours("2026-06-03T11:00:00", self.now), 1.0,
                               places=3)


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
        # probe_reachable=True: model a REACHABLE bus with no index yet, so the
        # F3 degraded-load guard doesn't (correctly) skip the view-build phase
        # this test exists to exercise. Uploads still fail (the `false`
        # backend), which is the partial-failure outcome under test.
        with patch("fulcra_coord.cli.remote.probe_reachable", return_value=True):
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
        # probe_reachable=True: same F3 note as above — reachable, index absent.
        with patch("fulcra_coord.cli.remote.probe_reachable", return_value=True):
            climod.cmd_reconcile(types.SimpleNamespace(),
                                 backend=self.fake_backend)
        na = cache.read_cached_view("needs-attention")
        self.assertEqual(na["tasks"], [])


class TestStaleClaimDetection(unittest.TestCase):
    """A1 — the stale-claim scan must survive an id-less task body.

    A real bus body with status==active + an expired claim but NO ``id`` field
    (an imperfect/older write) used to raise KeyError out of the reconcile
    stale-claim loop (bracket access ``t["id"]``). That KeyError was NOT caught
    (the inner try only guards ValueError from fromisoformat) and ran BEFORE
    build_all_views/upload — so the whole reconcile aborted and every heartbeat
    tick failed. Same class as the build_search_index incident."""

    def _expired_claim_task(self, *, with_id=True, status="active"):
        from datetime import datetime, timedelta, timezone
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat().replace(
            "+00:00", "Z")
        t = {"status": status, "claim": {"claim_expires_at": past}}
        if with_id:
            t["id"] = "TASK-good"
        return t

    def test_idless_active_expired_claim_does_not_raise(self):
        from fulcra_coord import cli as climod
        now = datetime.now(timezone.utc)
        tasks = [self._expired_claim_task(with_id=False)]
        # Must NOT raise KeyError on the missing id.
        stale = climod._detect_stale_claims(tasks, now)
        self.assertEqual(stale, [])  # id-less body contributes nothing

    def test_good_tasks_still_detected_alongside_idless(self):
        from fulcra_coord import cli as climod
        now = datetime.now(timezone.utc)
        tasks = [
            self._expired_claim_task(with_id=False),   # the poison body
            self._expired_claim_task(with_id=True),    # a well-formed stale claim
        ]
        stale = climod._detect_stale_claims(tasks, now)
        self.assertEqual(stale, ["TASK-good"])

    def test_non_active_expired_claim_ignored(self):
        from fulcra_coord import cli as climod
        now = datetime.now(timezone.utc)
        t = self._expired_claim_task(status="done")
        self.assertEqual(climod._detect_stale_claims([t], now), [])

    def test_unparseable_expiry_does_not_raise(self):
        from fulcra_coord import cli as climod
        now = datetime.now(timezone.utc)
        t = {"id": "TASK-x", "status": "active",
             "claim": {"claim_expires_at": "not-a-date"}}
        self.assertEqual(climod._detect_stale_claims([t], now), [])


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
            heartbeat.install_heartbeat(target_dir=self.target,
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
            heartbeat.install_heartbeat(
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
        plist = os.path.join(self.target,
                             "com.fulcra.coord.listener.codex-h-r.plist")
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
        ln = os.path.join(self.target,
                          "com.fulcra.coord.listener.codex-h-r.plist")
        self.assertTrue(os.path.exists(hb) and os.path.exists(ln))
        with open(hb, "rb") as f:
            self.assertEqual(plistlib.load(f)["Label"],
                             "com.fulcra.coord.heartbeat")
        with open(ln, "rb") as f:
            self.assertEqual(plistlib.load(f)["Label"],
                             "com.fulcra.coord.listener.codex-h-r")

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
    def test_reuses_claude_code_script_bodies_with_codex_review_capability(self):
        from fulcra_coord import codex, claude_code as cc
        # SessionStart mostly shares the Claude Code body (same stdin shape) with
        # three intentional Codex transforms: (a) it publishes the review
        # capability (Codex is the canonical review target, else request-review
        # can't find it after startup); (b) it backgrounds an `ensure-codex-watch`
        # self-heal BEFORE that connect (so a fresh Codex box that never ran
        # `install-listener` still arms its listener); (c) the derived fallback id
        # is `codex:*` not `claude-code:*`.
        expected = cc.SESSION_START_SH.replace(
            '"${FULCRA_COORD[@]}" connect >/dev/null 2>&1 &',
            '# Re-arm Codex hooks + the per-agent inbox listener on every app start.\n'
            '# Backgrounded + silenced; an old CLI without ensure-codex-watch simply no-ops.\n'
            '"${FULCRA_COORD[@]}" ensure-codex-watch --agent "$AGENT" --no-connect >/dev/null 2>&1 &\n'
            '"${FULCRA_COORD[@]}" connect --can-review >/dev/null 2>&1 &',
        ).replace(
            '[ -z "$AGENT" ] && AGENT="claude-code:${HOST}:${REPO}"',
            '[ -z "$AGENT" ] && AGENT="codex:${HOST}:${REPO}"',
        )
        self.assertEqual(codex.SESSION_START_SH, expected)
        self.assertIn("connect --can-review", codex.SESSION_START_SH)
        # PreCompact reuses the CC body but keys the session-id env fallback on
        # FULCRA_COORD_SESSION_KEY (Codex's session id env differs).
        self.assertIn("FULCRA_COORD_SESSION_KEY", codex.PRE_COMPACT_SH)
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", codex.PRE_COMPACT_SH)
        # Gap 1 argv placeholder present so it gets a resolved argv at install.
        self.assertIn("__FULCRA_COORD_ARGV__", codex.PRE_COMPACT_SH)

    def test_session_start_self_heals_via_ensure_codex_watch(self):
        # Every Codex app start should re-arm its own hooks + per-agent listener so
        # a fresh machine that never ran `install-listener` still self-heals and
        # hears directed work. The SessionStart hook backgrounds an
        # `ensure-codex-watch --no-connect` (--no-connect because the hook already
        # does its own `connect --can-review` right after — avoid double-connect).
        from fulcra_coord import codex
        self.assertIn("ensure-codex-watch", codex.SESSION_START_SH)
        self.assertIn("--no-connect", codex.SESSION_START_SH)
        # LOAD-BEARING: the self-heal must be ADDED, never at the cost of the
        # review-capability connect (#70). The poisoned PR dropped this; guard it.
        self.assertIn("connect --can-review", codex.SESSION_START_SH)
        # Backgrounded + silenced so it never blocks or slows session boot.
        self.assertIn('ensure-codex-watch --agent "$AGENT" --no-connect '
                      '>/dev/null 2>&1 &', codex.SESSION_START_SH)

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


class TestEnsureCodexWatch(unittest.TestCase):
    """`ensure-codex-watch` — the single idempotent "make Codex coordination
    self-healing" entry point. It composes the already-hardened installers
    (install_codex + install_listener), best-effort launchctl-loads the listener
    plist, and (unless --no-connect) refreshes presence — all fail-safe so it can
    run backgrounded at every Codex SessionStart without ever hard-failing.
    """

    def _args(self, **overrides):
        base = dict(
            agent="codex:h:r", set_identity=None, no_connect=False,
            can_review=False, role=None, summary=None, workstream=None,
            interval_min=None, codex_target_dir=None, listener_target_dir=None,
            listener_logs_dir=None, no_load=False, dry_run=False, uninstall=False)
        base.update(overrides)
        return types.SimpleNamespace(**base)

    def _listener_plan(self, mechanism="launchd"):
        # Minimal shape ensure-codex-watch reads from the listener plan: a
        # mechanism (decides whether launchctl load is even attempted) and the
        # plist path at writes[0] (the load target).
        return {"mechanism": mechanism, "writes": ["/tmp/x.plist"], "removes": []}

    def test_happy_path_arms_both_installers_loads_and_connects(self):
        from fulcra_coord import installers
        with patch("fulcra_coord.installers.codex.install_codex") as m_codex, \
             patch("fulcra_coord.installers.listener.install_listener",
                   return_value=self._listener_plan()) as m_listener, \
             patch("fulcra_coord.installers.subprocess.run") as m_run, \
             patch("fulcra_coord.installers.cmd_connect", return_value=0) as m_connect:
            rc = installers.cmd_ensure_codex_watch(self._args())
        self.assertEqual(rc, 0)
        # Both hardened installers were composed (not open-coded).
        m_codex.assert_called_once()
        m_listener.assert_called_once()
        self.assertEqual(m_listener.call_args.kwargs.get("agent"), "codex:h:r")
        # The self-heal launchctl load targets the plan's plist (writes[0]).
        self.assertTrue(m_run.called, "launchctl load was not attempted")
        load_argv = m_run.call_args.args[0]
        self.assertEqual(load_argv[:3], ["launchctl", "load", "-w"])
        self.assertIn("/tmp/x.plist", load_argv)
        # Presence refreshed exactly once.
        m_connect.assert_called_once()

    def test_no_connect_skips_presence(self):
        from fulcra_coord import installers
        with patch("fulcra_coord.installers.codex.install_codex"), \
             patch("fulcra_coord.installers.listener.install_listener",
                   return_value=self._listener_plan()), \
             patch("fulcra_coord.installers.subprocess.run"), \
             patch("fulcra_coord.installers.cmd_connect") as m_connect:
            rc = installers.cmd_ensure_codex_watch(self._args(no_connect=True))
        self.assertEqual(rc, 0)
        m_connect.assert_not_called()

    def test_no_load_skips_launchctl(self):
        from fulcra_coord import installers
        with patch("fulcra_coord.installers.codex.install_codex"), \
             patch("fulcra_coord.installers.listener.install_listener",
                   return_value=self._listener_plan()), \
             patch("fulcra_coord.installers.subprocess.run") as m_run, \
             patch("fulcra_coord.installers.cmd_connect", return_value=0):
            rc = installers.cmd_ensure_codex_watch(self._args(no_load=True))
        self.assertEqual(rc, 0)
        m_run.assert_not_called()

    def test_non_launchd_mechanism_skips_launchctl(self):
        # crontab (Linux) has no launchctl — the load step must not fire.
        from fulcra_coord import installers
        with patch("fulcra_coord.installers.codex.install_codex"), \
             patch("fulcra_coord.installers.listener.install_listener",
                   return_value=self._listener_plan(mechanism="crontab")), \
             patch("fulcra_coord.installers.subprocess.run") as m_run, \
             patch("fulcra_coord.installers.cmd_connect", return_value=0):
            rc = installers.cmd_ensure_codex_watch(self._args())
        self.assertEqual(rc, 0)
        m_run.assert_not_called()

    def test_failsafe_load_raise_still_returns_zero(self):
        # A failed launchctl load (e.g. already-loaded, or launchctl missing) must
        # NEVER crash the command — it runs backgrounded at every SessionStart.
        from fulcra_coord import installers
        with patch("fulcra_coord.installers.codex.install_codex"), \
             patch("fulcra_coord.installers.listener.install_listener",
                   return_value=self._listener_plan()), \
             patch("fulcra_coord.installers.subprocess.run",
                   side_effect=OSError("boom")), \
             patch("fulcra_coord.installers.cmd_connect", return_value=0):
            rc = installers.cmd_ensure_codex_watch(self._args())
        self.assertEqual(rc, 0)

    def test_failsafe_connect_raise_still_returns_zero(self):
        from fulcra_coord import installers
        with patch("fulcra_coord.installers.codex.install_codex"), \
             patch("fulcra_coord.installers.listener.install_listener",
                   return_value=self._listener_plan()), \
             patch("fulcra_coord.installers.subprocess.run"), \
             patch("fulcra_coord.installers.cmd_connect",
                   side_effect=RuntimeError("boom")):
            rc = installers.cmd_ensure_codex_watch(self._args())
        self.assertEqual(rc, 0)

    def test_dry_run_delegates_no_load_no_connect(self):
        from fulcra_coord import installers
        with patch("fulcra_coord.installers.codex.install_codex") as m_codex, \
             patch("fulcra_coord.installers.listener.install_listener",
                   return_value=self._listener_plan()) as m_listener, \
             patch("fulcra_coord.installers.subprocess.run") as m_run, \
             patch("fulcra_coord.installers.cmd_connect") as m_connect:
            rc = installers.cmd_ensure_codex_watch(self._args(dry_run=True))
        self.assertEqual(rc, 0)
        # Installers run in dry-run (they print their own plans).
        self.assertTrue(m_codex.call_args.kwargs.get("dry_run"))
        self.assertTrue(m_listener.call_args.kwargs.get("dry_run"))
        # No side effects in dry-run.
        m_run.assert_not_called()
        m_connect.assert_not_called()

    def test_dry_run_set_identity_does_not_persist(self):
        # `--dry-run` promises zero side effects. A declared identity should shape
        # the printed listener plan, but must not write identity state.
        from fulcra_coord import installers
        with patch("fulcra_coord.installers.identity.set_identity") as m_set, \
             patch("fulcra_coord.installers.codex.install_codex") as m_codex, \
             patch("fulcra_coord.installers.listener.install_listener",
                   return_value=self._listener_plan()) as m_listener, \
             patch("fulcra_coord.installers.subprocess.run") as m_run, \
             patch("fulcra_coord.installers.cmd_connect") as m_connect:
            rc = installers.cmd_ensure_codex_watch(
                self._args(dry_run=True, set_identity="codex:box:repo"))
        self.assertEqual(rc, 0)
        m_set.assert_not_called()
        self.assertTrue(m_codex.call_args.kwargs.get("dry_run"))
        self.assertTrue(m_listener.call_args.kwargs.get("dry_run"))
        self.assertEqual(m_listener.call_args.kwargs.get("agent"), "codex:box:repo")
        m_run.assert_not_called()
        m_connect.assert_not_called()

    def test_set_identity_persists_before_arming(self):
        from fulcra_coord import installers
        with patch("fulcra_coord.installers.identity.set_identity") as m_set, \
             patch("fulcra_coord.installers.codex.install_codex"), \
             patch("fulcra_coord.installers.listener.install_listener",
                   return_value=self._listener_plan()), \
             patch("fulcra_coord.installers.subprocess.run"), \
             patch("fulcra_coord.installers.cmd_connect", return_value=0):
            rc = installers.cmd_ensure_codex_watch(
                self._args(set_identity="codex:box:repo"))
        self.assertEqual(rc, 0)
        m_set.assert_called_once_with("codex:box:repo")

    def test_idempotent_two_calls_both_return_zero(self):
        # The underlying installers are idempotent; running twice must be a clean
        # no-op (this is the every-SessionStart contract).
        from fulcra_coord import installers
        with patch("fulcra_coord.installers.codex.install_codex"), \
             patch("fulcra_coord.installers.listener.install_listener",
                   return_value=self._listener_plan()), \
             patch("fulcra_coord.installers.subprocess.run"), \
             patch("fulcra_coord.installers.cmd_connect", return_value=0):
            a = installers.cmd_ensure_codex_watch(self._args())
            b = installers.cmd_ensure_codex_watch(self._args())
        self.assertEqual((a, b), (0, 0))

    def test_uninstall_tears_down_both(self):
        from fulcra_coord import installers
        with patch("fulcra_coord.installers.codex.install_codex") as m_codex, \
             patch("fulcra_coord.installers.listener.install_listener",
                   return_value=self._listener_plan()) as m_listener, \
             patch("fulcra_coord.installers.subprocess.run"), \
             patch("fulcra_coord.installers.cmd_connect"):
            rc = installers.cmd_ensure_codex_watch(self._args(uninstall=True))
        self.assertEqual(rc, 0)
        self.assertTrue(m_codex.call_args.kwargs.get("uninstall"))
        self.assertTrue(m_listener.call_args.kwargs.get("uninstall"))


class TestEnsureCodexWatchDispatch(unittest.TestCase):
    def test_in_command_map(self):
        from fulcra_coord.entry import COMMAND_MAP
        from fulcra_coord import cli as climod
        self.assertIn("ensure-codex-watch", COMMAND_MAP)
        self.assertIs(COMMAND_MAP["ensure-codex-watch"],
                      climod.cmd_ensure_codex_watch)

    def test_parser_accepts_flags(self):
        from fulcra_coord.entry import build_parser
        parser = build_parser()
        ns = parser.parse_args([
            "ensure-codex-watch", "--agent", "codex:h:r",
            "--set-identity", "codex:h:r", "--no-connect", "--can-review",
            "--interval-min", "15", "--no-load", "--dry-run"])
        self.assertEqual(ns.command, "ensure-codex-watch")
        self.assertEqual(ns.agent, "codex:h:r")
        self.assertEqual(ns.set_identity, "codex:h:r")
        self.assertTrue(ns.no_connect)
        self.assertTrue(ns.can_review)
        self.assertEqual(ns.interval_min, 15)
        self.assertTrue(ns.no_load)
        self.assertTrue(ns.dry_run)


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
        status_json = os.path.join(self.tmp, "status.json")
        with open(self.fake, "w") as f:
            f.write("#!/usr/bin/env bash\n"
                    'if [ "$1" = "briefing" ]; then STATUS="%s" python3 -c \''
                    'import json,os;print(json.dumps({"agent":"",'
                    '"status":json.load(open(os.environ["STATUS"])),'
                    '"inbox":{"inbox":[]},"needs_me":{"items":[]}}))\'; exit 0; fi\n'
                    'if [ "$1" = "status" ]; then cat "%s"; exit 0; fi\n'
                    'if [ "$1" = "__session-task" ]; then echo "TASK-live"; exit 0; fi\n'
                    'echo "$@" >> "%s"\n'
                    % (status_json, status_json, self.calls))
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
    def test_inbox_views_not_materialized(self):
        # 2026-06-11 perf wave item 3: views/inbox/<slug>.json was emitted per
        # open-directive assignee on every write/reconcile and read by NOTHING
        # — cmd_inbox recomputes from the task set precisely because this view
        # goes stale once an inbox empties (the C1 phantom-directive bug). The
        # index's counts.inbox fold (below) is the surviving read surface.
        d = _directive("codex:h:r")
        views_out = build_all_views([d])
        inbox_views = [n for n in views_out if n.startswith("inbox/")]
        self.assertEqual(inbox_views, [],
                         f"zero-reader inbox views re-materialized: {inbox_views}")

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

    def test_inbox_pins_single_now_across_shown_and_hidden(self):
        # BUG 14: inbox_for (shown) and aged_out_inbox_count (hidden) each
        # evaluated _now() independently (3+ times per cmd_inbox). At the aging
        # boundary a broadcast could be SHOWN by one evaluation and COUNTED
        # HIDDEN by a later one — listed and hidden at once. cmd_inbox must pin a
        # single `now` and thread it into both so they agree.
        from fulcra_coord.cli import cmd_inbox
        from fulcra_coord import views
        from datetime import datetime, timezone, timedelta
        import io, contextlib

        # One-day age cutoff; a broadcast right at the boundary.
        os.environ["FULCRA_COORD_INBOX_AGE_DAYS"] = "1"
        try:
            base = datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)
            # updated_at sits just INSIDE the 1-day window at the first clock
            # read: at `base` age (~23h59m) < cutoff -> SHOWN. The clock then
            # advances 1h between reads, so the hidden-count's later read sees
            # age ~25h >= cutoff -> would count it HIDDEN. The boundary is
            # crossed purely by the unpinned clock advancing mid-command.
            bc = _directive("*", owner="boss", status="proposed")
            bc["updated_at"] = (
                base - timedelta(days=1) + timedelta(minutes=1)
            ).isoformat().replace("+00:00", "Z")
            cache.write_cached_task(bc)

            # A clock that advances 1h per call: the shown read sees `base`
            # (under cutoff), the hidden-count reads see `base + Nh` (over
            # cutoff). With the bug this makes the SAME broadcast both shown and
            # counted hidden; pinning a single `now` makes them agree.
            ticks = [base + timedelta(hours=i) for i in range(10)]
            it = iter(ticks)
            with patch.object(views, "_now", side_effect=lambda: next(it)):
                args = self._ns(agent="codex:h:r", format="json", ack=None,
                                all=False)
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    cmd_inbox(args, backend=self.fake_backend)
            out = json.loads(buf.getvalue())
            shown_ids = {i["id"] for i in out["inbox"]}
            # The broadcast must be EITHER shown OR counted hidden, never both.
            # If shown, hidden must be 0; if hidden, it must not be in shown.
            if bc["id"] in shown_ids:
                self.assertEqual(out["hidden_aged"], 0,
                    "a shown broadcast must not also be counted hidden")
            else:
                self.assertEqual(out["hidden_aged"], 1)
        finally:
            os.environ.pop("FULCRA_COORD_INBOX_AGE_DAYS", None)

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

    def test_inbox_all_reveals_aged_broadcast_and_default_counts_hidden(self):
        from fulcra_coord.cli import cmd_inbox
        from fulcra_coord.views import BROADCAST
        old = "2000-01-01T00:00:00Z"
        d = _directive(BROADCAST, owner="boss:h:r", updated_at=old)
        cache.write_cached_task(d)
        import io, contextlib

        default_buf = io.StringIO()
        default_args = self._ns(agent="codex:h:r", format="json", ack=None, all=False)
        with contextlib.redirect_stdout(default_buf):
            cmd_inbox(default_args, backend=self.fake_backend)
        default_out = json.loads(default_buf.getvalue())
        self.assertEqual(default_out["count"], 0)
        self.assertEqual(default_out["hidden_aged"], 1)
        self.assertEqual(default_out["inbox"], [])

        all_buf = io.StringIO()
        all_args = self._ns(agent="codex:h:r", format="json", ack=None, all=True)
        with contextlib.redirect_stdout(all_buf):
            cmd_inbox(all_args, backend=self.fake_backend)
        all_out = json.loads(all_buf.getvalue())
        self.assertEqual(all_out["count"], 1)
        self.assertEqual(all_out["hidden_aged"], 0)
        self.assertEqual([i["id"] for i in all_out["inbox"]], [d["id"]])

    def test_inbox_default_keeps_old_concrete_directive(self):
        from fulcra_coord.cli import cmd_inbox
        d = _directive("codex:h:r", owner="boss:h:r",
                       updated_at="2000-01-01T00:00:00Z")
        cache.write_cached_task(d)
        import io, contextlib
        args = self._ns(agent="codex:h:r", format="json", ack=None, all=False)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_inbox(args, backend=self.fake_backend)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["hidden_aged"], 0)
        self.assertEqual([i["id"] for i in out["inbox"]], [d["id"]])


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
            # briefing combines the canned status + inbox sections; its argv is
            # logged so the no-pinned---agent contract (I1) stays assertable.
            f.write("#!/usr/bin/env bash\n"
                    'if [ "$1" = "briefing" ]; then echo "$@" > "%s"; '
                    'STATUS="%s" INBOX="%s" python3 -c \''
                    'import json,os;print(json.dumps({"agent":"",'
                    '"status":json.load(open(os.environ["STATUS"])),'
                    '"inbox":json.load(open(os.environ["INBOX"])),'
                    '"needs_me":{"items":[]}}))\'; exit 0; fi\n'
                    'if [ "$1" = "__session-task" ]; then echo "TASK-live"; exit 0; fi\n'
                    'exit 0\n' % (self.inbox_args, self.status_json, self.inbox_json))
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

    def test_briefing_call_does_not_pin_agent(self):
        # I1: the hook must NOT pass --agent to the briefing call (which now
        # carries the inbox section). Passing it is highest-precedence in
        # resolve_agent and would override a persisted (`identity set`) or
        # $FULCRA_COORD_AGENT identity, so directives addressed to a declared
        # id would be missed. The briefing command must resolve its own identity.
        self._run(status=json.dumps({"active": []}),
                  inbox=json.dumps({"inbox": []}))
        with open(self.inbox_args) as f:
            recorded = f.read()
        self.assertNotIn("--agent", recorded,
                         "SessionStart briefing call must not pin --agent (I1)")


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
            listener.install_listener(agent="codex:h:r",
                                      target_dir=self.target,
                                      interval_min=10)
        plist = os.path.join(self.target,
                             "com.fulcra.coord.listener.codex-h-r.plist")
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
        self.assertEqual(
            files.count("com.fulcra.coord.listener.codex-h-r.plist"), 1)

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
        plist = os.path.join(self.target,
                             "com.fulcra.coord.listener.codex-h-r.plist")
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

    # --- per-agent identity: co-located agents must coexist ----------------

    def test_two_agents_get_distinct_coexisting_plists(self):
        """install A then B in the same target_dir -> two distinct plist files,
        each with its own Label and --agent value; neither overwrites the other.

        This is the core bug fix: a machine-global label made install B clobber
        install A so only one inbox was ever watched."""
        import plistlib
        from fulcra_coord import listener
        with patch("fulcra_coord.cli_invocation.resolve_cli_argv",
                   return_value=["/opt/bin/fulcra-coord"]), \
             patch("sys.platform", "darwin"):
            listener.install_listener(agent="agent-a:h:r", target_dir=self.target)
            listener.install_listener(agent="agent-b:h:r", target_dir=self.target)
        pa = os.path.join(self.target,
                          "com.fulcra.coord.listener.agent-a-h-r.plist")
        pb = os.path.join(self.target,
                          "com.fulcra.coord.listener.agent-b-h-r.plist")
        self.assertTrue(os.path.exists(pa) and os.path.exists(pb))
        with open(pa, "rb") as f:
            da = plistlib.load(f)
        with open(pb, "rb") as f:
            db = plistlib.load(f)
        self.assertEqual(da["Label"], "com.fulcra.coord.listener.agent-a-h-r")
        self.assertEqual(db["Label"], "com.fulcra.coord.listener.agent-b-h-r")
        self.assertIn("agent-a:h:r", da["ProgramArguments"])
        self.assertIn("agent-b:h:r", db["ProgramArguments"])

    def test_uninstall_one_agent_leaves_the_other(self):
        """uninstall A removes only A's plist; B's stays intact."""
        from fulcra_coord import listener
        with patch("sys.platform", "darwin"):
            listener.install_listener(agent="agent-a:h:r", target_dir=self.target)
            listener.install_listener(agent="agent-b:h:r", target_dir=self.target)
            listener.install_listener(agent="agent-a:h:r", target_dir=self.target,
                                      uninstall=True)
        pa = os.path.join(self.target,
                          "com.fulcra.coord.listener.agent-a-h-r.plist")
        pb = os.path.join(self.target,
                          "com.fulcra.coord.listener.agent-b-h-r.plist")
        self.assertFalse(os.path.exists(pa))
        self.assertTrue(os.path.exists(pb))

    def test_plan_reports_per_agent_plist_path(self):
        """The returned plan's writes path carries the per-agent slug."""
        from fulcra_coord import listener
        with patch("sys.platform", "darwin"):
            plan = listener.install_listener(agent="agent-a:h:r",
                                             target_dir=self.target)
        self.assertEqual(len(plan["writes"]), 1)
        self.assertTrue(
            plan["writes"][0].endswith(
                "com.fulcra.coord.listener.agent-a-h-r.plist"))

    # --- per-agent cron identity -------------------------------------------

    def test_two_agents_get_two_managed_cron_blocks(self):
        """install A then B (crontab path) -> two managed marker blocks, one
        per agent; the slug is embedded in each marker so they don't collide."""
        from fulcra_coord import listener
        crontab = os.path.join(self.tmp, "crontab.txt")
        with patch("sys.platform", "linux"), \
             patch("fulcra_coord.cli_invocation.resolve_cli_argv",
                   return_value=["/opt/bin/fulcra-coord"]):
            listener.install_listener(agent="agent-a:h:r", target_dir=self.tmp,
                                      crontab_path=crontab)
            listener.install_listener(agent="agent-b:h:r", target_dir=self.tmp,
                                      crontab_path=crontab)
        body = open(crontab).read()
        self.assertIn("fulcra-coord-listener:agent-a-h-r", body)
        self.assertIn("fulcra-coord-listener:agent-b-h-r", body)
        self.assertIn("agent-a:h:r", body)
        self.assertIn("agent-b:h:r", body)
        # two managed command lines (one per agent)
        self.assertEqual(body.count("notify-inbox"), 2)

    def test_cron_uninstall_one_agent_leaves_the_other(self):
        """uninstall A strips only A's managed block; B's line remains."""
        from fulcra_coord import listener
        crontab = os.path.join(self.tmp, "crontab.txt")
        with patch("sys.platform", "linux"), \
             patch("fulcra_coord.cli_invocation.resolve_cli_argv",
                   return_value=["/opt/bin/fulcra-coord"]):
            listener.install_listener(agent="agent-a:h:r", target_dir=self.tmp,
                                      crontab_path=crontab)
            listener.install_listener(agent="agent-b:h:r", target_dir=self.tmp,
                                      crontab_path=crontab)
            listener.install_listener(agent="agent-a:h:r", target_dir=self.tmp,
                                      crontab_path=crontab, uninstall=True)
        body = open(crontab).read()
        self.assertNotIn("fulcra-coord-listener:agent-a-h-r", body)
        self.assertNotIn("agent-a:h:r", body)
        self.assertIn("fulcra-coord-listener:agent-b-h-r", body)
        self.assertIn("agent-b:h:r", body)

    def test_strip_managed_cron_is_agent_scoped(self):
        """_strip_managed_cron / _is_managed_cron_command operate on the GIVEN
        agent's marker only; another agent's managed block is left intact."""
        from fulcra_coord import listener
        marker_a = listener._cron_marker_for("agent-a:h:r")
        marker_b = listener._cron_marker_for("agent-b:h:r")
        line_b = ("*/10 * * * * PATH=/x /opt/bin/fulcra-coord notify-inbox "
                  "--agent agent-b:h:r >/dev/null 2>&1")
        text = (marker_a + "\n"
                "*/10 * * * * PATH=/x /opt/bin/fulcra-coord notify-inbox "
                "--agent agent-a:h:r >/dev/null 2>&1\n"
                + marker_b + "\n" + line_b + "\n")
        stripped = listener._strip_managed_cron(text, "agent-a:h:r")
        self.assertNotIn(marker_a, stripped)
        self.assertNotIn("agent-a:h:r", stripped)
        self.assertIn(marker_b, stripped)
        self.assertIn(line_b, stripped)
        # The guard only claims this agent's line.
        self.assertTrue(
            listener._is_managed_cron_command(line_b, "agent-b:h:r"))
        self.assertFalse(
            listener._is_managed_cron_command(line_b, "agent-a:h:r"))

    # --- legacy (un-slugged) plist migration --------------------------------

    def test_install_supersedes_legacy_plist_for_same_agent(self):
        """A legacy un-slugged com.fulcra.coord.listener.plist that watches
        agent A is removed when A reinstalls (prevents A double-running)."""
        from fulcra_coord import listener
        legacy = os.path.join(self.target, "com.fulcra.coord.listener.plist")
        os.makedirs(self.target, exist_ok=True)
        import plistlib
        with open(legacy, "wb") as f:
            plistlib.dump({
                "Label": "com.fulcra.coord.listener",
                "ProgramArguments": ["/old/fulcra-coord", "notify-inbox",
                                     "--agent", "agent-a:h:r"],
            }, f)
        with patch("sys.platform", "darwin"), \
             patch("fulcra_coord.listener._launchctl_unload"):
            plan = listener.install_listener(agent="agent-a:h:r",
                                             target_dir=self.target)
        self.assertFalse(os.path.exists(legacy))
        self.assertIn(legacy, plan.get("removes", []))
        # The new per-agent plist still exists.
        self.assertTrue(os.path.exists(os.path.join(
            self.target, "com.fulcra.coord.listener.agent-a-h-r.plist")))

    def test_install_leaves_legacy_plist_for_different_agent(self):
        """A legacy plist watching agent B is LEFT when agent A installs — B
        migrates on its own reinstall, not when an unrelated agent installs."""
        from fulcra_coord import listener
        legacy = os.path.join(self.target, "com.fulcra.coord.listener.plist")
        os.makedirs(self.target, exist_ok=True)
        import plistlib
        with open(legacy, "wb") as f:
            plistlib.dump({
                "Label": "com.fulcra.coord.listener",
                "ProgramArguments": ["/old/fulcra-coord", "notify-inbox",
                                     "--agent", "agent-b:h:r"],
            }, f)
        with patch("sys.platform", "darwin"), \
             patch("fulcra_coord.listener._launchctl_unload"):
            plan = listener.install_listener(agent="agent-a:h:r",
                                             target_dir=self.target)
        self.assertTrue(os.path.exists(legacy))
        self.assertNotIn(legacy, plan.get("removes", []))

    def test_uninstall_removes_legacy_plist_for_same_agent_only(self):
        """Uninstall must also remove a pre-0.5.3 legacy plist when it watches
        this agent; otherwise upgrading then uninstalling leaves the old listener
        still polling the target agent. A different agent's legacy plist stays."""
        from fulcra_coord import listener
        legacy = os.path.join(self.target, "com.fulcra.coord.listener.plist")
        os.makedirs(self.target, exist_ok=True)
        import plistlib
        with open(legacy, "wb") as f:
            plistlib.dump({
                "Label": "com.fulcra.coord.listener",
                "ProgramArguments": ["/old/fulcra-coord", "notify-inbox",
                                     "--agent", "agent-a:h:r"],
            }, f)
        with patch("sys.platform", "darwin"), \
             patch("fulcra_coord.listener._launchctl_unload"):
            plan = listener.install_listener(agent="agent-a:h:r",
                                             target_dir=self.target,
                                             uninstall=True)
        self.assertFalse(os.path.exists(legacy))
        self.assertIn(legacy, plan.get("removes", []))

        with open(legacy, "wb") as f:
            plistlib.dump({
                "Label": "com.fulcra.coord.listener",
                "ProgramArguments": ["/old/fulcra-coord", "notify-inbox",
                                     "--agent", "agent-b:h:r"],
            }, f)
        with patch("sys.platform", "darwin"), \
             patch("fulcra_coord.listener._launchctl_unload"):
            plan = listener.install_listener(agent="agent-a:h:r",
                                             target_dir=self.target,
                                             uninstall=True)
        self.assertTrue(os.path.exists(legacy))
        self.assertNotIn(legacy, plan.get("removes", []))

    def test_dry_run_reports_legacy_supersede_without_writing(self):
        """--dry-run names the per-agent plist and reports it would supersede a
        matching legacy plist, but writes/removes nothing."""
        from fulcra_coord import listener
        legacy = os.path.join(self.target, "com.fulcra.coord.listener.plist")
        os.makedirs(self.target, exist_ok=True)
        import plistlib
        with open(legacy, "wb") as f:
            plistlib.dump({
                "Label": "com.fulcra.coord.listener",
                "ProgramArguments": ["/old/fulcra-coord", "notify-inbox",
                                     "--agent", "agent-a:h:r"],
            }, f)
        with patch("sys.platform", "darwin"):
            plan = listener.install_listener(agent="agent-a:h:r",
                                             target_dir=self.target,
                                             dry_run=True)
        self.assertTrue(os.path.exists(legacy))  # nothing removed
        self.assertTrue(plan["writes"][0].endswith(
            "com.fulcra.coord.listener.agent-a-h-r.plist"))
        self.assertTrue(plan.get("supersedes_legacy"))

    def test_cron_install_supersedes_legacy_marker_for_same_agent(self):
        """A legacy un-slugged managed marker line for agent A is superseded
        when A installs; a legacy marker for B is left."""
        from fulcra_coord import listener
        crontab = os.path.join(self.tmp, "crontab.txt")
        legacy_marker = "# fulcra-coord-listener (managed; do not edit this line)"
        legacy_cmd = ("*/10 * * * * PATH=/x /old/fulcra-coord notify-inbox "
                      "--agent agent-a:h:r >/dev/null 2>&1")
        with open(crontab, "w") as f:
            f.write(legacy_marker + "\n" + legacy_cmd + "\n")
        with patch("sys.platform", "linux"), \
             patch("fulcra_coord.cli_invocation.resolve_cli_argv",
                   return_value=["/opt/bin/fulcra-coord"]):
            listener.install_listener(agent="agent-a:h:r", target_dir=self.tmp,
                                      crontab_path=crontab)
        body = open(crontab).read()
        # legacy un-slugged marker for A is gone; exactly one (slugged) block.
        self.assertNotIn(legacy_marker + "\n", body)
        self.assertIn("fulcra-coord-listener:agent-a-h-r", body)
        self.assertEqual(body.count("notify-inbox"), 1)


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
        self.assertTrue(os.path.exists(os.path.join(
            self.target, "com.fulcra.coord.listener.codex-h-r.plist")))


class TestEnsureListener(unittest.TestCase):
    """Self-healing re-arm (spec 2026-06-09 Task 7): connect idempotently
    re-installs a missing listener, best-effort, never raising. Each test
    forces FULCRA_COORD_ENSURE_LISTENER=1 because conftest defaults it to 0
    (so no test that reaches cmd_connect can ever probe/write the REAL
    ~/Library/LaunchAgents or live crontab)."""

    def test_ensure_listener_installs_when_missing(self):
        from fulcra_coord import listener
        calls = []
        with patch.dict(os.environ, {"FULCRA_COORD_ENSURE_LISTENER": "1"}), \
             patch.object(listener, "_listener_armed", return_value=False), \
             patch.object(listener, "install_listener",
                          side_effect=lambda **kw: calls.append(kw) or 0):
            listener.ensure_listener(agent="a:h:r")
        self.assertEqual(len(calls), 1)

    def test_ensure_listener_loads_launchd_plist_after_install(self):
        from fulcra_coord import listener
        plan = {"mechanism": "launchd", "writes": ["/tmp/a.plist"]}
        with patch.dict(os.environ, {"FULCRA_COORD_ENSURE_LISTENER": "1"}), \
             patch.object(listener, "_listener_armed", return_value=False), \
             patch.object(listener, "install_listener", return_value=plan), \
             patch.object(listener.subprocess, "run") as run:
            listener.ensure_listener(agent="a:h:r")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0],
                         ["launchctl", "load", "-w", "/tmp/a.plist"])

    def test_listener_armed_requires_loaded_launchd_job(self):
        from fulcra_coord import listener
        with tempfile.TemporaryDirectory() as tmp:
            plist = Path(tmp) / listener._plist_name_for("a:h:r")
            plist.write_text("<plist/>")

            missing = subprocess.CompletedProcess(
                ["launchctl", "list"], 0, stdout="", stderr="")
            loaded = subprocess.CompletedProcess(
                ["launchctl", "list"], 0,
                stdout=f"123\t0\t{listener._label_for('a:h:r')}\n", stderr="")

            with patch.object(listener.scheduler_env, "is_macos", return_value=True), \
                 patch.object(listener.scheduler_env, "launchagents_dir",
                              return_value=Path(tmp)), \
                 patch.object(listener.subprocess, "run", return_value=missing):
                self.assertFalse(listener._listener_armed("a:h:r"))

            with patch.object(listener.scheduler_env, "is_macos", return_value=True), \
                 patch.object(listener.scheduler_env, "launchagents_dir",
                              return_value=Path(tmp)), \
                 patch.object(listener.subprocess, "run", return_value=loaded):
                self.assertTrue(listener._listener_armed("a:h:r"))

    def test_ensure_listener_noop_when_armed(self):
        from fulcra_coord import listener
        with patch.dict(os.environ, {"FULCRA_COORD_ENSURE_LISTENER": "1"}), \
             patch.object(listener, "_listener_armed", return_value=True), \
             patch.object(listener, "install_listener") as inst:
            listener.ensure_listener(agent="a:h:r")
        inst.assert_not_called()

    def test_ensure_listener_never_raises(self):
        from fulcra_coord import listener
        with patch.dict(os.environ, {"FULCRA_COORD_ENSURE_LISTENER": "1"}), \
             patch.object(listener, "_listener_armed",
                          side_effect=RuntimeError("boom")):
            listener.ensure_listener(agent="a:h:r")   # must not raise

    def test_ensure_listener_env_opt_out(self):
        from fulcra_coord import listener
        with patch.dict(os.environ, {"FULCRA_COORD_ENSURE_LISTENER": "0"}), \
             patch.object(listener, "_listener_armed") as armed, \
             patch.object(listener, "install_listener") as inst:
            listener.ensure_listener(agent="a:h:r")
        armed.assert_not_called()
        inst.assert_not_called()


class TestWebhookNotifier(unittest.TestCase):
    """Cross-platform push notifier: webhook (ntfy/slack/discord/json) + native
    desktop, both best-effort and independent. Network + subprocess fully mocked.
    """

    def setUp(self):
        for k in ("FULCRA_COORD_NOTIFY_WEBHOOK", "FULCRA_COORD_NOTIFY_FORMAT",
                  "FULCRA_COORD_NOTIFY_TIMEOUT"):
            os.environ.pop(k, None)

    def tearDown(self):
        for k in ("FULCRA_COORD_NOTIFY_WEBHOOK", "FULCRA_COORD_NOTIFY_FORMAT",
                  "FULCRA_COORD_NOTIFY_TIMEOUT"):
            os.environ.pop(k, None)

    # -- _webhook_format -----------------------------------------------------
    def test_format_autodetect_discord(self):
        from fulcra_coord import listener
        self.assertEqual(
            listener._webhook_format(
                "https://discord.com/api/webhooks/1/abc"), "discord")

    def test_format_autodetect_slack(self):
        from fulcra_coord import listener
        self.assertEqual(
            listener._webhook_format(
                "https://hooks.slack.com/services/T/B/x"), "slack")

    def test_format_autodetect_ntfy_default(self):
        from fulcra_coord import listener
        self.assertEqual(
            listener._webhook_format("https://ntfy.sh/mytopic"), "ntfy")

    def test_format_explicit_env_overrides_host(self):
        from fulcra_coord import listener
        os.environ["FULCRA_COORD_NOTIFY_FORMAT"] = "json"
        self.assertEqual(
            listener._webhook_format("https://hooks.slack.com/x"), "json")

    def test_format_bogus_explicit_falls_back_to_autodetect(self):
        from fulcra_coord import listener
        os.environ["FULCRA_COORD_NOTIFY_FORMAT"] = "carrier-pigeon"
        self.assertEqual(
            listener._webhook_format("https://discord.com/api/webhooks/1"),
            "discord")

    # -- _build_webhook_request ----------------------------------------------
    def test_build_ntfy_request(self):
        from fulcra_coord import listener
        os.environ["FULCRA_COORD_NOTIFY_FORMAT"] = "ntfy"
        req = listener._build_webhook_request(
            "https://ntfy.sh/t", "fulcra-coord", "hello world")
        self.assertEqual(req.full_url, "https://ntfy.sh/t")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.data, b"hello world")
        # urllib title-cases header keys.
        self.assertEqual(req.headers["Title"], "fulcra-coord")
        self.assertIn("text/plain", req.headers["Content-type"])

    def test_build_ntfy_request_sanitizes_nonascii_title(self):
        from fulcra_coord import listener
        os.environ["FULCRA_COORD_NOTIFY_FORMAT"] = "ntfy"
        req = listener._build_webhook_request(
            "https://ntfy.sh/t", "⛔ needs you", "msg")
        # Non-ascii title is not header-safe -> falls back to plain marker.
        self.assertEqual(req.headers["Title"], "fulcra-coord")
        # Body is still the raw message.
        self.assertEqual(req.data, b"msg")

    def test_build_slack_request(self):
        from fulcra_coord import listener
        os.environ["FULCRA_COORD_NOTIFY_FORMAT"] = "slack"
        req = listener._build_webhook_request(
            "https://hooks.slack.com/x", "T", "M")
        self.assertEqual(req.get_method(), "POST")
        self.assertIn("application/json", req.headers["Content-type"])
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["text"], "T: M")

    def test_build_discord_request_and_truncates(self):
        from fulcra_coord import listener
        os.environ["FULCRA_COORD_NOTIFY_FORMAT"] = "discord"
        req = listener._build_webhook_request(
            "https://discord.com/api/webhooks/1", "Title", "x" * 5000)
        self.assertEqual(req.get_method(), "POST")
        self.assertIn("application/json", req.headers["Content-type"])
        body = json.loads(req.data.decode("utf-8"))
        self.assertTrue(body["content"].startswith("**Title**\n"))
        self.assertLessEqual(len(body["content"]), 1900)

    def test_build_json_request(self):
        from fulcra_coord import listener
        os.environ["FULCRA_COORD_NOTIFY_FORMAT"] = "json"
        req = listener._build_webhook_request(
            "https://example.com/hook", "T", "M")
        self.assertEqual(req.get_method(), "POST")
        self.assertIn("application/json", req.headers["Content-type"])
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["title"], "T")
        self.assertEqual(body["message"], "M")

    # -- _post_webhook -------------------------------------------------------
    def test_post_webhook_success(self):
        from fulcra_coord import listener
        import contextlib
        cm = contextlib.nullcontext(types.SimpleNamespace(status=200))
        with patch("fulcra_coord.listener.urllib.request.urlopen",
                   return_value=cm) as uo:
            ok = listener._post_webhook(
                "https://ntfy.sh/t", "fulcra-coord", "hi")
        self.assertTrue(ok)
        uo.assert_called_once()
        # The first positional arg is the built Request.
        sent = uo.call_args[0][0]
        self.assertEqual(sent.full_url, "https://ntfy.sh/t")

    def test_post_webhook_failure_swallowed(self):
        from fulcra_coord import listener
        import urllib.error
        with patch("fulcra_coord.listener.urllib.request.urlopen",
                   side_effect=urllib.error.URLError("boom")):
            ok = listener._post_webhook(
                "https://ntfy.sh/t", "fulcra-coord", "hi")
        self.assertFalse(ok)

    # -- _deliver orchestration ----------------------------------------------
    def test_deliver_calls_both_when_webhook_set(self):
        from fulcra_coord import listener
        os.environ["FULCRA_COORD_NOTIFY_WEBHOOK"] = "https://ntfy.sh/t"
        with patch("fulcra_coord.listener._post_webhook") as pw, \
             patch("fulcra_coord.listener._emit_native") as en:
            listener._deliver("msg", title="t")
        pw.assert_called_once()
        en.assert_called_once_with("msg", "t")

    def test_deliver_skips_webhook_when_unset(self):
        from fulcra_coord import listener
        with patch("fulcra_coord.listener._post_webhook") as pw, \
             patch("fulcra_coord.listener._emit_native") as en:
            listener._deliver("msg", title="t")
        pw.assert_not_called()
        en.assert_called_once()

    def test_deliver_native_runs_even_if_webhook_raises(self):
        from fulcra_coord import listener
        os.environ["FULCRA_COORD_NOTIFY_WEBHOOK"] = "https://ntfy.sh/t"
        with patch("fulcra_coord.listener._post_webhook",
                   side_effect=Exception("explode")), \
             patch("fulcra_coord.listener._emit_native") as en:
            listener._deliver("msg", title="t")  # must not raise
        en.assert_called_once()

    # -- public surface routes through _deliver ------------------------------
    def test_emit_notification_routes_through_deliver(self):
        from fulcra_coord import listener
        with patch("fulcra_coord.listener._deliver") as dl:
            listener.emit_notification("codex:h:r", 3)
        dl.assert_called_once()
        msg = dl.call_args[0][0]
        self.assertIn("3 directive(s)", msg)
        self.assertEqual(dl.call_args[1]["title"], "fulcra-coord")

    def test_emit_message_routes_through_deliver(self):
        from fulcra_coord import listener
        with patch("fulcra_coord.listener._deliver") as dl:
            listener.emit_message("hello", title="alert")
        dl.assert_called_once_with("hello", title="alert")

    # -- _emit_native Linux branch -------------------------------------------
    def test_emit_native_linux_uses_notify_send(self):
        from fulcra_coord import listener
        with patch("fulcra_coord.listener.scheduler_env.is_macos",
                   return_value=False), \
             patch("fulcra_coord.listener.sys.platform", "linux"), \
             patch("fulcra_coord.listener.shutil.which",
                   return_value="/usr/bin/notify-send"), \
             patch("fulcra_coord.listener.subprocess.run") as run:
            listener._emit_native("body", "title")
        run.assert_called_once()
        argv = run.call_args[0][0]
        self.assertEqual(argv[0], "notify-send")
        self.assertEqual(argv[1], "title")
        self.assertEqual(argv[2], "body")

    def test_emit_native_linux_falls_back_to_stderr(self):
        from fulcra_coord import listener
        with patch("fulcra_coord.listener.scheduler_env.is_macos",
                   return_value=False), \
             patch("fulcra_coord.listener.sys.platform", "linux"), \
             patch("fulcra_coord.listener.shutil.which", return_value=None), \
             patch("fulcra_coord.listener.subprocess.run") as run:
            listener._emit_native("body", "title")  # must not raise
        run.assert_not_called()


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

    def _seen_path(self, agent):
        from fulcra_coord import cache, listener
        return cache.cache_root() / f"inbox-notified-{listener.agent_slug(agent)}.json"

    def test_inbox_notify_dedups_across_ticks(self):
        """Agent-inbox notify fires once for NEW ids: a re-tick over the SAME
        items does not re-alert, a genuinely new id does, and the seen-set on
        disk tracks the current ids (so resolved items drop and can re-alert)."""
        from fulcra_coord.cli import cmd_notify_inbox
        a, b = _directive("codex:h:r"), _directive("codex:h:r")
        cache.write_cached_task(a)
        cache.write_cached_task(b)
        ns = types.SimpleNamespace(agent="codex:h:r")
        with patch("fulcra_coord.listener.emit_notification") as emit:
            # First tick: two new items -> one emit with count 2.
            cmd_notify_inbox(ns, backend=self.fake_backend)
            self.assertEqual(emit.call_count, 1)
            self.assertEqual(emit.call_args[0][1], 2)
            seen = set(json.loads(self._seen_path("codex:h:r").read_text()))
            self.assertEqual(seen, {a["id"], b["id"]})

            # Second identical tick: nothing new -> no emit.
            emit.reset_mock()
            cmd_notify_inbox(ns, backend=self.fake_backend)
            emit.assert_not_called()

            # Third tick adds one NEW directive -> emit once with count 1.
            emit.reset_mock()
            c = _directive("codex:h:r")
            cache.write_cached_task(c)
            cmd_notify_inbox(ns, backend=self.fake_backend)
            self.assertEqual(emit.call_count, 1)
            self.assertEqual(emit.call_args[0][1], 1)
            seen = set(json.loads(self._seen_path("codex:h:r").read_text()))
            self.assertEqual(seen, {a["id"], b["id"], c["id"]})

    def test_notify_skips_overdue_loop_suffix_by_default(self):
        """The listener's new-item alert must not pay the optional loop scan
        unless explicitly enabled; a wedged decoration pass suppresses future
        launchd ticks."""
        from fulcra_coord.cli import cmd_notify_inbox
        cache.write_cached_task(_directive("codex:h:r"))
        with patch("fulcra_coord.inbox._overdue_loop_suffix") as suffix, \
             patch("fulcra_coord.listener.emit_notification") as emit:
            rc = cmd_notify_inbox(types.SimpleNamespace(agent="codex:h:r"),
                                  backend=self.fake_backend)
        self.assertEqual(rc, 0)
        suffix.assert_not_called()
        emit.assert_called_once()
        self.assertEqual(emit.call_args.kwargs.get("extra"), "")

    def test_notify_skips_stale_summary_fallback_by_default(self):
        """A scheduled listener tick must not win the stale-view fallback claim
        and start rebuilding the bus from every task body."""
        from fulcra_coord.cli import cmd_notify_inbox
        with patch("fulcra_coord.inbox._load_task_summaries",
                   return_value=[]) as load:
            rc = cmd_notify_inbox(types.SimpleNamespace(agent="codex:h:r"),
                                  backend=self.fake_backend)
        self.assertEqual(rc, 0)
        self.assertTrue(load.call_args.kwargs["skip_stale_fallback"])

    def test_notify_message_includes_overdue_loop_count(self):
        """An overdue open loop OPENED BY the notifying agent rides the inbox
        notification as a " · N overdue" suffix when the optional scan is
        enabled (spec 2026-06-09 Task 7).
        Asserted at the delivered-message level (the suffix is composed in
        cmd_notify_inbox, formatted by listener.emit_notification). A sub-log
        shard is seeded beside the record to pin the top-level-only filter —
        which, post filter-before-download, must reject the shard by PATH (the
        shard is never even downloaded)."""
        from fulcra_coord import remote
        from fulcra_coord.cli import cmd_notify_inbox
        cache.write_cached_task(_directive("codex:h:r"))  # makes notify fire
        prefix = remote.directives_prefix()
        record_path = prefix + "DIR-OVERDUE-1.json"
        shard_path = prefix + "DIR-OVERDUE-1/acks/codex-h-r.json"
        loop = {
            "id": "DIR-OVERDUE-1", "kind": "review", "state": "requested",
            "from": "codex:h:r", "audience": "other:h:r",
            "title": "please review", "expects_response": True,
            "created_at": "2026-01-01T00:00:00Z",  # far past the 24h review SLA
        }

        def fake_list_files(p, *, backend=None, **kw):
            if p == prefix:
                # ack shard beside the record: must be filtered out by path
                return [record_path, shard_path]
            return []

        downloaded = []

        def fake_download_json(p, *, backend=None, **kw):
            downloaded.append(p)
            return loop if p == record_path else None

        with patch("fulcra_coord.remote.list_files", side_effect=fake_list_files), \
             patch("fulcra_coord.remote.download_json",
                   side_effect=fake_download_json), \
             patch.dict(os.environ, {"FULCRA_COORD_NOTIFY_OVERDUE_SUFFIX": "1"}), \
             patch("fulcra_coord.listener._deliver") as dl:
            rc = cmd_notify_inbox(types.SimpleNamespace(agent="codex:h:r"),
                                  backend=self.fake_backend)
        self.assertEqual(rc, 0)
        inbox_msgs = [c[0][0] for c in dl.call_args_list
                      if "directive(s) waiting" in c[0][0]]
        self.assertEqual(len(inbox_msgs), 1)
        self.assertIn("1 overdue", inbox_msgs[0])
        # Filter-before-download: the sub-log shard was rejected by PATH and
        # never cost a download subprocess.
        self.assertNotIn(shard_path, downloaded)


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

    def test_ack_visible_summary_when_task_body_missing(self):
        """A directive visible via summaries must be ackable even if body load fails.

        This matches a cross-agent stale/dangling view case: ``inbox`` lists the
        summary, but ``inbox --ack`` cannot load ``tasks/<id>.json`` to append an
        event. The fallback records the ack in the summaries aggregate and
        rebuilds views so the listener stops re-notifying.
        """
        from fulcra_coord.cli import cmd_inbox
        from fulcra_coord import remote
        import io, contextlib

        me = "codex:h:r"
        d = _directive(me)
        summary = schema.task_summary(d)
        summaries = views.build_all_views([summary])["summaries"]
        remote.upload_json(summaries, remote.view_remote_path("summaries"),
                           backend=self.fake_backend)

        self.assertEqual([item["id"] for item in self._inbox_json(me)], [d["id"]])

        with contextlib.redirect_stdout(io.StringIO()):
            rc = cmd_inbox(self._ns(agent=me, format="json", ack=d["id"]),
                           backend=self.fake_backend)
        self.assertEqual(rc, 0)
        self.assertEqual(self._inbox_json(me), [])

        saved = remote.download_json(remote.view_remote_path("summaries"),
                                     backend=self.fake_backend)
        acked = [item for item in saved["summaries"] if item["id"] == d["id"]][0]
        self.assertEqual(acked["acked_by"], [me])

    def test_reconcile_preserves_summary_only_ack_when_body_returns(self):
        """A transient body-load miss must not resurrect an already-acked inbox item."""
        from fulcra_coord.cli import cmd_reconcile, cmd_tell
        from fulcra_coord import remote
        import io, contextlib

        me = "codex:h:r"
        tell_args = self._ns(assignee=me, title="durable ack", workstream="general",
                             priority="P2", next="", summary="",
                             **{"from": "boss:h:r"})
        with contextlib.redirect_stdout(io.StringIO()):
            cmd_tell(tell_args, backend=self.fake_backend)
        summary = self._inbox_json(me)[0]
        summary["acked_by"] = [me]

        # Body is now loadable again, but it lacks the inbox_ack event that the
        # summary-only fallback could not append during the transient miss.
        remote.upload_json({"summaries": [summary]},
                           remote.view_remote_path("summaries"),
                           backend=self.fake_backend)

        with contextlib.redirect_stdout(io.StringIO()):
            rc = cmd_reconcile(self._ns(), backend=self.fake_backend)
        self.assertEqual(rc, 0)

        saved = remote.download_json(remote.view_remote_path("summaries"),
                                     backend=self.fake_backend)
        acked = [item for item in saved["summaries"] if item["id"] == summary["id"]][0]
        self.assertEqual(acked["acked_by"], [me])
        self.assertEqual(self._inbox_json(me), [])

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
# BUG 1 (data-loss): _try_merge must carry ALL non-event scalar/dict fields
# from the MORE-RECENT side, not a 4-field allowlist. not_before/due/blocked_on/
# priority/title/etc. set on the newer side were previously kept from the merge
# base and silently LOST.
# ---------------------------------------------------------------------------

class TestTryMergeCarriesAllNewerFields(unittest.TestCase):
    def test_newer_side_scheduling_fields_survive(self):
        """Same-status concurrent edits: the more-recent side set not_before/
        due/blocked_on; the other side is a plain summary update. Merged must
        keep not_before/due/blocked_on from the newer side."""
        from fulcra_coord.cli import _try_merge
        from datetime import datetime, timezone, timedelta
        base = _with_status(_sample_task(), "active")

        # Older side: plain summary update.
        t_old = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
        older = apply_update(base, by="agent-a", summary="just a note", dt=t_old)

        # Newer side: a summary update that also sets scheduling fields.
        t_new = t_old + timedelta(seconds=30)
        newer = apply_update(base, by="agent-b", summary="scheduled it", dt=t_new)
        newer["not_before"] = "2026-06-02T09:00:00Z"
        newer["due"] = "2026-06-03T17:00:00Z"
        newer["blocked_on"] = "TASK-OTHER"

        result = _try_merge(older, newer)
        self.assertIsNotNone(result)
        self.assertEqual(result.get("not_before"), "2026-06-02T09:00:00Z")
        self.assertEqual(result.get("due"), "2026-06-03T17:00:00Z")
        self.assertEqual(result.get("blocked_on"), "TASK-OTHER")

    def test_title_and_priority_edit_survives(self):
        """A title/priority edit on the newer side must survive the merge."""
        from fulcra_coord.cli import _try_merge
        from datetime import datetime, timezone, timedelta
        base = _with_status(_sample_task(), "active")

        t_old = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
        older = apply_update(base, by="agent-a", summary="note", dt=t_old)

        t_new = t_old + timedelta(seconds=30)
        newer = apply_update(base, by="agent-b", summary="retitled", dt=t_new)
        newer["title"] = "Renamed task"
        newer["priority"] = "P0"

        result = _try_merge(older, newer)
        self.assertIsNotNone(result)
        self.assertEqual(result.get("title"), "Renamed task")
        self.assertEqual(result.get("priority"), "P0")

    def test_events_still_unioned_and_acked_by_merged(self):
        """The event-union + acked_by-union must remain intact after the rewrite."""
        from fulcra_coord.cli import _try_merge
        from datetime import datetime, timezone, timedelta
        base = _with_status(_sample_task(), "active")

        t_old = datetime(2026, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
        older = apply_update(base, by="agent-a", summary="local note", dt=t_old)
        older["acked_by"] = ["agent-a"]

        t_new = t_old + timedelta(seconds=30)
        newer = apply_update(base, by="agent-b", summary="remote note", dt=t_new)
        newer["acked_by"] = ["agent-b"]

        result = _try_merge(older, newer)
        self.assertIsNotNone(result)
        summaries = [e.get("summary") for e in result.get("events", [])]
        self.assertIn("local note", summaries)
        self.assertIn("remote note", summaries)
        self.assertEqual(set(result.get("acked_by") or []), {"agent-a", "agent-b"})


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


class TestInboxIndexPrefixOwnershipAgreement(unittest.TestCase):
    """BUG 3 (HIGH): index.counts.inbox must not over-count vs inbox_for.

    is_open_directive excluded self-owned only via exact owner_agent==assignee;
    inbox_for / the owner's read path treat a directive whose assignee is a
    short-id PREFIX of its owner_agent as the owner's own work (hidden). So a
    directive owner_agent='claude-code:h:r', assignee='claude-code' was hidden
    by the read path but still counted by the index — they disagreed.
    """

    def _directive_prefix_owner(self):
        return {
            "id": "TASK-PREFIX-OWN", "title": "self-owned via prefix",
            "status": "proposed", "workstream": "general",
            "owner_agent": "claude-code:h:r", "assignee": "claude-code",
            "priority": "P2", "updated_at": "2026-06-03T00:00:00Z",
            "tags": [], "events": [],
        }

    def test_inbox_for_hides_owners_own_prefixed_directive(self):
        from fulcra_coord.views import inbox_for
        t = self._directive_prefix_owner()
        # The owner querying its own inbox must NOT see its own directive.
        self.assertEqual(inbox_for("claude-code:h:r", [t]), [])

    def test_index_does_not_count_owners_own_prefixed_directive(self):
        from fulcra_coord.views import build_index
        t = self._directive_prefix_owner()
        idx = build_index([t])
        # The read path hides it; the index must agree and not count it.
        self.assertEqual(idx["counts"]["inbox"], {},
                         "index must not count a directive the owner's read path hides")

    def test_is_open_directive_excludes_prefix_self_owned(self):
        from fulcra_coord.views import is_open_directive
        t = self._directive_prefix_owner()
        self.assertFalse(is_open_directive(t, "claude-code"),
                         "prefix-self-owned directive is not an open inbox item")

    def test_normal_case_still_counts_and_shows(self):
        # A genuine cross-agent directive (owner is a DIFFERENT kind) must still
        # be counted and shown — the fix must not over-suppress.
        from fulcra_coord.views import inbox_for, build_index, is_open_directive
        t = self._directive_prefix_owner()
        t["owner_agent"] = "openclaw:host:repo"  # not a prefix-relation with assignee
        self.assertTrue(is_open_directive(t, "claude-code"))
        self.assertEqual(len(inbox_for("claude-code:h:r", [t])), 1)
        idx = build_index([t])
        self.assertEqual(idx["counts"]["inbox"].get("claude-code"), 1)


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


# ---------------------------------------------------------------------------
# Inbox auto-aging — stale informational broadcasts drop out of the live inbox
# view after FULCRA_COORD_INBOX_AGE_DAYS, WITHOUT touching the task (non-
# destructive: status/file unchanged; a peer on an older CLI still sees it).
# Concrete-assignee directives (real asks) are NEVER aged out, regardless of age.
# ---------------------------------------------------------------------------

class TestInboxBroadcastAging(unittest.TestCase):
    from datetime import datetime, timezone
    NOW = datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)

    def _aged_broadcast(self, days_old):
        """A proposed broadcast whose updated_at is `days_old` days before NOW."""
        from fulcra_coord.views import BROADCAST
        from datetime import timedelta
        stamp = (self.NOW - timedelta(days=days_old)).isoformat().replace("+00:00", "Z")
        return _directive(BROADCAST, owner="boss:h:r", updated_at=stamp)

    def test_old_broadcast_excluded_from_default_inbox(self):
        # 5 days old > default 3-day cutoff -> aged out of the default inbox.
        from fulcra_coord.views import inbox_for
        d = self._aged_broadcast(5)
        ids = [s["id"] for s in inbox_for("openclaw:host:repo", [d], now=self.NOW)]
        self.assertNotIn(d["id"], ids,
                         "a broadcast older than the cutoff must not appear by default")

    def test_recent_broadcast_included_in_default_inbox(self):
        # 1 day old < default 3-day cutoff -> still in the inbox.
        from fulcra_coord.views import inbox_for
        d = self._aged_broadcast(1)
        ids = [s["id"] for s in inbox_for("openclaw:host:repo", [d], now=self.NOW)]
        self.assertIn(d["id"], ids,
                      "a broadcast within the cutoff must still appear")

    def test_concrete_assignee_never_aged_out(self):
        # A directive addressed to a CONCRETE agent (a real ask) is NEVER aged out,
        # no matter how old. Only wildcard broadcasts age. 99 days old, still shown.
        from fulcra_coord.views import inbox_for
        from datetime import timedelta
        stamp = (self.NOW - timedelta(days=99)).isoformat().replace("+00:00", "Z")
        d = _directive("openclaw:host:repo", owner="boss:h:r", updated_at=stamp)
        ids = [s["id"] for s in inbox_for("openclaw:host:repo", [d], now=self.NOW)]
        self.assertIn(d["id"], ids,
                      "a concrete-assignee directive must NEVER be aged out")

    def test_inbox_for_all_includes_aged_broadcast(self):
        # include_aged=True (the `inbox --all` path) bypasses the age filter.
        from fulcra_coord.views import inbox_for
        d = self._aged_broadcast(5)
        ids = [s["id"] for s in inbox_for("openclaw:host:repo", [d],
                                          now=self.NOW, include_aged=True)]
        self.assertIn(d["id"], ids,
                      "--all must reveal aged-out broadcasts")

    def test_cutoff_respects_env(self):
        # With FULCRA_COORD_INBOX_AGE_DAYS=10, a 5-day-old broadcast is NOT yet aged.
        from fulcra_coord.views import inbox_for
        d = self._aged_broadcast(5)
        old = os.environ.get("FULCRA_COORD_INBOX_AGE_DAYS")
        os.environ["FULCRA_COORD_INBOX_AGE_DAYS"] = "10"
        try:
            ids = [s["id"] for s in inbox_for("openclaw:host:repo", [d], now=self.NOW)]
        finally:
            if old is None:
                os.environ.pop("FULCRA_COORD_INBOX_AGE_DAYS", None)
            else:
                os.environ["FULCRA_COORD_INBOX_AGE_DAYS"] = old
        self.assertIn(d["id"], ids,
                      "a larger FULCRA_COORD_INBOX_AGE_DAYS must keep the broadcast visible")

    def test_aging_is_non_destructive(self):
        # The view filter must NOT mutate the task: status stays proposed, the
        # dict is unchanged. (A peer on an older CLI still sees it.)
        from fulcra_coord.views import inbox_for
        import copy
        d = self._aged_broadcast(5)
        before = copy.deepcopy(d)
        inbox_for("openclaw:host:repo", [d], now=self.NOW)
        self.assertEqual(d, before, "aging must not mutate the task")
        self.assertEqual(d["status"], "proposed", "aging must not change task status")

    def test_aged_broadcast_count_helper(self):
        # The CLI surfaces "N older broadcasts hidden"; the view exposes the count.
        from fulcra_coord.views import aged_out_inbox_count
        d = self._aged_broadcast(5)
        n = aged_out_inbox_count("openclaw:host:repo", [d], now=self.NOW)
        self.assertEqual(n, 1, "one aged-out broadcast should be counted")

    def test_no_aged_count_for_recent_broadcast(self):
        from fulcra_coord.views import aged_out_inbox_count
        d = self._aged_broadcast(1)
        n = aged_out_inbox_count("openclaw:host:repo", [d], now=self.NOW)
        self.assertEqual(n, 0, "a recent broadcast is not aged out")


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

    def test_version_is_0_15_5(self):
        # 0.15.5: listener hot-path cut — notify-inbox no longer pays the
        # optional overdue-loop directive scan by default, so launchd ticks can
        # notify and exit even on large directive buses. Opt back into the suffix
        # with FULCRA_COORD_NOTIFY_OVERDUE_SUFFIX=1.
        from fulcra_coord import __version__
        self.assertEqual(__version__, "0.15.5")


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

    # active, touched by a different agent than the owner (exercises the
    # last_touched_by summary field the digest's hand-off grouping reads)
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
             patch("fulcra_coord.io._cache_remote_task", side_effect=boom), \
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
        task = apply_transition(_sample_task(), "active", by="claude-code")
        cache.write_cached_task(task)
        return task

    def test_raises_needs_reconcile_when_one_view_fails(self):
        from fulcra_coord.cli import _write_task_and_views
        task = self._setup_remote()

        def upload_json(data, path, *, backend=None, timeout=None):
            # Fail exactly one view upload (index); task + every other view ok.
            if path.endswith("/index.json"):
                return False
            return True

        # probe_reachable=True (F1 guard): stat None + download None on a
        # REACHABLE bus is a confirmed-new task; without the probe this world
        # would now (correctly) refuse to write at all.
        with patch("fulcra_coord.cli.remote.stat", return_value=None), \
             patch("fulcra_coord.cli.remote.probe_reachable", return_value=True), \
             patch("fulcra_coord.cli.remote.upload_json", side_effect=upload_json), \
             patch("fulcra_coord.writepipe._load_summaries_for_rebuild",
                   return_value=[schema.task_summary(task)]):
            with self.assertRaises(schema.NeedsReconcile):
                _write_task_and_views(task, backend=["false"], command="update")

    def test_emit_runs_before_needs_reconcile_on_partial_failure(self):
        # BUG 10: the task body uploaded successfully, so the lifecycle
        # transition is REAL and must be recorded — even when a view upload
        # fails. The emit was AFTER the NeedsReconcile raise, so a partial view
        # failure permanently dropped the annotation. Assert emit still runs AND
        # NeedsReconcile is still raised (emit best-effort, can't change outcome).
        from fulcra_coord.cli import _write_task_and_views
        task = self._setup_remote()

        def upload_json(data, path, *, backend=None, timeout=None):
            if path.endswith("/index.json"):
                return False  # one view fails -> partial -> NeedsReconcile
            return True

        with patch("fulcra_coord.cli.remote.stat", return_value=None), \
             patch("fulcra_coord.cli.remote.probe_reachable", return_value=True), \
             patch("fulcra_coord.cli.remote.upload_json", side_effect=upload_json), \
             patch("fulcra_coord.writepipe._load_summaries_for_rebuild",
                   return_value=[schema.task_summary(task)]), \
             patch("fulcra_coord.cli.lifecycle_annotations.emit_lifecycle_annotation") as emit:
            with self.assertRaises(schema.NeedsReconcile):
                _write_task_and_views(task, backend=["false"], command="update")
        self.assertTrue(emit.called,
                        "lifecycle annotation must be emitted before NeedsReconcile")

    def test_uploads_all_views_on_success(self):
        from fulcra_coord.cli import _write_task_and_views
        task = self._setup_remote()
        uploaded_paths = []

        def upload_json(data, path, *, backend=None, timeout=None):
            uploaded_paths.append(path)
            return True

        with patch("fulcra_coord.cli.remote.stat", return_value=None), \
             patch("fulcra_coord.cli.remote.probe_reachable", return_value=True), \
             patch("fulcra_coord.cli.remote.upload_json", side_effect=upload_json), \
             patch("fulcra_coord.writepipe._load_summaries_for_rebuild",
                   return_value=[schema.task_summary(task)]):
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

    def test_s2_newer_local_wins_across_mixed_precision_timestamps(self):
        # BUG 1 also affects the aggregate-vs-local freshness decision. A local
        # summary at ...45.000001Z is newer than the aggregate's ...45Z, but a
        # raw string compare inverts that ordering ('.' < 'Z') and would keep the
        # stale aggregate entry.
        from fulcra_coord.cli import _load_summaries_for_rebuild
        task_a = apply_transition(_sample_task(), "active", by="claude-code")
        local_b = _sample_task()
        local_b["id"] = "TASK-20260603-peer-task-mixedts"
        local_b["title"] = "LOCAL-NEWER"
        local_b["updated_at"] = "2026-06-03T12:30:45.000001Z"
        cache.write_cached_task(local_b)
        older_aggregate_b = dict(schema.task_summary(local_b))
        older_aggregate_b["title"] = "AGGREGATE-OLDER"
        older_aggregate_b["updated_at"] = "2026-06-03T12:30:45Z"

        with patch("fulcra_coord.cli.remote.download_json",
                   side_effect=self._agg([schema.task_summary(task_a),
                                          older_aggregate_b])):
            out = _load_summaries_for_rebuild(task_a, backend=["false"])
        entry = next(s for s in out if s["id"] == local_b["id"])
        self.assertEqual(entry["title"], "LOCAL-NEWER")

    def test_selfheal_recovers_dropped_task_via_file_listing(self):
        # The hard case: a peer's task was clobbered out of the aggregate AND this
        # agent never cached it — but its durable FILE exists. Enumerating the
        # task files must recover it (fetch its body), healing the drop on THIS
        # write instead of leaving it invisible until a reconcile.
        from fulcra_coord.cli import _load_summaries_for_rebuild
        from fulcra_coord import remote
        task_a = apply_transition(_sample_task(), "active", by="claude-code")
        dropped = _sample_task()
        dropped["id"] = "TASK-20260603-peer-dropped-cccccccc"
        droppath = remote.task_remote_path(dropped["id"])

        with patch("fulcra_coord.cli.remote.download_json",
                   side_effect=self._agg([schema.task_summary(task_a)])), \
             patch("fulcra_coord.cli.remote.list_files",
                   return_value=[remote.task_remote_path(task_a["id"]), droppath]), \
             patch("fulcra_coord.io._cache_remote_task",
                   side_effect=lambda tid, backend=None: dropped if tid == dropped["id"] else None):
            out = _load_summaries_for_rebuild(task_a, backend=["false"])
        ids = {s["id"] for s in out}
        self.assertIn(dropped["id"], ids)   # recovered from the file listing
        self.assertIn(task_a["id"], ids)

    def test_selfheal_no_body_fetch_when_aggregate_already_complete(self):
        # Steady state: every listed task file is already in the aggregate, so the
        # self-heal must fetch ZERO bodies (the cheap common path).
        from fulcra_coord.cli import _load_summaries_for_rebuild
        from fulcra_coord import remote
        task_a = apply_transition(_sample_task(), "active", by="claude-code")
        task_b = _sample_task(); task_b["id"] = "TASK-20260603-peer-known-dddddddd"

        def boom(*a, **k):
            raise AssertionError("fetched a body though the aggregate already covered the id")

        with patch("fulcra_coord.cli.remote.download_json",
                   side_effect=self._agg([schema.task_summary(task_a), schema.task_summary(task_b)])), \
             patch("fulcra_coord.cli.remote.list_files",
                   return_value=[remote.task_remote_path(task_a["id"]), remote.task_remote_path(task_b["id"])]), \
             patch("fulcra_coord.io._cache_remote_task", side_effect=boom):
            out = _load_summaries_for_rebuild(task_a, backend=["false"])
        self.assertEqual({s["id"] for s in out}, {task_a["id"], task_b["id"]})

    def test_selfheal_listing_failure_is_safe(self):
        # A failed/raising listing must never break the write — it just adds
        # nothing, leaving the aggregate-derived rebuild intact.
        from fulcra_coord.cli import _load_summaries_for_rebuild
        task_a = apply_transition(_sample_task(), "active", by="claude-code")
        with patch("fulcra_coord.cli.remote.download_json",
                   side_effect=self._agg([schema.task_summary(task_a)])), \
             patch("fulcra_coord.cli.remote.list_files",
                   side_effect=RuntimeError("list blew up")):
            out = _load_summaries_for_rebuild(task_a, backend=["false"])
        self.assertEqual({s["id"] for s in out}, {task_a["id"]})

    def test_selfheal_bad_body_does_not_stop_later_recovery(self):
        # A malformed/unreadable task body in the listing must not abort the
        # whole self-heal loop. Skip the bad one and still recover later valid
        # dropped tasks.
        from fulcra_coord.cli import _load_summaries_for_rebuild
        from fulcra_coord import remote
        task_a = apply_transition(_sample_task(), "active", by="claude-code")
        good = _sample_task()
        good["id"] = "TASK-20260603-peer-dropped-good"
        bad_id = "TASK-20260603-peer-dropped-bad"

        def fake_cache(tid, backend=None):
            if tid == bad_id:
                raise ValueError("bad task body")
            return good if tid == good["id"] else None

        with patch("fulcra_coord.cli.remote.download_json",
                   side_effect=self._agg([schema.task_summary(task_a)])), \
             patch("fulcra_coord.cli.remote.list_files",
                   return_value=[remote.task_remote_path(bad_id),
                                 remote.task_remote_path(good["id"])]), \
             patch("fulcra_coord.io._cache_remote_task", side_effect=fake_cache):
            out = _load_summaries_for_rebuild(task_a, backend=["false"])
        ids = {s["id"] for s in out}
        self.assertIn(good["id"], ids)
        self.assertNotIn(bad_id, ids)

    def test_bug2_corrupt_cached_body_is_surfaced_not_raised(self):
        # BUG 2 (debug sweep round 2-3): a corrupt/partial cached task body
        # (missing title/status/workstream/owner_agent) must NOT raise KeyError
        # out of the cache-union loop. task_summary is now DEFENSIVE — it renders
        # such a body with empty-string defaults instead of vanishing it — so the
        # rebuild completes AND the malformed task stays visible (and thus
        # fixable) rather than being silently dropped from every view. The good
        # cached tasks are of course still returned.
        from fulcra_coord.cli import _load_summaries_for_rebuild
        task_a = apply_transition(_sample_task(), "active", by="claude-code")

        good_cached = _sample_task()
        good_cached["id"] = "TASK-20260603-good-cached-eeeeeeee"
        good_cached["updated_at"] = "2026-06-03T10:00:00Z"
        cache.write_cached_task(good_cached)

        # A corrupt body: has an id but is missing title/status/workstream/
        # owner_agent → task_summary now renders it with "" defaults, no crash.
        corrupt = {"id": "TASK-20260603-corrupt-ffffffff",
                   "updated_at": "2026-06-03T11:00:00Z"}
        with patch("fulcra_coord.cli.cache.list_cached_tasks",
                   return_value=[good_cached, corrupt]), \
             patch("fulcra_coord.cli.remote.download_json",
                   side_effect=self._agg([schema.task_summary(task_a)])), \
             patch("fulcra_coord.cli.remote.list_files", return_value=[]):
            out = _load_summaries_for_rebuild(task_a, backend=["false"])
        by_id = {s["id"]: s for s in out}
        self.assertIn(good_cached["id"], by_id)   # good entry survives
        self.assertIn(task_a["id"], by_id)
        # The corrupt one now SURVIVES (rendered, not dropped) — the BUG 2 fix.
        self.assertIn(corrupt["id"], by_id)
        self.assertEqual(by_id[corrupt["id"]]["title"], "")
        self.assertEqual(by_id[corrupt["id"]]["workstream"], "")


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

        # probe_reachable=True (F1 guard): see TestParallelViewUpload.
        with patch("fulcra_coord.cli.remote.stat", return_value=None), \
             patch("fulcra_coord.cli.remote.probe_reachable", return_value=True), \
             patch("fulcra_coord.cli.remote.upload_json", side_effect=upload_json), \
             patch("fulcra_coord.writepipe._load_summaries_for_rebuild",
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

    def test_mutation_strips_baked_stale_version_suffix(self):
        """2026-06-11 bug hunt S6: connect appends '(vX behind canonical Y)'
        to the presence summary when the stale marker is set. workstream
        set/add/clear preserved the WHOLE stored summary, so the baked-in
        suffix was carried forever — even after the host updated — because
        only connect re-derives it from the marker. Mutations must strip the
        trailing suffix from the preserved summary; the rest survives."""
        from fulcra_coord.cli import cmd_connect, cmd_workstream
        me = "claude-code:h:r"
        self._run(cmd_connect, self._ns(
            agent=me, workstream="fulcra", summary="", format="table"))
        # Bake the suffixed summary in, exactly as a stale-marked connect does.
        self._run(cmd_workstream, self._ns(
            agent=me, ws_action="set", workstreams="x",
            summary="porting the digest (v0.15.2 behind canonical 0.16.0)",
            format="table"))
        # A later mutation that PRESERVES the summary (summary=None)…
        self._run(cmd_workstream, self._ns(
            agent=me, ws_action="add", workstreams="y", summary=None,
            format="table"))
        # …drops the suffix but keeps the operator's actual text.
        self.assertEqual(self._record(me)["summary"], "porting the digest")


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

    def test_agentless_aggregate_entry_does_not_crash(self):
        """A3 — cmd_presence must tolerate an agent-less aggregate entry.

        build_presence carries records through verbatim and does NOT inject an
        ``agent`` key, so an imperfect aggregate entry lacking ``agent`` made the
        table render (f"... {a['agent']} [{a['liveness']}] ...") raise KeyError,
        crashing the single ``presence`` command. The good entries must still
        print."""
        from fulcra_coord import remote
        from fulcra_coord.cli import cmd_presence
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        # Seed the aggregate directly with one agent-less entry + one good entry.
        agg = {
            "schema": "fulcra.coordination.presence_view.v1",
            "view": "presence",
            "updated_at": now_iso,
            "agents": [
                {"workstreams": ["orphan"], "last_seen": now_iso},  # NO 'agent'
                {"agent": "good:h:r", "workstreams": ["ok"], "last_seen": now_iso},
            ],
        }
        path = remote.presence_view_path()
        p = Path(self.fake_root) / path.lstrip("/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(agg))
        rc, out = self._run(cmd_presence, self._ns(format="table"))
        self.assertEqual(rc, 0)
        self.assertIn("good:h:r", out)  # the well-formed agent still renders


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


class TestPresenceUpsertSelfHeal(_PresenceBackendCase):
    """BUG 4 (S2-class): _upsert_presence_aggregate must self-heal by LISTING
    the durable presence/*.json files (like the task self-heal), so a peer that
    a concurrent connect clobbered out of the aggregate is recovered on the next
    connect — not lost until a full reconcile."""

    def test_clobbered_peer_recovered_from_durable_file(self):
        from fulcra_coord.cli import _write_presence, _upsert_presence_aggregate
        from fulcra_coord import remote
        a, b = "agent-a:h:r", "agent-b:h:r"

        # Both agents have written their durable per-agent presence files.
        rec_a = schema.make_presence(a, workstreams=["wa"], summary="")
        rec_b = schema.make_presence(b, workstreams=["wb"], summary="")
        _write_presence(rec_a, backend=self.fake_backend)
        _write_presence(rec_b, backend=self.fake_backend)

        # Simulate the clobber: a raced last-writer-wins upload left the
        # aggregate with ONLY b (a was dropped) — but a's durable file survives.
        clobbered = views.build_presence([rec_b])
        remote.upload_json(clobbered, remote.presence_view_path(),
                           backend=self.fake_backend)

        # b connects again → its upsert must rebuild from the durable files and
        # recover a, not just re-assert b over the clobbered aggregate.
        _upsert_presence_aggregate(rec_b, backend=self.fake_backend)

        agg = remote.download_json(remote.presence_view_path(),
                                   backend=self.fake_backend)
        agents = {x["agent"] for x in agg["agents"]}
        self.assertEqual(agents, {a, b},
                         "clobbered peer a must be recovered from its durable file")


class TestStartAgentOptional(_PresenceBackendCase):
    def test_start_without_agent_resolves_via_resolve_agent(self):
        from fulcra_coord.cli import cmd_start
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
# Onboarding UX hints (Task C) — and the start-vs-claim REFUSAL that replaced
# the first hint:
#   1. `start` with a TASK-id-shaped title -> REFUSED, exit 1, nothing written
#      (2026-06-11 live find: the warn-but-proceed hint let 6 junk tasks titled
#      after task ids land on the bus; bug filed as option (a) — refuse).
#   2. `connect`/`start` with a DERIVED identity + a legacy global identity.json
#      present -> "migrate your legacy identity" (still a warning only).
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

    # ---- start-vs-claim refusal ----

    def test_start_with_task_id_title_refuses_no_task_no_remote_writes(self):
        # 2026-06-11 live find: `start TASK-...` (the operator meant to CLAIM)
        # created junk tasks TITLED after task ids — 6 on the live bus. The old
        # non-blocking hint demonstrably did not prevent them, so an id-shaped
        # title is now refused outright: exit 1, hint on stderr, NO task
        # created, ZERO remote writes.
        from fulcra_coord.cli import cmd_start
        title = "TASK-20260101-deploy-the-thing-ab12cd34"
        with patch("fulcra_coord.remote.upload_json") as up:
            rc, out, err = self._run_capturing_stderr(cmd_start, self._ns(
                title=title, workstream="devops", agent="claude-code:h:r",
                kind="ops", priority="P2", summary="", next="", surface=None))
        self.assertEqual(rc, 1)
        self.assertIn("looks like a task id", err)
        self.assertIn("fulcra-coord update <id> --status active", err)
        # Nothing was created — locally or on the bus.
        self.assertFalse(any(t["title"] == title
                             for t in cache.list_cached_tasks()),
                         "a refused start must not create the task")
        self.assertEqual(up.call_count, 0,
                         "a refused start must make zero remote writes")

    def test_start_with_normal_title_still_creates(self):
        # No behavior change for genuine titles: the task is created.
        from fulcra_coord.cli import cmd_start
        rc, out, err = self._run_capturing_stderr(cmd_start, self._ns(
            title="Deploy the widget", workstream="devops",
            agent="claude-code:h:r", kind="ops", priority="P2",
            summary="", next="", surface=None))
        self.assertNotIn("--status active", err)
        self.assertTrue(any(t["title"] == "Deploy the widget"
                            for t in cache.list_cached_tasks()),
                        "a genuine title must still create the task")

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
# Situational awareness — DUE-DATE / NOT-BEFORE scheduling for "blocked on you"
# A task the human can't act on yet (future not_before) must NOT clutter the
# needs-me plate / SessionStart banner; it surfaces as "upcoming" until its
# not_before passes, at which point it becomes a real DUE-NOW ask.
# ---------------------------------------------------------------------------

class TestParseWhen(unittest.TestCase):
    """schema.parse_when: stdlib date parser. Accepts ISO-8601 dates/datetimes
    and relative offsets (Nd/Nh/Nm) relative to `now`; returns an ISO-Z string
    or None on unparseable input. Pure + testable by passing `now`."""

    def setUp(self):
        from datetime import datetime, timezone
        self.now = datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)

    # Emitted timestamps carry fixed-width microseconds (BUG 1) so a same-second
    # pair never mis-orders under a lexical compare; the .000000 suffix is the
    # zero-microsecond rendering of isoformat(timespec="microseconds").
    def test_iso_date(self):
        self.assertEqual(schema.parse_when("2026-06-08", now=self.now),
                         "2026-06-08T00:00:00.000000Z")

    def test_iso_datetime_z(self):
        self.assertEqual(schema.parse_when("2026-06-08T18:00:00Z", now=self.now),
                         "2026-06-08T18:00:00.000000Z")

    def test_relative_days(self):
        # 5 days from 2026-06-03T12:00 -> 2026-06-08T12:00.
        self.assertEqual(schema.parse_when("5d", now=self.now),
                         "2026-06-08T12:00:00.000000Z")

    def test_relative_hours(self):
        # 36h from 2026-06-03T12:00 -> 2026-06-05T00:00.
        self.assertEqual(schema.parse_when("36h", now=self.now),
                         "2026-06-05T00:00:00.000000Z")

    def test_relative_minutes(self):
        self.assertEqual(schema.parse_when("10m", now=self.now),
                         "2026-06-03T12:10:00.000000Z")

    def test_bad_input_returns_none(self):
        for bad in ("", "   ", "tomorrow", "5x", "not-a-date", "5dd", "d5",
                    None):
            self.assertIsNone(schema.parse_when(bad, now=self.now),
                              f"expected None for {bad!r}")

    def test_default_now_is_used(self):
        # With no `now`, a relative offset is anchored to datetime.now(utc);
        # we only assert it produces a parseable ISO-Z (not None).
        out = schema.parse_when("1d")
        self.assertIsNotNone(out)
        self.assertTrue(out.endswith("Z"))


class TestMakeTaskScheduleFields(unittest.TestCase):
    """make_task carries optional not_before/due (default None), and
    task_summary round-trips them so the rebuilt views see them."""

    def test_defaults_none(self):
        t = make_task(title="x", workstream="general", agent="a")
        self.assertIsNone(t["not_before"])
        self.assertIsNone(t["due"])

    def test_stored_when_given(self):
        t = make_task(title="x", workstream="general", agent="a",
                      not_before="2026-06-08T00:00:00Z", due="2026-06-10T00:00:00Z")
        self.assertEqual(t["not_before"], "2026-06-08T00:00:00Z")
        self.assertEqual(t["due"], "2026-06-10T00:00:00Z")

    def test_task_summary_carries_schedule_fields(self):
        t = make_task(title="x", workstream="general", agent="a",
                      not_before="2026-06-08T00:00:00Z", due="2026-06-10T00:00:00Z")
        s = schema.task_summary(t)
        self.assertEqual(s["not_before"], "2026-06-08T00:00:00Z")
        self.assertEqual(s["due"], "2026-06-10T00:00:00Z")

    def test_task_summary_defaults_none_on_old_body(self):
        # A pre-feature body missing the keys summarizes to None, not KeyError.
        t = make_task(title="x", workstream="general", agent="a")
        t.pop("not_before", None)
        t.pop("due", None)
        s = schema.task_summary(t)
        self.assertIsNone(s["not_before"])
        self.assertIsNone(s["due"])


class TestNeedsHumanNotBeforeGating(unittest.TestCase):
    """views.needs_human gates DUE-NOW on not_before: a future not_before is
    EXCLUDED from the due-now plate; absent/empty/past not_before behaves as
    today. The existing broadcast/needs:human/open-status rules are intact."""

    def _t(self, tid, assignee="human", status="blocked", not_before=None,
           updated="2026-06-01T00:00:00Z"):
        t = schema.make_task(title=tid, workstream="general", agent="o",
                             owner_agent="o", assignee=assignee,
                             not_before=not_before)
        t["status"] = status
        t["updated_at"] = updated
        return t

    def setUp(self):
        from datetime import datetime, timezone
        self.now = datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)

    def test_future_not_before_excluded(self):
        from fulcra_coord.views import needs_human
        tasks = [self._t("nest-reauth", not_before="2026-06-08T00:00:00Z")]
        self.assertEqual(needs_human(tasks, "human", now=self.now), [])

    def test_past_not_before_included(self):
        from fulcra_coord.views import needs_human
        tasks = [self._t("ready", not_before="2026-06-01T00:00:00Z")]
        self.assertEqual([s["title"] for s in needs_human(tasks, "human", now=self.now)],
                         ["ready"])

    def test_absent_not_before_included(self):
        from fulcra_coord.views import needs_human
        tasks = [self._t("plain", not_before=None)]
        self.assertEqual([s["title"] for s in needs_human(tasks, "human", now=self.now)],
                         ["plain"])

    def test_empty_string_not_before_included(self):
        from fulcra_coord.views import needs_human
        t = self._t("emptystr")
        t["not_before"] = ""
        self.assertEqual([s["title"] for s in needs_human([t], "human", now=self.now)],
                         ["emptystr"])

    def test_malformed_not_before_included(self):
        from fulcra_coord.views import needs_human
        tasks = [self._t("bad-date", not_before="tomorrow-ish")]
        self.assertEqual([s["title"] for s in needs_human(tasks, "human", now=self.now)],
                         ["bad-date"])

    def test_existing_broadcast_and_status_behavior_preserved(self):
        from fulcra_coord.views import needs_human
        tasks = [
            self._t("broadcast", assignee="*"),
            self._t("done-one", status="done"),
            self._t("real", assignee="human"),
        ]
        self.assertEqual([s["title"] for s in needs_human(tasks, "human", now=self.now)],
                         ["real"])

    def test_default_now_when_omitted(self):
        # No `now` -> resolves to wall-clock; a far-future not_before is still
        # excluded; an absent one is still included.
        from fulcra_coord.views import needs_human
        future = self._t("future", not_before="2099-01-01T00:00:00Z")
        plain = self._t("plain")
        out = needs_human([future, plain], "human")
        self.assertEqual([s["title"] for s in out], ["plain"])

    def test_subsecond_past_not_before_is_due_now(self):
        # BUG 7: now carries fractional seconds (microsecond != 0), not_before
        # is whole-second and 0.4s in the PAST. A lexical string compare of the
        # mixed-width ISO-Z strings ('Z' 0x5A vs '.' 0x2E) wrongly gated this as
        # future. Comparing PARSED datetimes must surface it as due-now.
        from datetime import datetime, timezone
        from fulcra_coord.views import needs_human
        now = datetime(2026, 6, 3, 12, 0, 0, 400000, tzinfo=timezone.utc)
        tasks = [self._t("just-past", not_before="2026-06-03T12:00:00Z")]
        self.assertEqual(
            [s["title"] for s in needs_human(tasks, "human", now=now)],
            ["just-past"],
            "a sub-second-past not_before must gate as due-now, not future")


class TestUpcomingForHuman(unittest.TestCase):
    """views.upcoming_for_human: future-not_before items (same human-match /
    open-status / broadcast rules) within `within_days`, sorted by not_before
    then due, each carrying not_before + due for the UI."""

    def _t(self, tid, assignee="human", status="blocked", not_before=None,
           due=None, updated="2026-06-01T00:00:00Z"):
        t = schema.make_task(title=tid, workstream="general", agent="o",
                             owner_agent="o", assignee=assignee,
                             not_before=not_before, due=due)
        t["status"] = status
        t["updated_at"] = updated
        return t

    def setUp(self):
        from datetime import datetime, timezone
        self.now = datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)

    def test_includes_future_within_window(self):
        from fulcra_coord.views import upcoming_for_human
        tasks = [self._t("soon", not_before="2026-06-06T00:00:00Z",
                         due="2026-06-08T00:00:00Z")]
        out = upcoming_for_human(tasks, "human", now=self.now, within_days=7)
        self.assertEqual([s["title"] for s in out], ["soon"])
        self.assertEqual(out[0]["not_before"], "2026-06-06T00:00:00Z")
        self.assertEqual(out[0]["due"], "2026-06-08T00:00:00Z")

    def test_excludes_due_now(self):
        # An item already actionable (past not_before) is NOT upcoming.
        from fulcra_coord.views import upcoming_for_human
        tasks = [self._t("ready", not_before="2026-06-01T00:00:00Z")]
        self.assertEqual(upcoming_for_human(tasks, "human", now=self.now), [])

    def test_excludes_far_future(self):
        from fulcra_coord.views import upcoming_for_human
        tasks = [self._t("far", not_before="2026-07-01T00:00:00Z")]
        self.assertEqual(upcoming_for_human(tasks, "human", now=self.now,
                                            within_days=7), [])

    def test_excludes_absent_not_before(self):
        from fulcra_coord.views import upcoming_for_human
        tasks = [self._t("plain", not_before=None)]
        self.assertEqual(upcoming_for_human(tasks, "human", now=self.now), [])

    def test_excludes_malformed_not_before(self):
        from fulcra_coord.views import upcoming_for_human
        tasks = [self._t("bad-date", not_before="tomorrow-ish")]
        self.assertEqual(upcoming_for_human(tasks, "human", now=self.now), [])

    def test_sorted_by_not_before_then_due(self):
        from fulcra_coord.views import upcoming_for_human
        tasks = [
            self._t("b-later-nb", not_before="2026-06-07T00:00:00Z",
                    due="2026-06-09T00:00:00Z"),
            self._t("a-earlier-nb", not_before="2026-06-05T00:00:00Z",
                    due="2026-06-09T00:00:00Z"),
            self._t("a-earlier-nb-earlier-due", not_before="2026-06-05T00:00:00Z",
                    due="2026-06-06T00:00:00Z"),
        ]
        out = upcoming_for_human(tasks, "human", now=self.now, within_days=7)
        self.assertEqual([s["title"] for s in out],
                         ["a-earlier-nb-earlier-due", "a-earlier-nb", "b-later-nb"])

    def test_respects_broadcast_and_open_status(self):
        from fulcra_coord.views import upcoming_for_human
        tasks = [
            self._t("broadcast", assignee="*", not_before="2026-06-06T00:00:00Z"),
            self._t("done", status="done", not_before="2026-06-06T00:00:00Z"),
            self._t("real", not_before="2026-06-06T00:00:00Z"),
        ]
        self.assertEqual([s["title"] for s in upcoming_for_human(
            tasks, "human", now=self.now)], ["real"])

    def test_subsecond_past_not_before_excluded_from_upcoming(self):
        # BUG 7 (symmetric): a sub-second-past not_before is due-now, NOT
        # upcoming. The lexical mixed-width compare wrongly treated it as future
        # and included it here; parsed-datetime compare excludes it.
        from datetime import datetime, timezone
        from fulcra_coord.views import upcoming_for_human
        now = datetime(2026, 6, 3, 12, 0, 0, 400000, tzinfo=timezone.utc)
        tasks = [self._t("just-past", not_before="2026-06-03T12:00:00Z")]
        self.assertEqual(upcoming_for_human(tasks, "human", now=now), [],
                         "a sub-second-past not_before is due-now, not upcoming")


class TestNeedsMeDateFormatting(unittest.TestCase):
    """Small CLI display helpers should stay portable across Python platforms."""

    def test_due_str_uses_unpadded_day_without_platform_specific_strftime(self):
        from fulcra_coord.textfmt import due_str
        self.assertEqual(due_str("2026-06-08T18:00:00Z"), "Jun 8")


class TestEquivalenceWithScheduleFields(unittest.TestCase):
    """The linchpin equivalence (build_all_views(bodies)==build_all_views(
    summaries)) must still hold once tasks carry not_before/due."""

    def test_equivalence_with_schedule_fields(self):
        tasks = _make_representative_tasks()
        # Stamp schedule fields onto a couple of tasks (one future, one past).
        tasks[0]["not_before"] = "2026-06-10T00:00:00Z"
        tasks[0]["due"] = "2026-06-12T00:00:00Z"
        tasks[1]["not_before"] = "2026-06-01T00:00:00Z"
        summaries = [schema.task_summary(t) for t in tasks]
        from unittest.mock import patch
        from datetime import datetime, timezone
        with patch("fulcra_coord.views._now") as mock_now:
            mock_now.return_value = datetime(2026, 6, 3, tzinfo=timezone.utc)
            from_full = build_all_views(tasks)
            from_summaries = build_all_views(summaries)
        for name in from_full:
            self.assertEqual(from_full[name], from_summaries[name],
                             f"view {name!r} differs with schedule fields")


class TestBlockOnUserSchedule(unittest.TestCase):
    """`block --on-user --not-before <when> --due <when>` parses and stores the
    schedule fields on the task; a future not_before keeps it off the DUE-NOW
    plate and onto the upcoming list."""

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
        kw.setdefault("not_before", None)
        kw.setdefault("due", None)
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

    def test_block_on_user_stores_parsed_schedule(self):
        from fulcra_coord.cli import cmd_block
        tid = self._make_active("nest-reauth")
        cmd_block(self._ns(task_id=tid, blocked_on=None,
                           on_user="re-auth Nest", agent=None,
                           not_before="2026-06-08", due="2026-06-08T18:00:00Z"),
                  backend=self.fake_backend)
        t = cache.read_cached_task(tid)
        # Fixed-width microseconds (BUG 1): parse_when now emits .000000Z.
        self.assertEqual(t["not_before"], "2026-06-08T00:00:00.000000Z")
        self.assertEqual(t["due"], "2026-06-08T18:00:00.000000Z")

    def test_block_on_user_no_schedule_leaves_none(self):
        from fulcra_coord.cli import cmd_block
        tid = self._make_active("plain")
        cmd_block(self._ns(task_id=tid, blocked_on=None, on_user="do it",
                           agent=None), backend=self.fake_backend)
        t = cache.read_cached_task(tid)
        self.assertIsNone(t.get("not_before"))
        self.assertIsNone(t.get("due"))


class TestNeedsMeSchedulingJSON(unittest.TestCase):
    """cmd_needs_me --format json separates DUE-NOW `items` from `upcoming`:
    a future-not_before task is in upcoming, not items."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp, "config")
        os.environ.pop("FULCRA_COORD_AGENT", None)
        os.environ.pop("FULCRA_COORD_HUMAN", None)
        self.fake_backend = ["false"]

    def tearDown(self):
        for k in ("XDG_CACHE_HOME", "XDG_CONFIG_HOME", "FULCRA_COORD_AGENT",
                  "FULCRA_COORD_HUMAN"):
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seed_blocked(self, title, not_before=None, due=None):
        # Build a blocked-on-human task directly in the cache (a few days out).
        t = schema.make_task(title=title, workstream="general",
                             agent="claude-code:h:r", owner_agent="claude-code:h:r",
                             assignee="human", not_before=not_before, due=due)
        t["status"] = "blocked"
        t["blocked_on"] = "do the thing"
        t["tags"] = sorted(set(t.get("tags", []) + ["needs:human"]))
        cache.write_cached_task(t)
        return t["id"]

    def _run_json(self):
        import io, contextlib
        from fulcra_coord.cli import cmd_needs_me
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cmd_needs_me(types.SimpleNamespace(human=None, format="json", all=False),
                         backend=self.fake_backend)
        return json.loads(buf.getvalue())

    def test_due_now_vs_upcoming_split(self):
        from datetime import datetime, timezone, timedelta
        now = datetime.now(timezone.utc)
        soon = (now + timedelta(days=3)).isoformat().replace("+00:00", "Z")
        self._seed_blocked("ready-now", not_before=None)
        self._seed_blocked("future-task", not_before=soon)
        out = self._run_json()
        item_titles = [i["title"] for i in out["items"]]
        upcoming_titles = [i["title"] for i in out["upcoming"]]
        self.assertIn("ready-now", item_titles)
        self.assertNotIn("future-task", item_titles)
        self.assertIn("future-task", upcoming_titles)
        # count reflects only due-now items, never upcoming.
        self.assertEqual(out["count"], len(out["items"]))


class TestSessionStartUpcomingBanner(unittest.TestCase):
    """SessionStart banner: the ⛔ headline counts ONLY due-now `items`; a
    non-empty `upcoming` adds a muted "(+N upcoming)" line and NEVER inflates
    the headline count. A future-only needs-me yields no ⛔ headline at all."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bin = os.path.join(self.tmp, "bin"); os.makedirs(self.bin)
        fake = os.path.join(self.bin, "fulcra-coord")
        with open(fake, "w") as f:
            # briefing combines the canned status + needs-me sections (the
            # one-process payload session-start now consumes; inbox empty).
            f.write("#!/usr/bin/env bash\n"
                    'if [ "$1" = "briefing" ]; then STATUS="%s" NEEDSME="%s" '
                    "python3 -c '"
                    'import json,os;print(json.dumps({"agent":"",'
                    '"status":json.load(open(os.environ["STATUS"])),'
                    '"inbox":{"inbox":[]},'
                    '"needs_me":json.load(open(os.environ["NEEDSME"]))}))'
                    "'; exit 0; fi\n"
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

    def test_headline_counts_only_due_now(self):
        needsme = json.dumps({"human": "ash", "count": 1, "items": [
            {"id": "TASK-now", "title": "ready", "status": "blocked",
             "owner_agent": "claude-code:h:r", "blocked_on": "approve it",
             "next_action": "", "updated_at": "2026-06-01T00:00:00Z"}],
            "upcoming": [
            {"id": "TASK-future", "title": "nest reauth", "status": "blocked",
             "owner_agent": "claude-code:h:r", "blocked_on": "re-auth",
             "not_before": "2099-01-01T00:00:00Z", "due": "2099-01-02T00:00:00Z"}]})
        r = self._run(json.dumps({"active": []}), needsme)
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("BLOCKED ON YOU (1)", ctx)
        self.assertIn("(+1 upcoming)", ctx)
        # The upcoming item must NOT appear in the headline list itself.
        self.assertNotIn("TASK-future", ctx.split("(+1 upcoming)")[0]
                         if "(+1 upcoming)" in ctx else ctx)

    def test_no_upcoming_line_when_empty(self):
        needsme = json.dumps({"human": "ash", "count": 1, "items": [
            {"id": "TASK-now", "title": "ready", "status": "blocked",
             "owner_agent": "claude-code:h:r", "blocked_on": "approve it",
             "next_action": "", "updated_at": "2026-06-01T00:00:00Z"}],
            "upcoming": []})
        r = self._run(json.dumps({"active": []}), needsme)
        ctx = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("BLOCKED ON YOU (1)", ctx)
        self.assertNotIn("upcoming", ctx)

    def test_future_only_yields_no_headline(self):
        # needs-me with ONLY upcoming items (no due-now) -> no ⛔ headline. With
        # a clean bus otherwise, the hook stays silent.
        needsme = json.dumps({"human": "ash", "count": 0, "items": [],
            "upcoming": [
            {"id": "TASK-future", "title": "nest reauth", "status": "blocked",
             "owner_agent": "claude-code:h:r", "blocked_on": "re-auth",
             "not_before": "2099-01-01T00:00:00Z", "due": "2099-01-02T00:00:00Z"}]})
        r = self._run(json.dumps({"active": []}), needsme)
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("BLOCKED ON YOU", r.stdout)


# ---------------------------------------------------------------------------
# PERF: reconcile parallelism (heartbeat regression fix)
# ---------------------------------------------------------------------------

class TestLoadAllTasksParallelFetch(unittest.TestCase):
    """PERF: _load_all_tasks fetches per-id task bodies CONCURRENTLY.

    The sequential body-fetch loop (one ~1.3s `fulcra file download` subprocess
    per remote id) made reconcile blow past its 90s timeout at ~76 tasks. The
    fix parallelizes the fetch with a thread pool. These tests pin the
    semantics the parallelization must preserve: the complete deduped task set,
    None-skip for a failed fetch, and that the fetches actually overlap in time
    (so a regression back to sequential is caught)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp

    def tearDown(self):
        del os.environ["XDG_CACHE_HOME"]

    def _index_only_download(self, ids):
        """download_json that exposes `ids` via the index, no search/next view."""
        def fake_download(path, *, backend=None, timeout=None):
            if path.endswith("/index.json"):
                return {"active": [{"id": i} for i in ids], "recent_done": []}
            return None
        return fake_download

    def test_returns_complete_deduped_set(self):
        from fulcra_coord.cli import _load_all_tasks
        ids = [f"TASK-2026-parallel-{n:08d}" for n in range(12)]
        bodies = {i: _sample_task(id=i) for i in ids}

        def fake_fetch(tid, backend=None):
            return bodies.get(tid)

        with patch("fulcra_coord.cli.remote.download_json",
                   side_effect=self._index_only_download(ids)), \
             patch("fulcra_coord.io._cache_remote_task", side_effect=fake_fetch):
            tasks = _load_all_tasks(backend=["false"])

        got = {t["id"] for t in tasks}
        self.assertEqual(got, set(ids), "all remote ids must be fetched and returned")
        # No duplicates even though each id is distinct in the map.
        self.assertEqual(len(tasks), len(ids))

    def test_none_results_are_skipped(self):
        from fulcra_coord.cli import _load_all_tasks
        ids = [f"TASK-2026-skip-{n:08d}" for n in range(6)]
        # Half the ids 404 (fetch returns None) — they must be dropped, not crash.
        good = {i: _sample_task(id=i) for i in ids[:3]}

        def fake_fetch(tid, backend=None):
            return good.get(tid)  # None for ids[3:]

        with patch("fulcra_coord.cli.remote.download_json",
                   side_effect=self._index_only_download(ids)), \
             patch("fulcra_coord.io._cache_remote_task", side_effect=fake_fetch):
            tasks = _load_all_tasks(backend=["false"])

        self.assertEqual({t["id"] for t in tasks}, set(ids[:3]))

    def test_local_cache_base_preserved_when_not_in_index(self):
        from fulcra_coord.cli import _load_all_tasks
        # A locally-cached task NOT named by any remote source must survive
        # (the task_map seeds from cache, then remote bodies upsert in).
        local = _sample_task(id="TASK-2026-localonly-00000001")
        cache.write_cached_task(local)
        remote_ids = ["TASK-2026-remote-00000002"]
        bodies = {remote_ids[0]: _sample_task(id=remote_ids[0])}

        def fake_fetch(tid, backend=None):
            return bodies.get(tid)

        with patch("fulcra_coord.cli.remote.download_json",
                   side_effect=self._index_only_download(remote_ids)), \
             patch("fulcra_coord.io._cache_remote_task", side_effect=fake_fetch):
            tasks = _load_all_tasks(backend=["false"])

        ids = {t["id"] for t in tasks}
        self.assertIn(local["id"], ids)
        self.assertIn(remote_ids[0], ids)

    def test_fetches_run_concurrently(self):
        """Regression guard: a sequential loop would serialize these blocking
        fetches; the pool must overlap them. We record the max concurrency seen
        across the fetch fn and require it to exceed 1."""
        import threading
        from fulcra_coord.cli import _load_all_tasks
        ids = [f"TASK-2026-conc-{n:08d}" for n in range(8)]
        bodies = {i: _sample_task(id=i) for i in ids}

        lock = threading.Lock()
        state = {"active": 0, "max": 0}
        barrier = threading.Barrier(len(ids), timeout=10)

        def fake_fetch(tid, backend=None):
            with lock:
                state["active"] += 1
                state["max"] = max(state["max"], state["active"])
            try:
                # All fetches must be in-flight at once for the barrier to release;
                # a sequential caller would deadlock here, so the timeout proves
                # concurrency rather than silently passing.
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
            with lock:
                state["active"] -= 1
            return bodies.get(tid)

        with patch("fulcra_coord.cli.remote.download_json",
                   side_effect=self._index_only_download(ids)), \
             patch("fulcra_coord.io._cache_remote_task", side_effect=fake_fetch):
            tasks = _load_all_tasks(backend=["false"])

        self.assertEqual({t["id"] for t in tasks}, set(ids))
        self.assertGreater(state["max"], 1,
                           "body fetches must run concurrently, not sequentially")


class TestReconcileParallelUpload(unittest.TestCase):
    """PERF: cmd_reconcile uploads its views CONCURRENTLY, with the exact same
    partial-failure / op-marker / exit-code semantics as the sequential loop."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp

    def tearDown(self):
        del os.environ["XDG_CACHE_HOME"]

    def test_uploads_all_views_on_success(self):
        from fulcra_coord.cli import cmd_reconcile
        task = apply_transition(_sample_task(), "active", by="claude-code")
        uploaded = []
        lock = __import__("threading").Lock()

        def upload_json(data, path, *, backend=None, timeout=None):
            with lock:
                uploaded.append(path)
            return True

        with patch("fulcra_coord.cli._load_all_tasks", return_value=[task]), \
             patch("fulcra_coord.cli.remote.upload_json", side_effect=upload_json), \
             patch("fulcra_coord.cli._reconcile_presence"):
            rc = cmd_reconcile(types.SimpleNamespace(), backend=["false"])

        self.assertEqual(rc, 0)
        self.assertTrue(any(p.endswith("/index.json") for p in uploaded))
        self.assertTrue(any(p.endswith("/views/active.json") for p in uploaded))
        # Every view is also cached locally.
        self.assertIsNotNone(cache.read_cached_view("index"))

    def test_partial_failure_preserves_markers_and_returns_1(self):
        from fulcra_coord.cli import cmd_reconcile
        task = apply_transition(_sample_task(), "active", by="claude-code")
        cache.ensure_dirs()
        cache.write_op_marker("repairP", {
            "op_id": "repairP", "command": "update", "task_id": task["id"],
            "status": "partial", "needs_reconcile": True,
            "started_at": "2026-01-01T00:00:00Z",
        })

        def upload_json(data, path, *, backend=None, timeout=None):
            return not path.endswith("/index.json")  # fail exactly one view

        with patch("fulcra_coord.cli._load_all_tasks", return_value=[task]), \
             patch("fulcra_coord.cli.remote.upload_json", side_effect=upload_json), \
             patch("fulcra_coord.cli._reconcile_presence"):
            rc = cmd_reconcile(types.SimpleNamespace(), backend=["false"])

        self.assertEqual(rc, 1, "a failed view upload must surface as exit 1")
        remaining = cache.list_op_markers()
        self.assertTrue(any(m["op_id"] == "repairP" for m in remaining),
                        "needs_reconcile marker must survive a partial reconcile")

    def test_raising_upload_is_caught_as_failure(self):
        """A view upload that RAISES must be counted as a failed view (exit 1),
        never escape and crash the whole reconcile."""
        from fulcra_coord.cli import cmd_reconcile
        task = apply_transition(_sample_task(), "active", by="claude-code")

        def upload_json(data, path, *, backend=None, timeout=None):
            if path.endswith("/index.json"):
                raise RuntimeError("simulated upload blowup")
            return True

        with patch("fulcra_coord.cli._load_all_tasks", return_value=[task]), \
             patch("fulcra_coord.cli.remote.upload_json", side_effect=upload_json), \
             patch("fulcra_coord.cli._reconcile_presence"):
            rc = cmd_reconcile(types.SimpleNamespace(), backend=["false"])

        self.assertEqual(rc, 1, "a raising upload must be caught and counted as a failure")

    def test_parallel_upload_respects_global_timeout_for_queued_views(self):
        """Queued upload work must not start after the reconcile deadline.

        With 8 workers and more than 8 views, a backend that stalls every upload
        used to consume one full timeout per batch. The worker checks the shared
        deadline before calling upload_json, so queued views past the deadline
        fail fast instead of starting another over-budget batch.
        """
        import threading
        import time
        from fulcra_coord.cli import cmd_reconcile

        views_to_upload = {f"view-{i}": {"tasks": []} for i in range(10)}
        active_calls = 0
        max_active = 0
        total_calls = 0
        lock = threading.Lock()

        def upload_json(data, path, *, backend=None, timeout=None):
            nonlocal active_calls, max_active, total_calls
            with lock:
                active_calls += 1
                total_calls += 1
                max_active = max(max_active, active_calls)
            time.sleep(1.05)
            with lock:
                active_calls -= 1
            return True

        # BUG 6b: the past-deadline guard now skips any worker with <1s of budget
        # left (not just <=0), so a 2s total timeout is needed for the first batch
        # to start (remaining ~2s >= 1) while the queued batch — which only gets to
        # run after the 1.05s sleep, leaving ~0.95s — is correctly skipped.
        old_timeout = os.environ.get("FULCRA_COORD_RECONCILE_TIMEOUT_SECONDS")
        os.environ["FULCRA_COORD_RECONCILE_TIMEOUT_SECONDS"] = "2"
        try:
            with patch("fulcra_coord.cli._load_all_tasks", return_value=[]), \
                 patch("fulcra_coord.cli.views.build_all_views",
                       return_value=views_to_upload), \
                 patch("fulcra_coord.cli.remote.upload_json",
                       side_effect=upload_json), \
                 patch("fulcra_coord.cli._reconcile_presence"):
                rc = cmd_reconcile(types.SimpleNamespace(), backend=["false"])
        finally:
            if old_timeout is None:
                del os.environ["FULCRA_COORD_RECONCILE_TIMEOUT_SECONDS"]
            else:
                os.environ["FULCRA_COORD_RECONCILE_TIMEOUT_SECONDS"] = old_timeout

        self.assertEqual(rc, 1)
        self.assertEqual(max_active, 8, "the first batch should still run in parallel")
        self.assertLessEqual(total_calls, 8,
                             "queued views past the deadline must not start uploads")


class TestReconcileUploadRetry(unittest.TestCase):
    """Bounded in-tick retry for reconcile view uploads (burst-throttling fix).

    Live 0.15.0 evidence (two hosts): under reconcile's parallel upload burst a
    ROTATING subset of views fails each tick (backend throttling / transient
    5xx) while single raw uploads succeed in <1s. Each failed view burned its
    timeout and failed the tick — self-healing next tick, but EVERY tick was
    partially failing and views went stale. The fix: each failed view upload is
    retried ONCE after a short jitter sleep, but only when there is real
    deadline headroom; a second failure is final and keeps the exact pre-fix
    semantics (failures list -> markers preserved -> exit 1).
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp
        # Tests control the knob explicitly; never inherit it from the host env.
        os.environ.pop("FULCRA_COORD_UPLOAD_RETRY", None)

    def tearDown(self):
        del os.environ["XDG_CACHE_HOME"]
        os.environ.pop("FULCRA_COORD_UPLOAD_RETRY", None)

    # Two small views keep the pool deterministic; the index view is the one
    # we make flaky (it ends in /index.json after _view_name_to_remote).
    _VIEWS = {"index": {"tasks": []}, "active": {"tasks": []}}

    @staticmethod
    def _flaky_uploader(fail_suffix, fail_first_n):
        """upload_json fake: paths ending in ``fail_suffix`` fail their first
        ``fail_first_n`` calls and succeed afterwards (None = fail forever).
        Returns (fn, per-path call counts) — counts are the retry oracle."""
        import threading
        counts: dict = {}
        lock = threading.Lock()

        def upload_json(data, path, *, backend=None, timeout=None):
            with lock:
                counts[path] = counts.get(path, 0) + 1
                n = counts[path]
            if path.endswith(fail_suffix):
                if fail_first_n is None or n <= fail_first_n:
                    return False
            return True

        return upload_json, counts

    def _run_reconcile(self, uploader, sleep_mock):
        from fulcra_coord.cli import cmd_reconcile
        with patch("fulcra_coord.cli._load_all_tasks", return_value=[]), \
             patch("fulcra_coord.cli.views.build_all_views",
                   return_value=dict(self._VIEWS)), \
             patch("fulcra_coord.cli.remote.upload_json", side_effect=uploader), \
             patch("fulcra_coord.cli._retry_sleep", sleep_mock), \
             patch("fulcra_coord.cli._reconcile_presence"), \
             patch("fulcra_coord.cli._info") as info:
            rc = cmd_reconcile(types.SimpleNamespace(), backend=["false"])
        return rc, info

    @staticmethod
    def _index_attempts(counts):
        return sum(n for p, n in counts.items() if p.endswith("/index.json"))

    def test_fail_once_then_recover_on_retry(self):
        """A transient (fail-once) view upload recovers in-tick: the view is
        NOT a failure, reconcile exits 0, and the recovery is reported."""
        from unittest.mock import MagicMock
        uploader, counts = self._flaky_uploader("/index.json", fail_first_n=1)
        rc, info = self._run_reconcile(uploader, MagicMock())

        self.assertEqual(rc, 0, "a recovered-on-retry view must not fail the tick")
        self.assertEqual(self._index_attempts(counts), 2,
                         "exactly one retry after the transient failure")
        messages = [str(c.args[0]) for c in info.call_args_list if c.args]
        self.assertTrue(any("recovered on retry" in m for m in messages),
                        f"recovery must be reported; got: {messages}")

    def test_fail_twice_is_final_failure_rc1(self):
        """A view that fails the attempt AND the retry stays a failed view:
        exit 1 (markers preserved by the unchanged downstream path), exactly
        one retry (bounded — never a retry loop), and the final failure leaves
        a diagnosable ops-log record."""
        from unittest.mock import MagicMock
        uploader, counts = self._flaky_uploader("/index.json", fail_first_n=None)
        rc, _ = self._run_reconcile(uploader, MagicMock())

        self.assertEqual(rc, 1, "a twice-failed view upload must surface as exit 1")
        self.assertEqual(self._index_attempts(counts), 2,
                         "retry is bounded to exactly one extra attempt")
        ops = cache.read_ops_log()
        self.assertTrue(any(e.get("status") == "view_upload_failed" for e in ops),
                        "final upload failure must land in the local ops log")

    def test_retry_disabled_via_env(self):
        """FULCRA_COORD_UPLOAD_RETRY=0 restores the pre-fix single attempt."""
        from unittest.mock import MagicMock
        os.environ["FULCRA_COORD_UPLOAD_RETRY"] = "0"
        uploader, counts = self._flaky_uploader("/index.json", fail_first_n=None)
        sleep = MagicMock()
        rc, _ = self._run_reconcile(uploader, sleep)

        self.assertEqual(rc, 1)
        self.assertEqual(self._index_attempts(counts), 1,
                         "retry disabled => exactly one attempt")
        sleep.assert_not_called()

    def test_no_retry_without_deadline_headroom(self):
        """With a nearly-expired deadline (2s budget < jitter + per-upload
        floor + 2s slack) the retry must be skipped entirely — no jitter sleep,
        single attempt — so the retry can never push reconcile past its
        deadline ceiling."""
        from unittest.mock import MagicMock
        uploader, counts = self._flaky_uploader("/index.json", fail_first_n=None)
        sleep = MagicMock()
        old_timeout = os.environ.get("FULCRA_COORD_RECONCILE_TIMEOUT_SECONDS")
        os.environ["FULCRA_COORD_RECONCILE_TIMEOUT_SECONDS"] = "2"
        try:
            rc, _ = self._run_reconcile(uploader, sleep)
        finally:
            if old_timeout is None:
                del os.environ["FULCRA_COORD_RECONCILE_TIMEOUT_SECONDS"]
            else:
                os.environ["FULCRA_COORD_RECONCILE_TIMEOUT_SECONDS"] = old_timeout

        self.assertEqual(rc, 1)
        self.assertEqual(self._index_attempts(counts), 1,
                         "no headroom => no retry attempt")
        sleep.assert_not_called()


# ---------------------------------------------------------------------------
# Debug sweep rounds 2-3 (v0.5.6)
# ---------------------------------------------------------------------------

class TestTimestampPrecision(unittest.TestCase):
    """BUG 1: isoformat() omits the fractional part when microseconds==0, so
    `...:45Z` (µs=0) sorts AFTER `...:45.000001Z` (µs=1, actually newer) under
    a lexical compare ('.' < 'Z'). Every emitted timestamp must carry 6-digit
    microseconds, and the merge/sort paths must compare PARSED datetimes so
    pre-fix mixed-precision data already on the bus still orders correctly."""

    def test_emitted_timestamps_always_have_six_digit_microseconds(self):
        import re
        from datetime import datetime, timezone
        from fulcra_coord import timeutil as timeutil_mod
        from fulcra_coord import cache as cache_mod
        from fulcra_coord import annotations as ann_mod
        pat = re.compile(r"\.\d{6}Z$")
        # A datetime whose microsecond is exactly 0 is the trigger case.
        zero_us = datetime(2026, 6, 3, 12, 30, 45, 0, tzinfo=timezone.utc)
        # schema emission helpers (parse_when / make_task / apply_*).
        self.assertRegex(schema.parse_when("0d", now=zero_us), pat)
        t = make_task(title="x", workstream="ws", agent="a", dt=zero_us)
        self.assertRegex(t["updated_at"], pat)
        # The per-file now-string helpers (timeutil — the cli write paths ride
        # it — plus cache / annotations) — exercised at runtime when µs happens
        # to be 0, so assert the shape directly.
        for helper in (timeutil_mod.now_iso, cache_mod._now_iso, ann_mod._recorded_at):
            val = helper({}) if helper is ann_mod._recorded_at else helper()
            self.assertRegex(val, pat, f"{helper.__module__}.{helper.__name__}")

    def test_try_merge_picks_newer_side_across_mixed_precision(self):
        from fulcra_coord.cli import _try_merge
        # Same second; local has µs>0 (truly newer) but its string lacks the
        # fractional part of remote? No — construct the silent-data-loss case:
        # remote µs=0 -> "...45Z"; local µs=1 -> "...45.000001Z" (newer).
        # Lexically "...45.000001Z" < "...45Z" so a raw-string compare wrongly
        # treats REMOTE as newer and drops local's field edit.
        base_events = [{"at": "2026-06-03T12:00:00.000000Z", "type": "active", "by": "a"}]
        local = {
            "id": "T1", "status": "active", "title": "loc",
            "workstream": "ws", "owner_agent": "a",
            "updated_at": "2026-06-03T12:30:45.000001Z",
            "current_summary": "LOCAL-NEWER", "events": list(base_events),
        }
        remote = {
            "id": "T1", "status": "active", "title": "rem",
            "workstream": "ws", "owner_agent": "a",
            "updated_at": "2026-06-03T12:30:45Z",
            "current_summary": "remote-older", "events": list(base_events),
        }
        merged = _try_merge(local, remote)
        self.assertIsNotNone(merged)
        # Local is genuinely newer, so its field edits must win.
        self.assertEqual(merged["current_summary"], "LOCAL-NEWER")
        self.assertEqual(merged["title"], "loc")

    def test_build_presence_sorts_correctly_across_mixed_precision(self):
        # Newest record carries µs>0; an older record has µs=0. A lexical sort
        # would put the µs=0 string first ('Z' > '.'), inverting the order.
        records = [
            {"agent": "older", "last_seen": "2026-06-03T12:30:45Z"},
            {"agent": "newer", "last_seen": "2026-06-03T12:30:45.500000Z"},
        ]
        out = views.build_presence(records)
        self.assertEqual(out["agents"][0]["agent"], "newer",
                         "most-recently-seen agent must sort first")


class TestAssignClearsNeedsHuman(unittest.TestCase):
    """BUG 3: `block --on-user` parks a task on the human (assignee=human +
    `needs:human` tag). Reassigning it to another agent changed the assignee but
    LEFT the `needs:human` tag, so views.needs_human (which counts that tag) kept
    showing it on the human's plate forever. cmd_assign must strip the tag when
    reassigning AWAY from the human — but keep it when assigning TO the human."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp
        os.environ["FULCRA_COORD_HUMAN"] = "ash"
        self.fake_backend = ["false"]

    def tearDown(self):
        del os.environ["XDG_CACHE_HOME"]
        del os.environ["FULCRA_COORD_HUMAN"]

    def _blocked_on_user_task(self):
        from fulcra_coord import identity
        task = make_task(title="Needs key", workstream="ops", agent="agent-a")
        task = apply_transition(task, "active", by="agent-a")
        task["status"] = "blocked"
        task["assignee"] = identity.resolve_human()
        task["tags"] = sorted(set(task.get("tags", []) + ["needs:human"]))
        cache.write_cached_task(task)
        return task

    def test_reassign_away_from_human_strips_needs_human(self):
        from fulcra_coord.cli import cmd_assign
        from fulcra_coord import identity
        task = self._blocked_on_user_task()
        tid = task["id"]
        human = identity.resolve_human()
        # Sanity: it's on the human's plate before reassignment.
        before = views.needs_human([schema.task_summary(task)], human)
        self.assertTrue(any(s["id"] == tid for s in before))

        args = types.SimpleNamespace(task_id=tid, assignee="agent-b", agent="agent-a")
        cmd_assign(args, backend=self.fake_backend)

        cached = cache.read_cached_task(tid)
        self.assertNotIn("needs:human", cached.get("tags", []))
        self.assertEqual(cached.get("assignee"), "agent-b")
        after = views.needs_human([schema.task_summary(cached)], human)
        self.assertFalse(any(s["id"] == tid for s in after),
                         "task must leave the human's plate after reassignment")

    def test_reassign_to_human_keeps_needs_human(self):
        from fulcra_coord.cli import cmd_assign
        from fulcra_coord import identity
        task = self._blocked_on_user_task()
        tid = task["id"]
        human = identity.resolve_human()
        # Reassigning back TO the human must NOT strip the tag.
        args = types.SimpleNamespace(task_id=tid, assignee=human, agent="agent-a")
        cmd_assign(args, backend=self.fake_backend)
        cached = cache.read_cached_task(tid)
        self.assertIn("needs:human", cached.get("tags", []))


class TestAgeStrNaive(unittest.TestCase):
    """BUG 6a: _age_str parsed updated_at then subtracted from an AWARE now. A
    tz-less (naive) stored timestamp made that subtraction raise TypeError — only
    ValueError/AttributeError were caught — crashing a read-only view. A naive
    parse must be coerced to UTC (matching views._parse_dt) and yield a sane age."""

    def test_age_str_naive_timestamp_does_not_crash(self):
        from fulcra_coord.cli import _age_str
        # A naive (no Z / no offset) timestamp far in the past -> "d" age, not crash.
        out = _age_str("2000-01-01T00:00:00")
        self.assertNotEqual(out, "?")
        self.assertTrue(out.endswith("d"))


class TestUploadOneSubSecondDeadline(unittest.TestCase):
    """BUG 6b: _upload_one used timeout=max(1, int(remaining)); with
    0<remaining<1 it floored UP to 1s, letting an upload run ~1s past the global
    reconcile deadline. A sub-1s remaining must be treated as past-deadline
    (skip, return False) — consistent with the prior `remaining <= 0` guard."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp

    def tearDown(self):
        del os.environ["XDG_CACHE_HOME"]

    def test_no_upload_starts_with_sub_second_budget(self):
        from fulcra_coord.cli import cmd_reconcile
        task = apply_transition(_sample_task(), "active", by="claude-code")
        called = {"n": 0}

        def upload_json(data, path, *, backend=None, timeout=None):
            called["n"] += 1
            return True

        # Drive time deterministically: cmd_reconcile reads monotonic() once at
        # t0 (=0.0) to set deadline = 0.0 + 90; every later read returns 89.5, so
        # each worker sees remaining = 0.5s (< 1) and must SKIP its upload.
        base = 0.0
        seq = iter([0.0])
        def fake_monotonic():
            try:
                return next(seq)
            except StopIteration:
                return base + 89.5  # 0.5s before the 90s deadline

        with patch("fulcra_coord.cli._load_all_tasks", return_value=[task]), \
             patch("fulcra_coord.cli.remote.upload_json", side_effect=upload_json), \
             patch("fulcra_coord.cli._reconcile_presence"), \
             patch("time.monotonic", side_effect=fake_monotonic):
            rc = cmd_reconcile(types.SimpleNamespace(), backend=["false"])

        self.assertEqual(called["n"], 0,
                         "no upload may start with <1s remaining before deadline")
        self.assertEqual(rc, 1, "all views skipped past-deadline -> partial -> exit 1")


class TestTaskSummaryDefensive(unittest.TestCase):
    """BUG 2: task_summary hard-indexed id/title/status/workstream/owner_agent.
    It runs inside the best-effort view loader (exceptions swallowed), so a body
    missing any of those fields raised KeyError and the task SILENTLY VANISHED
    from every view — the opposite of what the safety-net docstring promises.
    The summary must render with empty-string defaults instead."""

    def test_task_summary_tolerates_missing_required_fields(self):
        # A body missing workstream/owner_agent (and even title) must summarize,
        # not KeyError — so a malformed task still surfaces in views.
        partial = {"id": "T-orphan", "status": "active"}
        s = schema.task_summary(partial)
        self.assertEqual(s["id"], "T-orphan")
        self.assertEqual(s["workstream"], "")
        self.assertEqual(s["owner_agent"], "")
        self.assertEqual(s["title"], "")

    def test_task_summary_empty_dict_does_not_crash(self):
        s = schema.task_summary({})
        self.assertEqual(s["id"], "")
        self.assertEqual(s["title"], "")
        self.assertEqual(s["status"], "")

    def test_apply_transition_tolerates_missing_tag_fields(self):
        # apply_transition rebuilds tags from workstream/owner_agent/priority via
        # bracket-indexing; a slightly-malformed body must not KeyError mid-write.
        from datetime import datetime, timezone
        task = {"id": "T1", "status": "proposed", "title": "x", "events": []}
        out = schema.apply_transition(
            task, new_status="active", by="a",
            dt=datetime(2026, 6, 3, tzinfo=timezone.utc))
        self.assertEqual(out["status"], "active")


# ---------------------------------------------------------------------------
# Liveness-aware reviewer routing — Task 1: resolve_live_recipient
# ---------------------------------------------------------------------------


class TestPresenceGraceSeconds(unittest.TestCase):
    def test_presence_grace_seconds_default(self):
        from fulcra_coord import views
        os.environ.pop("FULCRA_COORD_PRESENCE_GRACE_SECONDS", None)
        self.assertEqual(views._presence_grace_seconds(), 1200.0)

    def test_presence_grace_seconds_env_override(self):
        from fulcra_coord import views
        os.environ["FULCRA_COORD_PRESENCE_GRACE_SECONDS"] = "300"
        try:
            self.assertEqual(views._presence_grace_seconds(), 300.0)
        finally:
            os.environ.pop("FULCRA_COORD_PRESENCE_GRACE_SECONDS", None)

    def test_presence_grace_seconds_bad_value_falls_back(self):
        from fulcra_coord import views
        os.environ["FULCRA_COORD_PRESENCE_GRACE_SECONDS"] = "not-a-number"
        try:
            self.assertEqual(views._presence_grace_seconds(), 1200.0)
        finally:
            os.environ.pop("FULCRA_COORD_PRESENCE_GRACE_SECONDS", None)


class TestResolveLiveRecipient(unittest.TestCase):
    NOW = datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc)

    def _rec(self, agent, minutes_ago, liveness="stale", caps=None):
        # liveness is DELIBERATELY wrong/stale here to prove the resolver
        # recomputes from last_seen and ignores the stored field.
        ls = (self.NOW - timedelta(minutes=minutes_ago)).isoformat(
            timespec="microseconds").replace("+00:00", "Z")
        r = {"agent": agent, "last_seen": ls, "liveness": liveness}
        if caps is not None:
            r["capabilities"] = caps
        return r

    def test_effective_liveness_recomputed_from_last_seen_not_stored_tier(self):
        from fulcra_coord import views
        # Aggregate says 'stale', but last_seen is 90 min old -> within
        # stale_cutoff (2h) so effectively idle -> qualifies at floor=idle.
        presence = [self._rec("a", 90, liveness="stale")]
        self.assertEqual(
            views.resolve_live_recipient(["a"], presence, floor="idle", now=self.NOW), "a")

    def test_grace_window_keeps_just_stale_agent_eligible(self):
        from fulcra_coord import views
        # 2h10m old: past the 2h idle->stale cutoff but within 2h + 1200s grace.
        presence = [self._rec("a", 130)]
        self.assertEqual(
            views.resolve_live_recipient(["a"], presence, floor="idle", now=self.NOW), "a")

    def test_beyond_grace_is_below_floor_returns_none(self):
        from fulcra_coord import views
        # 2h21m old: past 2h + 1200s (20m) grace -> below floor.
        presence = [self._rec("a", 141)]
        self.assertIsNone(
            views.resolve_live_recipient(["a"], presence, floor="idle", now=self.NOW))

    def test_tier_dominates_preference_live_noncanonical_beats_idle_canonical(self):
        from fulcra_coord import views
        # canonical 'canon' listed first but idle (90m); 'other' second but live.
        presence = [self._rec("canon", 90), self._rec("other", 10)]
        self.assertEqual(
            views.resolve_live_recipient(["canon", "other"], presence, floor="idle", now=self.NOW),
            "other")

    def test_preference_breaks_ties_within_same_tier(self):
        from fulcra_coord import views
        presence = [self._rec("first", 10), self._rec("second", 5)]  # both live
        self.assertEqual(
            views.resolve_live_recipient(["first", "second"], presence, floor="idle", now=self.NOW),
            "first")

    def test_floor_live_excludes_idle(self):
        from fulcra_coord import views
        presence = [self._rec("a", 90)]  # idle
        self.assertIsNone(
            views.resolve_live_recipient(["a"], presence, floor="live", now=self.NOW))
        self.assertEqual(
            views.resolve_live_recipient(["a"], presence, floor="idle", now=self.NOW), "a")

    def test_exclude_skips_tried(self):
        from fulcra_coord import views
        presence = [self._rec("a", 10), self._rec("b", 10)]
        self.assertEqual(
            views.resolve_live_recipient(["a", "b"], presence, floor="idle", now=self.NOW,
                                         exclude=("a",)), "b")

    def test_empty_candidates_returns_none(self):
        from fulcra_coord import views
        self.assertIsNone(
            views.resolve_live_recipient([], [], floor="idle", now=self.NOW))

    def test_all_below_floor_returns_none(self):
        from fulcra_coord import views
        presence = [self._rec("a", 200), self._rec("b", 300)]
        self.assertIsNone(
            views.resolve_live_recipient(["a", "b"], presence, floor="idle", now=self.NOW))

    def test_candidate_missing_from_presence_is_below_floor(self):
        from fulcra_coord import views
        # canonical seed that never connected: no presence record -> skipped.
        presence = [self._rec("b", 10)]
        self.assertEqual(
            views.resolve_live_recipient(["a", "b"], presence, floor="idle", now=self.NOW), "b")


# ---------------------------------------------------------------------------
# Liveness-aware reviewer routing — Task 2: presence capabilities
# ---------------------------------------------------------------------------


class TestPresenceCapabilities(unittest.TestCase):
    def test_make_presence_default_capabilities_empty(self):
        rec = schema.make_presence("claude-code:h:r")
        self.assertEqual(rec["capabilities"], [])

    def test_make_presence_records_capabilities_sorted_unique(self):
        rec = schema.make_presence("a", capabilities=["review", "review", "deploy"])
        self.assertEqual(rec["capabilities"], ["deploy", "review"])

    def test_build_presence_carries_capabilities_through(self):
        rec = schema.make_presence("a", capabilities=["review"])
        agg = views.build_presence([rec])
        self.assertEqual(agg["agents"][0]["capabilities"], ["review"])


# ---------------------------------------------------------------------------
# Liveness-aware reviewer routing — Task 3: routing-event vocabulary
# ---------------------------------------------------------------------------


class TestRoutingEvents(unittest.TestCase):
    def _task_with_events(self, events, assignee=None, tags=None):
        return {"id": "TASK-20260604-x-00000000", "assignee": assignee,
                "tags": tags or [], "events": events}

    def test_make_route_event_shape(self):
        from fulcra_coord import routing
        ev = routing.make_route_event(kind="routed", to="a", by="b", attempt=1,
                                      reason="live",
                                      candidate_snapshot=[{"agent": "a", "tier": "live"}],
                                      observed_updated_at="2026-06-04T12:00:00.000000Z",
                                      at="2026-06-04T12:00:00.000000Z", route_id="rid-1")
        self.assertEqual(ev["type"], "routed")
        self.assertEqual({"at", "type", "to", "by", "attempt", "reason",
                          "candidate_snapshot", "observed_updated_at", "route_id"},
                         set(ev))

    def test_is_review_directive_by_tag(self):
        from fulcra_coord import routing
        self.assertTrue(routing.is_review_directive(
            self._task_with_events([], tags=["kind:review"])))
        self.assertFalse(routing.is_review_directive(
            self._task_with_events([], tags=["kind:ops"])))

    def test_current_route_latest_by_at(self):
        from fulcra_coord import routing
        e1 = routing.make_route_event(kind="routed", to="a", by="s", attempt=1, reason="x",
                                      candidate_snapshot=[], observed_updated_at="t",
                                      at="2026-06-04T12:00:00.000000Z", route_id="r1")
        e2 = routing.make_route_event(kind="rerouted", to="b", by="s", attempt=2, reason="y",
                                      candidate_snapshot=[], observed_updated_at="t",
                                      at="2026-06-04T12:05:00.000000Z", route_id="r2")
        task = self._task_with_events([e1, e2])
        self.assertEqual(routing.current_route(task)["to"], "b")

    def test_current_route_tie_break_by_route_id(self):
        from fulcra_coord import routing
        same = "2026-06-04T12:00:00.000000Z"
        e1 = routing.make_route_event(kind="routed", to="a", by="s", attempt=1, reason="x",
                                      candidate_snapshot=[], observed_updated_at="t",
                                      at=same, route_id="r-aaa")
        e2 = routing.make_route_event(kind="rerouted", to="b", by="s", attempt=2, reason="y",
                                      candidate_snapshot=[], observed_updated_at="t",
                                      at=same, route_id="r-bbb")
        # higher route_id wins the tie deterministically (stable across machines).
        self.assertEqual(
            routing.current_route(self._task_with_events([e1, e2]))["route_id"], "r-bbb")

    def test_route_attempt_count_and_tried(self):
        from fulcra_coord import routing
        e1 = routing.make_route_event(kind="routed", to="a", by="s", attempt=1, reason="x",
                                      candidate_snapshot=[], observed_updated_at="t",
                                      at="2026-06-04T12:00:00.000000Z", route_id="r1")
        e2 = routing.make_route_event(kind="rerouted", to="b", by="s", attempt=2, reason="y",
                                      candidate_snapshot=[], observed_updated_at="t",
                                      at="2026-06-04T12:05:00.000000Z", route_id="r2")
        task = self._task_with_events([e1, e2])
        self.assertEqual(routing.route_attempt_count(task), 2)
        self.assertEqual(routing.tried_agents(task), {"a", "b"})

    def test_route_attempt_count_uses_cumulative_attempt_field(self):
        from fulcra_coord import routing
        e1 = routing.make_route_event(kind="routed", to="a", by="s", attempt=7, reason="x",
                                      candidate_snapshot=[], observed_updated_at="t",
                                      at="2026-06-04T12:00:00.000000Z", route_id="r1")
        e2 = routing.make_route_event(kind="rerouted", to="b", by="s", attempt=8, reason="y",
                                      candidate_snapshot=[], observed_updated_at="t",
                                      at="2026-06-04T12:05:00.000000Z", route_id="r2")
        task = self._task_with_events([e1, e2])
        self.assertEqual(routing.route_attempt_count(task), 8)

    def test_current_route_none_when_no_route_events(self):
        from fulcra_coord import routing
        self.assertIsNone(routing.current_route(
            self._task_with_events([{"at": "t", "type": "created", "by": "x"}])))

    def test_latest_route_event_alias(self):
        from fulcra_coord import routing
        e1 = routing.make_route_event(kind="routed", to="a", by="s", attempt=1, reason="x",
                                      candidate_snapshot=[], observed_updated_at="t",
                                      at="2026-06-04T12:00:00.000000Z", route_id="r1")
        task = self._task_with_events([e1])
        self.assertEqual(routing.latest_route_event(task)["route_id"], "r1")

    def test_make_route_event_rejects_bad_kind(self):
        from fulcra_coord import routing
        with self.assertRaises(ValueError):
            routing.make_route_event(kind="created", to="a", by="b", attempt=1,
                                     reason="x", candidate_snapshot=[],
                                     observed_updated_at="t", at="t")

    def test_make_route_event_mints_route_id_when_absent(self):
        from fulcra_coord import routing
        ev = routing.make_route_event(kind="routed", to="a", by="b", attempt=1,
                                      reason="x", candidate_snapshot=[],
                                      observed_updated_at="t", at="t")
        self.assertTrue(ev["route_id"])


# ---------------------------------------------------------------------------
# Liveness-aware reviewer routing — Task 4: request-review pool builder
# ---------------------------------------------------------------------------


class TestReviewPool(unittest.TestCase):
    def test_pool_seeds_from_config_even_when_undeclared(self):
        from fulcra_coord import cli, routing_ops as ro
        with patch.object(ro, "_review_seeds", lambda a: ["seed:h:r"]):
            presence = [{"agent": "x:y:z", "capabilities": ["review"]}]
            pool = cli._review_pool(author="who:h:r", presence=presence)
            self.assertEqual(pool[0], "seed:h:r")
            self.assertIn("x:y:z", pool)

    def test_pool_empty_seed_is_capability_only(self):
        from fulcra_coord import cli, routing_ops as ro
        with patch.object(ro, "_review_seeds", lambda a: []):
            presence = [{"agent": "rev:h:r", "capabilities": ["review"]},
                        {"agent": "no:h:r", "capabilities": []}]
            pool = cli._review_pool(author="who:h:r", presence=presence)
            self.assertEqual(pool, ["rev:h:r"])

    def test_pool_excludes_non_review_capable_and_devops(self):
        from fulcra_coord import cli, routing_ops as ro
        with patch.object(ro, "_review_seeds", lambda a: []):
            presence = [
                {"agent": "openclaw:discord:devops", "last_seen": "...", "capabilities": []},
                {"agent": "rev:h:r", "last_seen": "...", "capabilities": ["review"]},
            ]
            pool = cli._review_pool(author="who:h:r", presence=presence)
            self.assertNotIn("openclaw:discord:devops", pool)
            self.assertIn("rev:h:r", pool)

    def test_pool_no_duplicate_when_seed_also_declares(self):
        from fulcra_coord import cli, routing_ops as ro
        with patch.object(ro, "_review_seeds", lambda a: ["dup:h:r"]):
            presence = [{"agent": "dup:h:r", "last_seen": "...",
                         "capabilities": ["review"]}]
            pool = cli._review_pool(author="who:h:r", presence=presence)
            self.assertEqual(pool.count("dup:h:r"), 1)


# ---------------------------------------------------------------------------
# Liveness-aware reviewer routing — Task 4: request-review command
# ---------------------------------------------------------------------------


class TestRequestReview(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._old_cache = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = self._tmp

    def tearDown(self):
        if self._old_cache is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = self._old_cache
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _presence_agg(self, agents):
        return {"agents": agents}

    def test_dry_run_prints_pool_and_writes_nothing(self):
        from fulcra_coord.cli import cmd_request_review
        now_ls = datetime.now(timezone.utc).isoformat(
            timespec="microseconds").replace("+00:00", "Z")
        agg = self._presence_agg([{"agent": "codex:Mac.localdomain:main",
                                   "last_seen": now_ls, "capabilities": ["review"]}])
        with patch("fulcra_coord.cli.remote.download_json", return_value=agg), \
             patch("fulcra_coord.routing_ops._write_task_and_views") as wtv, \
             patch("fulcra_coord.cli.identity.resolve_agent",
                   return_value="codex:Mac.localdomain:main"):
            args = types.SimpleNamespace(pr="42", repo="fulcra-tools", dry_run=True,
                                         candidate_list=None, format="json", agent=None)
            rc = cmd_request_review(args, backend=["false"])
        self.assertEqual(rc, 0)
        wtv.assert_not_called()  # dry-run writes nothing

    def test_hit_routes_tagged_review_with_routed_event_and_assignee(self):
        from fulcra_coord.cli import cmd_request_review
        now_ls = datetime.now(timezone.utc).isoformat(
            timespec="microseconds").replace("+00:00", "Z")
        agg = self._presence_agg([{"agent": "codex:Mac.localdomain:main",
                                   "last_seen": now_ls, "capabilities": ["review"]}])
        captured = {}

        def fake_write(task, backend=None, command="write", lifecycle=None):
            captured["task"] = task
            return True

        with patch("fulcra_coord.cli.remote.download_json", return_value=agg), \
             patch("fulcra_coord.routing_ops._write_task_and_views", side_effect=fake_write), \
             patch("fulcra_coord.cli.identity.resolve_agent", return_value="claude-code:h:r"):
            args = types.SimpleNamespace(pr="42", repo="fulcra-tools", dry_run=False,
                                         candidate_list=None, format="json", agent=None)
            rc = cmd_request_review(args, backend=["false"])
        t = captured["task"]
        self.assertEqual(rc, 0)
        self.assertEqual(t["assignee"], "codex:Mac.localdomain:main")
        self.assertIn("kind:review", t["tags"])
        routed = [e for e in t["events"] if e["type"] == "routed"]
        self.assertEqual(len(routed), 1)
        self.assertIn("route_id", routed[0])
        self.assertEqual(routed[0]["to"], "codex:Mac.localdomain:main")

    def test_miss_escalates_via_block_on_user(self):
        from fulcra_coord.cli import cmd_request_review
        old_ls = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(
            timespec="microseconds").replace("+00:00", "Z")
        agg = self._presence_agg([{"agent": "codex:Mac.localdomain:main",
                                   "last_seen": old_ls, "capabilities": ["review"]}])
        escalated = {}
        with patch("fulcra_coord.cli.remote.download_json", return_value=agg), \
             patch("fulcra_coord.routing_ops._escalate_review_to_human",
                   side_effect=lambda **kw: escalated.update(kw) or True), \
             patch("fulcra_coord.cli.identity.resolve_agent", return_value="claude-code:h:r"):
            args = types.SimpleNamespace(pr="42", repo="fulcra-tools", dry_run=False,
                                         candidate_list=None, format="json", agent=None)
            rc = cmd_request_review(args, backend=["false"])
        self.assertEqual(rc, 0)
        self.assertIn("42", escalated.get("pr", ""))

    def test_miss_returns_nonzero_when_human_escalation_fails(self):
        from fulcra_coord.cli import cmd_request_review
        old_ls = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(
            timespec="microseconds").replace("+00:00", "Z")
        agg = self._presence_agg([{"agent": "codex:Mac.localdomain:main",
                                   "last_seen": old_ls, "capabilities": ["review"]}])
        with patch("fulcra_coord.cli.remote.download_json", return_value=agg), \
             patch("fulcra_coord.routing_ops._escalate_review_to_human",
                   return_value=False), \
             patch("fulcra_coord.cli.identity.resolve_agent", return_value="claude-code:h:r"):
            args = types.SimpleNamespace(pr="42", repo="fulcra-tools", dry_run=False,
                                         candidate_list=None, format="json", agent=None)
            rc = cmd_request_review(args, backend=["false"])
        self.assertEqual(rc, 1)

    def test_escalate_to_human_lands_blocked_needs_human(self):
        # _escalate_review_to_human must actually land a blocked, human-assigned
        # task carrying needs:human, even though make_task starts at 'proposed'
        # (proposed -> blocked is not a direct allowed transition).
        from fulcra_coord.cli import _escalate_review_to_human
        captured = {}

        def fake_write(task, backend=None, command="write", lifecycle=None):
            captured["task"] = task
            return True

        with patch("fulcra_coord.routing_ops._write_task_and_views", side_effect=fake_write), \
             patch("fulcra_coord.cli.identity.resolve_human", return_value="redacted@users.noreply.github.com"), \
             patch("fulcra_coord.cli.identity.resolve_agent", return_value="codex:m:main"):
            ok = _escalate_review_to_human(pr="42", repo="fulcra-tools",
                                           tried=["dead:h:r"], backend=["false"])
        self.assertTrue(ok)
        t = captured["task"]
        self.assertEqual(t["status"], "blocked")
        self.assertEqual(t["assignee"], "redacted@users.noreply.github.com")
        self.assertIn("needs:human", t["tags"])

    def test_escalation_forge_agnostic_no_pr_no_none(self):
        """Regression: escalating a non-numeric artifact with no --repo must
        produce a clean ask + marker — no hardcoded "PR ", no literal "None".

        Before the fix `_escalate_review_to_human` built `f"PR #{artifact} needs a
        reviewer ({repo})"` and a marker `review-escalation:{repo}#{artifact}`, so
        a branch ref with repo=None read "PR #feat/x needs a reviewer (None)" and
        the marker "review-escalation:None#feat/x"."""
        from fulcra_coord.cli import _escalate_review_to_human
        captured = {}

        def fake_write(task, backend=None, command="write", lifecycle=None):
            captured["task"] = task
            return True

        with patch("fulcra_coord.routing_ops._write_task_and_views", side_effect=fake_write), \
             patch("fulcra_coord.cli.identity.resolve_human", return_value="redacted@users.noreply.github.com"), \
             patch("fulcra_coord.cli.identity.resolve_agent", return_value="codex:m:main"):
            ok = _escalate_review_to_human(pr="feat/x", repo=None,
                                           tried=["dead:h:r"], backend=["false"])
        self.assertTrue(ok)
        t = captured["task"]
        blob = json.dumps(t)
        # No hardcoded "PR " and no literal "None" leaking into ask / title / marker.
        self.assertNotIn("PR ", blob)
        self.assertNotIn("None", blob)
        # The branch ref appears verbatim; the marker uses repo "general", not None.
        self.assertIn("feat/x", blob)
        marker = "review-escalation:general#feat/x"
        self.assertIn(marker, t["tags"])


# ---------------------------------------------------------------------------
# Forge-agnostic review handshake — Part 1: request-review opaque artifact ref
# ---------------------------------------------------------------------------


class TestRequestReviewArtifactRef(unittest.TestCase):
    """request-review's artifact is now an OPAQUE ref (PR#/MR#/branch/SHA/URL),
    not a GitHub PR. Numeric refs keep the bare "#<n>" display (backward compat
    with the old "Review PR #<n>" routing, minus the hardcoded "PR "); non-numeric
    refs (branches, SHAs) get the artifact verbatim. --repo is OPTIONAL."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._old_cache = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = self._tmp

    def tearDown(self):
        if self._old_cache is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = self._old_cache
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _live_presence(self):
        now_ls = datetime.now(timezone.utc).isoformat(
            timespec="microseconds").replace("+00:00", "Z")
        return {"agents": [{"agent": "codex:Mac.localdomain:main",
                            "last_seen": now_ls, "capabilities": ["review"]}]}

    def _route_and_capture(self, *, pr, repo):
        """Run request-review with a live reviewer, returning the written task."""
        from fulcra_coord.cli import cmd_request_review
        captured = {}

        def fake_write(task, backend=None, command="write", lifecycle=None):
            captured["task"] = task
            return True

        with patch("fulcra_coord.cli.remote.download_json",
                   return_value=self._live_presence()), \
             patch("fulcra_coord.routing_ops._write_task_and_views",
                   side_effect=fake_write), \
             patch("fulcra_coord.cli.identity.resolve_agent",
                   return_value="claude-code:h:r"):
            args = types.SimpleNamespace(pr=pr, repo=repo, dry_run=False,
                                         candidate_list=None, format="json",
                                         agent=None)
            rc = cmd_request_review(args, backend=["false"])
        return rc, captured["task"]

    def test_bare_number_artifact_titles_without_PR_word(self):
        rc, t = self._route_and_capture(pr="101", repo="o/r")
        self.assertEqual(rc, 0)
        # Old behaviour said "Review PR #101"; the de-named title drops "PR ".
        self.assertIn("Review #101", t["title"])
        self.assertNotIn("PR #101", t["title"])

    def test_branch_artifact_titles_verbatim(self):
        rc, t = self._route_and_capture(pr="feat/my-branch", repo=None)
        self.assertEqual(rc, 0)
        self.assertIn("Review feat/my-branch", t["title"])
        self.assertNotIn("#feat", t["title"])  # not numeric -> no '#'

    def test_repo_omitted_still_routes_and_tags_review(self):
        rc, t = self._route_and_capture(pr="feat/x", repo=None)
        self.assertEqual(rc, 0)
        self.assertEqual(t["assignee"], "codex:Mac.localdomain:main")
        self.assertIn("kind:review", t["tags"])
        routed = [e for e in t["events"] if e["type"] == "routed"]
        self.assertEqual(len(routed), 1)

    def test_backward_compat_number_with_repo_still_routes(self):
        rc, t = self._route_and_capture(pr="101", repo="o/r")
        self.assertEqual(rc, 0)
        self.assertEqual(t["assignee"], "codex:Mac.localdomain:main")
        self.assertIn("kind:review", t["tags"])
        self.assertEqual(t.get("repo"), "o/r")
        self.assertEqual(t.get("pr"), "101")


# ---------------------------------------------------------------------------
# Forge-agnostic review handshake — Part 2: review-done verdict directive
# ---------------------------------------------------------------------------


class TestReviewDone(unittest.TestCase):
    """`review-done` lands the reviewer's verdict as a BUS directive to the
    artifact's AUTHOR — never a GitHub comment. coord must NEVER call a forge."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._old_cache = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = self._tmp

    def tearDown(self):
        if self._old_cache is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = self._old_cache
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _args(self, **kw):
        base = dict(artifact="101", verdict="approve", note=None, repo=None,
                    to=None, format="table")
        base.update(kw)
        ns = types.SimpleNamespace(**{k: v for k, v in base.items() if k != "from"})
        setattr(ns, "from", base.get("from"))
        return ns

    def _review_task(self, *, artifact, author):
        """A routed kind:review directive as request-review would have written it
        (owner_agent = the author who requested the review)."""
        title = (f"Review #{artifact} — assume bugs, claim the review before working"
                 if str(artifact).isdigit()
                 else f"Review {artifact} — assume bugs, claim the review before working")
        return {"id": "TASK-20260608-rev-00000000", "status": "proposed",
                "title": title, "owner_agent": author, "assignee": "codex:m:main",
                "tags": ["kind:review"], "events": [], "workstream": "o/r",
                "pr": artifact}

    def test_to_override_lands_verdict_directive(self):
        from fulcra_coord.cli import cmd_review_done
        captured = {}

        def fake_write(task, backend=None, command="write", lifecycle=None):
            captured["task"] = task
            return True

        with patch("fulcra_coord.routing_ops._write_task_and_views",
                   side_effect=fake_write), \
             patch("fulcra_coord.routing_ops.identity.resolve_agent",
                   return_value="codex:rev:main"):
            rc = cmd_review_done(self._args(verdict="approve", to="claude-code:h:r",
                                            note="LGTM"), backend=["false"])
        self.assertEqual(rc, 0)
        t = captured["task"]
        self.assertEqual(t["assignee"], "claude-code:h:r")
        self.assertIn("kind:review-verdict", t["tags"])
        self.assertIn("approve", t["title"])
        self.assertIn("#101", t["title"])
        blob = json.dumps(t)
        self.assertIn("LGTM", blob)

    def test_changes_verdict_in_title(self):
        from fulcra_coord.cli import cmd_review_done
        captured = {}
        with patch("fulcra_coord.routing_ops._write_task_and_views",
                   side_effect=lambda task, **kw: captured.update(task=task) or True), \
             patch("fulcra_coord.routing_ops.identity.resolve_agent",
                   return_value="codex:rev:main"):
            rc = cmd_review_done(self._args(verdict="changes", to="claude-code:h:r"),
                                 backend=["false"])
        self.assertEqual(rc, 0)
        self.assertIn("changes", captured["task"]["title"])

    def test_author_resolved_from_existing_review_task(self):
        from fulcra_coord.cli import cmd_review_done
        captured = {}
        existing = [self._review_task(artifact="101", author="claude-code:author:r")]
        with patch("fulcra_coord.routing_ops._load_all_tasks", return_value=existing), \
             patch("fulcra_coord.routing_ops._write_task_and_views",
                   side_effect=lambda task, **kw: captured.update(task=task) or True), \
             patch("fulcra_coord.routing_ops.identity.resolve_agent",
                   return_value="codex:rev:main"):
            rc = cmd_review_done(self._args(verdict="approve", to=None),
                                 backend=["false"])
        self.assertEqual(rc, 0)
        # Author came from the existing kind:review task's owner_agent.
        self.assertEqual(captured["task"]["assignee"], "claude-code:author:r")

    def test_branch_artifact_author_resolution(self):
        from fulcra_coord.cli import cmd_review_done
        captured = {}
        existing = [self._review_task(artifact="feat/x", author="claude-code:author:r")]
        with patch("fulcra_coord.routing_ops._load_all_tasks", return_value=existing), \
             patch("fulcra_coord.routing_ops._write_task_and_views",
                   side_effect=lambda task, **kw: captured.update(task=task) or True), \
             patch("fulcra_coord.routing_ops.identity.resolve_agent",
                   return_value="codex:rev:main"):
            rc = cmd_review_done(self._args(artifact="feat/x", verdict="approve",
                                            to=None), backend=["false"])
        self.assertEqual(rc, 0)
        self.assertEqual(captured["task"]["assignee"], "claude-code:author:r")
        self.assertIn("Review verdict", captured["task"]["title"])

    def test_unresolvable_author_clean_error_no_guess(self):
        from fulcra_coord.cli import cmd_review_done
        wrote = {"called": False}
        with patch("fulcra_coord.routing_ops._load_all_tasks", return_value=[]), \
             patch("fulcra_coord.routing_ops._write_task_and_views",
                   side_effect=lambda task, **kw: wrote.update(called=True) or True), \
             patch("fulcra_coord.routing_ops.identity.resolve_agent",
                   return_value="codex:rev:main"):
            rc = cmd_review_done(self._args(verdict="approve", to=None),
                                 backend=["false"])
        # No author resolvable, no --to: clean non-zero, no write, no guess.
        self.assertNotEqual(rc, 0)
        self.assertFalse(wrote["called"])

    def test_no_forge_subprocess_call(self):
        """The verdict is bus-only: review-done must NOT shell out to gh / any
        forge. Assert subprocess is never invoked anywhere in the path."""
        from fulcra_coord.cli import cmd_review_done
        # The Phase 3b best-effort directive dual-write writes to the COORDINATION
        # bus (remote.upload_json), which itself spawns a backend subprocess — that
        # is the bus, NOT a forge. This test is specifically about review-done never
        # shelling out to gh / a forge, so stub the bus dual-write (as the real task
        # write is already stubbed via _write_task_and_views) and assert no
        # subprocess is invoked on the remaining (forge) path.
        with patch("fulcra_coord.routing_ops._write_task_and_views",
                   side_effect=lambda task, **kw: True), \
             patch("fulcra_coord.directives.dual_write"), \
             patch("fulcra_coord.routing_ops.identity.resolve_agent",
                   return_value="codex:rev:main"), \
             patch("subprocess.run") as sprun, \
             patch("subprocess.Popen") as spopen, \
             patch("subprocess.check_output") as spco:
            rc = cmd_review_done(self._args(verdict="approve", to="claude-code:h:r"),
                                 backend=["false"])
        self.assertEqual(rc, 0)
        sprun.assert_not_called()
        spopen.assert_not_called()
        spco.assert_not_called()

    def test_write_failure_is_fail_safe(self):
        """A write blowup warns, never crashes (fail-safe like other directive
        writers)."""
        from fulcra_coord.cli import cmd_review_done

        def boom(task, **kw):
            raise RuntimeError("remote exploded")

        with patch("fulcra_coord.routing_ops._write_task_and_views", side_effect=boom), \
             patch("fulcra_coord.routing_ops.identity.resolve_agent",
                   return_value="codex:rev:main"):
            rc = cmd_review_done(self._args(verdict="approve", to="claude-code:h:r"),
                                 backend=["false"])
        # Best-effort: a write failure must not raise out of the command.
        self.assertEqual(rc, 1)

    def test_resolves_in_routing_ops_namespace(self):
        """Non-vacuous patch guard (the recurring trap): the helpers review-done
        calls must actually live in routing_ops' namespace."""
        from fulcra_coord import routing_ops
        self.assertTrue(hasattr(routing_ops, "_write_task_and_views"))
        self.assertTrue(hasattr(routing_ops, "_load_all_tasks"))
        self.assertTrue(hasattr(routing_ops, "identity"))
        self.assertTrue(callable(routing_ops.cmd_review_done))

    # --- Regression: wrong-author misroute via substring match (CRITICAL) ---
    # Author resolution must match the EXACT stored artifact (task["pr"]), never
    # a substring of the directive title. Before the fix, `review-done 10`
    # resolved against a `#101` directive (because "#10" is a substring of
    # "#101"), confidently misrouting the verdict to PR #101's author.

    def test_numeric_substring_does_not_misroute(self):
        """`review-done 10` must NOT resolve from a `#101` directive — a confident
        misroute is worse than a clean error. Expect non-zero, no write."""
        from fulcra_coord.cli import cmd_review_done
        wrote = {"called": False}
        existing = [self._review_task(artifact="101", author="claude-code:author:r")]
        with patch("fulcra_coord.routing_ops._load_all_tasks", return_value=existing), \
             patch("fulcra_coord.routing_ops._write_task_and_views",
                   side_effect=lambda task, **kw: wrote.update(called=True) or True), \
             patch("fulcra_coord.routing_ops.identity.resolve_agent",
                   return_value="codex:rev:main"):
            rc = cmd_review_done(self._args(artifact="10", verdict="approve", to=None),
                                 backend=["false"])
        self.assertNotEqual(rc, 0)
        self.assertFalse(wrote["called"])

    def test_branch_substring_does_not_misroute(self):
        """`review-done feat/x` must NOT resolve from a `feat/xyz` directive."""
        from fulcra_coord.cli import cmd_review_done
        wrote = {"called": False}
        existing = [self._review_task(artifact="feat/xyz", author="claude-code:author:r")]
        with patch("fulcra_coord.routing_ops._load_all_tasks", return_value=existing), \
             patch("fulcra_coord.routing_ops._write_task_and_views",
                   side_effect=lambda task, **kw: wrote.update(called=True) or True), \
             patch("fulcra_coord.routing_ops.identity.resolve_agent",
                   return_value="codex:rev:main"):
            rc = cmd_review_done(self._args(artifact="feat/x", verdict="approve", to=None),
                                 backend=["false"])
        self.assertNotEqual(rc, 0)
        self.assertFalse(wrote["called"])

    def test_exact_numeric_artifact_resolves(self):
        """`review-done 101` against the `#101` directive resolves to its
        owner_agent (the exact-match path still works)."""
        from fulcra_coord.cli import cmd_review_done
        captured = {}
        existing = [self._review_task(artifact="101", author="claude-code:author:r")]
        with patch("fulcra_coord.routing_ops._load_all_tasks", return_value=existing), \
             patch("fulcra_coord.routing_ops._write_task_and_views",
                   side_effect=lambda task, **kw: captured.update(task=task) or True), \
             patch("fulcra_coord.routing_ops.identity.resolve_agent",
                   return_value="codex:rev:main"):
            rc = cmd_review_done(self._args(artifact="101", verdict="approve", to=None),
                                 backend=["false"])
        self.assertEqual(rc, 0)
        self.assertEqual(captured["task"]["assignee"], "claude-code:author:r")

    def test_dry_run_unresolved_author_prints_plan_returns_zero(self):
        """Dry-run never hard-fails: an unresolvable author under --dry-run prints
        the plan with `to: <unresolved>` and returns 0 (non-dry-run still errors)."""
        from fulcra_coord.cli import cmd_review_done
        wrote = {"called": False}
        with patch("fulcra_coord.routing_ops._load_all_tasks", return_value=[]), \
             patch("fulcra_coord.routing_ops._write_task_and_views",
                   side_effect=lambda task, **kw: wrote.update(called=True) or True), \
             patch("fulcra_coord.routing_ops.identity.resolve_agent",
                   return_value="codex:rev:main"):
            rc = cmd_review_done(self._args(artifact="feat/x", verdict="approve",
                                            to=None, dry_run=True), backend=["false"])
        self.assertEqual(rc, 0)
        self.assertFalse(wrote["called"])


# ---------------------------------------------------------------------------
# Liveness-aware reviewer routing — Task 6: general tell --route-capability
# ---------------------------------------------------------------------------


class TestTellRouteCapability(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._old_cache = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = self._tmp

    def tearDown(self):
        if self._old_cache is None:
            os.environ.pop("XDG_CACHE_HOME", None)
        else:
            os.environ["XDG_CACHE_HOME"] = self._old_cache
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_tell_route_capability_resolves_live_recipient(self):
        from fulcra_coord.cli import cmd_tell
        now_ls = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        agg = {"agents": [{"agent": "rev:h:r", "last_seen": now_ls, "capabilities": ["review"]}]}
        captured = {}
        with patch("fulcra_coord.cli.remote.download_json", return_value=agg), \
             patch("fulcra_coord.lifecycle._write_task_and_views",
                   side_effect=lambda task, backend=None, command="write", lifecycle=None: captured.update(task=task) or True), \
             patch("fulcra_coord.cli.identity.resolve_agent", return_value="a:b:c"):
            args = types.SimpleNamespace(assignee=None, title="Do X", next="", workstream="general",
                priority="P2", summary="", route_capability="review", floor="idle")
            setattr(args, "from", None)
            cmd_tell(args, backend=["false"])
        self.assertEqual(captured["task"]["assignee"], "rev:h:r")

    def test_tell_route_capability_miss_escalates(self):
        from fulcra_coord.cli import cmd_tell
        old_ls = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(timespec="microseconds").replace("+00:00", "Z")
        agg = {"agents": [{"agent": "rev:h:r", "last_seen": old_ls, "capabilities": ["review"]}]}
        escalated = {}
        with patch("fulcra_coord.cli.remote.download_json", return_value=agg), \
             patch("fulcra_coord.lifecycle._escalate_review_to_human",
                   side_effect=lambda **kw: escalated.update(kw) or True), \
             patch("fulcra_coord.cli.identity.resolve_agent", return_value="a:b:c"):
            args = types.SimpleNamespace(assignee=None, title="Do X", next="", workstream="general",
                priority="P2", summary="", route_capability="review", floor="idle")
            setattr(args, "from", None)
            cmd_tell(args, backend=["false"])
        self.assertTrue(escalated)


# ---------------------------------------------------------------------------
# Liveness-aware reviewer routing — Task 5: reroute sweep thresholds
# ---------------------------------------------------------------------------


class TestReviewSweepThresholds(unittest.TestCase):
    def test_reroute_minutes_defaults(self):
        from fulcra_coord import cli
        os.environ.pop("FULCRA_COORD_REVIEW_REROUTE_MINUTES_P1", None)
        os.environ.pop("FULCRA_COORD_REVIEW_REROUTE_MINUTES_P2", None)
        self.assertEqual(cli._reroute_minutes("P1"), 15.0)
        self.assertEqual(cli._reroute_minutes("P2"), 30.0)

    def test_reroute_minutes_env_override(self):
        from fulcra_coord import cli
        os.environ["FULCRA_COORD_REVIEW_REROUTE_MINUTES_P1"] = "5"
        try:
            self.assertEqual(cli._reroute_minutes("P1"), 5.0)
        finally:
            os.environ.pop("FULCRA_COORD_REVIEW_REROUTE_MINUTES_P1", None)

    def test_reroute_max_default(self):
        from fulcra_coord import cli
        os.environ.pop("FULCRA_COORD_REVIEW_REROUTE_MAX", None)
        self.assertEqual(cli._reroute_max(), 2)

    def test_accepted_stall_hours_default(self):
        from fulcra_coord import cli
        os.environ.pop("FULCRA_COORD_ACCEPTED_STALL_HOURS", None)
        self.assertEqual(cli._accepted_stall_hours(), 2.0)


# ---------------------------------------------------------------------------
# Liveness-aware reviewer routing — Task 5: review classification
# ---------------------------------------------------------------------------


class TestReviewClassification(unittest.TestCase):
    NOW = datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc)

    def _routed_review(self, assignee, routed_minutes_ago, priority="P1", attempt=1,
                       extra_events=None, last_seen_min_ago=300):
        from fulcra_coord import routing
        routed_at = (self.NOW - timedelta(minutes=routed_minutes_ago)).isoformat(
            timespec="microseconds").replace("+00:00", "Z")
        ev = routing.make_route_event(kind="routed", to=assignee, by="s", attempt=attempt,
                                      reason="x", candidate_snapshot=[],
                                      observed_updated_at=routed_at,
                                      at=routed_at, route_id=f"r{attempt}")
        events = [{"at": routed_at, "type": "created", "by": "s"}, ev] + (extra_events or [])
        return {"id": "TASK-20260604-rev-00000000", "status": "proposed",
                "priority": priority, "assignee": assignee, "tags": ["kind:review"],
                "events": events, "updated_at": routed_at, "workstream": "fulcra-tools"}

    def _presence(self, agent, min_ago):
        ls = (self.NOW - timedelta(minutes=min_ago)).isoformat(
            timespec="microseconds").replace("+00:00", "Z")
        return [{"agent": agent, "last_seen": ls, "capabilities": ["review"]}]

    def test_never_acted_below_floor_past_p1_threshold_reroutes(self):
        from fulcra_coord import cli
        t = self._routed_review("dead:h:r", routed_minutes_ago=20, priority="P1")  # >15m
        pres = self._presence("dead:h:r", 300)  # assignee long stale -> below floor
        self.assertEqual(cli._classify_review(t, pres, self.NOW), "reroute")

    def test_before_threshold_is_none(self):
        from fulcra_coord import cli
        t = self._routed_review("dead:h:r", routed_minutes_ago=5, priority="P1")  # <15m
        self.assertEqual(cli._classify_review(t, self._presence("dead:h:r", 300), self.NOW), "none")

    def test_p2_uses_30m_threshold(self):
        from fulcra_coord import cli
        t = self._routed_review("dead:h:r", routed_minutes_ago=20, priority="P2")  # <30m
        self.assertEqual(cli._classify_review(t, self._presence("dead:h:r", 300), self.NOW), "none")

    def test_bare_inbox_ack_does_not_count_as_acceptance(self):
        from fulcra_coord import cli
        ack = {"at": (self.NOW - timedelta(minutes=18)).isoformat(
            timespec="microseconds").replace("+00:00", "Z"),
            "type": "inbox_ack", "by": "dead:h:r"}
        t = self._routed_review("dead:h:r", routed_minutes_ago=20, priority="P1",
                                extra_events=[ack])
        # a read receipt is NOT acceptance -> still eligible to reroute.
        self.assertEqual(cli._classify_review(t, self._presence("dead:h:r", 300), self.NOW),
                         "reroute")

    def test_explicit_review_accepted_freezes(self):
        from fulcra_coord import cli
        acc = {"at": (self.NOW - timedelta(minutes=18)).isoformat(
            timespec="microseconds").replace("+00:00", "Z"),
            "type": "review-accepted", "by": "dead:h:r"}
        t = self._routed_review("dead:h:r", routed_minutes_ago=20, priority="P1",
                                extra_events=[acc])
        # accepted but not yet past ACCEPTED_STALL_HOURS -> freeze.
        self.assertEqual(cli._classify_review(t, self._presence("dead:h:r", 300), self.NOW),
                         "freeze")

    def test_accepted_then_long_stall_escalates(self):
        from fulcra_coord import cli
        acc = {"at": (self.NOW - timedelta(hours=3)).isoformat(
            timespec="microseconds").replace("+00:00", "Z"),
            "type": "review-accepted", "by": "dead:h:r"}
        t = self._routed_review("dead:h:r", routed_minutes_ago=200, priority="P1",
                                extra_events=[acc])
        self.assertEqual(cli._classify_review(t, self._presence("dead:h:r", 300), self.NOW),
                         "freeze-escalate")

    def test_claim_transition_by_assignee_counts_as_acceptance(self):
        from fulcra_coord import cli
        active = {"at": (self.NOW - timedelta(minutes=18)).isoformat(
            timespec="microseconds").replace("+00:00", "Z"),
            "type": "active", "by": "dead:h:r"}  # status transition by assignee
        t = self._routed_review("dead:h:r", routed_minutes_ago=20, priority="P1",
                                extra_events=[active])
        self.assertEqual(cli._classify_review(t, self._presence("dead:h:r", 300), self.NOW),
                         "freeze")

    def test_assignee_above_floor_is_none(self):
        from fulcra_coord import cli
        t = self._routed_review("alive:h:r", routed_minutes_ago=20, priority="P1")
        self.assertEqual(cli._classify_review(t, self._presence("alive:h:r", 5), self.NOW),
                         "none")  # live, give it time

    def test_cap_reached_escalates(self):
        from fulcra_coord import cli
        t = self._routed_review("dead:h:r", routed_minutes_ago=20, priority="P1", attempt=2)
        self.assertEqual(cli._classify_review(t, self._presence("dead:h:r", 300), self.NOW),
                         "escalate")

    def test_non_review_task_is_none(self):
        from fulcra_coord import cli
        t = self._routed_review("dead:h:r", routed_minutes_ago=20, priority="P1")
        t["tags"] = ["kind:ops"]  # NOT a review directive
        self.assertEqual(cli._classify_review(t, self._presence("dead:h:r", 300), self.NOW),
                         "none")

    def test_blocked_human_escalation_is_not_rerouted_again(self):
        from fulcra_coord import cli
        t = self._routed_review("dead:h:r", routed_minutes_ago=20, priority="P1", attempt=2)
        t["status"] = "blocked"
        t["assignee"] = "redacted@users.noreply.github.com"
        t["tags"] = sorted(set(t["tags"] + ["needs:human"]))
        self.assertEqual(cli._classify_review(t, self._presence("dead:h:r", 300), self.NOW),
                         "none")


# ---------------------------------------------------------------------------
# Liveness-aware reviewer routing — Task 5: sweep I/O wrapper
# ---------------------------------------------------------------------------


class TestReviewSweep(unittest.TestCase):
    NOW = datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc)

    def _routed_review(self, assignee, routed_minutes_ago, priority="P1", attempt=1):
        from fulcra_coord import routing
        routed_at = (self.NOW - timedelta(minutes=routed_minutes_ago)).isoformat(
            timespec="microseconds").replace("+00:00", "Z")
        ev = routing.make_route_event(kind="routed", to=assignee, by="s", attempt=attempt,
                                      reason="x", candidate_snapshot=[],
                                      observed_updated_at=routed_at,
                                      at=routed_at, route_id=f"r{attempt}")
        events = [{"at": routed_at, "type": "created", "by": "s"}, ev]
        return {"id": "TASK-20260604-rev-00000000", "status": "proposed",
                "priority": priority, "assignee": assignee, "tags": ["kind:review"],
                "events": events, "updated_at": routed_at, "workstream": "fulcra-tools",
                "owner_agent": "author:h:r"}

    def _presence(self, agent, min_ago):
        ls = (self.NOW - timedelta(minutes=min_ago)).isoformat(
            timespec="microseconds").replace("+00:00", "Z")
        return [{"agent": agent, "last_seen": ls, "capabilities": ["review"]}]

    def test_sweep_reroutes_and_writes_rerouted_event(self):
        from fulcra_coord.cli import _sweep_review_routes
        t = self._routed_review("dead:h:r", routed_minutes_ago=20, priority="P1")
        agg = {"agents": self._presence("dead:h:r", 300) + [{"agent": "alive:h:r",
               "last_seen": self.NOW.isoformat(timespec="microseconds").replace("+00:00", "Z"),
               "capabilities": ["review"]}]}
        written = {}

        def fake_write(task, backend=None, command="write", lifecycle=None):
            written["task"] = task
            return True

        with patch("fulcra_coord.cli.remote.download_json", return_value=agg), \
             patch("fulcra_coord.routing_ops._cache_remote_task", return_value=t), \
             patch("fulcra_coord.routing_ops._write_task_and_views", side_effect=fake_write):
            _sweep_review_routes([t], backend=["false"], now=self.NOW)
        out = written["task"]
        rer = [e for e in out["events"] if e["type"] == "rerouted"]
        self.assertEqual(len(rer), 1)
        self.assertEqual(out["assignee"], rer[0]["to"])
        self.assertNotEqual(rer[0]["to"], "dead:h:r")  # excluded as tried
        self.assertTrue(rer[0]["route_id"])  # new route_id minted

    def test_sweep_stale_observation_aborts_when_task_moved(self):
        from fulcra_coord.cli import _sweep_review_routes
        # The re-read task's latest route event differs from the snapshot the
        # decision was computed from -> abort (no competing reroute).
        t = self._routed_review("dead:h:r", routed_minutes_ago=20, priority="P1")
        moved = self._routed_review("someoneelse:h:r", routed_minutes_ago=1,
                                    priority="P1", attempt=2)
        agg = {"agents": self._presence("dead:h:r", 300) + [{"agent": "alive:h:r",
               "last_seen": self.NOW.isoformat(timespec="microseconds").replace("+00:00", "Z"),
               "capabilities": ["review"]}]}
        with patch("fulcra_coord.cli.remote.download_json", return_value=agg), \
             patch("fulcra_coord.routing_ops._cache_remote_task",
                   return_value=moved) as load_fresh, \
             patch("fulcra_coord.routing_ops._write_task_and_views") as wtv:
            _sweep_review_routes([t], backend=["false"], now=self.NOW)
        wtv.assert_not_called()  # another sweeper already moved it
        load_fresh.assert_called_once_with(t["id"], backend=["false"])

    def test_sweep_ignores_non_review_tasks(self):
        from fulcra_coord.cli import _sweep_review_routes
        t = self._routed_review("dead:h:r", routed_minutes_ago=20, priority="P1")
        t["tags"] = ["kind:ops"]
        agg = {"agents": self._presence("dead:h:r", 300)}
        with patch("fulcra_coord.cli.remote.download_json",
                   side_effect=lambda p, backend=None: agg), \
             patch("fulcra_coord.routing_ops._write_task_and_views") as wtv, \
             patch("fulcra_coord.routing_ops._cache_remote_task") as load_fresh:
            _sweep_review_routes([t], backend=["false"], now=self.NOW)
        wtv.assert_not_called()
        load_fresh.assert_not_called()

    def test_sweep_past_deadline_processes_nothing(self):
        """B1 — the sweep is now deadline-bounded.

        _sweep_review_routes loops O(review-directives) with a per-item network
        fetch + potential full view-rebuild write and previously had NO time
        check, so it could run past the reconcile ~90s deadline and starve the
        retention pass (which DOES gate on deadline). With a deadline already in
        the past it must process nothing (no reroute write) and not raise."""
        import time
        from fulcra_coord.cli import _sweep_review_routes
        t = self._routed_review("dead:h:r", routed_minutes_ago=20, priority="P1")
        agg = {"agents": self._presence("dead:h:r", 300) + [{"agent": "alive:h:r",
               "last_seen": self.NOW.isoformat(timespec="microseconds").replace("+00:00", "Z"),
               "capabilities": ["review"]}]}
        with patch("fulcra_coord.cli.remote.download_json", return_value=agg), \
             patch("fulcra_coord.routing_ops._cache_remote_task", return_value=t), \
             patch("fulcra_coord.routing_ops._write_task_and_views") as wtv:
            # deadline already in the past -> the per-directive loop should break
            # before doing any work.
            _sweep_review_routes([t], backend=["false"], now=self.NOW,
                                 deadline=time.monotonic() - 1)
        wtv.assert_not_called()

    def test_sweep_ample_deadline_behaves_as_before(self):
        """B1 — with ample deadline the sweep still reroutes (no regression)."""
        import time
        from fulcra_coord.cli import _sweep_review_routes
        t = self._routed_review("dead:h:r", routed_minutes_ago=20, priority="P1")
        agg = {"agents": self._presence("dead:h:r", 300) + [{"agent": "alive:h:r",
               "last_seen": self.NOW.isoformat(timespec="microseconds").replace("+00:00", "Z"),
               "capabilities": ["review"]}]}
        written = {}

        def fake_write(task, backend=None, command="write", lifecycle=None):
            written["task"] = task
            return True

        with patch("fulcra_coord.cli.remote.download_json", return_value=agg), \
             patch("fulcra_coord.routing_ops._cache_remote_task", return_value=t), \
             patch("fulcra_coord.routing_ops._write_task_and_views", side_effect=fake_write):
            _sweep_review_routes([t], backend=["false"], now=self.NOW,
                                 deadline=time.monotonic() + 60)
        self.assertIn("task", written)  # reroute still happened

    def test_sweep_deadline_optional_defaults_unbounded(self):
        """B1 — deadline is optional; omitting it preserves the old behavior."""
        from fulcra_coord.cli import _sweep_review_routes
        t = self._routed_review("dead:h:r", routed_minutes_ago=20, priority="P1")
        agg = {"agents": self._presence("dead:h:r", 300) + [{"agent": "alive:h:r",
               "last_seen": self.NOW.isoformat(timespec="microseconds").replace("+00:00", "Z"),
               "capabilities": ["review"]}]}
        written = {}

        def fake_write(task, backend=None, command="write", lifecycle=None):
            written["task"] = task
            return True

        with patch("fulcra_coord.cli.remote.download_json", return_value=agg), \
             patch("fulcra_coord.routing_ops._cache_remote_task", return_value=t), \
             patch("fulcra_coord.routing_ops._write_task_and_views", side_effect=fake_write):
            _sweep_review_routes([t], backend=["false"], now=self.NOW)
        self.assertIn("task", written)


# ---------------------------------------------------------------------------
# Test-harness hermeticity — proves the autouse conftest cache isolation works
# ---------------------------------------------------------------------------


class TestCacheIsolationHermetic(unittest.TestCase):
    """Regression guard for the 'tests pollute the operator's real bus' bug.

    The conftest autouse fixture redirects XDG_CACHE_HOME to a throwaway dir for
    every test. These tests prove it: cache.cache_root() must resolve UNDER that
    temp dir, never under the operator's real ~/.cache, and a real cache write
    from inside a test must NOT land in ~/.cache/fulcra-coord. Without the
    fixture, a routing/reconcile test's fixtures leaked into the real cache and
    reconcile pushed them to the live coordination bus."""

    def test_cache_root_is_redirected_away_from_real_home(self):
        # Cache root must be under a temp XDG dir, not the developer's ~/.cache.
        root = cache.cache_root()
        real_home_cache = Path.home() / ".cache" / "fulcra-coord"
        self.assertNotEqual(
            root.resolve(), real_home_cache.resolve(),
            "cache_root() resolved to the REAL ~/.cache/fulcra-coord — the "
            "hermetic conftest fixture is not active; tests can pollute the bus.",
        )
        # And it must sit under the redirected XDG_CACHE_HOME the fixture set.
        xdg = os.environ.get("XDG_CACHE_HOME", "")
        self.assertTrue(xdg, "XDG_CACHE_HOME not set by the hermetic fixture")
        self.assertTrue(
            str(root.resolve()).startswith(str(Path(xdg).resolve())),
            f"cache_root() {root} is not under XDG_CACHE_HOME {xdg}",
        )

    def test_cache_write_does_not_touch_real_home_cache(self):
        # Snapshot the real home cache, write a task through the normal cache
        # API, and assert the write landed in the temp dir, NOT the real cache.
        real_home_cache = Path.home() / ".cache" / "fulcra-coord"
        before = set()
        if real_home_cache.exists():
            before = {p for p in real_home_cache.rglob("*") if p.is_file()}

        sentinel_id = "TASK-99999999-isolation-probe-deadbeef"
        cache.write_cached_task({
            "id": sentinel_id,
            "title": "isolation probe — must never reach the real cache",
            "status": "proposed",
        })

        # The write must be readable back through the (redirected) cache...
        self.assertIsNotNone(cache.read_cached_task(sentinel_id))
        # ...and must live under the temp XDG dir.
        written = cache.tasks_dir() / f"{sentinel_id}.json"
        self.assertTrue(written.exists())
        self.assertTrue(str(written.resolve()).startswith(
            str(Path(os.environ["XDG_CACHE_HOME"]).resolve())))

        # The real home cache must be byte-for-byte unchanged: no new files, and
        # specifically not our sentinel anywhere under it.
        after = set()
        if real_home_cache.exists():
            after = {p for p in real_home_cache.rglob("*") if p.is_file()}
        self.assertEqual(
            after - before, set(),
            "a cache write from inside a test created files in the REAL "
            "~/.cache/fulcra-coord — isolation is broken.",
        )
        leaked = list(real_home_cache.rglob(f"{sentinel_id}.json")) if \
            real_home_cache.exists() else []
        self.assertEqual(leaked, [], "sentinel task leaked into the real home cache.")


class TestBuildHealthRecord(unittest.TestCase):
    def test_record_shape_from_locals(self):
        from fulcra_coord import cli
        from datetime import datetime, timezone
        now = datetime(2026, 6, 5, 12, 0, 0, tzinfo=timezone.utc)
        rec = cli._build_health_record(
            now=now, duration_s=1.5, tasks_loaded=5, views_refreshed=7,
            repair_backlog=2, retention_last_run="2026-06-05",
            listener_last_fire=None, bus_task_count=5)
        self.assertEqual(rec["schema"], "fulcra.coordination.health.v1")
        self.assertEqual(rec["tasks_loaded"], 5)
        self.assertEqual(rec["views_refreshed"], 7)
        self.assertEqual(rec["repair_backlog"], 2)
        self.assertEqual(rec["bus_task_count"], 5)
        self.assertTrue(rec["reconcile_at"].endswith("Z"))
        self.assertIn("host", rec)
        self.assertIn("agent", rec)
        self.assertIn("version", rec)


class TestReconcileHealthWrite(unittest.TestCase):
    """The success contract: health/<slug>.json is written on the failures==[]
    path, NOT when a view upload fails (return 1 first), and NOT suppressed by a
    raising best-effort sub-pass."""

    def setUp(self):
        import types
        self.types = types

    def _run_capturing_uploads(self, upload_side_effect):
        from fulcra_coord.cli import cmd_reconcile
        uploaded = []

        def _capture(data, path, **kw):
            uploaded.append(path)
            return upload_side_effect(data, path, **kw)

        # probe_reachable=True (F3 guard): a reachable bus with no index yet —
        # all-None reads on an UNREACHABLE bus now correctly skip the view
        # phase (degraded) and would never reach the health write under test.
        with patch("fulcra_coord.cli.remote.upload_json", side_effect=_capture), \
             patch("fulcra_coord.cli.remote.download_json", return_value=None), \
             patch("fulcra_coord.cli.remote.probe_reachable", return_value=True), \
             patch("fulcra_coord.cli.remote.list_files", return_value=[]):
            rc = cmd_reconcile(self.types.SimpleNamespace(), backend=["false"])
        return rc, uploaded

    def test_health_written_on_success(self):
        rc, uploaded = self._run_capturing_uploads(lambda *a, **k: True)
        self.assertEqual(rc, 0)
        self.assertTrue(any("/health/" in p for p in uploaded),
                        f"health record must be uploaded on success; got {uploaded}")

    def test_health_not_written_when_view_upload_fails(self):
        # Views fail -> failures != [] -> return 1 BEFORE the health write.
        def _side(data, path, **kw):
            return "/health/" not in path and False  # all uploads fail
        rc, uploaded = self._run_capturing_uploads(_side)
        self.assertEqual(rc, 1)
        self.assertFalse(any("/health/" in p for p in uploaded),
                         "a failing reconcile must NOT write a fresh health record")

    def test_health_write_failure_does_not_fail_the_tick(self):
        # Views succeed; the health upload itself raises -> still return 0.
        from fulcra_coord.cli import cmd_reconcile

        def _side(data, path, **kw):
            if "/health/" in path:
                raise RuntimeError("boom")
            return True

        with patch("fulcra_coord.cli.remote.upload_json", side_effect=_side), \
             patch("fulcra_coord.cli.remote.download_json", return_value=None), \
             patch("fulcra_coord.cli.remote.probe_reachable", return_value=True), \
             patch("fulcra_coord.cli.remote.list_files", return_value=[]):
            rc = cmd_reconcile(self.types.SimpleNamespace(), backend=["false"])
        self.assertEqual(rc, 0, "a health-write failure must never fail the tick")


class TestUnroutedPrReviews(unittest.TestCase):
    """views.unrouted_pr_reviews — catch PRs an author forgot to request-review.

    Regression guard for the PR #101 class: a review left as a free-text
    next_action (never routed via request-review) reaches no reviewer. The owner
    must see it on their resume so they route it.
    """
    ME = "claude-code:ArcBot:Arc-Code-Review"

    def _tasks(self):
        return [
            # owned by me, mentions a PR, not a routed review -> FLAG
            {"id": "T1", "owner_agent": self.ME, "status": "active", "tags": [],
             "title": "Demo continuity snapshots", "workstream": "ashfulcra/fulcra-tools",
             "next_action": "Review and merge PR #101, then propagate", "priority": "P2"},
            # already a routed kind:review directive -> NOT flagged
            {"id": "T2", "owner_agent": self.ME, "status": "active",
             "tags": ["kind:review"], "workstream": "ashfulcra/fulcra-tools",
             "title": "Review PR #102 — assume bugs", "priority": "P1"},
            # PR mention but owned by someone else -> NOT flagged (not my plate)
            {"id": "T3", "owner_agent": "openclaw:discord:main-comms", "status": "active",
             "tags": [], "title": "x", "next_action": "see PR #103", "workstream": "r"},
            # closed task -> NOT flagged
            {"id": "T4", "owner_agent": self.ME, "status": "done", "tags": [],
             "title": "x", "next_action": "PR #104 merged", "workstream": "r"},
            # bare "#105" with no PR/pull context -> NOT flagged (avoid false positives)
            {"id": "T5", "owner_agent": self.ME, "status": "active", "tags": [],
             "title": "fix issue #105", "workstream": "r"},
        ]

    def test_flags_only_unrouted_owned_open_pr_mentions(self):
        from fulcra_coord import views
        out = views.unrouted_pr_reviews(self._tasks(), self.ME)
        self.assertEqual([t["id"] for t in out], ["T1"])
        self.assertEqual(out[0]["pr_mentions"], ["101"])

    def test_matches_pull_url_and_dedupes(self):
        from fulcra_coord import views
        tasks = [{"id": "U1", "owner_agent": self.ME, "status": "waiting", "tags": [],
                  "title": "ship it",
                  "current_summary": "opened https://github.com/o/r/pull/77 ; PR #77 awaits review",
                  "workstream": "o/r"}]
        out = views.unrouted_pr_reviews(tasks, self.ME)
        self.assertEqual(out[0]["pr_mentions"], ["77"])

    def test_resume_json_surfaces_unrouted_pr_reviews(self):
        from fulcra_coord.cli import cmd_resume
        import io, contextlib
        from unittest.mock import patch
        with patch("fulcra_coord.query._load_task_summaries", return_value=self._tasks()), \
             patch("fulcra_coord.query.identity.resolve_agent", return_value=self.ME), \
             patch("fulcra_coord.query.identity.resolve_human", return_value="ash"), \
             patch("fulcra_coord.query.remote.download_json", return_value=None):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cmd_resume(types.SimpleNamespace(agent=self.ME, format="json"),
                                backend=["false"])
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual([t["id"] for t in data["unrouted_pr_reviews"]], ["T1"])

    def test_resume_table_prints_valid_request_review_command(self):
        from fulcra_coord.cli import cmd_resume
        import io, contextlib
        from unittest.mock import patch
        with patch("fulcra_coord.query._load_task_summaries", return_value=self._tasks()), \
             patch("fulcra_coord.query.identity.resolve_agent", return_value=self.ME), \
             patch("fulcra_coord.query.identity.resolve_human", return_value="ash"), \
             patch("fulcra_coord.query.remote.download_json", return_value=None):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = cmd_resume(types.SimpleNamespace(agent=self.ME,
                                                      format="table",
                                                      with_continuity=False),
                                backend=["false"])
        self.assertEqual(rc, 0)
        text = buf.getvalue()
        self.assertIn("fulcra-coord request-review 101 --repo ashfulcra/fulcra-tools",
                      text)
        self.assertNotIn("--pr 101", text)


# ---------------------------------------------------------------------------
# Layering: loops.py / loop_ops.py must not import any up-layer module.
# ---------------------------------------------------------------------------

def _first_party_imports(module_filename: str) -> set:
    """The set of fulcra_coord sibling-module names a package module imports.

    AST scan (never imports the module under test, so a layering violation
    can't crash the scanner) covering both relative (``from . import x`` /
    ``from .x import y``) and absolute (``import fulcra_coord.x`` /
    ``from fulcra_coord.x import y``) forms. Same idiom as
    test_directive_dualwrite.py's TestDirectivesLayering pin for directives.py.
    """
    import ast
    pkg = Path(__file__).resolve().parents[1] / "fulcra_coord"
    src = (pkg / module_filename).read_text(encoding="utf-8")
    imported: set = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom):
            if (node.level or 0) >= 1:
                if node.module:
                    imported.add(node.module.split(".")[0])
                else:
                    for a in node.names:
                        imported.add(a.name.split(".")[0])
            elif (node.module or "").split(".")[0] == "fulcra_coord":
                parts = node.module.split(".")
                if len(parts) >= 2:
                    imported.add(parts[1])
                else:
                    for a in node.names:
                        imported.add(a.name.split(".")[0])
        elif isinstance(node, ast.Import):
            for a in node.names:
                parts = a.name.split(".")
                if parts[0] == "fulcra_coord" and len(parts) >= 2:
                    imported.add(parts[1])
    return imported


class TestLoopsLayering(unittest.TestCase):

    def test_loops_imports_no_up_layer_module(self):
        # loops.py is the PURE lifecycle layer: schema + stdlib only. An import
        # of remote/cli/views/lifecycle/inbox/writepipe/listener here would let
        # I/O leak into the reducer — the exact creep the spec forbids.
        forbidden = {"remote", "cli", "views", "lifecycle", "inbox",
                     "writepipe", "routing_ops", "listener", "loop_ops",
                     "directives"}
        offenders = _first_party_imports("loops.py") & forbidden
        self.assertEqual(offenders, set(),
                         f"loops.py imports up-layer modules: {offenders}")

    def test_loop_ops_imports_no_up_layer_module(self):
        # loop_ops.py is the thin I/O layer over loops.py (the return-leg
        # writer): it may reach DOWN (schema/remote/loops/log/output and
        # directives/identity), but an import of cli/views/lifecycle/inbox/
        # writepipe/routing_ops/listener/query/presence would couple the
        # closed-loop write path to command/rendering layers — the same creep
        # the directives.py pin forbids.
        forbidden = {"cli", "views", "lifecycle", "inbox", "writepipe",
                     "routing_ops", "listener", "query", "presence"}
        offenders = _first_party_imports("loop_ops.py") & forbidden
        self.assertEqual(offenders, set(),
                         f"loop_ops.py imports up-layer modules: {offenders}")

    def test_no_core_module_imports_forge_mirror(self):
        # REVERSE fitness pin (phase 2): forge_mirror.py is the ONE sanctioned
        # forge poller — a PRODUCTION-side bridge that may import core, but
        # core may NEVER import it. If any core module reached into the
        # mirror, forge polling would creep back into the coordination layer
        # (the exact ad-hoc-poller disease the bridge exists to centralize),
        # and closure logic could grow a forge dependency. Scan direction is
        # inverted vs the pins above: each core module's import set must not
        # contain forge_mirror.
        core = ["cli.py", "views.py", "loops.py", "loop_ops.py", "inbox.py",
                "query.py", "presence.py", "lifecycle.py", "listener.py",
                "routing_ops.py", "directives.py", "writepipe.py",
                "schema.py", "remote.py"]
        offenders = sorted(
            m for m in core if "forge_mirror" in _first_party_imports(m))
        self.assertEqual(offenders, [],
                         f"core module(s) import forge_mirror: {offenders}")


# ---------------------------------------------------------------------------
# Coordination-loop health (spec 2026-06-09 Task 5): reconcile's report-only
# `_loop_health_check` sub-pass + the `status` surface. pytest-style functions
# (not TestCase) because they use the coord_backend fixture, mirroring the
# test_directive_parity.py / test_undelivered_directive.py idiom.
# ---------------------------------------------------------------------------


def _seed_open_review_loop(backend, *, opener="me:h:r", audience="rev:h:r",
                           hours_old=48):
    """An OPEN kind:review loop opened `hours_old` hours ago (sla 24 -> overdue
    when hours_old > 24), uploaded as a top-level directive record."""
    from fulcra_coord import remote
    d = schema.make_directive(
        directive_type="review", from_agent=opener, audience=audience,
        title="review PR 9", workstream="general",
        kind="review", state="requested", expects_response=True, sla_hours=24,
    )
    d["created_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=hours_old)
    ).isoformat(timespec="microseconds").replace("+00:00", "Z")
    assert remote.upload_json(d, remote.directive_remote_path(d["id"]),
                              backend=backend)
    return d


def test_loop_health_check_counts_overdue(coord_backend):
    """One open review loop opened by ME, 48h old against a 24h SLA -> the
    report counts it open AND overdue (awaiting_others side; nothing awaits me)."""
    from fulcra_coord import cli
    prev = os.environ.get("FULCRA_COORD_AGENT")
    os.environ["FULCRA_COORD_AGENT"] = "me:h:r"
    try:
        _seed_open_review_loop(coord_backend, opener="me:h:r")
        report = cli._loop_health_check(backend=coord_backend)
    finally:
        if prev is None:
            os.environ.pop("FULCRA_COORD_AGENT", None)
        else:
            os.environ["FULCRA_COORD_AGENT"] = prev
    assert report["overdue"] == 1
    assert report["open_loops"] == 1
    assert report["awaiting_me"] == 0


def test_loop_health_check_ignores_sublog_shards(coord_backend):
    """The check enumerates TOP-LEVEL directive records only — a response shard
    under directives/<id>/responses/ must never be counted as a loop record
    (same load-bearing filter as _directive_parity_check)."""
    from fulcra_coord import cli, loop_ops
    prev = os.environ.get("FULCRA_COORD_AGENT")
    os.environ["FULCRA_COORD_AGENT"] = "me:h:r"
    try:
        _seed_open_review_loop(coord_backend, opener="me:h:r")
        # A response shard from someone ELSE's loop bookkeeping lands under the
        # same prefix; it must not inflate (or close) anything by mere presence.
        assert loop_ops.append_loop_response(
            "DIR-OTHER", {"by": "x:h:r", "outcome": {"verdict": "done"}},
            backend=coord_backend)
        report = cli._loop_health_check(backend=coord_backend)
    finally:
        if prev is None:
            os.environ.pop("FULCRA_COORD_AGENT", None)
        else:
            os.environ["FULCRA_COORD_AGENT"] = prev
    assert report["open_loops"] == 1
    assert report["overdue"] == 1


def test_loop_health_check_counts_out_of_band(coord_backend):
    """Phase 2: a loop I opened whose evidence sub-log is nonempty counts as
    out_of_band — and the evidence NEVER changes the open/overdue accounting
    (mirrored signals are detection-only; closure stays bus-response-only)."""
    from fulcra_coord import cli, loop_ops
    prev = os.environ.get("FULCRA_COORD_AGENT")
    os.environ["FULCRA_COORD_AGENT"] = "me:h:r"
    try:
        flagged = _seed_open_review_loop(coord_backend, opener="me:h:r")
        _seed_open_review_loop(coord_backend, opener="me:h:r", hours_old=1)
        assert loop_ops.append_loop_evidence(
            flagged["id"],
            {"forge": "github", "kind": "comment-verdict",
             "summary": "approved on the forge"},
            backend=coord_backend)
        report = cli._loop_health_check(backend=coord_backend)
    finally:
        if prev is None:
            os.environ.pop("FULCRA_COORD_AGENT", None)
        else:
            os.environ["FULCRA_COORD_AGENT"] = prev
    assert report["out_of_band"] == 1
    # Evidence is invisible to the open/overdue fold: both loops still open,
    # the 48h one still overdue.
    assert report["open_loops"] == 2
    assert report["overdue"] == 1
    assert report["awaiting_me"] == 0


def test_reconcile_populates_loop_health(coord_backend):
    """A reconcile records the loop-health report beside event_parity in the
    per-host health record — and a check exception NEVER changes the exit code."""
    from unittest import mock as _mock
    from fulcra_coord import cli, remote, views as _views, identity as _identity
    prev = os.environ.get("FULCRA_COORD_AGENT")
    os.environ["FULCRA_COORD_AGENT"] = "me:h:r"
    try:
        _seed_open_review_loop(coord_backend, opener="me:h:r")
        rc = cli.cmd_reconcile(types.SimpleNamespace(), backend=coord_backend)
        assert rc == 0
        slug = _views.agent_slug(
            cli._build_health_record(
                now=datetime.now(timezone.utc), duration_s=0.0, tasks_loaded=0,
                views_refreshed=0, repair_backlog=0, retention_last_run=None,
                listener_last_fire=None, bus_task_count=0,
            ).get("host") or _identity.resolve_agent())
        rec = remote.download_json(remote.health_remote_path(slug),
                                   backend=coord_backend)
        assert isinstance(rec, dict)
        lh = rec.get("loop_health")
        assert isinstance(lh, dict)
        assert lh["overdue"] == 1
        # Failure isolation: a raising check never changes reconcile's exit code.
        with _mock.patch.object(cli, "_loop_health_check",
                                side_effect=RuntimeError("boom")):
            assert cli.cmd_reconcile(types.SimpleNamespace(),
                                     backend=coord_backend) == 0
    finally:
        if prev is None:
            os.environ.pop("FULCRA_COORD_AGENT", None)
        else:
            os.environ["FULCRA_COORD_AGENT"] = prev


def test_status_warns_on_overdue_loops(coord_backend, capsys):
    """`status` surfaces the loop line from the persisted health surface (the
    same direction the undelivered warning reads — query.py never imports cli)."""
    from fulcra_coord import query, remote
    current_agent = "me:h:r"
    prev = os.environ.get("FULCRA_COORD_AGENT")
    os.environ["FULCRA_COORD_AGENT"] = current_agent
    remote.upload_json(
        {"host": "host-a", "agent": current_agent,
         "loop_health": {"open_loops": 2, "overdue": 1, "awaiting_me": 1}},
        remote.health_remote_path("host-a"), backend=coord_backend,
    )
    try:
        rc = query.cmd_status(types.SimpleNamespace(workstream=None, agent=None,
                                                    format="table"),
                              backend=coord_backend)
    finally:
        if prev is None:
            os.environ.pop("FULCRA_COORD_AGENT", None)
        else:
            os.environ["FULCRA_COORD_AGENT"] = prev
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 coordination loop(s) overdue" in out
    assert "1 awaiting you" in out


def test_status_awaiting_you_uses_current_agent_health_only(coord_backend, capsys):
    """Overdue is bus-wide, but awaiting_me is identity-specific. Do not label
    another host/agent's awaiting_me count as "awaiting you"."""
    from fulcra_coord import query, remote
    prev = os.environ.get("FULCRA_COORD_AGENT")
    os.environ["FULCRA_COORD_AGENT"] = "me:h:r"
    try:
        remote.upload_json(
            {"host": "host-a", "agent": "other:h:r",
             "loop_health": {"open_loops": 9, "overdue": 2, "awaiting_me": 9}},
            remote.health_remote_path("host-a"), backend=coord_backend,
        )
        remote.upload_json(
            {"host": "host-b", "agent": "me:h:r",
             "loop_health": {"open_loops": 1, "overdue": 0, "awaiting_me": 1}},
            remote.health_remote_path("host-b"), backend=coord_backend,
        )
        rc = query.cmd_status(types.SimpleNamespace(workstream=None, agent=None,
                                                    format="table"),
                              backend=coord_backend)
    finally:
        if prev is None:
            os.environ.pop("FULCRA_COORD_AGENT", None)
        else:
            os.environ["FULCRA_COORD_AGENT"] = prev
    assert rc == 0
    out = capsys.readouterr().out
    assert "2 coordination loop(s) overdue" in out
    assert "1 awaiting you" in out
    assert "9 awaiting you" not in out


def test_status_renders_out_of_band_count(coord_backend, capsys):
    """Phase 2: the loop warning appends ` · K out-of-band` when K>0 — sourced
    from the CURRENT agent's health record only (out_of_band is the requester's
    own awaiting_others signal, same identity keying as awaiting_me — the
    a6d79f95 cross-agent-leak fix applies here too)."""
    from fulcra_coord import query, remote
    prev = os.environ.get("FULCRA_COORD_AGENT")
    os.environ["FULCRA_COORD_AGENT"] = "me:h:r"
    try:
        remote.upload_json(
            {"host": "host-a", "agent": "other:h:r",
             "loop_health": {"open_loops": 9, "overdue": 2, "awaiting_me": 9,
                             "out_of_band": 9}},
            remote.health_remote_path("host-a"), backend=coord_backend,
        )
        remote.upload_json(
            {"host": "host-b", "agent": "me:h:r",
             "loop_health": {"open_loops": 3, "overdue": 1, "awaiting_me": 1,
                             "out_of_band": 2}},
            remote.health_remote_path("host-b"), backend=coord_backend,
        )
        rc = query.cmd_status(types.SimpleNamespace(workstream=None, agent=None,
                                                    format="table"),
                              backend=coord_backend)
    finally:
        if prev is None:
            os.environ.pop("FULCRA_COORD_AGENT", None)
        else:
            os.environ["FULCRA_COORD_AGENT"] = prev
    assert rc == 0
    out = capsys.readouterr().out
    assert "· 2 out-of-band" in out
    assert "9 out-of-band" not in out


def test_status_omits_out_of_band_when_zero(coord_backend, capsys):
    """The ` · K out-of-band` suffix renders ONLY when K>0 — a zero count adds
    nothing to the existing warning line."""
    from fulcra_coord import query, remote
    prev = os.environ.get("FULCRA_COORD_AGENT")
    os.environ["FULCRA_COORD_AGENT"] = "me:h:r"
    try:
        remote.upload_json(
            {"host": "host-a", "agent": "me:h:r",
             "loop_health": {"open_loops": 2, "overdue": 1, "awaiting_me": 1,
                             "out_of_band": 0}},
            remote.health_remote_path("host-a"), backend=coord_backend,
        )
        rc = query.cmd_status(types.SimpleNamespace(workstream=None, agent=None,
                                                    format="table"),
                              backend=coord_backend)
    finally:
        if prev is None:
            os.environ.pop("FULCRA_COORD_AGENT", None)
        else:
            os.environ["FULCRA_COORD_AGENT"] = prev
    assert rc == 0
    out = capsys.readouterr().out
    assert "1 coordination loop(s) overdue" in out
    assert "out-of-band" not in out


def test_status_no_loop_warning_when_clean(coord_backend, capsys):
    """status prints NO loop line when every host reports zero overdue/awaiting."""
    from fulcra_coord import query, remote
    remote.upload_json(
        {"host": "host-a",
         "loop_health": {"open_loops": 0, "overdue": 0, "awaiting_me": 0}},
        remote.health_remote_path("host-a"), backend=coord_backend,
    )
    rc = query.cmd_status(types.SimpleNamespace(workstream=None, agent=None,
                                                format="table"),
                          backend=coord_backend)
    assert rc == 0
    out = capsys.readouterr().out
    assert "coordination loop" not in out


# ---------------------------------------------------------------------------
# Board rendering (spec step 7, phase 2 Task 3): the `board` command — the
# operator rendering of the SAME pure loops.loop_board fold the health record
# uses. pytest-style functions on coord_backend, mirroring the loop-health
# tests above (same seeding idiom, same env handling).
# ---------------------------------------------------------------------------


def _seed_idea_loop(backend, *, state="viable", opener="someone:h:r"):
    """An idea-kind record in a non-terminal pipeline state, uploaded as a
    top-level directive record (ideas count in the board's pipeline section)."""
    from fulcra_coord import remote
    d = schema.make_directive(
        directive_type="tell", from_agent=opener, audience="x:h:r",
        title="an idea", workstream="general", kind="idea", state=state,
    )
    assert remote.upload_json(d, remote.directive_remote_path(d["id"]),
                              backend=backend)
    return d


def _seed_board(backend):
    """The canonical four-corner board for me:h:r — one loop awaiting me, one
    of mine overdue, one of mine with mirrored evidence (out-of-band), one
    idea in `viable`. Returns the seeded records keyed by role."""
    from fulcra_coord import loop_ops
    awaiting_me = _seed_open_review_loop(
        backend, opener="other:h:r", audience="me:h:r", hours_old=1)
    overdue = _seed_open_review_loop(
        backend, opener="me:h:r", audience="rev:h:r", hours_old=48)
    evidenced = _seed_open_review_loop(
        backend, opener="me:h:r", audience="rev:h:r", hours_old=1)
    assert loop_ops.append_loop_evidence(
        evidenced["id"],
        {"forge": "github", "kind": "comment-verdict",
         "summary": "approved on the forge"},
        backend=backend)
    idea = _seed_idea_loop(backend)
    return {"awaiting_me": awaiting_me, "overdue": overdue,
            "evidenced": evidenced, "idea": idea}


def test_board_json_renders_all_four_sections(coord_backend, capsys):
    """`board --format json` prints the raw loop_board dict: awaiting_me /
    awaiting_others (overdue + out_of_band flags set) / in_flight_by_kind /
    ideas_pipeline — evidence flags out-of-band, never closes (both of my
    asks stay listed open)."""
    from fulcra_coord import query
    prev = os.environ.get("FULCRA_COORD_AGENT")
    os.environ["FULCRA_COORD_AGENT"] = "me:h:r"
    try:
        seeded = _seed_board(coord_backend)
        rc = query.cmd_board(types.SimpleNamespace(agent=None, format="json"),
                             backend=coord_backend)
    finally:
        if prev is None:
            os.environ.pop("FULCRA_COORD_AGENT", None)
        else:
            os.environ["FULCRA_COORD_AGENT"] = prev
    assert rc == 0
    board = json.loads(capsys.readouterr().out)
    assert set(board) >= {"awaiting_me", "awaiting_others",
                          "in_flight_by_kind", "ideas_pipeline"}
    assert [s["id"] for s in board["awaiting_me"]] == [seeded["awaiting_me"]["id"]]
    by_id = {s["id"]: s for s in board["awaiting_others"]}
    assert set(by_id) == {seeded["overdue"]["id"], seeded["evidenced"]["id"]}
    assert by_id[seeded["overdue"]["id"]]["overdue"] is True
    assert by_id[seeded["overdue"]["id"]]["out_of_band"] is False
    assert by_id[seeded["evidenced"]["id"]]["out_of_band"] is True
    assert by_id[seeded["evidenced"]["id"]]["overdue"] is False
    # All three review loops are still OPEN (evidence never closes a loop).
    assert board["in_flight_by_kind"] == {"review": 3}
    assert board["ideas_pipeline"] == {"viable": 1}


def test_board_table_renders_flags_and_sections(coord_backend, capsys):
    """The table view shows all four section headers and the ⚠ overdue /
    ◈ out-of-band trailing flags on the awaiting-others lines."""
    from fulcra_coord import query
    prev = os.environ.get("FULCRA_COORD_AGENT")
    os.environ["FULCRA_COORD_AGENT"] = "me:h:r"
    try:
        _seed_board(coord_backend)
        rc = query.cmd_board(types.SimpleNamespace(agent=None, format="table"),
                             backend=coord_backend)
    finally:
        if prev is None:
            os.environ.pop("FULCRA_COORD_AGENT", None)
        else:
            os.environ["FULCRA_COORD_AGENT"] = prev
    assert rc == 0
    out = capsys.readouterr().out
    assert "Awaiting me (1)" in out
    assert "Awaiting others (2)" in out
    assert "In flight by kind" in out
    assert "Ideas pipeline" in out
    assert "⚠ overdue" in out
    assert "◈ out-of-band" in out
    assert "review: 3" in out
    assert "viable: 1" in out


def test_board_empty_bus_renders_cleanly(coord_backend, capsys):
    """A bus with no loop records renders the clean empty message — rc 0,
    never a stack trace (board is a read surface)."""
    from fulcra_coord import query
    rc = query.cmd_board(types.SimpleNamespace(agent=None, format="table"),
                         backend=coord_backend)
    assert rc == 0
    out = capsys.readouterr().out
    assert "(no open coordination loops)" in out


def test_board_is_wired_into_map():
    """`board` dispatches like every other query command: parser registered
    with --agent/--format, COMMAND_MAP points at the cli re-export."""
    from fulcra_coord import cli, entry
    assert entry.COMMAND_MAP["board"] is cli.cmd_board
    args = entry.build_parser().parse_args(["board"])
    assert args.format == "table"
    assert args.agent is None


# ---------------------------------------------------------------------------
# Role vacancy surfacing (spec 2026-06-10): reconcile's report-only
# `_role_health_check` sub-pass + the board's Roles section. Vacancy is the
# new dark-agent signal — not "agent X is dark" but "FUNCTION X is unstaffed".
# Same pytest-on-coord_backend idiom as the loop-health/board tests above.
# ---------------------------------------------------------------------------


def _seed_role(backend, name, *, policy="shared", sla_hours=None,
               maintainer=None, created_hours_ago=100.0):
    from fulcra_coord import role_ops
    r = schema.make_role(name, f"the {name} role", policy=policy,
                         sla_hours=sla_hours, maintainer=maintainer)
    r["created_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=created_hours_ago)
    ).isoformat(timespec="microseconds").replace("+00:00", "Z")
    assert role_ops.upsert_role(r, backend=backend)
    return r


def _seed_lease(backend, role_name, agent, *, hours_old=0.0):
    """Write a lease shard directly (claim_role stamps `at`=now; aging a lease
    needs a hand-written shard) — same path claim_role writes."""
    from fulcra_coord import remote
    at = (datetime.now(timezone.utc) - timedelta(hours=hours_old)
          ).isoformat(timespec="microseconds").replace("+00:00", "Z")
    lease = {"schema": schema.ROLE_LEASE_SCHEMA, "role": role_name,
             "agent": agent, "at": at}
    assert remote.upload_json(lease, remote.role_lease_path(role_name, agent),
                              backend=backend)
    return lease


def _seed_presence(backend, agent, *, hours_old=0.0):
    """Durable per-agent presence + aggregate, via the production writer so
    the staleness-guarded roster read sees exactly what connect writes."""
    from fulcra_coord import presence
    last_seen = (datetime.now(timezone.utc) - timedelta(hours=hours_old)
                 ).isoformat(timespec="microseconds").replace("+00:00", "Z")
    rec = schema.make_presence(agent, workstreams=[], summary="",
                               last_seen=last_seen)
    presence._write_presence(rec, backend=backend)
    return rec


def test_role_health_check_reports_held_vacant_contested(coord_backend):
    from fulcra_coord import cli
    _seed_role(coord_backend, "held-role")
    _seed_lease(coord_backend, "held-role", "live:h:r")
    _seed_role(coord_backend, "vacant-role")
    _seed_lease(coord_backend, "vacant-role", "dead:h:r")
    _seed_role(coord_backend, "contested-role", policy="exclusive")
    _seed_lease(coord_backend, "contested-role", "live:h:r")
    _seed_lease(coord_backend, "contested-role", "live2:h:r")
    _seed_presence(coord_backend, "live:h:r")
    _seed_presence(coord_backend, "live2:h:r")
    _seed_presence(coord_backend, "dead:h:r", hours_old=9)

    report = cli._role_health_check(backend=coord_backend)

    by_name = {r["name"]: r for r in report["roles"]}
    assert by_name["held-role"]["vacant"] is False
    assert [h["agent"] for h in by_name["held-role"]["holders"]] == ["live:h:r"]
    assert by_name["vacant-role"]["vacant"] is True
    assert by_name["vacant-role"]["vacant_since"]
    assert by_name["contested-role"]["contested"] is True
    assert report["vacant"] == 1
    assert report["contested"] == 1


def test_role_health_check_empty_registry_is_quiet(coord_backend):
    from fulcra_coord import cli
    report = cli._role_health_check(backend=coord_backend)
    assert report == {"roles": [], "vacant": 0, "contested": 0, "escalated": 0,
                      "unknown": 0}


def test_role_health_uses_the_staleness_guarded_presence_read(coord_backend):
    """Lease freshness MUST ride the staleness-guarded roster read (post-#147)
    — _load_presence_agents — or vacancy detection inherits the stale-view
    blindness (a live holder reads dead because the aggregate lagged)."""
    from unittest import mock
    from fulcra_coord import cli
    _seed_role(coord_backend, "guarded-role")
    _seed_lease(coord_backend, "guarded-role", "live:h:r")
    fresh = (datetime.now(timezone.utc)).isoformat(
        timespec="microseconds").replace("+00:00", "Z")
    with mock.patch.object(
        cli, "_load_presence_agents",
        return_value=[{"agent": "live:h:r", "last_seen": fresh}],
    ) as guarded:
        report = cli._role_health_check(backend=coord_backend)
    assert guarded.called, "role health must read presence via the guarded loader"
    by_name = {r["name"]: r for r in report["roles"]}
    assert by_name["guarded-role"]["vacant"] is False


def test_role_health_missing_presence_aggregate_uses_durable_records(coord_backend):
    """A missing presence aggregate must not make every leased role VACANT.

    The durable per-agent presence records are the truth. If a partial
    connect/reconcile uploaded presence/<agent>.json but failed to upload
    views/presence.json, role health still has to see the live holder or it can
    falsely escalate vacancy."""
    from fulcra_coord import cli, remote
    _seed_role(coord_backend, "aggregate-missing-role")
    _seed_lease(coord_backend, "aggregate-missing-role", "live:h:r")
    rec = schema.make_presence("live:h:r")
    assert remote.upload_json(
        rec, remote.presence_remote_path("live-h-r"), backend=coord_backend)

    report = cli._role_health_check(backend=coord_backend)

    by_name = {r["name"]: r for r in report["roles"]}
    assert by_name["aggregate-missing-role"]["vacant"] is False
    assert [h["agent"] for h in by_name["aggregate-missing-role"]["holders"]] == [
        "live:h:r"
    ]


def test_role_vacancy_past_sla_escalates_to_maintainer(coord_backend):
    """A role vacant past sla_hours emits ONE escalation directive addressed
    to the role's maintainer (not the operator's plate) — and re-running the
    check the same day does NOT emit a second one (daily marker idempotence:
    once per vacancy-day, not once per reconcile tick)."""
    from fulcra_coord import cli, loop_ops
    _seed_role(coord_backend, "stale-role", sla_hours=24,
               maintainer="ops:h:r")
    _seed_lease(coord_backend, "stale-role", "dead:h:r", hours_old=30)
    # dead:h:r has no presence at all -> lease lapsed -> vacant since 30h ago.

    report1 = cli._role_health_check(backend=coord_backend)
    report2 = cli._role_health_check(backend=coord_backend)   # same day re-run

    assert report1["escalated"] == 1
    assert report2["escalated"] == 0    # marker claimed — once per day
    records = loop_ops.load_loop_records(backend=coord_backend)
    escalations = [r for r in records if r.get("audience") == "ops:h:r"]
    assert len(escalations) == 1
    assert "stale-role" in escalations[0].get("title", "")


def test_role_vacancy_escalation_reaches_the_maintainers_inbox(coord_backend):
    """2026-06-11 bug hunt C3 (P1): the escalation used to upload ONLY a
    first-class directives/<id>.json record — but nothing delivery-side reads
    that prefix for correctness yet (the inbox, the listener, SessionStart all
    fold over the legacy TASK set). The maintainer was never actually told;
    the vacancy alert was write-only. The fix routes the escalation through
    the legacy task path like every other directive creator (a proposed task
    assigned to the maintainer via the write pipeline) WITH the standard
    Phase-3b dual-write mirror — so it lands where the maintainer looks."""
    from fulcra_coord import cli, inbox
    _seed_role(coord_backend, "stale-role", sla_hours=24,
               maintainer="ops:h:r")
    _seed_lease(coord_backend, "stale-role", "dead:h:r", hours_old=30)

    report = cli._role_health_check(backend=coord_backend)
    assert report["escalated"] == 1

    items = inbox._load_inbox("ops:h:r", backend=coord_backend)
    assert any("stale-role" in (i.get("title") or "") for i in items), (
        f"escalation never reached the maintainer's inbox; inbox={items}")


def test_role_vacancy_within_sla_does_not_escalate(coord_backend):
    from fulcra_coord import cli, loop_ops
    _seed_role(coord_backend, "fresh-vacancy", sla_hours=24,
               maintainer="ops:h:r")
    _seed_lease(coord_backend, "fresh-vacancy", "dead:h:r", hours_old=2)
    report = cli._role_health_check(backend=coord_backend)
    assert report["escalated"] == 0
    assert loop_ops.load_loop_records(backend=coord_backend) == []


def test_role_vacancy_without_maintainer_never_escalates(coord_backend):
    # No maintainer = no escalation edge: the vacancy still counts/renders,
    # but there is nowhere to route a directive (and nothing to spam).
    from fulcra_coord import cli, loop_ops
    _seed_role(coord_backend, "orphan-role", sla_hours=24)
    _seed_lease(coord_backend, "orphan-role", "dead:h:r", hours_old=300)
    report = cli._role_health_check(backend=coord_backend)
    assert report["escalated"] == 0
    assert report["vacant"] == 1
    assert loop_ops.load_loop_records(backend=coord_backend) == []


def test_reconcile_populates_role_health(coord_backend):
    """The reconcile tick folds the role-health block into the per-host health
    record, mirroring loop_health (report-only, never changes the exit code)."""
    from unittest import mock
    from fulcra_coord import cli, remote
    _seed_role(coord_backend, "some-role")
    captured = {}
    real_upload = remote.upload_json

    def _capture(record, path, **kwargs):
        if "/health/" in path and isinstance(record, dict):
            captured["record"] = record
        return real_upload(record, path, **kwargs)

    with mock.patch.object(remote, "upload_json", side_effect=_capture):
        rc = cli.cmd_reconcile(mock.Mock(), backend=coord_backend)
    assert rc == 0
    assert "record" in captured, "no health record was uploaded"
    assert "role_health" in captured["record"]
    assert captured["record"]["role_health"]["vacant"] == 1


def test_reconcile_exit_code_unaffected_by_role_health_exception(coord_backend):
    from unittest import mock
    from fulcra_coord import cli
    with mock.patch.object(cli, "_role_health_check",
                           side_effect=RuntimeError("boom")):
        rc = cli.cmd_reconcile(mock.Mock(), backend=coord_backend)
    assert rc == 0


# ---------------------------------------------------------------------------
# Board: the Roles section — HELD / VACANT <duration>⚠ / CONTESTED
# ---------------------------------------------------------------------------


def _seed_three_state_roles(backend):
    _seed_role(backend, "held-role")
    _seed_lease(backend, "held-role", "live:h:r")
    _seed_role(backend, "vacant-role", sla_hours=24)
    _seed_lease(backend, "vacant-role", "dead:h:r", hours_old=30)
    _seed_role(backend, "contested-role", policy="exclusive")
    _seed_lease(backend, "contested-role", "live:h:r")
    _seed_lease(backend, "contested-role", "live2:h:r")
    _seed_presence(backend, "live:h:r")
    _seed_presence(backend, "live2:h:r")


def test_board_renders_roles_in_all_three_states(coord_backend, capsys):
    from fulcra_coord import query
    prev = os.environ.get("FULCRA_COORD_AGENT")
    os.environ["FULCRA_COORD_AGENT"] = "me:h:r"
    try:
        _seed_three_state_roles(coord_backend)
        rc = query.cmd_board(types.SimpleNamespace(agent=None, format="table"),
                             backend=coord_backend)
    finally:
        if prev is None:
            os.environ.pop("FULCRA_COORD_AGENT", None)
        else:
            os.environ["FULCRA_COORD_AGENT"] = prev
    assert rc == 0
    out = capsys.readouterr().out
    assert "Roles" in out
    assert "HELD by live:h:r" in out
    assert "VACANT" in out
    assert "⚠" in out          # vacant-role is past its 24h SLA
    assert "CONTESTED" in out


def test_board_json_includes_roles_section(coord_backend, capsys):
    from fulcra_coord import query
    prev = os.environ.get("FULCRA_COORD_AGENT")
    os.environ["FULCRA_COORD_AGENT"] = "me:h:r"
    try:
        _seed_three_state_roles(coord_backend)
        rc = query.cmd_board(types.SimpleNamespace(agent=None, format="json"),
                             backend=coord_backend)
    finally:
        if prev is None:
            os.environ.pop("FULCRA_COORD_AGENT", None)
        else:
            os.environ["FULCRA_COORD_AGENT"] = prev
    assert rc == 0
    board = json.loads(capsys.readouterr().out)
    by_name = {r["name"]: r for r in board["roles"]}
    assert by_name["held-role"]["vacant"] is False
    assert by_name["vacant-role"]["vacant"] is True
    assert by_name["contested-role"]["contested"] is True


def test_board_with_only_roles_still_renders(coord_backend, capsys):
    """A registry with no open loops must still render the Roles section —
    roles are board state, not an appendix to loops."""
    from fulcra_coord import query
    _seed_role(coord_backend, "lonely-role")
    rc = query.cmd_board(types.SimpleNamespace(agent=None, format="table"),
                         backend=coord_backend)
    assert rc == 0
    out = capsys.readouterr().out
    assert "lonely-role" in out
    assert "(no open coordination loops)" not in out


def test_board_empty_registry_omits_roles_section(coord_backend, capsys):
    from fulcra_coord import query
    rc = query.cmd_board(types.SimpleNamespace(agent=None, format="table"),
                         backend=coord_backend)
    assert rc == 0
    out = capsys.readouterr().out
    assert "(no open coordination loops)" in out
    assert "Roles" not in out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main(verbosity=2)
