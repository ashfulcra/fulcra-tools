"""Directives — inbox/ack folds for the fulcra-agent-directives skill.

A directive IS a task with an ``assignee`` (the incumbent's model): ``tell`` /
``broadcast`` / ``remind`` / ``later`` are sugar over task creation. What needs
deterministic code is the read side: *which open directives does agent X still
owe attention to* — a fold over the aggregate rows plus per-agent **ack shards**
(``_coord/acks/<slug>/<agent>.md``; one file per agent per directive, safe on
LWW storage). Acking hides an item for that agent and stops re-notify; a
broadcast (assignee ``*``) completes when every non-stale roster agent has acked.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .model import OPEN_STATUSES, sort_rows

BACKLOG = "@backlog"


def parse_when(when: str, *, now: str) -> Optional[str]:
    """``remind`` schedule: ISO-8601 passthrough, or relative ``5d``/``36h``/``10m``.
    Returns an ISO ``Z`` string, or None if unparseable."""
    s = (when or "").strip()
    if not s:
        return None
    if "T" in s:
        return s  # full ISO passthrough (lexically comparable)
    if s.count("-") >= 2:
        return f"{s}T23:59:59Z"  # date-only: gate until end-of-day, not midnight
    unit = s[-1].lower()
    try:
        n = float(s[:-1])
    except ValueError:
        return None
    delta = {"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n)}.get(unit)
    if delta is None:
        return None
    try:
        base = datetime.fromisoformat(now.replace("Z", "+00:00"))
    except ValueError:
        return None
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return (base + delta).isoformat().replace("+00:00", "Z")


def is_directed_at(
    row: dict[str, Any], agent: str,
    held_roles: "Optional[set[str] | list[str]]" = None,
) -> bool:
    a = row.get("assignee")
    if a == agent or a == "*":
        return True
    # Role routing: a directive assigned to a ROLE is directed at whoever holds a
    # fresh lease on it. The caller resolves holders (a lease read) and passes the
    # roles this agent holds; an empty/None set leaves behavior unchanged.
    return bool(held_roles) and a in held_roles


def inbox(
    rows: list[dict[str, Any]],
    acks: dict[str, list[str]],
    agent: str,
    *,
    now: Optional[str] = None,
    include_backlog: bool = False,
    include_history: bool = False,
    held_roles: "Optional[set[str] | list[str]]" = None,
) -> list[dict[str, Any]]:
    """Directives for agent X, defaulting to work that still needs attention.

    The default view is open, unacked, and past its ``not_before`` gate. With
    ``include_history`` the lifecycle/ack/time gates are bypassed so callers can
    expose the full directed history behind an explicit ``--all``. Priority-
    sorted. ``acks`` maps slug -> list of agents who acked.
    """
    out: list[dict[str, Any]] = []
    for r in rows:
        if not include_history and r.get("status") not in OPEN_STATUSES:
            continue
        a = r.get("assignee")
        if a == BACKLOG and not include_backlog:
            continue
        if not is_directed_at(r, agent, held_roles):
            continue
        slug = str(r.get("name") or r.get("id") or "")
        if not include_history and agent in (acks.get(slug) or []):
            continue
        nb = r.get("not_before")
        if not include_history and nb and now is not None and str(nb) > now:
            continue
        out.append(r)
    return sort_rows(out)


def broadcast_state(
    row: dict[str, Any], acked_by: list[str], roster: Optional[list[str]],
) -> dict[str, Any]:
    """Completion state of a ``*`` directive against the presence roster.
    Without a roster (presence add-on absent) completion is UNKNOWN — acking
    only hides the item per-agent (documented degradation)."""
    if roster is None:
        return {"complete": None, "pending": None, "acked": sorted(acked_by)}
    pending = sorted(set(roster) - set(acked_by))
    return {"complete": not pending, "pending": pending, "acked": sorted(acked_by)}


def renotify(
    rows: list[dict[str, Any]], acks: dict[str, list[str]], agent: str, *,
    now: Optional[str] = None, min_priority: str = "P1",
    held_roles: "Optional[set[str] | list[str]]" = None,
) -> list[dict[str, Any]]:
    """Unacked directives at/above ``min_priority`` — the re-notify surface
    (P0 outranks P1). Same gates as ``inbox``, including role routing: this is a
    strict filter OVER ``inbox``, so it takes ``held_roles`` for the same reason
    (a role-routed P0 nobody re-notifies is the loudest version of the silence
    this module's role expansion exists to prevent)."""
    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    ceiling = order.get(min_priority, 1)
    return [r for r in inbox(rows, acks, agent, now=now, held_roles=held_roles)
            if order.get(str(r.get("priority")), 9) <= ceiling]
