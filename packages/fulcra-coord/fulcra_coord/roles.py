"""Role lease/vacancy folds — PURE.

Spec: docs/superpowers/specs/2026-06-10-roles-as-durable-identity-design.md.
THE INVERSION: the ROLE is the durable identity; a session is an ephemeral
lease on it. This module owns what a lease MEANS: when it is fresh, when a
role is HELD / VACANT / CONTESTED, and when a vacancy is past its SLA.
Everything here is a pure function over record dicts — no I/O, no clock reads
(callers inject ``now``), importing NOTHING first-party (stricter than
loops.py, which may import schema), so the fold stays injectable and the
layering fitness test in tests/test_roles.py can pin it to stdlib-only.

THE FRESHNESS RULE (no new heartbeat machinery): a lease is fresh iff its
HOLDER'S PRESENCE is fresh. The presence heartbeat the bus already runs keeps
leases alive for free; a dead session's presence goes stale and its leases
lapse with it. The liveness thresholds arrive BY PARAMETER (``stale_hours`` /
``grace_seconds``) rather than by importing views — callers pass
``views._stale_hours()`` / ``views._presence_grace_seconds()`` so "stale"
keeps meaning one thing across the whole tool without coupling this fold to
the policy layer.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional


def _age_hours(stamp: str, now: datetime) -> float:
    """Age of a bus timestamp in hours. Missing/unparseable ages to +inf —
    the same fail-toward-stale choice views._age_hours makes, so a clock-less
    record can never read as a live holder."""
    if not stamp:
        return float("inf")
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if dt.tzinfo is None and getattr(now, "tzinfo", None) is not None:
            dt = dt.replace(tzinfo=now.tzinfo)
        return (now - dt).total_seconds() / 3600.0
    except Exception:
        return float("inf")


def lease_fresh(
    lease: Optional[dict[str, Any]],
    presence_record: Optional[dict[str, Any]],
    now: datetime,
    *,
    stale_hours: float,
    grace_seconds: float = 0.0,
) -> bool:
    """True iff this lease is alive — i.e. its holder's presence is fresh.

    The lease shard itself carries no liveness (it is written once at claim
    time); ``presence_record`` is the holder's durable presence record and
    its ``last_seen`` is the heartbeat. Fresh = younger than ``stale_hours``
    plus ``grace_seconds`` of wall-clock grace (the same grace routing applies
    so one missed heartbeat / a laptop sleep-wake never flaps a role to
    VACANT). No presence record at all → stale: an agent that never connected
    cannot be holding anything."""
    if not isinstance(lease, dict) or not isinstance(presence_record, dict):
        return False
    age_seconds = _age_hours(presence_record.get("last_seen", ""), now) * 3600.0
    try:
        cutoff = float(stale_hours) * 3600.0 + float(grace_seconds)
    except (TypeError, ValueError):
        return False
    return age_seconds < cutoff


def role_status(
    role: dict[str, Any],
    leases: list[dict[str, Any]],
    presence_by_agent: dict[str, dict[str, Any]],
    now: datetime,
    *,
    stale_hours: float,
    grace_seconds: float = 0.0,
) -> dict[str, Any]:
    """Fold one role's lease sub-log + the presence roster into its status.

    Returns ``{holders, vacant, vacant_since, contested}``:

      * ``holders`` — ``[{agent, since}, ...]`` for every FRESH lease, sorted
        by agent for deterministic rendering. Duplicate shards for one agent
        (impossible on a healthy bus — per-agent lease files — but the pure
        fold must not assume) collapse to that agent's newest ``at``.
      * ``vacant`` — no fresh lease at all. The new dark-agent signal: not
        "agent X is dark" but "FUNCTION X is unstaffed".
      * ``vacant_since`` — when vacant: the newest lease stamp (the last time
        anyone held it), or the role's ``created_at`` if never claimed. None
        when held. Drives the SLA clock in vacancy_escalation_due.
      * ``contested`` — exclusive policy + more than one fresh lease: visible,
        never silently double-held. A shared role is never contested, and a
        stale lease never contests (it is simply claimable)."""
    by_agent: dict[str, dict[str, Any]] = {}
    newest_at = ""
    for lease in leases:
        if not isinstance(lease, dict):
            continue
        agent = lease.get("agent")
        at = lease.get("at") or ""
        if at > newest_at:
            newest_at = at
        if not agent:
            continue
        prev = by_agent.get(agent)
        if prev is None or at > (prev.get("at") or ""):
            by_agent[agent] = lease
    holders = [
        {"agent": agent, "since": lease.get("at")}
        for agent, lease in sorted(by_agent.items())
        if lease_fresh(lease, presence_by_agent.get(agent), now,
                       stale_hours=stale_hours, grace_seconds=grace_seconds)
    ]
    vacant = not holders
    vacant_since: Optional[str] = None
    if vacant:
        vacant_since = newest_at or role.get("created_at") or None
    return {
        "holders": holders,
        "vacant": vacant,
        "vacant_since": vacant_since,
        "contested": (role.get("policy") == "exclusive" and len(holders) > 1),
    }


def vacancy_escalation_due(
    role: dict[str, Any], status: dict[str, Any], now: datetime
) -> bool:
    """True iff this role has sat VACANT longer than its ``sla_hours``.

    The escalation predicate behind "vacancy routes to the role's maintainer".
    Mirrors loops._is_overdue's failure discipline: a role with no SLA is
    never due, and garbage off the bus (unparseable ``vacant_since``, a
    non-numeric SLA) fails toward NOT due — an escalation writes a directive,
    and noise must never spam the maintainer's inbox."""
    if not status.get("vacant"):
        return False
    sla = role.get("sla_hours")
    if sla is None:
        return False
    try:
        sla_float = float(sla)
    except (TypeError, ValueError):
        return False
    since = status.get("vacant_since") or ""
    if not since:
        return False
    try:
        dt = datetime.fromisoformat(str(since).replace("Z", "+00:00"))
        if dt.tzinfo is None and getattr(now, "tzinfo", None) is not None:
            dt = dt.replace(tzinfo=now.tzinfo)
        age = (now - dt).total_seconds() / 3600.0
    except Exception:
        return False   # unparseable since: never spam on noise
    return age > sla_float
