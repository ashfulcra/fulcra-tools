"""CLI command implementations for fulcra-coord.

Each command accepts parsed argparse namespace and an optional backend=
override for testing without live Fulcra access.
"""

from __future__ import annotations

import concurrent.futures
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import cache, remote, schema, views, log as ops_log, heartbeat, identity
from . import env_int
from . import events as _events, eventlog as _eventlog
from . import directives as _directives
# Leaf-utility modules extracted from this file. Re-exported under the historical
# underscore-prefixed names so every internal call site AND the test patch targets
# (fulcra_coord.cli._info / ._now_iso / ...) keep resolving unchanged — output.py /
# timeutil.py do not import cli, so there is no import cycle.
from .output import err as _err, warn as _warn, info as _info
from .timeutil import iso_z as _iso_z, now_iso as _now_iso
from .textfmt import age_str as _age_str, until_str as _until_str, due_str as _due_str
# Retention / archival subsystem extracted from this file. Re-exported under the
# historical underscore-prefixed names so every remaining caller here
# (cmd_reconcile -> _run_retention; cmd_search / cmd_restore -> the cold-index
# readers) AND the test patch targets (fulcra_coord.cli._archive_task / ...)
# keep resolving. retention.py depends only on lower layers and never imports
# cli, so there is no import cycle.
from .retention import (
    _archive_month, _archive_index_shard, _archive_task, _read_index_shard,
    _list_index_shards, _retention_max_per_run, _claim_retention_marker,
    _prune_markers, _prune_dead_presence, _prune_dead_health, _run_retention,
    _prune_continuity_checkpoints, _continuity_keep,
    _expire_stale_broadcasts, _RETENTION_DEADLINE_HEADROOM_SECONDS,
    cmd_search, cmd_restore,
)
# Shared remote-task load/cache layer extracted from this file. Re-exported under
# the historical underscore-prefixed names so every cli-resident caller
# (cmd_status / cmd_reconcile / cmd_digest / _try_merge / _write_task_and_views /
# ...) AND the unmigrated test patch targets (fulcra_coord.cli._load_all_tasks /
# ...) keep resolving. io.py depends only on lower layers and never imports cli,
# so there is no import cycle.
from .io import (
    _cache_remote_task, _load_all_tasks, _load_task_summaries,
    _load_summaries_for_rebuild, _load_task, _updated_at_key,
)
# Presence subsystem extracted from this file. Re-exported under the historical
# names so the command dispatch (cmd_connect/cmd_workstream/cmd_presence),
# cmd_reconcile's _reconcile_presence call, cmd_start's _maybe_warn_legacy_identity
# call, and the test patch targets keep resolving. presence.py never imports cli.
from .presence import (
    _maybe_warn_legacy_identity, _derive_workstreams_from_open_tasks,
    _upsert_presence_aggregate, _write_presence, _load_own_presence, cmd_connect,
    _split_workstreams, cmd_workstream, cmd_presence, _reconcile_presence,
)
# Read-only situational-awareness commands extracted from this file. Re-exported so
# the command dispatch (entry.py) and the test imports of these commands keep
# resolving. query.py never imports cli.
from .query import cmd_status, cmd_agents, cmd_needs_me, cmd_resume
# Task write pipeline extracted from this file. Re-exported under the historical
# names so every write command (cmd_start/update/block/pause/done/abandon/tell/
# broadcast/assign/inbox/request-review) that calls _write_task_and_views, plus the
# test patch targets, keep resolving. writepipe.py never imports cli.
from .writepipe import (
    _stamp_session_pointer, _write_task_and_views, _emit_lifecycle, _lifecycle_for,
    _view_name_to_remote, _try_merge, _carry_fields, _union_events_and_acked,
    _repair_merged_tags,
)
# Liveness-aware reviewer routing extracted from this file. Re-exported so
# cmd_reconcile's _sweep_review_routes call, the request-review dispatch, and the
# test patch targets keep resolving. routing_ops.py never imports cli.
from .routing_ops import (
    _canonical_reviewer, _review_pool, _append_route_event_and_assignee,
    _force_block_for_human, _escalate_review_to_human, cmd_request_review,
    cmd_review_done, _resolve_review_author,
    _reroute_minutes, _reroute_max, _accepted_stall_hours,
    _review_accepted_by_assignee, _classify_review, _sweep_review_routes,
)
# Operator situational-awareness output (digest push + health pull) extracted from
# this file. Re-exported so the digest/health/install-digest dispatch, cmd_doctor's
# _assess_fleet fold, and the test patch targets keep resolving. digest.py never
# imports cli.
from .digest import (
    _load_health_records, _freshest_digest_emit, _assess_fleet, cmd_health,
    _digest_lines, _render_digest, _digest_window_since, _digest_marker_path,
    _claim_digest_marker, cmd_digest, cmd_install_digest,
)
# Task lifecycle + directive commands extracted from this file. Re-exported so the
# command dispatch (entry.py) and the test imports keep resolving. lifecycle.py
# never imports cli.
from .lifecycle import (
    cmd_tell, cmd_broadcast, cmd_assign, cmd_start, cmd_update, cmd_block,
    cmd_pause, cmd_snapshot, cmd_done, cmd_abandon,
)
# Inbox + blocked-on-you notification extracted from this file. Re-exported so the
# dispatch (inbox/notify-inbox), _build_health_record's read of the listener
# last-fire surface (_inbox_surface_path), and the test targets keep resolving.
# inbox.py never imports cli.
from .inbox import (
    cmd_inbox, _load_inbox, _inbox_surface_path, _needs_me_seen_path,
    _notify_new_needs_me, cmd_notify_inbox,
)
# Hook + scheduler installers extracted from this file. Re-exported so the
# install-* command dispatch (entry.py) and the test imports resolve. installers.py
# never imports cli.
from .installers import (
    _report_resolved_cli, cmd_install_claude_code, cmd_install_openclaw,
    cmd_install_codex, cmd_install_heartbeat, cmd_install_listener, cmd_install_shim,
    cmd_ensure_codex_watch,
)
# Diagnostics (capabilities + doctor) extracted from this file. Re-exported so the
# dispatch (entry.py) and test imports resolve. doctor.py never imports cli.
from .doctor import cmd_capabilities, cmd_doctor
# Local agent/host configuration commands extracted from this file. Re-exported so
# the dispatch (entry.py) and test imports resolve. config.py never imports cli.
from .config import cmd_session_task, cmd_identity, cmd_human, cmd_annotations
# Kept as a re-export (no cli code uses it now) because tests patch the writer via
# ``fulcra_coord.cli.lifecycle_annotations.emit_*`` — the shared annotations module,
# so the patch reaches the digest/lifecycle/config callers cross-namespace. Aliased
# because ``from __future__ import annotations`` binds the bare name ``annotations``.
from . import annotations as lifecycle_annotations

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def _derive_agent() -> str:
    """Resolve the caller's agent id when not given explicitly.

    Thin wrapper over identity.resolve_agent() — the single "who am I" entry
    point. Kept as a local alias so the (many) callsites read naturally; the
    resolution order (explicit > env > persisted identity > derived) now lives in
    fulcra_coord.identity so the CLI, listener, and `identity` command agree.
    """
    return identity.resolve_agent()


