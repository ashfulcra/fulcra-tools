"""CLI command implementations for fulcra-coord.

Each command accepts parsed argparse namespace and an optional backend=
override for testing without live Fulcra access.
"""

from __future__ import annotations

import concurrent.futures
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import cache, remote, schema, views, log as ops_log, identity
from . import env_int
# Direct store-module import for ONE read-only diagnostic: the transport's
# ``last_upload_error`` failure observable (see its comment in the store).
# It must be read as a LIVE module attribute — ``remote``'s function re-exports
# would not track the store mutating it — and importing the store here adds no
# new dependency edge (cli already reaches it through ``remote``).
from fulcra_coord_files import store as _files_store
from . import events as _events, eventlog as _eventlog
from . import directives as _directives
from . import loops as _loops
from . import roles as _roles
from . import role_ops as _role_ops
from . import continuity_ops as _continuity_ops
# Role-checkpoint + park commands (continuity spec 2026-06-10). Re-exported so
# the `checkpoint`/`park` dispatch (entry.py) resolves through the same
# _cli.cmd_* convention as every other command. continuity_ops.py never
# imports cli.
from .continuity_ops import cmd_checkpoint, cmd_park  # noqa: F401
# Leaf-utility modules extracted from this file. Re-exported under the historical
# underscore-prefixed names so every internal call site AND the test patch targets
# (fulcra_coord.cli._info / ._err / ...) keep resolving unchanged — output.py /
# timeutil.py do not import cli, so there is no import cycle.
from .output import err as _err, warn as _warn, info as _info, print_json as _print_json
from .timeutil import iso_z as _iso_z, now_iso as _now_iso
from .textfmt import age_str as _age_str
# Retention / archival subsystem extracted from this file. Re-exported under the
# historical underscore-prefixed names so every remaining caller here
# (cmd_reconcile -> _run_retention; cmd_search / cmd_restore -> the cold-index
# readers) AND the test patch targets (fulcra_coord.cli._archive_task / ...)
# keep resolving. retention.py depends only on lower layers and never imports
# cli, so there is no import cycle.
from .retention import (
    _archive_task, _read_index_shard,
    _list_index_shards, _claim_retention_marker,
    _prune_markers, _prune_dead_presence, _prune_dead_health, _run_retention,
    cmd_search, cmd_restore,
)
# Shared remote-task load/cache layer extracted from this file. Re-exported under
# the historical underscore-prefixed names so every cli-resident caller
# (cmd_reconcile / the parity checks / _reconcile_rebuild_source_preserving_acks)
# AND the unmigrated test patch targets (fulcra_coord.cli._load_all_tasks / ...)
# keep resolving. io.py depends only on lower layers and never imports cli,
# so there is no import cycle.
from .io import (
    _confirmed_absent, _load_all_tasks, _load_task_summaries,
    _load_all_tasks_by_listing, _load_summaries_for_rebuild, _load_task,
    _updated_at_key,
)
# Presence subsystem extracted from this file. Re-exported under the historical
# names so the command dispatch (cmd_connect/cmd_workstream/cmd_presence),
# cmd_reconcile's _reconcile_presence call, and the test patch targets keep
# resolving. presence.py never imports cli.
from .presence import (
    _upsert_presence_aggregate, _write_presence, cmd_connect,
    cmd_workstream, cmd_presence, _reconcile_presence,
    _load_presence_agents,
    # F5: the "presence could not be read AT ALL this tick" sentinel —
    # _reconcile_presence returns it when the per-agent read came back partial
    # AND the previous aggregate is unreadable; reconcile's liveness-sensitive
    # sub-passes must then take no action (see cmd_reconcile).
    PRESENCE_READ_ERROR as _PRESENCE_READ_ERROR,
    # C5: the merge-safe capability RMW pair `roles claim`/`release` use to
    # keep @role delivery (capabilities) in step with the lease layer.
    add_capabilities as _presence_add_capabilities,
    remove_capability as _presence_remove_capability,
)
# Read-only situational-awareness commands extracted from this file. Re-exported so
# the command dispatch (entry.py) and the test imports of these commands keep
# resolving. query.py never imports cli.
from .query import (cmd_status, cmd_board, cmd_agents, cmd_needs_me,
                    cmd_resume, cmd_briefing)
# Task write pipeline extracted from this file. Re-exported under the historical
# names so every write command (cmd_start/update/block/pause/done/abandon/tell/
# broadcast/assign/inbox/request-review) that calls _write_task_and_views, plus the
# test patch targets, keep resolving. writepipe.py never imports cli.
from .writepipe import (
    _stamp_session_pointer, _write_task_and_views,
    _view_name_to_remote, _try_merge,
    # Shared fingerprint function: reconcile records fingerprints on its
    # successful uploads (refreshing the write path's skip baseline), so the
    # two sites can never disagree on what "unchanged" means. Reconcile itself
    # never skips — see the division-of-labor comment in cmd_reconcile.
    _view_fingerprint,
)
# Liveness-aware reviewer routing extracted from this file. Re-exported so
# cmd_reconcile's _sweep_review_routes call, the request-review dispatch, and the
# test patch targets keep resolving. routing_ops.py never imports cli.
from .routing_ops import (
    _review_pool,
    _escalate_review_to_human, cmd_request_review,
    cmd_review_done,
    _reroute_minutes, _reroute_max, _accepted_stall_hours,
    _review_accepted_by_assignee, _classify_review, _sweep_review_routes,
)
# Coordination-loop return leg (spec 2026-06-09). Re-exported so the `respond`
# command dispatch (entry.py) resolves through the same _cli.cmd_* convention as
# every other command. loop_ops.py never imports cli. load_loop_records /
# evidence_ids_for are the shared records-sweep + bounded-evidence-probe pair
# consumed by _loop_health_check here and by query/digest (which may not import
# cli) — the single home for the top-level-shard filter.
from .loop_ops import cmd_respond, load_loop_records, evidence_ids_for
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
    cmd_tell, cmd_broadcast, cmd_later, cmd_remind, cmd_handoff, cmd_assign,
    cmd_start, cmd_update, cmd_block, cmd_pause, cmd_snapshot, cmd_done,
    cmd_abandon,
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

#: Sentinel distinguishing "caller did not supply this tick-scoped snapshot —
#: load it yourself" from "caller supplied it and it was genuinely absent/None"
#: (None is meaningful for these views: it is the read-failure / missing shape).
#: Used by the E4 snapshot-sharing params so cmd_reconcile can load each shared
#: view ONCE per tick and thread it through every sub-pass, while direct
#: callers (status, tests) keep the load-it-yourself behaviour.
_UNSET: Any = object()


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


def _live_agent_ids(
    *, backend: Optional[list[str]] = None, agg: Any = _UNSET,
) -> Optional[set[str]]:
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
        wraps this so a raise can never reach reconcile.

    ``agg`` (E4 snapshot sharing): cmd_reconcile passes the aggregate it just
    REBUILT from the per-agent records, so the tick never re-downloads a view
    it produced moments earlier. Default (the ``_UNSET`` sentinel) keeps the
    download for direct callers; an explicit ``None`` means the caller KNOWS
    the aggregate is unavailable and gets the INDETERMINATE verdict."""
    if agg is _UNSET:
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


def _summary_ack_map(
    *, backend: Optional[list[str]] = None, summaries_view: Any = _UNSET,
) -> dict[str, set[str]]:
    """Load durable summary-only acks, degrading to an empty map on old buses.

    ``summaries_view`` (E4 snapshot sharing): cmd_reconcile passes the view it
    already downloaded this tick (possibly None — absent stays absent for the
    whole tick); the ``_UNSET`` default keeps the download for direct callers."""
    try:
        if summaries_view is _UNSET:
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
    all_tasks: list[dict[str, Any]], *, backend: Optional[list[str]] = None,
    presence_view: Any = _UNSET, summaries_view: Any = _UNSET,
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
    task or view, NEVER reroutes (rerouting to a live role-holder is
    deliberately out of scope here), and the OUTER try/except guarantees a malformed body or a
    presence-load failure can never raise into reconcile or change its exit code.
    On any internal error it returns a valid empty report.

    ``presence_view`` / ``summaries_view`` (E4 snapshot sharing): cmd_reconcile
    threads the snapshots it already holds this tick; the ``_UNSET`` defaults
    keep the self-loading behaviour for direct callers."""
    try:
        human = identity.resolve_human()
        live = _live_agent_ids(backend=backend, agg=presence_view)
        # INDETERMINATE: a failed presence read (None sentinel) OR an empty live
        # set means we can't confirm any specific assignee is offline. Do NOT
        # enumerate — emit the distinct presence-unavailable signal instead of a
        # cry-wolf flood. (Only a NON-EMPTY live set lets us name a directive
        # undelivered: some agents ARE live and this assignee isn't among them.)
        if not live:
            return {"count": 0, "undelivered": [], "presence_unavailable": True}
        now = datetime.now(timezone.utc)
        summary_acks = _summary_ack_map(backend=backend,
                                        summaries_view=summaries_view)
        undelivered: list[dict[str, Any]] = []
        count = 0
        for t in all_tasks:
            tid = t.get("id")
            assignee = t.get("assignee")
            # Directed directive only: concrete agent id, not broadcast/human/empty.
            # A ROLE audience (@<role>) is NOT a concrete recipient — it resolves at
            # read time to whatever LIVE agent(s) hold the role (views.inbox_for), so
            # it is never "offline" the way a frozen id can be, and `assignee in live`
            # (a set of concrete ids) would ALWAYS be False for it and falsely flag it
            # undelivered. Skip role audiences here; live-holder rerouting for them is
            # a separate, out-of-scope follow-on. This keeps the undelivered detector
            # backward-compatible (concrete/broadcast behaviour unchanged).
            if (not assignee or assignee == "*" or assignee == human
                    or views.is_role_audience(assignee)):
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
    all_tasks: list[dict[str, Any]], *, backend: Optional[list[str]] = None,
    summaries_view: Any = _UNSET,
) -> list[dict[str, Any]]:
    """Summarize loaded bodies while preserving summary-only inbox acks.

    ``inbox --ack`` can suppress a visible directive by writing only the summaries
    aggregate when the task body is temporarily unloadable. A later reconcile may
    successfully load that body, but the body still lacks the ``inbox_ack`` event.
    If reconcile rebuilt views from raw bodies alone, the ack would disappear and
    the directive would re-notify. Treat the current aggregate's ``acked_by`` as a
    durable prior fact, matching ``_load_summaries_for_rebuild`` on normal writes.

    ``summaries_view`` (E4 snapshot sharing): cmd_reconcile passes the raw
    aggregate it downloaded once this tick — the durable home of summary-only
    acks (an absent/None view degrades to no priors, which loses nothing: the
    only other ack source is the bodies being summarized right below). The
    ``_UNSET`` default keeps the staleness-guarded ``_load_task_summaries``
    load for direct callers."""
    prior_acks: dict[str, set[str]] = {}
    try:
        if summaries_view is _UNSET:
            # BYPASS the fallback stampede breaker: this helper is the
            # reconcile path, whose job is exactly to repair the views — it
            # must never be locked out because listener ticks hold the
            # per-host fallback claim (the 2026-06-11 self-sustaining
            # stampede; see io._load_task_summaries).
            source = _load_task_summaries(backend=backend,
                                          bypass_fallback_throttle=True)
        else:
            source = (summaries_view or {}).get("summaries") or []
        for summary in source:
            if not isinstance(summary, dict):
                continue
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


