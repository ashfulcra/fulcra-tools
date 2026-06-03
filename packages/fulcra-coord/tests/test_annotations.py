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
# needs-user annotation (situational awareness piece 6): emitted on
# block --on-user, gated, off by default.
# ---------------------------------------------------------------------------

class TestNeedsUserAnnotation(unittest.TestCase):
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
        t = schema.make_task(title="Approve deploy", workstream="devops",
                             agent="claude-code:mb:vercel")
        t["status"] = "blocked"
        t["blocked_on"] = "approve the deploy"
        t["assignee"] = "ash"
        return t

    def test_build_needs_user_tags(self):
        p = annotations.build_needs_user_annotation(
            task=self._task(), agent="claude-code:mb:vercel")
        self.assertEqual(p["track"], "Agent Tasks")
        self.assertIn("agent-tasks", p["cli_tags"])
        self.assertIn("needs-user", p["cli_tags"])
        self.assertIn("agent:claude", p["cli_tags"])
        # The ask is carried in the description.
        self.assertIn("approve the deploy", p["desc"])

    def test_default_off_is_noop(self):
        r = annotations.emit_needs_user_annotation(
            task=self._task(), agent="claude-code:mb:vercel")
        self.assertFalse(r)

    def test_cli_mode_invokes_writer_once(self):
        os.environ["FULCRA_COORD_ANNOTATIONS"] = "cli"
        calls = []

        def fake_writer(payload, *, backend=None):
            calls.append(payload)
            return True

        with patch.object(annotations, "_write_cli", side_effect=fake_writer):
            r = annotations.emit_needs_user_annotation(
                task=self._task(), agent="claude-code:mb:vercel")
        self.assertTrue(r)
        self.assertEqual(len(calls), 1)
        self.assertIn("needs-user", calls[0]["cli_tags"])

    def test_raising_writer_never_propagates(self):
        os.environ["FULCRA_COORD_ANNOTATIONS"] = "cli"

        def boom(payload, *, backend=None):
            raise RuntimeError("boom")

        with patch.object(annotations, "_write_cli", side_effect=boom):
            r = annotations.emit_needs_user_annotation(
                task=self._task(), agent="claude-code:mb:vercel")
        self.assertFalse(r)


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
# CLI transport: _write_cli builds the create-data-type invocation
# ---------------------------------------------------------------------------

