"""Half-life decay. Pure functions of (signal, now); `now` is always explicit
so compile output is reproducible (the determinism contract in SPEC.md)."""
from __future__ import annotations
from datetime import datetime, timezone
from .schema import Signal

STALE_FACT_DAYS = 180  # undecaying facts older than this get flagged, not dropped


def parse_instant(observed_at: str) -> datetime:
    """observed_at as a tz-aware UTC datetime. Naive timestamps (real
    get-records values, hand-written shards) are treated as UTC so callers can
    compare/sort instants without TypeError on mixed naive/aware input, and so
    chronology is correct across timezone offsets (not lexicographic)."""
    dt = datetime.fromisoformat(observed_at)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _age_days(observed_at: str, now: datetime) -> float:
    observed = datetime.fromisoformat(observed_at)
    # observed_at may arrive tz-naive (real get-records timestamps, a hand-written
    # cache shard) while `now` is tz-aware. Coerce to a common basis as UTC rather
    # than raising TypeError on the subtraction — mirrors consent._active.
    if (observed.tzinfo is None) != (now.tzinfo is None):
        if observed.tzinfo is None:
            observed = observed.replace(tzinfo=timezone.utc)
        else:
            now = now.replace(tzinfo=timezone.utc)
    return (now - observed).total_seconds() / 86400.0


def effective_weight(sig: Signal, now: datetime) -> float:
    if sig.half_life_days is None:
        return sig.strength
    return sig.strength * 2 ** (-_age_days(sig.observed_at, now) / sig.half_life_days)


def is_stale(sig: Signal, now: datetime) -> bool:
    return sig.half_life_days is None and _age_days(sig.observed_at, now) > STALE_FACT_DAYS
