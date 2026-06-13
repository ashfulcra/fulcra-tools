"""Staleness-guarded reads — the 2026-06-10 STALE-VIEW BLINDNESS fix.

THE DEFECT (live evidence, 2026-06-10): every read surface (inbox, presence /
liveness, board) reads materialized views (``views/summaries.json``, the
presence aggregate) that refresh ONLY when a reconcile successfully uploads
them. Under backend write-throttling (20–80% upload failures per tick), the
views went HOURS stale while task bodies landed fine — so every agent polling
``inbox`` saw nothing (6 review verdicts + 2 direct messages + a review request
sat invisible), and ``request-review`` reported "no reviewer live" while the
reviewer WAS live (stale presence aggregate). The durable Tier-0 layer worked;
the read path lied.

THE CONTRACT under test:

1. The summaries view and the presence aggregate are stamped ``generated_at``
   (ISO Z) at build time. Additive — old readers ignore it.
2. ``_load_task_summaries`` checks freshness: absent ``generated_at`` (old bus)
   → today's behavior (use the view); present and older than
   ``FULCRA_COORD_VIEW_STALE_MIN`` (default 20m) → fall back to the DIRECT
   path (raw ``tasks/`` listing + body loads) and warn. Same guard for the
   presence aggregate used by liveness routing (per-agent ``presence/*.json``
   records are the direct source there).
3. The fallback is unbounded (never silently drops tasks); if the direct
   listing ALSO fails, the stale view is used with a louder warn — degraded,
   never blind.
4. ``FULCRA_COORD_VIEW_STALE_MIN=0`` disables the guard entirely.
"""

from __future__ import annotations

import json
import contextlib
import io
import os
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from fulcra_coord import cache, remote, schema, views
from fulcra_coord.io import _load_task_summaries


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _task(title: str, *, assignee: str | None = None) -> dict:
    t = schema.make_task(title=title, workstream="ws", agent="owner:h:r",
                         owner_agent="owner:h:r", assignee=assignee)
    return t


class TestGeneratedAtStamp(unittest.TestCase):
    """Contract 1: both aggregates carry a build-time ``generated_at``."""

    def test_build_summaries_stamps_generated_at(self):
        v = views.build_summaries([_task("a")])
        self.assertIn("generated_at", v)
        self.assertIsNotNone(views._parse_dt(v["generated_at"]))

    def test_build_presence_stamps_generated_at(self):
        v = views.build_presence([schema.make_presence("a:h:r")])
        self.assertIn("generated_at", v)
        self.assertIsNotNone(views._parse_dt(v["generated_at"]))


class TestViewStalenessMinutes(unittest.TestCase):
    """The pure freshness predicate behind both guards."""

    def test_fresh_view_is_not_stale(self):
        v = {"generated_at": _iso(_now())}
        self.assertIsNone(views.view_staleness_minutes(v))

    def test_two_hour_old_view_is_stale(self):
        v = {"generated_at": _iso(_now() - timedelta(hours=2))}
        age = views.view_staleness_minutes(v)
        self.assertIsNotNone(age)
        self.assertGreaterEqual(age, 119)

    def test_absent_generated_at_is_back_compat_fresh(self):
        # Old bus: no stamp → keep today's behavior (trust the view).
        self.assertIsNone(views.view_staleness_minutes({"updated_at": "2020-01-01T00:00:00Z"}))

    def test_unparseable_generated_at_fails_toward_stale(self):
        # Same fail-toward-surfacing choice _age_hours makes everywhere else.
        v = {"generated_at": "not-a-timestamp"}
        self.assertIsNotNone(views.view_staleness_minutes(v))

    def test_env_knob_zero_disables_the_guard(self):
        v = {"generated_at": _iso(_now() - timedelta(hours=2))}
        with mock.patch.dict(os.environ, {"FULCRA_COORD_VIEW_STALE_MIN": "0"}):
            self.assertIsNone(views.view_staleness_minutes(v))

    def test_env_knob_tightens_threshold(self):
        v = {"generated_at": _iso(_now() - timedelta(minutes=5))}
        with mock.patch.dict(os.environ, {"FULCRA_COORD_VIEW_STALE_MIN": "1"}):
            self.assertIsNotNone(views.view_staleness_minutes(v))


