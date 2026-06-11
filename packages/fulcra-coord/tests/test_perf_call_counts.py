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


# ---------------------------------------------------------------------------
# E2 — loop-record sweeps: filter paths BEFORE downloading
# ---------------------------------------------------------------------------

def _seed_loop_with_sublogs(backend, i: int) -> dict:
    """One top-level loop record plus ack + routing + response sub-log shards
    under the same prefix — the layout that made download-then-filter pay a
    subprocess for every shard on every listener tick and board render."""
    from fulcra_coord import directives
    t = schema.make_task(title=f"d{i}", workstream="ws", agent="a",
                         assignee="peer:h:r")
    remote.upload_json(t, remote.task_remote_path(t["id"]), backend=backend)
    d = directives.directive_from_task(t)
    remote.upload_json(d, remote.directive_remote_path(d["id"]), backend=backend)
    directives.write_directive_ack(d["id"], "peer:h:r", backend=backend)
    directives.append_directive_route(
        d["id"], {"event_id": f"e{i}", "at": "2026-01-01T00:00:00Z"},
        backend=backend)
    loop_ops.append_loop_response(
        d["id"], {"by": "peer:h:r", "outcome": {"verdict": "done"}},
        backend=backend)
    return d


def _no_sublog_shards(paths: list[str]) -> bool:
    return all("/acks/" not in p and "/routing/" not in p
               and "/responses/" not in p and "/evidence/" not in p
               for p in paths)


def test_load_loop_records_downloads_only_top_level_records(coord_backend):
    """The directives prefix holds 3 sub-log shards per loop beside each
    top-level record; load_loop_records used to download ALL of them (via
    list_json) and throw the shards away. The paths must be filtered BEFORE
    downloading: downloads under the prefix == top-level record count."""
    seeded = [_seed_loop_with_sublogs(coord_backend, i) for i in range(2)]
    prefix = remote.directives_prefix()
    # Sanity: the prefix really holds one record + 3 shards per loop.
    assert len(remote.list_files(prefix, backend=coord_backend)) == 8
    counter = OpCounter()
    with counter.patch():
        records = loop_ops.load_loop_records(backend=coord_backend)
    assert {r["id"] for r in records} == {d["id"] for d in seeded}
    assert sorted(counter.downloads_under(prefix)) == sorted(
        remote.directive_remote_path(d["id"]) for d in seeded)


def test_forge_mirror_sweep_downloads_only_top_level_records(coord_backend):
    """forge_mirror's sweep now rides load_loop_records — same pin."""
    import types
    from fulcra_coord import forge_mirror
    _seed_loop_with_sublogs(coord_backend, 0)
    prefix = remote.directives_prefix()
    counter = OpCounter()
    with counter.patch():
        forge_mirror.cmd_forge_mirror(
            types.SimpleNamespace(format="json", repo=None),
            backend=coord_backend)
    assert _no_sublog_shards(counter.downloads_under(prefix)), counter.downloads


def test_overdue_loop_suffix_downloads_only_top_level_records(coord_backend):
    """inbox's overdue-loop suffix (paid on every notifying listener tick) now
    rides load_loop_records — same pin."""
    from fulcra_coord import inbox
    _seed_loop_with_sublogs(coord_backend, 0)
    prefix = remote.directives_prefix()
    counter = OpCounter()
    with counter.patch():
        inbox._overdue_loop_suffix("someone:h:r", backend=coord_backend)
    assert _no_sublog_shards(counter.downloads_under(prefix)), counter.downloads
