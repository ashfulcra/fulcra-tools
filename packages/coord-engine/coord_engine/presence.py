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

# --- activity-implies-liveness (wake-router W1.5) ---------------------------
#
# Every engine bus WRITE bumps the ACTOR's presence timestamp so a *working*
# agent is provably live — distinct from a dead session whose launchd beat still
# ticks. The bump is throttled to at most one presence write per this interval
# per process (an in-memory memo in the CLI dispatch layer). 60s sits well under
# the ``live``/``idle`` boundary (1h) so a steadily-working agent never drifts
# out of ``live`` between beats, yet coarse enough that a burst of writes in one
# command sequence collapses to a single shard write. Tests pin the throttle
# with an INJECTED monotonic clock, never this wall value.
ACTIVITY_REFRESH_INTERVAL = 60.0  # seconds

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
    elif until is None:
        # The write path ALWAYS resolves a session's until, so a persisted session
        # with a missing/null until is malformed — never a valid never-expiring
        # session (that would be the dead-session-looks-alive bug the schema exists
        # to prevent). Degrade rather than hand a fold an immortal session.
        return _engagement_degraded("session engagement missing required until")

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


# --- engagement-aware liveness combiner (wake-router W2) --------------------
#
# ``classify`` above is the PURE freshness axis (a function of the timestamp
# alone) and stays that way — it is called widely under that contract. W2 layers
# the DORMANCY axis on top without touching it: ``liveness`` reads freshness from
# ``classify`` and engagement from ``parse_engagement``, then applies the authored
# truth table. The two axes are ORTHOGONAL and are rendered as two independent
# facts, never a single merged label:
#   STALENESS  — timestamp freshness (live/idle/stale). Post-W1.5 a working
#                agent's timestamp is refreshed by the bus write-path, so a fresh
#                timestamp already IS "recent activity" — no separate signal.
#   DORMANCY   — a ``session`` past its ``until`` (or a durable W3 ``state:
#                lapsed`` marker) is LAPSED: distinct from stale/dead, EXPLAINED
#                ("declared session window ended"), and ROLE-RETAINING. It is
#                shown REGARDLESS of freshness — a session overrunning its window
#                while still beating is honestly LAPSED+active (nudge to extend),
#                never silently live.

LAPSED = "lapsed"


def _ago_label(ts: Optional[str], now: str) -> str:
    """A compact human age (``12m`` / ``5h`` / ``3d``) for annotations, or
    ``unknown`` when unparseable."""
    age = age_hours(ts, now)
    if age == float("inf"):
        return "unknown"
    minutes = age * 60.0
    if minutes < 60:
        return f"{int(round(minutes))}m"
    if age < 48:
        return f"{int(round(age))}h"
    return f"{int(round(age / 24.0))}d"


def _is_lapsed(engagement: dict[str, Any], *, now: str) -> bool:
    """Dormancy predicate. True iff the durable W3 marker says lapsed, OR a
    session's ``until`` has passed in real time (``now >= until``, boundary
    inclusive per the truth table). A degraded engagement reads as the legacy
    ``resident``/``active`` default and is therefore never lapsed — a malformed
    shard can never manufacture dormancy."""
    if engagement.get("state") == LAPSED:
        return True
    if engagement.get("mode") == "session" and engagement.get("until"):
        n = parse_iso_z(now)
        until = parse_iso_z(engagement["until"])
        if n is not None and until is not None and n >= until:
            return True
    return False


def liveness(shard: Any, *, now: str, live_hours: float = LIVE_HOURS,
             stale_hours: float = STALE_HOURS) -> dict[str, Any]:
    """Engagement-aware liveness verdict for one presence shard.

    Returns ``{state, freshness, annotation, engagement}`` where:
      - ``freshness`` is the PURE ``classify`` band (live/idle/stale) — the
        timestamp axis, unchanged.
      - ``state`` is the rendered primary state: ``lapsed`` when dormant, else the
        freshness band. LAPSED and stale are DISTINCT — ``state`` never collapses a
        lapsed session onto ``stale`` nor hides the freshness axis.
      - ``annotation`` renders the OTHER axis as a second fact: for a lapsed row,
        the freshness ("still beating … — extend session" vs "stale Nh"); for a
        live row, an occasional/within-window note and a stale-beat nudge.
    """
    fm = shard if isinstance(shard, dict) else {}
    ts = fm.get("timestamp")
    freshness = classify(ts, now=now, live_hours=live_hours, stale_hours=stale_hours)
    engagement = parse_engagement(fm)
    if _is_lapsed(engagement, now=now):
        if freshness == "stale":
            annotation = (f"LAPSED (declared session window ended; "
                          f"stale {_ago_label(ts, now)})")
        else:
            annotation = (f"LAPSED (declared session window ended; still beating, "
                          f"last beat {_ago_label(ts, now)} ago — extend session "
                          f"or release)")
        return {"state": LAPSED, "freshness": freshness,
                "annotation": annotation, "engagement": engagement}
    parts: list[str] = []
    mode = engagement.get("mode")
    if mode == "occasional":
        parts.append("occasional")
    elif mode == "session":
        parts.append("within committed window")
    if freshness == "stale":
        parts.append(f"stale {_ago_label(ts, now)} — nudge")
    return {"state": freshness, "freshness": freshness,
            "annotation": "; ".join(parts), "engagement": engagement}


