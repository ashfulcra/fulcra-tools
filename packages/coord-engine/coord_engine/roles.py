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
#: The role HAS a holder whose lease has gone stale. Distinct from VACANT on
#: purpose: until 2026-08-07 both folded to VACANT, so `roles status` reported
#: ``{"status": "VACANT", "holders": ["codex-reviewer"]}`` — a claim the same
#: JSON object contradicts — and the vacancy alarm read "nobody is doing this
#: job" while codex-reviewer was filing exact-head verdicts hourly. The record
#: already knew who held the role; the classifier discarded it.
#:
#: "The lease lapsed" and "nobody holds this" are different facts and need
#: different names. Answering the second when you only measured the first is
#: how a responsive agent gets escalated to the operator as absent.
LAPSED = "LAPSED"
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
    """Fold lease freshness into HELD / LAPSED / VACANT / CONTESTED / UNKNOWN.

    - UNKNOWN: leases could not be read (None).
    - CONTESTED: policy is ``exclusive`` and two or more holders are fresh.
    - HELD: at least one fresh holder.
    - LAPSED: holders exist, none fresh — the lease went stale, somebody still
      holds the role.
    - VACANT: no holders at all.

    LAPSED vs VACANT is the whole point (see the constant). This function only
    ever measures lease FRESHNESS; it cannot see whether the work is being done.
    Reporting VACANT for a role with a named holder claimed something it had not
    measured, and the alarm built on it escalated a working reviewer to the
    operator as unattended for four days.
    """
    if leases is None:
        return UNKNOWN
    fresh = fresh_holders(leases, now=now, sla_hours=sla_hours)
    if policy == "exclusive" and len(fresh) >= 2:
        return CONTESTED
    if fresh:
        return HELD
    return LAPSED if leases else VACANT


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
    attended: Optional[bool] = None,
) -> bool:
    """Engine DECIDES escalation (the SKILL prose ACTS): true iff the role is
    vacant past its SLA, not deliberately parked (``dormant``), today's dedupe
    marker isn't already present, and the role is not demonstrably being served.

    ``attended`` is TRI-STATE and the distinction is the point:

    * ``True``  — a role holder produced work inside the SLA window. The lease
      lapsed; the JOB did not. Escalating "unattended" here is false.
    * ``False`` — checked, and no work product found. A real vacancy.
    * ``None``  — NOT CHECKED (the default). Escalate, because an unchecked
      window is not evidence of absence — but callers must say "attendance not
      checked" rather than assert nobody is working.

    WHY THIS EXISTS. Escalation used to key on lease timestamps alone, so the
    predicate was "has a lease been renewed lately" while the alarm it raised
    read "is anybody doing this job." Those diverged for four days:
    codex-reviewer's lease went stale while it filed verdicts hourly, and the
    detector filed a P1 per role per day, every one of them false. A standing
    alarm that is always wrong is worse than no alarm — it trains readers to
    ignore the one that is real."""
    if dormant or marker_exists_today or attended is True:
        return False
    # BOTH lapsed-and-unheld states escalate, deliberately. The 2026-08-07 split
    # of VACANT into LAPSED/VACANT is about honest REPORTING — the JSON no longer
    # says "VACANT" while naming a holder — and it must not quietly change WHO
    # gets alarmed on. A stale lease on a held role is still an SLA breach
    # somebody has to answer for; whether it *should* alarm differently is a
    # separate ruling with a real cost, not a side effect of renaming a state.
    return classify(leases, now=now, sla_hours=sla_hours) in (VACANT, LAPSED)


#: Title prefix of the ROLE VACANT family. The slug family is a CONTRACT (dedupe
#: key, existing queries), so this parses the contract rather than replacing it.
VACANCY_TITLE_PREFIX = "ROLE VACANT "