#: Wall-clock seconds of headroom the parity pass leaves before reconcile's
#: deadline — the _RETENTION_DEADLINE_HEADROOM_SECONDS discipline applied to the
#: parity sub-pass: probing one more task (a listing + K shard downloads) is
#: never worth blowing the tick's 90s ceiling; deferred tasks drain on the next
#: tick's rotated window.
_PARITY_DEADLINE_HEADROOM_SECONDS = 5.0

#: Headroom for cmd_reconcile's task-body-repair loop (2026-06-11 wave): the
#: _RETENTION_DEADLINE_HEADROOM_SECONDS discipline applied to the repair pass.
#: Each repair item is several remote round-trips (download + stat + upload +
#: re-stat) and, under the #167 transient retry, can legally take ~61s on the
#: default-timeout path — un-gated, a long backlog ran reconcile 40+ minutes
#: and overlapped the next cron tick. The loop checks the budget floor between
#: items and DEFERS the remainder (markers kept) to the next tick.
_REPAIR_DEADLINE_HEADROOM_SECONDS = 5.0

#: Cap (minutes) on the per-marker repair-failure backoff window
#: (2026-06-11 live find, repair-queue starvation). A marker whose repair
#: FAILED is stamped with ``repair_attempts``/``repair_last_attempt_at`` and
#: sits out min(2**attempts, this cap) minutes before being re-attempted.
#: WHY: ~12 deterministically-failing markers sat at the HEAD of the
#: list_op_markers() glob order, each re-failure burning 30-60s of remote ops
#: (download + stat probe + upload, with transient retries) — so every pass,
#: even a 900s one, spent its whole budget re-failing the same head and
#: deferred the ~60 healthy markers behind it. The queue could never drain
#: past the failing head. The cap keeps even a permanently-broken marker
#: retried about once per ~32 min (roughly alternate reconcile ticks), bounded
#: debt instead of a starved queue. Module constant so tests can patch it
#: (0 disables the window entirely).
_REPAIR_BACKOFF_CAP_MINUTES = 32.0


def _repair_backoff_minutes(attempts: Any) -> float:
    """Backoff window (minutes) earned by ``attempts`` failed repair tries.

    min(2**attempts, cap): exponential so a one-off transient costs ~2 min of
    extra latency while a chronic failure converges to the cap. Garbage /
    missing counts coerce to 0 — no window — failing toward retrying."""
    try:
        a = int(attempts)
    except (TypeError, ValueError):
        a = 0
    if a <= 0:
        return 0.0
    # min(a, 16) guards 2** against an absurd stored count before the cap.
    return min(float(2 ** min(a, 16)), _REPAIR_BACKOFF_CAP_MINUTES)


def _repair_in_backoff(marker: dict[str, Any], now: datetime) -> bool:
    """True when this op marker's last FAILED repair attempt is recent enough
    that the marker is still inside its backoff window (skip it this pass).

    Parse-don't-compare: the stamp goes through views._parse_dt, never a
    lexical comparison; an unparseable/missing stamp returns False — i.e. the
    marker is treated as never-attempted and retried NOW. Failing toward a
    retry is the safe direction: the worst case is one wasted re-probe,
    whereas failing toward skip could silently shelve a repairable debt."""
    window_minutes = _repair_backoff_minutes(marker.get("repair_attempts"))
    if window_minutes <= 0:
        return False
    last = views._parse_dt(marker.get("repair_last_attempt_at") or "")
    if last is None:
        return False
    return (now - last) < timedelta(minutes=window_minutes)


def _parity_sample_size() -> int:
    """Tasks probed per reconcile tick by the parity pass.

    WHY SAMPLE AT ALL (measured): each probed task costs one event-prefix
    listing plus one download per shard (~1.3s per subprocess). On the
    production bus (~440 tasks) probing everything every 20-minute tick was
    ~1,300+ spawns — the bulk of the measured 3,105-spawn reconcile. Drift is a
    slow-moving diagnostic (report-only health debt), so a rotating window that
    covers the full bus every ~ceil(N/sample) ticks (~9 at the default) loses
    nothing but latency on a signal nobody acts on within an hour anyway.
    ``FULCRA_COORD_PARITY_SAMPLE`` <= 0 disables sampling (probe everything)."""
    return env_int("FULCRA_COORD_PARITY_SAMPLE", 50)


def _parity_cursor_path():
    """The rotation cursor's local path — in the cache dir beside the other
    per-host bookkeeping (notified-state, op markers). Local-only: each host
    rotates independently; two hosts sampling different windows only *improves*
    fleet-wide coverage, so the cursor needs no bus coordination."""
    return cache.cache_root() / "parity-cursor"


def _read_parity_cursor() -> int:
    """Best-effort cursor read; 0 on any miss/corruption (a reset cursor only
    re-probes tasks sooner, never skips them forever)."""
    try:
        return int(_parity_cursor_path().read_text().strip())
    except Exception:
        return 0


def _write_parity_cursor(value: int) -> None:
    """Best-effort cursor persist (a bare int — no schema worth versioning) —
    a failed write must never fail the pass (the next tick just re-probes the
    same window)."""
    try:
        cache.cache_root().mkdir(parents=True, exist_ok=True)
        _parity_cursor_path().write_text(str(int(value)))
    except Exception:
        pass


