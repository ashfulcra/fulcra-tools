"""Per-host throttle on the stale-view direct-listing fallback — the
2026-06-11 SELF-SUSTAINING STAMPEDE fix.

THE DEFECT (live evidence, 2026-06-11): when the bus views go stale/broken,
``_load_task_summaries``' stale-view guard sends every reader to the
direct-listing fallback (``_load_all_tasks_by_listing``) — one listing plus
~450 per-task stat/body fetches at current bus size. On a host running
several listeners (the operator's Mac runs EIGHT, each with notify-inbox
ticks), every tick of every listener fell back SIMULTANEOUSLY: the host
saturated the API gateway with its own concurrent subprocesses (observed
15-18 concurrent fulcra-api calls around the clock; a single notify-inbox
tick running 40+ minutes), every call queued and timed out at the gateway,
the views could never repair, and the stampede sustained itself
indefinitely. The operator misread the result as a backend 504 outage —
twice.

THE CONTRACT under test:

1. Before entering the fallback, a caller must CLAIM a local per-host marker
   (``<cache>/roots/<slug>/fallback-throttle.json``). First claimant proceeds
   with the full fallback; a second caller while the marker is held gets the
   STALE CACHED summaries back (lesser evil vs joining the stampede) with a
   warn, and makes ZERO remote listing / body calls on the throttled path.
2. A marker older than ``FULCRA_COORD_FALLBACK_WINDOW_MINUTES`` (default 10)
   is stale — takeover (the holder crashed mid-fallback).
3. Fallback COMPLETION (success or failure) clears the marker: the window
   only guards concurrency, not rate across time; takeover handles crashes.
4. The reconcile path BYPASSES the throttle — its job is exactly to repair
   the views, so it must never be locked out by listener fallbacks.
5. The env knob is honored (wider window keeps a mid-aged marker authoritative;
   0/negative disables the throttle entirely).
"""

from __future__ import annotations

import json
import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from fulcra_coord import cache, remote, schema, views
from fulcra_coord import io as coord_io
from fulcra_coord.io import _load_task_summaries


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _task(title: str, *, assignee: str | None = None) -> dict:
    return schema.make_task(title=title, workstream="ws", agent="owner:h:r",
                            owner_agent="owner:h:r", assignee=assignee)


class _SummariesBackendFake:
    """Counting fake of the remote transport (same shape as the stale-guard
    tests): serves a summaries view + raw tasks/ listing + bodies, and counts
    list/body calls so a test can pin ZERO remote calls on the throttled path."""

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


