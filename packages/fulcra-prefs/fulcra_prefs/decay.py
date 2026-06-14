"""Half-life decay. Pure functions of (signal, now); `now` is always explicit
so compile output is reproducible (the determinism contract in SPEC.md)."""
from __future__ import annotations
from datetime import datetime, timezone
from .schema import Signal

STALE_FACT_DAYS = 180  # undecaying facts older than this get flagged, not dropped


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
    # Clamp age at 0: a future-dated observed_at (clock skew) would otherwise
    # give 2**(+x) > 1 and amplify the weight ABOVE strength. A not-yet-aged
    # signal decays toward, at most equal to, its strength.
    age = max(0.0, _age_days(sig.observed_at, now))
    return sig.strength * 2 ** (-age / sig.half_life_days)


def is_stale(sig: Signal, now: datetime) -> bool:
    return sig.half_life_days is None and _age_days(sig.observed_at, now) > STALE_FACT_DAYS
