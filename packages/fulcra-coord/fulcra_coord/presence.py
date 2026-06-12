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
import re
from typing import Any, Optional

from . import cache, remote, schema, views, identity
from . import role_ops as _role_ops
from . import continuity_ops as _continuity_ops
from . import selfupdate as _selfupdate
from .io import _load_task_summaries
from .output import info as _info, print_json as _print_json, warn as _warn, err as _err
from .textfmt import age_str as _age_str


class _PresenceReadError:
    """Sentinel type for :data:`PRESENCE_READ_ERROR` — see _load_own_presence
    and _reconcile_presence."""

    def __repr__(self) -> str:  # diagnosable in test failures / debug prints
        return "<presence READ_ERROR>"


#: 2026-06-11 roles/presence read-error audit (F5/F8): the "this presence read
#: FAILED" sentinel, distinct from None ("confirmed absent / nothing usable").
#: The role_ops.READ_ERROR idiom (bug hunt C1) applied to the presence layer:
#: _load_own_presence used to collapse a failed read of the agent's OWN record
#: into None ("never connected"), and the whole-record rewrites downstream
#: (connect, workstream set/add, the capability RMW) then wiped
#: capabilities/workstreams/summary/session off the bus. _reconcile_presence
#: returns it when NEITHER the per-agent enumeration NOR the previous
#: aggregate could be read — the tick's consumers must treat presence as
#: unknown and take no routing action. Callers must treat it as "do not write
#: / do not decide", never as absence.
PRESENCE_READ_ERROR = _PresenceReadError()


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

    FALLBACK LADDER (tightened by the 2026-06-11 read-error audit, F5): an
    enumeration that fails, is empty (a backend without a working ``list``),
    or is PARTIAL — the checked listing exposes per-record drops, and
    survivors of a partial read must never be uploaded as the roster, that is
    how a live peer vanished from presence — falls back to the previous
    aggregate's peers (stale-but-full). When THAT is also unreadable (or the
    bus unreachable), the upsert is skipped entirely: fail toward no-action.
    Only a probe-confirmed-fresh bus proceeds self-only. Whole thing is
    BEST-EFFORT: the durable per-agent record is already written and reconcile is
    the eventual-consistency backstop, so a transient aggregate write must never
    surface as an error to connect."""
    def _without_liveness(a: dict[str, Any]) -> dict[str, Any]:
        # build_presence re-derives liveness; strip any stale annotation so the
        # rebuilt entry carries a fresh one alongside the others.
        return {k: v for k, v in a.items() if k != "liveness"}

    try:
        records: list[dict[str, Any]] = []
        complete = False
        try:
            listed, complete = remote.list_json_checked(
                remote.presence_prefix(), backend=backend)
            for _, rec in listed:
                if rec.get("agent") and rec.get("agent") != record["agent"]:
                    records.append(_without_liveness(rec))
        except Exception:
            records, complete = [], False  # fall through to the download path

        if not complete:
            # 2026-06-11 read-error audit (F5): the enumeration was PARTIAL or
            # failed — a peer whose one record 504'd is missing from
            # ``records``, and uploading the survivors as the aggregate is
            # exactly how a LIVE reviewer vanished from the roster (and the
            # review sweep then escalated "no reviewer live" to the human
            # while the reviewer was up). Survivors of a partial read are
            # never the roster: discard them and recover peers from the
            # previous aggregate instead (stale-but-FULL beats fresh-but-
            # truncated; the durable per-agent records stay authoritative and
            # reconcile heals the view once reads recover).
            records = []

        if not records:
            # No usable listing (failed/partial — or genuinely no peers, in
            # which case the aggregate path is a harmless no-op source) →
            # recover peers from the current aggregate, so behaviour never
            # regresses below pre-BUG-4.
            agg = remote.download_json(remote.presence_view_path(), backend=backend)
            if isinstance(agg, dict):
                records = [
                    _without_liveness(a)
                    for a in agg.get("agents", []) or []
                    if a.get("agent") != record["agent"]
                ]
            elif not complete:
                # Boundary case (F5, documented policy: fail toward NO-ACTION):
                # the enumeration is untrustworthy AND the previous aggregate
                # would not read. Writing now could only shrink the roster to
                # self. Disambiguate absence per the C1 idiom: if the aggregate
                # demonstrably EXISTS (stat sees it) or the bus is unreachable,
                # skip the upsert entirely — the durable per-agent record above
                # already landed, so nothing is lost but an opportunistic
                # refresh. Only a probe-confirmed-absent aggregate on a
                # reachable bus (a genuinely fresh bus) may proceed self-only.
                if (remote.stat(remote.presence_view_path(),
                                backend=backend) is not None
                        or not remote.probe_reachable(backend)):
                    return

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
            listed, complete = remote.list_json_checked(
                remote.presence_prefix(), backend=backend)
            records = [rec for _, rec in listed if rec.get("agent")]
        except Exception:
            records, complete = [], False
        if records:
            if not complete:
                # F5-adjacent: with no aggregate to fall back to, a partial
                # listing is still the best roster available — but the reader
                # must know it may UNDER-report (a live agent whose one record
                # failed to read is missing). Loud, never blind.
                _warn("per-agent presence listing was PARTIAL (some records "
                      "failed to read) — the roster may be missing live agents")
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
        listed, complete = remote.list_json_checked(
            remote.presence_prefix(), backend=backend)
        records = [rec for _, rec in listed if rec.get("agent")]
    except Exception:
        records, complete = [], False
    # 2026-06-11 read-error audit (F5): only a COMPLETE per-agent read may
    # replace the aggregate. A partial listing here used to hand liveness-
    # sensitive consumers (review routing) a roster silently missing the very
    # agent whose record failed to read; stale-but-FULL beats fresh-but-
    # truncated, so an incomplete read degrades to the stale aggregate below.
    if records and complete:
        return records
    _warn(f"presence aggregate is {int(stale_min)}m stale AND the per-agent "
          "listing failed or was partial — using the stale roster (liveness "
          "may under-report)")
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
    me: str, backend: Optional[list[str]] = None) -> Any:
    """This agent's own presence record (``presence/<slug>.json``): the dict,
    None on CONFIRMED absence (genuinely never connected), or the
    :data:`PRESENCE_READ_ERROR` sentinel when the read failed. Used by
    `workstream`/`connect`/the capability RMW to mutate the existing record
    rather than clobber declared streams/summary.

    2026-06-11 read-error audit (F8): this used to return the transport's raw
    None for BOTH "no record" and "read failed", and every caller rebuilds the
    WHOLE record from that base — so one 504 on the agent's own read meant the
    next write wiped its capabilities/workstreams/summary/session off the bus.
    Same C1 discipline as role_ops.read_role, with the PR-#170 probe on top: a
    failed download is absence only when the stat probe ALSO misses AND
    ``probe_reachable`` confirms the bus was answering — an unreachable bus
    can confirm nothing. The probes are spent on the failure path only."""
    slug = views.agent_slug(me)
    path = remote.presence_remote_path(slug)
    try:
        rec = remote.download_json(path, backend=backend)
        if isinstance(rec, dict):
            return rec
        if remote.stat(path, backend=backend) is not None:
            return PRESENCE_READ_ERROR  # record exists but is unreadable now
        if not remote.probe_reachable(backend):
            return PRESENCE_READ_ERROR  # bus dark: absence is unconfirmable
        return None                     # confirmed absent: first connect
    except Exception:
        return PRESENCE_READ_ERROR


def touch_presence(me: str, backend: Optional[list[str]] = None) -> bool:
    """Refresh this agent's presence timestamp without changing declarations.

    Durable listeners run ``notify-inbox --agent X`` while the agent runtime is
    idle. Without this touch, a reviewer can have a healthy listener that sees
    direct work, yet age out of liveness-aware routing because its presence
    record is never refreshed. Preserve workstreams, summary, session, and
    capabilities; if the own-record read fails, skip the write rather than
    blindly shrinking those fields.
    """
    try:
        current = _load_own_presence(me, backend=backend)
        if current is PRESENCE_READ_ERROR:
            return False
        current = current or {}
        record = schema.make_presence(
            me,
            workstreams=list(current.get("workstreams") or []),
            summary=current.get("summary", "") or "",
            session=current.get("session"),
            capabilities=list(current.get("capabilities") or []),
        )
        return _write_presence(record, backend=backend)
    except Exception:
        return False


def _read_own_capabilities(
    me: str, backend: Optional[list[str]] = None) -> Any:
    """This agent's currently declared capabilities: a list, or
    :data:`PRESENCE_READ_ERROR` when the own-record read failed (F8 — the
    callers are all about to REWRITE the record, and a failed read collapsed
    to [] here is precisely the wiped-capabilities bug). Confirmed absence is
    simply []. The shared read half of the C4/C5 merge-safe capability RMW —
    connect and the add/remove helpers below all start from here so "what do
    I already declare" has exactly one definition."""
    try:
        rec = _load_own_presence(me, backend=backend)
        if rec is PRESENCE_READ_ERROR:
            return PRESENCE_READ_ERROR
        return [c for c in ((rec or {}).get("capabilities") or []) if c]
    except Exception:
        return PRESENCE_READ_ERROR


