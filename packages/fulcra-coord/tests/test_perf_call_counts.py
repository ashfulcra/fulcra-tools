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
import types
from datetime import datetime, timezone
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


# ---------------------------------------------------------------------------
# E3 — stat-gated body fetch in io._cache_remote_task
# ---------------------------------------------------------------------------

def _seed_body(backend, summary="v1") -> dict:
    t = schema.make_task(title="stat-gate", workstream="ws", agent="a")
    t["current_summary"] = summary
    remote.upload_json(t, remote.task_remote_path(t["id"]), backend=backend)
    return t


def test_cache_remote_task_unchanged_body_is_one_stat_zero_downloads(coord_backend):
    """The steady-state read: the task did not change since the last read, so
    the strong version key in the fresh stat matches the cached meta and the
    body download is SKIPPED — 1 stat replaces the old unconditional
    download+stat pair. On a 440-task bus where a tick touches a handful of
    tasks, this halves the body-fetch spawn count."""
    t = _seed_body(coord_backend)
    path = remote.task_remote_path(t["id"])
    warm = io._cache_remote_task(t["id"], backend=coord_backend)  # seeds cache+meta
    counter = OpCounter()
    with counter.patch():
        got = io._cache_remote_task(t["id"], backend=coord_backend)
    assert got == warm
    assert counter.stats == [path]
    assert counter.downloads == []


def test_load_task_unchanged_body_uses_stat_gate(coord_backend):
    """A single-task read must keep the cheap steady-state path while still
    checking freshness: one stat, zero downloads when the strong version key
    matches the cached meta."""
    t = _seed_body(coord_backend)
    path = remote.task_remote_path(t["id"])
    warm = io._cache_remote_task(t["id"], backend=coord_backend)  # seeds cache+meta
    counter = OpCounter()
    with counter.patch():
        got = io._load_task(t["id"], backend=coord_backend)
    assert got == warm
    assert counter.stats == [path]
    assert counter.downloads == []


def test_load_task_cached_without_meta_stays_local_only(coord_backend):
    """A cached task with no remote stat meta is local-only/offline work.

    Single-task commands must keep operating on it instead of forcing a remote
    fetch that can only report "not found" on disconnected fake/offline backends.
    """
    from fulcra_coord import cache
    t = schema.make_task(title="local-only", workstream="ws", agent="a")
    cache.write_cached_task(t)
    path = remote.task_remote_path(t["id"])
    assert cache.read_meta(path) is None
    counter = OpCounter()
    with counter.patch():
        got = io._load_task(t["id"], backend=coord_backend)
    assert got == t
    assert counter.stats == []
    assert counter.downloads == []


def test_cache_remote_task_changed_body_stats_then_downloads(coord_backend):
    """A changed task (new strong version key) costs exactly stat + download
    and returns the NEW body — the gate never serves a stale cache."""
    t = _seed_body(coord_backend, summary="v1")
    path = remote.task_remote_path(t["id"])
    io._cache_remote_task(t["id"], backend=coord_backend)  # warm
    t2 = dict(t)
    t2["current_summary"] = "v2"
    remote.upload_json(t2, path, backend=coord_backend)
    counter = OpCounter()
    with counter.patch():
        got = io._cache_remote_task(t["id"], backend=coord_backend)
    assert got["current_summary"] == "v2"
    assert counter.stats == [path]
    assert counter.downloads == [path]


def test_load_task_changed_body_does_not_serve_stale_cache(coord_backend):
    """Regression: _load_task used to return cache.read_cached_task directly.

    That bypassed the stat gate and could resurrect stale local task state after
    another agent had already updated the remote body (seen live as review
    tasks whose summaries were done while direct body reads still looked
    proposed).
    """
    t = _seed_body(coord_backend, summary="v1")
    path = remote.task_remote_path(t["id"])
    io._cache_remote_task(t["id"], backend=coord_backend)  # warm
    t2 = dict(t)
    t2["current_summary"] = "v2"
    remote.upload_json(t2, path, backend=coord_backend)
    counter = OpCounter()
    with counter.patch():
        got = io._load_task(t["id"], backend=coord_backend)
    assert got["current_summary"] == "v2"
    assert counter.stats == [path]
    assert counter.downloads == [path]


