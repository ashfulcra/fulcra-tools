"""Role status fold — the deterministic core of the fulcra-agent-roles skill.

A role's status is a fold over multiple lease files' freshness — exactly the
category that must be code, not prose an agent eyeballs (two agents must AGREE
whether a role is vacant before one escalates). Pure functions here; the I/O
wrapper + CLI live in ``cli.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

HELD = "HELD"
VACANT = "VACANT"
CONTESTED = "CONTESTED"
UNKNOWN = "UNKNOWN"

DEFAULT_SLA_HOURS = 24.0


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def age_hours(ts: Optional[str], now: Optional[str]) -> float:
    """Hours between ``ts`` and ``now`` (both ISO-8601). ``inf`` if unparseable."""
    a, n = _parse(ts), _parse(now)
    if a is None or n is None:
        return float("inf")
    return (n - a).total_seconds() / 3600.0


def fresh_holders(
    leases: list[dict[str, Any]], *, now: str, sla_hours: float
) -> list[dict[str, Any]]:
    """Leases whose ``timestamp`` is within ``sla_hours`` of ``now``."""
    return [
        l for l in leases
        if isinstance(l, dict) and age_hours(l.get("timestamp"), now) <= sla_hours
    ]


def classify(
    leases: Optional[list[dict[str, Any]]],
    *,
    now: str,
    sla_hours: float = DEFAULT_SLA_HOURS,
    policy: str = "shared",
) -> str:
    """Fold lease freshness into HELD / VACANT / CONTESTED / UNKNOWN.

    - UNKNOWN: leases could not be read (None).
    - CONTESTED: policy is ``exclusive`` and two or more holders are fresh.
    - HELD: at least one fresh holder.
    - VACANT: no fresh holder.
    """
    if leases is None:
        return UNKNOWN
    fresh = fresh_holders(leases, now=now, sla_hours=sla_hours)
    if policy == "exclusive" and len(fresh) >= 2:
        return CONTESTED
    return HELD if fresh else VACANT


def escalation_due(
    leases: Optional[list[dict[str, Any]]],
    *,
    now: str,
    sla_hours: float = DEFAULT_SLA_HOURS,
    marker_exists_today: bool = False,
) -> bool:
    """Engine DECIDES escalation (the SKILL prose ACTS): true iff the role is
    vacant past its SLA and today's dedupe marker isn't already present."""
    if marker_exists_today:
        return False
    return classify(leases, now=now, sla_hours=sla_hours) == VACANT
