"""Counting-fake pins for the measured perf refactor (2026-06-10 night pass).

THE MEASURED PROBLEM: every remote operation is one `fulcra file` subprocess
spawn (~1.3s median). A reconcile tick was measured at ~3,105 spawns on the
production bus (~440 tasks) — the parity check alone re-downloaded every task
body cmd_reconcile already held and double-listed every event prefix. These
tests pin the *call counts* of the fixed paths with counting wrappers around
the remote layer, so a regression that quietly reintroduces N extra spawns per
tick fails loudly here instead of surfacing as a 90s-timeout incident.

Each wrapper DELEGATES to the real (fake-backend) implementation — semantics
stay end-to-end real; only the counts are observed.
"""

from __future__ import annotations

import os
import time
from unittest import mock

from fulcra_coord import cli, eventlog, events, io, loop_ops, remote, schema


# ---------------------------------------------------------------------------
# Counting helpers
# ---------------------------------------------------------------------------

class OpCounter:
    """Wrap remote.list_files / remote.download_json / remote.stat with
    delegating counters. Counts are keyed by (op, path-or-prefix) so a test can
    assert per-prefix discipline (e.g. one event listing per task)."""

    def __init__(self):
        self.lists: list[str] = []
        self.downloads: list[str] = []
        self.stats: list[str] = []

    def patch(self):
        real_list, real_dl, real_stat = (
            remote.list_files, remote.download_json, remote.stat)

        def list_files(prefix, **kw):
            self.lists.append(prefix)
            return real_list(prefix, **kw)

        def download_json(path, **kw):
            self.downloads.append(path)
            return real_dl(path, **kw)

        def stat(path, **kw):
            self.stats.append(path)
            return real_stat(path, **kw)

        return mock.patch.multiple(
            remote, list_files=list_files, download_json=download_json,
            stat=stat)

    def downloads_under(self, prefix: str) -> list[str]:
        return [p for p in self.downloads if p.startswith(prefix)]

    def lists_of(self, prefix: str) -> list[str]:
        return [p for p in self.lists if p == prefix]


def _seed_task_with_events(backend, i: int) -> dict:
    t = schema.make_task(title=f"t{i}", workstream="ws", agent="a")
    t["status"] = "active"
    remote.upload_json(t, remote.task_remote_path(t["id"]), backend=backend)
    eventlog.append_event(
        events.make_event(family="tasks", task_id=t["id"], kind="start",
                          actor="a", payload=dict(t)),
        backend=backend)
    return t


# ---------------------------------------------------------------------------
# E1a — parity reuses the bodies cmd_reconcile already holds
# ---------------------------------------------------------------------------

def test_parity_with_all_tasks_never_redownloads_task_bodies(coord_backend):
    """When cmd_reconcile passes its in-hand all_tasks, the parity check must
    perform ZERO task-body downloads and ZERO tasks/ listings — those ~440
    re-downloads per tick were the single biggest measured waste."""
    tasks = [_seed_task_with_events(coord_backend, i) for i in range(3)]
    counter = OpCounter()
    with counter.patch():
        report = cli._event_parity_check(tasks, backend=coord_backend)
    tasks_prefix = f"{remote.remote_root()}/tasks/"
    assert counter.downloads_under(tasks_prefix) == []
    assert counter.lists_of(tasks_prefix) == []
    assert report["checked"] == 3
    assert report["tasks_total"] == 3
    assert report["drift"] == 0


def test_parity_fallback_load_matches_passed_bodies(coord_backend):
    """Direct callers (no all_tasks) still get the full report — the load
    fallback must produce the same verdicts as the passed-bodies path."""
    tasks = [_seed_task_with_events(coord_backend, i) for i in range(2)]
    direct = cli._event_parity_check(backend=coord_backend)
    passed = cli._event_parity_check(tasks, backend=coord_backend)
    assert direct == passed


# ---------------------------------------------------------------------------
# E1b — one event listing per task (the double-list elimination)
# ---------------------------------------------------------------------------