def vacancy_role_of(title: str) -> Optional[str]:
    """The role a ``ROLE VACANT`` title is about, or None if it is not one.

    Title shape, both variants::

        ROLE VACANT <date>: <role> lease lapsed past <sla>h SLA (attendance UNVERIFIED)
        ROLE VACANT <date>: <role> UNATTENDED past <sla>h SLA — no holder work found

    The role is the first whitespace-delimited token after ``": "``. Role names
    contain hyphens but never spaces, so this is EXACT — deliberately not a
    prefix or substring test. A prefix test would let ``codex-reviewer``
    suppress ``codex-reviewer-2``; slug-prefix collisions have silently dropped
    messages on this bus before, and an identity transform used to SUPPRESS an
    alarm has to be injective or it hides the alarm it was not asked to hide."""
    if not isinstance(title, str) or not title.startswith(VACANCY_TITLE_PREFIX):
        return None
    _, sep, rest = title.partition(": ")
    if not sep or not rest:
        return None
    return rest.split(" ", 1)[0] or None


#: Slug form of the same family, as `tasks.new_task_doc` renders it:
#: ``role-vacant-<yyyy>-<mm>-<dd>-<role>-lease-lapsed-...`` / ``-unattended-...``
VACANCY_SLUG_PREFIX = "role-vacant-"
_VACANCY_SLUG_MARKERS = ("-lease-lapsed-", "-unattended-")


def vacancy_role_of_slug(slug: str) -> Optional[str]:
    """The role a ROLE VACANT *slug* is about, or None if it is not one.

    The mint path can enumerate the task directory cheaply but would have to
    READ every document to see titles, so the guard has to recognise the slug
    form too. Fixing only the title form would leave the sibling broken in the
    one place the guard actually runs.

    Role names are already lowercase and hyphenated, so they survive
    slugification unchanged; the role is the span between the date and the
    first family marker. Same injectivity requirement as ``vacancy_role_of``:
    an exact span, never a prefix test."""
    if not isinstance(slug, str) or not slug.startswith(VACANCY_SLUG_PREFIX):
        return None
    rest = slug[len(VACANCY_SLUG_PREFIX):]
    # strip "yyyy-mm-dd-"
    if len(rest) < 11 or rest[4] != "-" or rest[7] != "-" or rest[10] != "-":
        return None
    rest = rest[11:]
    cut = min((i for i in (rest.find(m) for m in _VACANCY_SLUG_MARKERS)
               if i > 0), default=-1)
    if cut <= 0:
        return None
    return rest[:cut] or None


def _vacancy_role(identifier: str) -> Optional[str]:
    """Role behind either representation. Unrecognised input is None, never a
    guess — a wrong guess here SUPPRESSES an alarm."""
    return vacancy_role_of(identifier) or vacancy_role_of_slug(identifier)


def vacancy_already_open(open_titles: list[str], role: str) -> bool:
    """True iff a vacancy row for exactly ``role`` is already open.

    STATE-CHANGE TRIGGERING (coord-boss ruling, 2026-09-02). The vacancy title
    embeds the date, so every sweep mints a NEW slug and cli's existing
    ``transport.read(dst) is None`` guard can never match across days;
    ``marker_exists_today`` suppresses only WITHIN a day. Measured consequence:
    117 open rows carrying 12 distinct facts, growing 2-6 rows/day fleetwide.
    A standing alarm that restates itself daily trains readers to ignore it —
    the same failure the ``attended`` tri-state was added to prevent.

    This does NOT silence the first notice for a role. It silences the second
    and later restatements of a fact already on the board.

    ``open_titles`` of None RAISES. A listing that could not be read is UNKNOWN,
    and rendering UNKNOWN as "nothing is open" is precisely the decision that
    mints a row — the caller must confirm absence with a listing that fails
    loudly, never with a falsy read."""
    if open_titles is None:
        raise ValueError(
            "vacancy_already_open: open_titles is None (UNKNOWN). Refusing to "
            "treat an unreadable listing as an empty board — confirm absence "
            "with a raising list, not a falsy read.")
    return any(_vacancy_role(t) == role for t in open_titles)
