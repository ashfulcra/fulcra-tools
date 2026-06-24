"""Task 3 (profiling-first re-scope): pins for the reconcile sub-pass tail.

The Task 3 plan HYPOTHESIZED that the ~57s sub-pass tail was redundant snapshot
I/O the tick already held (presence / summaries / the directives prefix). The
profile (profile_reconcile.py) showed that the E4/E5 tick-scoped snapshot sharing
ALREADY eliminated those re-downloads, and that the dominant recurring tail cost
is GENUINE per-item sub-log I/O (directive-parity's per-record ack sub-log read,
load_loop_records' per-record download, event-parity's per-sampled-task event
read) — none of which a shared snapshot can remove without changing each pass's
correctness contract.

These tests therefore pin the two things the re-scoped task actually delivered:

  1. SNAPSHOT-SHARING INVARIANT (the brief's test pattern): one reconcile tick
     loads each shared snapshot at most the documented number of times — proving
     the E4 sharing the hypothesis depended on is in place and stays in place
     (a regression that reintroduced a per-pass re-download would fail here).
  2. PER-PASS PHASE TIMINGS: the permanent fine-grained diagnostics — every
     sub-pass gets its own ``pass_*`` entry in phase_timings_ms, the coarse
     load/views/subpasses phases are preserved, and ``subpasses`` equals the sum
     of the per-pass slices.
"""
from __future__ import annotations

import types
from unittest import mock

from fulcra_coord import (
    cli, eventlog, events, remote, schema, views, directives,
)
from fulcra_coord.timeutil import now_iso


def _seed_task_with_events(backend, i: int) -> dict:
    t = schema.make_task(title=f"t{i}", workstream="ws", agent="a")
    t["status"] = "active"
    remote.upload_json(t, remote.task_remote_path(t["id"]), backend=backend)
    eventlog.append_event(
        events.make_event(family="tasks", task_id=t["id"], kind="start",
                          actor="a", payload=dict(t)),
        backend=backend)
    return t


def _seed_loop_with_sublogs(backend, i: int) -> dict:
    t = schema.make_task(title=f"d{i}", workstream="ws", agent="a",
                         assignee="peer:h:r")
    remote.upload_json(t, remote.task_remote_path(t["id"]), backend=backend)
    d = directives.directive_from_task(t)
    remote.upload_json(d, remote.directive_remote_path(d["id"]), backend=backend)
    directives.write_directive_ack(d["id"], "peer:h:r", backend=backend)
    return d


def _seed_reconcilable_bus(backend):
    tasks = [_seed_task_with_events(backend, i) for i in range(2)]
    loops_seeded = [_seed_loop_with_sublogs(backend, i) for i in range(2)]
    for agent in ("a", "peer:h:r"):
        remote.upload_json(
            {"agent": agent, "last_seen": now_iso(), "workstreams": ["ws"]},
            remote.presence_remote_path(views.agent_slug(agent)),
            backend=backend)
    remote.upload_json(
        {"generated_at": now_iso(),
         "summaries": [{"id": tasks[0]["id"], "acked_by": ["peer:h:r"]}]},
        remote.view_remote_path("summaries"), backend=backend)
    remote.upload_json(
        {"active": [{"id": t["id"]} for t in tasks], "recent_done": []},
        remote.view_remote_path("index"), backend=backend)
    return tasks, loops_seeded


# ---------------------------------------------------------------------------
# 1. Snapshot-sharing invariant (the brief's test pattern)
# ---------------------------------------------------------------------------