class TestWriteCli(unittest.TestCase):
    """Unit-test the real `_write_cli` transport with a MOCKED subprocess.

    These tests NEVER shell out. They assert the exact `create-data-type`
    invocation shape: MomentAnnotation base type, the resolved CLI base
    (honouring FULCRA_CLI_COMMAND), the agent-tasks track tag plus lifecycle /
    agent / session tags, --add-to-timeline, and best-effort rc handling.
    """

    def setUp(self):
        self._saved_cli = os.environ.get("FULCRA_CLI_COMMAND")
        self._saved_backend = os.environ.get("FULCRA_COORD_BACKEND")
        # Pin an explicit CLI base so the resolver is deterministic and we can
        # assert it propagates into the built command. Clear the file-ops fake
        # backend override so it can't leak into annotation resolution.
        os.environ["FULCRA_CLI_COMMAND"] = "myfulcra --flag"
        os.environ.pop("FULCRA_COORD_BACKEND", None)

    def tearDown(self):
        for k, v in (
            ("FULCRA_CLI_COMMAND", self._saved_cli),
            ("FULCRA_COORD_BACKEND", self._saved_backend),
        ):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _payload(self, lifecycle="complete", agent="claude-code:mb:repo"):
        task = schema.make_task(
            title="Fix the widget pipeline",
            workstream="devops",
            agent=agent,
            summary="rewiring the spline",
            next_action="ship it",
        )
        task["id"] = "20260602-fix-the-widget"
        return annotations.build_annotation(
            lifecycle=lifecycle, task=task, agent=agent
        )

    def _capture_cmd(self, returncode=0, raises=None):
        """Patch subprocess.run; return (recorded_cmds, fake_run)."""
        recorded = []

        def fake_run(cmd, *args, **kwargs):
            recorded.append(cmd)
            if raises is not None:
                raise raises
            return types.SimpleNamespace(returncode=returncode, stdout="", stderr="")

        return recorded, fake_run

    def test_annotation_cli_override_decouples_from_file_cli(self):
        # FULCRA_COORD_ANNOTATION_CLI must take precedence over FULCRA_CLI_COMMAND
        # so the annotation writer can point at the annotations-capable build while
        # file-ops stay on the Files-capable build (no single fulcra-api build has
        # both create-data-type AND the file group yet).
        recorded, fake_run = self._capture_cmd(returncode=0)
        with patch.dict(os.environ, {"FULCRA_COORD_ANNOTATION_CLI": "annfulcra --x"}):
            with patch.object(annotations.subprocess, "run", side_effect=fake_run):
                annotations._write_cli(self._payload())
        self.assertEqual(recorded[0][:2], ["annfulcra", "--x"])  # override, not myfulcra
        # and unset -> falls back to the shared file CLI base (FULCRA_CLI_COMMAND)
        self.assertEqual(annotations._annotation_cli_base()[:1], ["myfulcra"])

    def test_builds_create_data_type_momentannotation(self):
        recorded, fake_run = self._capture_cmd(returncode=0)
        with patch.object(annotations.subprocess, "run", side_effect=fake_run):
            ok = annotations._write_cli(self._payload())
        self.assertTrue(ok)
        self.assertEqual(len(recorded), 1)
        cmd = recorded[0]
        self.assertIn("create-data-type", cmd)
        self.assertIn("MomentAnnotation", cmd)
        self.assertIn("--add-to-timeline", cmd)

    def test_uses_resolved_cli_base(self):
        # Honours FULCRA_CLI_COMMAND — never hardcodes `fulcra`.
        recorded, fake_run = self._capture_cmd(returncode=0)
        with patch.object(annotations.subprocess, "run", side_effect=fake_run):
            annotations._write_cli(self._payload())
        cmd = recorded[0]
        self.assertEqual(cmd[:2], ["myfulcra", "--flag"])
        self.assertEqual(cmd[2], "create-data-type")

    def test_name_is_annotation_text(self):
        recorded, fake_run = self._capture_cmd(returncode=0)
        payload = self._payload(lifecycle="complete")
        with patch.object(annotations.subprocess, "run", side_effect=fake_run):
            annotations._write_cli(payload)
        cmd = recorded[0]
        # NAME is the positional after MomentAnnotation.
        name_idx = cmd.index("MomentAnnotation") + 1
        name = cmd[name_idx]
        self.assertIn("complete", name)
        self.assertIn("Fix the widget pipeline", name)
        self.assertIn("20260602-fix-the-widget", name)

    def test_tag_set_is_track_lifecycle_agent_session(self):
        recorded, fake_run = self._capture_cmd(returncode=0)
        payload = self._payload(lifecycle="complete", agent="claude-code:mb:repo")
        with patch.object(annotations.subprocess, "run", side_effect=fake_run):
            annotations._write_cli(payload)
        cmd = recorded[0]
        tags = [cmd[i + 1] for i, a in enumerate(cmd) if a in ("--tag", "-t")]
        self.assertIn("agent-tasks", tags)          # shared TRACK tag
        self.assertIn("complete", tags)             # lifecycle
        self.assertIn("agent:claude", tags)         # agent:<kind>
        self.assertIn("session:mb", tags)           # session:<sess>

    def test_description_present(self):
        recorded, fake_run = self._capture_cmd(returncode=0)
        with patch.object(annotations.subprocess, "run", side_effect=fake_run):
            annotations._write_cli(self._payload())
        cmd = recorded[0]
        self.assertTrue("--description" in cmd or "-d" in cmd)

    def test_each_lifecycle_carries_its_tag(self):
        for lc in ("create", "pickup", "update", "complete"):
            recorded, fake_run = self._capture_cmd(returncode=0)
            with patch.object(annotations.subprocess, "run", side_effect=fake_run):
                annotations._write_cli(self._payload(lifecycle=lc))
            cmd = recorded[0]
            tags = [cmd[i + 1] for i, a in enumerate(cmd) if a in ("--tag", "-t")]
            self.assertIn(lc, tags, f"lifecycle {lc} missing from tags")
            self.assertIn("agent-tasks", tags)

    def test_nonzero_rc_returns_false(self):
        recorded, fake_run = self._capture_cmd(returncode=2)
        with patch.object(annotations.subprocess, "run", side_effect=fake_run):
            ok = annotations._write_cli(self._payload())
        self.assertFalse(ok)

    def test_raising_subprocess_returns_false(self):
        recorded, fake_run = self._capture_cmd(raises=FileNotFoundError("no cli"))
        with patch.object(annotations.subprocess, "run", side_effect=fake_run):
            ok = annotations._write_cli(self._payload())
        self.assertFalse(ok)