def test_parity_lists_each_event_prefix_exactly_once(coord_backend):
    """read_events used to list every event prefix TWICE (once inside
    list_json, once for drop detection). One listing must now serve both."""
    tasks = [_seed_task_with_events(coord_backend, i) for i in range(3)]
    counter = OpCounter()
    with counter.patch():
        cli._event_parity_check(tasks, backend=coord_backend)
    for t in tasks:
        prefix = remote.events_prefix(t["id"])
        assert len(counter.lists_of(prefix)) == 1, (
            f"event prefix for {t['id']} listed "
            f"{len(counter.lists_of(prefix))}x — must be exactly once")


def test_read_events_one_listing_per_call(coord_backend):
    """The same pin at the eventlog layer, independent of parity."""
    t = _seed_task_with_events(coord_backend, 0)
    counter = OpCounter()
    with counter.patch():
        evs = eventlog.read_events(t["id"], backend=coord_backend)
    assert len(evs) == 1
    assert len(counter.lists_of(remote.events_prefix(t["id"]))) == 1


# ---------------------------------------------------------------------------
# E1c — sampling: a rotating window of FULCRA_COORD_PARITY_SAMPLE tasks/tick
# ---------------------------------------------------------------------------

def test_parity_sample_honors_window_size(coord_backend):
    """With 5 tasks and a sample of 2, exactly 2 event prefixes are probed per
    call (full coverage arrives via rotation, not via one mega-tick)."""
    tasks = [_seed_task_with_events(coord_backend, i) for i in range(5)]
    counter = OpCounter()
    with mock.patch.dict(os.environ, {"FULCRA_COORD_PARITY_SAMPLE": "2"}):
        with counter.patch():
            report = cli._event_parity_check(tasks, backend=coord_backend)
    probed = [t for t in tasks
              if counter.lists_of(remote.events_prefix(t["id"]))]
    assert len(probed) == 2
    assert report["sampled"] == 2
    assert report["tasks_total"] == 5  # population stays the true denominator


def test_parity_sample_rotates_to_full_coverage(coord_backend):
    """Successive ticks advance a persisted cursor: with 5 tasks and sample=2,
    three ticks must collectively probe every task (coverage every ~ceil(N/S)
    ticks — the property that makes sampling safe to run forever)."""
    tasks = [_seed_task_with_events(coord_backend, i) for i in range(5)]
    seen: set[str] = set()
    with mock.patch.dict(os.environ, {"FULCRA_COORD_PARITY_SAMPLE": "2"}):
        for _ in range(3):
            counter = OpCounter()
            with counter.patch():
                cli._event_parity_check(tasks, backend=coord_backend)
            seen |= {t["id"] for t in tasks
                     if counter.lists_of(remote.events_prefix(t["id"]))}
    assert seen == {t["id"] for t in tasks}


def test_parity_sample_disabled_checks_everything(coord_backend):
    """A sample size >= population (or the small-bus default) checks all."""
    tasks = [_seed_task_with_events(coord_backend, i) for i in range(3)]
    with mock.patch.dict(os.environ, {"FULCRA_COORD_PARITY_SAMPLE": "50"}):
        report = cli._event_parity_check(tasks, backend=coord_backend)
    assert report["checked"] == 3
    assert report["sampled"] == 3


# ---------------------------------------------------------------------------
# E1d — deadline gate: a spent reconcile budget stops the pass
# ---------------------------------------------------------------------------

def test_parity_deadline_already_spent_probes_nothing(coord_backend):
    """With reconcile's deadline already past, the pass must not start ANY
    per-task probe (mirrors _run_retention's budget-floor discipline)."""
    tasks = [_seed_task_with_events(coord_backend, i) for i in range(3)]
    counter = OpCounter()
    with counter.patch():
        report = cli._event_parity_check(
            tasks, backend=coord_backend, deadline=time.monotonic() - 10)
    for t in tasks:
        assert counter.lists_of(remote.events_prefix(t["id"])) == []
    assert report["checked"] == 0
    assert report["deferred"] == 3


def test_parity_no_deadline_keeps_unbounded_behavior(coord_backend):
    """deadline=None (direct callers/tests) keeps the old unbounded pass."""
    tasks = [_seed_task_with_events(coord_backend, i) for i in range(2)]
    report = cli._event_parity_check(tasks, backend=coord_backend)
    assert report["checked"] == 2
    assert report["deferred"] == 0