def lapsed_holder(lease_agents: list[str], shards: list[dict[str, Any]], *,
                  now: str) -> Optional[str]:
    """The first lease agent whose presence reads LAPSED, or ``None``.

    Backs the gated vacancy-suppression: a role whose holder's session has lapsed
    is EXPLAINED absence (role-retaining), not gone-dark. Lookup is by EXACT id —
    no substring/fuzzy match (the corrupt-id lesson) — so a near-miss id in a
    lease never resolves to a live shard."""
    by_agent: dict[str, dict[str, Any]] = {}
    for s in shards:
        if isinstance(s, dict) and s.get("agent"):
            by_agent[str(s["agent"])] = s
    for a in lease_agents:
        s = by_agent.get(a)          # exact-id match only
        if s is not None and liveness(s, now=now)["state"] == LAPSED:
            return a
    return None


def _covered_via(shard: dict[str, Any], defaults: dict[str, Any], *,
                 defaults_ok: bool) -> Optional[str]:
    """How a live agent's engagement is covered for the mixed-fleet gate:
    ``"engagement"`` (its own well-formed engagement field), ``"defaults"`` (an
    operator-approved default mode), or ``None`` (uncovered). A malformed
    engagement field is NOT coverage — it degrades in ``parse_engagement`` and
    leaves coverage unknown."""
    raw = shard.get("engagement")
    if isinstance(raw, dict) and "_engagement_degraded" not in parse_engagement(shard):
        return "engagement"
    if defaults_ok and isinstance(defaults, dict):
        mode = defaults.get(str(shard.get("agent")))
        if mode in ENGAGEMENT_MODES:
            return "defaults"
    return None


def engagement_gate(shards: list[dict[str, Any]], defaults: dict[str, Any], *,
                    now: str, defaults_ok: bool = True,
                    live_hours: float = LIVE_HOURS,
                    stale_hours: float = STALE_HOURS) -> dict[str, Any]:
    """Deterministic mixed-fleet gate (plan §3). No vacancy/escalation semantic
    change may activate until every LIVE roster agent is covered — either it beats
    with a well-formed ``engagement`` field, or it appears in the operator-approved
    defaults map. Returns ``{status, defaults_ok, agents}``.

    Only agents whose freshness is ``live`` gate — stale/dead legacy shards (and
    idle ones) never block. Fail-closed on unknown coverage: an UNKNOWN defaults
    map (``defaults_ok=False`` — present-but-unreadable or unparseable, which the
    caller cannot distinguish from absent via the None-on-any-failure read
    contract) never yields PASS; the gate reports DEGRADED regardless of how the
    live agents' own fields look."""
    agents: list[dict[str, Any]] = []
    for s in shards:
        if not isinstance(s, dict) or not s.get("agent"):
            continue
        fresh = classify(s.get("timestamp"), now=now,
                         live_hours=live_hours, stale_hours=stale_hours)
        if fresh != "live":
            continue
        via = _covered_via(s, defaults, defaults_ok=defaults_ok)
        if via is not None:
            coverage = "COVERED"
        elif not defaults_ok:
            coverage = "UNKNOWN"      # could not certify via a degraded defaults map
        else:
            coverage = "UNCOVERED"
        agents.append({"agent": str(s["agent"]), "coverage": coverage, "via": via})
    agents.sort(key=lambda a: a["agent"])
    if not defaults_ok:
        status = "DEGRADED"
    elif all(a["coverage"] == "COVERED" for a in agents):
        status = "PASS"
    else:
        status = "BLOCKED"
    return {"status": status, "defaults_ok": defaults_ok, "agents": agents}


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
        lv = liveness(s, now=now, live_hours=live_hours, stale_hours=stale_hours)
        out.append({
            "agent": str(s["agent"]),
            "workstreams": [str(w) for w in ws],
            "summary": s.get("summary") or "",
            "last_seen": s.get("timestamp"),
            # ``liveness`` stays the PURE freshness band for back-compat — every
            # existing caller (broadcast_roster, briefing, agents_digest) reads it
            # and its meaning must not shift under them. The W2 engagement-aware
            # verdict rides ADDITIVELY alongside it.
            "liveness": lv["freshness"],
            "state": lv["state"],            # W2: lapsed-aware primary state
            "freshness": lv["freshness"],    # W2: the orthogonal freshness axis
            "annotation": lv["annotation"],  # W2: the rendered second-axis fact
            "engagement": lv["engagement"],
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
            # W2: the engagement-aware verdict, carried additively from the roster
            # row. ``liveness`` above stays the pure freshness band for back-compat.
            "state": pres["state"] if pres else "unknown",
            "freshness": pres["freshness"] if pres else "unknown",
            "annotation": pres["annotation"] if pres else "",
            "summary": pres["summary"] if pres else "",
            "workstreams": pres["workstreams"] if pres else [],
            "engagement": pres["engagement"] if pres else dict(_ENGAGEMENT_DEFAULT),
            "open": counts,
        })
    return out
