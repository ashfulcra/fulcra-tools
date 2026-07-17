"""Role status fold — the deterministic core of the fulcra-agent-roles skill.

A role's status is a fold over multiple lease files' freshness — exactly the
category that must be code, not prose an agent eyeballs (two agents must AGREE
whether a role is vacant before one escalates). Pure functions here; the I/O
wrapper + CLI live in ``cli.py``.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Optional

HELD = "HELD"
VACANT = "VACANT"
CONTESTED = "CONTESTED"
UNKNOWN = "UNKNOWN"
DORMANT = "DORMANT"

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


def parse_sla_hours(value: Any) -> Optional[float]:
    """Fold a role doc's ``sla_hours`` field into the SLA to fold leases under, or
    ``None`` meaning UNKNOWN — the caller must fail closed.

    The distinction this exists to draw, and the reason it is one function rather
    than three call sites (2026-07-16):

    - **absent / blank** (key missing, ``null``, bare ``sla_hours:``, or empty
      string) -> ``DEFAULT_SLA_HOURS``. The field is OPTIONAL, and omitting it is a
      legitimate statement: "the default applies". Substituting the default here is
      honouring an intent, not guessing at one.
    - **explicitly invalid** (``abc``, ``true``, a list, negative, zero, ``inf``,
      ``nan``) -> ``None``, i.e. UNKNOWN. The operator SET this field and it does
      not parse; we cannot know what window they meant, so lease freshness is
      unknowable and no answer about it is honest.

    Until 2026-07-16 all three role surfaces ran ``float(reg.get("sla_hours") or
    DEFAULT_SLA_HOURS)`` under a bare ``except``, which mapped BOTH cases onto the
    default. That is the module's load-bearing rule inverted: an unparseable
    ``sla_hours: abc`` produced a confident, undegraded answer about lease
    freshness (reviewer-reproduced: a lease 36h old under a doc whose real SLA
    might well be 720h folded to ``([], True)`` — a clean "not a holder", no
    ``role-degraded`` marker, silently dropping role-routed work or minting a false
    vacancy). A default is never a substitute for a value someone explicitly set
    and got wrong. Same fact-class as a failed read and a failed parse: we do not
    know, so we say so.

    Non-positive is UNKNOWN rather than honoured-literally on purpose: a 0h or
    negative window makes every lease stale forever, so treating it as intent would
    mint an escalation storm off what is, in practice, always a typo.
    """
    if value is None:
        return DEFAULT_SLA_HOURS
    if isinstance(value, str) and not value.strip():
        return DEFAULT_SLA_HOURS  # blank -> unset -> the default applies
    if isinstance(value, bool):
        return None  # `sla_hours: true` is a stated intent, and not a number
    try:
        sla = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(sla) or sla <= 0:
        return None
    return sla


def dormant_state(dormant_until: Optional[str], *, now: str) -> tuple[bool, bool]:
    """Fold a role doc's ``dormant_until`` into ``(is_dormant, parse_error)``.

    A deliberately-parked role sets ``dormant_until: <ISO>`` on its doc; the
    ENGINE (not agent-side convention) must suppress the mechanical vacancy sweep
    until that date. This is the code half of that decision.

    - absent / None / blank -> ``(False, False)``: current behavior, not parked.
    - ISO ts in the FUTURE  -> ``(True, False)``:  dormant, suppress escalation.
    - ISO ts in the PAST    -> ``(False, False)``: park elapsed, resume normally.
    - unparseable garbage    -> ``(False, True)``:  fail OPEN toward escalation and
      report the error, so a typo can never silently suppress an escalation
      (the safe direction HERE, since dormancy is what SUPPRESSES).
    """
    if dormant_until is None:
        return (False, False)
    raw = str(dormant_until).strip()
    if not raw:
        return (False, False)
    until = _parse(raw)
    if until is None:
        return (False, True)  # garbage: absent + error, fail open toward escalation
    n = _parse(now)
    if n is None:
        return (False, False)
    return (until > n, False)


def escalation_due(
    leases: Optional[list[dict[str, Any]]],
    *,
    now: str,
    sla_hours: float = DEFAULT_SLA_HOURS,
    marker_exists_today: bool = False,
    dormant: bool = False,
) -> bool:
    """Engine DECIDES escalation (the SKILL prose ACTS): true iff the role is
    vacant past its SLA, not deliberately parked (``dormant``), and today's dedupe
    marker isn't already present."""
    if dormant or marker_exists_today:
        return False
    return classify(leases, now=now, sla_hours=sla_hours) == VACANT
