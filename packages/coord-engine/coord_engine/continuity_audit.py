"""Continuity-staleness audit — the harness-independent guarantee.

An agent with fresh presence but no fresh continuity snapshot is working
without a recoverable trail. This fold flags them; the flag surfaces in
``coord-engine health`` (both the text output and the ``--json`` payload's
``continuity_stale`` key). Pure function: injected rows + clock, no I/O
(repo convention: prose for judgment, code for folds).
"""
from __future__ import annotations
from datetime import datetime
from typing import Any, Optional


def _hours(now: datetime, ts: datetime) -> float:
    return round((now - ts).total_seconds() / 3600.0, 1)


def stale_agents(
    presence: list[dict[str, Any]],
    snapshots: list[dict[str, Any]],
    now: datetime,
    presence_fresh_hours: int = 24,
    snapshot_stale_hours: int = 24,
) -> list[dict[str, Any]]:
    latest_snap: dict[str, datetime] = {}
    for row in snapshots:
        agent, ts = row["agent"], row["ts"]
        if agent not in latest_snap or ts > latest_snap[agent]:
            latest_snap[agent] = ts

    out: list[dict[str, Any]] = []
    for row in presence:
        agent, ts = row["agent"], row["ts"]
        if _hours(now, ts) > presence_fresh_hours:
            continue  # dead agent: presence problem, not continuity problem
        snap_ts = latest_snap.get(agent)
        snap_age: Optional[float] = _hours(now, snap_ts) if snap_ts else None
        if snap_ts is None or snap_age > snapshot_stale_hours:
            out.append({"agent": agent,
                        "presence_age_h": _hours(now, ts),
                        "snapshot_age_h": snap_age})
    return sorted(out, key=lambda r: r["agent"])