def _rewrite_own_capabilities(
    me: str, capabilities: list[str],
    backend: Optional[list[str]] = None) -> bool:
    """Rewrite this agent's presence record with new capabilities, preserving
    workstreams/summary/session (the cmd_workstream preserve pattern). The
    write half shared by add_capabilities/remove_capability.

    F8: "preserving" requires a READABLE base — when the own-record read
    fails, rebuilding from an empty dict would wipe every preserved field, so
    the rewrite refuses (False) and the caller's RMW aborts visibly."""
    try:
        current = _load_own_presence(me, backend=backend)
        if current is PRESENCE_READ_ERROR:
            _warn(f"presence: {me}'s own record could not be read — refusing "
                  "the capability rewrite (a blind rebuild would wipe "
                  "workstreams/summary/session)")
            return False
        current = current or {}
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
        current = _read_own_capabilities(me, backend=backend)
        if current is PRESENCE_READ_ERROR:
            # F8: the union base is unknowable — merging onto [] would write a
            # record that DROPS every undeclared-this-call capability (and the
            # rewrite would wipe workstreams/summary too). Abort; the caller
            # (`roles claim`) already warns-and-continues on False.
            return False
        existing = set(current)
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
        if existing is PRESENCE_READ_ERROR:
            # F8: same refusal as add_capabilities — "remove one" cannot be
            # computed from an unreadable base without wiping the siblings.
            return False
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
    # of reconnecting.
    #
    # 2026-06-11 read-error audit (F8): C4's comment admitted the residual
    # exposure — "if the own-presence read fails the union degrades to just
    # the flags", i.e. one 504 on the agent's own read still wiped its
    # declarations on the very next bare connect. The read now distinguishes
    # READ_ERROR from confirmed absence (the C1 probe idiom in
    # _load_own_presence); on READ_ERROR the presence WRITE below is skipped
    # entirely — connect cannot merge-preserve fields it cannot see, and a
    # missed heartbeat (healed by the next connect/tick) is strictly cheaper
    # than a wiped record (only healed by an operator noticing). An explicit
    # --clear-roles still writes: the operator sanctioned the rebuild.
    own_read_failed = False
    if getattr(args, "clear_roles", False):
        roles = sorted(set(roles))   # explicit drop of prior declarations
    else:
        # Shared read half with the C5 add/remove capability helpers below.
        existing = _read_own_capabilities(me, backend=backend)
        if existing is PRESENCE_READ_ERROR:
            own_read_failed = True
            roles = sorted(set(roles))   # flags only — used for lease claims
        else:
            roles = sorted(set(existing) | set(roles))

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
    if own_read_failed:
        # F8: the record demonstrably exists (or the bus is dark) but could not
        # be read — uploading the rebuilt record above would shrink it to what
        # THIS invocation happens to know. Skip the write, say so, and carry on
        # with the rest of boot (lease claims for the explicit --role flags are
        # clobber-free per-agent shards, so they still land below).
        _warn(f"connect: {me}'s presence record could not be read — presence "
              "write SKIPPED this boot (a blind rebuild would wipe declared "
              "capabilities/workstreams; heartbeat resumes next connect)")
    else:
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


