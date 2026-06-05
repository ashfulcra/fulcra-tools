"""View generation for fulcra-coord.

Generates materialized JSON views from a list of task dicts:
  - index.json               — global compact index with counts
  - views/active.json        — all active/waiting/blocked tasks
  - views/next.json          — proposed + waiting (candidates for starting)
  - views/recently-done.json — done/abandoned within retention window
  - views/search-index.json  — tag/title/summary records for search
  - workstreams/{ws}.json    — per-workstream active view
  - agents/{agent}.json      — per-agent active view
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import task_file_path
from .schema import task_summary

# Statuses at which a directive is "open" in someone's inbox: proposed (not yet
# acted on) or waiting (parked, still awaiting the assignee). active/blocked are
# in-flight work, terminal states are finished — none belong in an inbox.
INBOX_OPEN_STATUSES = ("proposed", "waiting")

# Wildcard assignee: a directive addressed to BROADCAST belongs in EVERY agent's
# inbox (feature #2 — all-agents broadcast). It is the durable "tell every agent
# X" primitive; per-agent acks (one inbox_ack event per `by`) let each agent
# clear it independently without removing it for the others. A real agent id can
# never be "*" (ids are kind:host:repo triples), so the sentinel can't collide.
BROADCAST = "*"

RECENTLY_DONE_DAYS = 7
SEARCH_INDEX_DONE_DAYS = 30
# Default age (days) after which a still-`proposed` BROADCAST directive drops out
# of the live inbox view. Broadcasts are informational fan-out ("X joined the
# mesh") — once a few days old they have served their purpose and only clutter
# every agent's inbox / SessionStart banner. Aging is a READ filter only: it
# never touches task status or the task file (a peer on an older CLI still sees
# it), and `inbox --all` bypasses it. CONCRETE-assignee directives (real asks)
# are NEVER aged out regardless of this threshold.
INBOX_AGE_DAYS_DEFAULT = 3
# Default staleness threshold (hours). An `active` task whose updated_at is older
# than this is "possibly forgotten" and surfaced in views/needs-attention.json.
STALE_HOURS_DEFAULT = 2


def _stale_hours(stale_hours: Optional[float] = None) -> float:
    """Resolve the staleness threshold: explicit arg > env > default.

    Centralizing this (Gap 2) means hooks, the status view, and the reconciler
    all agree on what "stale" means instead of recomputing it ad-hoc.
    """
    if stale_hours is not None:
        return stale_hours
    raw = os.environ.get("FULCRA_COORD_STALE_HOURS", "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return float(STALE_HOURS_DEFAULT)


# Wall-clock grace (seconds) the resolver tolerates BEYOND the idle->stale
# cutoff before treating an agent as below routing floor. A single missed
# heartbeat or a laptop sleep/wake must not drop a reviewer. Expressed as an
# ABSOLUTE duration (not a count of listener intervals) because listener
# cadence differs per machine, while presence last_seen is bus-global, so the
# grace evaluates identically on every machine (machine-agnostic invariant).
PRESENCE_GRACE_SECONDS_DEFAULT = 1200.0  # 20 min


def _presence_grace_seconds(grace: Optional[float] = None) -> float:
    """Resolve the routing presence grace (seconds): explicit arg > env > default."""
    if grace is not None:
        return grace
    raw = os.environ.get("FULCRA_COORD_PRESENCE_GRACE_SECONDS", "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return float(PRESENCE_GRACE_SECONDS_DEFAULT)


def _inbox_age_days(age_days: Optional[float] = None) -> float:
    """Resolve the broadcast inbox age cutoff (days): explicit arg > env > default.

    Mirrors _stale_hours so the knob is read in exactly one place. The env var
    FULCRA_COORD_INBOX_AGE_DAYS lets a fleet tune how long informational
    broadcasts linger; a non-numeric value falls back to the default rather than
    crashing a read path.
    """
    if age_days is not None:
        return age_days
    raw = os.environ.get("FULCRA_COORD_INBOX_AGE_DAYS", "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return float(INBOX_AGE_DAYS_DEFAULT)


def is_aged_out_broadcast(task: dict[str, Any], now: Optional[datetime] = None,
                          age_days: Optional[float] = None) -> bool:
    """True when `task` is a stale informational BROADCAST that should drop out of
    the default inbox view.

    A directive ages out ONLY when ALL of these hold:
      * assignee == BROADCAST ("*") — it is fan-out, not a personal ask. A
        directive addressed to a CONCRETE agent is a real ask and is NEVER aged
        out here, regardless of age (callers rely on this exact guarantee).
      * status == "proposed" — still un-acted-on informational noise. (A waiting
        broadcast was deliberately parked; we don't second-guess that.)
      * its updated_at is older than the age cutoff (now - age_days).

    Age is measured from `updated_at` vs `now`. A missing/unparseable timestamp
    ages to +inf (via _age_hours), so a clock-less old broadcast is treated as
    aged — the same fail-toward-cleanup choice is_stale makes. This is a pure
    predicate over a read filter: it never mutates the task and never changes its
    status, so the underlying task file/aggregate is untouched and a peer on an
    older CLI still sees the broadcast.
    """
    if task.get("assignee") != BROADCAST:
        return False
    if task.get("status") != "proposed":
        return False
    if now is None:
        now = _now()
    cutoff_hours = _inbox_age_days(age_days) * 24.0
    return _age_hours(task.get("updated_at", ""), now) >= cutoff_hours


def _age_hours(updated_at: str, now: datetime) -> float:
    """Age of a timestamp in hours. Missing/unparseable -> +inf (maximally
    stale): an active task that lost its clock is exactly the forgotten case we
    want to surface, never silently treat as fresh (consistent with the I4 fix)."""
    dt = _parse_dt(updated_at)
    if dt is None:
        return float("inf")
    return (now - dt).total_seconds() / 3600.0


def is_stale(task: dict[str, Any], now: Optional[datetime] = None,
             stale_hours: Optional[float] = None) -> bool:
    """True when an `active` task is older than the staleness threshold.

    Only `active` tasks can be stale — waiting/blocked are deliberately parked,
    and terminal tasks are finished, so neither is "forgotten work churning."
    """
    if task.get("status") != "active":
        return False
    if now is None:
        now = _now()
    return _age_hours(task.get("updated_at", ""), now) >= _stale_hours(stale_hours)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(iso: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp to a tz-AWARE UTC datetime, or None.

    BUG 8: a tz-less string parses to a NAIVE datetime; subtracting it from the
    aware ``now`` in ``_age_hours`` raised TypeError, breaking the +inf fail-safe
    that the liveness/aging docstrings promise (missing/unparseable -> +inf, i.e.
    maximally stale). Coerce a naive result to UTC here, in the shared helper, so
    every caller (_age_hours, presence_liveness, is_aged_out_broadcast, is_stale,
    inbox_for) is safe — matching how the schedule layer already assumes UTC."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _done_at(t: dict[str, Any]) -> str:
    """Resolve a task's done/abandoned timestamp from EITHER a full body (nested
    ``done.done_at``) OR a summary dict (flat ``done_at`` emitted by
    schema.task_summary), falling back to ``updated_at``.

    This is the linchpin of the summaries-as-view-source refactor: builders gate
    recently-done / search retention on this timestamp, and they must compute the
    SAME value whether handed full task bodies or task_summary dicts. Centralizing
    the lookup here (instead of the old inline ``t.get("done",{}).get("done_at")``,
    which returned None for a summary that has no nested ``done`` block) is what
    makes build_all_views(summaries) == build_all_views(full_bodies)."""
    return (t.get("done") or {}).get("done_at") or t.get("done_at") or t.get("updated_at", "")


def _acked_by(t: dict[str, Any], who: str) -> bool:
    """True when ``who`` has inbox-acked this task, resolved from EITHER a full
    body (an inbox_ack event whose ``by`` is who) OR a summary's flat ``acked_by``
    list (schema.task_summary). The inbox builders use this so they give the same
    answer whether handed full bodies or summaries — without it, a rebuilt inbox
    view (sourced from summaries, which carry no event log) would re-surface
    directives the assignee already acked."""
    if "acked_by" in t and "events" not in t:
        return who in (t.get("acked_by") or [])
    for e in t.get("events", []):
        if e.get("type") == "inbox_ack" and e.get("by") == who:
            return True
    return False


# ---------------------------------------------------------------------------
# Individual view builders
# ---------------------------------------------------------------------------

def build_index(tasks: list[dict[str, Any]], updated_at: Optional[str] = None) -> dict[str, Any]:
    """Global compact index with counts and active summaries."""
    if updated_at is None:
        updated_at = _now().isoformat(timespec="microseconds").replace("+00:00", "Z")

    counts_by_status: dict[str, int] = {}
    counts_by_workstream: dict[str, int] = {}
    counts_by_agent: dict[str, int] = {}
    active_summaries = []
    now = _now()
    sh = _stale_hours()

    for t in tasks:
        status = t.get("status", "unknown")
        ws = t.get("workstream", "unknown")
        agent = t.get("owner_agent", "unknown")

        counts_by_status[status] = counts_by_status.get(status, 0) + 1
        if status not in ("done", "abandoned"):
            counts_by_workstream[ws] = counts_by_workstream.get(ws, 0) + 1
            counts_by_agent[agent] = counts_by_agent.get(agent, 0) + 1

        if status in ("active", "waiting", "blocked"):
            # Stamp the stale flag (Gap 2) so the JSON status surface carries it.
            active_summaries.append(_summary_with_stale(t, now, sh))

    cutoff = _now() - timedelta(days=RECENTLY_DONE_DAYS)
    recent_done = []
    for t in tasks:
        if t.get("status") not in ("done", "abandoned"):
            continue
        done_at = _done_at(t)
        if done_at:
            dt = _parse_dt(done_at)
            if dt and dt >= cutoff:
                recent_done.append(task_summary(t))

    # Inbox counts per assignee-slug — a cheap fold so a session-start hook or
    # the agents digest can see "you have N directives" without loading every
    # inbox view. Computed from the same is_open_directive predicate.
    inbox = build_inbox(tasks)
    counts_by_inbox = {slug: len(items) for slug, items in inbox.items()}

    return {
        "schema": "fulcra.coordination.index.v1",
        "updated_at": updated_at,
        "counts": {
            "by_status": counts_by_status,
            "by_workstream": counts_by_workstream,
            "by_agent": counts_by_agent,
            "inbox": counts_by_inbox,
        },
        "active": active_summaries,
        "recent_done": recent_done,
    }


def _summary_with_stale(t: dict[str, Any], now: datetime,
                        stale_hours: float) -> dict[str, Any]:
    """task_summary() with a stale flag stamped on (Gap 2). The flag is the
    single source of truth so consumers read it instead of recomputing age."""
    s = task_summary(t)
    s["stale"] = is_stale(t, now, stale_hours)
    return s


def build_active(tasks: list[dict[str, Any]], updated_at: Optional[str] = None,
                 stale_hours: Optional[float] = None) -> dict[str, Any]:
    """All active, waiting, and blocked tasks. Active summaries carry a `stale`
    flag (Gap 2) so the status view can mark possibly-forgotten work."""
    if updated_at is None:
        updated_at = _now().isoformat(timespec="microseconds").replace("+00:00", "Z")
    now = _now()
    sh = _stale_hours(stale_hours)
    active = [
        _summary_with_stale(t, now, sh)
        for t in tasks if t.get("status") in ("active", "waiting", "blocked")
    ]
    return {
        "schema": "fulcra.coordination.view.v1",
        "view": "active",
        "updated_at": updated_at,
        "tasks": sorted(active, key=lambda x: (x.get("priority", "P9"), x.get("updated_at", ""))),
    }


def build_needs_attention(tasks: list[dict[str, Any]], updated_at: Optional[str] = None,
                          stale_hours: Optional[float] = None) -> dict[str, Any]:
    """Materialized view of `active` tasks that have gone stale (Gap 2).

    The safety-net surface: crashed agents (SessionEnd never fired) and ChatGPT
    (no end hook) leave active tasks dangling; this view collects them so the
    heartbeat reconciler / `agents` digest can flag them. Every entry carries
    stale=true (it would not be here otherwise) for symmetry with the active view.
    """
    if updated_at is None:
        updated_at = _now().isoformat(timespec="microseconds").replace("+00:00", "Z")
    now = _now()
    sh = _stale_hours(stale_hours)
    stale = []
    for t in tasks:
        if is_stale(t, now, sh):
            s = task_summary(t)
            s["stale"] = True
            stale.append(s)
    return {
        "schema": "fulcra.coordination.view.v1",
        "view": "needs-attention",
        "updated_at": updated_at,
        "stale_hours": sh,
        "tasks": sorted(stale, key=lambda x: x.get("updated_at", "")),
    }


def agent_slug(agent: str) -> str:
    """Filesystem-safe slug for an agent id, used as the inbox view basename and
    the surface-file suffix. Agent ids look like ``claude-code:host:repo`` — the
    colons are not portable in filenames, so collapse every non-[a-z0-9-_.] run
    to a single ``-`` (mirrors cache._root_slug's approach). Lowercased so two
    ids differing only in case can't fork into two views.

    The broadcast sentinel ``*`` is special-cased to ``broadcast`` (M3): every
    char of ``*`` is non-portable, so the generic path would strip to empty and
    fall back to the opaque ``agent``. ``broadcast`` makes the materialized
    ``views/inbox/broadcast.json`` file and the ``index.counts.inbox`` bucket
    human-legible. The literal ``*`` never reaches a filesystem path — this slug
    is the only place a broadcast assignee is turned into a path segment."""
    if agent == BROADCAST:
        return "broadcast"
    s = "".join(c if (c.isalnum() or c in "-_.") else "-" for c in agent.lower())
    return s.strip("-") or "agent"


def agent_matches(me: str, assignee: str) -> bool:
    """True when a directive addressed to `assignee` belongs in `me`'s inbox.

    Match rule: ``assignee == me`` OR ``assignee``'s colon-segments are a prefix
    of ``me``'s colon-segments. So a directive addressed to the SHORT id
    ``claude-code`` reaches the full id ``claude-code:DeskbookPro:fulcra-coord``,
    and ``claude-code:DeskbookPro`` does too — but ``openclaw`` (different kind)
    and ``claude-code:other`` (divergent segment) do NOT, and a MORE-specific
    assignee never matches a less-specific me.

    This is the correctness fix for the inbox bug: agents derive a full id, but
    another agent addressed directives to the short id, so the strict
    slug-equality match silently dropped them. Prefix matching is per-colon-
    segment (not per-character), so ``claude`` is not a prefix of ``claude-code``.

    Defensive (M2): a falsy ``me`` or ``assignee`` (None/empty) can never be a
    real match, and ``None.split(":")`` would raise into a best-effort read path,
    so return False early rather than crashing the caller.
    """
    if not me or not assignee:
        return False
    if assignee == BROADCAST:
        # A broadcast directive reaches every agent. This is the only place the
        # wildcard widens matching; concrete assignees below still match strictly
        # by colon-segment prefix, so "*" can't be reproduced by any real id.
        return True
    if assignee == me:
        return True
    me_parts = me.split(":")
    as_parts = assignee.split(":")
    if len(as_parts) >= len(me_parts):
        # Equal length is handled by the == check above; a longer assignee is
        # strictly more specific than me and must not match.
        return False
    return me_parts[:len(as_parts)] == as_parts


def _is_owners_own_directive(owner_agent: Optional[str], assignee: str) -> bool:
    """True when a directive is the OWNER's own work, not a cross-agent directive.

    BUG 3: a task whose assignee is a short-id PREFIX of its owner_agent (e.g.
    owner='claude-code:h:r', assignee='claude-code') is the owner's own task —
    the owner's read path (inbox_for) hides it because owner_agent matches `me`.
    The index used a bare exact `owner_agent == assignee` test, so it kept
    counting these even though the read path hid them. Using the SAME prefix-
    aware ownership relation here makes the count and the read path agree.
    agent_matches(owner, assignee) is True when assignee equals or prefixes the
    owner — exactly the "owner is (a refinement of) the assignee" relation.

    A BROADCAST directive (assignee == "*") is fan-out to every agent, NOT the
    owner's own work, so it is never self-owned — guarding here keeps broadcasts
    visible/counted (agent_matches treats "*" as matching everything).
    """
    if not owner_agent or assignee == BROADCAST:
        return False
    return agent_matches(owner_agent, assignee)


def is_open_directive(task: dict[str, Any], assignee: str,
                      now: Optional[datetime] = None,
                      include_aged: bool = False,
                      age_days: Optional[float] = None) -> bool:
    """True when `task` is an unacknowledged directive addressed to `assignee`.

    Open inbox item = assigned to me, in an open status (proposed/waiting),
    owned by someone *else* (a self-owned task is my own work, not a directive),
    and not yet acked by me (no inbox_ack event whose `by` is me). Claiming the
    task (status -> active, owner becomes me) also drops it from the inbox via
    both the status and owner checks.

    AGE-OUT: a stale informational broadcast (is_aged_out_broadcast) is treated
    as no longer open by default, so the materialized inbox view and the
    index.counts.inbox fold don't keep counting fan-out that the live `inbox`
    read filter (inbox_for) already hides — the SessionStart banner count and the
    materialized buckets must agree with the read path. `include_aged=True`
    keeps the legacy "everything open" semantics. Concrete-assignee directives
    are never aged out (is_aged_out_broadcast guards on assignee == BROADCAST).
    """
    if task.get("assignee") != assignee:
        return False
    if task.get("status") not in INBOX_OPEN_STATUSES:
        return False
    if _is_owners_own_directive(task.get("owner_agent"), assignee):
        return False
    if _acked_by(task, assignee):
        return False
    if not include_aged and is_aged_out_broadcast(task, now, age_days):
        return False
    return True


def build_inbox(tasks: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group open directives by assignee slug -> list of compact summaries.

    The read surface for `fulcra-coord inbox`: one bucket per assignee that has
    at least one open directive. Assignees with no open directives are omitted
    (an empty inbox needs no view). Keyed by agent_slug so the per-assignee view
    files (`views/inbox/<slug>.json`) and the index counts agree on the key.
    """
    inbox: dict[str, list[dict[str, Any]]] = {}
    for t in tasks:
        assignee = t.get("assignee")
        if not assignee or not is_open_directive(t, assignee):
            continue
        inbox.setdefault(agent_slug(assignee), []).append(task_summary(t))
    for slug in inbox:
        inbox[slug] = sorted(inbox[slug],
                             key=lambda x: (x.get("priority", "P9"), x.get("updated_at", "")))
    return inbox


def inbox_for(me: str, tasks: list[dict[str, Any]], now: Optional[datetime] = None,
              include_aged: bool = False,
              age_days: Optional[float] = None) -> list[dict[str, Any]]:
    """Open directives addressed to `me`, using prefix-aware matching.

    Membership for a querying agent is decided by agent_matches (a directive
    addressed to a short id reaches the full-id agent it prefixes), NOT by exact
    slug equality. The materialized inbox view stays keyed by each assignee's own
    slug (build_inbox, unchanged); this is the read path that a concrete agent
    uses to gather every directive it should act on across those buckets.

    A directive is in my inbox when it is an open directive for its OWN assignee
    (open status, owned by someone else, not acked by me) AND agent_matches(me,
    assignee). The ack check is against `me`: my own ack clears it from my inbox
    even if the directive was addressed to a short id I prefix.

    AGE-OUT (default behaviour): a stale informational BROADCAST (see
    is_aged_out_broadcast) is excluded so old "X joined the mesh" fan-out stops
    cluttering every inbox / SessionStart. This is a VIEW filter only — the task
    is untouched. `include_aged=True` (the `inbox --all` path) bypasses it and
    shows everything. `now` is injectable for deterministic tests (like
    needs_human / is_stale). CONCRETE-assignee directives are never aged out.
    """
    items: list[dict[str, Any]] = []
    for t in tasks:
        assignee = t.get("assignee")
        if not assignee or not agent_matches(me, assignee):
            continue
        if t.get("status") not in INBOX_OPEN_STATUSES:
            continue
        # Hide my own work (owner_agent == me) and any directive that is the
        # owner's own (assignee equals/prefixes owner_agent) — the same
        # prefix-aware ownership relation the index now uses (BUG 3), so the
        # read path and index.counts.inbox agree.
        if t.get("owner_agent") == me or _is_owners_own_directive(t.get("owner_agent"), assignee):
            continue
        if _acked_by(t, me):
            continue
        if not include_aged and is_aged_out_broadcast(t, now, age_days):
            continue
        items.append(task_summary(t))
    return sorted(items, key=lambda x: (x.get("priority", "P9"), x.get("updated_at", "")))


def aged_out_inbox_count(me: str, tasks: list[dict[str, Any]],
                         now: Optional[datetime] = None,
                         age_days: Optional[float] = None) -> int:
    """How many of `me`'s otherwise-open directives are hidden purely by age-out.

    The default `inbox` surfaces this as a one-line "(N older broadcasts hidden —
    --all to show)" note so an agent knows the live view was trimmed and can opt
    back in. Defined as the difference between the full membership (include_aged)
    and the default membership, so the count can never disagree with what
    inbox_for actually hides — there is one filter, queried two ways.
    """
    full = {s["id"] for s in inbox_for(me, tasks, now, include_aged=True, age_days=age_days)}
    shown = {s["id"] for s in inbox_for(me, tasks, now, include_aged=False, age_days=age_days)}
    return len(full - shown)


# Statuses at which a task counts as "still on the human's plate": proposed
# (asked, not started), waiting (parked on them), or blocked (blocked --on-user).
# Terminal/active states are not awaiting the human, so they are excluded.
NEEDS_HUMAN_OPEN_STATUSES = ("proposed", "waiting", "blocked")


def _human_match(t: dict[str, Any], human: str) -> bool:
    """True when an OPEN task is directed at the human (the strict needs-me
    membership rule, factored out so needs_human and upcoming_for_human share
    one definition): open status AND (a CONCRETE non-broadcast assignee matching
    the human OR the explicit ``needs:human`` tag). A bare ``*`` broadcast never
    qualifies — see needs_human's docstring for why."""
    if t.get("status") not in NEEDS_HUMAN_OPEN_STATUSES:
        return False
    assignee = t.get("assignee")
    tags = t.get("tags") or []
    concrete_for_human = (
        assignee and assignee != BROADCAST and agent_matches(human, assignee)
    )
    return bool(concrete_for_human or "needs:human" in tags)


def _schedule_iso_z(value: Any) -> Optional[str]:
    """Normalize an optional stored schedule timestamp for comparisons.

    Scheduling is a visibility gate, so malformed persisted data must degrade to
    "no gate" rather than hiding a concrete human ask forever.
    """
    dt = _schedule_dt(value)
    if dt is None:
        return None
    return dt.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _schedule_dt(value: Any) -> Optional[datetime]:
    """Parse an optional stored schedule timestamp to a tz-aware UTC datetime.

    BUG 7: the not_before/due gates must compare PARSED datetimes, never the
    stored ISO-Z strings. The wall-clock instant emits fractional seconds only
    when its microsecond != 0 while a stored value keeps its own precision, so a
    lexical compare of the mixed-width strings is unsound (``Z`` 0x5A sorts after
    ``.`` 0x2E) — a sub-second-past not_before could gate as future. A naive
    parsed value is assumed UTC (matching the rest of the schedule layer).
    Malformed/absent -> None ("no gate")."""
    dt = _parse_dt(value)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def needs_human(tasks: list[dict[str, Any]], human: str, *,
                now: Optional[datetime] = None) -> list[dict[str, Any]]:
    """Every OPEN task assigned to / blocked on the human that is ACTIONABLE NOW.

    The data layer behind ``needs-me`` and the SessionStart "BLOCKED ON YOU"
    banner: the human's situational-awareness glance of "what's on my plate from
    my agents." Membership is prefix-aware via ``agent_matches`` so both the
    neutral ``human`` and a personalized handle (``ash``) resolve, and an
    assignee addressed to a short id reaches the full one. Open = proposed /
    waiting / blocked (an active or terminal task is not awaiting the human).

    Returned oldest-first (by ``updated_at``) so the longest-waiting ask — the
    thing the human has been blocking the longest — sorts to the top.

    PRECISION (broadcast exclusion): a broadcast (``assignee == "*"``) reaches
    every agent's inbox via ``agent_matches``'s wildcard, but an all-agent
    announcement is NOT a personal ask blocked on the human. Including them
    floods the SessionStart "⛔ BLOCKED ON YOU" banner with join-announcement
    noise and buries the real asks — defeating the whole situational-awareness
    point. So membership here is the STRICT signal: a CONCRETE assignee that
    matches the human (``block --on-user`` sets ``assignee = human``), OR the
    explicit ``needs:human`` tag. The broadcast wildcard never qualifies.

    SCHEDULING (the not_before gate): a task with a FUTURE ``not_before`` is
    something the human CANNOT act on yet (e.g. a re-auth window that opens next
    week), so it is EXCLUDED from this DUE-NOW plate — it would otherwise show
    "BLOCKED ON YOU" every session for days. Such items are surfaced separately
    by ``upcoming_for_human``. A task with no/empty/past ``not_before`` behaves
    exactly as before — the gate only ever HIDES future work, never the rest."""
    now_dt = (now or _now()).astimezone(timezone.utc)
    items = []
    for t in tasks:
        if not _human_match(t, human):
            continue
        nb = _schedule_dt(t.get("not_before"))
        # Gate: a future not_before keeps it off the due-now plate. Empty/None
        # or malformed not_before is "no gate" -> always due-now (today's
        # behavior). Compare PARSED datetimes (BUG 7) so sub-second precision
        # differences between `now` and the stored value can't misgate.
        if nb and nb > now_dt:
            continue
        items.append(task_summary(t))
    return sorted(items, key=lambda x: x.get("updated_at", ""))


def upcoming_for_human(tasks: list[dict[str, Any]], human: str, *,
                       now: Optional[datetime] = None,
                       within_days: int = 7) -> list[dict[str, Any]]:
    """The human's NOT-YET-ACTIONABLE asks: open tasks directed at the human
    whose ``not_before`` is in the FUTURE but within ``within_days``.

    The companion to ``needs_human``: the not_before gate hides future work from
    the DUE-NOW plate, and this surfaces it as a compact "Upcoming (next 7d)"
    list so the human still has line-of-sight on what's coming — without it
    inflating the "BLOCKED ON YOU (N)" count. Same human-match / open-status /
    broadcast-exclusion rules as needs_human (via ``_human_match``).

    Sorted by ``not_before`` then ``due`` (soonest-actionable first, then by
    deadline) so the UI can render "in 3d / due Jun 8". Each entry carries
    ``not_before`` + ``due`` (already on task_summary)."""
    now_dt = (now or _now()).astimezone(timezone.utc)
    horizon_dt = now_dt + timedelta(days=within_days)
    items = []
    for t in tasks:
        if not _human_match(t, human):
            continue
        nb = _schedule_dt(t.get("not_before"))
        # Only FUTURE not_before items within the horizon. No/empty/malformed/
        # past not_before is due-now (handled by needs_human), not upcoming.
        # Parsed-datetime compare (BUG 7) so a sub-second-past not_before is
        # correctly excluded (it's due-now) rather than wrongly listed.
        if not nb or nb <= now_dt or nb > horizon_dt:
            continue
        s = task_summary(t)
        # Sort key stays a string (ISO-Z) — sorting only orders entries and is
        # unaffected by the lexical/precision issue the GATE had.
        items.append((_schedule_iso_z(s.get("not_before")) or "",
                      _schedule_iso_z(s.get("due")) or "", s))
    return [s for _, _, s in sorted(items, key=lambda x: (x[0], x[1]))]


def build_next(tasks: list[dict[str, Any]], updated_at: Optional[str] = None) -> dict[str, Any]:
    """Proposed and waiting tasks — candidates for starting next."""
    if updated_at is None:
        updated_at = _now().isoformat(timespec="microseconds").replace("+00:00", "Z")
    candidates = [
        task_summary(t) for t in tasks if t.get("status") in ("proposed", "waiting")
    ]
    return {
        "schema": "fulcra.coordination.view.v1",
        "view": "next",
        "updated_at": updated_at,
        "tasks": sorted(candidates, key=lambda x: (x.get("priority", "P9"), x.get("updated_at", ""))),
    }


def build_recently_done(
    tasks: list[dict[str, Any]],
    updated_at: Optional[str] = None,
    days: int = RECENTLY_DONE_DAYS,
) -> dict[str, Any]:
    """Done and abandoned tasks within retention window."""
    if updated_at is None:
        updated_at = _now().isoformat(timespec="microseconds").replace("+00:00", "Z")
    cutoff = _now() - timedelta(days=days)
    recent = []
    for t in tasks:
        if t.get("status") not in ("done", "abandoned"):
            continue
        done_at = _done_at(t)
        if done_at:
            dt = _parse_dt(done_at)
            if dt and dt >= cutoff:
                recent.append(task_summary(t))
    return {
        "schema": "fulcra.coordination.view.v1",
        "view": "recently-done",
        "updated_at": updated_at,
        "retention_days": days,
        "tasks": sorted(recent, key=lambda x: x.get("updated_at", ""), reverse=True),
    }


def build_search_index(tasks: list[dict[str, Any]], updated_at: Optional[str] = None) -> dict[str, Any]:
    """Compact tag/title/summary records for search."""
    if updated_at is None:
        updated_at = _now().isoformat(timespec="microseconds").replace("+00:00", "Z")

    cutoff_done = _now() - timedelta(days=SEARCH_INDEX_DONE_DAYS)
    records = []
    for t in tasks:
        status = t.get("status", "")
        if status in ("done", "abandoned"):
            done_at = _done_at(t)
            if done_at:
                dt = _parse_dt(done_at)
                if not dt or dt < cutoff_done:
                    continue

        records.append({
            "id": t["id"],
            "title": t["title"],
            "status": status,
            "priority": t.get("priority", ""),
            "workstream": t.get("workstream", ""),
            "owner_agent": t.get("owner_agent", ""),
            "tags": t.get("tags", []),
            "summary": t.get("current_summary", ""),
            "task_file": task_file_path(t["id"]),
            "updated_at": t.get("updated_at", ""),
        })

    return {
        "schema": "fulcra.coordination.search_index.v1",
        "updated_at": updated_at,
        "records": records,
    }


def build_workstream_view(
    workstream: str,
    tasks: list[dict[str, Any]],
    updated_at: Optional[str] = None,
    done_days: int = RECENTLY_DONE_DAYS,
) -> dict[str, Any]:
    """Per-workstream view: active/waiting/blocked + recent done."""
    if updated_at is None:
        updated_at = _now().isoformat(timespec="microseconds").replace("+00:00", "Z")
    cutoff = _now() - timedelta(days=done_days)
    ws_tasks = [t for t in tasks if t.get("workstream") == workstream]
    active = [
        task_summary(t) for t in ws_tasks if t.get("status") in ("active", "waiting", "blocked")
    ]
    recent_done = []
    for t in ws_tasks:
        if t.get("status") not in ("done", "abandoned"):
            continue
        done_at = _done_at(t)
        if done_at:
            dt = _parse_dt(done_at)
            if dt and dt >= cutoff:
                recent_done.append(task_summary(t))

    return {
        "schema": "fulcra.coordination.workstream_view.v1",
        "workstream": workstream,
        "updated_at": updated_at,
        "active": sorted(active, key=lambda x: (x.get("priority", "P9"), x.get("updated_at", ""))),
        "recent_done": sorted(recent_done, key=lambda x: x.get("updated_at", ""), reverse=True),
    }


def build_agent_view(
    agent: str,
    tasks: list[dict[str, Any]],
    updated_at: Optional[str] = None,
    done_days: int = RECENTLY_DONE_DAYS,
) -> dict[str, Any]:
    """Per-agent view: tasks owned or recently touched by agent."""
    if updated_at is None:
        updated_at = _now().isoformat(timespec="microseconds").replace("+00:00", "Z")
    cutoff = _now() - timedelta(days=done_days)
    agent_tasks = [
        t for t in tasks
        if t.get("owner_agent") == agent or t.get("last_touched_by") == agent
    ]
    active = [
        task_summary(t) for t in agent_tasks
        if t.get("status") in ("active", "waiting", "blocked", "proposed")
    ]
    recent_done = []
    for t in agent_tasks:
        if t.get("status") not in ("done", "abandoned"):
            continue
        done_at = _done_at(t)
        if done_at:
            dt = _parse_dt(done_at)
            if dt and dt >= cutoff:
                recent_done.append(task_summary(t))

    return {
        "schema": "fulcra.coordination.agent_view.v1",
        "agent": agent,
        "updated_at": updated_at,
        "active": sorted(active, key=lambda x: (x.get("priority", "P9"), x.get("updated_at", ""))),
        "recent_done": sorted(recent_done, key=lambda x: x.get("updated_at", ""), reverse=True),
    }


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def search_tasks(query: str, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Simple text search across title, summary, tags, workstream, agent."""
    q = query.lower()
    results = []
    for t in tasks:
        text = " ".join([
            t.get("title", ""),
            t.get("current_summary", ""),
            t.get("workstream", ""),
            t.get("owner_agent", ""),
            " ".join(t.get("tags", [])),
        ]).lower()
        if q in text:
            results.append(task_summary(t))
    return sorted(results, key=lambda x: x.get("updated_at", ""), reverse=True)


# ---------------------------------------------------------------------------
# Summaries aggregate (the read-side + rebuild source)
# ---------------------------------------------------------------------------

def build_summaries(tasks: list[dict[str, Any]],
                    updated_at: Optional[str] = None) -> dict[str, Any]:
    """The single durable aggregate of task_summary dicts (``views/summaries.json``).

    WHY this exists (the performance refactor): before this, every READ
    (status/agents/needs-me/resume/search) and every WRITE's view rebuild called
    _load_all_tasks, which fetches each task BODY one network round-trip at a
    time (~N round-trips). This aggregate is ONE file: reads load it directly, and
    the write path upserts the just-written summary into it and rebuilds all views
    from it — so neither path ever fetches N bodies again.

    Membership: ALL tasks passed in. Callers already pass exactly the set the
    other views collectively cover (all non-terminal tasks plus done/abandoned
    inside the recently-done / search cutoffs), so including everything passed is
    the simplest correct choice; the per-view cutoffs still apply inside each
    builder when views are rebuilt from this list.

    A LIST (not a map) of summaries, for consistency with the other views' task
    lists and so it round-trips as plain JSON without key-ordering concerns."""
    if updated_at is None:
        updated_at = _now().isoformat(timespec="microseconds").replace("+00:00", "Z")
    return {
        "schema": "fulcra.coordination.summaries.v1",
        "view": "summaries",
        "updated_at": updated_at,
        "summaries": [task_summary(t) for t in tasks],
    }


# ---------------------------------------------------------------------------
# Agent presence roster (situational awareness)
# ---------------------------------------------------------------------------

PRESENCE_VIEW_SCHEMA = "fulcra.coordination.presence_view.v1"


def presence_liveness(last_seen: str, now: Optional[datetime] = None,
                      stale_hours: Optional[float] = None) -> str:
    """Classify a presence record's recency into live / idle / stale.

    Reuses the SAME staleness threshold the task views use (``_stale_hours`` ->
    ``FULCRA_COORD_STALE_HOURS``, default 2h) so "stale" means one thing across
    the whole tool. Bands (documented in the design):

      * live  — younger than HALF the threshold: actively working right now.
      * idle  — between half and the full threshold: quiet but not yet suspect.
      * stale — at or past the threshold: probably crashed / forgotten session.

    Splitting at 0.5x gives a meaningful "live" band without a second env knob;
    a missing/unparseable ``last_seen`` ages to +inf (via _age_hours) and so
    classifies stale — the same fail-toward-surfacing choice is_stale makes."""
    threshold = _stale_hours(stale_hours)
    if now is None:
        now = _now()
    age = _age_hours(last_seen, now)
    if age < threshold * 0.5:
        return "live"
    if age < threshold:
        return "idle"
    return "stale"


_ROUTING_TIER = {"live": 0, "idle": 1}  # below-floor never appears here


def _effective_routing_liveness(last_seen: str, now: datetime,
                                grace_seconds: float,
                                stale_hours: Optional[float] = None) -> Optional[str]:
    """Recompute a candidate's liveness FOR ROUTING from bus-global last_seen.

    Owned entirely by the resolver — it does NOT trust an aggregate's stored
    `liveness` field, because a stale rebuild could under-report it (codex
    tightening #1). One consistent judgment, identical on every machine:
      * within the idle cutoff (presence_liveness live/idle bands) -> that band.
      * within stale_cutoff + grace_seconds -> 'idle' (the wall-clock grace
        window: one missed heartbeat / a sleep-wake must not drop a reviewer).
      * beyond -> None (below floor).
    A missing/unparseable last_seen ages to +inf -> below floor (None)."""
    band = presence_liveness(last_seen, now, stale_hours)  # live | idle | stale
    if band in ("live", "idle"):
        return band
    # band == "stale": apply the wall-clock grace before dropping below floor.
    age_seconds = _age_hours(last_seen, now) * 3600.0
    cutoff_seconds = _stale_hours(stale_hours) * 3600.0
    if age_seconds < cutoff_seconds + grace_seconds:
        return "idle"
    return None


def resolve_live_recipient(candidates: list[str], presence: list[dict[str, Any]],
                           *, floor: str = "idle", now: Optional[datetime] = None,
                           exclude: tuple[str, ...] = (),
                           grace_seconds: Optional[float] = None) -> Optional[str]:
    """Pick the live/idle candidate minimizing (effective_tier, preference_index).

    Pure + deterministic given `presence` + `now` (both injectable -> testable).
    `candidates` is in PREFERENCE order (canonical reviewer first). `floor`
    'idle' accepts live OR idle; 'live' accepts live only. Below-floor and
    `exclude`d agents are skipped. Returns None when nobody clears the floor
    (the caller then escalates to the human — never parks on a dead agent).

    Effective liveness is recomputed inside (via _effective_routing_liveness)
    from each candidate's bus-global last_seen + the wall-clock grace, so the
    stored aggregate tier is never trusted and the judgment is identical on
    every machine (machine-agnostic invariant)."""
    if now is None:
        now = _now()
    grace = _presence_grace_seconds(grace_seconds)
    floor_rank = _ROUTING_TIER.get(floor, 1)  # default to idle floor
    by_agent = {r.get("agent"): r for r in presence}
    best: Optional[tuple[int, int, str]] = None
    for idx, agent in enumerate(candidates):
        if agent in exclude:
            continue
        rec = by_agent.get(agent)
        if not rec:
            continue  # never connected -> below floor
        eff = _effective_routing_liveness(rec.get("last_seen", ""), now, grace)
        if eff is None:
            continue
        tier = _ROUTING_TIER[eff]
        if tier > floor_rank:
            continue  # below the requested floor (e.g. idle when floor=live)
        key = (tier, idx, agent)
        if best is None or key < best:
            best = key
    return best[2] if best else None


def build_presence(records: list[dict[str, Any]], now: Optional[datetime] = None,
                   updated_at: Optional[str] = None) -> dict[str, Any]:
    """Build the aggregate presence roster from per-agent presence records.

    Pure function over a list of ``make_presence`` dicts: each entry is carried
    through with a ``liveness`` annotation (live/idle/stale by last_seen age) and
    the roster is sorted most-recently-active first, so the human glancing at it
    sees who is actively working at the top. This is the read-side aggregate the
    ``presence`` / ``agents`` / ``resume`` surfaces consume in one download."""
    if updated_at is None:
        updated_at = _now().isoformat(timespec="microseconds").replace("+00:00", "Z")
    if now is None:
        now = _now()
    agents = []
    for rec in records:
        entry = dict(rec)
        entry["liveness"] = presence_liveness(rec.get("last_seen", ""), now)
        agents.append(entry)
    # Most-recently-seen first (descending last_seen). Sort by the PARSED
    # datetime, not the raw string (BUG 1): a same-second pair where one record
    # was stamped with microsecond=0 ("...45Z") and the other with µs>0
    # ("...45.5Z") would mis-order under a lexical compare ('.' < 'Z'), putting
    # a STALER agent on top. A missing/unparseable timestamp coerces to epoch
    # (datetime.min) so it sorts LAST under reverse — matching the prior intent.
    _epoch = datetime.min.replace(tzinfo=timezone.utc)
    agents.sort(key=lambda a: _parse_dt(a.get("last_seen", "")) or _epoch,
                reverse=True)
    return {
        "schema": PRESENCE_VIEW_SCHEMA,
        "view": "presence",
        "updated_at": updated_at,
        "agents": agents,
    }


# ---------------------------------------------------------------------------
# All views at once (for write fan-out)
# ---------------------------------------------------------------------------

def build_all_views(tasks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Build every standard view. Returns dict of name -> view data.

    INVARIANT (the linchpin of the perf refactor): this must produce IDENTICAL
    output whether ``tasks`` are full task bodies or ``schema.task_summary`` dicts.
    Every builder reads only fields task_summary emits (done_at and last_touched_by
    were added there for exactly this reason), so the write path can rebuild views
    from the summaries aggregate instead of re-fetching task bodies. The
    equivalence test (TestBuildAllViewsEquivalence) guards this property."""
    now = _now().isoformat(timespec="microseconds").replace("+00:00", "Z")
    result: dict[str, dict[str, Any]] = {
        "index": build_index(tasks, now),
        "active": build_active(tasks, now),
        "next": build_next(tasks, now),
        "recently-done": build_recently_done(tasks, now),
        "search-index": build_search_index(tasks, now),
        "needs-attention": build_needs_attention(tasks, now),
        # The summaries aggregate is itself a view, so it is rebuilt and uploaded
        # on every write alongside the others — keeping the read-side source fresh.
        "summaries": build_summaries(tasks, now),
    }

    workstreams = sorted(set(t.get("workstream", "") for t in tasks) - {""})
    for ws in workstreams:
        result[f"workstreams/{ws}"] = build_workstream_view(ws, tasks, now)

    agents = sorted({
        agent
        for t in tasks
        for agent in (t.get("owner_agent", ""), t.get("last_touched_by", ""))
        if agent
    })
    for agent in agents:
        result[f"agents/{agent}"] = build_agent_view(agent, tasks, now)

    # One inbox view per assignee with open directives, keyed by agent_slug.
    # NOTE: cmd_inbox no longer reads this view — it recomputes from the task set
    # (see cli._load_inbox), because this view is only emitted for assignees who
    # *still* have open directives, so it goes stale (never overwritten) once an
    # inbox empties. The view file is retained as a materialized artifact (index
    # inbox counts come from build_inbox directly), but is no longer the read path.
    for slug, items in build_inbox(tasks).items():
        result[f"inbox/{slug}"] = {
            "schema": "fulcra.coordination.inbox_view.v1",
            "view": "inbox",
            "assignee_slug": slug,
            "updated_at": now,
            "inbox": items,
        }

    return result


# ---------------------------------------------------------------------------
# Operator digest (situational-awareness fold, piece 7)
# ---------------------------------------------------------------------------

def _digest_due_key(s: dict[str, Any]) -> tuple:
    """Ranking key for the blocked-on-you block: due soonest first, then oldest.

    Both components are PARSED via _parse_dt (BUG 7 / PR #39): a missing/malformed
    ``due`` sorts LAST (datetime.max) so dated asks lead, and the age tiebreak is
    the parsed ``updated_at`` (oldest first) so the longest-waiting ask wins ties.
    We compare parsed datetimes, never the mixed-precision ISO-Z strings."""
    due = _parse_dt(s.get("due") or "")
    upd = _parse_dt(s.get("updated_at") or "")
    return (
        due or datetime.max.replace(tzinfo=timezone.utc),
        upd or datetime.max.replace(tzinfo=timezone.utc),
    )


def build_operator_digest(summaries: list[dict[str, Any]],
                          presence: list[dict[str, Any]], *,
                          human: str,
                          now: Optional[datetime] = None,
                          since: Optional[datetime] = None) -> dict[str, Any]:
    """Fold bus state into the operator's situational-awareness digest (pure).

    Four blocks, derived ONLY from task_summary dicts + presence records (no I/O,
    no body fetch). Deterministic given injected ``now``/``since`` (both injected
    for tests). Reuses the existing read-model so the digest can never disagree
    with ``needs-me`` / ``presence`` / ``needs-attention``:

      * ``blocked_on_you`` — ``needs_human`` (due-now only), RE-RANKED by due
        soonest then oldest updated_at (the human reads the most-urgent ask
        first). ``needs_human`` returns oldest-first; we re-sort by _digest_due_key.
      * ``upcoming`` — ``upcoming_for_human`` (future not_before within 7d).
      * ``per_agent`` — one entry per presence record: its agent id, workstreams,
        liveness, summary, and the tasks it FINISHED/transitioned since ``since``
        (done/abandoned with done_at >= since). Parsed-datetime ``since`` compare.
      * ``stale`` — active tasks past the stale threshold (``is_stale``), the same
        needs-attention safety-net set, sorted oldest-first.

    ``now``/``since`` default to wall-clock / (now - 12h) so a bare call still
    works, but the command always injects them explicitly."""
    now_dt = (now or _now()).astimezone(timezone.utc)
    since_dt = (since or (now_dt - timedelta(hours=12))).astimezone(timezone.utc)

    blocked = sorted(needs_human(summaries, human, now=now_dt), key=_digest_due_key)
    upcoming = upcoming_for_human(summaries, human, now=now_dt)

    # Index finished/transitioned-since tasks by the owning/touching agent, so a
    # per_agent entry can list what that agent wrapped up this window. A summary
    # carries done_at (flattened) — gate on a PARSED compare against since.
    by_agent_done: dict[str, list[dict[str, Any]]] = {}
    for s in summaries:
        if s.get("status") not in ("done", "abandoned"):
            continue
        done_dt = _parse_dt(s.get("done_at") or s.get("updated_at") or "")
        if done_dt is None or done_dt < since_dt:
            continue
        for who in {s.get("owner_agent"), s.get("last_touched_by")}:
            if who:
                by_agent_done.setdefault(who, []).append(s)

    per_agent = []
    for rec in presence:
        agent = rec.get("agent", "")
        per_agent.append({
            "agent": agent,
            "workstreams": list(rec.get("workstreams", [])),
            "summary": rec.get("summary", ""),
            "liveness": presence_liveness(rec.get("last_seen", ""), now_dt),
            "finished_since": sorted(
                by_agent_done.get(agent, []),
                key=lambda x: _parse_dt(x.get("done_at") or x.get("updated_at") or "")
                or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True),
        })

    stale = sorted(
        (s for s in summaries if is_stale(s, now_dt)),
        key=lambda x: _parse_dt(x.get("updated_at") or "")
        or datetime.min.replace(tzinfo=timezone.utc))

    return {
        "schema": "fulcra.coordination.operator_digest.v1",
        "human": human,
        "now": now_dt.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "since": since_dt.isoformat(timespec="microseconds").replace("+00:00", "Z"),
        "blocked_on_you": blocked,
        "upcoming": upcoming,
        "per_agent": per_agent,
        "stale": stale,
    }