def test_load_task_remote_outage_serves_cached_body_with_warn(coord_backend):
    """If both the stat probe and body download fail after a task was already
    synced, keep the cached task visible instead of reporting "not found"."""
    t = _seed_body(coord_backend, summary="v1")
    path = remote.task_remote_path(t["id"])
    warm = io._cache_remote_task(t["id"], backend=coord_backend)  # seeds cache+meta
    with mock.patch.object(remote, "stat", return_value=None) as stat:
        with mock.patch.object(remote, "download_json", return_value=None) as dl:
            with mock.patch("fulcra_coord.io._warn") as warn:
                got = io._load_task(t["id"], backend=coord_backend)
    assert got == warm
    assert stat.call_count == 1
    dl.assert_called_once_with(path, backend=coord_backend)
    warn.assert_called_once()
    assert "freshness not confirmed" in warn.call_args.args[0]


def test_load_task_download_failure_does_not_advance_cached_meta(coord_backend):
    """A changed remote stat followed by a failed download serves the cached
    body with a warning, but must not pair stale bytes with the new stat."""
    from fulcra_coord import cache

    t = _seed_body(coord_backend, summary="v1")
    path = remote.task_remote_path(t["id"])
    warm = io._cache_remote_task(t["id"], backend=coord_backend)  # seeds cache+meta
    old_meta = dict(cache.read_meta(path))
    changed_stat = dict(old_meta)
    changed_stat["version"] = f"{old_meta.get('version', 'v')}-new"
    with mock.patch.object(remote, "stat", return_value=changed_stat) as stat:
        with mock.patch.object(remote, "download_json", return_value=None) as dl:
            with mock.patch("fulcra_coord.io._warn") as warn:
                got = io._load_task(t["id"], backend=coord_backend)
    assert got == warm
    assert stat.call_count == 1
    dl.assert_called_once_with(path, backend=coord_backend)
    warn.assert_called_once()
    assert cache.read_meta(path) == old_meta


def test_cache_remote_task_cold_cache_downloads(coord_backend):
    """No prior meta / no cached body -> the gate cannot prove anything, so the
    body is downloaded (the pre-fix behaviour, one download + one meta stat)."""
    t = _seed_body(coord_backend)
    path = remote.task_remote_path(t["id"])
    counter = OpCounter()
    with counter.patch():
        got = io._cache_remote_task(t["id"], backend=coord_backend)
    assert got["id"] == t["id"]
    assert counter.downloads == [path]


# ---------------------------------------------------------------------------
# E4 — tick-scoped snapshot sharing in cmd_reconcile
# ---------------------------------------------------------------------------

def test_reconcile_tick_loads_each_shared_snapshot_once(coord_backend):
    """One reconcile tick used to download the summaries view 3x (rebuild-source
    acks, event-parity ack authority, undelivered-check ack map), load presence
    3x (reroute sweep, role health, undelivered live-set), and sweep the
    directives prefix 2x (directive parity, loop health). Each snapshot is now
    loaded ONCE at the top of the relevant section and threaded through, with
    one additional summaries read-back after upload to prove reconcile did not
    leave a stale aggregate in place."""
    import types
    from fulcra_coord import views
    from fulcra_coord.timeutil import now_iso

    tasks = [_seed_task_with_events(coord_backend, i) for i in range(2)]
    loops_seeded = [_seed_loop_with_sublogs(coord_backend, i) for i in range(2)]
    # Presence: per-agent records (what _reconcile_presence rebuilds from).
    for agent in ("a", "peer:h:r"):
        remote.upload_json(
            {"agent": agent, "last_seen": now_iso(), "workstreams": ["ws"]},
            remote.presence_remote_path(views.agent_slug(agent)),
            backend=coord_backend)
    # Summaries view with an ack worth preserving.
    remote.upload_json(
        {"generated_at": now_iso(),
         "summaries": [{"id": tasks[0]["id"], "acked_by": ["peer:h:r"]}]},
        remote.view_remote_path("summaries"), backend=coord_backend)
    # Index view so _load_all_tasks actually loads the seeded tasks.
    remote.upload_json(
        {"active": [{"id": t["id"]} for t in tasks], "recent_done": []},
        remote.view_remote_path("index"), backend=coord_backend)

    counter = OpCounter()
    with counter.patch():
        rc = cli.cmd_reconcile(types.SimpleNamespace(), backend=coord_backend)
    assert rc == 0

    # Summaries view: one shared snapshot read plus one post-upload read-back.
    summaries_path = remote.view_remote_path("summaries")
    assert counter.downloads.count(summaries_path) == 2, (
        f"summaries view downloaded "
        f"{counter.downloads.count(summaries_path)}x — must be two per tick")

    # Presence aggregate view: ZERO downloads — the tick rebuilds it from the
    # per-agent records and shares the rebuilt roster with every sub-pass.
    assert counter.downloads.count(remote.presence_view_path()) == 0, (
        "presence view re-downloaded — the tick already holds the rebuilt one")

    # Directives: each top-level loop record downloaded exactly ONCE (was 2-3x
    # across loop-health + directive-parity), prefix listed exactly once.
    for d in loops_seeded:
        path = remote.directive_remote_path(d["id"])
        assert counter.downloads.count(path) == 1, (
            f"loop record {d['id']} downloaded "
            f"{counter.downloads.count(path)}x — must be once per tick")
    assert len(counter.lists_of(remote.directives_prefix())) == 1