# ---------------------------------------------------------------------------
# Listener inbox surface + per-host health record assembly
# ---------------------------------------------------------------------------

def _build_health_record(*, now, duration_s, tasks_loaded, views_refreshed,
                         repair_backlog, retention_last_run, listener_last_fire,
                         bus_task_count) -> dict:
    """Assemble the per-host health record from a SUCCESSFUL reconcile's locals
    plus cheap reads. Pure given its args; identity/version read here so the
    caller stays a one-liner. host = short hostname (matches identity.derived_agent);
    agent = resolve_agent(). reconcile_at is the success instant."""
    import socket
    from . import __version__
    try:
        host = socket.gethostname().split(".")[0]
    except Exception:
        host = "host"
    return {
        "schema": "fulcra.coordination.health.v1",
        "host": host,
        "agent": identity.resolve_agent(),
        "version": __version__,
        "reconcile_at": _iso_z(now),
        "duration_s": duration_s,
        "tasks_loaded": tasks_loaded,
        "views_refreshed": views_refreshed,
        "repair_backlog": repair_backlog,
        "retention_last_run": retention_last_run,
        "listener_last_fire": listener_last_fire,
        "bus_task_count": bus_task_count,
    }


#: Window for the dual-write append-failure liveness count. 24h is long enough to
#: catch an intermittent failure between reconciles, short enough that a
#: long-resolved blip ages out instead of dragging the signal forever. The ops-log
#: file is append-only/unbounded today; pruning it is a separate retention
#: follow-up (NOT built here).
_DUAL_WRITE_FAILURE_WINDOW = timedelta(hours=24)


def _event_dual_write_health() -> dict:
    """SIGNAL C (dual-write liveness): recent ``event_append_failed`` count.

    The dual-write append path records an ``event_append_failed`` op on every
    failed event append, but those entries were write-only — a host whose
    dual-write is silently failing was invisible to the fleet. This counts them
    over a recent window from the local ops log and returns an
    ``event_dual_write`` block for the health record.

    Best-effort: any failure reading/counting yields ``append_failures_recent``
    0 (the block is still emitted with the window), and this never raises — a
    corrupt ops log must NEVER break reconcile. ``window_since`` records the
    window start so a reader knows the count's horizon."""
    since = datetime.now(timezone.utc) - _DUAL_WRITE_FAILURE_WINDOW
    n = 0
    try:
        n = sum(
            1 for e in cache.read_ops_log(since=since)
            if e.get("status") == "event_append_failed"
        )
    except Exception:
        n = 0
    return {"append_failures_recent": n, "window_since": _iso_z(since)}


#: Cap on the undelivered-directive LIST (not the count). Keeps the health-record
#: block and the reconcile/status warning bounded — a fleet that somehow piled up
#: hundreds of directives into a dead inbox must not produce a wall of ids — while
#: ``count`` always carries the TRUE total so the signal is never silently lost.
_UNDELIVERED_LIST_CAP = 50


def _live_agent_ids(*, backend: Optional[list[str]] = None) -> Optional[set[str]]:
    """The set of agent ids whose presence is LIVE (live or idle), reusing the
    EXISTING liveness rule ``cmd_agents`` applies — no reinvented staleness.

    Loads the presence aggregate (``presence_view_path`` → ``views/presence.json``)
    and rebuilds it through ``views.build_presence`` so each entry carries a fresh
    ``liveness`` annotation derived by ``views.presence_liveness`` (the single
    ``FULCRA_COORD_STALE_HOURS`` rule the whole tool shares). An agent counts as
    live iff that band is ``live`` or ``idle``; a ``stale`` band — or no presence
    record at all — is NOT live (a crashed/forgotten session is exactly the dead
    inbox this safety net exists to surface).

    Returns:
      * a ``set`` (possibly empty) when the presence aggregate genuinely LOADED —
        the derived live roster;
      * ``None`` (sentinel) when the aggregate could NOT be read.
        ``remote.download_json`` returns ``None`` (it does NOT raise) on any
        transport failure — timeout / non-zero / OSError. We MUST distinguish that
        read failure from a genuinely-empty roster: treating a failed read as an
        empty live set made every directed directive look "addressed to a non-live
        agent" and produced a cry-wolf FLOOD on a single presence blip (the bug
        this sentinel fixes). The caller treats ``None`` as INDETERMINATE — emit a
        single "presence unavailable" signal, never enumerate. The caller also
        wraps this so a raise can never reach reconcile."""
    agg = remote.download_json(remote.presence_view_path(), backend=backend)
    # `agg is None` is the read-failure shape (download_json returns None on any
    # transport failure). A loaded-but-empty dict is a genuine roster, not a
    # failure — so only the None case is INDETERMINATE.
    if agg is None:
        return None
    # Strip any stored liveness so build_presence re-derives it from last_seen —
    # exactly how cmd_agents avoids trusting a possibly-stale rebuilt annotation.
    roster = views.build_presence([
        {k: v for k, v in a.items() if k != "liveness"}
        for a in agg.get("agents", [])
    ])
    return {
        a["agent"]
        for a in roster["agents"]
        if a.get("agent") and a.get("liveness") in ("live", "idle")
    }


