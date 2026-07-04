"""Presence — roster + liveness fold for the fulcra-agent-presence skill.

Each agent writes a presence shard ``presence/<agent>.md`` (a heartbeat: who I am,
what workstreams I'm on, one-line summary, timestamp). Folding shards into a
roster with liveness — and supplying the broadcast roster for directives — is
deterministic code. Writing your own shard is a single-file action the CLI wraps.

Liveness (mirrors the incumbent's presence fold):
- ``live``  — beat within ``live_hours``  (default 1h)
- ``idle``  — beat within ``stale_hours`` (default 24h)
- ``stale`` — older than ``stale_hours`` (kept in roster, excluded from the
  broadcast roster; shard files are NOT garbage-collected — reconcile's GC
  covers ack and health shards only)
"""

from __future__ import annotations

from typing import Any, Optional

from .model import OPEN_STATUSES
from .roles import age_hours

LIVE_HOURS = 1.0
STALE_HOURS = 24.0


def classify(ts: Optional[str], *, now: str, live_hours: float = LIVE_HOURS,
             stale_hours: float = STALE_HOURS) -> str:
    age = age_hours(ts, now)
    if age <= live_hours:
        return "live"
    if age <= stale_hours:
        return "idle"
    return "stale"


def roster(
    shards: list[dict[str, Any]], *, now: str,
    live_hours: float = LIVE_HOURS, stale_hours: float = STALE_HOURS,
) -> list[dict[str, Any]]:
    """Fold presence shards into a deterministic roster (sorted by agent)."""
    out: list[dict[str, Any]] = []
    for s in shards:
        if not isinstance(s, dict) or not s.get("agent"):
            continue
        ws = s.get("workstreams")
        if not isinstance(ws, list):
            ws = [ws] if ws else []
        out.append({
            "agent": str(s["agent"]),
            "workstreams": [str(w) for w in ws],
            "summary": s.get("summary") or "",
            "last_seen": s.get("timestamp"),
            "liveness": classify(s.get("timestamp"), now=now,
                                 live_hours=live_hours, stale_hours=stale_hours),
        })
    return sorted(out, key=lambda r: r["agent"])


def broadcast_roster(shards: list[dict[str, Any]], *, now: str,
                     stale_hours: float = STALE_HOURS) -> list[str]:
    """Agents a ``*`` directive must reach: everyone not stale. The A2 inbox
    fold uses this to decide when a broadcast is fully acked."""
    return [r["agent"] for r in roster(shards, now=now, stale_hours=stale_hours)
            if r["liveness"] != "stale"]


def agents_digest(
    rows: list[dict[str, Any]], shards: list[dict[str, Any]], *, now: str,
) -> list[dict[str, Any]]:
    """Cross-agent digest: per agent (from presence ∪ task owners/assignees),
    their liveness + open-task counts by status. Pure fold over aggregate rows +
    presence shards."""
    ros = {r["agent"]: r for r in roster(shards, now=now)}
    names = set(ros)
    open_rows = [r for r in rows if r.get("status") in OPEN_STATUSES]
    for row in open_rows:
        for key in ("owner", "assignee"):
            v = row.get(key)
            if v and v != "*":
                names.add(str(v))
    out: list[dict[str, Any]] = []
    for name in sorted(names):
        agent_open_rows = [r for r in open_rows
                           if r.get("owner") == name or r.get("assignee") == name]
        counts: dict[str, int] = {}
        for r in agent_open_rows:
            counts[str(r.get("status"))] = counts.get(str(r.get("status")), 0) + 1
        pres = ros.get(name)
        out.append({
            "agent": name,
            "liveness": pres["liveness"] if pres else "unknown",
            "summary": pres["summary"] if pres else "",
            "workstreams": pres["workstreams"] if pres else [],
            "open": counts,
        })
    return out