# 2026-06-11 bug hunt S6: the stale-version suffix selfupdate/connect appends
# to the presence summary ('… (v0.15.2 behind canonical 0.16.0)'). Anchored to
# end-of-string so only the BAKED-IN trailing copy is ever stripped — never
# the operator's own words mid-summary.
_STALE_SUFFIX_RE = re.compile(r"\s*\(v\S+ behind canonical \S+\)\s*$")


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
    if current is PRESENCE_READ_ERROR:
        # F8: every mutating action below REBUILDS the whole record from this
        # read ("preserve" = copy from `current`), so acting on a failed read
        # used to write a record with empty workstreams/summary/capabilities —
        # `workstream add` could wipe everything. Nothing here is boot-critical
        # (unlike connect's heartbeat), so the honest move is to abort loudly;
        # the bare show degrades the same way rather than rendering a record we
        # know exists but cannot see.
        _err(f"workstream: {me}'s presence record could not be read — "
             "aborting (mutating it blind would wipe the existing "
             "workstreams/summary/capabilities); re-run when the bus answers")
        return 1
    cur_workstreams = list((current or {}).get("workstreams", []))
    # 2026-06-11 bug hunt S6: strip the trailing stale-version suffix from the
    # PRESERVED summary. Connect bakes '(vX behind canonical Y)' into the
    # stored summary text when the stale marker is set, and re-derives it
    # from the marker on every connect — but set/add/clear preserved the
    # stored text verbatim, so the suffix was carried forever (even after
    # the host updated). Stripping here is lossless: the suffix's single
    # source of truth is the marker, and connect re-appends it while the
    # host is genuinely behind.
    cur_summary = _STALE_SUFFIX_RE.sub(
        "", (current or {}).get("summary", "") or "")

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