def _assignee_acked(task: dict[str, Any], assignee: str) -> bool:
    """True when ``assignee`` has acknowledged receipt of ``task``.

    Mirrors the two durable ack representations ``schema.task_summary`` folds:
    an ``inbox_ack`` event from the assignee in the task's ``events`` log, OR an
    ``acked_by`` entry (the summary-only ack path, used when the body was
    temporarily unloadable). Either means the directive WAS delivered — the
    recipient saw it — so it is not undelivered regardless of presence state."""
    if assignee in (task.get("acked_by") or []):
        return True
    for e in task.get("events", []) or []:
        if e.get("type") == "inbox_ack" and e.get("by") == assignee:
            return True
    return False


def _summary_ack_map(*, backend: Optional[list[str]] = None) -> dict[str, set[str]]:
    """Load durable summary-only acks, degrading to an empty map on old buses."""
    try:
        summaries_view = remote.download_json(
            remote.view_remote_path("summaries"), backend=backend
        )
        acks: dict[str, set[str]] = {}
        for summary in (summaries_view or {}).get("summaries", []):
            if not isinstance(summary, dict):
                continue
            tid = summary.get("id")
            if not tid:
                continue
            acked_by = summary.get("acked_by") or []
            if not isinstance(acked_by, list):
                continue
            acks[tid] = {agent for agent in acked_by if agent}
        return acks
    except Exception:
        return {}


def _undelivered_directive_check(
    all_tasks: list[dict[str, Any]], *, backend: Optional[list[str]] = None
) -> dict:
    """SAFETY NET (report-only): open directives addressed to an OFFLINE agent.

    THE BUG THIS CATCHES — a real, demonstrated incident: agents sent directives
    to an identity whose live session had been presence-stale for days, so the
    messages rotted in a dead inbox. The bus accepted them into a void and never
    flagged that nobody was reading them. This check reconciles ``all_tasks``
    (which cmd_reconcile already has) against the LIVE presence set and surfaces
    every directed directive that is sitting un-picked-up in an offline/stale
    inbox.

    A task is UNDELIVERED when ALL hold:

      * DIRECTED directive — ``assignee`` is a concrete agent id: not ``"*"``
        (a broadcast has no single recipient inbox), not the human handle
        (humans aren't presence agents, so "offline" is meaningless for them),
        not empty.
      * OPEN and un-picked-up — ``status == "proposed"``. An ``active`` / ``done``
        / ``abandoned`` task was demonstrably received and acted on.
      * NOT acked by the assignee — no ``inbox_ack`` / ``acked_by`` from them
        (including durable summary-only ``acked_by`` priors; an ack means it was
        seen, i.e. delivered).
      * assignee NOT in the LIVE set — offline or stale presence (the dead inbox).

    Returns ``{"count": N, "undelivered": [{"id", "assignee", "age_days"}...]}``.
    The list is capped at ``_UNDELIVERED_LIST_CAP`` (so the timeline note / warn
    line stay bounded) and ``truncated`` is set True when the cap bit — but
    ``count`` is ALWAYS the true total, never the truncated length, so the signal
    is never silently dropped.

    INDETERMINATE presence (the anti-flood rule): we can ONLY confidently call a
    directive undelivered when we have a NON-EMPTY live set to compare against —
    i.e. we KNOW some agents are live and the assignee is not among them. Two
    cases mean we CANNOT distinguish "assignee offline" from "presence
    unavailable", and so we must NOT enumerate (a safety net that cries wolf on
    every read blip gets ignored):
      * ``_live_agent_ids() is None`` — the presence aggregate couldn't be read
        (``remote.download_json`` returned ``None`` on a transport failure); and
      * an EMPTY live set — no live agents we can see (failed-equivalent or a
        genuinely empty roster).
    In both, this returns ``{"count": 0, "undelivered": [], "presence_unavailable":
    True}`` so the caller emits ONE distinct "couldn't check delivery this cycle"
    note instead of flagging every open directive. ``presence_unavailable`` is
    ``False`` on the normal path so the existing undelivered warning still stands.

    REPORT-ONLY and best-effort, mirroring ``_event_parity_check`` /
    ``_event_dual_write_health`` / ``_directive_parity_check``: it NEVER mutates a
    task or view, NEVER reroutes (rerouting to a live role-holder is a separate
    later phase), and the OUTER try/except guarantees a malformed body or a
    presence-load failure can never raise into reconcile or change its exit code.
    On any internal error it returns a valid empty report."""
    try:
        human = identity.resolve_human()
        live = _live_agent_ids(backend=backend)
        # INDETERMINATE: a failed presence read (None sentinel) OR an empty live
        # set means we can't confirm any specific assignee is offline. Do NOT
        # enumerate — emit the distinct presence-unavailable signal instead of a
        # cry-wolf flood. (Only a NON-EMPTY live set lets us name a directive
        # undelivered: some agents ARE live and this assignee isn't among them.)
        if not live:
            return {"count": 0, "undelivered": [], "presence_unavailable": True}
        now = datetime.now(timezone.utc)
        summary_acks = _summary_ack_map(backend=backend)
        undelivered: list[dict[str, Any]] = []
        count = 0
        for t in all_tasks:
            tid = t.get("id")
            assignee = t.get("assignee")
            # Directed directive only: concrete agent id, not broadcast/human/empty.
            if not assignee or assignee == "*" or assignee == human:
                continue
            # Open and un-picked-up: only a `proposed` task is still awaiting receipt.
            if t.get("status") != "proposed":
                continue
            # Delivered if the recipient acked it (seen), regardless of presence.
            if _assignee_acked(t, assignee) or assignee in summary_acks.get(tid, set()):
                continue
            # The dead-inbox condition: the recipient is offline / stale.
            if assignee in live:
                continue
            count += 1
            if len(undelivered) < _UNDELIVERED_LIST_CAP:
                # created_at is the directive's true age; fall back to updated_at.
                created = t.get("created_at") or t.get("updated_at") or ""
                # _age_hours returns +inf for a missing/unparseable stamp. Render
                # that as the "?" sentinel rather than letting `inf` leak into the
                # warn line (an unreadable "inf days" is noise, not signal).
                hours = views._age_hours(created, now)
                age_days: Any = "?" if hours == float("inf") else round(hours / 24.0, 1)
                undelivered.append({
                    "id": tid,
                    "assignee": assignee,
                    "age_days": age_days,
                })
        report = {"count": count, "undelivered": undelivered,
                  "presence_unavailable": False}
        if count > len(undelivered):
            report["truncated"] = True
        return report
    except Exception:
        # Report-only: a failure here must never break reconcile. Degrade to a
        # valid empty report (the wrapping at the call site also guards, but this
        # keeps the helper itself non-raising for direct callers like `status`).
        # An internal raise is also an INDETERMINATE outcome — we couldn't check —
        # so mark presence_unavailable so the surfacing says "couldn't check" (the
        # honest, non-flooding signal) rather than implying a clean zero.
        return {"count": 0, "undelivered": [], "presence_unavailable": True}


