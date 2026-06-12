"""Inbox + blocked-on-you notification for fulcra-coord.

The agent's directive inbox (the ``inbox`` command + its load/ack), the listener's
pending-directive surface file, and the desktop-notification path for NEW
blocked-on-you items (notify once per item via a seen-set, never re-firing). Reads
the summaries aggregate and writes acks through the single write pipeline.

Extracted from cli.py behind stable re-exports; depends only on lower layers (io
loaders, the write pipeline, output, cache/remote/schema/views/identity/listener/
wake) and never imports cli, so the split has no cycle. ``_inbox_surface_path`` is
re-exported because the reconcile-side ``_build_health_record`` (still in cli) reads
the listener's last-fire mtime from it; ``_derive_agent`` is the usual thin alias.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any, Optional

from . import cache, remote, schema, views, identity, listener, selfupdate, wake, env_int
from .io import _cache_remote_task, _load_task, _load_task_summaries
from .output import info as _info, print_json as _print_json, warn as _warn, err as _err
from .writepipe import _view_name_to_remote, _write_task_and_views


def _derive_agent() -> str:
    return identity.resolve_agent()

def _my_roles(me: str, backend: Optional[list[str]] = None) -> set[str]:
    """The set of roles `me` currently HOLDS, read from its OWN presence record.

    This is what makes role-audience (``@<role>``) directives deliverable: a
    directive addressed to a role lands in this agent's inbox iff the role is in
    this set (views.inbox_for). We read the durable per-agent ``presence/<slug>.json``
    (via presence._load_own_presence — the same single read path `workstream`
    uses) and take its ``capabilities``.

    BACKWARD-COMPATIBLE / FAIL-SAFE: an agent that never declared roles, an old
    presence record predating the ``capabilities`` field (None), or any read
    failure all collapse to the EMPTY set — so no role directives are surfaced
    and a role-resolution read can never crash the inbox. ``presence`` is
    lazy-imported so importing inbox stays cheap and the module-load graph flat.
    """
    try:
        from . import presence  # lazy: keep inbox import light, avoid load-order risk
        rec = presence._load_own_presence(me, backend=backend)
    except Exception:
        return set()
    # F8: the loader now returns PRESENCE_READ_ERROR (truthy, not a dict) for
    # a failed read; the isinstance guard keeps this surface on its documented
    # fail-safe — a blind read surfaces no role directives, never a crash.
    if not isinstance(rec, dict):
        return set()
    return {c for c in (rec.get("capabilities") or []) if c}

def _load_task_from_summary(summary: dict[str, Any], *,
                            backend: Optional[list[str]] = None) -> Optional[dict[str, Any]]:
    """Load a task body using the exact durable path carried by a summary."""
    task_id = summary.get("id")
    task_file = summary.get("task_file")
    if not (task_id and task_file):
        return None
    # Route the body read through the single funnel so it honors the per-host
    # read_source() knob (file vs event-fold) like every other read path. The
    # funnel derives the path from task_id itself, but we still require the
    # summary to NAME a durable file (the guard above) — that's what makes this
    # fallback applicable at all. The funnel doesn't assert the id matches, so
    # keep the caller's identity guard below.
    task = _cache_remote_task(task_id, backend=backend)
    if not (task and task.get("id") == task_id):
        return None
    cache.write_cached_task(task)
    return task

def _ack_summary_only(task_id: str, me: str, *,
                      backend: Optional[list[str]] = None) -> bool:
    """Record an inbox ack in summaries when the task body is missing."""
    summaries = _load_task_summaries(backend=backend)
    changed = False
    found = False
    rebuilt: list[dict[str, Any]] = []
    for item in summaries:
        if item.get("id") != task_id:
            rebuilt.append(item)
            continue
        found = True
        updated = dict(item)
        acked_by = set(updated.get("acked_by") or [])
        if me not in acked_by:
            acked_by.add(me)
            updated["acked_by"] = sorted(acked_by)
            changed = True
        rebuilt.append(updated)

    if not found:
        return False
    if not changed:
        return True

    ok = True
    for view_name, view_data in views.build_all_views(rebuilt).items():
        try:
            uploaded = remote.upload_json(
                view_data, _view_name_to_remote(view_name), backend=backend)
        except Exception:
            uploaded = False
        cache.write_cached_view(view_name, view_data)
        ok = uploaded and ok
    return ok

def cmd_inbox(args: Any, backend: Optional[list[str]] = None) -> int:
    """List (or ack) open directives addressed to the calling agent.

    Read path recomputes authoritatively from the task summaries (see
    _load_inbox) — mirroring cmd_agents. (The per-assignee inbox view files
    this once avoided trusting were retired entirely in the 2026-06-11 perf
    wave; the recompute IS the read path.) `--ack <id>` records an inbox_ack
    event so the listener stops re-notifying, without claiming the task.
    """
    me = getattr(args, "agent", None) or _derive_agent()
    out_format = getattr(args, "format", "table")
    ack_id = getattr(args, "ack", None)
    show_all = bool(getattr(args, "all", False))

    if ack_id:
        task = _load_task(ack_id, backend=backend)
        if task is None:
            match = next(
                (item for item in _load_inbox(me, backend=backend, include_aged=True)
                 if item.get("id") == ack_id),
                None,
            )
            if match:
                task = _load_task_from_summary(match, backend=backend)
            if task is None:
                if match and _ack_summary_only(ack_id, me, backend=backend):
                    # Same durable per-agent directive ack as the full-body path
                    # (best-effort): the summary-only fallback still records the
                    # clobber-safe ack so the durable union stays complete.
                    try:
                        from . import directives
                        directives.write_directive_ack(
                            directives.stable_directive_id(ack_id), me, backend=backend)
                    except Exception:
                        pass
                    _info(f"Acknowledged: {ack_id}")
                    return 0
                _err(f"Task not found: {ack_id}")
                return 1
        task = schema.apply_event(task, "inbox_ack", by=me,
                                  summary=f"Inbox acknowledged by {me}.")
        cache.write_cached_task(task)
        ok = False
        try:
            ok = _write_task_and_views(task, backend=backend, command="inbox-ack")
        except schema.ConflictError as e:
            _err(str(e))
            return 2
        except schema.NeedsReconcile as e:
            _warn(str(e))
            ok = True
        if not ok:
            _warn(f"Ack cached locally but remote upload failed: {ack_id}.")
            return 1
        # ALSO record a DURABLE, clobber-safe per-agent directive
        # ack in the append-only sub-log. The task's inline ``inbox_ack`` event is
        # capped (the bounded event log can drop it) and a single-record update
        # would clobber concurrent acks of a broadcast; the per-agent ack file is
        # the durable truth (one file per acking agent, idempotent). Lazy-import
        # keeps directives low-layer (it never imports inbox). Best-effort: wrapped
        # so a sub-log failure NEVER affects the ack command's result.
        try:
            from . import directives
            directives.write_directive_ack(
                directives.stable_directive_id(ack_id), me, backend=backend)
        except Exception:
            pass
        _info(f"Acknowledged: {ack_id}")
        return 0

    # Load the task set ONCE, then derive both the shown items and the aged-out
    # count from it — no second backend round-trip. With --all the age-out filter
    # is bypassed and aged-out broadcasts are included; otherwise stale
    # informational broadcasts are hidden and only counted for the note below.
    all_tasks = _load_task_summaries(backend=backend)
    # BUG 14: pin a single `now` for the whole command. inbox_for and
    # aged_out_inbox_count each resolve _now() independently (3+ reads per
    # cmd_inbox), so at the age-out boundary the same broadcast could be SHOWN by
    # one read and COUNTED HIDDEN by a later one. One timestamp keeps them
    # consistent — an id is either shown or counted hidden, never both.
    now = views._now()
    # Load `me`'s declared roles so role-audience (@<role>) directives this agent
    # HOLDS resolve into its inbox (delivery-time role resolution). Empty for an
    # agent with no declared roles — fully backward-compatible.
    roles = _my_roles(me, backend=backend)
    items = views.inbox_for(me, all_tasks, now=now, include_aged=show_all, roles=roles)
    hidden = 0 if show_all else views.aged_out_inbox_count(me, all_tasks, now=now)

    if out_format == "json":
        _print_json({"agent": me, "count": len(items), "hidden_aged": hidden,
                     "inbox": items})
        return 0

    if not items:
        if hidden:
            _info(f"Inbox empty for {me} "
                  f"({hidden} older broadcast{'s' if hidden != 1 else ''} "
                  f"hidden — --all to show).")
        else:
            _info(f"Inbox empty for {me}.")
        return 0

    print(f"\n{'='*60}")
    print(f"  Inbox — directives for {me}")
    print(f"{'='*60}")
    for s in items:
        frm = s.get("owner_agent", "?")
        print(f"  [{s.get('priority','??')}] {s.get('id','')}  {s.get('title','')[:50]}")
        print(f"        from: {frm}")
        if s.get("next_action"):
            print(f"        next: {s['next_action'][:70]}")
        # Continuity handoff (spec 2026-06-10): show the resume point so the
        # recipient knows this directive carries where-the-sender-left-off
        # BEFORE claiming it (the brief itself renders at claim time).
        if s.get("checkpoint_ref"):
            print(f"        checkpoint: {s['checkpoint_ref']}")
    if hidden:
        print(f"\n  ({hidden} older broadcast{'s' if hidden != 1 else ''} "
              f"hidden — --all to show)")
    print()
    return 0

def _load_inbox(me: str, backend: Optional[list[str]] = None,
                include_aged: bool = False,
                summaries: Optional[list[dict[str, Any]]] = None
                ) -> list[dict[str, Any]]:
    """Open directives for `me`, recomputed authoritatively from the full task set.

    Mirrors cmd_agents: inbox_for over the live tasks is the single source of
    truth.

    Membership uses prefix-aware matching (views.inbox_for / agent_matches): a
    directive addressed to a short id like `claude-code` reaches the full-id
    agent `claude-code:<host>:<repo>` it prefixes. This is the correctness fix
    for the original bug — strict slug equality silently dropped short-id
    directives.

    Why recompute (history): the materialized inbox/<slug> views were only
    emitted for assignees who still had at least one open directive, so when an
    inbox emptied the stale file was never overwritten and preferring it
    returned a phantom directive forever (`inbox` re-listed handled work, the
    listener re-notified, SessionStart re-injected). The recompute became the
    read path, which left those view files write-only — and as of the
    2026-06-11 perf wave they are no longer materialized at all (see
    views.build_all_views' RETIRED VIEWS note). Recomputing from the task set
    always reflects the current truth, at the cost of one task-set load — the
    same cost cmd_agents pays.
    """
    # Summaries fast-path: inbox_for reads assignee/status/owner_agent and the
    # ack set, which the summary now carries (acked_by) — no event log / body
    # fetch needed. Falls back to a full load on an older bus. ``summaries``
    # threads an already-loaded set through (perf loop-2 #2: the listener tick
    # loads once and shares it with the needs-me pass — one spawn saved per
    # tick, and in stale-guard mode one full direct-listing fallback re-run).
    all_tasks = (summaries if summaries is not None
                 else _load_task_summaries(backend=backend))
    # Role resolution: load `me`'s declared roles so a directive addressed to a
    # role this agent HOLDS (@<role>) is delivered here, not lost. Empty set for
    # an agent with no roles / old presence record — backward-compatible.
    roles = _my_roles(me, backend=backend)
    # include_aged bypasses the broadcast age-out filter (the `inbox --all` path);
    # the default read hides stale informational broadcasts so they stop
    # cluttering the inbox / SessionStart, without touching any task.
    return views.inbox_for(me, all_tasks, include_aged=include_aged, roles=roles)

def _inbox_surface_path(agent: str):
    """Where the listener drops pending directives for the next SessionStart to
    read. Root-scoped via cache_root() and suffixed by the agent slug so two
    agents on one machine don't clobber each other's surface file."""
    return cache.cache_root() / f"inbox-pending-{listener.agent_slug(agent)}.json"

def _needs_me_seen_path(human: str):
    """Seen-set surface for blocked-on-you notifications, keyed by the HUMAN
    handle (not the polling agent): the "has the operator already been alerted
    about this item" marker. Like the inbox-pending surface but a set of task
    ids, so the listener notifies ONCE per new needs-me item and never re-fires
    for one it already announced. Slugged via the same agent_slug so a handle
    with odd characters maps to a safe filename."""
    return cache.cache_root() / f"needs-me-seen-{listener.agent_slug(human)}.json"

def _inbox_notified_seen_path(agent: str):
    """Seen-set surface for the agent's OWN inbox-count notification, keyed by
    the polling agent. Mirrors ``_needs_me_seen_path`` (which guards the human
    blocked-on-you path): a set of directive ids the operator was already pinged
    about, so a tick re-alerts only on NEW inbox items instead of every tick.
    Slugged via the same agent_slug so an odd-character handle maps to a safe
    filename, and distinct from the inbox-pending surface (which holds the full
    payload, not just ids)."""
    return cache.cache_root() / f"inbox-notified-{listener.agent_slug(agent)}.json"

def _notify_new_needs_me(backend: Optional[list[str]] = None,
                         summaries: Optional[list[dict[str, Any]]] = None
                         ) -> None:
    """Fire a desktop notification for each NEW item blocked on the human.

    Polled alongside the inbox by the listener (Part 5). Resolves the human via
    resolve_human(), loads what's blocked on them, and for every item not yet in
    the per-human seen-set emits "⛔ <agent> needs you: <ask>" once. Idempotent:
    the seen-set (a task-id list persisted next to the inbox surface) means a
    repeat tick over the same item does not re-notify, while a genuinely new
    blocked-on-you item alerts. Best-effort — wrapped by the caller's try/except
    so it can never crash a polling tick. No-op when nothing is blocked."""
    human = identity.resolve_human()
    # needs_human reads status/assignee/tags — all on a summary; no body fetch.
    # ``summaries`` threads the tick's already-loaded set through (perf loop-2
    # #2); None (direct callers) keeps the self-load.
    items = views.needs_human(
        summaries if summaries is not None
        else _load_task_summaries(backend=backend), human)
    seen_path = _needs_me_seen_path(human)
    seen: set[str] = set()
    if seen_path.exists():
        try:
            seen = set(json.loads(seen_path.read_text()))
        except (json.JSONDecodeError, OSError, TypeError):
            seen = set()

    current_ids = {i["id"] for i in items}
    for it in items:
        if it["id"] in seen:
            continue
        ask = (it.get("blocked_on") or it.get("next_action") or "").strip()
        frm = it.get("owner_agent", "?")
        listener.emit_message(f"⛔ {frm} needs you: {ask}" if ask
                              else f"⛔ {frm} needs you: {it.get('title','')}")

    # Persist the seen-set as the CURRENT item ids: newly-notified items are now
    # seen, and items that have since cleared (resolved) drop out so that if the
    # SAME task is blocked-on-you again later it re-notifies (a fresh ask).
    cache.cache_root().mkdir(parents=True, exist_ok=True)
    seen_path.write_text(json.dumps(sorted(current_ids)))

def _overdue_loop_suffix(me: str, backend: Optional[list[str]] = None) -> str:
    """Best-effort " · N overdue" notification suffix: open loops `me` OPENED
    whose per-kind SLA has lapsed (spec 2026-06-09 Task 7), folded by
    ``loops.awaiting_others`` over the directives prefix — the listener-side
    twin of reconcile's ``_loop_health_check`` overdue count, riding the
    notification the operator actually sees.

    Empty string when N == 0 or when the scan fails for ANY reason, so the
    inbox notification reads exactly as before whenever loops can't be counted
    (fail-safe: a polling tick must never grow a new failure mode). Closure
    notifications ("loop closed") are deliberately NOT here: announcing a
    closure exactly once needs a durable seen-marker (like the blocked-on-you
    seen-set) and is deferred until one exists.

    Cost: one paths-only listing of the directives prefix plus one download
    per TOP-LEVEL loop record per notifying tick — ``loop_ops.load_loop_records``
    owns both the load-bearing top-level-only filter and the filter-before-
    download discipline (this used to be an inline copy that downloaded every
    sub-log shard too; loop_ops sits BELOW inbox in the layering — it imports
    only remote/loops/log/output — so sharing the helper costs no inversion).
    Lazy imports keep the module load graph flat — this only runs inside a
    tick."""
    try:
        from datetime import datetime, timezone
        from . import loop_ops, loops
        records = loop_ops.load_loop_records(backend=backend)
        overdue = sum(1 for x in loops.awaiting_others(
            me, records, now=datetime.now(timezone.utc)) if x.get("overdue"))
        return f" · {overdue} overdue" if overdue else ""
    except Exception:
        return ""  # best-effort: fall back to the plain count message


def _notify_overdue_loop_suffix_enabled() -> bool:
    """Whether notify-inbox may pay the optional directive-loop scan.

    The core listener path must stay cheap enough to run under launchd forever:
    write the inbox surface, emit the new-item notification, wake if configured,
    and exit. The overdue-loop suffix is nice operator context, but it walks the
    directives prefix and can fan out into many remote downloads. Keep it opt-in
    so optional decoration cannot wedge the listener and suppress future ticks.
    """
    return env_int("FULCRA_COORD_NOTIFY_OVERDUE_SUFFIX", 0) != 0


def _notify_stale_summary_fallback_enabled() -> bool:
    """Whether a scheduled listener tick may rebuild stale summaries.

    Interactive reads can trade latency for freshness. A launchd/cron listener
    cannot: if it wins the direct-listing fallback claim it may spend minutes
    statting/downloading task bodies, which makes the installed listener look
    armed while suppressing later interval fires. Default to serving the stale
    aggregate for this tick; operators can opt into the old repair-shaped path
    with FULCRA_COORD_NOTIFY_STALE_SUMMARY_FALLBACK=1.
    """
    return env_int("FULCRA_COORD_NOTIFY_STALE_SUMMARY_FALLBACK", 0) != 0


def _stale_summary_alert_threshold_min() -> int:
    """View-staleness age that should alert the operator from a listener tick.

    The listener's default bounded mode deliberately avoids the direct-listing
    fallback. That is right for host health, but a chronic stale summaries view
    can otherwise make listeners silently miss newly-directed work. Alerting is
    cheap and throttled; set to 0 to disable.
    """
    return env_int("FULCRA_COORD_NOTIFY_STALE_ALERT_MIN", 60)


def _stale_summary_alert_interval_h() -> int:
    """Minimum hours between stale-summary operator alerts for one agent."""
    return env_int("FULCRA_COORD_NOTIFY_STALE_ALERT_INTERVAL_H", 6)


def _stale_summary_alert_marker_path(agent: str):
    return cache.cache_root() / (
        f"stale-summaries-alert-{listener.agent_slug(agent)}")


def _alert_stale_summaries_if_needed(agent: str, stale_min: float) -> None:
    """Best-effort operator alert when listener-bounded mode may be deaf.

    This is intentionally alert-only: no direct-listing fallback, no task-body
    fan-out. It makes the degraded mode visible through the same notification
    path the listener normally uses, throttled per agent on this host.
    """
    try:
        threshold = _stale_summary_alert_threshold_min()
        if threshold <= 0:
            return
        if stale_min < threshold:
            return
        marker = _stale_summary_alert_marker_path(agent)
        interval_h = max(0, _stale_summary_alert_interval_h())
        try:
            age_h = (time.time() - marker.stat().st_mtime) / 3600.0
            if interval_h and age_h < interval_h:
                return
        except OSError:
            pass
        listener.emit_message(
            f"coord summaries view is {int(stale_min)}m stale on this host; "
            "listener ticks are bounded and may miss newly-directed work until "
            "reconcile repairs views",
            title="fulcra-coord degraded",
        )
        cache.cache_root().mkdir(parents=True, exist_ok=True)
        marker.write_text(str(int(stale_min)))
    except Exception:
        pass


def cmd_notify_inbox(args: Any, backend: Optional[list[str]] = None) -> int:
    """Poll the inbox for an agent; on non-empty, surface + notify (Part 3).

    The single call the scheduled listener (launchd/cron/heartbeat/scheduled
    remote agent) runs each tick. Notify-only: it writes the open directives to
    a local surface file the next SessionStart injects AND emits a best-effort
    desktop notification. No-op (no notification; surface file cleared to an
    empty inbox so a stale one doesn't linger) when the inbox is empty.
    Fail-safe — never raises out; a polling tick must not crash the scheduler.
    """
    me = getattr(args, "agent", None) or _derive_agent()
    try:
        try:
            from . import presence
            presence.touch_presence(me, backend=backend)
        except Exception:
            pass
        # ONE summaries load per tick, shared by the inbox fold AND the
        # needs-me pass below (perf loop-2 #2 — each used to pay its own
        # download; under the stale-view guard each re-ran the whole
        # direct-listing fallback, ~one spawn per task on the bus).
        summaries = _load_task_summaries(
            backend=backend,
            skip_stale_fallback=not _notify_stale_summary_fallback_enabled(),
            on_stale_skipped=lambda stale_min:
                _alert_stale_summaries_if_needed(me, stale_min),
        )
        items = _load_inbox(me, backend=backend, summaries=summaries)
        surface = _inbox_surface_path(me)
        cache.cache_root().mkdir(parents=True, exist_ok=True)
        payload = {"agent": me, "count": len(items), "inbox": items}
        surface.write_text(json.dumps(payload, indent=2))
        # Notify ONLY on NEW inbox items, not every tick. Mirrors the human
        # blocked-on-you seen-set: load the ids we've already pinged about, alert
        # only when something new arrived, then persist the CURRENT ids (so a
        # resolved/acked directive drops out and a re-arrival re-alerts). The
        # surface-file write above is Tier 0 (unconditional, guaranteed delivery)
        # and is intentionally left untouched by this dedup.
        seen_path = _inbox_notified_seen_path(me)
        seen: set[str] = set()
        if seen_path.exists():
            try:
                seen = set(json.loads(seen_path.read_text()))
            except (json.JSONDecodeError, OSError, TypeError):
                seen = set()
        current_ids = {i["id"] for i in items}
        new_ids = current_ids - seen
        if new_ids:
            # Loop signal rides the same notification (spec 2026-06-09 Task 7):
            # count semantics stay untouched; the suffix is best-effort and ""
            # whenever the loop scan fails or finds nothing overdue. The scan is
            # opt-in because listener ticks must not block future launchd fires.
            extra = (_overdue_loop_suffix(me, backend=backend)
                     if _notify_overdue_loop_suffix_enabled() else "")
            listener.emit_notification(
                me, len(new_ids), extra=extra)
        cache.cache_root().mkdir(parents=True, exist_ok=True)
        seen_path.write_text(json.dumps(sorted(current_ids)))
        # HOST WAKE (operator directive 2026-06-10: "this can't die if i do
        # other stuff for a bit"): with pending work and a per-adopter wake.json
        # entry for this agent, spawn the configured command detached — the
        # listener can now WAKE an agent runtime, not just notify. Keyed off the
        # TOTAL pending count (not the new-ids dedup above) on purpose: an
        # already-notified directive that is still sitting unprocessed is
        # exactly the case a wake exists for; wake.maybe_wake's own
        # min-interval throttle + single-flight pidfile prevent spam. Fail-safe
        # (never raises) by contract — no config means exactly the old behavior.
        woke = wake.maybe_wake(me, len(items))
        print(
            f"[fulcra-coord] notify-inbox: agent={me} pending={len(items)} "
            f"new={len(new_ids)} surface={surface} wake={'spawned' if woke else 'no'}",
            file=sys.stderr,
        )
        # ALSO notice anything newly blocked on the human (Part 5). Independent
        # of the agent's own inbox: a tick with an empty inbox can still alert on
        # a new blocked-on-you item. Best-effort within the same fail-safe guard.
        _notify_new_needs_me(backend=backend, summaries=summaries)
        # VERSION SELF-INCORPORATION (operator directive 2026-06-10): the
        # durable listener is the call site that keeps an OPERATOR-ABSENT host
        # current — exactly the host that would otherwise freeze on an old
        # build until someone manually woke it. Throttled (default one check
        # per 6h, FULCRA_COORD_SELF_UPDATE_INTERVAL_H) because ticks run every
        # few minutes and the manifest is one remote read; the callee itself
        # never raises (fail-safe contract), and runs LAST so an update can
        # never delay this tick's notify/wake delivery.
        selfupdate.maybe_self_update(backend=backend, throttle=True)
    except Exception as e:
        # A polling tick that fails must not bring down the scheduler; report to
        # stderr and exit clean (fail-safe contract).
        _warn(f"notify-inbox failed (non-fatal): {e}")
        return 0
    return 0
