"""Is a source still producing? — the question run-status cannot answer.

``PluginState`` records whether a plugin RAN: ``last_run``, ``last_outcome``,
``consecutive_failures``. None of that says whether the run collected anything.
A plugin whose upstream has gone quiet still executes, still exits cleanly, and
still writes ``last_outcome="done"`` with ``consecutive_failures=0`` — forever.
Every health signal stays green while the data stops. That is not hypothetical:
a media source stopped producing for four days and no check in the system went
red, because none of them were looking at the data.

Liveness of a collector is not freshness of its data. This module supplies the
missing half.

Two INDEPENDENT clocks
----------------------
Tracking only one of these leaves the other failure wide open, so both exist:

``last_yield_at`` (daemon clock)
    When the plugin last accepted at least one record. Catches "runs fine,
    produces nothing" — the outage above.

``newest_item_at`` (source clock)
    The newest SOURCE timestamp the plugin has ever accepted. Catches the
    sibling failure: a plugin that keeps writing while upstream is frozen —
    re-emitting the same stale item, or emitting heartbeat/backfill rows.
    ``last_yield_at`` advances the whole time, so a yield-only check calls that
    healthy. It is not: no new information has arrived.

Deliberate non-goals
--------------------
**Expectations are opt-in.** A plugin without a ``FreshnessExpectation`` reports
``NOT_CONFIGURED`` and never alerts. Guessing a bound from ``default_interval``
would fire constantly on legitimately rare sources — a lab importer, a manual
upload, an event source that is silent for weeks by nature. An alert that cries
wolf on healthy sources is worse than no alert, because it teaches people to
ignore the one that matters.

**Absence is UNKNOWN, not healthy and not stale.** A plugin that has never
yielded has no evidence either way. Reporting UNKNOWN keeps "we have not looked"
distinguishable from "we looked and it is fine" — the distinction whose loss
caused the outage in the first place.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum


class Freshness(str, Enum):
    """Verdict for one source. ``str`` mixin so it serializes as a plain
    string into the health JSON without a custom encoder."""

    FRESH = "fresh"
    STALE = "stale"
    UNKNOWN = "unknown"
    NOT_CONFIGURED = "not_configured"


@dataclass(frozen=True)
class FreshnessExpectation:
    """How long this source may be quiet before silence means something.

    Both bounds are optional so a plugin can declare only the one that is
    meaningful for it. A source with no natural cadence should declare neither
    and stay ``NOT_CONFIGURED`` rather than being given a guessed bound.
    """

    #: How long the plugin may accept NOTHING before that is a fault.
    max_yield_silence: timedelta | None = None
    #: How old the newest accepted SOURCE item may be before upstream is
    #: presumed stalled. Set this above the source's natural quiet periods
    #: (overnight, weekends) or it will alert on normal behaviour.
    max_upstream_lag: timedelta | None = None

    def __post_init__(self) -> None:
        for name in ("max_yield_silence", "max_upstream_lag"):
            value = getattr(self, name)
            if value is not None and value <= timedelta(0):
                raise ValueError(f"{name} must be positive, got {value!r}")

    @property
    def is_configured(self) -> bool:
        return self.max_yield_silence is not None or self.max_upstream_lag is not None


@dataclass(frozen=True)
class FreshnessReport:
    """What the health surface shows for one plugin."""

    plugin_id: str
    state: Freshness
    summary: str
    yield_age: timedelta | None = None
    upstream_age: timedelta | None = None
    #: True when a source timestamp is in the future beyond tolerance. The age
    #: is then not trustworthy, so the verdict degrades to UNKNOWN rather than
    #: letting a bad clock read as permanently fresh.
    clock_skew: bool = False

    @property
    def is_alerting(self) -> bool:
        return self.state is Freshness.STALE


#: Small allowance for clock differences between this host and the source.
#: Below this, a future timestamp is treated as "now"; above it, the source
#: clock is untrustworthy and the verdict becomes UNKNOWN.
CLOCK_SKEW_TOLERANCE = timedelta(minutes=5)


def parse_instant(value: str | datetime | None) -> datetime | None:
    """Parse a source timestamp into an aware instant, or None if unusable.

    ISO strings must never be compared as STRINGS. Offsets make lexical order
    diverge from chronological order: ``2026-08-08T12:00:00+05:00`` is 07:00Z,
    genuinely EARLIER than ``2026-08-08T08:00:00+00:00``, yet sorts after it.
    A watermark maintained by string comparison therefore moves backwards in
    real time — destroying the monotonic guarantee it exists to provide — and
    unparseable junk sorts above every real timestamp, freezing the watermark
    permanently. Parse, then compare instants.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None
    # A naive timestamp is assumed UTC: state rows written before timestamps
    # carried offsets must keep working rather than crash the daemon loop.
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def to_canonical_utc(value: str | datetime | None) -> str | None:
    """Normalize to a UTC ISO string, so stored watermarks are directly
    comparable and no future reader has to re-derive the offset."""
    dt = parse_instant(value)
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def _age(now: datetime, then: datetime | None) -> timedelta | None:
    instant = parse_instant(then)
    return None if instant is None else now - instant