def _reconcile_presence(
    backend: Optional[list[str]] = None,
) -> Optional[dict[str, Any]]:
    """Rebuild ``views/presence.json`` from the durable ``presence/*.json`` files.

    Lists ``<root>/presence/`` and downloads each per-agent record in parallel
    (remote.list_json), then rebuilds the aggregate roster — the presence analogue
    of the task view self-heal. This is what makes the opportunistic connect-time
    aggregate merge eventually-consistent: even if a connect's best-effort upsert
    was lost, reconcile reconstructs the roster from the authoritative per-agent
    records.

    Returns the REBUILT aggregate view on success, else None (E4 snapshot
    sharing): the rebuilt roster is the freshest presence truth this tick will
    see, so cmd_reconcile threads it through the reroute sweep / role health /
    undelivered checks instead of each one re-loading presence. Returned even
    when the aggregate UPLOAD failed — the view was still built from the
    authoritative per-agent records, so in-tick consumers can trust it.

    PARTIAL-READ POLICY (2026-06-11 read-error audit, F5): remote.list_json's
    per-item isolation silently DROPS a record whose individual download
    fails — and this function used to upload the SURVIVORS as the
    authoritative aggregate. One 504 on a live reviewer's record erased it
    from presence, and the truncated roster threaded into the review sweep +
    role health that same tick ("no reviewer live" escalated to the human
    while the reviewer was up — lived incident). The checked listing now
    exposes the drop, and a partial rebuild:

      * NEVER uploads (the previous aggregate, however stale, stays — full
        beats truncated; the per-agent records remain authoritative and the
        next clean tick heals the view);
      * hands the tick the PREVIOUS aggregate when it is readable (consumers
        decide on a full roster);
      * returns :data:`PRESENCE_READ_ERROR` when the previous aggregate is
        ALSO unreadable/absent — the boundary case. Policy: fail toward
        NO-ACTION; cmd_reconcile skips the route sweep and role-health
        vacancy judgment outright, because no trustworthy roster of any age
        exists this tick.

    LISTING REQUIREMENT: relies on the remote listing being able to enumerate
    the presence dir. If listing returns nothing (empty dir, or a backend
    without a working list), no aggregate is written — the existing one is
    left intact rather than clobbered to empty. Best-effort: never raises
    into reconcile."""
    try:
        listed, complete = remote.list_json_checked(
            remote.presence_prefix(), backend=backend)
        records = [rec for _, rec in listed if rec.get("agent")]
        if not complete:
            _warn("  presence rebuild: per-agent listing was PARTIAL — "
                  "aggregate upload skipped (uploading survivors would erase "
                  "live agents from the roster)")
            prev = remote.download_json(remote.presence_view_path(),
                                        backend=backend)
            if isinstance(prev, dict):
                return prev   # previous FULL aggregate: usable, just older
            return PRESENCE_READ_ERROR
        if not records:
            return None
        view = views.build_presence(records)
        remote.upload_json(view, remote.presence_view_path(), backend=backend)
        cache.write_cached_view("presence", view)
        return view
    except Exception:
        # presence rebuild is best-effort; task-view reconcile is the contract
        return None
