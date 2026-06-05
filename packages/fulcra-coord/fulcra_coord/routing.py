"""Routing state for liveness-aware reviewer routing — pure, no I/O.

Routing state lives ENTIRELY in a task's event log (no new schema fields).
Each route/reroute appends a routing event; the CURRENT route is DERIVED
deterministically from those events (latest by parsed ``at``, ties broken by
route_id) so every machine reading the same task agrees on who it is routed
to — the machine-agnostic invariant. The directive carries an EXTRA tag
``kind:review`` (REVIEW_TAG) as a membership marker, exactly like needs:human;
it is NOT the task's ``kind`` field ("review" is not a valid kind).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from .views import _parse_dt  # the ONE parsed-datetime helper (never lexical)

REVIEW_TAG = "kind:review"
ROUTE_EVENT_TYPES = ("routed", "rerouted")


def new_route_id() -> str:
    """A UUID minting the deterministic identity of one routing decision."""
    return uuid.uuid4().hex


def make_route_event(*, kind: str, to: str, by: str, attempt: int, reason: str,
                     candidate_snapshot: list[dict[str, Any]],
                     observed_updated_at: str, at: str,
                     route_id: Optional[str] = None) -> dict[str, Any]:
    """Build one routing event (kind in ROUTE_EVENT_TYPES). ``route_id`` defaults
    to a fresh UUID. ``candidate_snapshot`` is the ranked pool + tiers at decision
    time (debuggability: 'why did it pick X'). ``observed_updated_at`` is the
    task.updated_at the decider saw — the multi-sweeper convergence anchor."""
    if kind not in ROUTE_EVENT_TYPES:
        raise ValueError(f"route event kind must be one of {ROUTE_EVENT_TYPES}")
    return {
        "at": at,
        "type": kind,
        "to": to,
        "by": by,
        "attempt": attempt,
        "reason": reason,
        "candidate_snapshot": candidate_snapshot,
        "observed_updated_at": observed_updated_at,
        "route_id": route_id or new_route_id(),
    }


def route_events(task: dict[str, Any]) -> list[dict[str, Any]]:
    """Every routing event on a task, in log order."""
    return [e for e in task.get("events", []) if e.get("type") in ROUTE_EVENT_TYPES]


def latest_route_event(task: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The current routing decision: latest by PARSED ``at``, ties broken by
    route_id (stable across machines — Files has no global clock). Parsed
    compare, never lexical (BUG 1/7/8)."""
    evs = route_events(task)
    if not evs:
        return None
    _epoch = datetime.min.replace(tzinfo=timezone.utc)
    return max(evs, key=lambda e: (_parse_dt(e.get("at", "")) or _epoch,
                                   e.get("route_id", "")))


def current_route(task: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Alias for latest_route_event — the task's effective route (whose ``to``
    the writer keeps task.assignee in sync with)."""
    return latest_route_event(task)


def route_attempt_count(task: dict[str, Any]) -> int:
    """Cumulative routing attempt count.

    New routing events carry a durable cumulative ``attempt`` value because the
    inline event log is bounded and may have dropped older route decisions. Fall
    back to the visible event count only for legacy/manually-authored events that
    predate the field.
    """
    attempts = [
        e.get("attempt") for e in route_events(task)
        if isinstance(e.get("attempt"), int)
    ]
    return max(attempts) if attempts else len(route_events(task))


def tried_agents(task: dict[str, Any]) -> set[str]:
    """Every agent a route/reroute has targeted — the exclude set for the next
    resolve (a tried agent stays excluded for this cycle)."""
    return {e.get("to") for e in route_events(task) if e.get("to")}


def is_review_directive(task: dict[str, Any]) -> bool:
    """True iff this task carries the kind:review membership marker. The sweep
    keys on THIS (explicit tag membership) so it can never reroute an ordinary
    tell/directive — never via _extract_kind_from_tags (kind:ops sorts first)."""
    return REVIEW_TAG in (task.get("tags") or [])
