"""Inbox + blocked-on-you notification for fulcra-coord.

The agent's directive inbox (the ``inbox`` command + its load/ack), the listener's
pending-directive surface file, and the desktop-notification path for NEW
blocked-on-you items (notify once per item via a seen-set, never re-firing). Reads
the summaries aggregate and writes acks through the single write pipeline.

Extracted from cli.py behind stable re-exports; depends only on lower layers (io
loaders, the write pipeline, output, cache/remote/schema/views/identity/listener)
and never imports cli, so the split has no cycle. ``_inbox_surface_path`` is
re-exported because the reconcile-side ``_build_health_record`` (still in cli) reads
the listener's last-fire mtime from it; ``_derive_agent`` is the usual thin alias.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from . import cache, schema, views, identity, listener
from .io import _load_task, _load_task_summaries
from .output import info as _info, print_json as _print_json, warn as _warn, err as _err
from .writepipe import _write_task_and_views


def _derive_agent() -> str:
    return identity.resolve_agent()

def cmd_inbox(args: Any, backend: Optional[list[str]] = None) -> int:
    """List (or ack) open directives addressed to the calling agent.

    Read path recomputes authoritatively from the full task set (see
    _load_inbox) — mirroring cmd_agents — rather than trusting a materialized
    inbox view, which can go stale once an inbox empties. `--ack <id>` records an
    inbox_ack event so the listener stops re-notifying, without claiming the task.
    """
    me = getattr(args, "agent", None) or _derive_agent()
    out_format = getattr(args, "format", "table")
    ack_id = getattr(args, "ack", None)
    show_all = bool(getattr(args, "all", False))

    if ack_id:
        task = _load_task(ack_id, backend=backend)
        if task is None:
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
    items = views.inbox_for(me, all_tasks, now=now, include_aged=show_all)
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
    if hidden:
        print(f"\n  ({hidden} older broadcast{'s' if hidden != 1 else ''} "
              f"hidden — --all to show)")
    print()
    return 0

def _load_inbox(me: str, backend: Optional[list[str]] = None,
                include_aged: bool = False) -> list[dict[str, Any]]:
    """Open directives for `me`, recomputed authoritatively from the full task set.

    Mirrors cmd_agents: inbox_for over the live tasks is the single source of
    truth. We deliberately do NOT prefer a materialized inbox/<slug> view here.

    Membership uses prefix-aware matching (views.inbox_for / agent_matches): a
    directive addressed to a short id like `claude-code` reaches the full-id
    agent `claude-code:<host>:<repo>` it prefixes. This is the correctness fix
    for the original bug — strict slug equality silently dropped short-id
    directives.

    Why recompute (not read a materialized view): build_all_views only emits an
    inbox/<slug> view for assignees who still have at least one open directive.
    When an inbox empties — the last directive is acked or claimed — the stale
    inbox/<slug>.json (local cache AND remote) is never overwritten, so preferring
    it returned a phantom directive forever (`inbox` re-listed handled work, the
    listener re-notified, SessionStart re-injected). Recomputing from the task set
    always reflects the current truth, at the cost of one task-set load — the same
    cost cmd_agents pays.
    """
    # Summaries fast-path: inbox_for reads assignee/status/owner_agent and the
    # ack set, which the summary now carries (acked_by) — no event log / body
    # fetch needed. Falls back to a full load on an older bus.
    all_tasks = _load_task_summaries(backend=backend)
    # include_aged bypasses the broadcast age-out filter (the `inbox --all` path);
    # the default read hides stale informational broadcasts so they stop
    # cluttering the inbox / SessionStart, without touching any task.
    return views.inbox_for(me, all_tasks, include_aged=include_aged)

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

def _notify_new_needs_me(backend: Optional[list[str]] = None) -> None:
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
    items = views.needs_human(_load_task_summaries(backend=backend), human)
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
        items = _load_inbox(me, backend=backend)
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
            listener.emit_notification(me, len(new_ids))
        cache.cache_root().mkdir(parents=True, exist_ok=True)
        seen_path.write_text(json.dumps(sorted(current_ids)))
        # ALSO notice anything newly blocked on the human (Part 5). Independent
        # of the agent's own inbox: a tick with an empty inbox can still alert on
        # a new blocked-on-you item. Best-effort within the same fail-safe guard.
        _notify_new_needs_me(backend=backend)
    except Exception as e:
        # A polling tick that fails must not bring down the scheduler; report to
        # stderr and exit clean (fail-safe contract).
        _warn(f"notify-inbox failed (non-fatal): {e}")
        return 0
    return 0