# ---------------------------------------------------------------------------
# emit gating drives the real _write_cli: default off makes no subprocess call
# ---------------------------------------------------------------------------

class TestEmitCliIntegration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp
        self._saved_mode = os.environ.get("FULCRA_COORD_ANNOTATIONS")
        self._saved_cli = os.environ.get("FULCRA_CLI_COMMAND")
        self._saved_backend = os.environ.get("FULCRA_COORD_BACKEND")
        os.environ["FULCRA_CLI_COMMAND"] = "myfulcra"
        os.environ.pop("FULCRA_COORD_BACKEND", None)

    def tearDown(self):
        os.environ.pop("XDG_CACHE_HOME", None)
        for k, v in (
            ("FULCRA_COORD_ANNOTATIONS", self._saved_mode),
            ("FULCRA_CLI_COMMAND", self._saved_cli),
            ("FULCRA_COORD_BACKEND", self._saved_backend),
        ):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _task(self):
        return schema.make_task(
            title="A task", workstream="devops", agent="claude-code:mb:repo"
        )

    def test_default_off_makes_no_subprocess_call(self):
        os.environ.pop("FULCRA_COORD_ANNOTATIONS", None)
        recorded = []
        with patch.object(annotations.subprocess, "run",
                          side_effect=lambda *a, **k: recorded.append(a)):
            result = annotations.emit_lifecycle_annotation(
                lifecycle="create", task=self._task(), agent="claude-code:mb:repo")
        self.assertFalse(result)
        self.assertEqual(recorded, [])

    def test_cli_mode_shells_out_and_records_marker(self):
        os.environ["FULCRA_COORD_ANNOTATIONS"] = "cli"
        recorded = []

        def fake_run(cmd, *a, **k):
            recorded.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        task = self._task()
        with patch.object(annotations.subprocess, "run", side_effect=fake_run):
            r1 = annotations.emit_lifecycle_annotation(
                lifecycle="create", task=task, agent="claude-code:mb:repo")
            # Retry the identical transition -> idempotent, no second shell-out.
            r2 = annotations.emit_lifecycle_annotation(
                lifecycle="create", task=task, agent="claude-code:mb:repo")
        self.assertTrue(r1)
        self.assertFalse(r2)
        self.assertEqual(len(recorded), 1)

    def test_cli_mode_failure_writes_no_marker(self):
        # A non-zero rc on the first call must NOT record a marker, so a later
        # retry is free to try again (failure is not "already annotated").
        os.environ["FULCRA_COORD_ANNOTATIONS"] = "cli"
        rcs = [2, 0]
        calls = []

        def fake_run(cmd, *a, **k):
            calls.append(cmd)
            return types.SimpleNamespace(returncode=rcs[len(calls) - 1],
                                         stdout="", stderr="")

        task = self._task()
        with patch.object(annotations.subprocess, "run", side_effect=fake_run):
            r1 = annotations.emit_lifecycle_annotation(
                lifecycle="create", task=task, agent="claude-code:mb:repo")
            r2 = annotations.emit_lifecycle_annotation(
                lifecycle="create", task=task, agent="claude-code:mb:repo")
        self.assertFalse(r1)   # rc 2 -> failure
        self.assertTrue(r2)    # retry succeeds (no marker blocked it)
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