def _event_parity_check(
    all_tasks: Optional[list[dict[str, Any]]] = None, *,
    backend: Optional[list[str]] = None,
    deadline: Optional[float] = None,
    summaries_view: Any = _UNSET,
) -> dict:
    """Compare each task snapshot against the fold of its event log.

    Safety net for the event dual-write: surfaces drift as health debt (the
    mutable file stays authoritative, so drift is REPORTED, never acted on).

    When ``events.fold_is_complete(folded)`` is True
    (at least one full-task snapshot event has been applied), the check
    compares ALL durable task fields — not just status — giving a precise
    whole-task parity signal.

    Root cause C2 broadening: for legacy delta-only tasks where the fold is NOT
    complete, the check no longer compares status alone.  It compares every field
    the fold ACTUALLY carries (``set(folded.keys()) - ignore``) against the file,
    but ONLY those fields — a field the fold never saw is skipped, so genuinely
    partial older-CLI delta payloads can't false-positive.  status is one of the
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
    makes the fold carry every ack is deliberately deferred (the ack sub-log
    union into summaries covers the read side meanwhile).

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

    PERF (2026-06-10 measured pass — this check was the single biggest cost in
    the 3,105-spawn reconcile tick):

    * ``all_tasks`` — cmd_reconcile passes the bodies it ALREADY loaded, so the
      check re-downloads nothing (~440 saved downloads/tick). The load fallback
      (one ``tasks/`` listing + pooled body downloads) remains for direct
      callers and tests; its tasks prefix is ``{remote_root()}/tasks/`` and the
      events prefix ``{remote_root()}/events/tasks/`` — separate trees, so the
      ``.json`` filter and ``/events/`` guard are belt-and-suspenders.
    * SAMPLING — a rotating window of ``_parity_sample_size()`` tasks per tick
      (cursor persisted locally), full-bus coverage every ~ceil(N/sample)
      ticks. ``sampled`` reports the window size; ``tasks_total`` stays the
      TRUE population so a reader can see the coverage ratio.
    * DEADLINE — when reconcile passes its deadline, the pass stops probing
      once the budget (minus headroom) is spent, mirroring ``_run_retention``;
      unprobed tasks are counted in ``deferred`` and drain on later ticks'
      rotated windows. ``deadline=None`` keeps the unbounded behaviour for
      direct callers.
    * POOLED — per-task probes run on a small thread pool (the
      ``_load_all_tasks`` shape): each probe is an independent listing + shard
      downloads with no shared mutable state.

    Remaining cost: O(sample) remote I/O per tick — one event listing plus one
    download per shard for each sampled task.
    """
    import time
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
    # E4 snapshot sharing: cmd_reconcile passes the view it already downloaded
    # this tick; the _UNSET default keeps the download for direct callers.
    summ_acks: dict[str, set[str]] = {}
    try:
        if summaries_view is _UNSET:
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

    # Resolve the task BODIES. cmd_reconcile hands over the set it already
    # loaded (zero extra I/O); the fallback for direct callers/tests is one
    # ``tasks/`` listing plus pooled body downloads — same verdicts, just paid
    # for locally instead of borrowed from the tick.
    if all_tasks is None:
        tasks_prefix = f"{remote.remote_root()}/tasks/"
        try:
            task_paths = [
                p for p in remote.list_files(tasks_prefix, backend=backend)
                # Only actual task JSON files directly under tasks/ — skip
                # anything that looks like an events shard or a non-JSON file.
                if p.endswith(".json") and "/events/" not in p
            ]
        except Exception:
            task_paths = []
        bodies: list[dict[str, Any]] = []
        if task_paths:
            workers = min(8, max(2, len(task_paths)))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                for snap in pool.map(
                        lambda p: remote.download_json(p, backend=backend),
                        task_paths):
                    if snap and "id" in snap:
                        bodies.append(snap)
    else:
        bodies = [t for t in all_tasks if isinstance(t, dict) and t.get("id")]

    # tasks_total reflects the full bus task population (the denominator the
    # flip gate's coverage check divides tasks_with_events by). A task with no
    # events still counts toward the total — it just isn't compared.
    tasks_total = len(bodies)

    # SAMPLING: a deterministic rotation over the id-sorted population. The
    # cursor persists locally so consecutive ticks tile the bus; sorting makes
    # the window stable against listing order so rotation actually advances.
    bodies.sort(key=lambda t: t["id"])
    sample = _parity_sample_size()
    if sample > 0 and len(bodies) > sample:
        cursor = _read_parity_cursor() % len(bodies)
        window = [bodies[(cursor + i) % len(bodies)] for i in range(sample)]
    else:
        cursor = None  # sampling inactive — no cursor to advance
        window = bodies

    budget_floor = (deadline - _PARITY_DEADLINE_HEADROOM_SECONDS
                    if deadline is not None else None)

    def _probe_one(snap: dict[str, Any]):
        """One task's parity probe. Returns None when the tick budget is spent
        (deferred — NOT checked), else (id, has_events, fold_complete,
        drifted, ack_missing)."""
        if budget_floor is not None and time.monotonic() >= budget_floor:
            return None
        evs = _eventlog.read_events(snap["id"], backend=backend)
        if not evs:
            # not yet dual-written (pre-migration task) — not drift
            return (snap["id"], False, False, False, False)
        folded = _events.fold_task(evs)
        fold_complete = _events.fold_is_complete(folded)
        if fold_complete:
            # Compare the durable task fields. Exclude bookkeeping the fold adds
            # (_applied_event_count) and fields that legitimately differ between a
            # point-in-time snapshot and the live file: updated_at / last_touched_*
            # move on every write, and the in-task events[] log grows independently.
            a = {k: v for k, v in folded.items() if k not in ignore}
            b = {k: v for k, v in snap.items() if k not in ignore}
            drifted = a != b
        else:
            # delta-only: the fold is reconstructed from partial deltas, so only
            # compare fields the fold ACTUALLY carries (skip anything it never saw —
            # that would false-positive on genuinely-partial pre-migration payloads).
            # Reuse the same ignore-set as the full-task branch. status is one of the
            # fold's keys, so the original status-only behaviour is still covered.
            keys = set(folded.keys()) - ignore
            drifted = any(folded.get(k) != snap.get(k) for k in keys)
        # C1: regardless of fold completeness, the fold must carry every durable
        # ack the summaries authority holds. If it's missing any, a post-flip read
        # would re-notify an already-acked directive — surface it (report-only).
        ack_missing = bool(
            summ_acks.get(snap["id"], set()) - set(folded.get("acked_by") or []))
        return (snap["id"], True, fold_complete, drifted, ack_missing)

    # POOLED probes (the _load_all_tasks pool shape): each probe is one listing
    # + K shard downloads with no shared mutable state. pool.map preserves
    # window order, so the deferred count below maps cleanly onto the window.
    deferred = 0
    results = []
    if window:
        workers = min(8, len(window))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(_probe_one, window))
    for res in results:
        if res is None:
            deferred += 1
            continue
        tid, has_events, fold_complete, drifted, ack_missing = res
        if not has_events:
            continue
        checked += 1
        if fold_complete:
            folds_complete += 1
        if drifted:
            drift_set.add(tid)
        if ack_missing:
            ack_drift_ids_set.add(tid)
            drift_set.add(tid)

    # Advance the rotation by what was actually PROBED this tick, so a
    # deadline-shortened window resumes where it stopped instead of skipping
    # the deferred tail until the next full revolution.
    if cursor is not None:
        _write_parity_cursor((cursor + (len(window) - deferred)) % len(bodies))

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
        # PERF telemetry: window actually probed this tick + tasks the deadline
        # gate pushed to a later tick. sampled < tasks_total means the coverage
        # ratio above is per-revolution, not per-tick.
        "sampled": len(window),
        "deferred": deferred,
    }


def _directive_parity_check(
    *, backend: Optional[list[str]] = None,
    records: Optional[list[dict[str, Any]]] = None,
    all_tasks: Optional[list[dict[str, Any]]] = None,
    deadline: Optional[float] = None,
) -> dict:
    """Compare each first-class directive record against its back-ref task.

    Safety net for the directive dual-write — every directive-creating command
    writes a ``directives/<id>.json`` LWW snapshot mirroring its task (id
    ``DIR-T-<task_id>``). The TASK record stays authoritative for task state,
    but the mirrored loop records carry coordination state (board / digest /
    review-done / health read them), so silent divergence matters. This
    sub-pass folds the STORED directive record against the EXPECTED mirror of its
    current back-ref task and surfaces divergence as health debt — REPORT-ONLY,
    exactly like ``_event_parity_check``: drift is recorded, never acted on, and
    a failure here can never change reconcile's exit code or mutate anything.

    THE TOP-LEVEL-ONLY FILTER (load-bearing). ``remote.directives_prefix()``
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

    E4 snapshot sharing: cmd_reconcile passes ``records`` (the top-level loop
    records its single ``load_loop_records`` sweep already downloaded — the
    SAME top-level-only filtered set this check used to re-list and re-download
    itself) and ``all_tasks`` (the bodies it already holds, replacing one
    ``_load_task`` per record). Both default to None, which keeps the
    self-loading behaviour for direct callers; with ``all_tasks`` supplied, a
    back-ref absent from the tick's working set counts as ``orphan`` — the
    same verdict the dangling-ref path gives.

    Cost when self-loading: O(D) remote I/O — one ``download_json`` per
    directive record plus one task load (and a sub-log ``list_json``) per
    comparable record. With the tick's snapshots threaded in: one ack sub-log
    listing per comparable record, nothing else.
    """
    drift_set: set[str] = set()
    checked = 0
    orphans = 0
    deferred = 0
    if deadline is not None:
        import time

    # Volatile fields (created_at / updated_at) are ignored BY CONSTRUCTION here:
    # rather than diff whole records and subtract an ignore-set (the
    # _event_parity_check approach), we compare only an explicit ALLOW-LIST of the
    # mapped fields below (directive_type / audience / status / acked_by-as-set).
    # A write timestamp is never in that list, so it can never register as drift.

    if records is None:
        # Self-loading fallback (direct callers/tests): list the prefix and
        # download each top-level record.
        prefix = remote.directives_prefix()
        try:
            paths = remote.list_files(prefix, backend=backend)
        except Exception:
            paths = []
        stored_records: list[dict[str, Any]] = []
        for path in paths:
            # TOP-LEVEL-ONLY FILTER (load-bearing): strip the prefix and accept
            # only a direct child ``<id>.json`` — no further '/' (which would
            # make it a sub-log shard under ``<id>/acks/`` or ``<id>/routing/``).
            rel = path[len(prefix):] if path.startswith(prefix) else path
            if "/" in rel:
                continue  # sub-log shard (acks/ or routing/) — never a record
            if not rel.endswith(".json"):
                continue
            stored = remote.download_json(path, backend=backend)
            if isinstance(stored, dict):
                stored_records.append(stored)
    else:
        # Tick-shared records: load_loop_records already applied the same
        # top-level-only filter, so these ARE the top-level directive records.
        stored_records = [r for r in records if isinstance(r, dict)]

    # Tick-shared task bodies: keyed once; None keeps the per-record _load_task.
    task_map: Optional[dict[str, dict[str, Any]]] = None
    if all_tasks is not None:
        task_map = {
            tid: t for t in all_tasks
            if isinstance(t, dict) and (tid := t.get("id"))
        }

    for stored in stored_records:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining < 2.0:
                deferred += 1
                continue
        else:
            remaining = None
        task_id = stored.get("task_id")
        if not task_id:
            continue  # orphan / directly-authored directive — can't compare (gate)

        if task_map is not None:
            task = task_map.get(task_id)
        else:
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
                    ack_timeout = None
                    if remaining is not None:
                        ack_timeout = max(1.0, min(5.0, remaining - 1.0))
                    sublog_acks = _directives.read_directive_acks(
                        directive_id, backend=backend, timeout=ack_timeout)
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
        "deferred": deferred,
    }


def _loop_health_check(
    *, backend: Optional[list[str]] = None,
    records: Optional[list[dict[str, Any]]] = None,
) -> dict:
    """Open/overdue coordination-loop counts for the health record (spec
    2026-06-09 Task 5).

    REPORT-ONLY and best-effort, mirroring ``_event_parity_check`` /
    ``_directive_parity_check``: it never mutates anything, and the caller wraps
    it in the same try/except so a failure here can NEVER change reconcile's
    exit code — the result, present or absent, is health debt only.

    Records load + evidence probes live in ``loop_ops.load_loop_records`` /
    ``loop_ops.evidence_ids_for`` (the single home for the load-bearing
    top-level-shard filter and the bounded-probe discipline — shared with the
    board and the digest, which may not import this module).

    The fold itself is the pure ``loops.loop_board`` over THIS host's resolved
    agent: ``awaiting_me`` = open loops directed at me, ``awaiting_others`` =
    open loops I opened that nobody answered (with per-kind-SLA overdue flags).
    Returned shape: ``{open_loops, overdue, awaiting_me, out_of_band}`` where
    ``open_loops`` is |awaiting_me| + |awaiting_others| and ``overdue`` counts
    the overdue subset of ``awaiting_others`` — the loops-never-ANSWERED
    counterpart to the undelivered (never-ARRIVED) check beside it in the
    health record. ``out_of_band`` counts the awaiting_others
    subset whose EVIDENCE sub-log is nonempty: a forge-mirrored answer exists
    off the bus, so the requester should close the loop explicitly, citing it
    (mirrored evidence never closes anything — fold_loop's invariant).

    ``records`` (E4 snapshot sharing): cmd_reconcile passes the loop records
    its single per-tick sweep already loaded; None (direct callers, or a tick
    whose shared sweep failed) keeps the self-loading behaviour.
    """
    if records is None:
        try:
            records = load_loop_records(backend=backend)
        except Exception:
            records = []   # report-only: an unreadable bus reads as "no loops"
    me = identity.resolve_agent(None)
    now = datetime.now(timezone.utc)
    evidence_ids = evidence_ids_for(me, records, now=now, backend=backend)
    board = _loops.loop_board(me, records, now=now, evidence_ids=evidence_ids)
    awaiting_me = board["awaiting_me"]
    awaiting_others = board["awaiting_others"]
    return {
        "open_loops": len(awaiting_me) + len(awaiting_others),
        "overdue": sum(1 for x in awaiting_others if x.get("overdue")),
        "awaiting_me": len(awaiting_me),
        "out_of_band": sum(1 for x in awaiting_others if x.get("out_of_band")),
    }


