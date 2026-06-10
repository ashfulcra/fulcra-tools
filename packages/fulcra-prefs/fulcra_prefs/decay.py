"""Half-life decay. Pure functions of (signal, now); `now` is always explicit
so compile output is reproducible (the determinism contract in SPEC.md)."""
from __future__ import annotations
from datetime import datetime
from .schema import Signal

STALE_FACT_DAYS = 180  # undecaying facts older than this get flagged, not dropped


def _age_days(observed_at: str, now: datetime) -> float:
    observed = datetime.fromisoformat(observed_at)
    return (now - observed).total_seconds() / 86400.0


def effective_weight(sig: Signal, now: datetime) -> float:
    if sig.half_life_days is None:
        return sig.strength
    return sig.strength * 2 ** (-_age_days(sig.observed_at, now) / sig.half_life_days)


def is_stale(sig: Signal, now: datetime) -> bool:
    return sig.half_life_days is None and _age_days(sig.observed_at, now) > STALE_FACT_DAYS
