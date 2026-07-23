"""Wake-router core (W4) — pure logic for `coord-engine router run`.

Normative contract: docs/coord/wake-router-PLAN.md §2/§2.5 and
wake-router-SPEC.md §4 (relay contract). The router is the fleet's model-free
wake policy: it scans the store by cursor (structurally immune to the
2026-07-22 listen-starvation class — it never touches the `listen` fold),
evaluates per-agent policy, and ENQUEUES wake decisions under the one namespace
it owns, `team/<team>/_coord/router/`. W4 executes nothing — execution is W5
(cloud-reachable) and W5.5 (thin host executor).

Two design facts everything else hangs off:

- **Tie-safe scan.** Store mtimes are minute-granular, so equal-mtime shards
  are the COMMON case. The scan is inclusive (`mtime >= watermark`) and the
  durable `processed` ledger — key ``<source-shard-id>:<agent>`` — suppresses
  replays. A strict `>` scan would skip forever any same-minute shard that
  landed after checkpoint.
- **Enablement is explicit.** An agent absent from `config.json` is
  observe-only: the router classifies and ledgers its items but never enqueues
  a wake for it. A config that fails validation routes to the fail-visible
  unroutable lane, never a silent drop.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

#: Poll interval while `router run` is resident. FIXED by plan §2.5 — the W7
#: acceptance latency bounds reference this constant, so it is not tunable.
ROUTER_POLL_SECONDS = 60

#: A presence beat younger than this reads as "actively working" — busy-aware
#: deferral queues below-floor wakes to this idle boundary (beat + window).
BUSY_FRESH_MIN = 30

#: Reduced check-in cadence for LAPSED agents (minutes) — plan §2 default,
#: valid range enforced by config validation.
LAPSED_CHECKIN_DEFAULT = 360
LAPSED_CHECKIN_MIN = 60
LAPSED_CHECKIN_MAX = 1440

#: Adapter allowlist (spec §Part-A / plan §2). Cloud-reachable adapters execute
#: on the decision plane (W5); host-local ones are enqueued with an executor id
#: and fired only by that host's thin executor (W5.5).
ADAPTERS_CLOUD = frozenset({"managed-agents-message", "routine-align"})
ADAPTERS_HOST_LOCAL = frozenset(
    {"codex-exec-resume", "openclaw-post", "macos-notify", "queued-wake-file"})

#: Per-adapter allowlisted `adapter_args` keys — free-form keys are a config
#: validation error (the relay contract: no commands, no session keys, no raw
#: URLs ride the store).
ADAPTER_ARG_KEYS: dict[str, frozenset] = {
    "codex-exec-resume": frozenset({"thread_id"}),
    "openclaw-post": frozenset({"endpoint_name"}),
    "managed-agents-message": frozenset({"session_ref"}),
    "macos-notify": frozenset(),
    "queued-wake-file": frozenset(),
    "routine-align": frozenset(),
}

PRIORITY_RANK = {"P1": 1, "P2": 2, "P3": 3}

#: Terminal statuses — a settled item wakes nobody.
TERMINAL_STATUSES = frozenset({"done", "abandoned", "archived"})

DECISIONS = ("interrupt", "batch", "defer", "checkin", "debounce",
             "observe", "unroutable")

_EXECUTOR_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")


def router_prefix(team: str) -> str:
    return f"team/{team}/_coord/router/"


def parse_store_mtime(mtime: Any) -> Optional[datetime]:
    """`fulcra-api file list` mtime ("2026-07-22 04:22PM UTC") → aware UTC
    datetime, or None on any other shape. Minute-granular by contract."""
    if not isinstance(mtime, str):
        return None
    text = mtime.strip()
    if text.endswith(" UTC"):
        text = text[:-4]
    try:
        return datetime.strptime(text, "%Y-%m-%d %I:%M%p").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z")


# --- cursor -----------------------------------------------------------------

def parse_cursor(raw: Optional[str]) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """(cursor, None) on a valid cursor; (None, reason) when the router must
    restart in observe-only (missing or corrupt — plan §2)."""
    if raw is None:
        return None, "cursor missing (first run or reclaimed state)"
    try:
        data = json.loads(raw)
    except ValueError:
        return None, "cursor corrupt (unparseable JSON)"
    if not isinstance(data, dict) or not isinstance(data.get("processed"), dict):
        return None, "cursor corrupt (wrong shape)"
    if data.get("watermark") is not None and parse_iso(data.get("watermark")) is None:
        return None, "cursor corrupt (unparseable watermark)"
    return {"watermark": data.get("watermark"),
            "processed": {str(k): str(v) for k, v in data["processed"].items()}}, None


def render_cursor(watermark: Optional[str], processed: dict[str, str]) -> str:
    return json.dumps({"watermark": watermark, "processed": processed},
                      sort_keys=True) + "\n"


def idempotency_key(shard_id: str, agent: str) -> str:
    return f"{shard_id}:{agent}"


# --- config -----------------------------------------------------------------

def validate_config(
    raw: Optional[str],
) -> tuple[dict[str, dict[str, Any]], list[str], dict[str, str]]:
    """→ (valid per-agent configs, executor allowlist, per-agent errors).

    A corrupt document returns ({}, [], {"_config": reason}) — every agent then
    reads as unconfigured (observe-only), and the caller reports the corruption
    loudly. Per-agent validation errors exclude ONLY that agent, routing its
    items to the unroutable lane; enablement never defaults."""
    if raw is None:
        return {}, [], {}
    try:
        doc = json.loads(raw)
    except ValueError:
        return {}, [], {"_config": "config corrupt (unparseable JSON)"}
    if not isinstance(doc, dict):
        return {}, [], {"_config": "config corrupt (not an object)"}
    executors = [e for e in doc.get("executors", [])
                 if isinstance(e, str) and _EXECUTOR_ID.match(e)] \
        if isinstance(doc.get("executors"), list) else []
    agents: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    for agent, cfg in doc.items():
        if agent == "executors":
            continue
        if not isinstance(cfg, dict):
            errors[agent] = "agent config is not an object"
            continue
        problems: list[str] = []
        floor = cfg.get("priority_floor", "P1")
        if floor not in PRIORITY_RANK:
            problems.append(f"priority_floor {floor!r} not in P1|P2|P3")
        debounce = cfg.get("debounce_min", 15)
        if not isinstance(debounce, int) or isinstance(debounce, bool) or debounce < 0:
            problems.append(f"debounce_min {debounce!r} not a non-negative int")
        adapter = cfg.get("adapter")
        if adapter not in ADAPTERS_CLOUD | ADAPTERS_HOST_LOCAL:
            problems.append(f"adapter {adapter!r} not in the allowlist")
            allowed_keys: frozenset = frozenset()
        else:
            allowed_keys = ADAPTER_ARG_KEYS[adapter]
        args = cfg.get("adapter_args", {})
        if not isinstance(args, dict):
            problems.append("adapter_args is not an object")
        else:
            free = sorted(set(args) - set(allowed_keys))
            if free and adapter in ADAPTER_ARG_KEYS:
                problems.append(
                    f"adapter_args carries non-allowlisted key(s) {free} "
                    f"(allowed for {adapter}: {sorted(allowed_keys) or 'none'})")
        checkin = cfg.get("lapsed_checkin_min", LAPSED_CHECKIN_DEFAULT)
        if (not isinstance(checkin, int) or isinstance(checkin, bool)
                or not LAPSED_CHECKIN_MIN <= checkin <= LAPSED_CHECKIN_MAX):
            problems.append(
                f"lapsed_checkin_min {checkin!r} outside "
                f"{LAPSED_CHECKIN_MIN}–{LAPSED_CHECKIN_MAX}")
        executor = cfg.get("executor")
        if adapter in ADAPTERS_HOST_LOCAL and executor not in executors:
            problems.append(
                f"host-local adapter {adapter!r} needs an executor from the "
                f"config's executor allowlist (got {executor!r})")
        if executor is not None and (not isinstance(executor, str)
                                     or not _EXECUTOR_ID.match(executor)):
            problems.append(f"executor {executor!r} is not a valid host id")
        if problems:
            errors[agent] = "; ".join(problems)
            continue
        agents[agent] = {
            "priority_floor": floor,
            "debounce_min": debounce,
            "adapter": adapter,
            "adapter_args": dict(args),
            "lapsed_checkin_min": checkin,
            "executor": ("decision-plane" if adapter in ADAPTERS_CLOUD
                         else executor),
            "active_hours": cfg.get("active_hours"),
        }
    return agents, executors, errors


# --- delivered view ---------------------------------------------------------

def fold_delivered(shards: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministic per-agent fold of the delivery-record shards — the
    decision-plane-owned `delivered.json` view. Dedup authority stays with the
    cursor ledger; this is observability bookkeeping only."""
    view: dict[str, Any] = {}
    for shard in shards:
        if not isinstance(shard, dict):
            continue
        agent = shard.get("agent")
        at = shard.get("delivered_at")
        if not isinstance(agent, str) or parse_iso(at) is None:
            continue
        row = view.setdefault(agent, {"last_delivered_at": None, "count": 0,
                                      "last_source_shard": None})
        row["count"] += 1
        if row["last_delivered_at"] is None or at > row["last_delivered_at"]:
            row["last_delivered_at"] = at
            row["last_source_shard"] = shard.get("source_shard")
    return view