#: Max items rendered per digest block before collapsing the tail into "+N more".
#: Keeps the timeline note bounded (a 284-event-in-two-days bus could otherwise
#: produce a wall of text) while always showing the most-salient head of each list.
# Headroom for the review-route sweep's deadline gate (B1). The sweep runs
# BEFORE retention in cmd_reconcile and does per-directive network fetches +
# potential full view-rebuild writes, so it must leave enough of the reconcile
# budget for retention (which gates on the same deadline) to still make
# progress. Mirrors _RETENTION_DEADLINE_HEADROOM_SECONDS' role.
def _detect_stale_claims(all_tasks: list[dict[str, Any]],
                         now: datetime) -> list[str]:
    """Collect the ids of active tasks holding an EXPIRED claim.

    Tolerant of imperfect bus data by construction (A1): a body missing ``id``
    contributes nothing instead of raising ``KeyError``, and an unparseable
    ``claim_expires_at`` is skipped instead of raising ``ValueError``. This runs
    early in cmd_reconcile, BEFORE build_all_views/upload — an uncaught raise
    here would abort the whole reconcile and fail every heartbeat tick (the
    heartbeat-outage class of bug). So it must never raise on a real-world body
    that merely lacks a field."""
    stale_claims: list[str] = []
    for t in all_tasks:
        tid = t.get("id")
        if not tid:
            continue  # an id-less body can't be named as a stale claim
        claim = t.get("claim", {})
        expires = claim.get("claim_expires_at")
        if not expires:
            continue
        exp_dt = views._parse_dt(expires)
        if exp_dt is None:
            continue  # unparseable expiry — skip, never raise
        if now > exp_dt and t.get("status") == "active":
            stale_claims.append(tid)
    return stale_claims


def _reconcile_rebuild_source_preserving_acks(
    all_tasks: list[dict[str, Any]], *, backend: Optional[list[str]] = None
) -> list[dict[str, Any]]:
    """Summarize loaded bodies while preserving summary-only inbox acks.

    ``inbox --ack`` can suppress a visible directive by writing only the summaries
    aggregate when the task body is temporarily unloadable. A later reconcile may
    successfully load that body, but the body still lacks the ``inbox_ack`` event.
    If reconcile rebuilt views from raw bodies alone, the ack would disappear and
    the directive would re-notify. Treat the current aggregate's ``acked_by`` as a
    durable prior fact, matching ``_load_summaries_for_rebuild`` on normal writes.
    """
    prior_acks: dict[str, set[str]] = {}
    try:
        for summary in _load_task_summaries(backend=backend):
            tid = summary.get("id")
            if tid:
                prior_acks[tid] = set(summary.get("acked_by") or [])
    except Exception:
        prior_acks = {}

    rebuild_source: list[dict[str, Any]] = []
    for task in all_tasks:
        summary = schema.task_summary(task)
        tid = summary.get("id")
        if tid in prior_acks:
            summary["acked_by"] = sorted(
                set(summary.get("acked_by") or []) | prior_acks[tid]
            )
        rebuild_source.append(summary)
    return rebuild_source


