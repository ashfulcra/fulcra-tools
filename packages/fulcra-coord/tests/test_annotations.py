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
import subprocess
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

    def test_recorded_at_anchored_to_latest_event_not_now(self):
        # BUG 12: build_annotation must thread the transition timestamp into the
        # payload so _recorded_at anchors the moment at transition time (its
        # documented promise), not the wall-clock now(). The anchor branch was
        # dead because the builders never set recorded_at/at/ts/timestamp.
        task = self._task()
        # Force a latest event with an OLD timestamp.
        old = "2026-01-01T00:00:00Z"
        task["events"] = [
            {"at": "2025-12-31T00:00:00Z", "type": "created", "by": "x"},
            {"at": old, "type": "active", "by": "x"},
        ]
        payload = annotations.build_annotation(
            lifecycle="pickup", task=task, agent="claude-code:mb:repo"
        )
        self.assertEqual(annotations._recorded_at(payload), old,
                         "annotation must anchor to the transition time, not now()")

    def test_needs_user_recorded_at_anchored_to_latest_event(self):
        task = self._task()
        old = "2026-02-02T12:00:00Z"
        task["blocked_on"] = "approve it"
        task["events"] = [{"at": old, "type": "blocked", "by": "x"}]
        payload = annotations.build_needs_user_annotation(
            task=task, agent="claude-code:mb:repo"
        )
        self.assertEqual(annotations._recorded_at(payload), old)


# ---------------------------------------------------------------------------
# Capability gating + best-effort
# ---------------------------------------------------------------------------

