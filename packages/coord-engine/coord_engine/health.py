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

from .roles import age_hours

#: A host whose last reconcile is older than this is reported stale.
STALE_HOURS = 24.0
#: Health shards older than this are pruned by reconcile (age-based GC only —
#: no parent-liveness question here, so a plain window is safe).
SHARD_RETENTION_HOURS = 24.0 * 30


def health_prefix(team: str) -> str:
    return f"team/{team}/_coord/health/"


def build_shard(*, host: str, now: str, engine_version: str,
                result: dict[str, Any]) -> dict[str, Any]:
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
    }


def fold(shards: list[dict[str, Any]], *, now: str,
         stale_hours: float = STALE_HOURS) -> dict[str, Any]:
    """Fold host shards into the fleet view: per-host age + stale flag, plus
    rollups (hosts, fresh count, newest pass)."""
    hosts: list[dict[str, Any]] = []
    for s in shards:
        if not isinstance(s, dict) or not s.get("host"):
            continue
        age = age_hours(s.get("at"), now)
        hosts.append({
            "host": str(s["host"]),
            "last_reconcile": s.get("at"),
            "age_hours": None if age == float("inf") else round(age, 2),
            "stale": age > stale_hours,
            "engine_version": s.get("engine_version"),
            "tasks": s.get("tasks"),
            "warnings": s.get("warnings"),
        })
    hosts.sort(key=lambda h: str(h.get("last_reconcile") or ""), reverse=True)
    fresh = [h for h in hosts if not h["stale"]]
    return {
        "hosts": hosts,
        "fresh": len(fresh),
        "total": len(hosts),
        "healthy": bool(fresh),
        "newest": hosts[0]["last_reconcile"] if hosts else None,
    }


def parse_shard(raw: Optional[str]) -> Optional[dict[str, Any]]:
    if not raw:
        return None
    try:
        got = json.loads(raw)
        return got if isinstance(got, dict) else None
    except Exception:
        return None