# ---------------------------------------------------------------------------
# E5 — role fold: ONE roles/ listing serves registry + every role's leases
# (2026-06-11 loop-2 mechanical pass, item 1)
# ---------------------------------------------------------------------------

def _seed_role(backend, name, holders=()):
    from fulcra_coord import role_ops
    role = schema.make_role(name, "d")
    assert role_ops.upsert_role(role, backend=backend) is True
    for h in holders:
        assert role_ops.claim_role(name, h, backend=backend) is True
    return role


def _seed_role_bus(backend):
    """Two roles, three lease shards, one escalation marker — the layout that
    made list_roles + per-role read_leases pay 1+R listings and R+2L+E
    downloads per surface render."""
    _seed_role(backend, "reviewer", ("a:h:r", "b:h:r"))
    _seed_role(backend, "deployer", ("c:h:r",))
    remote.upload_json(
        {"role": "reviewer", "date": "2026-01-01"},
        remote.role_escalation_marker_path("reviewer", "2026-01-01"),
        backend=backend)


def _assert_one_roles_listing_no_waste(counter):
    """The shared E5 pin: exactly ONE listing under roles/ (no per-role lease
    re-list), each registry record + lease shard downloaded exactly once, and
    the escalation marker never downloaded (path-partitioned away)."""
    prefix = remote.roles_prefix()
    assert [p for p in counter.lists if p.startswith(prefix)] == [prefix], (
        f"roles/ listings: {[p for p in counter.lists if p.startswith(prefix)]}"
        " — must be exactly one listing of the prefix, no per-role re-lists")
    expected = sorted([
        remote.role_record_path("reviewer"),
        remote.role_record_path("deployer"),
        remote.role_lease_path("reviewer", "a:h:r"),
        remote.role_lease_path("reviewer", "b:h:r"),
        remote.role_lease_path("deployer", "c:h:r"),
    ])
    assert sorted(counter.downloads_under(prefix)) == expected, (
        "downloads under roles/ must be each registry record + lease shard "
        f"exactly once (no escalation markers): {counter.downloads_under(prefix)}")


def test_role_health_check_one_roles_listing_no_waste(coord_backend):
    """_role_health_check used to pay list_roles (1 list + R+L+E downloads)
    plus one read_leases per role (R lists + L re-downloads) EVERY reconcile
    tick. One partitioned listing must serve it all."""
    _seed_role_bus(coord_backend)
    counter = OpCounter()
    with counter.patch():
        out = cli._role_health_check(backend=coord_backend)
    assert {r["name"] for r in out["roles"]} == {"reviewer", "deployer"}
    _assert_one_roles_listing_no_waste(counter)


def test_cmd_roles_one_roles_listing_no_waste(coord_backend, capsys):
    _seed_role_bus(coord_backend)
    counter = OpCounter()
    with counter.patch():
        rc = cli.cmd_roles(
            types.SimpleNamespace(roles_action=None, format="json", agent=None),
            backend=coord_backend)
    assert rc == 0
    _assert_one_roles_listing_no_waste(counter)


def test_board_roles_section_one_roles_listing_no_waste(coord_backend, capsys):
    from fulcra_coord import query
    _seed_role_bus(coord_backend)
    counter = OpCounter()
    with counter.patch():
        rc = query.cmd_board(
            types.SimpleNamespace(agent="me:h:r", format="json"),
            backend=coord_backend)
    assert rc == 0
    _assert_one_roles_listing_no_waste(counter)