def test_subpasses_reuse_shared_snapshots(coord_backend):
    """A reconcile tick threads the presence/summaries/loop-record snapshots it
    already holds through the sub-passes instead of having each one re-load.

    Pins the E4/E5 sharing the Task 3 hypothesis depended on:
      * summaries view: one shared read + one post-upload read-back (<= 2),
      * presence AGGREGATE view: zero re-downloads (the tick rebuilt it),
      * directives prefix: listed once, each top-level loop record downloaded
        once across load_loop_records + directive-parity + loop-health.
    """
    _tasks, loops_seeded = _seed_reconcilable_bus(coord_backend)

    real_dl, real_list = remote.download_json, remote.list_files
    downloads: list[str] = []
    lists: list[str] = []

    def dl(path, **kw):
        downloads.append(path)
        return real_dl(path, **kw)

    def lf(prefix, **kw):
        lists.append(prefix)
        return real_list(prefix, **kw)

    with mock.patch.object(remote, "download_json", dl), \
         mock.patch.object(remote, "list_files", lf):
        rc = cli.cmd_reconcile(types.SimpleNamespace(), backend=coord_backend)
    assert rc == 0

    summaries_path = remote.view_remote_path("summaries")
    assert downloads.count(summaries_path) <= 1 + 1, (
        f"summaries downloaded {downloads.count(summaries_path)}x — must be the "
        "single shared read plus at most the post-upload read-back")

    assert downloads.count(remote.presence_view_path()) == 0, (
        "presence aggregate re-downloaded — the tick already holds the rebuilt one")

    assert lists.count(remote.directives_prefix()) == 1, (
        f"directives prefix listed {lists.count(remote.directives_prefix())}x — "
        "one shared sweep must serve loop-record load + directive parity + health")
    for d in loops_seeded:
        path = remote.directive_remote_path(d["id"])
        assert downloads.count(path) == 1, (
            f"loop record {d['id']} downloaded {downloads.count(path)}x — "
            "must be once per tick (shared across the tail)")


# ---------------------------------------------------------------------------
# 2. Per-pass phase timings (permanent fine-grained diagnostics)
# ---------------------------------------------------------------------------

_EXPECTED_PASS_KEYS = {
    "pass_presence_rebuild", "pass_review_sweep", "pass_retention",
    "pass_event_parity", "pass_dual_write_health", "pass_loop_record_load",
    "pass_directive_parity", "pass_loop_health", "pass_role_health",
    "pass_verdict_adopt", "pass_undelivered", "pass_health_assembly",
}


def test_phase_timings_carry_per_pass_breakdown(coord_backend):
    """phase_timings_ms keeps the coarse load/views phases AND adds a per-pass
    entry for every sub-pass, with ``subpasses`` == the sum of the per-pass
    slices (the coarse three-phase view preserved)."""
    _seed_reconcilable_bus(coord_backend)

    captured: dict = {}
    real_summary = cli._PhaseTimer.summary

    def summary(self_timer):
        out = real_summary(self_timer)
        captured.update(out)
        return out

    with mock.patch.object(cli._PhaseTimer, "summary", summary):
        rc = cli.cmd_reconcile(types.SimpleNamespace(), backend=coord_backend)
    assert rc == 0

    # Coarse phases survive.
    assert "load" in captured
    assert "views" in captured
    assert "subpasses" in captured
    # Every per-pass slice is present.
    missing = _EXPECTED_PASS_KEYS - set(captured)
    assert not missing, f"missing per-pass timing keys: {sorted(missing)}"
    # subpasses == sum of the per-pass slices (synthesized, not a stray mark).
    pass_total = sum(v for k, v in captured.items() if k.startswith("pass_"))
    assert abs(captured["subpasses"] - round(pass_total, 1)) < 0.2, (
        f"subpasses {captured['subpasses']} != sum of pass slices "
        f"{round(pass_total, 1)}")


def test_phase_timer_never_raises_on_summary():
    """The never-raise contract: summary() over a timer with mixed coarse +
    per-pass marks returns a plain dict and never raises."""
    pt = cli._PhaseTimer()
    pt.mark("load")
    pt.mark("pass_event_parity")
    pt.mark("pass_directive_parity")
    s = pt.summary()
    assert isinstance(s, dict)
    assert "subpasses" in s  # synthesized from the pass_* slices
    assert s["subpasses"] == round(s["pass_event_parity"]
                                   + s["pass_directive_parity"], 1)