# ---------------------------------------------------------------------------
# HTTP transport: _write_http replicates the fulcra-collect API flow over
# urllib (stdlib-only). These tests NEVER hit the network — urllib.request
# .urlopen and the token resolver are mocked.
# ---------------------------------------------------------------------------

import io
import urllib.error
from urllib import request as urllib_request


class _FakeResp:
    """Minimal context-manager stand-in for an http.client.HTTPResponse.

    urllib.request.urlopen returns an object usable as a context manager
    whose .read() yields the body bytes and .status carries the code. We
    only need read()/status here."""

    def __init__(self, body, status=200):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self._body = body or b""
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _http_error(code, body=b""):
    return urllib.error.HTTPError(
        url="http://x", code=code, msg="err", hdrs=None,
        fp=io.BytesIO(body if isinstance(body, bytes) else body.encode()),
    )


class _Router:
    """Records every urlopen call and answers by (METHOD, path-substring).

    Each entry in `routes` is (method, needle, response-or-callable). The
    first matching route wins; a callable is invoked with the Request so a
    route can record bodies or raise an HTTPError. Unmatched -> AssertionError
    so a test fails loudly on an unexpected endpoint rather than silently
    passing."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []  # list of (method, full_url, body_bytes, headers)

    def __call__(self, req, *args, **kwargs):
        method = req.get_method()
        url = req.full_url
        body = req.data
        # Header keys are capitalized by Request.add_header; normalize.
        headers = {k.lower(): v for k, v in req.header_items()}
        self.calls.append((method, url, body, headers))
        for m, needle, resp in self.routes:
            if m == method and needle in url:
                if callable(resp):
                    return resp(req)
                if isinstance(resp, Exception):
                    raise resp
                return resp
        raise AssertionError(f"unrouted request: {method} {url}")

    def posts_to(self, needle):
        return [c for c in self.calls if c[0] == "POST" and needle in c[1]]

    def gets_to(self, needle):
        return [c for c in self.calls if c[0] == "GET" and needle in c[1]]


class TestWriteHttp(unittest.TestCase):
    """Unit-test the real `_write_http` transport with urlopen + token mocked.

    Asserts the exact 3-endpoint flow fulcra-collect uses: tag resolve/create,
    moment-definition resolve/create (cached), then the JSONL record POST. The
    write must be best-effort — any urllib error returns False, never raises."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp
        self._saved = {
            k: os.environ.get(k)
            for k in ("FULCRA_COORD_ANNOTATIONS", "FULCRA_ACCESS_TOKEN",
                      "FULCRA_API_BASE", "FULCRA_COORD_REMOTE_ROOT")
        }
        os.environ["FULCRA_ACCESS_TOKEN"] = "tkn-abc"
        os.environ["FULCRA_API_BASE"] = "https://api.example.test"
        os.environ["FULCRA_COORD_REMOTE_ROOT"] = "/coordination-httptest"

    def tearDown(self):
        os.environ.pop("XDG_CACHE_HOME", None)
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _payload(self, lifecycle="complete", agent="claude-code:mb:repo"):
        task = schema.make_task(
            title="Fix the widget pipeline", workstream="devops",
            agent=agent, summary="rewiring", next_action="ship it",
        )
        task["id"] = "20260602-fix-the-widget"
        return annotations.build_annotation(
            lifecycle=lifecycle, task=task, agent=agent)

    def _happy_router(self, defs_initially_empty=True):
        """A router that resolves every tag to id 'tag-<name>', returns an
        empty (or pre-populated) annotation list, creates the def as 'def-1',
        and accepts the ingest POST."""
        tag_counter = {"n": 0}

        def tag_get(req):
            # Every tag name resolves on GET (200) -> deterministic id.
            name = req.full_url.rsplit("/", 1)[-1]
            return _FakeResp({"id": f"tag-{name}"})

        def ann_list(req):
            if defs_initially_empty:
                return _FakeResp([])
            return _FakeResp([{"id": "def-existing", "name": "Agent Tasks"}])

        def ann_create(req):
            return _FakeResp({"id": "def-1"})

        return _Router([
            ("GET", "/user/v1alpha1/tag/name/", tag_get),
            ("POST", "/user/v1alpha1/tag", lambda r: _FakeResp({"id": "tag-posted"})),
            ("GET", "/user/v1alpha1/annotation", ann_list),
            ("POST", "/user/v1alpha1/annotation", ann_create),
            ("POST", "/ingest/v1/record/batch", lambda r: _FakeResp(b"", 200)),
        ])

    def test_happy_path_three_endpoint_flow(self):
        router = self._happy_router()
        with patch.object(urllib_request, "urlopen", side_effect=router):
            ok = annotations._write_http(self._payload())
        self.assertTrue(ok)
        # (a) tag resolution happened for each cli_tag
        self.assertTrue(router.gets_to("/user/v1alpha1/tag/name/"))
        # (b) definition resolve (GET list) + create (POST) happened
        self.assertTrue(router.gets_to("/user/v1alpha1/annotation"))
        self.assertTrue(router.posts_to("/user/v1alpha1/annotation"))
        # (c) exactly one ingest POST
        ingest = router.posts_to("/ingest/v1/record/batch")
        self.assertEqual(len(ingest), 1)

    def test_ingest_post_shape(self):
        router = self._happy_router()
        with patch.object(urllib_request, "urlopen", side_effect=router):
            annotations._write_http(self._payload(lifecycle="complete"))
        _, url, body, headers = router.posts_to("/ingest/v1/record/batch")[0]
        self.assertEqual(headers.get("content-type"), "application/x-jsonl")
        self.assertEqual(headers.get("authorization"), "Bearer tkn-abc")
        # Body is JSONL: one object + trailing newline.
        self.assertTrue(body.endswith(b"\n"))
        rec = json.loads(body.decode().strip())
        self.assertEqual(rec["metadata"]["data_type"], "MomentAnnotation")
        self.assertEqual(rec["specversion"], 1)
        # source carries the definition source entry
        self.assertTrue(any(
            s == "com.fulcradynamics.annotation.def-1"
            for s in rec["metadata"]["source"]))
        # source also carries a lifecycle-stamped fulcra-coord source id
        self.assertTrue(any(
            "com.fulcradynamics.fulcra-coord.complete." in s
            for s in rec["metadata"]["source"]))
        # resolved tag ids (not raw names) ride in metadata.tags
        self.assertTrue(all(t.startswith("tag-") for t in rec["metadata"]["tags"]))
        # inner data carries title + note
        inner = json.loads(rec["data"])
        self.assertIn("Fix the widget pipeline", inner["title"])
        self.assertTrue(inner.get("note"))

    def test_definition_id_is_cached_across_calls(self):
        # Second annotation must NOT re-resolve the definition: the GET list /
        # POST create pair runs once, then the cached id is reused.
        router = self._happy_router()
        with patch.object(urllib_request, "urlopen", side_effect=router):
            annotations._write_http(self._payload(lifecycle="create"))
            annotations._write_http(self._payload(lifecycle="update"))
        self.assertEqual(len(router.gets_to("/user/v1alpha1/annotation")), 1)
        self.assertEqual(len(router.posts_to("/user/v1alpha1/annotation")), 1)
        # Both ingests still posted.
        self.assertEqual(len(router.posts_to("/ingest/v1/record/batch")), 2)

    def test_existing_definition_is_adopted_not_created(self):
        router = self._happy_router(defs_initially_empty=False)
        with patch.object(urllib_request, "urlopen", side_effect=router):
            ok = annotations._write_http(self._payload())
        self.assertTrue(ok)
        # No create POST when an "Agent Tasks" def already exists.
        self.assertEqual(len(router.posts_to("/user/v1alpha1/annotation")), 0)
        _, _, body, _ = router.posts_to("/ingest/v1/record/batch")[0]
        rec = json.loads(body.decode().strip())
        self.assertIn("com.fulcradynamics.annotation.def-existing",
                      rec["metadata"]["source"])

    def test_tag_404_then_create(self):
        # On a 404 GET, the writer POSTs to create the tag and uses that id.
        def tag_get(req):
            raise _http_error(404)

        router = _Router([
            ("GET", "/user/v1alpha1/tag/name/", tag_get),
            ("POST", "/user/v1alpha1/tag", lambda r: _FakeResp({"id": "tag-created"})),
            ("GET", "/user/v1alpha1/annotation", lambda r: _FakeResp([])),
            ("POST", "/user/v1alpha1/annotation", lambda r: _FakeResp({"id": "def-1"})),
            ("POST", "/ingest/v1/record/batch", lambda r: _FakeResp(b"", 200)),
        ])
        with patch.object(urllib_request, "urlopen", side_effect=router):
            ok = annotations._write_http(self._payload())
        self.assertTrue(ok)
        self.assertTrue(router.posts_to("/user/v1alpha1/tag"))
        _, _, body, _ = router.posts_to("/ingest/v1/record/batch")[0]
        rec = json.loads(body.decode().strip())
        self.assertIn("tag-created", rec["metadata"]["tags"])

    def test_http_error_anywhere_returns_false_never_raises(self):
        # A 500 on the ingest POST must yield False, not an exception.
        router = _Router([
            ("GET", "/user/v1alpha1/tag/name/",
             lambda r: _FakeResp({"id": "tag-x"})),
            ("GET", "/user/v1alpha1/annotation", lambda r: _FakeResp([])),
            ("POST", "/user/v1alpha1/annotation", lambda r: _FakeResp({"id": "def-1"})),
            ("POST", "/ingest/v1/record/batch", _http_error(500)),
        ])
        with patch.object(urllib_request, "urlopen", side_effect=router):
            ok = annotations._write_http(self._payload())
        self.assertFalse(ok)

    def test_urlerror_returns_false(self):
        def boom(req, *a, **k):
            raise urllib.error.URLError("connection refused")

        with patch.object(urllib_request, "urlopen", side_effect=boom):
            ok = annotations._write_http(self._payload())
        self.assertFalse(ok)


