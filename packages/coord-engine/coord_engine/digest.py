"""Operator digest — the situational-awareness fold (fulcra-agent-health, A5b).

Answers the incumbent digest's four questions from the aggregate + presence:
what's blocked on YOU, what's upcoming, what each agent is doing, what's stale.
Pure fold; the CLI renders it and (optionally) persists it to the team store.

Timeline annotation note: the incumbent wrote digests to the Fulcra timeline and
grew DUPLICATE data types from racy check-then-create (operator bug report).
coord defers the timeline write until the record-write CLI surface is verified
(research-before-building); `--store` persists the digest durably on the team
store instead, deduped per day+window.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .model import OPEN_STATUSES, sort_rows
from .roles import age_hours

#: An open task untouched for longer than this shows in the "stale" section.
STALE_TASK_HOURS = 48.0
#: "Upcoming" = not_before within this many days from now.
UPCOMING_DAYS = 7.0


def _blocked_on_human(value: Any, human: str) -> bool:
    if value is None:
        return False
    if isinstance(value, list):
        return any(str(v).strip() == human for v in value)
    tokens = str(value).replace(",", " ").split()
    return human in tokens


def _plus_days(now: str, days: float) -> str:
    try:
        base = datetime.fromisoformat(now.replace("Z", "+00:00"))
    except ValueError:
        return now
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return (base + timedelta(days=days)).isoformat().replace("+00:00", "Z")


def build(
    rows: list[dict[str, Any]],
    presence_shards: list[dict[str, Any]],
    *,
    now: str,
    human: str = "human",
) -> dict[str, Any]:
    """The four digest sections. Tolerates absent add-ons: with no presence
    shards, per_agent degrades to task parties; acks/needs:human tags simply
    don't appear if directives aren't used."""
    from . import presence as presence_mod

    open_rows = [r for r in rows if r.get("status") in OPEN_STATUSES]
    horizon = _plus_days(now, UPCOMING_DAYS)

    blocked_on_you = sort_rows([
        r for r in open_rows
        if "needs:human" in (r.get("tags") or []) or r.get("assignee") == human
        or _blocked_on_human(r.get("blocked_on"), human)
    ])
    upcoming = sort_rows([
        r for r in open_rows
        if r.get("not_before") and now < str(r.get("not_before")) <= horizon
    ])
    stale = sort_rows([
        r for r in open_rows
        if r.get("status") == "active" and age_hours(r.get("timestamp"), now) > STALE_TASK_HOURS
    ])
    return {
        "schema": "coord.teams.digest.v1",
        "at": now,
        "human": human,
        "blocked_on_you": blocked_on_you,
        "upcoming": upcoming,
        "per_agent": presence_mod.agents_digest(rows, presence_shards, now=now),
        "stale": stale,
    }


def render(d: dict[str, Any]) -> str:
    """Deterministic text rendering (also what --store persists)."""
    def _line(r: dict[str, Any]) -> str:
        who = f"  ({r.get('assignee')})" if r.get("assignee") else ""
        return f"- [{r.get('priority')}] {r.get('title') or r.get('name')}{who}"

    parts = [f"# Digest — {d.get('at')}"]
    sections = (
        (f"Blocked on {d.get('human')}", d.get("blocked_on_you") or []),
        ("Upcoming (7d)", d.get("upcoming") or []),
        ("Stale (active, untouched > 48h)", d.get("stale") or []),
    )
    for title, items in sections:
        if items:
            parts.append(f"\n## {title}")
            parts.extend(_line(r) for r in items)
    agents = d.get("per_agent") or []
    if agents:
        parts.append("\n## Agents")
        for a in agents:
            counts = ", ".join(f"{k}={v}" for k, v in sorted((a.get("open") or {}).items()))
            parts.append(f"- [{a.get('liveness')}] {a.get('agent')}"
                         + (f" — {counts}" if counts else " — no open work")
                         + (f" — {a.get('summary')}" if a.get("summary") else ""))
    return "\n".join(parts) + "\n"


def window_for(now: str) -> str:
    """morning/evening bucket for the dedup marker (UTC hour < 12 -> morning)."""
    try:
        h = int(str(now)[11:13])
    except (ValueError, IndexError):
        h = 12
    return "morning" if h < 12 else "evening"
