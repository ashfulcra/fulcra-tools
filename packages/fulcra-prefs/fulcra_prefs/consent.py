"""Consent enforcement at the export boundary. Filtering happens at `get
--for <audience>` time (not at storage time) so revoking a grant immediately
affects the next export. Every export is itself a consent-kind signal — the
disclosure log IS the Privacy Ledger."""
from __future__ import annotations
from datetime import datetime, timezone
from fnmatch import fnmatch
from .schema import Signal, temp_signal_id


def _active(grant: dict, audience: str, now: datetime) -> bool:
    # Grants are raw dicts with no schema validation; a legacy/partial grant
    # missing 'audience' must read as inactive, never raise.
    if grant.get("audience") != audience:
        return False
    exp = grant.get("expires")
    if exp is None:
        return True
    exp_dt = datetime.fromisoformat(exp)
    # Either side may arrive tz-naive (a user-supplied expires string, or a
    # caller passing datetime.now() without a tz). Coerce both to a common UTC
    # basis rather than raising TypeError on the comparison -- mirrors
    # decay._age_days.
    if exp_dt.tzinfo is None:
        exp_dt = exp_dt.replace(tzinfo=timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return exp_dt > now


# Capability ladder: a higher level satisfies every purpose at or below it.
# 'read' = may be read/displayed; 'solve' = may also feed group decisions.
_LEVEL_RANK = {"read": 0, "solve": 1}


def _level_allows(grant_level: object, purpose: str) -> bool:
    # A grant satisfies a purpose when its level ranks >= the purpose. An unknown
    # or missing grant level reads as 'read' (the floor) so a legacy/partial grant
    # never confers solve capability.
    have = _LEVEL_RANK.get(grant_level if isinstance(grant_level, str) else "", 0)
    return have >= _LEVEL_RANK[purpose]


def filter_for_audience(doc: dict, grants: list[dict], audience: str,
                        now: datetime, *, purpose: str = "read") -> dict:
    if purpose not in _LEVEL_RANK:
        raise ValueError(f"purpose must be one of {sorted(_LEVEL_RANK)}, got {purpose!r}")
    live = [g for g in grants if _active(g, audience, now)]
    # A key is exposed only if some active grant both matches its glob AND carries
    # a level that permits this purpose — so a read-only grant can't feed the solver.
    keys = {k: v for k, v in doc.get("keys", {}).items()
            if any(fnmatch(k, g["key_glob"]) for g in live
                   if g.get("key_glob") and _level_allows(g.get("level"), purpose))}
    # fnmatch '*' crosses dots: 'dining.*' matches all depths; skip grants w/o key_glob.
    return {**doc, "keys": keys}


def disclosure_signal(shared_keys: list[str], audience: str, platform: str,
                      now: datetime) -> Signal:
    observed = now.isoformat()
    key = f"consent.disclosure.{audience}"
    value = {"keys": sorted(shared_keys), "audience": audience}
    return Signal(
        id=temp_signal_id(key, observed, platform, value),
        kind="consent", key=key, scope="global",
        value=value,
        strength=1.0, confidence=1.0, half_life_days=None,
        observed_at=observed, platform=platform, agent=None, session=None,
        supersedes=None,
    )
