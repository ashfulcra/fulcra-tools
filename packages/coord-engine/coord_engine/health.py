"""Fleet health — per-host reconcile shards + the health fold (fulcra-agent-health).

Every reconcile pass writes a small health shard ``_coord/health/<host-key>.json``
(who reconciled, when, how it went). The ``health`` fold answers the fleet
question the incumbent's health command did — *which hosts are keeping this team
healed, and who has gone dark* — deterministically. ``doctor`` is the local
preflight (tooling + store reachability) run before trusting automation.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from . import pin_currency
from .roles import age_hours

#: A host whose last reconcile is older than this is reported stale.
STALE_HOURS = 24.0
#: Health shards older than this are pruned by reconcile (age-based GC only —
#: no parent-liveness question here, so a plain window is safe).
SHARD_RETENTION_HOURS = 24.0 * 30


def health_prefix(team: str) -> str:
    return f"team/{team}/_coord/health/"


def build_shard(*, host: str, now: str, engine_version: str,
                result: dict[str, Any],
                engine_build: Optional[str] = None) -> dict[str, Any]:
    """One host's reconcile beat.

    ``advanced`` is the fact the shard used to omit, and omitting it is how a
    frozen team read as green: EVERY reconcile exit writes a shard, including
    the aborts, which return early with ``degraded: True`` and a ``reason`` while
    preserving the current generation. A shard carrying only ``warnings`` as a
    COUNT cannot tell a reader which happened, so ``fresh`` meant no more than
    "this host wrote a file recently".

    In the RESULT dict, an absent ``degraded`` key means the pass succeeded --
    the success call site passes only tasks/parsed/reused/warnings. In the
    SHARD, absence means something entirely different (a writer too old to
    record it), so this always writes the key explicitly and ``fold`` reads a
    missing one as UNKNOWN.

    Known imprecision, stated rather than hidden: a pass whose generation was
    refused on inventory sections only still writes ``summaries.json`` as the
    compatibility cache, and its shard call site does not receive that reason,
    so it reports ``advanced: True`` with its warnings. "Advanced" here means
    "was not aborted and wrote the index", not "published a clean generation".

    ``engine_build`` is the VCS commit this engine was installed from
    (:func:`coord_engine.pin_currency.build_sha`). ``engine_version`` cannot
    stand in for it: ``__version__`` changes only on a release, so hosts running
    different commits can report the same version and no reader could tell
    which of them matched the fleet pin. The key is always written; ``None`` means the build is
    unknowable on that host (editable or non-VCS install), never "current".
    """
    return {
        "schema": "coord.teams.health.v1",
        "host": host,
        "at": now,
        "engine_version": engine_version,
        "tasks": result.get("tasks"),
        "parsed": result.get("parsed"),
        "reused": result.get("reused"),
        "warnings": len(result.get("warnings") or []),
        "fast_path": bool(result.get("fast_path")),
        "advanced": not bool(result.get("degraded")),
        "degraded_reason": result.get("reason") if result.get("degraded") else None,
        "engine_build": engine_build,
    }


def _valid_build(raw: Any) -> Optional[str]:
    """A shard's ``engine_build`` if it is a usable sha, else None (UNKNOWN)."""
    if not isinstance(raw, str):
        return None
    sha = raw.strip().lower()
    if len(sha) < pin_currency.MIN_SHA_PREFIX or any(
            c not in "0123456789abcdef" for c in sha):
        return None
    return sha


def fold(shards: list[dict[str, Any]], *, now: str,
         stale_hours: float = STALE_HOURS,
         pin: Optional[str] = None,
         pin_status: str = "error") -> dict[str, Any]:
    """Fold host shards into the fleet view: per-host age + stale flag, plus
    rollups (hosts, fresh count, newest pass).

    ``pin``/``pin_status`` are the fleet pin as :func:`pin_currency.load_pin`
    read it. Each host gets a ``pin_verdict`` from :func:`pin_currency.classify`:
    ``current`` only when the shard names a build that matches a readable pin.
    A shard with no ``engine_build`` (a writer older than the field, or a
    non-VCS install) is ``unknown-build``; an unreadable pin makes every host
    ``unknown-pin``. Neither UNKNOWN is ever counted as current."""
    hosts: list[dict[str, Any]] = []
    for s in shards:
        if not isinstance(s, dict) or not s.get("host"):
            continue
        age = age_hours(s.get("at"), now)
        # A missing `advanced` key is a shard from a writer that predates the
        # field. That is UNKNOWN, never True: a missing field is not evidence
        # that a pass succeeded, and during a rollout a pre-upgrade host must
        # read as unknown rather than either accused or excused.
        raw_advanced = s.get("advanced")
        hosts.append({
            "host": str(s["host"]),
            "last_reconcile": s.get("at"),
            "age_hours": None if age == float("inf") else round(age, 2),
            "stale": age > stale_hours,
            "engine_version": s.get("engine_version"),
            "tasks": s.get("tasks"),
            "warnings": s.get("warnings"),
            "advanced": raw_advanced if isinstance(raw_advanced, bool) else None,
            "degraded_reason": s.get("degraded_reason"),
            "engine_build": _valid_build(s.get("engine_build")),
        })
        hosts[-1]["pin_verdict"] = pin_currency.classify(
            hosts[-1]["engine_build"], pin, pin_status)
    hosts.sort(key=lambda h: str(h.get("last_reconcile") or ""), reverse=True)
    fresh = [h for h in hosts if not h["stale"]]
    # The advance rollups answer "is the index moving NOW", so only fresh hosts
    # count. `fresh`, `stale` and `healthy` keep their existing meaning exactly:
    # redefining them would shift every reader at once, and whether an
    # all-aborting fleet should flip `healthy` (and so the exit code) is a
    # deliberate open question rather than something this fold decides.
    return {
        "hosts": hosts,
        "fresh": len(fresh),
        "total": len(hosts),
        "healthy": bool(fresh),
        "newest": hosts[0]["last_reconcile"] if hosts else None,
        "advancing": sum(1 for h in fresh if h["advanced"] is True),
        "aborting": sum(1 for h in fresh if h["advanced"] is False),
        "unknown_advance": sum(1 for h in fresh if h["advanced"] is None),
        # Build currency of the FRESH hosts, against the pin read for this fold.
        "fleet_pin": pin if pin_status == "ok" else None,
        "pin_status": pin_status,
        "at_pin": sum(1 for h in fresh if h["pin_verdict"] == "current"),
        "off_pin": sum(1 for h in fresh if h["pin_verdict"] == "mismatch"),
        "unknown_build": sum(1 for h in fresh if h["pin_verdict"] == "unknown-build"),
    }


def parse_shard(raw: Optional[str]) -> Optional[dict[str, Any]]:
    if not raw:
        return None
    try:
        got = json.loads(raw)
        return got if isinstance(got, dict) else None
    except Exception:
        return None