class _SummariesBackendFake:
    """Counting fake of the remote transport for the summaries guard tests.

    Serves a summaries view (whose ``generated_at`` the test controls), a raw
    ``tasks/`` listing, and per-task bodies; counts list/body calls so a test
    can assert the FAST path did no body listing and the STALE path did.
    """

    def __init__(self, summaries_view, bodies):
        self.summaries_view = summaries_view
        self.bodies = {t["id"]: t for t in bodies}
        self.list_calls = 0
        self.body_downloads = 0

    def download_json(self, path, *, backend=None, timeout=None):
        if path == remote.view_remote_path("summaries"):
            return self.summaries_view
        for tid, body in self.bodies.items():
            if path == remote.task_remote_path(tid):
                self.body_downloads += 1
                return body
        return None

    def list_files(self, prefix, *, backend=None, timeout=None):
        self.list_calls += 1
        from fulcra_coord import remote_root
        assert prefix == f"{remote_root()}/tasks/"
        return [remote.task_remote_path(tid) for tid in self.bodies]


class TestSummariesStaleGuard(unittest.TestCase):
    """Contract 2+3 for ``_load_task_summaries``."""

    def setUp(self):
        self._xdg = tempfile.TemporaryDirectory()
        self._env = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self._xdg.name})
        self._env.start()
        self.t1 = _task("in the view")
        self.t2 = _task("MISSING from the stale view", assignee="me:h:r")

    def tearDown(self):
        self._env.stop()
        self._xdg.cleanup()

    def _patched(self, fake):
        return [
            mock.patch("fulcra_coord.remote.download_json",
                       side_effect=fake.download_json),
            mock.patch("fulcra_coord.remote.list_files",
                       side_effect=fake.list_files),
            mock.patch("fulcra_coord.remote.stat", return_value=None),
        ]

    def _run(self, fake, **kwargs):
        patches = self._patched(fake)
        for p in patches:
            p.start()
        try:
            return _load_task_summaries(backend=["false"], **kwargs)
        finally:
            for p in patches:
                p.stop()

    def test_fresh_complete_view_uses_fast_path_no_listing_by_default(self):
        view = views.build_summaries([self.t1, self.t2])  # fresh generated_at
        fake = _SummariesBackendFake(view, [self.t1, self.t2])
        got = self._run(fake)
        self.assertEqual({s["id"] for s in got}, {self.t1["id"], self.t2["id"]})
        self.assertEqual(fake.list_calls, 0, "fresh view must not list tasks/")
        self.assertEqual(fake.body_downloads, 0,
                         "complete fresh view must not fetch bodies")

    def test_fresh_incomplete_view_can_heal_missing_task_files(self):
        # Live 2026-06-13 failure: summaries was freshly stamped but omitted
        # newly-routed Arc review tasks. Staleness detection could not fire, so
        # listener/inbox reads saw an empty inbox while durable task files and
        # directive loops existed.
        view = views.build_summaries([self.t1])
        fake = _SummariesBackendFake(view, [self.t1, self.t2])
        with mock.patch("fulcra_coord.io._warn") as warned:
            got = self._run(fake, heal_missing_entries=True)
        self.assertEqual({s["id"] for s in got}, {self.t1["id"], self.t2["id"]})
        self.assertEqual(fake.list_calls, 1)
        self.assertEqual(fake.body_downloads, 2)
        self.assertTrue(warned.called, "fresh-but-incomplete view must be visible")
        self.assertTrue(
            any("freshly stamped" in call.args[0]
                for call in warned.call_args_list)
        )

    def test_fresh_open_summary_row_refreshes_from_task_body(self):
        stale = dict(self.t2)
        stale["status"] = "waiting"
        fresh = dict(self.t2)
        fresh["status"] = "done"
        fresh["updated_at"] = _iso(_now())
        view = views.build_summaries([self.t1, stale])
        fake = _SummariesBackendFake(view, [self.t1, fresh])
        with mock.patch("fulcra_coord.io._warn"):
            got = self._run(fake, heal_missing_entries=True)
        by_id = {s["id"]: s for s in got}
        self.assertEqual(by_id[self.t2["id"]]["status"], "done")

    def test_fresh_open_summary_row_refresh_is_rate_limited(self):
        closed = dict(self.t1)
        closed["status"] = "done"
        stale = dict(self.t2)
        stale["status"] = "waiting"
        fresh = dict(self.t2)
        fresh["status"] = "done"
        fresh["updated_at"] = _iso(_now())
        view = views.build_summaries([closed, stale])
        fake = _SummariesBackendFake(view, [closed, fresh])

        with mock.patch("fulcra_coord.io._warn"):
            first = self._run(fake, heal_missing_entries=True)
            first_downloads = fake.body_downloads
            second = self._run(fake, heal_missing_entries=True)

        self.assertEqual(
            {s["id"]: s for s in first}[self.t2["id"]]["status"], "done")
        self.assertEqual(
            {s["id"]: s for s in second}[self.t2["id"]]["status"], "waiting")
        self.assertEqual(
            fake.body_downloads, first_downloads,
            "nearby readers must not re-fetch every open row")

    def test_stale_view_falls_back_to_direct_listing(self):
        # Stale view KNOWS ONLY t1; t2's body landed on the bus but no view
        # refresh succeeded (the live 2026-06-10 failure shape). The direct
        # path must surface t2 anyway.
        view = views.build_summaries([self.t1])
        view["generated_at"] = _iso(_now() - timedelta(hours=2))
        fake = _SummariesBackendFake(view, [self.t1, self.t2])
        with mock.patch("fulcra_coord.io._warn") as warned:
            got = self._run(fake)
        self.assertEqual({s["id"] for s in got}, {self.t1["id"], self.t2["id"]})
        self.assertGreaterEqual(fake.list_calls, 1, "stale view must list tasks/ directly")
        self.assertTrue(warned.called, "staleness must be visible, not silent")
        self.assertIn("stale", warned.call_args_list[0].args[0])

    def test_stale_view_can_skip_direct_listing_for_listener_ticks(self):
        # notify-inbox's scheduled listener path must be bounded: when a view is
        # stale, it may serve the stale aggregate for this tick rather than
        # spawning the full direct-listing fallback.
        view = views.build_summaries([self.t1])
        view["generated_at"] = _iso(_now() - timedelta(hours=2))
        fake = _SummariesBackendFake(view, [self.t1, self.t2])
        with mock.patch("fulcra_coord.io._warn") as warned:
            got = self._run(fake, skip_stale_fallback=True)
        self.assertEqual({s["id"] for s in got}, {self.t1["id"]})
        self.assertEqual(fake.list_calls, 0, "listener skip must not list tasks/")
        self.assertEqual(fake.body_downloads, 0,
                         "listener skip must not fetch task bodies")
        self.assertTrue(warned.called, "stale-but-skipped read should be visible")
        self.assertIn("without direct-listing fallback",
                      warned.call_args_list[0].args[0])

    def test_stale_view_directive_surfaces_in_inbox(self):
        # End-to-end through the inbox read path: a directive ADDRESSED TO ME
        # that the stale view never heard of must still reach my inbox.
        from fulcra_coord.inbox import _load_inbox
        view = views.build_summaries([self.t1])
        view["generated_at"] = _iso(_now() - timedelta(hours=2))
        fake = _SummariesBackendFake(view, [self.t1, self.t2])
        patches = self._patched(fake)
        for p in patches:
            p.start()
        try:
            with mock.patch("fulcra_coord.io._warn"):
                items = _load_inbox("me:h:r", backend=["false"])
        finally:
            for p in patches:
                p.stop()
        self.assertIn(self.t2["id"], {i["id"] for i in items},
                      "directive invisible in the stale view must surface in inbox")

    def test_fresh_incomplete_view_directive_surfaces_in_inbox(self):
        # End-to-end for the listener failure: a fresh summaries view can still
        # be incomplete after write races. The inbox delivery path opts into
        # missing-entry healing so concrete assignees do not see a false empty
        # inbox.
        from fulcra_coord.inbox import _load_inbox
        view = views.build_summaries([self.t1])
        fake = _SummariesBackendFake(view, [self.t1, self.t2])
        patches = self._patched(fake)
        for p in patches:
            p.start()
        try:
            with mock.patch("fulcra_coord.io._warn"):
                items = _load_inbox("me:h:r", backend=["false"])
        finally:
            for p in patches:
                p.stop()
        self.assertIn(self.t2["id"], {i["id"] for i in items},
                      "directive missing from a fresh view must surface in inbox")

    def test_fresh_incomplete_view_surfaces_through_inbox_command(self):
        from fulcra_coord import cli
        view = views.build_summaries([self.t1])
        fake = _SummariesBackendFake(view, [self.t1, self.t2])
        patches = self._patched(fake)
        for p in patches:
            p.start()
        try:
            buf = io.StringIO()
            args = types.SimpleNamespace(
                agent="me:h:r", format="json", ack=None, all=False)
            with mock.patch("fulcra_coord.io._warn"), \
                 contextlib.redirect_stdout(buf):
                rc = cli.cmd_inbox(args, backend=["false"])
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        self.assertIn(self.t2["id"], {i["id"] for i in payload["inbox"]},
                      "cmd_inbox must not report a false empty inbox")

    def test_fresh_stale_open_row_does_not_surface_through_status_command(self):
        from fulcra_coord import cli
        stale = dict(self.t2)
        stale["status"] = "waiting"
        fresh = dict(self.t2)
        fresh["status"] = "done"
        fresh["updated_at"] = _iso(_now())
        view = views.build_summaries([self.t1, stale])
        fake = _SummariesBackendFake(view, [self.t1, fresh])
        patches = self._patched(fake)
        for p in patches:
            p.start()
        try:
            buf = io.StringIO()
            args = types.SimpleNamespace(
                format="json", workstream=None, agent=None)
            with mock.patch("fulcra_coord.io._warn"), \
                 contextlib.redirect_stdout(buf):
                rc = cli.cmd_status(args, backend=["false"])
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(rc, 0)
        payload = json.loads(buf.getvalue())
        active_ids = {i["id"] for i in payload["active"]}
        self.assertNotIn(self.t2["id"], active_ids,
                         "cmd_status must not show body-done tasks as open")

    def test_direct_listing_does_not_resurrect_unlisted_cached_tasks(self):
        # The stale fallback is driven by the authoritative raw tasks/ listing.
        # If it seeds from *all* local cache entries, a locally cached task that
        # is no longer listed remotely can reappear in inbox/status during the
        # very fallback meant to bypass stale views.
        from fulcra_coord import cache
        stale_cached = _task("stale local cache only", assignee="me:h:r")
        view = views.build_summaries([self.t1])
        view["generated_at"] = _iso(_now() - timedelta(hours=2))
        fake = _SummariesBackendFake(view, [self.t1, self.t2])

        with tempfile.TemporaryDirectory() as td, \
             mock.patch.dict(os.environ, {"XDG_CACHE_HOME": td}):
            cache.write_cached_task(stale_cached)

            with mock.patch("fulcra_coord.io._warn"):
                got = self._run(fake)

        ids = {s["id"] for s in got}
        self.assertEqual(ids, {self.t1["id"], self.t2["id"]})
        self.assertNotIn(stale_cached["id"], ids)

    def test_absent_generated_at_keeps_fast_path(self):
        # Old bus: the aggregate predates the stamp → trust it (back-compat).
        view = views.build_summaries([self.t1])
        view.pop("generated_at", None)
        view["updated_at"] = _iso(_now() - timedelta(hours=6))
        fake = _SummariesBackendFake(view, [self.t1, self.t2])
        got = self._run(fake)
        self.assertEqual({s["id"] for s in got}, {self.t1["id"]})
        self.assertEqual(fake.list_calls, 0)

    def test_env_knob_zero_disables_guard(self):
        view = views.build_summaries([self.t1])
        view["generated_at"] = _iso(_now() - timedelta(hours=2))
        fake = _SummariesBackendFake(view, [self.t1, self.t2])
        with mock.patch.dict(os.environ, {"FULCRA_COORD_VIEW_STALE_MIN": "0"}):
            got = self._run(fake)
        self.assertEqual({s["id"] for s in got}, {self.t1["id"]})
        self.assertEqual(fake.list_calls, 0)

    def test_direct_listing_failure_falls_back_to_stale_view(self):
        # Degraded, not blind: listing blew up → use the stale view, warn louder.
        view = views.build_summaries([self.t1])
        view["generated_at"] = _iso(_now() - timedelta(hours=2))
        fake = _SummariesBackendFake(view, [self.t1, self.t2])

        def boom(prefix, *, backend=None, timeout=None):
            raise RuntimeError("backend list outage")

        with mock.patch("fulcra_coord.remote.download_json",
                        side_effect=fake.download_json), \
             mock.patch("fulcra_coord.remote.list_files", side_effect=boom), \
             mock.patch("fulcra_coord.io._warn") as warned:
            got = _load_task_summaries(backend=["false"])
        self.assertEqual({s["id"] for s in got}, {self.t1["id"]},
                         "stale view beats NO view when the direct path fails")
        self.assertGreaterEqual(warned.call_count, 2,
                                "listing failure must warn louder, not go silent")

    def test_failed_direct_listing_releases_fallback_claim(self):
        # The stampede breaker is a concurrency claim, not a rate limiter. If a
        # fallback attempt fails, the next tick must be allowed to try again
        # immediately instead of waiting for stale takeover.
        view = views.build_summaries([self.t1])
        view["generated_at"] = _iso(_now() - timedelta(hours=2))
        fake = _SummariesBackendFake(view, [self.t1, self.t2])

        def boom(prefix, *, backend=None, timeout=None):
            raise RuntimeError("backend list outage")

        with tempfile.TemporaryDirectory() as td, \
             mock.patch.dict(os.environ, {"XDG_CACHE_HOME": td}), \
             mock.patch("fulcra_coord.remote.download_json",
                        side_effect=fake.download_json), \
             mock.patch("fulcra_coord.remote.list_files", side_effect=boom), \
             mock.patch("fulcra_coord.io._warn"):
            got = _load_task_summaries(backend=["false"])
            self.assertEqual({s["id"] for s in got}, {self.t1["id"]})
            self.assertFalse(
                cache.fallback_throttle_path().exists(),
                "failed direct-listing fallback must release the host claim")

    def test_empty_direct_listing_falls_back_to_stale_view(self):
        # An empty listing is indistinguishable from a backend without a working
        # `list` — never downgrade stale data to NO data.
        view = views.build_summaries([self.t1])
        view["generated_at"] = _iso(_now() - timedelta(hours=2))
        fake = _SummariesBackendFake(view, [])  # listing returns nothing
        with mock.patch("fulcra_coord.io._warn"):
            got = self._run(fake)
        self.assertEqual({s["id"] for s in got}, {self.t1["id"]})


