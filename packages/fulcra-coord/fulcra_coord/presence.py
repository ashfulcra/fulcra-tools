"""Agent-presence subsystem for fulcra-coord.

The presence half of the coordination store: each agent writes a durable
per-agent ``presence/<slug>.json`` (who it is, what workstreams it is on,
liveness), the ``connect`` / ``workstream`` / ``presence`` commands that
read/mutate it, the opportunistic aggregate upsert into ``views/presence.json``,
and the reconcile-time rebuild of that aggregate from the authoritative per-agent
files. This is the surface that answers "what is every agent working on right now"
even for agents with no active task — the operator's situational-awareness north
star.

Extracted from cli.py behind stable re-exports; depends only on lower layers
(cache / remote / schema / views / identity, the io loader, and the output /
textfmt leaf utils) and never imports cli, so the split introduces no cycle.
``_maybe_warn_legacy_identity`` lives here because ``connect`` is its primary
caller (the onboarding nudge fires on connect); cli re-exports it for ``start``.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from . import cache, remote, schema, views, identity
from .io import _load_task_summaries
from .output import info as _info, print_json as _print_json, warn as _warn, err as _err
from .textfmt import age_str as _age_str


def _maybe_warn_legacy_identity(explicit: Optional[str]) -> None:
    """Print a one-line migration hint (to STDERR) iff the resolved identity is
    purely DERIVED (nothing explicit/env/per-cwd) AND a legacy global
    ``identity.json`` exists.

    Rationale: an operator who set the pre-split global identity gets a derived
    id in every repo now (the global is no longer resolved automatically, I-1),
    so their agent shows up under an unexpected ``claude-code:<host>:<repo>``.
    This nudges them to declare a per-cwd id. STDERR so the backgrounded
    ``connect`` hook discards it; one line; only when BOTH conditions hold so it
    never nags a correctly-configured session."""
    _agent, source = identity.resolve_agent_source(explicit)
    if source != "derived":
        return
    legacy = identity.read_legacy_identity()
    if not legacy:
        return
    _warn("legacy identity.json found but per-cwd identity isn't set here — run "
          "'fulcra-coord identity migrate' (or 'identity set <vendor>:<host>:<purpose>').")


def _derive_workstreams_from_open_tasks(
    me: str, backend: Optional[list[str]] = None) -> list[str]:
    """The distinct ``workstream`` of this agent's OPEN tasks (proposed/active/
    waiting/blocked) that it OWNS.

    Read via the summaries fast-path (one download), so deriving presence
    workstreams costs the same single round-trip the other read commands pay —
    no per-task body fetch. Best-effort: any failure yields an empty list rather
    than raising into the connect path."""
    open_statuses = ("proposed", "active", "waiting", "blocked")
    try:
        summaries = _load_task_summaries(backend=backend)
    except Exception:
        return []
    return sorted({
        t.get("workstream") for t in summaries
        if t.get("owner_agent") == me
        and t.get("status") in open_statuses
        and t.get("workstream")
    })


def _upsert_presence_aggregate(
    record: dict[str, Any], backend: Optional[list[str]] = None) -> None:
    """Opportunistically merge this agent's presence record into the aggregate
    roster (``views/presence.json``) so ``presence`` / ``agents`` see it
    immediately, without waiting for a reconcile.

    BUG 4 (S2-class self-heal): the per-agent ``presence/<slug>.json`` files are
    the durable, un-clobberable truth — each owned by one agent. ``_write_presence``
    has already uploaded THIS agent's durable file before calling here, so we
    rebuild the aggregate by LISTING ``presence/*.json`` (the same authoritative
    enumeration ``_reconcile_presence`` uses) and upserting self on top. This
    recovers any peer that a concurrent last-writer-wins upload dropped from the
    aggregate — on the very next connect, instead of leaving it invisible until a
    90s reconcile (the task views already self-heal this way).

    FALLBACK: if the listing fails or is empty (a backend without a working
    ``list``), fall back to the old download-aggregate + upsert-self path so we
    never regress below the prior single-file behaviour. Whole thing is
    BEST-EFFORT: the durable per-agent record is already written and reconcile is
    the eventual-consistency backstop, so a transient aggregate write must never
    surface as an error to connect."""
    def _without_liveness(a: dict[str, Any]) -> dict[str, Any]:
        # build_presence re-derives liveness; strip any stale annotation so the
        # rebuilt entry carries a fresh one alongside the others.
        return {k: v for k, v in a.items() if k != "liveness"}

    try:
        records: list[dict[str, Any]] = []
        try:
            for _, rec in remote.list_json(remote.presence_prefix(), backend=backend):
                if rec.get("agent") and rec.get("agent") != record["agent"]:
                    records.append(_without_liveness(rec))
        except Exception:
            records = []  # listing best-effort; fall through to the download path

        if not records:
            # Fallback: no usable listing → recover peers from the current
            # aggregate instead, so behaviour never regresses below pre-BUG-4.
            agg = remote.download_json(remote.presence_view_path(), backend=backend)
            existing = (agg or {}).get("agents", []) if agg else []
            records = [
                _without_liveness(a)
                for a in existing if a.get("agent") != record["agent"]
            ]

        records.append(record)
        view = views.build_presence(records)
        remote.upload_json(view, remote.presence_view_path(), backend=backend)
        cache.write_cached_view("presence", view)
    except Exception:
        pass  # aggregate is opportunistic; reconcile heals it


def _write_presence(
    record: dict[str, Any], backend: Optional[list[str]] = None) -> bool:
    """Write a presence record to its per-agent file + upsert the aggregate.

    Returns True when the durable per-agent record uploaded. The aggregate upsert
    is best-effort on top. Whole thing is guarded so a presence write can never
    raise into a caller (mirrors _stamp_session_pointer's contract)."""
    try:
        slug = views.agent_slug(record["agent"])
        ok = remote.upload_json(
            record, remote.presence_remote_path(slug), backend=backend)
        _upsert_presence_aggregate(record, backend=backend)
        return ok
    except Exception:
        return False


def _load_own_presence(
    me: str, backend: Optional[list[str]] = None) -> Optional[dict[str, Any]]:
    """Download this agent's own presence record (``presence/<slug>.json``), or
    None if it has never connected. Used by `workstream` to mutate the existing
    record rather than clobber declared streams/summary."""
    slug = views.agent_slug(me)
    return remote.download_json(remote.presence_remote_path(slug), backend=backend)


def cmd_connect(args: Any, backend: Optional[list[str]] = None) -> int:
    """Record this agent's presence on connect (workstream-on-connect).

    The SessionStart/Codex hooks call this so the human sees what each agent is
    working on even when it owns no active task — the north star. Workstreams are
    the UNION of explicit ``--workstream`` values and the distinct ``workstream``
    of this agent's open tasks, so the common case needs no extra typing. Writes
    the durable per-agent record and opportunistically refreshes the aggregate.
    Best-effort: a presence write never fails the session boot."""
    explicit_agent = getattr(args, "agent", None)
    me = identity.resolve_agent(explicit_agent)
    out_format = getattr(args, "format", "table")
    summary = getattr(args, "summary", "") or ""

    # Non-blocking onboarding nudge (Task C): a derived identity + a lingering
    # legacy global identity.json means the operator's old declared id is being
    # silently ignored here. Hint to migrate. STDERR so the backgrounded connect
    # hook discards it (it only matters in an interactive run).
    _maybe_warn_legacy_identity(explicit_agent)

    explicit = _split_workstreams(getattr(args, "workstream", None))
    derived = _derive_workstreams_from_open_tasks(me, backend=backend)
    workstreams = sorted(set(explicit) | set(derived))

    # Declared capabilities (Task 2): --can-review is sugar for --role review.
    # These drive liveness-aware reviewer routing's candidate pool. Undeclared
    # agents stay [] (backward compatible).
    roles = list(getattr(args, "role", None) or [])
    if getattr(args, "can_review", False):
        roles.append("review")
    if not roles:
        existing = _load_own_presence(me, backend=backend)
        if isinstance(existing, dict):
            roles = list(existing.get("capabilities") or [])
    record = schema.make_presence(me, workstreams=workstreams, summary=summary,
                                  capabilities=roles or None,
                                  session=os.environ.get("FULCRA_COORD_SESSION") or None)
    _write_presence(record, backend=backend)

    if out_format == "json":
        _print_json(record)
        return 0
    ws = ", ".join(record["workstreams"]) or "(none)"
    _info(f"Connected: {me} — workstreams: {ws}")
    if summary:
        _info(f"  on: {summary}")
    return 0


def _split_workstreams(raw: Optional[str]) -> list[str]:
    """Split a comma-separated ``--workstream`` value into a clean list. Empty
    tokens (e.g. a trailing comma) are dropped; make_presence normalizes the rest."""
    if not raw:
        return []
    return [w.strip() for w in raw.split(",") if w.strip()]


def cmd_workstream(args: Any, backend: Optional[list[str]] = None) -> int:
    """Declare/update THIS agent's presence workstreams (manual path).

    Subcommands mutate the agent's own presence record:
      * ``set <ws>[,…]`` — REPLACE the workstream list.
      * ``add <ws>``     — APPEND to the existing list.
      * ``clear``        — empty the list.
    A bare ``workstream`` (no subcommand) just SHOWS the current presence. A
    ``--summary`` updates the one-line "what I'm on" on any mutating action.
    Reads the agent's own ``presence/<slug>.json``, mutates, and rewrites +
    upserts the aggregate (same writer as connect → no contention)."""
    me = identity.resolve_agent(getattr(args, "agent", None))
    out_format = getattr(args, "format", "table")
    action = getattr(args, "ws_action", None)
    summary_arg = getattr(args, "summary", None)

    current = _load_own_presence(me, backend=backend)
    cur_workstreams = list((current or {}).get("workstreams", []))
    cur_summary = (current or {}).get("summary", "")

    if action is None:
        # Show current presence (no mutation).
        rec = current or schema.make_presence(me, workstreams=[], summary="")
        if out_format == "json":
            _print_json(rec)
            return 0
        ws = ", ".join(rec.get("workstreams", [])) or "(none)"
        _info(f"{me} — workstreams: {ws}")
        if rec.get("summary"):
            _info(f"  on: {rec['summary']}")
        return 0

    if action == "set":
        new_workstreams = _split_workstreams(getattr(args, "workstreams", None))
    elif action == "add":
        new_workstreams = cur_workstreams + _split_workstreams(
            getattr(args, "workstreams", None))
    elif action == "clear":
        new_workstreams = []
    else:
        _err(f"Unknown workstream action: {action}")
        return 1

    new_summary = summary_arg if summary_arg is not None else cur_summary
    record = schema.make_presence(me, workstreams=new_workstreams,
                                  summary=new_summary,
                                  session=(current or {}).get("session"))
    _write_presence(record, backend=backend)

    if out_format == "json":
        _print_json(record)
        return 0
    ws = ", ".join(record["workstreams"]) or "(none)"
    _info(f"Workstreams for {me}: {ws}")
    return 0


def cmd_presence(args: Any, backend: Optional[list[str]] = None) -> int:
    """Show the agent presence roster — who is working on what, right now.

    Reads the aggregate ``views/presence.json`` (one download) and renders, per
    agent: workstreams · summary · last-seen age · liveness. This is the surface
    that answers "what is every agent on" even for agents with no active task.
    Empty/missing roster → a clear "nothing recorded yet" message."""
    out_format = getattr(args, "format", "table")
    agg = remote.download_json(remote.presence_view_path(), backend=backend)
    # Re-derive liveness at read time so the age reflects NOW, not the moment the
    # aggregate was last written (the stored liveness can have drifted to stale).
    records = [
        {k: v for k, v in a.items() if k != "liveness"}
        for a in (agg or {}).get("agents", [])
    ] if agg else []
    view = views.build_presence(records)

    if out_format == "json":
        _print_json(view)
        return 0

    if not view["agents"]:
        _info("No agent presence recorded yet.")
        return 0

    print(f"\n{'='*60}")
    print("  Fulcra Coordination — Presence")
    print(f"{'='*60}")
    for a in view["agents"]:
        # A3: build_presence carries records through verbatim and never injects
        # an "agent" key, so an imperfect aggregate entry missing "agent" (or
        # "liveness") must not KeyError-crash the whole roster. Tolerate the gap.
        ws = ", ".join(a.get("workstreams", [])) or "(none)"
        age = _age_str(a.get("last_seen", ""))
        print(f"\n  {a.get('agent', '')}  [{a.get('liveness', '')}]  (seen {age})")
        print(f"    workstreams: {ws}")
        if a.get("summary"):
            print(f"    on: {a['summary'][:80]}")
    print()
    return 0


def _reconcile_presence(backend: Optional[list[str]] = None) -> None:
    """Rebuild ``views/presence.json`` from the durable ``presence/*.json`` files.

    Lists ``<root>/presence/`` and downloads each per-agent record in parallel
    (remote.list_json), then rebuilds the aggregate roster — the presence analogue
    of the task view self-heal. This is what makes the opportunistic connect-time
    aggregate merge eventually-consistent: even if a connect's best-effort upsert
    was lost, reconcile reconstructs the roster from the authoritative per-agent
    records.

    LISTING REQUIREMENT: relies on remote.list_json being able to enumerate the
    presence dir. If listing returns nothing (empty dir, or a backend without a
    working list), no aggregate is written — the existing one is left intact
    rather than clobbered to empty. Best-effort: never raises into reconcile."""
    try:
        records = [
            rec for _, rec in remote.list_json(remote.presence_prefix(), backend=backend)
            if rec.get("agent")
        ]
        if not records:
            return
        view = views.build_presence(records)
        remote.upload_json(view, remote.presence_view_path(), backend=backend)
        cache.write_cached_view("presence", view)
    except Exception:
        pass  # presence rebuild is best-effort; task-view reconcile is the contract