# --- policy -----------------------------------------------------------------

def queue_filename(agent: str, key: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", agent)
    return f"{safe}-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:8]}.json"


def decide(
    *,
    item_priority: str,
    agent_cfg: Optional[dict[str, Any]],
    config_error: Optional[str],
    presence_ts: Optional[datetime],
    lapsed: bool,
    last_wake_at: Optional[datetime],
    last_delivered_at: Optional[datetime],
    now: datetime,
) -> tuple[str, Optional[datetime], str]:
    """One item's wake decision → (decision, not_before, reason).

    Order of authority: enablement (observe) → validity (unroutable) →
    debounce (one wake per window per agent, ALL classes — safe because every
    wake payload is a check-your-bus nudge, so an in-window wake already covers
    this item) → interrupt gating (floor) → lapsed reduced cadence → busy
    deferral → batch.
    """
    if config_error is not None:
        return "unroutable", None, f"config invalid: {config_error}"
    if agent_cfg is None:
        return "observe", None, "agent not enabled in router config"
    rank = PRIORITY_RANK.get(item_priority, PRIORITY_RANK["P2"])
    floor = PRIORITY_RANK[agent_cfg["priority_floor"]]
    debounce = timedelta(minutes=agent_cfg["debounce_min"])
    recent = [t for t in (last_wake_at, last_delivered_at) if t is not None]
    if recent and debounce and max(recent) > now - debounce:
        return "debounce", None, "coalesced into a wake inside the debounce window"
    if rank <= floor:
        return "interrupt", now, f"priority {item_priority} at/above floor"
    if lapsed:
        cadence = timedelta(minutes=agent_cfg["lapsed_checkin_min"])
        due = (last_delivered_at + cadence) if last_delivered_at else now
        return "checkin", max(due, now) if due > now else now, \
            "lapsed session — reduced check-in cadence"
    if presence_ts is not None and presence_ts > now - timedelta(minutes=BUSY_FRESH_MIN):
        boundary = presence_ts + timedelta(minutes=BUSY_FRESH_MIN)
        return "defer", boundary, "agent busy — queued to idle boundary"
    return "batch", None, "below interrupt floor — rides digest/next check-in"