# ---------------------------------------------------------------------------
# E6 — notify-inbox: the summaries aggregate is loaded ONCE per listener tick
# (loop-2 item 2: _load_inbox loaded it, then _notify_new_needs_me re-loaded)
# ---------------------------------------------------------------------------

def test_notify_inbox_loads_summaries_once(coord_backend):
    from fulcra_coord import inbox
    from fulcra_coord.timeutil import now_iso
    remote.upload_json(
        {"generated_at": now_iso(), "summaries": []},
        remote.view_remote_path("summaries"), backend=coord_backend)
    counter = OpCounter()
    with mock.patch("fulcra_coord.listener.emit_notification"), \
         mock.patch("fulcra_coord.listener.emit_message"), \
         mock.patch("fulcra_coord.selfupdate.maybe_self_update"):
        with counter.patch():
            rc = inbox.cmd_notify_inbox(
                types.SimpleNamespace(agent="agent-a:h:r"),
                backend=coord_backend)
    assert rc == 0
    path = remote.view_remote_path("summaries")
    assert counter.downloads.count(path) == 1, (
        f"summaries view downloaded {counter.downloads.count(path)}x per "
        "notify tick — the needs-me pass must reuse the inbox load")


# ---------------------------------------------------------------------------
# E7 — evidence probe is LIST-ONLY: nonemptiness never downloads shard bodies
# (loop-2 item 3: evidence_ids_for rode list_json = 1 list + K downloads)
# ---------------------------------------------------------------------------

def _seed_open_loop_with_evidence(backend, opener="me:h:r"):
    d = schema.make_directive(
        directive_type="review", from_agent=opener, audience="rev:h:r",
        title="review PR 9", workstream="general",
        kind="review", state="requested", expects_response=True, sla_hours=24)
    assert remote.upload_json(d, remote.directive_remote_path(d["id"]),
                              backend=backend)
    assert loop_ops.append_loop_evidence(
        d["id"], {"by": "mirror", "note": "PR merged"}, backend=backend) is True
    return d


def test_evidence_probe_lists_without_downloading_shards(coord_backend):
    """Only listing-NONEMPTINESS is consumed (loops.awaiting_others reads the
    id set; bodies are read elsewhere via read_loop_evidence), so the probe
    must cost 1 list + 0 downloads per candidate — not 1 list + K downloads
    on every board render, digest, and reconcile tick."""
    d = _seed_open_loop_with_evidence(coord_backend)
    records = loop_ops.load_loop_records(backend=coord_backend)
    counter = OpCounter()
    with counter.patch():
        ids = loop_ops.evidence_ids_for(
            "me:h:r", records, now=datetime.now(timezone.utc),
            backend=coord_backend)
    assert ids == {d["id"]}
    eprefix = remote.directive_evidence_prefix(d["id"])
    assert counter.lists_of(eprefix) == [eprefix]
    assert counter.downloads_under(eprefix) == [], (
        "evidence shard bodies downloaded for a nonemptiness-only probe")


# ---------------------------------------------------------------------------
# E8 — dual_write folds the responses it already read (no sub-log double-read)
# (loop-2 item 4: the `if responses:` probe + fold_loop re-read the prefix)
# ---------------------------------------------------------------------------

def test_dual_write_reads_response_sublog_once(coord_backend):
    from fulcra_coord import directives
    t = schema.make_task(title="d", workstream="ws", agent="a",
                         assignee="peer:h:r")
    remote.upload_json(t, remote.task_remote_path(t["id"]), backend=coord_backend)
    d = directives.directive_from_task(t)
    assert loop_ops.append_loop_response(
        d["id"], {"by": "peer:h:r", "outcome": {"verdict": "done"}},
        backend=coord_backend) is True
    counter = OpCounter()
    with counter.patch():
        directives.dual_write(t, command="tell", backend=coord_backend)
    rprefix = remote.directive_responses_prefix(d["id"])
    assert counter.lists_of(rprefix) == [rprefix], (
        f"responses prefix listed {len(counter.lists_of(rprefix))}x per "
        "dual_write — the fold must reuse the probe's read")
    assert len(counter.downloads_under(rprefix)) == 1
    # The fold semantics stay intact: the mirrored snapshot carries closure.
    stored = remote.download_json(remote.directive_remote_path(d["id"]),
                                  backend=coord_backend)
    assert (stored.get("outcome") or {}).get("verdict") == "done"