def _maybe_escalate_role_vacancy(
    role: dict, status: dict, now: datetime, *,
    backend: Optional[list[str]] = None,
) -> bool:
    """Best-effort escalation of one SLA-breaching vacancy to the role's
    maintainer. Returns True iff THIS call emitted the directive.

    IDEMPOTENCE: first-writer-wins DAILY marker
    (``roles/<name>/escalations/<YYYY-MM-DD>.json``) — the _claim_digest_marker
    protocol verbatim: an existing marker means today's escalation already
    went out (this tick or another host's), so NO-OP; a marker claim failure
    also no-ops so a flaky bus never risks a double. One directive per
    vacancy-DAY, not per 20-minute reconcile tick — a vacancy that persists
    re-pings the maintainer daily instead of flooding their inbox.

    ROUTING: the directive goes to the role's ``maintainer`` (which may itself
    be an @role), NOT the operator's plate unless the maintainer chain ends
    there — the generalization of "agent is dark" into "function is unstaffed,
    whoever owns staffing it should know". No maintainer ⇒ nowhere to route ⇒
    warn-only (the vacancy still counts/renders on board + health)."""
    name = role.get("name") or ""
    maintainer = role.get("maintainer")
    if not maintainer:
        _warn(f"  role '{name}' vacant past SLA but has NO maintainer — "
              "set one via `roles set --maintainer` to enable escalation")
        return False
    try:
        day = now.astimezone(timezone.utc).strftime("%Y-%m-%d")
        marker_path = remote.role_escalation_marker_path(name, day)
        if remote.download_json(marker_path, backend=backend) is not None:
            return False   # already escalated today (any host)
        marker = {
            "schema": "fulcra.coordination.role_escalation_marker.v1",
            "role": name,
            "date": day,
            "by": identity.resolve_agent(),
            "claimed_at": _iso_z(now),
        }
        if not remote.upload_json(marker, marker_path, backend=backend):
            return False   # uncertain claim: skip, never risk a double
        vacant_for = _age_str(status.get("vacant_since") or "")
        # 2026-06-11 bug hunt C3 (P1): this used to upload ONLY a first-class
        # directives/<id>.json record — which NOTHING delivery-side reads
        # (inbox / listener / SessionStart all fold over the authoritative
        # TASK set), so the maintainer was never actually told. Route
        # through the task path like every other directive creator
        # (a proposed task assigned to the maintainer via the write pipeline)
        # plus the standard directive dual-write mirror — the exact
        # writepipe+dual_write combination request-review uses (lifecycle's
        # cmd_tell can't be reused here without an args shim; the pipeline
        # call IS its machinery).
        me = identity.resolve_agent()
        task = schema.make_task(
            title=f"Role '{name}' VACANT past SLA",
            workstream="coordination",
            agent=me,
            owner_agent=me,
            assignee=maintainer,
            priority="P1",
            summary=(f"Role '{name}' has been vacant for {vacant_for} "
                     f"(SLA {role.get('sla_hours')}h). "
                     f"{role.get('description') or ''}").strip(),
            next_action=(f"Restaff it: have an agent run "
                         f"`fulcra-coord roles claim {name}` (or connect "
                         f"--role {name}), or adjust the registry record."),
        )
        cache.write_cached_task(task)
        try:
            ok = _write_task_and_views(task, backend=backend, command="tell")
        except (schema.ConflictError, schema.NeedsReconcile):
            # The task BODY landed (these raise only after the body uploaded)
            # — the escalation is delivered; views self-heal on reconcile.
            ok = True
        if ok:
            # Best-effort mirror into directives/<id>.json (never fails or
            # alters the authoritative task write — see directives.dual_write).
            _directives.dual_write(task, command="tell", backend=backend)
            _warn(f"  ⚠ role '{name}' vacant past SLA — escalation directive "
                  f"sent to {maintainer}")
        return bool(ok)
    except Exception:
        return False   # best-effort: escalation must never break the sweep


def _role_health_check(
    *, backend: Optional[list[str]] = None,
    presence_agents: Optional[list[dict[str, Any]]] = None,
) -> dict:
    """Role registry status for the health record — vacancy is the new
    dark-agent signal (spec 2026-06-10): not "agent X is dark" but "FUNCTION X
    is unstaffed".

    REPORT-ONLY and best-effort, mirroring ``_loop_health_check``: the caller
    wraps it so a failure can NEVER change reconcile's exit code. The ONE
    write it triggers — the SLA-vacancy escalation directive — is itself
    best-effort and idempotent (see _maybe_escalate_role_vacancy), so the
    sub-pass stays safe to re-run every tick.

    STALENESS-GUARDED presence (post-#147, load-bearing): lease freshness IS
    presence freshness, so this check reads the roster through
    ``_load_presence_agents`` — the guarded loader that falls back to listing
    the durable per-agent records when the aggregate lags. Reading the raw
    aggregate here would re-inherit the stale-view blindness: a live holder
    would read dead and the role would falsely escalate as VACANT.
    ``presence_agents`` (E4 snapshot sharing): cmd_reconcile passes the roster
    it just REBUILT from those same durable per-agent records — fresher than
    any aggregate, so the guard's purpose is preserved without a re-load; None
    (direct callers) keeps the guarded self-load; the
    ``presence.PRESENCE_READ_ERROR`` sentinel means NO trustworthy roster of
    any age exists this tick (F5) — every role's vacancy judgment is then
    UNKNOWN, because lease freshness IS presence freshness.

    UNKNOWN, not vacant (2026-06-11 read-error audit, F4/F5): a lease sub-log
    that could not be read (role_ops.READ_ERROR), or an unknowable roster,
    used to fold as VACANT-since-created_at — instantly past any SLA, so one
    transport blip put a false "Role VACANT past SLA" P1 directive on the
    maintainer's plate (durable: a human has to read and dismiss it). Those
    roles now report ``unknown`` and the escalation is SKIPPED with a logged
    reason; the SLA clock only ever runs on a CONFIRMED-empty lease read.

    The fold itself is the pure ``roles.role_status`` with the SAME liveness
    thresholds routing uses (stale_hours + wall-clock grace), injected by
    parameter — one definition of "fresh" across the whole tool. Returned
    shape: ``{roles: [{name, policy, holders, vacant, vacant_since,
    contested, escalation_due, unknown}], vacant, contested, escalated,
    unknown}``."""
    # ONE partitioned roles/ listing serves the registry AND every role's
    # lease sub-log (perf loop-2 #1: list_roles + per-role read_leases used to
    # re-list and re-download the same shards every tick). READ_ERROR per role
    # is preserved by the fold — translated below exactly as before.
    registry = _role_ops.load_roles_with_leases(backend=backend)
    if not registry:
        return {"roles": [], "vacant": 0, "contested": 0, "escalated": 0,
                "unknown": 0}
    now = datetime.now(timezone.utc)
    presence_unknown = presence_agents is _PRESENCE_READ_ERROR
    if presence_unknown:
        presence_by_agent = None   # role_status's explicit unknown input
    else:
        if presence_agents is None:
            presence_agents = _load_presence_agents(backend=backend)
        presence_by_agent = {
            a.get("agent"): a
            for a in presence_agents
            if isinstance(a, dict) and a.get("agent")
        }
    stale_hours = views._stale_hours()
    grace = views._presence_grace_seconds()
    out: list[dict] = []
    vacant = contested = escalated = unknown = 0
    for role, leases in registry:
        name = role.get("name") or ""
        if leases is _role_ops.READ_ERROR:
            leases = None   # translate the sentinel for the pure fold
        status = _roles.role_status(role, leases, presence_by_agent, now,
                                    stale_hours=stale_hours,
                                    grace_seconds=grace)
        due = _roles.vacancy_escalation_due(role, status, now)
        if status.get("unknown"):
            unknown += 1
            _warn(f"  role '{name}': lease/presence state could not be read — "
                  "vacancy judgment (and any SLA escalation) skipped this tick")
        if status["vacant"]:
            vacant += 1
        if status["contested"]:
            contested += 1
        if due and _maybe_escalate_role_vacancy(role, status, now,
                                                backend=backend):
            escalated += 1
        out.append({
            "name": name,
            "policy": role.get("policy"),
            "holders": status["holders"],
            "vacant": status["vacant"],
            "vacant_since": status["vacant_since"],
            "contested": status["contested"],
            "escalation_due": due,
            "unknown": status.get("unknown", False),
        })
    return {"roles": out, "vacant": vacant, "contested": contested,
            "escalated": escalated, "unknown": unknown}


