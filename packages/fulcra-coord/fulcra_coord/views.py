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
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Individual view builders
# ---------------------------------------------------------------------------

def build_index(tasks: list[dict[str, Any]], updated_at: Optional[str] = None) -> dict[str, Any]:
    """Global compact index with counts and active summaries."""
    if updated_at is None:
        updated_at = _now().isoformat().replace("+00:00", "Z")

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
        done_at = t.get("done", {}).get("done_at") or t.get("updated_at", "")
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
        updated_at = _now().isoformat().replace("+00:00", "Z")
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
        updated_at = _now().isoformat().replace("+00:00", "Z")
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


def is_open_directive(task: dict[str, Any], assignee: str) -> bool:
    """True when `task` is an unacknowledged directive addressed to `assignee`.

    Open inbox item = assigned to me, in an open status (proposed/waiting),
    owned by someone *else* (a self-owned task is my own work, not a directive),
    and not yet acked by me (no inbox_ack event whose `by` is me). Claiming the
    task (status -> active, owner becomes me) also drops it from the inbox via
    both the status and owner checks.
    """
    if task.get("assignee") != assignee:
        return False
    if task.get("status") not in INBOX_OPEN_STATUSES:
        return False
    if task.get("owner_agent") == assignee:
        return False
    for e in task.get("events", []):
        if e.get("type") == "inbox_ack" and e.get("by") == assignee:
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


def inbox_for(me: str, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
    """
    items: list[dict[str, Any]] = []
    for t in tasks:
        assignee = t.get("assignee")
        if not assignee or not agent_matches(me, assignee):
            continue
        if t.get("status") not in INBOX_OPEN_STATUSES:
            continue
        if t.get("owner_agent") == me or t.get("owner_agent") == assignee:
            continue
        if any(e.get("type") == "inbox_ack" and e.get("by") == me
               for e in t.get("events", [])):
            continue
        items.append(task_summary(t))
    return sorted(items, key=lambda x: (x.get("priority", "P9"), x.get("updated_at", "")))


# Statuses at which a task counts as "still on the human's plate": proposed
# (asked, not started), waiting (parked on them), or blocked (blocked --on-user).
# Terminal/active states are not awaiting the human, so they are excluded.
NEEDS_HUMAN_OPEN_STATUSES = ("proposed", "waiting", "blocked")


def needs_human(tasks: list[dict[str, Any]], human: str) -> list[dict[str, Any]]:
    """Every OPEN task assigned to / blocked on the human, across all owners.

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
    explicit ``needs:human`` tag. The broadcast wildcard never qualifies."""
    items = []
    for t in tasks:
        if t.get("status") not in NEEDS_HUMAN_OPEN_STATUSES:
            continue
        assignee = t.get("assignee")
        tags = t.get("tags") or []
        # Concrete (non-broadcast) assignee directed at the human, OR the
        # explicit needs:human marker. A bare "*" broadcast does NOT count.
        concrete_for_human = (
            assignee and assignee != BROADCAST and agent_matches(human, assignee)
        )
        if not (concrete_for_human or "needs:human" in tags):
            continue
        items.append(task_summary(t))
    return sorted(items, key=lambda x: x.get("updated_at", ""))


def build_next(tasks: list[dict[str, Any]], updated_at: Optional[str] = None) -> dict[str, Any]:
    """Proposed and waiting tasks — candidates for starting next."""
    if updated_at is None:
        updated_at = _now().isoformat().replace("+00:00", "Z")
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
        updated_at = _now().isoformat().replace("+00:00", "Z")
    cutoff = _now() - timedelta(days=days)
    recent = []
    for t in tasks:
        if t.get("status") not in ("done", "abandoned"):
            continue
        done_at = t.get("done", {}).get("done_at") or t.get("updated_at", "")
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
        updated_at = _now().isoformat().replace("+00:00", "Z")

    cutoff_done = _now() - timedelta(days=SEARCH_INDEX_DONE_DAYS)
    records = []
    for t in tasks:
        status = t.get("status", "")
        if status in ("done", "abandoned"):
            done_at = t.get("done", {}).get("done_at") or t.get("updated_at", "")
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
        updated_at = _now().isoformat().replace("+00:00", "Z")
    cutoff = _now() - timedelta(days=done_days)
    ws_tasks = [t for t in tasks if t.get("workstream") == workstream]
    active = [
        task_summary(t) for t in ws_tasks if t.get("status") in ("active", "waiting", "blocked")
    ]
    recent_done = []
    for t in ws_tasks:
        if t.get("status") not in ("done", "abandoned"):
            continue
        done_at = t.get("done", {}).get("done_at") or t.get("updated_at", "")
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
        updated_at = _now().isoformat().replace("+00:00", "Z")
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
        done_at = t.get("done", {}).get("done_at") or t.get("updated_at", "")
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
# All views at once (for write fan-out)
# ---------------------------------------------------------------------------

def build_all_views(tasks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Build every standard view. Returns dict of name -> view data."""
    now = _now().isoformat().replace("+00:00", "Z")
    result: dict[str, dict[str, Any]] = {
        "index": build_index(tasks, now),
        "active": build_active(tasks, now),
        "next": build_next(tasks, now),
        "recently-done": build_recently_done(tasks, now),
        "search-index": build_search_index(tasks, now),
        "needs-attention": build_needs_attention(tasks, now),
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
