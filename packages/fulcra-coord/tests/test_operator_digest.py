"""Tests for the Operator Digest (views.build_operator_digest, cli._render_digest,
the digest command + dedup guard, emit_digest_annotation, install-digest)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fulcra_coord import views, schema, cli

NOW = datetime(2026, 6, 4, 18, 0, 0, tzinfo=timezone.utc)
SINCE = NOW - timedelta(hours=12)


def _summary(**over):
    """A task_summary-shaped dict with sane defaults (mirrors schema.task_summary keys)."""
    base = {
        "id": "20260604-x", "title": "X", "status": "active", "priority": "P2",
        "workstream": "devops", "owner_agent": "claude-code:mb:repo",
        "assignee": None, "last_touched_by": "claude-code:mb:repo",
        "current_summary": "", "next_action": "", "blocked_on": None,
        "not_before": None, "due": None, "tags": [], "updated_at": "2026-06-04T17:00:00Z",
        "done_at": None, "acked_by": [],
    }
    base.update(over)
    return base


class TestBuildOperatorDigestEmpty(unittest.TestCase):
    def test_all_blocks_present_and_empty(self):
        d = views.build_operator_digest([], [], human="ash", now=NOW, since=SINCE)
        self.assertEqual(d["blocked_on_you"], [])
        self.assertEqual(d["upcoming"], [])
        self.assertEqual(d["per_agent"], [])
        self.assertEqual(d["stale"], [])


class TestBlockedRanking(unittest.TestCase):
    def test_due_soonest_then_oldest_age(self):
        # Three blocked-on-user asks: B due first, A&C undated; among undated,
        # oldest updated_at leads. needs:human tag makes them blocked-on-user.
        a = _summary(id="A", status="blocked", tags=["needs:human"],
                     updated_at="2026-06-04T09:00:00Z", due=None)
        b = _summary(id="B", status="blocked", tags=["needs:human"],
                     updated_at="2026-06-04T17:00:00Z",
                     due="2026-06-05T00:00:00Z")
        c = _summary(id="C", status="blocked", tags=["needs:human"],
                     updated_at="2026-06-04T08:00:00Z", due=None)
        d = views.build_operator_digest([a, b, c], [], human="ash",
                                        now=NOW, since=SINCE)
        self.assertEqual([s["id"] for s in d["blocked_on_you"]], ["B", "C", "A"])


class TestPerAgentAndWindows(unittest.TestCase):
    def test_finished_since_filters_by_done_at(self):
        recent = _summary(id="R", status="done", owner_agent="claude-code:mb:repo",
                          done_at="2026-06-04T12:00:00Z")           # after SINCE
        old = _summary(id="O", status="done", owner_agent="claude-code:mb:repo",
                       done_at="2026-06-03T12:00:00Z")              # before SINCE
        presence = [{"agent": "claude-code:mb:repo",
                     "workstreams": ["devops"], "summary": "shipping",
                     "last_seen": "2026-06-04T17:55:00Z"}]
        d = views.build_operator_digest([recent, old], presence, human="ash",
                                        now=NOW, since=SINCE)
        self.assertEqual(len(d["per_agent"]), 1)
        entry = d["per_agent"][0]
        self.assertEqual(entry["liveness"], "live")
        self.assertEqual([s["id"] for s in entry["finished_since"]], ["R"])

    def test_digest_now_and_since_use_fixed_microsecond_timestamps(self):
        d = views.build_operator_digest([], [], human="ash", now=NOW, since=SINCE)
        self.assertRegex(d["now"], r"\.\d{6}Z$")
        self.assertRegex(d["since"], r"\.\d{6}Z$")

    def test_upcoming_and_stale_blocks(self):
        # upcoming: future not_before within 7d, blocked-on-user.
        up = _summary(id="U", status="waiting", tags=["needs:human"],
                      not_before="2026-06-06T00:00:00Z")
        # stale: active, updated_at older than the 2h default threshold.
        st = _summary(id="S", status="active", updated_at="2026-06-04T10:00:00Z")
        d = views.build_operator_digest([up, st], [], human="ash",
                                        now=NOW, since=SINCE)
        self.assertEqual([s["id"] for s in d["upcoming"]], ["U"])
        self.assertEqual([s["id"] for s in d["stale"]], ["S"])


class TestRenderDigest(unittest.TestCase):
    def _full_digest(self):
        return {
            "schema": "fulcra.coordination.operator_digest.v1",
            "human": "ash", "now": NOW.isoformat().replace("+00:00", "Z"),
            "since": SINCE.isoformat().replace("+00:00", "Z"),
            "blocked_on_you": [
                _summary(id="B1", title="Re-auth GitHub", status="blocked",
                         owner_agent="claude-code:mb:repo",
                         blocked_on="approve the OAuth scope"),
                _summary(id="B2", title="Review PR", status="waiting",
                         owner_agent="codex:mb:main"),
            ],
            "upcoming": [_summary(id="U1", title="Rotate key",
                                  not_before="2026-06-06T00:00:00Z")],
            "per_agent": [{
                "agent": "claude-code:mb:repo", "workstreams": ["devops"],
                "summary": "shipping the digest", "liveness": "live",
                "finished_since": [_summary(id="F1", title="Land annotations",
                                            status="done")],
            }],
            "stale": [_summary(id="S1", title="Old churn", status="active")],
        }

    def test_name_summarizes_counts(self):
        name, note = cli._render_digest(self._full_digest(), window="evening")
        self.assertIn("evening", name)
        self.assertIn("2 on you", name)
        self.assertIn("1 upcoming", name)

    def test_note_has_all_sections(self):
        _, note = cli._render_digest(self._full_digest(), window="evening")
        self.assertIn("Re-auth GitHub", note)
        self.assertIn("approve the OAuth scope", note)
        self.assertIn("Rotate key", note)
        self.assertIn("claude-code:mb:repo", note)
        self.assertIn("Land annotations", note)
        self.assertIn("Old churn", note)

    def test_empty_digest_is_clean(self):
        empty = {"blocked_on_you": [], "upcoming": [], "per_agent": [], "stale": []}
        name, note = cli._render_digest(empty, window="morning")
        self.assertIn("0 on you", name)
        self.assertEqual(note, "")  # no empty section headers

    def test_missing_fields_do_not_crash(self):
        # A digest with a sparse summary (only id/status) must still render.
        d = {"blocked_on_you": [{"id": "Z", "status": "blocked"}],
             "upcoming": [], "per_agent": [], "stale": []}
        name, note = cli._render_digest(d, window="morning")
        self.assertIn("1 on you", name)

    def test_long_block_caps_with_more(self):
        many = [_summary(id=f"B{i}", title=f"ask {i}", status="blocked")
                for i in range(12)]
        d = {"blocked_on_you": many, "upcoming": [], "per_agent": [], "stale": []}
        _, note = cli._render_digest(d, window="evening")
        self.assertIn("…and 4 more", note)


import io
import urllib.error
from fulcra_coord import annotations


class _FakeResp:
    def __init__(self, body, status=200):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self._body = body or b""
        self.status = status
    def read(self): return self._body
    def __enter__(self): return self
    def __exit__(self, *a): return False


class TestEmitDigestAnnotation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp
        self._saved = {k: os.environ.get(k) for k in
                       ("FULCRA_ACCESS_TOKEN", "FULCRA_API_BASE",
                        "FULCRA_COORD_REMOTE_ROOT", "FULCRA_COORD_ANNOTATIONS")}
        os.environ["FULCRA_ACCESS_TOKEN"] = "tkn-abc"
        os.environ["FULCRA_API_BASE"] = "https://api.example.test"
        os.environ["FULCRA_COORD_REMOTE_ROOT"] = "/coordination-digesttest"
        os.environ["FULCRA_COORD_ANNOTATIONS"] = "http"

    def tearDown(self):
        os.environ.pop("XDG_CACHE_HOME", None)
        for k, v in self._saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def test_writes_against_digest_definition(self):
        calls = []
        def fake_urlopen(req, *a, **k):
            method, url = req.get_method(), req.full_url
            calls.append((method, url, req.data))
            if method == "GET" and "/tag/name/" in url:
                raise urllib.error.HTTPError(url, 404, "nf", None, io.BytesIO(b""))
            if method == "POST" and url.endswith("/user/v1alpha1/tag"):
                return _FakeResp({"id": "tag-1"})
            if method == "GET" and url.endswith("/user/v1alpha1/annotation"):
                return _FakeResp([])           # no existing defs -> create
            if method == "POST" and url.endswith("/user/v1alpha1/annotation"):
                return _FakeResp({"id": "digest-def-1"})
            if method == "POST" and "/ingest/v1/record/batch" in url:
                return _FakeResp(b"", status=202)
            raise AssertionError(f"unrouted: {method} {url}")
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            ok = annotations.emit_digest_annotation(
                name="Agent digest — evening (1 on you, 0 upcoming)",
                note="⛔ Blocked on you (1):\n  • thing",
                window="evening", agent="claude-code:mb:repo")
        self.assertTrue(ok)
        # The definition POST carried the DIGEST definition name, not "Agent Tasks".
        def_posts = [c for c in calls
                     if c[0] == "POST" and c[1].endswith("/user/v1alpha1/annotation")]
        self.assertEqual(len(def_posts), 1)
        self.assertIn(annotations.DIGEST_DEFINITION_NAME,
                      def_posts[0][2].decode())
        # The digest definition id was cached separately from "Agent Tasks".
        self.assertEqual(annotations._cached_digest_definition_id(), "digest-def-1")
        cached = json.loads(annotations._digest_definition_cache_path().read_text())
        self.assertRegex(cached["written_at"], r"\.\d{6}Z$")
        self.assertNotEqual(annotations._digest_definition_cache_path(),
                            annotations._definition_cache_path())
        # The digest cache must not clobber or populate the per-event definition
        # cache file.
        self.assertFalse(annotations._definition_cache_path().exists())

        records = [c for c in calls
                   if c[0] == "POST" and "/ingest/v1/record/batch" in c[1]]
        self.assertEqual(len(records), 1)
        record = json.loads(records[0][2].decode().splitlines()[0])
        self.assertRegex(record["metadata"]["recorded_at"], r"\.\d{6}Z$")

    def test_best_effort_returns_false_on_no_token(self):
        os.environ.pop("FULCRA_ACCESS_TOKEN", None)
        with patch.object(annotations, "_resolve_token", return_value=None):
            ok = annotations.emit_digest_annotation(
                name="n", note="b", window="morning", agent="claude-code:mb:repo")
        self.assertFalse(ok)

    def test_digest_definition_cache_obeys_ttl(self):
        annotations._store_digest_definition_id("digest-def-fresh")
        self.assertEqual(annotations._cached_digest_definition_id(), "digest-def-fresh")

        old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(
            timespec="microseconds").replace("+00:00", "Z")
        annotations._digest_definition_cache_path().write_text(
            json.dumps({"id": "digest-def-stale", "written_at": old}))
        self.assertIsNone(annotations._cached_digest_definition_id())

        annotations._digest_definition_cache_path().write_text(
            json.dumps({"id": "digest-def-legacy"}))
        self.assertIsNone(annotations._cached_digest_definition_id())


from fulcra_coord import entry


class TestDigestCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        os.environ["XDG_CACHE_HOME"] = self.tmp
        self.summaries = [
            _summary(id="B1", title="Re-auth", status="blocked",
                     tags=["needs:human"], owner_agent="claude-code:mb:repo"),
        ]
        self.presence = {"agents": [{"agent": "claude-code:mb:repo",
                                     "workstreams": ["devops"], "summary": "x",
                                     "last_seen": "2026-06-04T17:55:00Z"}]}

    def tearDown(self):
        os.environ.pop("XDG_CACHE_HOME", None)

    def _args(self, **over):
        ns = types.SimpleNamespace(window="evening", format="table",
                                   dry_run=False, human="ash")
        for k, v in over.items():
            setattr(ns, k, v)
        return ns

    def test_dry_run_writes_nothing(self):
        with patch("fulcra_coord.digest._load_task_summaries", return_value=self.summaries), \
             patch("fulcra_coord.cli.remote.download_json", return_value=self.presence), \
             patch("fulcra_coord.cli.lifecycle_annotations.emit_digest_annotation") as emit:
            rc = cli.cmd_digest(self._args(dry_run=True), backend=["false"])
        self.assertEqual(rc, 0)
        emit.assert_not_called()

    def test_real_run_emits(self):
        with patch("fulcra_coord.digest._load_task_summaries", return_value=self.summaries), \
             patch("fulcra_coord.cli.remote.download_json", return_value=self.presence), \
             patch("fulcra_coord.digest._claim_digest_marker", return_value=True), \
             patch("fulcra_coord.cli.lifecycle_annotations.emit_digest_annotation",
                   return_value=True) as emit:
            rc = cli.cmd_digest(self._args(), backend=["false"])
        self.assertEqual(rc, 0)
        emit.assert_called_once()
        _, kw = emit.call_args
        self.assertEqual(kw["window"], "evening")
        self.assertIn("on you", kw["name"])

    def test_json_format_prints_structured_digest(self):
        import io, contextlib
        buf = io.StringIO()
        with patch("fulcra_coord.digest._load_task_summaries", return_value=self.summaries), \
             patch("fulcra_coord.cli.remote.download_json", return_value=self.presence), \
             contextlib.redirect_stdout(buf):
            rc = cli.cmd_digest(self._args(format="json"), backend=["false"])
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["schema"], "fulcra.coordination.operator_digest.v1")
        self.assertEqual([s["id"] for s in payload["blocked_on_you"]], ["B1"])

    def test_command_is_wired_into_map(self):
        self.assertIs(entry.COMMAND_MAP["digest"], cli.cmd_digest)


class TestDigestMarker(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 6, 4, 18, 0, 0, tzinfo=timezone.utc)

    def test_absent_marker_is_claimed_and_written(self):
        uploaded = {}
        def fake_download_json(path, *, backend=None, timeout=None):
            return None  # marker absent
        def fake_upload_json(data, path, *, backend=None, timeout=None):
            uploaded["path"] = path
            uploaded["data"] = data
            return True
        with patch("fulcra_coord.cli.remote.download_json", side_effect=fake_download_json), \
             patch("fulcra_coord.cli.remote.upload_json", side_effect=fake_upload_json):
            granted = cli._claim_digest_marker("evening", self.now, backend=["false"])
        self.assertTrue(granted)
        self.assertTrue(uploaded["path"].endswith("digest/markers/2026-06-04-evening.json"))
        self.assertEqual(uploaded["data"]["window"], "evening")
        self.assertRegex(uploaded["data"]["claimed_at"], r"\.\d{6}Z$")

    def test_present_marker_is_noop(self):
        with patch("fulcra_coord.cli.remote.download_json",
                   return_value={"window": "evening", "by": "codex:mb:main"}), \
             patch("fulcra_coord.cli.remote.upload_json") as up:
            granted = cli._claim_digest_marker("evening", self.now, backend=["false"])
        self.assertFalse(granted)
        up.assert_not_called()

    def test_upload_failure_skips(self):
        with patch("fulcra_coord.cli.remote.download_json", return_value=None), \
             patch("fulcra_coord.cli.remote.upload_json", return_value=False):
            granted = cli._claim_digest_marker("evening", self.now, backend=["false"])
        self.assertFalse(granted)  # don't risk a double on a failed claim


import plistlib
from fulcra_coord import digest_schedule


class TestInstallDigest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_launchd_plist_has_both_windows_and_calendar(self):
        if not digest_schedule.scheduler_env.is_macos():
            self.skipTest("launchd path is macOS-only")
        plan = digest_schedule.install_digest(
            target_dir=self.tmp, logs_dir=self.tmp / "logs")
        self.assertEqual(plan["mechanism"], "launchd")
        # Two plists, one per window.
        names = sorted(Path(p).name for p in plan["writes"])
        self.assertEqual(names, ["com.fulcra.coord.digest.evening.plist",
                                 "com.fulcra.coord.digest.morning.plist"])
        morning = plistlib.loads(
            (self.tmp / "com.fulcra.coord.digest.morning.plist").read_bytes())
        self.assertIn("digest", morning["ProgramArguments"])
        self.assertIn("morning", morning["ProgramArguments"])
        self.assertEqual(morning["StartCalendarInterval"], {"Hour": 8, "Minute": 0})
        evening = plistlib.loads(
            (self.tmp / "com.fulcra.coord.digest.evening.plist").read_bytes())
        self.assertEqual(evening["StartCalendarInterval"], {"Hour": 18, "Minute": 0})

    def test_dry_run_writes_nothing(self):
        plan = digest_schedule.install_digest(
            target_dir=self.tmp, logs_dir=self.tmp / "logs", dry_run=True)
        self.assertTrue(plan["writes"])
        self.assertFalse(any(Path(p).exists() for p in plan["writes"]))

    def test_cron_has_two_managed_lines(self):
        cron = self.tmp / "cron.txt"
        plan = digest_schedule.install_digest(crontab_path=cron, force_cron=True)
        text = cron.read_text()
        self.assertIn("0 8 * * *", text)
        self.assertIn("0 18 * * *", text)
        self.assertIn("--window morning", text)
        self.assertIn("--window evening", text)
        self.assertEqual(text.count(digest_schedule.CRON_MARKER), 2)

    def test_cron_uninstall_is_surgical(self):
        cron = self.tmp / "cron.txt"
        cron.write_text("# my own job\n*/5 * * * * echo hi\n")
        digest_schedule.install_digest(crontab_path=cron, force_cron=True)
        digest_schedule.install_digest(crontab_path=cron, force_cron=True, uninstall=True)
        text = cron.read_text()
        self.assertIn("echo hi", text)
        self.assertNotIn(digest_schedule.CRON_MARKER, text)


class TestInstallDigestCommand(unittest.TestCase):
    def test_command_is_wired(self):
        self.assertIs(entry.COMMAND_MAP["install-digest"], cli.cmd_install_digest)

    def test_dry_run_reports_plan(self):
        import io, contextlib
        tmp = Path(tempfile.mkdtemp())
        args = types.SimpleNamespace(uninstall=False, dry_run=True,
                                     target_dir=str(tmp), logs_dir=str(tmp / "l"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_install_digest(args, backend=["false"])
        self.assertEqual(rc, 0)
        self.assertIn("dry-run", buf.getvalue())


class TestDigestInfraLine(unittest.TestCase):
    def test_infra_key_present_when_assessment_given(self):
        assessment = {"hosts": [{"host": "mac", "status": "healthy",
                                 "reasons": [], "metrics": {}}],
                      "bus": {"missed_digest_window": False},
                      "worst_status": "healthy"}
        d = views.build_operator_digest([], [], human="ash",
                                        infra=assessment)
        self.assertEqual(d["infra"], assessment)

    def test_infra_defaults_none_when_absent(self):
        d = views.build_operator_digest([], [], human="ash")
        self.assertIsNone(d.get("infra"))


class TestRenderInfraLine(unittest.TestCase):
    def test_degraded_infra_renders_a_warning_line(self):
        digest = {"blocked_on_you": [], "upcoming": [], "per_agent": [],
                  "stale": [],
                  "infra": {"hosts": [{"host": "mac", "status": "degraded",
                                       "reasons": ["reconcile stale 120m"],
                                       "metrics": {}}],
                            "bus": {"missed_digest_window": False},
                            "worst_status": "degraded"}}
        name, note = cli._render_digest(digest, window="evening")
        self.assertIn("infra", note)
        self.assertIn("mac", note)

    def test_all_healthy_infra_is_affirmative_or_brief(self):
        digest = {"blocked_on_you": [], "upcoming": [], "per_agent": [],
                  "stale": [],
                  "infra": {"hosts": [{"host": "a", "status": "healthy",
                                       "reasons": [], "metrics": {}},
                                      {"host": "b", "status": "healthy",
                                       "reasons": [], "metrics": {}}],
                            "bus": {"missed_digest_window": False},
                            "worst_status": "healthy"}}
        name, note = cli._render_digest(digest, window="evening")
        self.assertIn("2 hosts healthy", note)

    def test_no_infra_renders_nothing_extra(self):
        digest = {"blocked_on_you": [], "upcoming": [], "per_agent": [],
                  "stale": [], "infra": None}
        name, note = cli._render_digest(digest, window="evening")
        self.assertNotIn("infra", note)

    def test_single_host_reconcile_down_still_reports(self):
        # The v1 push surface: a single-host box with reconcile down but the
        # digest scheduler alive still emits this line.
        digest = {"blocked_on_you": [], "upcoming": [], "per_agent": [],
                  "stale": [],
                  "infra": {"hosts": [{"host": "solo", "status": "outage",
                                       "reasons": ["reconcile stale 400m (outage)"],
                                       "metrics": {}}],
                            "bus": {"missed_digest_window": False},
                            "worst_status": "outage"}}
        name, note = cli._render_digest(digest, window="morning")
        self.assertIn("solo", note)
        self.assertIn("infra", note)


class TestVersion(unittest.TestCase):
    def test_version_is_0_11_0(self):
        from fulcra_coord import __version__
        self.assertEqual(__version__, "0.11.0")