def cmd_roles(args: Any, backend: Optional[list[str]] = None) -> int:
    """The role registry surface: list (default), ``set``, ``claim``, ``release``.

    * (bare)  — registry + live status per role: HELD by whom / VACANT how
      long (⚠ past SLA) / CONTESTED. This is the DISCOVERY surface the spec
      calls for: senders learn what roles exist instead of guessing session
      ids — the machine-readable answer to broadcast-and-hope routing.
    * ``set <name> [--description --instructions --policy --sla-hours
      --maintainer]`` — operator CRUD (upsert). An UPDATE preserves every
      field not explicitly passed (and the original ``created_at``), so
      tightening one knob never wipes the runbook.
    * ``claim <name>`` / ``release <name>`` — the manual lease path (connect
      ``--role`` is the automatic one). After a claim the status is re-read
      through the staleness-guarded roster so an exclusive double-hold warns
      CONTESTED right at the claimer, not just on the next board glance.

    Status folding reuses the exact pure fold + injected thresholds the
    health check and board use — three surfaces, one judgment."""
    out_format = getattr(args, "format", "table")
    action = getattr(args, "roles_action", None)

    if action == "set":
        name = (getattr(args, "name", "") or "").strip()
        existing = _role_ops.read_role(name, backend=backend)
        if existing is _role_ops.READ_ERROR:
            # 2026-06-11 bug hunt C1: a failed registry read must not be
            # treated as "new role" — the _pick/preserve update below would
            # rebuild from defaults and wipe every field not passed this run.
            _err(f"roles set: registry record for '{name}' could not be read "
                 "— re-run (updating blind would wipe unspecified fields)")
            return 1
        existing = existing or {}

        def _pick(arg_name: str, field: str, default):
            val = getattr(args, arg_name, None)
            return val if val is not None else existing.get(field, default)

        try:
            record = schema.make_role(
                name,
                _pick("description", "description", ""),
                standing_instructions=_pick("instructions",
                                            "standing_instructions", ""),
                policy=_pick("policy", "policy", "shared"),
                sla_hours=_pick("sla_hours", "sla_hours", None),
                maintainer=_pick("maintainer", "maintainer", None),
                checkpoint_ref=existing.get("checkpoint_ref"),
            )
        except ValueError as e:
            _err(f"roles set: {e}")
            return 1
        if existing.get("created_at"):
            record["created_at"] = existing["created_at"]
        if not _role_ops.upsert_role(record, backend=backend):
            _err(f"roles set: registry write for '{name}' could not be "
                 "verified — re-run (the record may not have landed)")
            return 1
        if out_format == "json":
            _print_json(record)
        else:
            _info(f"Role '{record['name']}' registered "
                  f"(policy={record['policy']}, sla="
                  f"{record['sla_hours'] or '—'}, "
                  f"maintainer={record['maintainer'] or '—'})")
        return 0

    if action in ("claim", "release"):
        name = (getattr(args, "name", "") or "").strip()
        me = identity.resolve_agent(getattr(args, "agent", None))
        if action == "claim":
            if not _role_ops.claim_role(name, me, backend=backend):
                _err(f"roles claim: lease write failed for '{name}'")
                return 1
            # 2026-06-11 bug hunt C5: the lease alone is HALF the truth —
            # @role inbox delivery reads presence capabilities
            # (inbox._my_roles), so a claim that only wrote the lease left
            # the board saying HELD while directives @<role> never arrived.
            # Merge the role into the claimer's capabilities via the C4
            # merge-safe helper. This lives HERE (not in role_ops.claim_role)
            # because role_ops must never import presence — the layering pin
            # in tests/test_roles.py; connect's lease path syncs capabilities
            # already by construction (it writes the presence record).
            if not _presence_add_capabilities(me, [name], backend=backend):
                _warn(f"roles claim: lease landed but the presence capability "
                      f"merge failed — @{name} directives may not deliver "
                      "until the next connect")
            # Post-claim contested check, through the guarded roster (the
            # claim itself stays presence-blind by layering — see role_ops).
            try:
                role = _role_ops.read_role(name, backend=backend)
                if not isinstance(role, dict):  # absent or READ_ERROR (C1)
                    role = {}
                leases = _role_ops.read_leases(name, backend=backend)
                if leases is _role_ops.READ_ERROR:
                    leases = None   # F4: unknown — fold reports no contest
                status = _roles.role_status(
                    role, leases,
                    {a.get("agent"): a
                     for a in _load_presence_agents(backend=backend)
                     if isinstance(a, dict) and a.get("agent")},
                    datetime.now(timezone.utc),
                    stale_hours=views._stale_hours(),
                    grace_seconds=views._presence_grace_seconds())
                if status["contested"]:
                    others = [h["agent"] for h in status["holders"]
                              if h["agent"] != me]
                    _warn(f"⚠ role '{name}' is now CONTESTED — exclusive with "
                          f"fresh lease(s) from {others}; coordinate a release")
            except Exception:
                pass
            _info(f"Claimed role '{name}' for {me}")
            # Role claim → resume (continuity spec 2026-06-10): when the
            # claimed role's registry record carries a checkpoint_ref, print
            # the where-it-left-off (ref + best-effort rendered brief) right
            # at the claimer — the role's resume state surviving session
            # death is the whole point of the field. Helper never raises.
            _continuity_ops.print_role_resume(name, backend=backend)
            return 0
        if not _role_ops.release_role(name, me, backend=backend):
            _err(f"roles release: no lease of yours to release on '{name}'")
            return 1
        # C5 counterpart: stop @role delivery too (capabilities are the
        # delivery truth). Kept deliberately simple — release always removes
        # the capability; siblings survive (merge-safe RMW, never a rebuild).
        if not _presence_remove_capability(me, name, backend=backend):
            _warn(f"roles release: lease removed but the presence capability "
                  f"removal failed — @{name} directives may keep delivering "
                  "until the next release/connect --clear-roles")
        _info(f"Released role '{name}' for {me}")
        return 0

    # Default: list the registry with live status folded in. One partitioned
    # roles/ listing carries the lease sub-logs too (perf loop-2 #1 — no
    # per-role re-list/re-download).
    registry = _role_ops.load_roles_with_leases(backend=backend)
    now = datetime.now(timezone.utc)
    presence_by_agent = {
        a.get("agent"): a
        for a in _load_presence_agents(backend=backend)
        if isinstance(a, dict) and a.get("agent")
    }
    stale_hours = views._stale_hours()
    grace = views._presence_grace_seconds()
    merged: list[dict] = []
    for role, leases in registry:
        name = role.get("name") or ""
        if leases is _role_ops.READ_ERROR:
            # F4: an unreadable lease sub-log must render as UNKNOWN below,
            # never as VACANT (the false-vacancy class this audit closed).
            leases = None
        status = _roles.role_status(
            role, leases,
            presence_by_agent, now, stale_hours=stale_hours,
            grace_seconds=grace)
        row = dict(role)
        row.update(status)
        row["escalation_due"] = _roles.vacancy_escalation_due(
            role, status, now)
        merged.append(row)

    if out_format == "json":
        _print_json({"roles": merged})
        return 0

    if not merged:
        _info("No roles registered yet. Create one: "
              "fulcra-coord roles set <name> --description '...'")
        return 0

    print(f"\n{'='*60}")
    print("  Fulcra Coordination — Roles")
    print(f"{'='*60}")
    for r in merged:
        if r.get("unknown"):
            # F4: say what we know (nothing) rather than guessing a vacancy.
            state = "leases UNREADABLE — held/vacant unknown this read"
        elif r["contested"]:
            holders = ", ".join(h["agent"] for h in r["holders"])
            state = f"CONTESTED — exclusive, fresh leases: {holders}"
        elif r["vacant"]:
            state = f"VACANT {_age_str(r.get('vacant_since') or '')}"
            if r["escalation_due"]:
                state += " ⚠"
        else:
            state = "HELD by " + ", ".join(h["agent"] for h in r["holders"])
        print(f"\n  {r['name']}  [{r.get('policy')}]  {state}")
        if r.get("description"):
            print(f"    {r['description'][:80]}")
        meta = []
        if r.get("sla_hours") is not None:
            meta.append(f"sla: {r['sla_hours']}h")
        if r.get("maintainer"):
            meta.append(f"maintainer: {r['maintainer']}")
        if meta:
            print(f"    {' · '.join(meta)}")
        if r.get("standing_instructions"):
            print(f"    instructions: {r['standing_instructions'][:80]}")
    print()
    return 0


def _retry_sleep(seconds: float) -> None:
    """Jitter sleep before a view-upload retry. A module-level wrapper (not an
    inline ``time.sleep``) ONLY so tests can patch it out — patching the global
    ``time.sleep`` would also stall the other pool workers' real waits."""
    import time
    time.sleep(seconds)


