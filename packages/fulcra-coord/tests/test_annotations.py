"""Tests for fulcra-coord Agent Tasks lifecycle annotations (feature #3).

These tests NEVER touch a live annotations API. Two layers are exercised:

  1. The pure writer (`fulcra_coord.annotations`): tag derivation, agent-kind
     mapping, text/link building, capability gating, best-effort error
     swallowing, and the idempotency marker.
  2. The CLI hook: each lifecycle command (start/tell, update --status active,
     update/assign, done) emits exactly ONE annotation with the right lifecycle
     tag, via a monkeypatched `emit_lifecycle_annotation` so nothing leaves the
     process.

The CLI hook tests run against the STATEFUL FAKE BACKEND (a local fulcra-api
emulator) so the real `_write_task_and_views` success path runs end-to-end and
the post-write annotation hook fires.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fulcra_coord import annotations, cache, schema

FAKE_BACKEND = Path(__file__).resolve().parents[1] / "tests" / "fake_fulcra_backend.py"


# ---------------------------------------------------------------------------
# Pure writer: agent-kind mapping
# ---------------------------------------------------------------------------

class TestAgentKindMapping(unittest.TestCase):
    def test_claude_code_maps_to_claude(self):
        self.assertEqual(annotations.agent_kind("claude-code:macbook:repo"), "claude")

    def test_openclaw_stays_openclaw(self):
        self.assertEqual(annotations.agent_kind("openclaw:host:chan"), "openclaw")

    def test_codex_maps_to_chatgpt(self):
        self.assertEqual(annotations.agent_kind("codex:host:session"), "chatgpt")

    def test_chatgpt_stays_chatgpt(self):
        self.assertEqual(annotations.agent_kind("chatgpt:host:session"), "chatgpt")

    def test_unknown_first_segment_is_lowercased_passthrough(self):
        self.assertEqual(annotations.agent_kind("Gemini:host"), "gemini")

    def test_empty_agent_is_safe(self):
        self.assertEqual(annotations.agent_kind(""), "unknown")
        self.assertEqual(annotations.agent_kind(None), "unknown")


class TestSessionTag(unittest.TestCase):
    def test_session_tag_is_second_segment(self):
        self.assertEqual(annotations.session_tag("claude-code:macbook:repo"), "macbook")

    def test_session_tag_falls_back_to_third_when_second_blank(self):
        self.assertEqual(annotations.session_tag("claude-code::chan"), "chan")

    def test_session_tag_none_when_no_segments(self):
        self.assertIsNone(annotations.session_tag("claude-code"))
        self.assertIsNone(annotations.session_tag(""))


# ---------------------------------------------------------------------------
# Pure writer: payload building
# ---------------------------------------------------------------------------

class TestBuildAnnotation(unittest.TestCase):
    def _task(self, **kw):
        t = schema.make_task(
            title="Fix the widget pipeline",
            workstream="devops",
            agent="claude-code:mb:repo",
        )
        t.update(kw)
        return t

    def test_track_name_is_agent_tasks(self):
        payload = annotations.build_annotation(
            lifecycle="create", task=self._task(), agent="claude-code:mb:repo"
        )
        self.assertEqual(payload["track"], "Agent Tasks")

    def test_tags_include_lifecycle_kind_and_session(self):
        task = self._task()
        payload = annotations.build_annotation(
            lifecycle="pickup", task=task, agent="claude-code:mb:repo"
        )
        self.assertIn("pickup", payload["tags"])
        self.assertIn("claude", payload["tags"])
        self.assertIn("mb", payload["tags"])

    def test_text_contains_lifecycle_title_and_id(self):
        task = self._task()
        payload = annotations.build_annotation(
            lifecycle="complete", task=task, agent="claude-code:mb:repo"
        )
        self.assertIn("complete", payload["text"])
        self.assertIn("Fix the widget pipeline", payload["text"])
        self.assertIn(task["id"], payload["text"])

    def test_text_contains_library_link(self):
        task = self._task()
        payload = annotations.build_annotation(
            lifecycle="create", task=task, agent="claude-code:mb:repo"
        )
        self.assertIn("https://library.fulcradynamics.com", payload["text"])
        self.assertIn(task["id"], payload["link"])


# ---------------------------------------------------------------------------
# Capability gating + best-effort
# ---------------------------------------------------------------------------

class TestEmitGating(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp
        self._saved = os.environ.get("FULCRA_COORD_ANNOTATIONS")
        os.environ.pop("FULCRA_COORD_ANNOTATIONS", None)

    def tearDown(self):
        os.environ.pop("XDG_CACHE_HOME", None)
        if self._saved is None:
            os.environ.pop("FULCRA_COORD_ANNOTATIONS", None)
        else:
            os.environ["FULCRA_COORD_ANNOTATIONS"] = self._saved

    def _task(self):
        return schema.make_task(
            title="A task", workstream="devops", agent="claude-code:mb:repo"
        )

    def test_default_off_is_noop(self):
        # No flag set -> writer must not attempt any real write and must report
        # it did nothing (False), without raising.
        result = annotations.emit_lifecycle_annotation(
            lifecycle="create", task=self._task(), agent="claude-code:mb:repo"
        )
        self.assertFalse(result)

    def test_unknown_flag_value_is_off(self):
        os.environ["FULCRA_COORD_ANNOTATIONS"] = "bogus"
        result = annotations.emit_lifecycle_annotation(
            lifecycle="create", task=self._task(), agent="claude-code:mb:repo"
        )
        self.assertFalse(result)

    def test_cli_mode_invokes_writer_once(self):
        os.environ["FULCRA_COORD_ANNOTATIONS"] = "cli"
        calls = []

        def fake_writer(payload, *, backend=None):
            calls.append(payload)
            return True

        with patch.object(annotations, "_write_cli", side_effect=fake_writer):
            result = annotations.emit_lifecycle_annotation(
                lifecycle="create", task=self._task(), agent="claude-code:mb:repo"
            )
        self.assertTrue(result)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["track"], "Agent Tasks")

    def test_raising_writer_never_propagates(self):
        os.environ["FULCRA_COORD_ANNOTATIONS"] = "cli"

        def boom(payload, *, backend=None):
            raise RuntimeError("annotation backend exploded")

        with patch.object(annotations, "_write_cli", side_effect=boom):
            # Must NOT raise; returns False.
            result = annotations.emit_lifecycle_annotation(
                lifecycle="create", task=self._task(), agent="claude-code:mb:repo"
            )
        self.assertFalse(result)


# ---------------------------------------------------------------------------
# Idempotency: one annotation per real transition, not per write-retry
# ---------------------------------------------------------------------------

class TestIdempotency(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp
        os.environ["FULCRA_COORD_ANNOTATIONS"] = "cli"

    def tearDown(self):
        os.environ.pop("XDG_CACHE_HOME", None)
        os.environ.pop("FULCRA_COORD_ANNOTATIONS", None)

    def _task(self):
        return schema.make_task(
            title="A task", workstream="devops", agent="claude-code:mb:repo"
        )

    def test_repeated_same_transition_emits_once(self):
        task = self._task()
        calls = []
        with patch.object(annotations, "_write_cli",
                          side_effect=lambda p, *, backend=None: calls.append(p) or True):
            r1 = annotations.emit_lifecycle_annotation(
                lifecycle="create", task=task, agent="claude-code:mb:repo")
            # Retry the SAME transition (same task, same events) — simulates a
            # write-retry after a transient view-upload failure.
            r2 = annotations.emit_lifecycle_annotation(
                lifecycle="create", task=task, agent="claude-code:mb:repo")
        self.assertTrue(r1)
        self.assertFalse(r2)  # second call is a no-op (already annotated)
        self.assertEqual(len(calls), 1)

    def test_distinct_same_second_transitions_get_distinct_markers(self):
        # M1: two distinct transitions that happen to stamp the IDENTICAL `at`
        # (same ISO second) must NOT collide on the idempotency anchor. The
        # anchor folds in the event count and type, so an update followed by a
        # status change sharing one timestamp produce different anchors and both
        # emit — no false dedupe.
        from datetime import datetime, timezone
        same = datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc)
        task = self._task()
        # Two genuine transitions forced onto the SAME second via dt=.
        t1 = schema.apply_update(task, by="claude-code:mb:repo",
                                 summary="note", dt=same)
        t2 = schema.apply_transition(t1, "active", by="claude-code:mb:repo",
                                     dt=same)
        a1 = annotations._transition_anchor(t1)
        a2 = annotations._transition_anchor(t2)
        self.assertNotEqual(a1, a2,
                            "distinct same-second transitions must differ")
        calls = []
        with patch.object(annotations, "_write_cli",
                          side_effect=lambda p, *, backend=None: calls.append(p) or True):
            r1 = annotations.emit_lifecycle_annotation(
                lifecycle="update", task=t1, agent="claude-code:mb:repo")
            r2 = annotations.emit_lifecycle_annotation(
                lifecycle="pickup", task=t2, agent="claude-code:mb:repo")
        self.assertTrue(r1)
        self.assertTrue(r2)
        self.assertEqual(len(calls), 2)

    def test_true_retry_same_second_still_dedupes(self):
        # M1 corollary: the hardened anchor must still skip a genuine RETRY — the
        # identical task re-uploaded reproduces the identical (count|at|type)
        # anchor, so the second call is a no-op.
        task = self._task()
        calls = []
        with patch.object(annotations, "_write_cli",
                          side_effect=lambda p, *, backend=None: calls.append(p) or True):
            r1 = annotations.emit_lifecycle_annotation(
                lifecycle="create", task=task, agent="claude-code:mb:repo")
            r2 = annotations.emit_lifecycle_annotation(
                lifecycle="create", task=task, agent="claude-code:mb:repo")
        self.assertTrue(r1)
        self.assertFalse(r2)
        self.assertEqual(len(calls), 1)

    def test_different_lifecycle_on_same_task_emits_again(self):
        task = self._task()
        calls = []
        with patch.object(annotations, "_write_cli",
                          side_effect=lambda p, *, backend=None: calls.append(p) or True):
            annotations.emit_lifecycle_annotation(
                lifecycle="create", task=task, agent="claude-code:mb:repo")
            # A genuinely new transition appends a new event -> new anchor.
            task = schema.apply_transition(task, "active", by="claude-code:mb:repo")
            annotations.emit_lifecycle_annotation(
                lifecycle="pickup", task=task, agent="claude-code:mb:repo")
        self.assertEqual(len(calls), 2)


# ---------------------------------------------------------------------------
# CLI hook wiring: each lifecycle command emits exactly one annotation
# ---------------------------------------------------------------------------

class TestCLILifecycleHooks(unittest.TestCase):
    """Drive the real CLI commands through the fake backend and assert the
    annotation hook fires once per command with the expected lifecycle tag."""

    def setUp(self):
        self.fake_state = tempfile.mkdtemp()
        self.cache_dir = tempfile.mkdtemp()
        self._saved = {
            k: os.environ.get(k)
            for k in (
                "FULCRA_COORD_BACKEND", "FULCRA_FAKE_ROOT",
                "FULCRA_COORD_REMOTE_ROOT", "XDG_CACHE_HOME",
                "FULCRA_COORD_ANNOTATIONS",
            )
        }
        os.environ["FULCRA_COORD_BACKEND"] = f"{sys.executable} {FAKE_BACKEND}"
        os.environ["FULCRA_FAKE_ROOT"] = self.fake_state
        os.environ["FULCRA_COORD_REMOTE_ROOT"] = "/coordination-anntest"
        os.environ["XDG_CACHE_HOME"] = self.cache_dir
        # The hook is wired but the WRITER is monkeypatched per-test, so the
        # flag value here is irrelevant to whether the hook is called — we patch
        # emit_lifecycle_annotation directly to record (lifecycle, task, agent).
        os.environ.pop("FULCRA_COORD_ANNOTATIONS", None)
        self.backend = os.environ["FULCRA_COORD_BACKEND"].split()

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _args(self, **kw):
        return types.SimpleNamespace(**kw)

    def _emit_recorder(self):
        calls = []

        def rec(*, lifecycle, task, agent, backend=None):
            calls.append({"lifecycle": lifecycle, "task": task, "agent": agent})
            return True

        return calls, rec

    def test_start_emits_create(self):
        from fulcra_coord import cli
        calls, rec = self._emit_recorder()
        with patch.object(cli.lifecycle_annotations, "emit_lifecycle_annotation", side_effect=rec):
            rc = cli.cmd_start(self._args(
                title="Build the thing", workstream="devops",
                agent="claude-code:mb:repo", kind="ops", priority="P2",
                summary="", next="", surface=None,
            ), backend=self.backend)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["lifecycle"], "create")
        self.assertEqual(calls[0]["task"]["title"], "Build the thing")

    def test_tell_emits_create(self):
        from fulcra_coord import cli
        calls, rec = self._emit_recorder()
        args = self._args(
            assignee="openclaw:host:chan", title="Please do X",
            workstream="general", priority="P2", summary="", next="",
        )
        setattr(args, "from", "claude-code:mb:repo")
        with patch.object(cli.lifecycle_annotations, "emit_lifecycle_annotation", side_effect=rec):
            rc = cli.cmd_tell(args, backend=self.backend)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["lifecycle"], "create")

    def test_update_to_active_emits_pickup(self):
        from fulcra_coord import cli
        # Seed a task first (create), then claim it via update --status active.
        cli.cmd_start(self._args(
            title="Claimable task", workstream="devops",
            agent="claude-code:mb:repo", kind="ops", priority="P2",
            summary="", next="", surface=None,
        ), backend=self.backend)
        task = cache.list_cached_tasks()[0]

        calls, rec = self._emit_recorder()
        with patch.object(cli.lifecycle_annotations, "emit_lifecycle_annotation", side_effect=rec):
            rc = cli.cmd_update(self._args(
                task_id=task["id"], summary="claiming", next=None,
                blocked_on=None, status="active", agent="claude-code:mb:repo",
            ), backend=self.backend)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["lifecycle"], "pickup")

    def test_plain_update_emits_update(self):
        from fulcra_coord import cli
        cli.cmd_start(self._args(
            title="Updatable task", workstream="devops",
            agent="claude-code:mb:repo", kind="ops", priority="P2",
            summary="", next="", surface=None,
        ), backend=self.backend)
        task = cache.list_cached_tasks()[0]

        calls, rec = self._emit_recorder()
        with patch.object(cli.lifecycle_annotations, "emit_lifecycle_annotation", side_effect=rec):
            rc = cli.cmd_update(self._args(
                task_id=task["id"], summary="progress note", next=None,
                blocked_on=None, status=None, agent="claude-code:mb:repo",
            ), backend=self.backend)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["lifecycle"], "update")

    def test_update_status_active_when_already_active_emits_update(self):
        # I2: 'pickup' must mean a genuine transition INTO active THIS call, not
        # merely a resulting active state. Re-asserting --status active on an
        # already-active task is a progress touch, so it tags 'update', not
        # 'pickup' — otherwise routine updates on active work were mis-tagged.
        from fulcra_coord import cli
        cli.cmd_start(self._args(
            title="Already active task", workstream="devops",
            agent="claude-code:mb:repo", kind="ops", priority="P2",
            summary="", next="", surface=None,
        ), backend=self.backend)
        task = cache.list_cached_tasks()[0]
        # First update --status active is the genuine pickup (proposed -> active).
        cli.cmd_update(self._args(
            task_id=task["id"], summary="claiming", next=None,
            blocked_on=None, status="active", agent="claude-code:mb:repo",
        ), backend=self.backend)

        # A SECOND update --status active (already active) is benign -> 'update'.
        calls, rec = self._emit_recorder()
        with patch.object(cli.lifecycle_annotations, "emit_lifecycle_annotation", side_effect=rec):
            rc = cli.cmd_update(self._args(
                task_id=task["id"], summary="still going", next=None,
                blocked_on=None, status="active", agent="claude-code:mb:repo",
            ), backend=self.backend)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["lifecycle"], "update")

    def test_assign_emits_update(self):
        from fulcra_coord import cli
        cli.cmd_start(self._args(
            title="Assignable task", workstream="devops",
            agent="claude-code:mb:repo", kind="ops", priority="P2",
            summary="", next="", surface=None,
        ), backend=self.backend)
        task = cache.list_cached_tasks()[0]

        calls, rec = self._emit_recorder()
        with patch.object(cli.lifecycle_annotations, "emit_lifecycle_annotation", side_effect=rec):
            rc = cli.cmd_assign(self._args(
                task_id=task["id"], assignee="openclaw:host:chan",
                agent="claude-code:mb:repo",
            ), backend=self.backend)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["lifecycle"], "update")

    def test_done_emits_complete(self):
        from fulcra_coord import cli
        cli.cmd_start(self._args(
            title="Finishable task", workstream="devops",
            agent="claude-code:mb:repo", kind="ops", priority="P2",
            summary="", next="", surface=None,
        ), backend=self.backend)
        task = cache.list_cached_tasks()[0]
        # Move to active first so done is a valid transition.
        cli.cmd_update(self._args(
            task_id=task["id"], summary=None, next=None, blocked_on=None,
            status="active", agent="claude-code:mb:repo",
        ), backend=self.backend)

        calls, rec = self._emit_recorder()
        with patch.object(cli.lifecycle_annotations, "emit_lifecycle_annotation", side_effect=rec):
            rc = cli.cmd_done(self._args(
                task_id=task["id"], evidence="shipped", verification_level="agent-verified",
                confidence=None, agent="claude-code:mb:repo",
            ), backend=self.backend)
        self.assertEqual(rc, 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["lifecycle"], "complete")

    def test_hook_is_noop_when_flag_unset_and_task_ops_succeed(self):
        # With no monkeypatch and the flag unset (default), the real
        # emit_lifecycle_annotation is a no-op and the task op still succeeds.
        from fulcra_coord import cli
        rc = cli.cmd_start(self._args(
            title="Silent task", workstream="devops",
            agent="claude-code:mb:repo", kind="ops", priority="P2",
            summary="", next="", surface=None,
        ), backend=self.backend)
        self.assertEqual(rc, 0)

    def test_raising_writer_does_not_break_task_op(self):
        # If the annotation emit raises, the task command must still return 0.
        from fulcra_coord import cli

        def boom(*, lifecycle, task, agent, backend=None):
            raise RuntimeError("emit blew up")

        # The hook call site must itself be defensive (best-effort) even if the
        # writer's own try/except were bypassed.
        with patch.object(cli.lifecycle_annotations, "emit_lifecycle_annotation", side_effect=boom):
            rc = cli.cmd_start(self._args(
                title="Resilient task", workstream="devops",
                agent="claude-code:mb:repo", kind="ops", priority="P2",
                summary="", next="", surface=None,
            ), backend=self.backend)
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