class TestFallbackStampedeBreaker(unittest.TestCase):
    """The per-host claim/serve-stale/release lifecycle."""

    def setUp(self):
        self.t1 = _task("in the view")
        self.t2 = _task("missing from the stale view", assignee="me:h:r")
        stale_view = views.build_summaries([self.t1])
        stale_view["generated_at"] = _iso(_now() - timedelta(hours=2))
        self.stale_view = stale_view
        self.fake = _SummariesBackendFake(stale_view, [self.t1, self.t2])

    def _marker(self):
        return cache.fallback_throttle_path()

    def _hold_marker(self, age_minutes: float = 0.0, holder: str = "other-pid"):
        """Simulate another process mid-fallback: marker present, mtime aged."""
        path = self._marker()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"at": _iso(_now()), "holder": holder}))
        if age_minutes:
            ts = time.time() - age_minutes * 60.0
            os.utime(path, (ts, ts))

    def _run(self, **kwargs):
        with mock.patch("fulcra_coord.remote.download_json",
                        side_effect=self.fake.download_json), \
             mock.patch("fulcra_coord.remote.list_files",
                        side_effect=self.fake.list_files), \
             mock.patch("fulcra_coord.remote.stat", return_value=None), \
             mock.patch("fulcra_coord.io._warn") as warned:
            got = _load_task_summaries(backend=["false"], **kwargs)
        return got, warned

    # -- contract 1: first caller claims and falls back ----------------------

    def test_first_caller_claims_and_falls_back(self):
        got, _ = self._run()
        self.assertEqual({s["id"] for s in got}, {self.t1["id"], self.t2["id"]})
        self.assertGreaterEqual(self.fake.list_calls, 1)

    def test_second_caller_within_window_serves_stale_with_zero_remote_calls(self):
        # Marker freshly held by "another listener" mid-fallback: this caller
        # must NOT join the stampede — stale view back, warn, and ZERO
        # listing / per-task fetches (the pin that makes the breaker real).
        self._hold_marker(age_minutes=1.0)
        got, warned = self._run()
        self.assertEqual({s["id"] for s in got}, {self.t1["id"]},
                         "throttled caller must serve the stale view")
        self.assertEqual(self.fake.list_calls, 0,
                         "throttled path must not list tasks/")
        self.assertEqual(self.fake.body_downloads, 0,
                         "throttled path must not fetch task bodies")
        # The degradation is visible, names the rate limit, and carries the
        # holder age so an operator can tell throttle from outage.
        msgs = [c.args[0] for c in warned.call_args_list]
        self.assertTrue(any("stale" in m for m in msgs))
        self.assertTrue(any("rate-limit" in m or "throttl" in m for m in msgs),
                        f"warn must say the fallback is rate-limited: {msgs}")
        self.assertTrue(any("1" in m and "m" in m for m in msgs),
                        f"warn must include the holder age: {msgs}")
        # The held marker belongs to the other process — a throttled caller
        # must never release it.
        self.assertTrue(self._marker().exists())

    # -- contract 2: stale takeover ------------------------------------------

    def test_marker_older_than_window_is_taken_over(self):
        # Holder crashed 30m ago (window default 10m): the marker is stale,
        # this caller reclaims it and runs the full fallback.
        self._hold_marker(age_minutes=30.0)
        got, _ = self._run()
        self.assertEqual({s["id"] for s in got}, {self.t1["id"], self.t2["id"]})
        self.assertGreaterEqual(self.fake.list_calls, 1)

    def test_stale_original_holder_cannot_release_takeover_marker(self):
        # Ownership token pin: if a slow-but-live fallback exceeds the takeover
        # window, another process may reclaim the stale marker. When the
        # original finally finishes, it must NOT unlink the replacement
        # holder's marker, or a third listener can join the still-running
        # replacement fallback and restart the stampede.
        claimed, _, original_token = coord_io._claim_fallback_throttle(10.0)
        self.assertTrue(claimed)
        self.assertTrue(original_token)
        ts = time.time() - 30 * 60.0
        os.utime(self._marker(), (ts, ts))
        claimed, _, takeover_token = coord_io._claim_fallback_throttle(10.0)
        self.assertTrue(claimed)
        self.assertTrue(takeover_token)
        self.assertNotEqual(original_token, takeover_token)

        coord_io._release_fallback_throttle(original_token)

        self.assertTrue(self._marker().exists(),
                        "the replacement holder's marker must survive a stale "
                        "original holder's release")
        coord_io._release_fallback_throttle(takeover_token)
        self.assertFalse(self._marker().exists())

    # -- contract 3: completion releases --------------------------------------

    def test_completion_clears_marker_on_success(self):
        self._run()
        self.assertFalse(self._marker().exists(),
                         "a completed fallback must release the claim")

    def test_completion_clears_marker_on_listing_failure(self):
        def boom(prefix, *, backend=None, timeout=None):
            raise RuntimeError("backend list outage")

        with mock.patch("fulcra_coord.remote.download_json",
                        side_effect=self.fake.download_json), \
             mock.patch("fulcra_coord.remote.list_files", side_effect=boom), \
             mock.patch("fulcra_coord.remote.stat", return_value=None), \
             mock.patch("fulcra_coord.io._warn"):
            got = _load_task_summaries(backend=["false"])
        # Degraded-not-blind behavior unchanged: stale view comes back...
        self.assertEqual({s["id"] for s in got}, {self.t1["id"]})
        # ...and the claim is released so the NEXT tick can retry, instead of
        # a failed fallback wedging the host until stale takeover.
        self.assertFalse(self._marker().exists())

    # -- contract 4: reconcile bypasses ---------------------------------------

    def test_bypass_param_ignores_a_held_marker(self):
        self._hold_marker(age_minutes=1.0)
        got, _ = self._run(bypass_fallback_throttle=True)
        self.assertEqual({s["id"] for s in got}, {self.t1["id"], self.t2["id"]},
                         "the bypass caller must run the full fallback")
        self.assertGreaterEqual(self.fake.list_calls, 1)
        # Bypass never claimed, so it must not release the listener's marker.
        self.assertTrue(self._marker().exists())

    def test_reconcile_rebuild_source_bypasses_throttle(self):
        # The reconcile path's self-loading ack source must never be locked
        # out by listener fallbacks — reconcile exists to REPAIR the views.
        from fulcra_coord.cli import _reconcile_rebuild_source_preserving_acks
        self._hold_marker(age_minutes=1.0)
        with mock.patch("fulcra_coord.remote.download_json",
                        side_effect=self.fake.download_json), \
             mock.patch("fulcra_coord.remote.list_files",
                        side_effect=self.fake.list_files), \
             mock.patch("fulcra_coord.remote.stat", return_value=None), \
             mock.patch("fulcra_coord.io._warn"):
            _reconcile_rebuild_source_preserving_acks(
                [self.t1], backend=["false"])
        self.assertGreaterEqual(
            self.fake.list_calls, 1,
            "reconcile's ack-source load must bypass the fallback throttle")

    # -- contract 5: env knob --------------------------------------------------

    def test_env_knob_widens_the_window(self):
        # 30m-old marker would be STALE at the 10m default; with a 60m window
        # it is still authoritative — the caller stays throttled.
        self._hold_marker(age_minutes=30.0)
        with mock.patch.dict(os.environ,
                             {"FULCRA_COORD_FALLBACK_WINDOW_MINUTES": "60"}):
            got, _ = self._run()
        self.assertEqual({s["id"] for s in got}, {self.t1["id"]})
        self.assertEqual(self.fake.list_calls, 0)

    def test_env_knob_tightens_the_window(self):
        # 5m-old marker is fresh at the default but stale at a 2m window.
        self._hold_marker(age_minutes=5.0)
        with mock.patch.dict(os.environ,
                             {"FULCRA_COORD_FALLBACK_WINDOW_MINUTES": "2"}):
            got, _ = self._run()
        self.assertEqual({s["id"] for s in got}, {self.t1["id"], self.t2["id"]})
        self.assertGreaterEqual(self.fake.list_calls, 1)

    def test_env_knob_zero_disables_the_throttle(self):
        # Operator escape hatch, mirroring FULCRA_COORD_VIEW_STALE_MIN=0:
        # a non-positive window disables the breaker entirely.
        self._hold_marker(age_minutes=0.0)
        with mock.patch.dict(os.environ,
                             {"FULCRA_COORD_FALLBACK_WINDOW_MINUTES": "0"}):
            got, _ = self._run()
        self.assertEqual({s["id"] for s in got}, {self.t1["id"], self.t2["id"]})
        self.assertGreaterEqual(self.fake.list_calls, 1)

    # -- placement -------------------------------------------------------------

    def test_marker_is_per_remote_root(self):
        # The cache dir is per-host (XDG cache, keyed by OS user — all of this
        # host's listeners share it) and scoped per remote ROOT: throttling
        # one bus's fallback must never gate a different bus on the same host.
        p_default = cache.fallback_throttle_path()
        with mock.patch.dict(os.environ,
                             {"FULCRA_COORD_REMOTE_ROOT": "/coordination-demo"}):
            p_demo = cache.fallback_throttle_path()
        self.assertNotEqual(p_default, p_demo)
        self.assertTrue(str(p_default).startswith(str(cache.cache_root())))


if __name__ == "__main__":
    unittest.main()