def _event_parity_check(*, backend: Optional[list[str]] = None) -> dict:
    """Compare each task snapshot against the fold of its event log.

    Phase-1 safety net: surfaces drift as health debt (the mutable file is
    still authoritative, so drift is REPORTED, never acted on).

    Phase-2a broadening: when ``events.fold_is_complete(folded)`` is True
    (at least one full-task snapshot event has been applied), the check
    compares ALL durable task fields — not just status — giving a precise
    whole-task parity signal.

    Root cause C2 broadening: for legacy delta-only tasks where the fold is NOT
    complete, the check no longer compares status alone.  It compares every field
    the fold ACTUALLY carries (``set(folded.keys()) - ignore``) against the file,
    but ONLY those fields — a field the fold never saw is skipped, so genuinely
    partial pre-migration payloads can't false-positive.  status is one of the
    fold's keys, so the original status drift is still caught.

    Root cause C1 — ack divergence (report-only).  The AUTHORITATIVE ack set for a
    task is ``summaries.acked_by`` (the summaries view), NOT the fold: io.py UNIONS
    prior acks into each summary because the in-task event log is truncated to
    ``MAX_EVENTS_INLINE``, and ``inbox._ack_summary_only`` records an ack in
    ``summaries`` with NO event shard at all.  So the FOLD (what a post-flip
    events-as-source read would return) can be MISSING acks the summaries view has —
    and a flip would then re-notify an already-acked directive.  The summaries view
    is loaded ONCE before the loop and each task's fold ``acked_by`` is cross-checked
    against it; any task whose fold is missing >=1 durable ack is recorded in
    ``ack_drift_task_ids`` AND folded into ``drift_task_ids`` so the flip-readiness
    gate (drift>0) trips.  A missing / old-bus summaries view degrades to no
    ack-drift, never raises.  This is report-only; the durable write-path fix that
    makes the fold carry every ack is deferred to a later phase.

    Fields excluded from the full-task comparison (the ignore-set):

    * ``_applied_event_count`` — bookkeeping added by ``fold_task``; absent
      from the live file entirely.
    * ``updated_at`` — updated on every write; legitimately differs between a
      point-in-time snapshot and the current live file.
    * ``last_touched_by``, ``last_touched_in`` — same as ``updated_at``;
      stamps the most-recent writer, which is always the live file's most
      recent write, not the snapshot instant.
    * ``events`` — the in-task human-readable event log grows independently
      with every write and is NOT part of the machine-readable event stream;
      it legitimately lags or diverges from the canonical event shard.

    These fields are expected to differ and their difference is not drift.
    Any other top-level field that differs IS drift and will be flagged.

    Only tasks that have at least one event shard are compared — tasks with no
    events were written before dual-write was introduced and are not drift by
    definition (they haven't been through the dual-write path yet).

    The tasks prefix is ``{remote_root()}/tasks/`` and the events prefix is
    ``{remote_root()}/events/tasks/`` — completely separate directory trees, so
    listing the tasks prefix never returns event shards. The ``.json`` filter and
    ``/events/`` guard are belt-and-suspenders against any future layout change.

    Cost: O(N) remote I/O — one ``download_json`` per task snapshot plus one
    ``read_events`` (a ``list_json`` sweep) per task's event prefix. Acceptable
    for a Phase-2a diagnostic on the current bus scale (reconcile already sweeps
    all tasks).
    """
    # Union set of every task id that drifts for ANY reason (field/status drift
    # OR ack divergence). A task that drifts for multiple reasons is counted
    # ONCE here, so ``drift``/``drift_task_ids`` never double-count.
    drift_set: set[str] = set()
    # Separate breakdown: tasks whose fold is missing >=1 durable ack the
    # authoritative summaries view holds (root cause C1, report-only).
    ack_drift_ids_set: set[str] = set()
    checked = 0
    # SIGNAL A (coverage liveness): "drift == 0" is satisfiable two ways — the
    # fold faithfully reconstructs every task, OR the fold folded nothing / there
    # are no events so there is nothing to disagree with. These additive counts
    # make the difference visible so a host that folded nothing can no longer read
    # green just because there was nothing to compare.
    #   tasks_total      — every task .json file iterated under tasks/.
    #   tasks_with_events — tasks that had >=1 event and were compared (== checked).
    #   folds_complete    — tasks whose fold_is_complete (trustworthy full-snapshot).
    tasks_total = 0
    folds_complete = 0

    # Fields excluded from BOTH the full-task and delta-only comparisons — shared
    # so the two branches stay consistent. See the docstring for why each differs
    # legitimately between a point-in-time fold and the live file.
    ignore = {"_applied_event_count", "updated_at", "last_touched_by",
              "last_touched_in", "events"}

    # C1: load the AUTHORITATIVE ack view ONCE, before the loop. summaries.acked_by
    # is the durable ack set; the fold can lag it (MAX_EVENTS_INLINE truncation, or
    # inbox._ack_summary_only acks that emit NO event shard). A missing / old-bus
    # summaries view degrades to an empty map -> no ack drift flagged, never raises.
    summ_acks: dict[str, set[str]] = {}
    try:
        summaries_view = remote.download_json(
            remote.view_remote_path("summaries"), backend=backend
        )
        for s in (summaries_view or {}).get("summaries", []):
            if not isinstance(s, dict):
                continue
            tid = s.get("id")
            if not tid:
                continue
            acked_by = s.get("acked_by") or []
            if not isinstance(acked_by, list):
                continue
            summ_acks[tid] = set(acked_by)
    except Exception:
        summ_acks = {}

    tasks_prefix = f"{remote.remote_root()}/tasks/"
    task_paths = remote.list_files(tasks_prefix, backend=backend)
    for path in task_paths:
        # Only process actual task JSON files directly under tasks/ — skip
        # anything that looks like an events shard or a non-JSON file.
        if not path.endswith(".json"):
            continue
        if "/events/" in path:
            continue
        # Count every well-formed task file BEFORE the events gate, so
        # tasks_total reflects the full bus task population (the denominator the
        # flip gate's coverage check divides tasks_with_events by). A task with no
        # events still counts toward the total — it just isn't compared.
        snap = remote.download_json(path, backend=backend)
        if not snap or "id" not in snap:
            continue
        tasks_total += 1
        evs = _eventlog.read_events(snap["id"], backend=backend)
        if not evs:
            continue  # not yet dual-written (pre-migration task) — not drift
        checked += 1
        folded = _events.fold_task(evs)
        fold_complete = _events.fold_is_complete(folded)
        if fold_complete:
            folds_complete += 1
        if fold_complete:
            # Compare the durable task fields. Exclude bookkeeping the fold adds
            # (_applied_event_count) and fields that legitimately differ between a
            # point-in-time snapshot and the live file: updated_at / last_touched_*
            # move on every write, and the in-task events[] log grows independently.
            a = {k: v for k, v in folded.items() if k not in ignore}
            b = {k: v for k, v in snap.items() if k not in ignore}
            if a != b:
                drift_set.add(snap["id"])
        else:
            # delta-only: the fold is reconstructed from partial deltas, so only
            # compare fields the fold ACTUALLY carries (skip anything it never saw —
            # that would false-positive on genuinely-partial pre-migration payloads).
            # Reuse the same ignore-set as the full-task branch. status is one of the
            # fold's keys, so the original status-only behaviour is still covered.
            keys = set(folded.keys()) - ignore
            if any(folded.get(k) != snap.get(k) for k in keys):
                drift_set.add(snap["id"])

        # C1: regardless of fold completeness, the fold must carry every durable
        # ack the summaries authority holds. If it's missing any, a post-flip read
        # would re-notify an already-acked directive — surface it (report-only).
        missing = summ_acks.get(snap["id"], set()) - set(folded.get("acked_by") or [])
        if missing:
            ack_drift_ids_set.add(snap["id"])
            drift_set.add(snap["id"])

    drift_task_ids = sorted(drift_set)
    ack_drift_task_ids = sorted(ack_drift_ids_set)
    return {
        "checked": checked,
        "drift": len(drift_task_ids),
        "drift_task_ids": drift_task_ids,
        "ack_drift": len(ack_drift_task_ids),
        "ack_drift_task_ids": ack_drift_task_ids,
        # SIGNAL A — additive coverage/liveness counts (existing keys unchanged):
        "tasks_total": tasks_total,
        "tasks_with_events": checked,  # same as checked; named for the flip gate
        "folds_complete": folds_complete,
    }