class TestEmitGating(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp
        # Isolate XDG_CONFIG_HOME too: with the env var unset, _mode() falls
        # through to the PERSISTED annotations config, so a machine that has run
        # `annotations on` would flip these default-off assertions. An empty tmp
        # config dir resolves cleanly to off.
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp, "config")
        self._saved = os.environ.get("FULCRA_COORD_ANNOTATIONS")
        os.environ.pop("FULCRA_COORD_ANNOTATIONS", None)
        os.environ.pop("FULCRA_COORD_ANNOTATIONS", None)

    def tearDown(self):
        os.environ.pop("XDG_CACHE_HOME", None)
        os.environ.pop("XDG_CONFIG_HOME", None)
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

    def test_on_mode_invokes_writer_once(self):
        os.environ["FULCRA_COORD_ANNOTATIONS"] = "on"
        calls = []

        def fake_writer(payload, *, backend=None):
            calls.append(payload)
            return True

        with patch.object(annotations, "_write_http", side_effect=fake_writer):
            result = annotations.emit_lifecycle_annotation(
                lifecycle="create", task=self._task(), agent="claude-code:mb:repo"
            )
        self.assertTrue(result)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["track"], "Agent Tasks")

    def test_raising_writer_never_propagates(self):
        os.environ["FULCRA_COORD_ANNOTATIONS"] = "on"

        def boom(payload, *, backend=None):
            raise RuntimeError("annotation backend exploded")

        with patch.object(annotations, "_write_http", side_effect=boom):
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
        # Isolate XDG_CONFIG_HOME too: with the env var unset, _mode() falls
        # through to the PERSISTED annotations config, so a machine that has run
        # `annotations on` would flip these default-off assertions. An empty tmp
        # config dir resolves cleanly to off.
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp, "config")
        self._saved = os.environ.get("FULCRA_COORD_ANNOTATIONS")
        os.environ.pop("FULCRA_COORD_ANNOTATIONS", None)
        os.environ.pop("FULCRA_COORD_ANNOTATIONS", None)

    def tearDown(self):
        os.environ.pop("XDG_CACHE_HOME", None)
        os.environ.pop("XDG_CONFIG_HOME", None)
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

    def test_on_mode_invokes_writer_once(self):
        os.environ["FULCRA_COORD_ANNOTATIONS"] = "on"
        calls = []

        def fake_writer(payload, *, backend=None):
            calls.append(payload)
            return True

        with patch.object(annotations, "_write_http", side_effect=fake_writer):
            r = annotations.emit_needs_user_annotation(
                task=self._task(), agent="claude-code:mb:vercel")
        self.assertTrue(r)
        self.assertEqual(len(calls), 1)
        self.assertIn("needs-user", calls[0]["cli_tags"])

    def test_raising_writer_never_propagates(self):
        os.environ["FULCRA_COORD_ANNOTATIONS"] = "on"

        def boom(payload, *, backend=None):
            raise RuntimeError("boom")

        with patch.object(annotations, "_write_http", side_effect=boom):
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
        os.environ["FULCRA_COORD_ANNOTATIONS"] = "on"

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
        with patch.object(annotations, "_write_http",
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
        with patch.object(annotations, "_write_http",
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
        with patch.object(annotations, "_write_http",
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
        with patch.object(annotations, "_write_http",
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

    def _cli_resolver(self, defs_initially_empty=True):
        """Patch target for BOTH tag and definition resolution via the CLI.

        Tags: ``fulcra tag get <name>`` -> ``{"id": "tag-<name>"}`` (the
        deterministic id assertions key on). Definitions: ``catalog --name`` is
        served by ``_fulcra_cli_json_lines`` (see ``fake_cli_lines``) — when
        ``defs_initially_empty`` it returns [] so resolution falls through to
        ``data-type create`` (``_fulcra_cli_json``) which mints ``def-1``;
        otherwise the catalog already carries an exact-name moment def
        ``def-existing``. Records every call so callers can count CLI hits.

        Returns ``(calls, fake_cli, fake_cli_lines)`` — the latter two patch
        ``_fulcra_cli_json`` and ``_fulcra_cli_json_lines`` respectively."""
        calls = []

        def fake_cli(args, **k):
            calls.append(list(args))
            if args[:2] == ["tag", "get"]:
                return {"id": f"tag-{args[2]}"}
            if args[:2] == ["data-type", "create"]:
                return {"id": "def-1"}
            return None

        def fake_cli_lines(args, **k):
            calls.append(list(args))
            if args[:1] == ["catalog"]:
                if defs_initially_empty:
                    return []
                name = args[args.index("--name") + 1] if "--name" in args else ""
                return [{
                    "id": f"MomentAnnotation/{name}",
                    "name": name,
                    "metadata": {"annotation_type": "moment",
                                 "id": "def-existing", "deleted_at": None},
                }]
            return []

        return calls, fake_cli, fake_cli_lines

    def test_happy_path_three_endpoint_flow(self):
        router = _Router([
            ("POST", "/ingest/v1/record/batch", lambda r: _FakeResp(b"", 200)),
        ])
        calls, fake_cli, fake_cli_lines = self._cli_resolver()
        with patch.object(annotations, "_fulcra_cli_json", side_effect=fake_cli), \
                patch.object(annotations, "_fulcra_cli_json_lines", side_effect=fake_cli_lines):
            with patch.object(urllib_request, "urlopen", side_effect=router):
                ok = annotations._write_http(self._payload())
        self.assertTrue(ok)
        # (a) tag resolution happened for each cli_tag via the fulcra tag CLI
        self.assertTrue(any(c[:2] == ["tag", "get"] for c in calls))
        # (b) definition resolve (catalog) + create (data-type create) happened
        self.assertTrue(any(c[:1] == ["catalog"] for c in calls))
        self.assertTrue(any(c[:2] == ["data-type", "create"] for c in calls))
        # (c) exactly one ingest POST
        ingest = router.posts_to("/ingest/v1/record/batch")
        self.assertEqual(len(ingest), 1)

    def test_ingest_post_shape(self):
        router = _Router([
            ("POST", "/ingest/v1/record/batch", lambda r: _FakeResp(b"", 200)),
        ])
        _, fake_cli, fake_cli_lines = self._cli_resolver()
        with patch.object(annotations, "_fulcra_cli_json", side_effect=fake_cli), \
                patch.object(annotations, "_fulcra_cli_json_lines", side_effect=fake_cli_lines):
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
        # Second annotation must NOT re-resolve the definition: the catalog /
        # data-type create pair runs once, then the cached id is reused.
        router = _Router([
            ("POST", "/ingest/v1/record/batch", lambda r: _FakeResp(b"", 200)),
        ])
        calls, fake_cli, fake_cli_lines = self._cli_resolver()
        with patch.object(annotations, "_fulcra_cli_json", side_effect=fake_cli), \
                patch.object(annotations, "_fulcra_cli_json_lines", side_effect=fake_cli_lines):
            with patch.object(urllib_request, "urlopen", side_effect=router):
                annotations._write_http(self._payload(lifecycle="create"))
                annotations._write_http(self._payload(lifecycle="update"))
        self.assertEqual(len([c for c in calls if c[:1] == ["catalog"]]), 1)
        self.assertEqual(
            len([c for c in calls if c[:2] == ["data-type", "create"]]), 1)
        # Both ingests still posted.
        self.assertEqual(len(router.posts_to("/ingest/v1/record/batch")), 2)

    def test_tag_ids_are_cached_across_calls(self):
        # BUG 6 (perf): tag ids must be cached per-name like the definition id.
        # Two emits with the SAME tag set must resolve each tag via the CLI ONCE
        # total (cache hit on the second emit), not once per emit.
        router = _Router([
            ("POST", "/ingest/v1/record/batch", lambda r: _FakeResp(b"", 200)),
        ])
        calls, fake_cli, fake_cli_lines = self._cli_resolver()
        with patch.object(annotations, "_fulcra_cli_json", side_effect=fake_cli), \
                patch.object(annotations, "_fulcra_cli_json_lines", side_effect=fake_cli_lines):
            with patch.object(urllib_request, "urlopen", side_effect=router):
                annotations._write_http(self._payload(lifecycle="create"))
                annotations._write_http(self._payload(lifecycle="update"))
        # Per distinct tag name, exactly one `tag get` across BOTH emits.
        from collections import Counter
        gets = Counter(c[2] for c in calls if c[:2] == ["tag", "get"])
        self.assertTrue(gets, "expected at least one tag resolution")
        for name, count in gets.items():
            self.assertEqual(count, 1,
                             f"tag {name} resolved {count}x via CLI; expected 1 (cached)")
        # Both ingests still posted (the emits themselves still happen).
        self.assertEqual(len(router.posts_to("/ingest/v1/record/batch")), 2)

    def test_existing_definition_is_adopted_not_created(self):
        router = _Router([
            ("POST", "/ingest/v1/record/batch", lambda r: _FakeResp(b"", 200)),
        ])
        calls, fake_cli, fake_cli_lines = self._cli_resolver(defs_initially_empty=False)
        with patch.object(annotations, "_fulcra_cli_json", side_effect=fake_cli), \
                patch.object(annotations, "_fulcra_cli_json_lines", side_effect=fake_cli_lines):
            with patch.object(urllib_request, "urlopen", side_effect=router):
                ok = annotations._write_http(self._payload())
        self.assertTrue(ok)
        # No `data-type create` when an "Agent Tasks" def already exists.
        self.assertFalse([c for c in calls if c[:2] == ["data-type", "create"]])
        _, _, body, _ = router.posts_to("/ingest/v1/record/batch")[0]
        rec = json.loads(body.decode().strip())
        self.assertIn("com.fulcradynamics.annotation.def-existing",
                      rec["metadata"]["source"])

    def test_tag_get_miss_then_create_rides_into_record(self):
        # `tag get` misses (None) -> `tag create` mints a LIST, and the new id
        # rides into the ingest record's metadata.tags. The def resolves via
        # the catalog/data-type CLI; record write stays over urllib.
        def fake_cli(args, **k):
            if args[:2] == ["tag", "get"]:
                return None
            if args[:2] == ["tag", "create"]:
                return [{"id": "tag-created"}]
            if args[:2] == ["data-type", "create"]:
                return {"id": "def-1"}
            return None

        def fake_cli_lines(args, **k):
            return []  # catalog empty -> create path

        router = _Router([
            ("POST", "/ingest/v1/record/batch", lambda r: _FakeResp(b"", 200)),
        ])
        with patch.object(annotations, "_fulcra_cli_json", side_effect=fake_cli), \
                patch.object(annotations, "_fulcra_cli_json_lines", side_effect=fake_cli_lines):
            with patch.object(urllib_request, "urlopen", side_effect=router):
                ok = annotations._write_http(self._payload())
        self.assertTrue(ok)
        _, _, body, _ = router.posts_to("/ingest/v1/record/batch")[0]
        rec = json.loads(body.decode().strip())
        self.assertIn("tag-created", rec["metadata"]["tags"])

    def test_http_error_anywhere_returns_false_never_raises(self):
        # A 500 on the ingest POST must yield False, not an exception.
        _, fake_cli, fake_cli_lines = self._cli_resolver()
        router = _Router([
            ("POST", "/ingest/v1/record/batch", _http_error(500)),
        ])
        with patch.object(annotations, "_fulcra_cli_json", side_effect=fake_cli), \
                patch.object(annotations, "_fulcra_cli_json_lines", side_effect=fake_cli_lines):
            with patch.object(urllib_request, "urlopen", side_effect=router):
                ok = annotations._write_http(self._payload())
        self.assertFalse(ok)

    def test_urlerror_returns_false(self):
        _, fake_cli, fake_cli_lines = self._cli_resolver()

        def boom(req, *a, **k):
            raise urllib.error.URLError("connection refused")

        with patch.object(annotations, "_fulcra_cli_json", side_effect=fake_cli), \
                patch.object(annotations, "_fulcra_cli_json_lines", side_effect=fake_cli_lines):
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

    def test_token_cli_uses_resolved_base_not_hardcoded_fulcra(self):
        # BUG 11: on a fulcra-api-only install (FULCRA_CLI_COMMAND set, no
        # FULCRA_ACCESS_TOKEN), the token CLI must use the SAME resolved base
        # remote.cli_base_cmd() returns + ["auth","print-access-token"], not a
        # hardcoded `fulcra` (which doesn't exist there -> dead annotations).
        recorded = {}

        def fake_run(cmd, *a, **k):
            recorded["cmd"] = cmd
            return types.SimpleNamespace(returncode=0, stdout="cli-token\n", stderr="")

        saved = os.environ.get("FULCRA_CLI_COMMAND")
        os.environ["FULCRA_CLI_COMMAND"] = "fake-fulcra-api --flag"
        try:
            with patch.object(annotations.subprocess, "run", side_effect=fake_run):
                tok = annotations._resolve_token()
        finally:
            if saved is None:
                os.environ.pop("FULCRA_CLI_COMMAND", None)
            else:
                os.environ["FULCRA_CLI_COMMAND"] = saved
        self.assertEqual(tok, "cli-token")
        # argv must start with the resolved base, not "fulcra".
        self.assertEqual(recorded["cmd"][:2], ["fake-fulcra-api", "--flag"])
        self.assertNotEqual(recorded["cmd"][0], "fulcra")
        self.assertEqual(recorded["cmd"][-2:], ["auth", "print-access-token"])

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


class TestFulcraCliJson(unittest.TestCase):
    """`_fulcra_cli_json` shells out to the resolved CLI base + args and parses
    stdout as JSON. It is the shared subprocess->JSON helper the annotation
    writer uses for tag/definition resolution. Best-effort: ANY failure
    (rc!=0, timeout, missing CLI, non-JSON) yields None and NEVER raises."""

    def test_rc0_dict_json_returned(self):
        def fake_run(cmd, *a, **k):
            return types.SimpleNamespace(returncode=0, stdout='{"id": "T"}', stderr="")

        with patch.object(annotations.subprocess, "run", side_effect=fake_run):
            self.assertEqual(
                annotations._fulcra_cli_json(["tag", "name", "x"]), {"id": "T"})

    def test_rc0_list_json_returned(self):
        def fake_run(cmd, *a, **k):
            return types.SimpleNamespace(
                returncode=0, stdout='[{"id": "N"}]', stderr="")

        with patch.object(annotations.subprocess, "run", side_effect=fake_run):
            self.assertEqual(
                annotations._fulcra_cli_json(["annotation", "list"]), [{"id": "N"}])

    def test_rc_nonzero_returns_none(self):
        def fake_run(cmd, *a, **k):
            return types.SimpleNamespace(returncode=1, stdout='{"id": "T"}', stderr="x")

        with patch.object(annotations.subprocess, "run", side_effect=fake_run):
            self.assertIsNone(annotations._fulcra_cli_json(["tag"]))

    def test_timeout_returns_none(self):
        def fake_run(cmd, *a, **k):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)

        with patch.object(annotations.subprocess, "run", side_effect=fake_run):
            self.assertIsNone(annotations._fulcra_cli_json(["tag"]))

    def test_missing_cli_returns_none(self):
        def fake_run(cmd, *a, **k):
            raise FileNotFoundError("no such binary")

        with patch.object(annotations.subprocess, "run", side_effect=fake_run):
            self.assertIsNone(annotations._fulcra_cli_json(["tag"]))

    def test_oserror_returns_none(self):
        def fake_run(cmd, *a, **k):
            raise OSError("boom")

        with patch.object(annotations.subprocess, "run", side_effect=fake_run):
            self.assertIsNone(annotations._fulcra_cli_json(["tag"]))

    def test_non_json_stdout_returns_none(self):
        def fake_run(cmd, *a, **k):
            return types.SimpleNamespace(returncode=0, stdout="not json", stderr="")

        with patch.object(annotations.subprocess, "run", side_effect=fake_run):
            self.assertIsNone(annotations._fulcra_cli_json(["tag"]))

    def test_backend_override_is_used_as_base(self):
        recorded = {}

        def fake_run(cmd, *a, **k):
            recorded["cmd"] = cmd
            return types.SimpleNamespace(returncode=1, stdout="", stderr="")

        with patch.object(annotations.subprocess, "run", side_effect=fake_run):
            annotations._fulcra_cli_json(["tag", "name", "x"], backend=["false"])
        # The override base must prefix the argv passed to subprocess.run.
        self.assertEqual(recorded["cmd"][:1], ["false"])
        self.assertEqual(recorded["cmd"], ["false", "tag", "name", "x"])

    def test_default_base_is_annotation_cli_base(self):
        # With no backend override, the base must come from
        # _annotation_cli_base() (the same resolution file ops use via
        # remote.cli_base_cmd), not a hardcoded command.
        recorded = {}

        def fake_run(cmd, *a, **k):
            recorded["cmd"] = cmd
            return types.SimpleNamespace(returncode=1, stdout="", stderr="")

        with patch.object(annotations, "_annotation_cli_base",
                          return_value=["base-cli", "--flag"]):
            with patch.object(annotations.subprocess, "run", side_effect=fake_run):
                annotations._fulcra_cli_json(["tag"])
        self.assertEqual(recorded["cmd"], ["base-cli", "--flag", "tag"])


class TestFulcraCliJsonLines(unittest.TestCase):
    """`_fulcra_cli_json_lines` shells out to the resolved CLI base + args and
    parses stdout as JSONL — one JSON object per non-empty line. Best-effort:
    ANY failure (rc!=0, timeout, missing CLI) yields [] and NEVER raises; a
    single unparseable line is SKIPPED, not fatal (the rest still parse)."""

    def test_rc0_jsonl_parsed_per_line(self):
        def fake_run(cmd, *a, **k):
            return types.SimpleNamespace(
                returncode=0, stdout='{"id": "A"}\n{"id": "B"}\n', stderr="")

        with patch.object(annotations.subprocess, "run", side_effect=fake_run):
            self.assertEqual(
                annotations._fulcra_cli_json_lines(["catalog", "--name", "x"]),
                [{"id": "A"}, {"id": "B"}])

    def test_blank_lines_skipped(self):
        def fake_run(cmd, *a, **k):
            return types.SimpleNamespace(
                returncode=0, stdout='\n{"id": "A"}\n\n', stderr="")

        with patch.object(annotations.subprocess, "run", side_effect=fake_run):
            self.assertEqual(
                annotations._fulcra_cli_json_lines(["catalog"]), [{"id": "A"}])

    def test_unparseable_line_skipped_not_fatal(self):
        def fake_run(cmd, *a, **k):
            return types.SimpleNamespace(
                returncode=0, stdout='{"id": "A"}\nNOT JSON\n{"id": "B"}\n', stderr="")

        with patch.object(annotations.subprocess, "run", side_effect=fake_run):
            self.assertEqual(
                annotations._fulcra_cli_json_lines(["catalog"]),
                [{"id": "A"}, {"id": "B"}])

    def test_rc_nonzero_returns_empty(self):
        def fake_run(cmd, *a, **k):
            return types.SimpleNamespace(returncode=1, stdout='{"id": "A"}', stderr="x")

        with patch.object(annotations.subprocess, "run", side_effect=fake_run):
            self.assertEqual(annotations._fulcra_cli_json_lines(["catalog"]), [])

    def test_timeout_returns_empty(self):
        def fake_run(cmd, *a, **k):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)

        with patch.object(annotations.subprocess, "run", side_effect=fake_run):
            self.assertEqual(annotations._fulcra_cli_json_lines(["catalog"]), [])

    def test_missing_cli_returns_empty(self):
        def fake_run(cmd, *a, **k):
            raise FileNotFoundError("no such binary")

        with patch.object(annotations.subprocess, "run", side_effect=fake_run):
            self.assertEqual(annotations._fulcra_cli_json_lines(["catalog"]), [])

    def test_backend_override_and_base_resolution(self):
        recorded = {}

        def fake_run(cmd, *a, **k):
            recorded["cmd"] = cmd
            return types.SimpleNamespace(returncode=1, stdout="", stderr="")

        with patch.object(annotations.subprocess, "run", side_effect=fake_run):
            annotations._fulcra_cli_json_lines(["catalog", "--name", "x"],
                                               backend=["false"])
        self.assertEqual(recorded["cmd"], ["false", "catalog", "--name", "x"])
        # And with no override, the base comes from _annotation_cli_base().
        with patch.object(annotations, "_annotation_cli_base",
                          return_value=["base-cli", "--flag"]):
            with patch.object(annotations.subprocess, "run", side_effect=fake_run):
                annotations._fulcra_cli_json_lines(["catalog"])
        self.assertEqual(recorded["cmd"], ["base-cli", "--flag", "catalog"])


class TestResolveDefViaCli(unittest.TestCase):
    """`_resolve_def_via_cli` resolves-or-creates a moment definition by EXACT
    name via ``fulcra catalog`` (skipping substring-only and soft-deleted
    matches), else creates it via ``fulcra data-type create`` with the lifecycle
    tag NAMES. Returns the def UUID or "" on total failure. NEVER raises."""

    def _entry(self, name, *, ann_type="moment", deleted=None, meta_id="UUID"):
        return {
            "id": f"MomentAnnotation/{meta_id}",
            "name": name,
            "metadata": {"annotation_type": ann_type, "id": meta_id,
                         "deleted_at": deleted},
        }

    def test_exact_name_moment_not_deleted_resolves(self):
        catalog = [self._entry("Agent Tasks", meta_id="def-resolved")]
        with patch.object(annotations, "_fulcra_cli_json_lines",
                          return_value=catalog) as lines, \
                patch.object(annotations, "_fulcra_cli_json") as cli:
            got = annotations._resolve_def_via_cli("Agent Tasks", "desc", ["a", "b"])
        self.assertEqual(got, "def-resolved")
        lines.assert_called_once_with(["catalog", "--name", "Agent Tasks"])
        cli.assert_not_called()  # no create when resolved

    def test_substring_only_and_soft_deleted_are_skipped(self):
        # A substring-only match ("Agent Tasks — Digest") AND a soft-deleted
        # same-name entry are BOTH skipped; with no live exact match, the
        # resolver falls through to data-type create.
        catalog = [
            self._entry("Agent Tasks — Digest", meta_id="digest-uuid"),
            self._entry("Agent Tasks", deleted="2026-01-01T00:00:00Z",
                        meta_id="deleted-uuid"),
        ]
        with patch.object(annotations, "_fulcra_cli_json_lines", return_value=catalog), \
                patch.object(annotations, "_fulcra_cli_json",
                             return_value={"id": "made-uuid"}) as cli:
            got = annotations._resolve_def_via_cli("Agent Tasks", "desc", ["x"])
        self.assertEqual(got, "made-uuid")
        cmd = cli.call_args[0][0]
        self.assertEqual(cmd[:4],
                         ["data-type", "create", "MomentAnnotation", "Agent Tasks"])
        self.assertIn("--add-to-timeline", cmd)
        self.assertEqual(cmd[cmd.index("--tag") + 1], "x")

    def test_non_moment_exact_name_is_skipped(self):
        catalog = [self._entry("Agent Tasks", ann_type="span", meta_id="span-uuid")]
        with patch.object(annotations, "_fulcra_cli_json_lines", return_value=catalog), \
                patch.object(annotations, "_fulcra_cli_json",
                             return_value={"id": "made-uuid"}):
            got = annotations._resolve_def_via_cli("Agent Tasks", "desc", [])
        self.assertEqual(got, "made-uuid")

    def test_empty_catalog_then_create(self):
        with patch.object(annotations, "_fulcra_cli_json_lines", return_value=[]), \
                patch.object(annotations, "_fulcra_cli_json",
                             return_value={"id": "D1"}) as cli:
            got = annotations._resolve_def_via_cli("Agent Tasks", "desc",
                                                   ["a", "", "b"])
        self.assertEqual(got, "D1")
        cmd = cli.call_args[0][0]
        # Empty tag names are dropped; non-empty become --tag args.
        tags = [cmd[i + 1] for i, v in enumerate(cmd) if v == "--tag"]
        self.assertEqual(tags, ["a", "b"])
        self.assertEqual(cmd[cmd.index("--description") + 1], "desc")

    def test_total_failure_returns_empty(self):
        with patch.object(annotations, "_fulcra_cli_json_lines", return_value=[]), \
                patch.object(annotations, "_fulcra_cli_json", return_value=None):
            got = annotations._resolve_def_via_cli("Agent Tasks", "desc", ["x"])
        self.assertEqual(got, "")

    def test_create_without_id_is_total_failure(self):
        with patch.object(annotations, "_fulcra_cli_json_lines", return_value=[]), \
                patch.object(annotations, "_fulcra_cli_json", return_value={"no": "id"}):
            got = annotations._resolve_def_via_cli("Agent Tasks", "desc", [])
        self.assertEqual(got, "")

    def test_never_raises_on_bad_entries(self):
        # Non-dict and metadata-less catalog entries must not raise.
        catalog = ["weird", {"name": "Agent Tasks"}, 42]
        with patch.object(annotations, "_fulcra_cli_json_lines", return_value=catalog), \
                patch.object(annotations, "_fulcra_cli_json",
                             return_value={"id": "made"}):
            got = annotations._resolve_def_via_cli("Agent Tasks", "desc", [])
        self.assertEqual(got, "made")


class _DefResolveBase(unittest.TestCase):
    """Shared isolation for the def-resolver tests (cache dir + remote root)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp
        self._saved_root = os.environ.get("FULCRA_COORD_REMOTE_ROOT")
        os.environ["FULCRA_COORD_REMOTE_ROOT"] = "/coordination-deftest"

    def tearDown(self):
        os.environ.pop("XDG_CACHE_HOME", None)
        if self._saved_root is None:
            os.environ.pop("FULCRA_COORD_REMOTE_ROOT", None)
        else:
            os.environ["FULCRA_COORD_REMOTE_ROOT"] = self._saved_root


class TestResolveDefinitionId(_DefResolveBase):
    """`_resolve_definition_id(tag_names, *, token=None)` checks the cache, else
    resolves-or-creates via the catalog/data-type CLI on DEFINITION_NAME, caches
    the non-empty UUID, and returns it. Takes tag NAMES now (not ids)."""

    def test_cache_hit_skips_cli(self):
        annotations._store_definition_id("def-cached")
        with patch.object(annotations, "_fulcra_cli_json_lines") as lines, \
                patch.object(annotations, "_fulcra_cli_json") as cli:
            got = annotations._resolve_definition_id(["agent-tasks"])
        self.assertEqual(got, "def-cached")
        lines.assert_not_called()
        cli.assert_not_called()

    def test_catalog_exact_match_resolves_and_caches(self):
        entry = {
            "id": "MomentAnnotation/def-x",
            "name": annotations.DEFINITION_NAME,
            "metadata": {"annotation_type": "moment", "id": "def-x",
                         "deleted_at": None},
        }
        # Catalog also carries a substring + a soft-deleted entry; both skipped.
        catalog = [
            {"id": "x", "name": annotations.DIGEST_DEFINITION_NAME,
             "metadata": {"annotation_type": "moment", "id": "digest-uuid",
                          "deleted_at": None}},
            {"id": "y", "name": annotations.DEFINITION_NAME,
             "metadata": {"annotation_type": "moment", "id": "dead-uuid",
                          "deleted_at": "2026-01-01T00:00:00Z"}},
            entry,
        ]
        with patch.object(annotations, "_fulcra_cli_json_lines", return_value=catalog), \
                patch.object(annotations, "_fulcra_cli_json") as cli:
            got = annotations._resolve_definition_id(["agent-tasks", "create"])
        self.assertEqual(got, "def-x")
        cli.assert_not_called()
        self.assertEqual(annotations._cached_definition_id(), "def-x")

    def test_empty_catalog_creates_and_caches(self):
        with patch.object(annotations, "_fulcra_cli_json_lines", return_value=[]), \
                patch.object(annotations, "_fulcra_cli_json",
                             return_value={"id": "D1"}) as cli:
            got = annotations._resolve_definition_id(["agent-tasks"])
        self.assertEqual(got, "D1")
        cmd = cli.call_args[0][0]
        self.assertEqual(cmd[3], annotations.DEFINITION_NAME)
        self.assertEqual(annotations._cached_definition_id(), "D1")

    def test_total_failure_returns_empty_and_does_not_cache(self):
        with patch.object(annotations, "_fulcra_cli_json_lines", return_value=[]), \
                patch.object(annotations, "_fulcra_cli_json", return_value=None):
            got = annotations._resolve_definition_id(["agent-tasks"])
        self.assertEqual(got, "")
        self.assertIsNone(annotations._cached_definition_id())

    def test_token_kwarg_is_accepted_back_compat(self):
        annotations._store_definition_id("def-cached")
        # A caller passing token= must not break (it is unused).
        got = annotations._resolve_definition_id(["agent-tasks"], token="ignored")
        self.assertEqual(got, "def-cached")


class TestResolveDigestDefinitionId(_DefResolveBase):
    """Same matrix as TestResolveDefinitionId but on DIGEST_DEFINITION_NAME and
    the digest-specific cache file."""

    def test_cache_hit_skips_cli(self):
        annotations._store_digest_definition_id("dig-cached")
        with patch.object(annotations, "_fulcra_cli_json_lines") as lines, \
                patch.object(annotations, "_fulcra_cli_json") as cli:
            got = annotations._resolve_digest_definition_id(["agent-digest"])
        self.assertEqual(got, "dig-cached")
        lines.assert_not_called()
        cli.assert_not_called()

    def test_catalog_exact_match_resolves_and_caches(self):
        catalog = [
            # The per-event "Agent Tasks" def is a substring of nothing here but
            # is the WRONG exact name -> skipped.
            {"id": "x", "name": annotations.DEFINITION_NAME,
             "metadata": {"annotation_type": "moment", "id": "event-uuid",
                          "deleted_at": None}},
            {"id": "y", "name": annotations.DIGEST_DEFINITION_NAME,
             "metadata": {"annotation_type": "moment", "id": "dig-x",
                          "deleted_at": None}},
        ]
        with patch.object(annotations, "_fulcra_cli_json_lines", return_value=catalog), \
                patch.object(annotations, "_fulcra_cli_json") as cli:
            got = annotations._resolve_digest_definition_id(["agent-digest"])
        self.assertEqual(got, "dig-x")
        cli.assert_not_called()
        self.assertEqual(annotations._cached_digest_definition_id(), "dig-x")
        # Must NOT clobber the per-event definition cache.
        self.assertFalse(annotations._definition_cache_path().exists())

    def test_empty_catalog_creates_and_caches(self):
        with patch.object(annotations, "_fulcra_cli_json_lines", return_value=[]), \
                patch.object(annotations, "_fulcra_cli_json",
                             return_value={"id": "DIG1"}) as cli:
            got = annotations._resolve_digest_definition_id(["agent-digest"])
        self.assertEqual(got, "DIG1")
        cmd = cli.call_args[0][0]
        self.assertEqual(cmd[3], annotations.DIGEST_DEFINITION_NAME)
        self.assertEqual(annotations._cached_digest_definition_id(), "DIG1")

    def test_total_failure_returns_empty_and_does_not_cache(self):
        with patch.object(annotations, "_fulcra_cli_json_lines", return_value=[]), \
                patch.object(annotations, "_fulcra_cli_json", return_value=None):
            got = annotations._resolve_digest_definition_id(["agent-digest"])
        self.assertEqual(got, "")
        self.assertIsNone(annotations._cached_digest_definition_id())


class TestResolveTagId(unittest.TestCase):
    """`_resolve_tag_id` resolves a tag NAME -> id via the ``fulcra tag`` CLI
    (`_fulcra_cli_json`), no longer raw urllib.

    Contract:
      * cache hit -> return cached id, no CLI call;
      * ``tag get <name>`` returning a dict with an ``id`` -> use + cache it;
      * a get miss (None) falls back to ``tag create <name>`` whose stdout is a
        LIST ``[created]`` (existing -> ``[]``) or, defensively, a dict;
      * total failure -> ``""`` (caller skips empty tag ids), nothing cached;
      * NEVER raises (best-effort writer).

    Tag names with colons (``agent:claude``) are passed as a plain argv argument
    — NO percent-encoding (that was a urllib-path concern only)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp
        self._saved_root = os.environ.get("FULCRA_COORD_REMOTE_ROOT")
        os.environ["FULCRA_COORD_REMOTE_ROOT"] = "/coordination-tagtest"

    def tearDown(self):
        os.environ.pop("XDG_CACHE_HOME", None)
        if self._saved_root is None:
            os.environ.pop("FULCRA_COORD_REMOTE_ROOT", None)
        else:
            os.environ["FULCRA_COORD_REMOTE_ROOT"] = self._saved_root

    def test_cache_hit_skips_cli(self):
        annotations._store_tag_id("status:active", "tag-cached")
        with patch.object(annotations, "_fulcra_cli_json") as cli:
            got = annotations._resolve_tag_id("status:active")
        self.assertEqual(got, "tag-cached")
        cli.assert_not_called()

    def test_get_hit_returns_id_and_caches(self):
        with patch.object(annotations, "_fulcra_cli_json",
                          return_value={"id": "tag-got"}) as cli:
            got = annotations._resolve_tag_id("agent-tasks")
        self.assertEqual(got, "tag-got")
        cli.assert_called_once_with(["tag", "get", "agent-tasks"])
        # Persisted so a second resolve is a zero-CLI cache hit.
        self.assertEqual(annotations._load_tag_cache().get("agent-tasks"), "tag-got")

    def test_get_miss_then_create_list_returns_id_and_caches(self):
        # get -> None (404), create -> LIST [{"id": ...}].
        calls = []

        def fake(args, **k):
            calls.append(args)
            if args[:2] == ["tag", "get"]:
                return None
            if args[:2] == ["tag", "create"]:
                return [{"id": "tag-made"}]
            return None

        with patch.object(annotations, "_fulcra_cli_json", side_effect=fake):
            got = annotations._resolve_tag_id("create")
        self.assertEqual(got, "tag-made")
        self.assertEqual(
            calls, [["tag", "get", "create"], ["tag", "create", "create"]])
        self.assertEqual(annotations._load_tag_cache().get("create"), "tag-made")

    def test_get_miss_then_create_dict_returns_id(self):
        # Defensive: a create that yields a bare dict (not a list) still works.
        def fake(args, **k):
            if args[:2] == ["tag", "get"]:
                return None
            return {"id": "tag-dict"}

        with patch.object(annotations, "_fulcra_cli_json", side_effect=fake):
            got = annotations._resolve_tag_id("pickup")
        self.assertEqual(got, "tag-dict")
        self.assertEqual(annotations._load_tag_cache().get("pickup"), "tag-dict")

    def test_total_failure_returns_empty_and_does_not_cache(self):
        # get None AND create None -> "" and nothing persisted.
        with patch.object(annotations, "_fulcra_cli_json", return_value=None):
            got = annotations._resolve_tag_id("complete")
        self.assertEqual(got, "")
        self.assertNotIn("complete", annotations._load_tag_cache())

    def test_create_empty_list_is_total_failure(self):
        # An existing tag yields create -> [] (409 skipped); with get already a
        # miss there is no id to extract, so the resolve fails to "" (the caller
        # skips it) rather than caching a bogus id.
        def fake(args, **k):
            if args[:2] == ["tag", "get"]:
                return None
            return []  # create: nothing minted

        with patch.object(annotations, "_fulcra_cli_json", side_effect=fake):
            got = annotations._resolve_tag_id("update")
        self.assertEqual(got, "")
        self.assertNotIn("update", annotations._load_tag_cache())

    def test_colon_name_passed_through_unescaped(self):
        # A namespaced tag name must reach the CLI as a plain argv arg, NOT
        # percent-encoded the way the old urllib path required.
        recorded = {}

        def fake(args, **k):
            recorded.setdefault("get", args)
            return {"id": "tag-x"}

        with patch.object(annotations, "_fulcra_cli_json", side_effect=fake):
            annotations._resolve_tag_id("agent:claude")
        self.assertEqual(recorded["get"], ["tag", "get", "agent:claude"])

    def test_never_raises_on_unexpected_shape(self):
        # A get returning a non-dict and create returning a non-list/dict must
        # not raise — best-effort writer.
        def fake(args, **k):
            if args[:2] == ["tag", "get"]:
                return "weird"
            return 42

        with patch.object(annotations, "_fulcra_cli_json", side_effect=fake):
            got = annotations._resolve_tag_id("session:Mac")
        self.assertEqual(got, "")

    def test_token_arg_is_ignored_back_compat(self):
        # The legacy positional ``token`` is accepted (callers still pass it) but
        # unused — resolution goes through the CLI regardless.
        with patch.object(annotations, "_fulcra_cli_json",
                          return_value={"id": "tag-y"}):
            got = annotations._resolve_tag_id("agent:claude", "ignored-token")
        self.assertEqual(got, "tag-y")


class TestEmitHttpIntegration(unittest.TestCase):
    """emit_* gating routes the enabled mode (``on``, and the legacy ``http`` /
    ``api`` aliases) to the single _write_http writer, records the marker on
    success, and stays a no-op when the flag is off."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp
        # Isolate XDG_CONFIG_HOME too: with the env var unset, _mode() falls
        # through to the PERSISTED annotations config, so a machine that has run
        # `annotations on` would flip these default-off assertions. An empty tmp
        # config dir resolves cleanly to off.
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp, "config")
        self._saved = os.environ.get("FULCRA_COORD_ANNOTATIONS")
        os.environ.pop("FULCRA_COORD_ANNOTATIONS", None)

    def tearDown(self):
        os.environ.pop("XDG_CACHE_HOME", None)
        os.environ.pop("XDG_CONFIG_HOME", None)
        if self._saved is None:
            os.environ.pop("FULCRA_COORD_ANNOTATIONS", None)
        else:
            os.environ["FULCRA_COORD_ANNOTATIONS"] = self._saved

    def _task(self):
        return schema.make_task(title="A task", workstream="devops",
                                agent="claude-code:mb:repo")

    def test_on_mode_routes_to_write_http(self):
        os.environ["FULCRA_COORD_ANNOTATIONS"] = "on"
        calls = []
        with patch.object(annotations, "_write_http",
                          side_effect=lambda p, *, backend=None: calls.append(p) or True):
            r = annotations.emit_lifecycle_annotation(
                lifecycle="create", task=self._task(), agent="claude-code:mb:repo")
        self.assertTrue(r)
        self.assertEqual(len(calls), 1)

    def test_legacy_aliases_route_to_write_http(self):
        # Legacy enable tokens from the old transport-duality era still enable the
        # single writer (back-compat): a machine with an inherited http/api/cli
        # env export keeps emitting after the mode collapse.
        for legacy in ("http", "api", "cli"):
            os.environ["FULCRA_COORD_ANNOTATIONS"] = legacy
            calls = []
            with patch.object(annotations, "_write_http",
                              side_effect=lambda p, *, backend=None: calls.append(p) or True):
                r = annotations.emit_lifecycle_annotation(
                    lifecycle="create", task=self._task(), agent="claude-code:mb:repo")
            self.assertTrue(r, f"legacy {legacy!r} should enable the writer")
            self.assertEqual(len(calls), 1)

    def test_off_default_no_call(self):
        os.environ.pop("FULCRA_COORD_ANNOTATIONS", None)
        calls = []
        with patch.object(annotations, "_write_http",
                          side_effect=lambda p, *, backend=None: calls.append(p) or True):
            r = annotations.emit_lifecycle_annotation(
                lifecycle="create", task=self._task(), agent="claude-code:mb:repo")
        self.assertFalse(r)
        self.assertEqual(calls, [])

    def test_needs_user_routes_to_write_http(self):
        os.environ["FULCRA_COORD_ANNOTATIONS"] = "on"
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

    def test_mode_resolves_on_and_legacy_aliases(self):
        # The new ``on`` token and every legacy enable token collapse to ``on``;
        # anything unrecognized or unset is ``off``.
        os.environ["FULCRA_COORD_ANNOTATIONS"] = "on"
        self.assertEqual(annotations._mode(), "on")
        os.environ["FULCRA_COORD_ANNOTATIONS"] = "http"
        self.assertEqual(annotations._mode(), "on")
        os.environ["FULCRA_COORD_ANNOTATIONS"] = "api"
        self.assertEqual(annotations._mode(), "on")
        os.environ["FULCRA_COORD_ANNOTATIONS"] = "cli"
        self.assertEqual(annotations._mode(), "on")
        os.environ["FULCRA_COORD_ANNOTATIONS"] = "bogus"
        self.assertEqual(annotations._mode(), "off")
        os.environ.pop("FULCRA_COORD_ANNOTATIONS", None)
        self.assertEqual(annotations._mode(), "off")


# ---------------------------------------------------------------------------
# Persisted annotation-mode config (Task B): the operator enables annotations
# ONCE (a config file) instead of exporting FULCRA_COORD_ANNOTATIONS in every
# shell, so every agent emits. Env still wins when present (a session override).
# Mirrors the human-handle persistence in identity.py.
# ---------------------------------------------------------------------------

class TestPersistedAnnotationMode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Isolate BOTH the config dir (where the persisted mode lives) and the
        # env var so a real ~/.config/fulcra-coord/annotations or an inherited
        # export on the dev machine can't leak into these assertions.
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp, "config")
        self._saved = os.environ.get("FULCRA_COORD_ANNOTATIONS")
        os.environ.pop("FULCRA_COORD_ANNOTATIONS", None)

    def tearDown(self):
        os.environ.pop("XDG_CONFIG_HOME", None)
        if self._saved is None:
            os.environ.pop("FULCRA_COORD_ANNOTATIONS", None)
        else:
            os.environ["FULCRA_COORD_ANNOTATIONS"] = self._saved
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_default_off_when_nothing_set(self):
        self.assertEqual(annotations._mode(), "off")
        self.assertIsNone(annotations._persisted_mode())

    def test_persisted_on_resolves_when_no_env(self):
        annotations.set_persisted_mode("on")
        self.assertEqual(annotations._persisted_mode(), "on")
        self.assertEqual(annotations._mode(), "on")

    def test_env_wins_over_persisted_config(self):
        # A session can override the persisted enablement: env always takes
        # precedence so a single shell can force off (or a legacy enable token).
        annotations.set_persisted_mode("on")
        os.environ["FULCRA_COORD_ANNOTATIONS"] = "off"
        self.assertEqual(annotations._mode(), "off")

    def test_legacy_persisted_value_normalizes_to_on(self):
        # Back-compat: a machine that persisted a legacy transport token
        # (``http``/``api``/``cli``) under the old duality keeps emitting — the
        # value normalizes to ``on`` on read, so the writer is NOT silently inert.
        for legacy in ("http", "api", "cli"):
            annotations.set_persisted_mode(legacy)
            self.assertEqual(annotations._persisted_mode(), "on",
                             f"legacy persisted {legacy!r} must resolve to on")
            self.assertEqual(annotations._mode(), "on")

    def test_clear_persisted_reverts_to_off(self):
        annotations.set_persisted_mode("on")
        removed = annotations.clear_persisted_mode()
        self.assertTrue(removed)
        self.assertIsNone(annotations._persisted_mode())
        self.assertEqual(annotations._mode(), "off")

    def test_clear_when_absent_returns_false(self):
        self.assertFalse(annotations.clear_persisted_mode())

    def test_resolve_mode_source_reports_env_config_default(self):
        # env source
        os.environ["FULCRA_COORD_ANNOTATIONS"] = "on"
        self.assertEqual(annotations.resolve_mode_source(), ("on", "env"))
        os.environ.pop("FULCRA_COORD_ANNOTATIONS", None)
        # config source
        annotations.set_persisted_mode("on")
        self.assertEqual(annotations.resolve_mode_source(), ("on", "config"))
        # default source
        annotations.clear_persisted_mode()
        self.assertEqual(annotations.resolve_mode_source(), ("off", "default"))


# ---------------------------------------------------------------------------
# `annotations` command (Task B): on/off/status.
# ---------------------------------------------------------------------------

class TestAnnotationsCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CONFIG_HOME"] = os.path.join(self.tmp, "config")
        self._saved = os.environ.get("FULCRA_COORD_ANNOTATIONS")
        os.environ.pop("FULCRA_COORD_ANNOTATIONS", None)

    def tearDown(self):
        os.environ.pop("XDG_CONFIG_HOME", None)
        if self._saved is None:
            os.environ.pop("FULCRA_COORD_ANNOTATIONS", None)
        else:
            os.environ["FULCRA_COORD_ANNOTATIONS"] = self._saved
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, action, out_format="table"):
        import io, contextlib
        from fulcra_coord.cli import cmd_annotations
        args = types.SimpleNamespace(annotations_action=action, format=out_format)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cmd_annotations(args)
        return rc, buf.getvalue()

    def test_on_persists_on(self):
        rc, _ = self._run("on")
        self.assertEqual(rc, 0)
        self.assertEqual(annotations._mode(), "on")
        self.assertEqual(annotations._persisted_mode(), "on")

    def test_off_clears_config(self):
        self._run("on")
        rc, _ = self._run("off")
        self.assertEqual(rc, 0)
        self.assertEqual(annotations._mode(), "off")

    def test_status_reports_config_source_json(self):
        self._run("on")
        # Never resolve a real token in the unit test — stub it out.
        with patch.object(annotations, "_resolve_token", return_value=None):
            rc, out = self._run("status", out_format="json")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["mode"], "on")
        self.assertEqual(data["source"], "config")
        self.assertFalse(data["token_ok"])
        # The token value itself must NEVER appear in output.
        self.assertNotIn("token", {k for k in data if k != "token_ok"})

    def test_status_default_reports_off(self):
        with patch.object(annotations, "_resolve_token", return_value=None):
            rc, out = self._run(None, out_format="json")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data["mode"], "off")
        self.assertEqual(data["source"], "default")

    def test_status_env_source(self):
        os.environ["FULCRA_COORD_ANNOTATIONS"] = "on"
        with patch.object(annotations, "_resolve_token", return_value="tok"):
            rc, out = self._run("status", out_format="json")
        data = json.loads(out)
        self.assertEqual(data["mode"], "on")
        self.assertEqual(data["source"], "env")
        self.assertTrue(data["token_ok"])


class TestDefinitionCacheTTL(unittest.TestCase):
    """BUG 4: the definition-id and tag-id caches never expired. A server-side
    definition deletion/rename left the cached id stale, so every future
    annotation silently failed (the id no longer resolves). A TTL turns an old
    cache entry into a MISS so the resolver re-runs; a fresh one stays a hit."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp

    def tearDown(self):
        os.environ.pop("XDG_CACHE_HOME", None)
        os.environ.pop("FULCRA_COORD_ANNOTATION_CACHE_TTL_SECONDS", None)

    def test_fresh_definition_cache_is_a_hit(self):
        annotations._store_definition_id("def-123")
        self.assertEqual(annotations._cached_definition_id(), "def-123")

    def test_stale_definition_cache_is_a_miss(self):
        from datetime import datetime, timezone, timedelta
        # Write a cache file with a written-at timestamp older than the TTL.
        path = annotations._definition_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat().replace("+00:00", "Z")
        path.write_text(json.dumps({"id": "def-stale", "written_at": old}))
        # Default TTL is 24h; a 2-day-old entry must read as a miss.
        self.assertIsNone(annotations._cached_definition_id())

    def test_ttl_env_override(self):
        from datetime import datetime, timezone, timedelta
        path = annotations._definition_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        ts = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat().replace("+00:00", "Z")
        path.write_text(json.dumps({"id": "def-x", "written_at": ts}))
        # A 5s TTL makes a 10s-old entry stale.
        os.environ["FULCRA_COORD_ANNOTATION_CACHE_TTL_SECONDS"] = "5"
        self.assertIsNone(annotations._cached_definition_id())
        # A large TTL makes it fresh again.
        os.environ["FULCRA_COORD_ANNOTATION_CACHE_TTL_SECONDS"] = "100000"
        self.assertEqual(annotations._cached_definition_id(), "def-x")

    def test_legacy_entry_without_written_at_is_a_miss(self):
        # A pre-TTL cache file (no written_at) must be treated as stale so it
        # re-resolves once and gets re-stamped, rather than trusting it forever.
        path = annotations._definition_cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"id": "def-legacy"}))
        self.assertIsNone(annotations._cached_definition_id())

    def test_tag_cache_ttl(self):
        from datetime import datetime, timezone, timedelta
        annotations._store_tag_id("status:active", "tag-1")
        self.assertEqual(annotations._load_tag_cache().get("status:active"), "tag-1")
        # Rewrite the tag cache with an old written_at to simulate age.
        path = annotations._tag_cache_path()
        old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat().replace("+00:00", "Z")
        path.write_text(json.dumps({"written_at": old, "tags": {"status:active": "tag-1"}}))
        self.assertEqual(annotations._load_tag_cache(), {},
                         "a stale tag cache must read empty so tags re-resolve")


class TestBuildAnnotationEnrichment(unittest.TestCase):
    def _task(self, **over):
        t = schema.make_task(
            title="Fix the widget", workstream="devops", agent="claude-code:mb:repo",
            kind="feature", summary="rewiring the pump", next_action="ship it")
        t["id"] = "20260604-fix-widget"
        t.update(over)
        return t

    def test_desc_carries_work_substance(self):
        p = annotations.build_annotation(
            lifecycle="update", task=self._task(), agent="claude-code:mb:repo")
        self.assertIn("devops", p["desc"])
        self.assertIn("feature", p["desc"])      # kind from tags
        self.assertIn("rewiring the pump", p["desc"])
        self.assertIn("ship it", p["desc"])

    def test_backward_compatible_when_sparse(self):
        # No summary/next_action -> still produces a non-empty desc, never raises.
        t = self._task(current_summary="", next_action="")
        p = annotations.build_annotation(lifecycle="create", task=t,
                                         agent="claude-code:mb:repo")
        self.assertTrue(p["desc"])
        self.assertIn("devops", p["desc"])


if __name__ == "__main__":
    unittest.main()