class TestPresenceStaleGuard(unittest.TestCase):
    """Contract 2+3 for the presence aggregate used by liveness routing."""

    def _stale_aggregate(self, agent, last_seen_hours_ago):
        agg = views.build_presence([
            schema.make_presence(
                agent, last_seen=_iso(_now() - timedelta(hours=last_seen_hours_ago)),
                capabilities=["review"]),
        ])
        agg["generated_at"] = _iso(_now() - timedelta(hours=2))
        return agg

    def test_stale_aggregate_reads_per_agent_records(self):
        # The live 2026-06-10 shape: the reviewer's per-agent record is FRESH
        # (it heartbeats fine), but the aggregate hasn't uploaded for hours, so
        # the stored roster says it was last seen 3h ago (below routing floor).
        from fulcra_coord.presence import _load_presence_agents
        reviewer = "rev:h:r"
        agg = self._stale_aggregate(reviewer, last_seen_hours_ago=3)
        fresh_record = schema.make_presence(reviewer, capabilities=["review"])

        def fake_list_json(prefix, *, backend=None, **kw):
            assert prefix == remote.presence_prefix()
            return [(f"{prefix}rev-h-r.json", fresh_record)], True

        with mock.patch("fulcra_coord.remote.download_json", return_value=agg), \
             mock.patch("fulcra_coord.remote.list_json_checked",
                        side_effect=fake_list_json), \
             mock.patch("fulcra_coord.presence._warn") as warned:
            agents = _load_presence_agents(backend=["false"])
        self.assertTrue(warned.called)
        # The liveness check the router runs must now see the agent live.
        winner = views.resolve_live_recipient([reviewer], agents, floor="idle")
        self.assertEqual(winner, reviewer,
                         "a live reviewer must not be invisible behind a stale aggregate")

    def test_missing_aggregate_reads_per_agent_records(self):
        # A partial connect/reconcile can leave the durable per-agent presence
        # record present while the aggregate is missing. Liveness-sensitive
        # readers must use the durable records instead of treating the whole
        # roster as empty.
        from fulcra_coord.presence import _load_presence_agents
        reviewer = "rev:h:r"
        fresh_record = schema.make_presence(reviewer, capabilities=["review"])

        def fake_list_json(prefix, *, backend=None, **kw):
            assert prefix == remote.presence_prefix()
            return [(f"{prefix}rev-h-r.json", fresh_record)], True

        with mock.patch("fulcra_coord.remote.download_json", return_value=None), \
             mock.patch("fulcra_coord.remote.list_json_checked",
                        side_effect=fake_list_json), \
             mock.patch("fulcra_coord.presence._warn") as warned:
            agents = _load_presence_agents(backend=["false"])
        self.assertTrue(warned.called)
        winner = views.resolve_live_recipient([reviewer], agents, floor="idle")
        self.assertEqual(winner, reviewer,
                         "a live reviewer must not be invisible behind a missing aggregate")

    def test_fresh_aggregate_skips_listing(self):
        from fulcra_coord.presence import _load_presence_agents
        agg = views.build_presence([schema.make_presence("rev:h:r")])  # fresh
        with mock.patch("fulcra_coord.remote.download_json", return_value=agg), \
             mock.patch("fulcra_coord.remote.list_json_checked") as listed:
            agents = _load_presence_agents(backend=["false"])
        self.assertEqual([a["agent"] for a in agents], ["rev:h:r"])
        listed.assert_not_called()

    def test_absent_generated_at_keeps_aggregate(self):
        from fulcra_coord.presence import _load_presence_agents
        agg = views.build_presence([schema.make_presence("rev:h:r")])
        agg.pop("generated_at", None)
        agg["updated_at"] = _iso(_now() - timedelta(hours=9))
        with mock.patch("fulcra_coord.remote.download_json", return_value=agg), \
             mock.patch("fulcra_coord.remote.list_json_checked") as listed:
            agents = _load_presence_agents(backend=["false"])
        self.assertEqual([a["agent"] for a in agents], ["rev:h:r"])
        listed.assert_not_called()

    def test_listing_failure_falls_back_to_stale_aggregate(self):
        from fulcra_coord.presence import _load_presence_agents
        reviewer = "rev:h:r"
        agg = self._stale_aggregate(reviewer, last_seen_hours_ago=3)

        def boom(prefix, *, backend=None, **kw):
            raise RuntimeError("backend list outage")

        with mock.patch("fulcra_coord.remote.download_json", return_value=agg), \
             mock.patch("fulcra_coord.remote.list_json_checked", side_effect=boom), \
             mock.patch("fulcra_coord.presence._warn") as warned:
            agents = _load_presence_agents(backend=["false"])
        self.assertEqual([a["agent"] for a in agents], [reviewer],
                         "stale roster beats NO roster when the direct path fails")
        self.assertGreaterEqual(warned.call_count, 2)

    def test_request_review_sees_live_reviewer_through_stale_aggregate(self):
        # End-to-end: `request-review --dry-run` must pick the reviewer whose
        # per-agent record is live even though the aggregate is hours stale —
        # the exact "no reviewer live while the reviewer WAS live" failure.
        from fulcra_coord.routing_ops import cmd_request_review
        reviewer = "rev:h:r"
        agg = self._stale_aggregate(reviewer, last_seen_hours_ago=3)
        fresh_record = schema.make_presence(reviewer, capabilities=["review"])

        def fake_list_json(prefix, *, backend=None, **kw):
            return [(f"{prefix}rev-h-r.json", fresh_record)], True

        import io as _io
        from contextlib import redirect_stdout
        buf = _io.StringIO()
        args = types.SimpleNamespace(pr="123", repo=None, dry_run=True,
                                     format="json", agent="author:h:r",
                                     candidate_list=None)
        with mock.patch("fulcra_coord.remote.download_json", return_value=agg), \
             mock.patch("fulcra_coord.remote.list_json_checked",
                        side_effect=fake_list_json), \
             mock.patch("fulcra_coord.presence._warn"), \
             redirect_stdout(buf):
            rc = cmd_request_review(args, backend=["false"])
        self.assertEqual(rc, 0)
        report = json.loads(buf.getvalue())
        self.assertEqual(report["winner"], reviewer)

    def test_agents_surface_uses_stale_guarded_presence(self):
        # `agents` is the operator's "what is everyone doing" surface. It must not
        # show a stale presence-only roster while fresh per-agent records exist.
        from fulcra_coord.query import cmd_agents
        reviewer = "rev:h:r"
        agg = self._stale_aggregate(reviewer, last_seen_hours_ago=3)
        fresh_record = schema.make_presence(
            reviewer, workstreams=["review"], summary="fresh review loop")

        def fake_list_json(prefix, *, backend=None, **kw):
            return [(f"{prefix}rev-h-r.json", fresh_record)], True

        import io as _io
        from contextlib import redirect_stdout
        buf = _io.StringIO()
        args = types.SimpleNamespace(mine=None, format="json")
        with mock.patch("fulcra_coord.query._load_task_summaries", return_value=[]), \
             mock.patch("fulcra_coord.remote.download_json", return_value=agg), \
             mock.patch("fulcra_coord.remote.list_json_checked",
                        side_effect=fake_list_json), \
             mock.patch("fulcra_coord.presence._warn"), \
             redirect_stdout(buf):
            rc = cmd_agents(args, backend=["false"])
        self.assertEqual(rc, 0)
        report = json.loads(buf.getvalue())
        presence_only = {a["agent"]: a for a in report["presence_only"]}
        self.assertIn(reviewer, presence_only)
        self.assertEqual(presence_only[reviewer]["summary"], "fresh review loop")
        self.assertEqual(presence_only[reviewer]["liveness"], "live")

    def test_resume_surface_uses_stale_guarded_presence(self):
        # Resume's "other agents" room-state section must also read fresh per-agent
        # records when the aggregate is stale.
        from fulcra_coord.query import cmd_resume
        me = "me:h:r"
        reviewer = "rev:h:r"
        agg = self._stale_aggregate(reviewer, last_seen_hours_ago=3)
        fresh_record = schema.make_presence(
            reviewer, workstreams=["review"], summary="fresh review loop")

        def fake_list_json(prefix, *, backend=None, **kw):
            return [(f"{prefix}rev-h-r.json", fresh_record)], True

        import io as _io
        from contextlib import redirect_stdout
        buf = _io.StringIO()
        args = types.SimpleNamespace(agent=me, format="json",
                                     with_continuity=False)
        with mock.patch("fulcra_coord.query._load_task_summaries", return_value=[]), \
             mock.patch("fulcra_coord.remote.download_json", return_value=agg), \
             mock.patch("fulcra_coord.remote.list_json_checked",
                        side_effect=fake_list_json), \
             mock.patch("fulcra_coord.presence._warn"), \
             redirect_stdout(buf):
            rc = cmd_resume(args, backend=["false"])
        self.assertEqual(rc, 0)
        report = json.loads(buf.getvalue())
        other = {a["agent"]: a for a in report["other_agents"]}
        self.assertIn(reviewer, other)
        self.assertEqual(other[reviewer]["summary"], "fresh review loop")
        self.assertEqual(other[reviewer]["liveness"], "live")

    def test_digest_surface_uses_stale_guarded_presence(self):
        # The scheduled operator digest is another "what are agents doing" surface;
        # it should not summarize stale aggregate liveness when fresh records exist.
        from fulcra_coord.digest import cmd_digest
        reviewer = "rev:h:r"
        agg = self._stale_aggregate(reviewer, last_seen_hours_ago=3)
        fresh_record = schema.make_presence(
            reviewer, workstreams=["review"], summary="fresh review loop")

        def fake_list_json(prefix, *, backend=None, **kw):
            return [(f"{prefix}rev-h-r.json", fresh_record)], True

        import io as _io
        from contextlib import redirect_stdout
        buf = _io.StringIO()
        args = types.SimpleNamespace(window="ondemand", format="json",
                                     dry_run=False, human="ash")
        with mock.patch("fulcra_coord.digest._load_task_summaries", return_value=[]), \
             mock.patch("fulcra_coord.digest._assess_fleet", return_value=None), \
             mock.patch("fulcra_coord.digest._loop_board_summary", return_value=None), \
             mock.patch("fulcra_coord.remote.download_json", return_value=agg), \
             mock.patch("fulcra_coord.remote.list_json_checked",
                        side_effect=fake_list_json), \
             mock.patch("fulcra_coord.presence._warn"), \
             redirect_stdout(buf):
            rc = cmd_digest(args, backend=["false"])
        self.assertEqual(rc, 0)
        report = json.loads(buf.getvalue())
        per_agent = {a["agent"]: a for a in report["per_agent"]}
        self.assertIn(reviewer, per_agent)
        self.assertEqual(per_agent[reviewer]["summary"], "fresh review loop")
        self.assertEqual(per_agent[reviewer]["liveness"], "live")


if __name__ == "__main__":
    unittest.main()