def _directive_parity_check(*, backend: Optional[list[str]] = None) -> dict:
    """Compare each first-class directive record against its back-ref task.

    Phase 3b Task 4 safety net — the strangler-fig dual-write writes a
    ``directives/<id>.json`` LWW snapshot mirroring each directive-creating task
    (id ``DIR-T-<task_id>``), but NOTHING reads the directive store for
    correctness yet (the legacy task-with-assignee stays authoritative). This
    sub-pass folds the STORED directive record against the EXPECTED mirror of its
    current back-ref task and surfaces divergence as health debt — REPORT-ONLY,
    exactly like ``_event_parity_check``: drift is recorded, never acted on, and
    a failure here can never change reconcile's exit code or mutate anything.

    THE TOP-LEVEL-ONLY FILTER (load-bearing). ``remote.directives_prefix()`` now
    contains SUB-LOG SUBTREES as well as top-level records:

      * top-level record : ``directives/<id>.json``
      * ack sub-log shard: ``directives/<id>/acks/<agent-slug>.json``
      * route sub-log shard: ``directives/<id>/routing/<event_id>.json``

    ``remote.list_files(directives_prefix())`` returns ALL of these. The check
    MUST enumerate top-level records ONLY — a path that, after stripping the
    prefix, has NO further ``/`` AND ends in ``.json``. A sub-log shard always
    has a ``/`` after the directive id (``<id>/acks/...``), so the
    no-inner-slash test rejects it. WITHOUT this filter every ack/route shard
    would be mis-counted as a directive record and produce massive false drift
    (a shard has no ``task_id`` back-ref shape). This is pinned by
    ``test_directive_parity_counts_top_level_records_only``.

    Per top-level record:

      * Load it (``remote.download_json``). Read its ``task_id`` back-ref. No
        ``task_id`` -> SKIP (gate): a directly-authored / orphan directive has no
        task to compare against, so it is neither checked nor drift.
      * Load the back-ref task via the shared task-load path (``_load_task``).
        Task gone -> count as ``orphan`` and SKIP (the back-ref dangles; nothing
        to compare, and a dangling ref is not the directive record's "drift").
      * Recompute the EXPECTED mirror ``directives.directive_from_task(task)``,
        then fold in the SAME durable ack union ``dual_write`` applies
        (``read_directive_acks(id)`` ∪ the task-derived acks already on the
        mirror) so the expected ``acked_by`` reflects every durable ack — even
        ones the task's capped inline event log has since dropped.
      * Compare the mapped fields to the STORED record:
          - ``directive_type`` (exact)
          - ``audience``       (exact — maps the task's current assignee)
          - ``status``         (exact — the mapped directive status)
          - ``acked_by`` as a SET: the meaningful drift is the stored record
            MISSING an acker the task/sub-log holds (``expected - stored``
            non-empty). A stored record that is a SUPERSET of expected is fine —
            an extra acker is never a re-notify risk — so we do NOT flag that.

    Volatile fields (``created_at`` / ``updated_at``) are IGNORED, mirroring the
    way ``_event_parity_check`` ignores its volatile set: a stored snapshot and a
    freshly-recomputed mirror legitimately differ on write timestamps and that is
    not drift.

    Returns ``{checked, drift, drift_task_ids, orphans}``. ``checked`` counts only
    top-level records that had a comparable back-ref task (shards and orphans are
    excluded). ``orphans`` counts records whose back-ref task is gone.

    Cost: O(D) remote I/O — one ``download_json`` per directive record plus one
    task load (and a sub-log ``list_json``) per comparable record. Acceptable for
    a reconcile diagnostic at the current bus scale.
    """
    drift_set: set[str] = set()
    checked = 0
    orphans = 0

    # Volatile fields (created_at / updated_at) are ignored BY CONSTRUCTION here:
    # rather than diff whole records and subtract an ignore-set (the
    # _event_parity_check approach), we compare only an explicit ALLOW-LIST of the
    # mapped fields below (directive_type / audience / status / acked_by-as-set).
    # A write timestamp is never in that list, so it can never register as drift.

    prefix = remote.directives_prefix()
    try:
        paths = remote.list_files(prefix, backend=backend)
    except Exception:
        paths = []

    for path in paths:
        # TOP-LEVEL-ONLY FILTER (load-bearing): strip the prefix and accept only a
        # direct child ``<id>.json`` — no further '/' (which would make it a
        # sub-log shard under ``<id>/acks/`` or ``<id>/routing/``).
        rel = path[len(prefix):] if path.startswith(prefix) else path
        if "/" in rel:
            continue  # sub-log shard (acks/ or routing/) — never a directive record
        if not rel.endswith(".json"):
            continue

        stored = remote.download_json(path, backend=backend)
        if not isinstance(stored, dict):
            continue

        task_id = stored.get("task_id")
        if not task_id:
            continue  # orphan / directly-authored directive — can't compare (gate)

        task = _load_task(task_id, backend=backend)
        if not task:
            orphans += 1  # back-ref task gone — dangling ref, nothing to compare
            continue

        checked += 1

        # Recompute the EXPECTED mirror, then fold in the SAME durable ack union
        # dual_write applies, so expected.acked_by reflects every durable ack.
        try:
            expected = _directives.directive_from_task(task)
            directive_id = expected.get("id")
            if directive_id:
                try:
                    sublog_acks = _directives.read_directive_acks(
                        directive_id, backend=backend)
                    if sublog_acks:
                        expected["acked_by"] = sorted(
                            set(expected.get("acked_by") or []) | set(sublog_acks))
                except Exception:
                    pass  # sub-log read miss -> compare against task-derived acks only
        except Exception:
            # A mapping failure on one record must not abort the whole sweep.
            continue

        # Compare the mapped fields. directive_type / audience / status are exact;
        # acked_by compares as SETS — the meaningful drift is the stored record
        # MISSING an acker the expected (task/sub-log) set holds.
        drifted = False
        for field in ("directive_type", "audience", "status"):
            if stored.get(field) != expected.get(field):
                drifted = True
                break
        if not drifted:
            missing_ackers = set(expected.get("acked_by") or []) - set(
                stored.get("acked_by") or [])
            if missing_ackers:
                drifted = True
        if drifted:
            drift_set.add(task_id)

    drift_task_ids = sorted(drift_set)
    return {
        "checked": checked,
        "drift": len(drift_task_ids),
        "drift_task_ids": drift_task_ids,
        "orphans": orphans,
    }