class TestHttpTokenResolution(unittest.TestCase):
    """The token comes from FULCRA_ACCESS_TOKEN, else `fulcra auth
    print-access-token`; no token at all -> _write_http no-ops to False with
    NO POST attempted."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp
        self._saved = {
            k: os.environ.get(k)
            for k in ("FULCRA_ACCESS_TOKEN", "FULCRA_API_BASE",
                      "FULCRA_COORD_REMOTE_ROOT")
        }
        os.environ.pop("FULCRA_ACCESS_TOKEN", None)
        os.environ["FULCRA_API_BASE"] = "https://api.example.test"
        os.environ["FULCRA_COORD_REMOTE_ROOT"] = "/coordination-tok"

    def tearDown(self):
        os.environ.pop("XDG_CACHE_HOME", None)
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _payload(self):
        task = schema.make_task(title="T", workstream="devops",
                                agent="claude-code:mb:repo")
        task["id"] = "20260602-t"
        return annotations.build_annotation(
            lifecycle="create", task=task, agent="claude-code:mb:repo")

    def test_env_token_used_when_set(self):
        os.environ["FULCRA_ACCESS_TOKEN"] = "env-token"
        self.assertEqual(annotations._resolve_token(), "env-token")

    def test_falls_back_to_cli_when_env_unset(self):
        # No env token -> shell out to `fulcra auth print-access-token`.
        recorded = {}

        def fake_run(cmd, *a, **k):
            recorded["cmd"] = cmd
            return types.SimpleNamespace(returncode=0, stdout="cli-token\n", stderr="")

        with patch.object(annotations.subprocess, "run", side_effect=fake_run):
            tok = annotations._resolve_token()
        self.assertEqual(tok, "cli-token")
        self.assertIn("print-access-token", recorded["cmd"])

    def test_no_token_makes_write_a_noop_false(self):
        # Neither env nor CLI yields a token -> _write_http returns False and
        # never posts.
        called = []

        def no_token():
            return None

        with patch.object(annotations, "_resolve_token", side_effect=no_token):
            with patch.object(urllib_request, "urlopen",
                              side_effect=lambda *a, **k: called.append(a)):
                ok = annotations._write_http(self._payload())
        self.assertFalse(ok)
        self.assertEqual(called, [])

    def test_cli_failure_yields_no_token(self):
        def fake_run(cmd, *a, **k):
            return types.SimpleNamespace(returncode=1, stdout="", stderr="nope")

        with patch.object(annotations.subprocess, "run", side_effect=fake_run):
            self.assertIsNone(annotations._resolve_token())


class TestEmitHttpIntegration(unittest.TestCase):
    """emit_* gating routes mode=http (and the `api` alias) to _write_http,
    records the marker on success, and stays a no-op when the flag is off."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp
        self._saved = os.environ.get("FULCRA_COORD_ANNOTATIONS")

    def tearDown(self):
        os.environ.pop("XDG_CACHE_HOME", None)
        if self._saved is None:
            os.environ.pop("FULCRA_COORD_ANNOTATIONS", None)
        else:
            os.environ["FULCRA_COORD_ANNOTATIONS"] = self._saved

    def _task(self):
        return schema.make_task(title="A task", workstream="devops",
                                agent="claude-code:mb:repo")

    def test_http_mode_routes_to_write_http(self):
        os.environ["FULCRA_COORD_ANNOTATIONS"] = "http"
        calls = []
        with patch.object(annotations, "_write_http",
                          side_effect=lambda p, *, backend=None: calls.append(p) or True):
            r = annotations.emit_lifecycle_annotation(
                lifecycle="create", task=self._task(), agent="claude-code:mb:repo")
        self.assertTrue(r)
        self.assertEqual(len(calls), 1)

    def test_api_alias_routes_to_write_http(self):
        os.environ["FULCRA_COORD_ANNOTATIONS"] = "api"
        calls = []
        with patch.object(annotations, "_write_http",
                          side_effect=lambda p, *, backend=None: calls.append(p) or True):
            r = annotations.emit_lifecycle_annotation(
                lifecycle="create", task=self._task(), agent="claude-code:mb:repo")
        self.assertTrue(r)
        self.assertEqual(len(calls), 1)

    def test_http_mode_off_default_no_call(self):
        os.environ.pop("FULCRA_COORD_ANNOTATIONS", None)
        calls = []
        with patch.object(annotations, "_write_http",
                          side_effect=lambda p, *, backend=None: calls.append(p) or True):
            r = annotations.emit_lifecycle_annotation(
                lifecycle="create", task=self._task(), agent="claude-code:mb:repo")
        self.assertFalse(r)
        self.assertEqual(calls, [])

    def test_needs_user_routes_to_write_http(self):
        os.environ["FULCRA_COORD_ANNOTATIONS"] = "http"
        t = self._task()
        t["status"] = "blocked"
        t["blocked_on"] = "approve it"
        calls = []
        with patch.object(annotations, "_write_http",
                          side_effect=lambda p, *, backend=None: calls.append(p) or True):
            r = annotations.emit_needs_user_annotation(
                task=t, agent="claude-code:mb:repo")
        self.assertTrue(r)
        self.assertEqual(len(calls), 1)

    def test_mode_resolves_http_and_api(self):
        os.environ["FULCRA_COORD_ANNOTATIONS"] = "http"
        self.assertEqual(annotations._mode(), "http")
        os.environ["FULCRA_COORD_ANNOTATIONS"] = "api"
        self.assertEqual(annotations._mode(), "http")
        os.environ["FULCRA_COORD_ANNOTATIONS"] = "cli"
        self.assertEqual(annotations._mode(), "cli")
        os.environ.pop("FULCRA_COORD_ANNOTATIONS", None)
        self.assertEqual(annotations._mode(), "off")


if __name__ == "__main__":
    unittest.main()
