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

from datetime import datetime, timezone
from typing import Any, Optional

from .model import OPEN_STATUSES
from .roles import age_hours

LIVE_HOURS = 1.0
STALE_HOURS = 24.0

# --- engagement (wake-router W1, inert schema) ------------------------------
#
# The presence shard MAY carry an ``engagement`` object describing how the agent
# occupies the fleet:
#   ``{mode: resident|session|occasional, until: <iso8601Z|null>,
#      state: active|lapsed, lapsed_at: <iso8601Z|null>}``
# Absent ``engagement`` reads as ``resident`` + ``active`` — today's behavior, so
# legacy shards are unchanged. ``state``/``lapsed_at`` are PARSE-ONLY here: a beat
# always writes ``active``/``null`` and only the W3 sweep writes the lapsed values.
# W1 is inert — every fold PARSES engagement, NONE acts on it. This helper is the
# single parse seam, and it is DEFENSIVE by contract: a malformed engagement field
# (non-dict, unknown mode/state, unparseable timestamp) degrades to the legacy
# ``resident``/``active`` default AND carries a ``_engagement_degraded`` marker —
# it never raises, so one bad shard cannot break the fold for every other agent.

ENGAGEMENT_MODES = ("resident", "session", "occasional")
ENGAGEMENT_STATES = ("active", "lapsed")
SESSION_DEFAULT_TTL_HOURS = 8.0

_ENGAGEMENT_DEFAULT: dict[str, Any] = {
    "mode": "resident", "until": None, "state": "active", "lapsed_at": None,
}


def parse_iso_z(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp (``…Z`` or explicit offset) to a tz-aware UTC
    ``datetime``, or ``None``. Never raises. Mirrors the engine's other ISO
    readers (``roles._parse``/``reconcile._parse_iso_utc``) — ``fromisoformat``
    on 3.10 does not accept a bare ``Z``, so we swap it for ``+00:00`` first."""
    if not value:
        return None
    txt = str(value).strip()
    iso = (txt[:-1] + "+00:00") if txt.endswith(("Z", "z")) else txt
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def to_iso_z(dt: datetime) -> str:
    """Canonical UTC ``…Z`` rendering (inverse of ``parse_iso_z`` for the stored
    form)."""
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _engagement_degraded(reason: str) -> dict[str, Any]:
    out = dict(_ENGAGEMENT_DEFAULT)
    out["_engagement_degraded"] = reason
    return out


def parse_engagement(fm: Any) -> dict[str, Any]:
    """Defensive fold-side read of a presence shard's engagement object.

    Returns a normalized ``{mode, until, state, lapsed_at}``. Absent (or null)
    engagement is the legacy ``resident``/``active`` default with NO marker — that
    is a valid legacy shard, not an error. Any malformation degrades to that same
    safe default plus a ``_engagement_degraded`` reason and NEVER raises."""
    if not isinstance(fm, dict) or "engagement" not in fm:
        return dict(_ENGAGEMENT_DEFAULT)
    raw = fm.get("engagement")
    if raw is None:
        return dict(_ENGAGEMENT_DEFAULT)          # explicit null == legacy absent
    if not isinstance(raw, dict):
        return _engagement_degraded(f"engagement is not a mapping: {type(raw).__name__}")

    mode = raw.get("mode")
    if mode not in ENGAGEMENT_MODES:
        return _engagement_degraded(f"unknown engagement.mode: {mode!r}")

    until: Optional[str] = None
    until_raw = raw.get("until")
    if until_raw not in (None, ""):
        dt = parse_iso_z(until_raw)
        if dt is None:
            return _engagement_degraded(f"unparseable engagement.until: {until_raw!r}")
        until = to_iso_z(dt)
    # ``until`` is only meaningful for a session; other modes never carry an expiry.
    if mode != "session":
        until = None

    state = raw.get("state") or "active"
    if state not in ENGAGEMENT_STATES:
        return _engagement_degraded(f"unknown engagement.state: {state!r}")

    lapsed_at: Optional[str] = None
    lapsed_raw = raw.get("lapsed_at")
    if lapsed_raw not in (None, ""):
        dt = parse_iso_z(lapsed_raw)
        if dt is None:
            return _engagement_degraded(f"unparseable engagement.lapsed_at: {lapsed_raw!r}")
        lapsed_at = to_iso_z(dt)

    return {"mode": mode, "until": until, "state": state, "lapsed_at": lapsed_at}


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
            # W1: PARSE and carry engagement additively — liveness above is
            # computed from the timestamp ALONE and never consults this value.
            "engagement": parse_engagement(s),
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
            # W1: carried additively from the roster row (parse-only, no action).
            "engagement": pres["engagement"] if pres else dict(_ENGAGEMENT_DEFAULT),
            "open": counts,
        })
    return out
