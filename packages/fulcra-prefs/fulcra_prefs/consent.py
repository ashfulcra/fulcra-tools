"""Consent enforcement at the export boundary. Filtering happens at `get
--for <audience>` time (not at storage time) so revoking a grant immediately
affects the next export. Every export is itself a consent-kind signal — the
disclosure log IS the Privacy Ledger."""
from __future__ import annotations
from datetime import datetime, timezone
from fnmatch import fnmatch
from .schema import Signal, temp_signal_id


def _active(grant: dict, audience: str, now: datetime) -> bool:
    if grant["audience"] != audience:
        return False
    exp = grant.get("expires")
    if exp is None:
        return True
    exp_dt = datetime.fromisoformat(exp)
    if exp_dt.tzinfo is None:
        # User-supplied expires strings may be naive; treat them as UTC
        # rather than raising TypeError against the tz-aware `now`.
        exp_dt = exp_dt.replace(tzinfo=timezone.utc)
    return exp_dt > now


def filter_for_audience(doc: dict, grants: list[dict], audience: str,
                        now: datetime) -> dict:
    live = [g for g in grants if _active(g, audience, now)]
    # NOTE: grant 'level' (read|solve) is recorded but not yet enforced anywhere; enforcement arrives with cross-user sharing post-v1.
    keys = {k: v for k, v in doc.get("keys", {}).items()
            if any(fnmatch(k, g["key_glob"]) for g in live)}  # fnmatch '*' crosses dots: 'dining.*' matches all depths
    return {**doc, "keys": keys}


def disclosure_signal(shared_keys: list[str], audience: str, platform: str,
                      now: datetime) -> Signal:
    observed = now.isoformat()
    key = f"consent.disclosure.{audience}"
    return Signal(
        id=temp_signal_id(key, observed, platform),
        kind="consent", key=key, scope="global",
        value={"keys": sorted(shared_keys), "audience": audience},
        strength=1.0, confidence=1.0, half_life_days=None,
        observed_at=observed, platform=platform, agent=None, session=None,
        supersedes=None,
    )