def cmd_reconcile(args: Any, backend: Optional[list[str]] = None) -> int:
    """Repair views and resolve pending operation markers."""
    import random
    import time
    _info("Reconciling coordination views...")
    t0 = time.monotonic()
    timeout = env_int("FULCRA_COORD_RECONCILE_TIMEOUT_SECONDS", 90)
    deadline = t0 + timeout
    skipped_checks: list[str] = []

    class _SkipBestEffortCheck(Exception):
        pass

    def _deadline_spent(label: str, *, headroom: float = 1.0) -> bool:
        """Best-effort sub-passes must not consume the heartbeat's last breath."""
        if time.monotonic() + headroom < deadline:
            return False
        skipped_checks.append(label)
        try:
            _warn(f"  {label} skipped (reconcile deadline budget spent)")
        except Exception:
            pass
        return True

    markers = cache.list_op_markers()
    needs_repair = [m for m in markers if m.get("needs_reconcile")]
    if needs_repair:
        _info(f"  {len(needs_repair)} operation(s) need view repair.")

    body_repair_failures = []
    # 2026-06-11 wave (live find): this loop was NOT deadline-gated between
    # items. Each repair is several remote round-trips, and with the #167
    # transient retry a single op can legally take ~61s on the default-timeout
    # path — a 42-item backlog ran 40+ minutes and overlapped the NEXT cron
    # tick's reconcile. Gate on the same budget floor _run_retention uses:
    # stop + defer the remainder (markers KEPT, so the next tick's fresh
    # budget drains them), log the deferred count. Deferral is debt, not
    # failure — it must not trip the body_repair_failures early-return.
    repair_budget_floor = deadline - _REPAIR_DEADLINE_HEADROOM_SECONDS
    repair_deferred_ops: set[str] = set()

    # 2026-06-11 live find (repair-queue starvation): list_op_markers() glob
    # order put a dozen deterministically-failing markers at the HEAD of this
    # loop, and each re-failure cost 30-60s of remote ops — every pass burned
    # its whole budget re-failing the same head and the healthy majority
    # behind it never got a turn. Two defenses, both keyed off the failure
    # stamps _note_repair_failure writes onto the marker:
    #   * ORDERING — never-attempted markers run FIRST (first claim on
    #     budget); previously-failed-but-eligible ones run after, on whatever
    #     budget is left.
    #   * BACKOFF — a marker still inside its min(2**attempts, cap)-minute
    #     window since its last failure is SKIPPED outright this pass (its
    #     marker is KEPT — skip is debt, not success). Without the skip, a
    #     pass with spare budget would still re-burn 30-60s per chronic
    #     failure every ~20-minute tick, forever.
    repair_now = datetime.now(timezone.utc)
    repair_backoff_ops: set[str] = set()
    fresh_repairs: list[dict] = []
    retry_repairs: list[dict] = []
    for m in needs_repair:
        if _repair_in_backoff(m, repair_now):
            repair_backoff_ops.add(m.get("op_id"))
        elif m.get("repair_attempts"):
            retry_repairs.append(m)
        else:
            fresh_repairs.append(m)
    if repair_backoff_ops:
        _info(f"  {len(repair_backoff_ops)} repair marker(s) in failure "
              "backoff — skipped this pass (retried after their window).")

    body_repair_reasons: dict[str, str] = {}

    def _note_repair_failure(marker: dict, task_id: str, reason: str) -> None:
        """Record one failed repair: the per-task reason (ops-log diagnosis,
        truncated — reasons are for operators, not payloads) plus the attempt
        stamps the next pass's ordering/backoff partition reads. The marker
        update is best-effort: a failed stamp write only costs the backoff,
        never the failure accounting."""
        body_repair_failures.append(task_id)
        body_repair_reasons[task_id] = reason[:120]
        try:
            attempts = int(marker.get("repair_attempts") or 0)
        except (TypeError, ValueError):
            attempts = 0
        marker["repair_attempts"] = attempts + 1
        marker["repair_last_attempt_at"] = _now_iso()
        try:
            cache.write_op_marker(marker["op_id"], marker)
        except Exception:
            pass

    for m in fresh_repairs + retry_repairs:
        if m.get("status") not in ("failed", "unverified"):
            continue
        tid = m.get("task_id")
        if not tid:
            continue
        if time.monotonic() >= repair_budget_floor:
            # Budget nearly spent: defer this and every remaining repair to
            # the next tick rather than overrun reconcile's ceiling.
            repair_deferred_ops.add(m.get("op_id"))
            continue
        cached_task = cache.read_cached_task(tid)
        task_path = remote.task_remote_path(tid)
        if not cached_task:
            # 2026-06-11 live find (15 zombie markers, ops.log "no cached body
            # to replay"): a marker with no cached body used to FAIL here every
            # pass — and a failed repair fails the tick, which preserves every
            # marker — so these survived every reconcile until an operator
            # deleted them by hand. There is nothing to replay, so the marker's
            # fate is decided by what the REMOTE says, not by failing blind:
            #   * remote body READABLE -> the write evidently landed by another
            #     path (another host's replay, a later successful write of the
            #     same task) — the marker is obsolete, clear it.
            #   * remote CONFIRMED absent/tombstoned (the io._confirmed_absent
            #     idiom from #170/#177) -> nothing can EVER replay this marker:
            #     pure debt, no asset. Clear it.
            #   * remote state UNKNOWN (transport failure) -> KEEP the marker —
            #     fail toward retrying, never toward forgetting a write whose
            #     fate is unproven (next tick's weather may differ).
            # (The mint-side fix lives in writepipe: the body is now cached
            # BEFORE the upload attempt, so new markers are always replayable —
            # this branch drains the pre-fix zombies and any cache eviction.)
            try:
                landed = remote.download_json(task_path, backend=backend)
            except Exception:
                landed = None
            if landed is not None:
                reason = ("no cached body but remote body readable — write "
                          "landed by another path; marker cleared")
                _info(f"  Task {tid}: {reason}")
                ops_log.log_op("reconcile", tid,
                               status="task_body_repair_unreplayable",
                               detail=reason)
                # Seed the cache from the bus (same idiom as the stale-replay
                # skip below) so later reads/merges start from remote truth.
                cache.write_cached_task(landed)
                try:
                    landed_stat = remote.stat(task_path, backend=backend)
                    if landed_stat:
                        cache.write_meta(task_path, landed_stat)
                except Exception:
                    pass
                # Not a failure: fall through to the end-of-tick marker sweep.
                continue
            if _confirmed_absent(task_path, backend=backend):
                reason = ("no cached body and remote absent — unreplayable "
                          "marker cleared")
                _info(f"  Task {tid}: {reason}")
                ops_log.log_op("reconcile", tid,
                               status="task_body_repair_unreplayable",
                               detail=reason)
                # Not a failure: fall through to the end-of-tick marker sweep.
                continue
            _note_repair_failure(
                m, tid, "no cached body to replay (remote state unknown — "
                "kept for retry)")
            continue
        # 2026-06-11 bug hunt C2 (P1): the replay used to upload the cached
        # body BLINDLY. An "unverified" marker only means OUR write may not
        # have landed — it says nothing about what landed SINCE: another
        # host's newer body was silently reverted by the stale replay. So
        # look first: if a remote body exists, route through the same
        # _try_merge the write pipeline uses; the blind upload survives only
        # for the remote-absent case (the genuine lost-write this repair
        # exists for).
        body_to_upload = cached_task
        try:
            current_remote = remote.download_json(task_path, backend=backend)
        except Exception:
            current_remote = None
        if not current_remote:
            probe_failed = False
            try:
                remote_exists = remote.stat(task_path, backend=backend) is not None
            except Exception:
                remote_exists = True
                probe_failed = True
            if remote_exists:
                # TOMBSTONE (2026-06-11, the forever-blocked-markers bug): the
                # platform DELETE is a SOFT delete, so for a task that was
                # deliberately deleted (archived/pruned/moved) stat keeps
                # reporting the version history while downloads 404 — the old
                # guard read that as "exists but unreadable" and this marker
                # re-failed every pass, forever. _confirmed_absent now
                # recognizes the signature (stat visible + download fails
                # not-found-class + bus reachable => confirmed absent).
                #
                # RESURRECTION HAZARD (the F7-adjacent class) — why a
                # confirmed tombstone must NOT fall through to the
                # upload-cached-body branch below: a tombstoned path means
                # someone deliberately deleted this task. Re-uploading the
                # cached body would resurrect it. So consult the archive
                # cold-index instead: ARCHIVED => the marker is simply
                # obsolete (the truth lives in the archive — the very move
                # that tombstoned the hot path); NOT archived => operator
                # intent is ambiguous (manual prune? out-of-band cleanup?),
                # and between silently resurrecting a deleted task and
                # clearing a marker whose body remains recoverable (platform
                # version history via `fulcra file restore`, plus the ops-log
                # trail here), clearing is the safe side. Either way the
                # local cached copy is evicted, same rationale as
                # _archive_task: the cache-seeded loader would otherwise
                # rebuild the dead id straight back into the views.
                if not probe_failed and _confirmed_absent(task_path,
                                                          backend=backend):
                    try:
                        shard = _read_index_shard(tid, backend=backend)
                    except Exception:
                        shard = None
                    reason = ("tombstone: archived, marker cleared" if shard
                              else "tombstone: not in archive, marker cleared "
                                   "without re-upload")
                    _info(f"  Task {tid}: remote is a soft-delete tombstone — "
                          f"{reason} (cached body NOT re-uploaded; see "
                          "archive/restore).")
                    ops_log.log_op("reconcile", tid,
                                   status="task_body_repair_tombstone",
                                   detail=reason)
                    cache.delete_cached_task(tid)
                    # Not a failure: fall through to the end-of-tick marker
                    # sweep, which clears this op's marker with the repaired ones.
                    continue
                # A failed/unreadable download is not proof of absence. Blind
                # replay is safe only when the remote body is confirmed absent;
                # otherwise this reintroduces the stale-body clobber C2 fixed.
                _warn(f"  Task {tid}: remote body exists but could not be "
                      "downloaded for merge — keeping the repair marker.")
                _note_repair_failure(
                    m, tid,
                    "absence unconfirmable (stat probe failed)"
                    if probe_failed else
                    "remote stat exists but fresh download unreadable "
                    "(cannot merge, cannot confirm absence)")
                continue
        if current_remote:
            merged = _try_merge(cached_task, current_remote)
            if merged is not None:
                body_to_upload = merged
            elif _updated_at_key(current_remote) >= _updated_at_key(cached_task):
                # Unsafe merge but the bus already carries the as-new-or-newer
                # body: our cached replay is the stale side — skip the upload
                # entirely and let the marker resolve as repaired (re-meta the
                # remote body so the next write's stat check starts clean).
                try:
                    post_stat = remote.stat(task_path, backend=backend)
                    if post_stat:
                        cache.write_meta(task_path, post_stat)
                except Exception:
                    pass
                cache.write_cached_task(current_remote)
                continue
            else:
                # Unsafe merge and OUR side is newer: neither replaying (would
                # clobber the remote transition) nor skipping (would silently
                # drop newer local work) is safe — keep the debt visible.
                _warn(f"  Task {tid}: cached replay conflicts with a changed "
                      "remote body and cannot merge safely — keeping the "
                      "repair marker for manual resolution.")
                _note_repair_failure(
                    m, tid, "unsafe merge: cached and remote bodies diverged, "
                    "cached side newer (manual resolution)")
                continue
        upload_err: Optional[str] = None
        try:
            ok = remote.upload_json(body_to_upload, task_path, backend=backend)
        except Exception as ue:
            ok = False
            upload_err = f"{type(ue).__name__}: {ue}"
        if not ok:
            if upload_err is None:
                # Transport-level reason: the store's stderr-tail observable
                # (best-effort — under concurrency it may carry a sibling's
                # failure, still the right throttle/HTTP hint).
                upload_err = getattr(_files_store, "last_upload_error", None)
            tail = (upload_err or "").strip()[-80:]
            _note_repair_failure(
                m, tid, f"upload failed: {tail}" if tail else "upload failed")
            continue
        cache.write_cached_task(body_to_upload)
        try:
            post_stat = remote.stat(task_path, backend=backend)
            if post_stat:
                cache.write_meta(task_path, post_stat)
        except Exception:
            pass

    if repair_deferred_ops:
        _warn(f"  Task body repair: deferred {len(repair_deferred_ops)} "
              "marker(s) at the reconcile budget floor — they drain next tick.")
        ops_log.log_op(
            "reconcile",
            status="task_body_repair_deferred",
            detail=(f"deferred {len(repair_deferred_ops)} body repair(s) at "
                    "the deadline budget floor; markers kept for next tick"),
        )

    if body_repair_failures:
        failed_ids = sorted(set(body_repair_failures))
        _warn(f"  Task body repair failures: {failed_ids}")
        # Surface the first few WHYs inline: an operator tailing the log must
        # see the reason, not just the id (the 2026-06-11 diagnosis had to
        # guess from an id-only aggregate).
        for _ftid in failed_ids[:3]:
            _warn(f"    {_ftid}: {body_repair_reasons.get(_ftid, 'unknown')}")
        ops_log.log_op(
            "reconcile",
            status="task_body_repair_failed",
            detail=(f"failed task body repairs: {failed_ids}; reasons: "
                    + json.dumps({t: body_repair_reasons.get(t, "unknown")
                                  for t in failed_ids}, sort_keys=True)),
        )
        # Do NOT clear op markers. These are authoritative-body repair debts, not
        # mere view debts, so rebuilding views from cache would only make a
        # still-missing task look delivered.
        return 1

    try:
        # Reconcile is the authoritative view repair path, so its task set must
        # come from durable task files when the raw listing is available. The
        # view-seeded loader can miss ids, or keep a stale cached body for an id
        # absent from every stale view, which makes reconcile faithfully re-upload
        # the stale view forever.
        all_tasks = _load_all_tasks_by_listing(backend=backend)
        load_degraded = False
        if all_tasks is None:
            all_tasks = _load_all_tasks(backend=backend)
            load_degraded = bool(getattr(all_tasks, "load_degraded", False))
    except Exception as e:
        _warn(f"Could not load remote tasks: {e}")
        all_tasks = cache.list_cached_tasks()
        load_degraded = True

    _info(f"  {len(all_tasks)} task(s) loaded.")

    now = datetime.now(timezone.utc)
    stale_claims = _detect_stale_claims(all_tasks, now)

    if stale_claims:
        _warn(f"  Stale claims detected: {stale_claims}")

    if load_degraded:
        # 2026-06-11 write-path read-error audit (F3): the task load fell back
        # to LOCAL CACHE ONLY because the remote index could not be READ (not
        # because the bus is genuinely fresh/empty — that case is probed apart
        # inside _load_all_tasks and is not degraded). Rebuilding + uploading
        # the views from this host's partial cache would TRUNCATE the bus's
        # global read surface — and since reconcile re-discovers tasks through
        # those very views, a dropped task could never come back. A reconcile
        # that can't see the bus must not rewrite the bus's views: skip the
        # whole view rebuild/upload phase this tick and fail loudly (the
        # body-repair pass above already ran — it needs only the markers'
        # cached bodies, not the full set). Markers stay for the next tick.
        _err("  Remote task index unreadable — view rebuild skipped this tick "
             "(rebuilding from local cache alone would truncate the bus's views).")
        ops_log.log_op("reconcile", status="degraded",
                       detail="index unreadable; cache-only task set — view "
                              "rebuild/upload skipped")
        return 1

    if time.monotonic() - t0 > timeout:
        _err("Reconcile timeout exceeded.")
        return 1

    # E4 TICK-SCOPED SNAPSHOT SHARING (measured): one reconcile tick used to
    # download the summaries view THREE times (rebuild-source acks, parity's
    # ack authority, the undelivered check's ack map), load presence three
    # times, and sweep the directives prefix two-three times — each repeat a
    # fresh ~1.3s subprocess answering a question this tick already answered.
    # Load each shared snapshot ONCE here and thread it through the sub-passes
    # (every helper keeps a self-loading fallback for direct callers). An
    # absent/None snapshot stays absent for the WHOLE tick — one consistent
    # world-view per tick, not three chances at a different one.
    try:
        summaries_view = remote.download_json(
            remote.view_remote_path("summaries"), backend=backend)
    except Exception:
        summaries_view = None

    all_views = views.build_all_views(
        _reconcile_rebuild_source_preserving_acks(
            all_tasks, backend=backend, summaries_view=summaries_view)
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
    #
    # NO SKIP HERE — division of labor with the write path (2026-06-11 review
    # finding on the skip-unchanged change): the success-only fingerprint
    # proves only what THIS HOST last uploaded, never the remote's CURRENT
    # content — the store has no compare-and-swap and views are shared mutable
    # paths, so host B can overwrite a view after host A recorded its digest.
    # If reconcile honored the fingerprint skip, A would rebuild the same
    # content, match its local fingerprint, skip — and B's clobber would
    # persist INDEFINITELY, because reconcile is the designated repair path.
    # So: the WRITE path skips unchanged views (cheap, hot, single-host
    # correct — see writepipe._write_task_and_views), while reconcile
    # authoritatively re-asserts EVERY rebuilt view, bounding cross-host view
    # drift to one reconcile cadence (~20 min) — the pre-skip status quo for
    # repair. Fingerprints ARE still recorded on this pass's successes: that
    # refreshes the write path's skip baseline so the next write doesn't
    # re-upload what reconcile just confirmed.
    failures = []
    view_digests = {name: _view_fingerprint(data) for name, data in view_items}
    upload_items = list(view_items)
    # Bounded in-tick retry (live 0.15.0 evidence, two hosts): under this very
    # burst a ROTATING subset of views fails each tick — backend throttling /
    # transient 5xx — while single raw uploads succeed in <1s. One jittered
    # retry converts those transients into successes instead of failing the
    # tick (which preserved markers and self-healed next tick, but left EVERY
    # tick partially failing and views stale). Bounded to ONE retry, gated on
    # real deadline headroom, and call-site-local by design: store.upload is
    # shared by many paths whose single-write callers already have their own
    # ops-log/self-heal discipline — a transport-level retry would silently
    # double every timeout everywhere.
    retry_enabled = env_int("FULCRA_COORD_UPLOAD_RETRY", 1) != 0
    retry_stats = {"recovered": 0}
    import threading
    retry_lock = threading.Lock()

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
        # 2026-06-11 bug hunt S7: the per-view budget is min(remaining
        # deadline, transport write timeout) — NOT the whole remaining
        # deadline. Handing one upload the full deadline let a single wedged
        # backend call eat the entire tick's budget (and its retry eat it
        # again), starving every other view behind it in the pool. Resolved
        # via remote._write_timeout() (env-tunable), never a constant.
        try:
            ok = remote.upload_json(
                view_data, vpath, backend=backend,
                timeout=min(int(remaining), remote._write_timeout()))
        except Exception:
            ok = False
        if not ok and retry_enabled:
            # Jitter de-syncs the retry burst from the original burst that got
            # us throttled (and from the other workers' retries).
            jitter = random.uniform(0.5, 2.0)
            remaining = deadline - time.monotonic()
            # Deadline-headroom gate: the retry only runs if the jitter sleep,
            # the 1s per-upload budget floor (the BUG 6b guard above), AND 2s
            # of slack all fit before the global deadline. The deadline stays
            # a hard ceiling — a retry can never be what pushes reconcile past
            # it, so the no-headroom case fails exactly as before the fix.
            if remaining > jitter + 1.0 + 2.0:
                _retry_sleep(jitter)
                remaining = deadline - time.monotonic()
                if remaining >= 1:
                    try:
                        # Same S7 cap as the first attempt: a retry must
                        # never inherit the whole remaining deadline either.
                        ok = remote.upload_json(
                            view_data, vpath, backend=backend,
                            timeout=min(int(remaining),
                                        remote._write_timeout()))
                    except Exception:
                        ok = False
                    if ok:
                        with retry_lock:
                            retry_stats["recovered"] += 1
        if not ok:
            # FINAL failure (attempt + any retry exhausted): leave a diagnosable
            # trace beyond the view name. ``last_upload_error`` is the
            # transport's best-effort stderr-tail observable (see its comment in
            # fulcra_coord_files.store) — under the parallel pool it may carry a
            # sibling failure's reason, which is still the right throttle hint.
            # Best-effort: a logging error must never turn into a crashed view.
            try:
                ops_log.log_op("reconcile", view_name,
                               status="view_upload_failed",
                               error=getattr(_files_store, "last_upload_error", None))
            except Exception:
                pass
        return view_name, ok

    max_workers = min(8, len(upload_items)) or 1
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    future_to_view = {
        executor.submit(_upload_one, item): item[0]
        for item in upload_items
    }
    try:
        for future in concurrent.futures.as_completed(
            future_to_view, timeout=max(0, deadline - time.monotonic())
        ):
            try:
                view_name, ok = future.result()
            except Exception:
                view_name = future_to_view.get(future, "<unknown>")
                ok = False
            if ok:
                # SUCCESS-ONLY fingerprint (main thread, like the write path):
                # the next WRITE may skip this view (reconcile itself never
                # skips — it is the cross-host drift repair). Failures keep
                # their stale/absent fingerprint so the next write re-attempts.
                cache.write_view_fingerprint(view_name, view_digests[view_name])
            else:
                failures.append(view_name)
    except concurrent.futures.TimeoutError:
        pending = [name for future, name in future_to_view.items()
                   if not future.done()]
        failures.extend(pending)
        _warn(f"  View uploads timed out before completion: {pending}")
    finally:
        # Do not let a wedged upload worker hold the reconcile command past its
        # global deadline. Finished futures were consumed above; not-yet-started
        # futures are cancelled, and already-running subprocesses are allowed to
        # wind down under their own transport timeout without blocking this tick.
        executor.shutdown(wait=False, cancel_futures=True)

    recovered = retry_stats["recovered"]
    if recovered:
        # Reported even on a fully-green tick: a steady recovered count is the
        # signal that the backend is throttling the burst (the condition this
        # retry exists for) — invisible if only surfaced next to failures.
        _info(f"  View uploads: {recovered} recovered on retry.")

    if time.monotonic() - t0 > timeout:
        _err("Reconcile timeout exceeded mid-upload.")
        ops_log.log_op("reconcile", status="timeout")
        return 1

    # Rebuild the presence aggregate from the durable per-agent presence records,
    # mirroring how the task views self-heal here. Best-effort: a presence rebuild
    # failure must not fail a task-view reconcile, so it is reported but does not
    # count toward `failures`. E4: the REBUILT roster is captured and threaded
    # through the presence-consuming sub-passes below (reroute sweep, role
    # health, undelivered check) — it is the freshest presence truth this tick
    # will see, so re-loading it would only spend spawns to learn less. The
    # isinstance guard also protects against a patched/failed rebuild handing
    # back a non-dict.
    if _deadline_spent("Presence rebuild", headroom=5.0):
        presence_view = None
    else:
        presence_view = _reconcile_presence(backend=backend)
    # F5 boundary case: the rebuild read PARTIALLY failed AND the previous
    # aggregate is unreadable — presence is unknowable this tick (the sentinel,
    # distinct from None = "nothing to rebuild, sub-passes may self-load").
    presence_unknown = presence_view is _PRESENCE_READ_ERROR
    if not isinstance(presence_view, dict):
        presence_view = None
    presence_agents = (
        [a for a in presence_view.get("agents", []) if isinstance(a, dict)]
        if presence_view else None
    )

    # Liveness-aware reroute sweep (best-effort; never fails a reconcile tick).
    # Runs AFTER the presence rebuild so it reads the freshly-reconciled
    # roster (threaded in; the sweep self-loads when the rebuild yielded
    # nothing). Considers only kind:review directives; reroutes never-acted
    # reviews whose assignee fell below liveness floor, escalates on cap/miss,
    # freezes accepted-then-stalled ones. Whichever machine reconciles first
    # wins; others converge via the stale-observation re-read inside the sweep.
    #
    # F5: when presence is UNKNOWN this tick the sweep is skipped outright —
    # every verdict it can reach (reroute away from a "below-floor" assignee,
    # escalate "no reviewer live" to the human) is a durable wrong decision
    # when the assignee merely failed to READ. We lived this: a live
    # reviewer's record 504'd and the operator got "no reviewer live"
    # escalations while the reviewer was up. Letting the sweep self-load
    # would re-read through the same failing transport, so skip is the only
    # honest option; deferred directives drain on the next clean tick.
    if presence_unknown:
        _warn("  ⚠ presence unreadable this tick (partial per-agent read, no "
              "usable aggregate) — review-route sweep skipped (no rerouting "
              "on an unknowable roster)")
    elif _deadline_spent("Review-route sweep", headroom=5.0):
        pass
    else:
        try:
            _sweep_review_routes(all_tasks, backend=backend, now=now,
                                 deadline=deadline, presence=presence_agents)
        except Exception:
            pass

    # Retention pass (best-effort, throttled to ~once/day, bounded + time-budgeted
    # against THIS reconcile's deadline so it never double-counts the 90s ceiling).
    # Never raises into the tick; logs its tally.
    retention_marker = None   # threaded to the health record (perf loop-2 #6):
    # the pass's throttle claim just read (or wrote) retention/last-run.json,
    # so the health write below must not pay a third download for it.
    try:
        if _deadline_spent("Retention pass", headroom=5.0):
            raise _SkipBestEffortCheck()
        ret = _run_retention(all_tasks, now=now, deadline=deadline, backend=backend)
        retention_marker = ret.get("retention_marker")
        if not ret.get("skipped"):
            _info(f"  Retention: archived {ret['archived']} task(s) "
                  f"(deferred {ret['deferred']}), expired {ret.get('expired_broadcasts', 0)} "
                  f"broadcast(s), closed {ret.get('closed_messages', 0)} "
                  f"message(s), pruned {ret['pruned_markers']} marker(s), "
                  f"{ret['pruned_presence']} dead presence, {ret.get('pruned_health', 0)} health, "
                  f"{ret.get('pruned_continuity', 0)} continuity, "
                  f"{ret.get('pruned_events', 0)} events, "
                  f"{ret.get('pruned_provenance', 0)} prov.")
    except _SkipBestEffortCheck:
        pass
    except Exception as e:
        _warn(f"  Retention pass error (skipped): {e}")

    if failures:
        retry_note = f" ({recovered} recovered on retry)" if recovered else ""
        _warn(f"  View upload failures: {failures}{retry_note}")
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
            # Reuse the marker the retention pass already read/wrote this tick
            # (loop-2 #6); the download remains ONLY as the fallback for ticks
            # where the pass never reached the marker (budget-gated skip,
            # claim error, or the pass itself raising).
            rmark = retention_marker
            if rmark is None:
                rmark = remote.download_json(
                    remote.retention_marker_path(now), backend=backend)
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
        # Event-parity sub-pass: fold each task's event log and compare
        # status to the mutable snapshot. Best-effort — any error is swallowed and
        # the result, whether present or absent, NEVER changes the reconcile exit
        # code. Drift is recorded in the health record as health debt only; the
        # mutable file remains authoritative (the events read cutover is gated
        # on sustained parity). PERF: hand over the
        # bodies this tick already loaded (no re-downloads) and the tick's
        # deadline (the pass samples + stops on a spent budget — see the check).
        try:
            if _deadline_spent("Event-parity check", headroom=5.0):
                raise _SkipBestEffortCheck()
            parity = _event_parity_check(all_tasks, backend=backend,
                                         deadline=deadline,
                                         summaries_view=summaries_view)
            record["event_parity"] = parity
        except _SkipBestEffortCheck:
            pass
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
            if _deadline_spent("Dual-write health", headroom=2.0):
                raise _SkipBestEffortCheck()
            record["event_dual_write"] = _event_dual_write_health()
        except _SkipBestEffortCheck:
            pass
        except Exception as _de:
            try:
                _warn(f"  Dual-write health skipped (error): {_de}")
            except Exception:
                pass
        # E4: ONE directives-prefix sweep serves both the directive-parity and
        # loop-health sub-passes below (each used to pay its own listing +
        # per-record downloads). None on failure -> both fall back to their
        # self-loading paths (which will most likely fail the same way and
        # degrade as before).
        try:
            if _deadline_spent("Loop-record load", headroom=10.0):
                raise _SkipBestEffortCheck()
            loop_records = load_loop_records(backend=backend)
        except _SkipBestEffortCheck:
            loop_records = None
        except Exception:
            loop_records = None
        # Directive-parity sub-pass: fold each first-class directive record
        # against its back-ref task and surface divergence as health debt.
        # REPORT-ONLY and best-effort — the task record stays authoritative for
        # task state (the loop records carry coordination state), so any error
        # is swallowed and the result, present or absent, NEVER changes
        # reconcile's exit code. Wrapped exactly like the event-parity pass above.
        try:
            if _deadline_spent("Directive-parity check", headroom=10.0):
                raise _SkipBestEffortCheck()
            record["directive_parity"] = _directive_parity_check(
                backend=backend, records=loop_records, all_tasks=all_tasks,
                deadline=deadline)
        except _SkipBestEffortCheck:
            pass
        except Exception as _dpe:
            # Best-effort but not silent: a checker that ate its own error would
            # report green while broken. Surface it (guarded so the warn can't
            # break reconcile either); record["directive_parity"] is simply absent.
            try:
                _warn(f"  Directive-parity check skipped (error): {_dpe}")
            except Exception:
                pass
        # Coordination-loop health sub-pass (spec 2026-06-09 Task 5): open /
        # overdue / awaiting-me counts from the pure board fold — the
        # never-ANSWERED counterpart to the undelivered (never-ARRIVED) check
        # below. REPORT-ONLY and best-effort, wrapped exactly like the parity
        # passes above: the result, present or absent, NEVER changes
        # reconcile's exit code, and the error is surfaced rather than eaten.
        try:
            if _deadline_spent("Loop-health check", headroom=30.0):
                raise _SkipBestEffortCheck()
            record["loop_health"] = _loop_health_check(backend=backend,
                                                       records=loop_records)
        except _SkipBestEffortCheck:
            pass
        except Exception as _lhe:
            try:
                _warn(f"  Loop-health check skipped (error): {_lhe}")
            except Exception:
                pass
        # Role-health sub-pass (spec 2026-06-10): registry status per role —
        # HELD / VACANT / CONTESTED — with the SLA-vacancy escalation riding
        # inside it (idempotent via the daily marker). Vacancy is the
        # generalized dark-agent signal: "function is unstaffed", not "agent
        # is dark". REPORT-ONLY and best-effort, wrapped exactly like the
        # loop-health pass above: the result, present or absent, NEVER
        # changes reconcile's exit code, and the error is surfaced, not eaten.
        try:
            if _deadline_spent("Role-health check", headroom=30.0):
                raise _SkipBestEffortCheck()
            # F5: an unknowable roster (sentinel) makes every role's vacancy
            # judgment unknown inside the check — reported, never escalated.
            record["role_health"] = _role_health_check(
                backend=backend,
                presence_agents=(_PRESENCE_READ_ERROR if presence_unknown
                                 else presence_agents))
        except _SkipBestEffortCheck:
            pass
        except Exception as _rhe:
            try:
                _warn(f"  Role-health check skipped (error): {_rhe}")
            except Exception:
                pass
        # SAFETY NET: directives addressed to an OFFLINE/stale agent that were
        # never picked up — the dead-inbox bug. Report-only and best-effort: it
        # never mutates or reroutes, and the wrapping guarantees a failure can
        # never change reconcile's exit code (mirrors the parity passes above).
        # all_tasks is already in hand, so this adds no remote round-trip beyond
        # the single presence-aggregate read inside the check.
        try:
            if _deadline_spent("Undelivered-directive check", headroom=15.0):
                raise _SkipBestEffortCheck()
            ud = _undelivered_directive_check(
                all_tasks, backend=backend,
                # E4: hand over the freshly-rebuilt presence view when we have
                # one; otherwise leave the default so the check downloads the
                # aggregate itself (its None-vs-absent INDETERMINATE semantics
                # stay intact).
                presence_view=(presence_view if presence_view is not None
                               else _UNSET),
                summaries_view=summaries_view)
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
        except _SkipBestEffortCheck:
            pass
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
        if skipped_checks:
            record["skipped_checks"] = list(skipped_checks)
        slug = views.agent_slug(record.get("host") or identity.resolve_agent())
        if not remote.upload_json(record, remote.health_remote_path(slug), backend=backend):
            _warn("  Health record upload failed (best-effort; tick unaffected).")
    except Exception as e:
        _warn(f"  Health record write error (skipped): {e}")
    # ------------------------------------------------------------------------

    for m in needs_repair:
        # A deadline-DEFERRED repair was never attempted — its marker is the
        # only record of the debt, so clearing it here would silently drop the
        # pending body write. Same for a backoff-SKIPPED one: it was parked,
        # not repaired, and clearing it would silently forgive the debt. Keep
        # both for a later tick's budget / window expiry.
        if m.get("op_id") in repair_deferred_ops:
            continue
        if m.get("op_id") in repair_backoff_ops:
            continue
        cache.clear_op_marker(m["op_id"])

    ops_log.log_op("reconcile", status="ok", detail=f"{len(all_tasks)} tasks, {len(all_views)} views")
    _info(f"  Reconcile complete. {len(all_views)} views refreshed.")
    return 0