# ---------------------------------------------------------------------------
# E9 — digest: the summaries aggregate is loaded ONCE per digest
# (loop-2 item 5: cmd_digest loaded it, then _assess_fleet re-loaded for a count)
# ---------------------------------------------------------------------------

def test_digest_loads_summaries_once(coord_backend, capsys):
    from fulcra_coord import digest as digest_mod
    from fulcra_coord.timeutil import now_iso
    remote.upload_json(
        {"generated_at": now_iso(), "summaries": []},
        remote.view_remote_path("summaries"), backend=coord_backend)
    counter = OpCounter()
    with counter.patch():
        rc = digest_mod.cmd_digest(
            types.SimpleNamespace(window="evening", format="json",
                                  dry_run=True, human="ash"),
            backend=coord_backend)
    assert rc == 0
    path = remote.view_remote_path("summaries")
    assert counter.downloads.count(path) == 1, (
        f"summaries view downloaded {counter.downloads.count(path)}x per "
        "digest — _assess_fleet must reuse cmd_digest's load")


# ---------------------------------------------------------------------------
# E10 — reconcile: the health record reuses the retention pass's marker read
# (loop-2 item 6: cli re-downloaded retention/last-run.json the pass just read)
# ---------------------------------------------------------------------------

def test_reconcile_running_retention_reads_marker_twice_only(coord_backend):
    """A tick that RUNS retention reads retention/last-run.json exactly twice
    (the claim's existence read + its post-write race confirm). The health
    record's retention_last_run must come from the threaded marker — the old
    third download is the regression this pins out."""
    counter = OpCounter()
    with counter.patch():
        rc = cli.cmd_reconcile(types.SimpleNamespace(), backend=coord_backend)
    assert rc == 0
    path = remote.retention_marker_path(datetime.now(timezone.utc))
    assert counter.downloads.count(path) == 2, (
        f"retention marker downloaded {counter.downloads.count(path)}x on a "
        "running-retention tick — must be claim read + confirm only")


def test_reconcile_throttled_retention_reads_marker_once(coord_backend):
    """The steady state (every tick after today's first): the claim reads the
    existing today-marker once, yields, and threads it to the health record —
    ONE download, not two."""
    from fulcra_coord.timeutil import now_iso
    now = datetime.now(timezone.utc)
    remote.upload_json(
        {"schema": "fulcra.coordination.retention_marker.v1",
         "date": now.strftime("%Y-%m-%d"), "by": "other:h:r", "at": now_iso()},
        remote.retention_marker_path(now), backend=coord_backend)
    counter = OpCounter()
    with counter.patch():
        rc = cli.cmd_reconcile(types.SimpleNamespace(), backend=coord_backend)
    assert rc == 0
    path = remote.retention_marker_path(now)
    assert counter.downloads.count(path) == 1, (
        f"retention marker downloaded {counter.downloads.count(path)}x on a "
        "throttled tick — the health record must reuse the claim's read")


def test_cache_remote_task_weak_only_stat_falls_back_to_download(coord_backend):
    """A stat with NO strong identity key (version_id/version/etag) can never
    prove the body unchanged — equal sizes/timestamps don't (a re-upload can
    produce the same size), so the gate must fall back to downloading."""
    t = _seed_body(coord_backend)
    path = remote.task_remote_path(t["id"])
    io._cache_remote_task(t["id"], backend=coord_backend)  # warm
    # Make BOTH sides weak-only: strip strong keys from the cached meta and
    # serve a weak-only fresh stat.
    from fulcra_coord import cache
    meta = cache.read_meta(path)
    meta.pop("version", None)
    meta.pop("version_id", None)
    meta.pop("etag", None)
    cache.write_meta(path, meta)
    weak_stat = {"path": path, "size": meta.get("size", 1)}
    counter = OpCounter()
    with mock.patch.object(remote, "stat", return_value=weak_stat):
        with counter.patch():
            got = io._cache_remote_task(t["id"], backend=coord_backend)
    assert got["id"] == t["id"]
    assert counter.downloads == [path]
