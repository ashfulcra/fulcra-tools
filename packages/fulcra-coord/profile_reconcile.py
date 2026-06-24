#!/usr/bin/env python3
"""Per-pass profiler for the reconcile sub-pass tail (Task 3, profiling-first).

NOT a test — a throwaway diagnostics harness. Seeds a realistic synthetic bus
(~N tasks with events, D directives with sub-logs, R roles, presence, views)
into a fake backend, then runs cmd_reconcile while:

  * counting remote calls (list/download/stat/upload) PER PASS, by snapshotting
    the running OpCounter totals at every pt.mark() boundary, and
  * reading the phase_timings_ms the tick records.

The fake backend's per-call latency is far below live fulcra-api's ~1.3s, so the
WALL-TIME here is not the live number. The latency-invariant signal is the
per-pass REMOTE-CALL COUNT: each call is one subprocess live, so count maps
directly to live seconds (count * ~1.3s). That is what we report and reason on.

Run:  uv run python profile_reconcile.py [N_TASKS] [N_DIRECTIVES] [N_ROLES]
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import types
from datetime import datetime, timezone, timedelta

# Make the in-tree package importable when run via uv from the package dir.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fulcra_coord import (  # noqa: E402
    cli, eventlog, events, remote, schema, views, directives, role_ops,
    loop_ops,
)
from fulcra_coord.timeutil import now_iso  # noqa: E402


def _backend():
    fake = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "tests", "fake_fulcra_backend.py")
    return [sys.executable, fake]


class PassProfiler:
    """Wrap remote.{list_files,download_json,stat,upload_json} with delegating
    counters, and wrap cli._PhaseTimer.mark to snapshot the running totals at
    each pass boundary. Yields per-pass deltas keyed by the mark label."""

    def __init__(self):
        self.lists: list[str] = []
        self.downloads: list[str] = []
        self.stats: list[str] = []
        self.uploads: list[str] = []
        self.snapshots: list[tuple[str, int]] = []  # (label, total calls so far)

    def _total(self) -> int:
        return (len(self.lists) + len(self.downloads)
                + len(self.stats) + len(self.uploads))

    def install(self):
        real_list, real_dl = remote.list_files, remote.download_json
        real_stat, real_up = remote.stat, remote.upload_json
        real_mark = cli._PhaseTimer.mark
        prof = self

        def list_files(prefix, **kw):
            prof.lists.append(prefix)
            return real_list(prefix, **kw)

        def download_json(path, **kw):
            prof.downloads.append(path)
            return real_dl(path, **kw)

        def stat(path, **kw):
            prof.stats.append(path)
            return real_stat(path, **kw)

        def upload_json(data, path, **kw):
            prof.uploads.append(path)
            return real_up(data, path, **kw)

        def mark(self_timer, label):
            prof.snapshots.append((label, prof._total()))
            return real_mark(self_timer, label)

        remote.list_files = list_files
        remote.download_json = download_json
        remote.stat = stat
        remote.upload_json = upload_json
        cli._PhaseTimer.mark = mark
        self._restore = (real_list, real_dl, real_stat, real_up, real_mark)

    def uninstall(self):
        (remote.list_files, remote.download_json, remote.stat,
         remote.upload_json) = self._restore[:4]
        cli._PhaseTimer.mark = self._restore[4]

    def per_pass_calls(self) -> list[tuple[str, int]]:
        out = []
        prev = 0
        for label, total in self.snapshots:
            out.append((label, total - prev))
            prev = total
        return out


def seed_bus(backend, n_tasks: int, n_directives: int, n_roles: int):
    now = datetime.now(timezone.utc)
    active_ids = []
    proposed_ids = []
    for i in range(n_tasks):
        t = schema.make_task(title=f"task-{i}", workstream="ws",
                             agent=f"agent-{i % 8}:h:r")
        # Mix: ~70% active, ~30% proposed (proposed directed at an OFFLINE agent
        # so the undelivered check has real work to consider).
        if i % 10 < 7:
            t["status"] = "active"
            active_ids.append(t["id"])
        else:
            t["status"] = "proposed"
            t["assignee"] = f"offline-{i}:h:r"
            proposed_ids.append(t["id"])
        remote.upload_json(t, remote.task_remote_path(t["id"]), backend=backend)
        # Every task dual-written: one full-snapshot event so event-parity has
        # a complete fold to compare (the realistic post-migration state).
        eventlog.append_event(
            events.make_event(family="tasks", task_id=t["id"], kind="start",
                              actor="a", payload=dict(t)),
            backend=backend)

    # Directives with sub-logs (acks/routing/responses): the load_loop_records
    # + directive-parity working set.
    dir_ids = []
    for i in range(n_directives):
        t = schema.make_task(title=f"dtask-{i}", workstream="ws", agent="a",
                             assignee=f"peer-{i % 5}:h:r")
        remote.upload_json(t, remote.task_remote_path(t["id"]), backend=backend)
        d = directives.directive_from_task(t)
        remote.upload_json(d, remote.directive_remote_path(d["id"]),
                           backend=backend)
        directives.write_directive_ack(d["id"], f"peer-{i % 5}:h:r",
                                       backend=backend)
        directives.append_directive_route(
            d["id"], {"event_id": f"e{i}", "at": now_iso()}, backend=backend)
        dir_ids.append(d["id"])

    # Roles with leases.
    for r in range(n_roles):
        role = schema.make_role(f"role-{r}", "desc")
        role_ops.upsert_role(role, backend=backend)
        for h in range(2):
            role_ops.claim_role(f"role-{r}", f"holder-{r}-{h}:h:r",
                                backend=backend)

    # Presence per-agent records (what _reconcile_presence rebuilds from).
    for a in range(8):
        remote.upload_json(
            {"agent": f"agent-{a}:h:r", "last_seen": now_iso(),
             "workstreams": ["ws"]},
            remote.presence_remote_path(views.agent_slug(f"agent-{a}:h:r")),
            backend=backend)

    # Retention throttle marker: when STEADY (already claimed today), retention
    # is a no-op after one marker read — the state most of the 72 ticks/day see.
    # Set via env PROFILE_THROTTLE_RETENTION=1.
    if os.environ.get("PROFILE_THROTTLE_RETENTION") == "1":
        remote.upload_json(
            {"schema": "fulcra.coordination.retention_marker.v1",
             "date": now.strftime("%Y-%m-%d"), "by": "other:h:r",
             "at": now_iso()},
            remote.retention_marker_path(now), backend=backend)

    # Summaries view + index view so _load_all_tasks loads the seeded tasks.
    all_active = active_ids + [remote.download_json(
        remote.task_remote_path(tid), backend=backend)["id"]
        for tid in []]  # active_ids already collected
    remote.upload_json(
        {"generated_at": now_iso(),
         "summaries": [{"id": tid, "acked_by": []} for tid in active_ids]},
        remote.view_remote_path("summaries"), backend=backend)
    remote.upload_json(
        {"active": [{"id": tid} for tid in active_ids],
         "recent_done": []},
        remote.view_remote_path("index"), backend=backend)


def main():
    n_tasks = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    n_directives = int(sys.argv[2]) if len(sys.argv) > 2 else 80
    n_roles = int(sys.argv[3]) if len(sys.argv) > 3 else 6

    tmp = tempfile.mkdtemp(prefix="profile-reconcile-")
    os.environ["FULCRA_FAKE_ROOT"] = tmp
    # Disable retention throttle interference: leave default (runs once/day).
    backend = _backend()

    print(f"Seeding bus: {n_tasks} tasks, {n_directives} directives, "
          f"{n_roles} roles -> {tmp}", file=sys.stderr)
    t_seed = time.monotonic()
    seed_bus(backend, n_tasks, n_directives, n_roles)
    print(f"Seed done in {time.monotonic()-t_seed:.1f}s", file=sys.stderr)

    prof = PassProfiler()
    prof.install()
    captured = {}
    real_build = cli._build_health_record

    def capture_health(**kw):
        rec = real_build(**kw)
        return rec
    try:
        t0 = time.monotonic()
        rc = cli.cmd_reconcile(types.SimpleNamespace(), backend=backend)
        wall = time.monotonic() - t0
    finally:
        prof.uninstall()

    print(f"\nreconcile rc={rc} wall={wall:.1f}s "
          f"total_remote_calls={prof._total()}")
    print(f"  lists={len(prof.lists)} downloads={len(prof.downloads)} "
          f"stats={len(prof.stats)} uploads={len(prof.uploads)}")

    print("\n--- per-pass remote-call counts (count * ~1.3s = live seconds) ---")
    per = prof.per_pass_calls()
    LAT = 1.3
    for label, calls in per:
        print(f"  {label:28s} calls={calls:5d}   est_live={calls*LAT:7.1f}s")

    # Read the phase_timings the tick recorded (from the uploaded health record).
    print("\n--- phase_timings_ms (fake-backend wall, NOT live) ---")
    # Find the health record on the fake bus.
    health_prefix = f"{remote.remote_root()}/health/"
    try:
        for p in remote.list_files(health_prefix, backend=backend):
            rec = remote.download_json(p, backend=backend)
            if isinstance(rec, dict) and "phase_timings_ms" in rec:
                print(json.dumps(rec["phase_timings_ms"], indent=2))
                break
    except Exception as e:
        print(f"  (could not read health record: {e})")


if __name__ == "__main__":
    main()