def cmd_reconcile(args: Any, backend: Optional[list[str]] = None) -> int:
    """Repair views and resolve pending operation markers."""
    import time
    _info("Reconciling coordination views...")
    t0 = time.monotonic()
    timeout = env_int("FULCRA_COORD_RECONCILE_TIMEOUT_SECONDS", 90)
    deadline = t0 + timeout

    markers = cache.list_op_markers()
    needs_repair = [m for m in markers if m.get("needs_reconcile")]
    if needs_repair:
        _info(f"  {len(needs_repair)} operation(s) need view repair.")

    try:
        all_tasks = _load_all_tasks(backend=backend)
    except Exception as e:
        _warn(f"Could not load remote index: {e}")
        all_tasks = cache.list_cached_tasks()

    _info(f"  {len(all_tasks)} task(s) loaded.")

    now = datetime.now(timezone.utc)
    stale_claims = _detect_stale_claims(all_tasks, now)

    if stale_claims:
        _warn(f"  Stale claims detected: {stale_claims}")

    if time.monotonic() - t0 > timeout:
        _err("Reconcile timeout exceeded.")
        return 1

    all_views = views.build_all_views(
        _reconcile_rebuild_source_preserving_acks(all_tasks, backend=backend)
    )
    view_items = list(all_views.items())

    # Cache every view locally regardless of upload outcome — matches the prior
    # sequential loop, which wrote the cache for each view before attempting its
    # upload. Done up front (main thread) so the cache write is never racy.
    for view_name, view_data in view_items:
        cache.write_cached_view(view_name, view_data)

    # Upload the views CONCURRENTLY (PERF), the same way _write_task_and_views
    # (P1) does: remote.upload_json is thread-safe (each call writes a unique
    # tempfile + runs an independent subprocess; remote.py holds no shared
    # mutable state), so a small pool collapses the ~50 serial uploads into one
    # round-trip's wall-time — the second half of the reconcile-timeout fix.
    # Semantics are preserved exactly: per-view success is collected, any
    # failure (False OR a raise) lands in `failures`, and the partial-upload
    # handling below is unchanged.
    failures = []

    def _upload_one(item):
        view_name, view_data = item
        remaining = deadline - time.monotonic()
        # BUG 6b: the old guard was `remaining <= 0` with `timeout=max(1, int(
        # remaining))`. With 0<remaining<1 that floored the per-view timeout UP to
        # 1s, letting an upload run up to ~1s PAST the global reconcile deadline.
        # Treat any sub-1s budget as past-deadline (skip, count as a failed view)
        # so the deadline is a hard ceiling — consistent with the `<= 0` guard.
        if remaining < 1:
            return view_name, False
        vpath = _view_name_to_remote(view_name)
        # Treat a RAISING upload as a failed view, not an escape hatch: an
        # unguarded pool.map would re-raise out of cmd_reconcile, bypassing the
        # failures -> "preserve markers, return 1" path and crashing the
        # heartbeat. Catching keeps the contract: any failure is a failed view.
        try:
            ok = remote.upload_json(view_data, vpath, backend=backend,
                                    timeout=int(remaining))
        except Exception:
            ok = False
        return view_name, ok

    max_workers = min(8, len(view_items)) or 1
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        for view_name, ok in pool.map(_upload_one, view_items):
            if not ok:
                failures.append(view_name)

    if time.monotonic() - t0 > timeout:
        _err("Reconcile timeout exceeded mid-upload.")
        ops_log.log_op("reconcile", status="timeout")
        return 1

    # Rebuild the presence aggregate from the durable per-agent presence records,
    # mirroring how the task views self-heal here. Best-effort: a presence rebuild
    # failure must not fail a task-view reconcile, so it is reported but does not
    # count toward `failures`.
    _reconcile_presence(backend=backend)

    # Liveness-aware reroute sweep (best-effort; never fails a reconcile tick).
    # Runs AFTER the presence rebuild so it reads the freshly-reconciled
    # aggregate. Considers only kind:review directives; reroutes never-acted
    # reviews whose assignee fell below liveness floor, escalates on cap/miss,
    # freezes accepted-then-stalled ones. Whichever machine reconciles first
    # wins; others converge via the stale-observation re-read inside the sweep.
    try:
        _sweep_review_routes(all_tasks, backend=backend, now=now, deadline=deadline)
    except Exception:
        pass

    # Retention pass (best-effort, throttled to ~once/day, bounded + time-budgeted
    # against THIS reconcile's deadline so it never double-counts the 90s ceiling).
    # Never raises into the tick; logs its tally.
    try:
        ret = _run_retention(all_tasks, now=now, deadline=deadline, backend=backend)
        if not ret.get("skipped"):
            _info(f"  Retention: archived {ret['archived']} task(s) "
                  f"(deferred {ret['deferred']}), expired {ret.get('expired_broadcasts', 0)} "
                  f"broadcast(s), pruned {ret['pruned_markers']} marker(s), "
                  f"{ret['pruned_presence']} dead presence, {ret.get('pruned_health', 0)} health, "
                  f"{ret.get('pruned_continuity', 0)} continuity, "
                  f"{ret.get('pruned_events', 0)} events, "
                  f"{ret.get('pruned_provenance', 0)} prov.")
    except Exception as e:
        _warn(f"  Retention pass error (skipped): {e}")

    if failures:
        _warn(f"  View upload failures: {failures}")
        ops_log.log_op("reconcile", status="partial", detail=f"failed views: {failures}")
        # Do NOT clear op markers — views are still broken and need another reconcile run.
        return 1

    # --- Self-reported per-host health record (spec v2 §1) -------------------
    # SUCCESS POINT: we are PAST the `if failures: return 1` guard above, so
    # failures == [] here. The health write is its OWN failure-isolated upload —
    # NOT a member of the parallel view-upload batch (which completes BEFORE the
    # failure verdict, so a batched health file would upload even on a FAILING
    # reconcile and falsely read healthy). It is also NOT gated on the best-effort
    # sub-passes (_sweep_review_routes / _run_retention ran above and never fail
    # the tick); gating on their flakiness would suppress a healthy heartbeat. A
    # health-write failure logs and NEVER changes this tick's return code.
    try:
        retention_last_run = None
        try:
            rmark = remote.download_json(remote.retention_marker_path(now), backend=backend)
            if isinstance(rmark, dict):
                retention_last_run = rmark.get("at") or rmark.get("date")
        except Exception:
            retention_last_run = None
        listener_last_fire = None
        try:
            surface = _inbox_surface_path(identity.resolve_agent())
            if surface.exists():
                listener_last_fire = _iso_z(datetime.fromtimestamp(
                    surface.stat().st_mtime, tz=timezone.utc))
        except Exception:
            listener_last_fire = None
        record = _build_health_record(
            now=now,
            duration_s=round(time.monotonic() - t0, 3),
            tasks_loaded=len(all_tasks),
            views_refreshed=len(all_views),
            repair_backlog=len(needs_repair),
            retention_last_run=retention_last_run,
            listener_last_fire=listener_last_fire,
            bus_task_count=len(all_tasks),
        )
        # Phase-1 event-parity sub-pass: fold each task's event log and compare
        # status to the mutable snapshot. Best-effort — any error is swallowed and
        # the result, whether present or absent, NEVER changes the reconcile exit
        # code. Drift is recorded in the health record as health debt only; the
        # mutable file remains authoritative until Phase 2.
        try:
            parity = _event_parity_check(backend=backend)
            record["event_parity"] = parity
        except Exception as _pe:
            # Best-effort, but NOT silent: a checker that eats its own errors
            # would report zero drift and look healthy while actually being
            # broken — the worst failure mode for the very pass meant to catch
            # dual-write problems. Surface it (guarded so the warn can't break
            # reconcile either). record["event_parity"] is simply absent.
            try:
                _warn(f"  Event-parity check skipped (error): {_pe}")
            except Exception:
                pass
        # SIGNAL C: surface recent dual-write append failures so a host whose
        # event-append is silently failing is no longer invisible. Best-effort and
        # self-guarding (the helper never raises); wrapped here too so a future
        # change to it can never break reconcile.
        try:
            record["event_dual_write"] = _event_dual_write_health()
        except Exception as _de:
            try:
                _warn(f"  Dual-write health skipped (error): {_de}")
            except Exception:
                pass
        # Phase 3b Task 4 directive-parity sub-pass: fold each first-class
        # directive record against its back-ref task and surface divergence as
        # health debt. REPORT-ONLY and best-effort — the directive store is not
        # authoritative (nothing reads it for correctness), so any error is
        # swallowed and the result, present or absent, NEVER changes reconcile's
        # exit code. Wrapped exactly like the event-parity pass above.
        try:
            record["directive_parity"] = _directive_parity_check(backend=backend)
        except Exception as _dpe:
            # Best-effort but not silent: a checker that ate its own error would
            # report green while broken. Surface it (guarded so the warn can't
            # break reconcile either); record["directive_parity"] is simply absent.
            try:
                _warn(f"  Directive-parity check skipped (error): {_dpe}")
            except Exception:
                pass
        # SAFETY NET: directives addressed to an OFFLINE/stale agent that were
        # never picked up — the dead-inbox bug. Report-only and best-effort: it
        # never mutates or reroutes, and the wrapping guarantees a failure can
        # never change reconcile's exit code (mirrors the parity passes above).
        # all_tasks is already in hand, so this adds no remote round-trip beyond
        # the single presence-aggregate read inside the check.
        try:
            ud = _undelivered_directive_check(all_tasks, backend=backend)
            record["undelivered_directives"] = ud
            # Distinct signals — a genuine presence OUTAGE must stay LOUD (never
            # silent) but as "couldn't check," NOT as "N directives rotting." When
            # the presence aggregate was unreadable / no live agents were visible,
            # the check is INDETERMINATE: emit ONE note instead of flooding the log
            # with every open directive (the cry-wolf bug). Only a real, non-empty
            # live set yields the "N undelivered" enumeration.
            if ud.get("presence_unavailable"):
                _warn(
                    "  ⚠ presence aggregate unavailable — could not check "
                    "directive delivery this cycle"
                )
            elif ud.get("count"):
                # LOUD when >0: a maintainer reading the reconcile log must SEE that
                # directives are rotting in dead inboxes, not have it buried in JSON.
                ids = [u.get("id") for u in ud.get("undelivered", [])]
                _warn(
                    f"  ⚠ {ud['count']} directive(s) undelivered — assignee "
                    f"offline/stale, never picked up: {ids}"
                )
        except Exception as _ue:
            # Best-effort but not silent: a checker that ate its own error would
            # look healthy while the dead-inbox bug it exists to catch festers.
            # Surface it (guarded so the warn can't break reconcile either);
            # record["undelivered_directives"] is simply absent.
            try:
                _warn(f"  Undelivered-directive check skipped (error): {_ue}")
            except Exception:
                pass
        # Key the health record by the stable MACHINE host, not the per-cwd agent:
        # the health surface is per-host ("is this machine reconciling?"), and every
        # worktree/clone on a machine runs the same reconcile against the same bus.
        # Per-cwd keying made each worktree write its own health/<agent>.json, so a
        # deleted worktree left an orphan that dragged fleet status to a false
        # "outage" until the 30-day prune. One record per machine fixes that at the
        # source (and assess_infra_health also judges freshest-per-host, so legacy
        # per-cwd orphans already on the bus are superseded too). Fall back to the
        # agent id only if host is somehow absent.
        slug = views.agent_slug(record.get("host") or identity.resolve_agent())
        if not remote.upload_json(record, remote.health_remote_path(slug), backend=backend):
            _warn("  Health record upload failed (best-effort; tick unaffected).")
    except Exception as e:
        _warn(f"  Health record write error (skipped): {e}")
    # ------------------------------------------------------------------------

    for m in needs_repair:
        cache.clear_op_marker(m["op_id"])

    ops_log.log_op("reconcile", status="ok", detail=f"{len(all_tasks)} tasks, {len(all_views)} views")
    _info(f"  Reconcile complete. {len(all_views)} views refreshed.")
    return 0
