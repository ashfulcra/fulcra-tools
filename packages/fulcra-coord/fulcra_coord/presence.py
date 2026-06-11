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
from . import role_ops as _role_ops
from . import continuity_ops as _continuity_ops
from . import selfupdate as _selfupdate
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


def _load_presence_agents(backend: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """Staleness-guarded read of the presence roster (``views/presence.json``).

    THE single roster read for liveness-sensitive consumers (`presence`,
    review routing, capability routing). The aggregate refreshes only when a
    connect/reconcile successfully uploads it; under backend write-throttling
    it lags the durable per-agent records by hours, so the stored ``last_seen``
    under-reports liveness — the 2026-06-10 failure where ``request-review``
    said "no reviewer live" while the reviewer's own ``presence/<slug>.json``
    was fresh. When the aggregate's ``generated_at`` is older than
    ``FULCRA_COORD_VIEW_STALE_MIN`` (views.view_staleness_minutes), list the
    per-agent ``presence/*.json`` files directly — the same authoritative
    enumeration ``_reconcile_presence`` rebuilds from — and use those.

    Returns RAW agent records: entries from the aggregate may carry a stored
    ``liveness`` annotation, listed per-agent records carry none. That is fine
    for every consumer — the routing resolver recomputes liveness from
    ``last_seen`` (it never trusts the stored tier), and ``cmd_presence`` strips
    and re-derives it. Degradation order: no aggregate at all → ``[]`` (today's
    behavior); stale aggregate + working listing → fresh per-agent records;
    stale aggregate + failed/empty listing → the stale aggregate with a louder
    warn (degraded, never blind)."""
    agg = remote.download_json(remote.presence_view_path(), backend=backend)
    if not agg:
        _warn("presence aggregate is missing — reading per-agent presence "
              "records directly")
        try:
            records = [
                rec for _, rec in remote.list_json(
                    remote.presence_prefix(), backend=backend)
                if rec.get("agent")
            ]
        except Exception:
            records = []
        if records:
            return records
        _warn("presence aggregate is missing AND the per-agent listing failed "
              "or was empty — treating presence as unavailable")
        return []
    agents = list(agg.get("agents", []) or [])
    stale_min = views.view_staleness_minutes(agg)
    if stale_min is None:
        return agents
    _warn(f"presence aggregate is {int(stale_min)}m stale — "
          "reading per-agent presence records directly")
    try:
        records = [
            rec for _, rec in remote.list_json(
                remote.presence_prefix(), backend=backend)
            if rec.get("agent")
        ]
    except Exception:
        records = []
    if records:
        return records
    _warn(f"presence aggregate is {int(stale_min)}m stale AND the per-agent "
          "listing failed — using the stale roster (liveness may under-report)")
    return agents


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


def _read_own_capabilities(
    me: str, backend: Optional[list[str]] = None) -> list[str]:
    """This agent's currently declared capabilities, best-effort ([] on any
    read failure). The shared read half of the C4/C5 merge-safe capability
    RMW — connect and the add/remove helpers below all start from here so
    "what do I already declare" has exactly one definition."""
    try:
        rec = _load_own_presence(me, backend=backend) or {}
        return [c for c in (rec.get("capabilities") or []) if c]
    except Exception:
        return []


def _rewrite_own_capabilities(
    me: str, capabilities: list[str],
    backend: Optional[list[str]] = None) -> bool:
    """Rewrite this agent's presence record with new capabilities, preserving
    workstreams/summary/session (the cmd_workstream preserve pattern). The
    write half shared by add_capabilities/remove_capability."""
    try:
        current = _load_own_presence(me, backend=backend) or {}
        record = schema.make_presence(
            me,
            workstreams=list(current.get("workstreams") or []),
            summary=current.get("summary", "") or "",
            session=current.get("session"),
            capabilities=capabilities,
        )
        return _write_presence(record, backend=backend)
    except Exception:
        return False


def add_capabilities(
    me: str, roles: list[str], backend: Optional[list[str]] = None) -> bool:
    """UNION ``roles`` into this agent's presence capabilities (merge-safe RMW).

    2026-06-11 bug hunt C5 (with C4): ``roles claim`` wrote only the lease
    shard while @role inbox delivery reads only presence capabilities
    (inbox._my_roles) — split brain: the board said HELD, the directives
    never arrived. The claim surface calls this so the two truths converge;
    a no-op when every role is already declared (skips the write). Built on
    the C4 merge discipline, so it can never wipe sibling declarations."""
    try:
        wanted = {r for r in (roles or []) if r}
        if not wanted:
            return True
        existing = set(_read_own_capabilities(me, backend=backend))
        if wanted <= existing:
            return True   # already declared — no write needed
        return _rewrite_own_capabilities(
            me, sorted(existing | wanted), backend=backend)
    except Exception:
        return False


def remove_capability(
    me: str, role: str, backend: Optional[list[str]] = None) -> bool:
    """Remove ONE role from this agent's presence capabilities (the release
    half of C5). Deliberately simple: a release drops the capability even if
    other machinery might still reference it — the operator released the
    role, so @role delivery to this agent must stop. Siblings survive (RMW
    union discipline, never a wholesale rebuild from flags)."""
    try:
        existing = _read_own_capabilities(me, backend=backend)
        if role not in existing:
            return True   # nothing to remove
        return _rewrite_own_capabilities(
            me, sorted(c for c in existing if c != role), backend=backend)
    except Exception:
        return False


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

    # RMW-class instance #5 (2026-06-11 bug hunt C4, P1): make_presence below
    # rebuilds the WHOLE record, so a bare `connect` — exactly what the
    # shipped SessionStart hook runs — used to stamp capabilities=[] over
    # whatever a previous `connect --role X` declared. That silently dropped
    # the agent from reviewer routing AND from @role inbox delivery
    # (inbox._my_roles reads these capabilities). Read-modify-write instead:
    # UNION the flags with the existing record's capabilities; dropping a
    # declaration is now an EXPLICIT act (--clear-roles), never a side effect
    # of reconnecting. Best-effort read: if the own-presence read fails the
    # union degrades to just the flags — the same exposure every presence
    # write already has when the bus is down (and a lost union heals on the
    # next flagged connect).
    if getattr(args, "clear_roles", False):
        roles = sorted(set(roles))   # explicit drop of prior declarations
    else:
        # Shared read half with the C5 add/remove capability helpers below.
        roles = sorted(set(_read_own_capabilities(me, backend=backend))
                       | set(roles))

    # Staleness suffix from the PERSISTED marker (2026-06-11 bug hunt S2):
    # read BEFORE the presence write so a host already known-behind renders
    # '(vX behind canonical Y)' on the roster THIS connect. A marker written
    # by this connect's own update attempt (below, AFTER the presence write)
    # rides the FOLLOWING connect/heartbeat — the marker file persists state
    # across invocations precisely so the suffix never needs to block boot.
    try:
        stale = _selfupdate.stale_summary_suffix()
        if stale:
            summary = f"{summary} {stale}".strip()
    except Exception:
        pass

    record = schema.make_presence(me, workstreams=workstreams, summary=summary,
                                  capabilities=roles or None,
                                  session=os.environ.get("FULCRA_COORD_SESSION") or None)
    _write_presence(record, backend=backend)

    # Roles-as-durable-identity (spec 2026-06-10): each declared role is ALSO
    # a lease CLAIM on that role — additive on top of the capabilities field
    # (which routing keeps reading unchanged). The lease shard is what makes
    # the role read HELD on the board/health; its freshness then rides this
    # very presence record's heartbeat, so no extra keep-alive is ever needed.
    # Per-claim best-effort: a lease failure (or even a raising role_ops) must
    # never fail the session boot, mirroring _write_presence's contract.
    for role_name in record["capabilities"]:
        try:
            _role_ops.claim_role(role_name, me, backend=backend)
            # Role claim → resume (continuity spec 2026-06-10): connect IS the
            # spawn-session → claim-role moment of the ArcBot backbone, so a
            # role that carries a checkpoint_ref prints its where-it-left-off
            # (ref + best-effort brief) here too — same helper as `roles
            # claim`, so the two lease paths can't diverge. Inside the same
            # per-claim guard: a resume problem never fails a session boot.
            _continuity_ops.print_role_resume(role_name, backend=backend)
        except Exception:
            pass

    # VERSION SELF-INCORPORATION (operator directive 2026-06-10: "i'm not
    # going to go around and wake the entire fleet for each incremental
    # upgrade"): check the bus version pointer and update if behind.
    # UNthrottled on the manifest CHECK — a fresh session must never boot
    # stale because a tick checked recently (attempts toward a canonical a
    # recent try failed to reach ARE throttled — selfupdate S1 (c)). Runs
    # AFTER the presence write (2026-06-11 bug hunt S2): the update step can
    # legitimately take minutes (git pull + a cold uv build, bounded 300s),
    # and running it first left the booting session INVISIBLE on the roster
    # for that whole window. Presence is the boot-critical write; any stale
    # marker this attempt leaves surfaces as the summary suffix on the NEXT
    # connect/heartbeat (read above). If an update ran it takes effect next
    # invocation (no re-exec). Doubly guarded: the callee never raises, and
    # this block must never fail a session boot.
    try:
        _selfupdate.maybe_self_update(backend=backend)
    except Exception:
        pass

    # Self-healing listener re-arm (spec 2026-06-09): connect runs on every
    # session start, so this is the idempotent "heal a dead listener" hook.
    # Doubly guarded (ensure_listener itself never raises) and BOUNDED — its
    # only subprocess probe carries timeout=5 — because connect runs in
    # SessionStart hooks fleet-wide and must never hang or fail a session boot.
    # Opt-out: FULCRA_COORD_ENSURE_LISTENER=0. Lazy import keeps the presence
    # module's load graph flat.
    try:
        from . import listener
        listener.ensure_listener(agent=me)
    except Exception:
        pass

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
    record = schema.make_presence(
        me, workstreams=new_workstreams, summary=new_summary,
        session=(current or {}).get("session"),
        capabilities=(current or {}).get("capabilities"),
    )
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
    # Staleness-guarded roster read (falls back to per-agent records when the
    # aggregate has gone stale under backend throttling — see the loader).
    agents = _load_presence_agents(backend=backend)
    # Re-derive liveness at read time so the age reflects NOW, not the moment the
    # aggregate was last written (the stored liveness can have drifted to stale).
    records = [
        {k: v for k, v in a.items() if k != "liveness"}
        for a in agents
    ]
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