def assess(
    *,
    plugin_id: str,
    now: datetime,
    expectation: FreshnessExpectation | None,
    last_yield_at: datetime | None,
    newest_item_at: datetime | None = None,
) -> FreshnessReport:
    """Judge one source's freshness.

    ``now`` is injected rather than read from the clock so this stays pure and
    testable; callers pass ``datetime.now(timezone.utc)``.

    Note what is deliberately absent from the signature: ``last_outcome``. A
    clean run is not evidence of freshness, and letting ``done`` participate
    here would reintroduce the exact confusion this module exists to remove.
    """
    if expectation is None or not expectation.is_configured:
        return FreshnessReport(
            plugin_id=plugin_id,
            state=Freshness.NOT_CONFIGURED,
            summary="no freshness expectation declared — not monitored",
        )

    yield_age = _age(now, last_yield_at)
    upstream_age = _age(now, newest_item_at)

    # EVERY configured dimension is evaluated before any verdict is formed.
    # Returning early from inside a dimension let an UNKNOWN in one erase a
    # breach PROVEN by the other — a two-day-stale source with no yield clock
    # reported UNKNOWN and would not have alerted. Either bound alone is
    # sufficient to prove staleness, so a proof must outrank an absence.
    breaches: list[str] = []
    unknowns: list[str] = []
    skewed = False

    def _judge(age: timedelta | None, bound: timedelta | None,
               missing: str, breach: str) -> timedelta | None:
        """Assess one dimension. Returns the age, clamped."""
        nonlocal skewed
        if bound is None:
            return age
        if age is None:
            unknowns.append(missing)
            return None
        if age < -CLOCK_SKEW_TOLERANCE:
            # A timestamp in the future makes THIS dimension's age
            # meaningless, and would otherwise make a stalled source look
            # permanently fresh. It is not licence to ignore the other
            # dimension.
            skewed = True
            unknowns.append("timestamp is in the future — clock skew")
            return age
        age = max(age, timedelta(0))    # tolerate sub-tolerance skew
        if age > bound:
            breaches.append(breach.format(age=_humanize(age),
                                          limit=_humanize(bound)))
        return age

    yield_age = _judge(
        yield_age, expectation.max_yield_silence,
        "has never accepted a record",
        "collected nothing for {age} (limit {limit})",
    )
    upstream_age = _judge(
        upstream_age, expectation.max_upstream_lag,
        "no source timestamp recorded yet",
        "newest item is {age} old (limit {limit})",
    )

    if breaches:
        return FreshnessReport(
            plugin_id=plugin_id,
            state=Freshness.STALE,
            summary="; ".join(breaches),
            yield_age=yield_age,
            upstream_age=upstream_age,
            clock_skew=skewed,
        )

    if unknowns:
        return FreshnessReport(
            plugin_id=plugin_id,
            state=Freshness.UNKNOWN,
            summary="; ".join(unknowns) + " — nothing proven either way",
            yield_age=yield_age,
            upstream_age=upstream_age,
            clock_skew=skewed,
        )

    return FreshnessReport(
        plugin_id=plugin_id,
        state=Freshness.FRESH,
        summary="producing within expectation",
        yield_age=yield_age,
        upstream_age=upstream_age,
    )


def _humanize(delta: timedelta) -> str:
    """Compact age for operator-facing summaries ("3d 4h", "12m")."""
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"
