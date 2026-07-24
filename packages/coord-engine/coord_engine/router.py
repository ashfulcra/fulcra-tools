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

#: Logical executor id for adapters executed on the cloud decision plane.
DECISION_PLANE = "decision-plane"

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
    latest: dict[str, datetime] = {}
    for shard in shards:
        if not isinstance(shard, dict):
            continue
        agent = shard.get("agent")
        at_dt = parse_iso(shard.get("delivered_at"))
        if not isinstance(agent, str) or at_dt is None:
            continue
        row = view.setdefault(agent, {"last_delivered_at": None, "count": 0,
                                      "last_source_shard": None})
        row["count"] += 1
        # codex #460 fix: compare PARSED datetimes, not ISO strings — a lexical
        # string compare mis-orders non-UTC offsets (e.g. "…T14:00+02:00" sorts
        # after "…T12:30Z" but is earlier). Store the normalized Z form.
        if agent not in latest or at_dt > latest[agent]:
            latest[agent] = at_dt
            row["last_delivered_at"] = iso(at_dt)
            row["last_source_shard"] = shard.get("source_shard")
    return view


def record_filename(key: str) -> str:
    """Return the canonical idempotency-keyed delivery-record filename."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", key)
    return f"{safe}-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:8]}.json"


def delivery_record(entry: dict[str, Any], delivered_at: str) -> dict[str, Any]:
    """Build the standard successful-execution record consumed by the fold."""
    return {
        "key": idempotency_key(str(entry.get("source_shard")),
                               str(entry.get("agent"))),
        "agent": entry.get("agent"),
        "source_shard": entry.get("source_shard"),
        "adapter": entry.get("adapter"),
        "executor": entry.get("executor"),
        "delivered_at": delivered_at,
    }


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


# --- W5: adapter integration + execution (pure core) ------------------------
#
# The decision plane EXECUTES the cloud-reachable adapters (executor ==
# DECISION_PLANE); host-local adapters are enqueued with executor: <host id>
# and left for the W5.5 thin host executor — one component executes each adapter
# class, by construction (plan §W5). Delivery is AT-LEAST-ONCE (the store has no
# atomic claim/CAS); the system is safe by ADAPTER CONTENT DESIGN, enforced by
# `adapter_invocation` below: every wake is a keyed check-your-bus nudge with NO
# per-event command, so N deliveries converge to one bus check (plan §2).

#: Logical executor id the decision plane claims. Cloud-reachable adapters
#: resolve to this at enqueue time (see `validate_config`); host-local adapters
#: resolve to a host id and are never executed here.
DECISION_PLANE = "decision-plane"

#: Bounded retry before an execution is dead-lettered (plan §2 relay contract:
#: bounded retry → dead-letter, never an unbounded loop).
MAX_DELIVERY_ATTEMPTS = 3

#: A FOREIGN claim younger than this reads as "another executor is mid-flight" —
#: skip it. Own claims and stale foreign claims are retryable, which is
#: at-least-once again, which is safe by the content rule (plan §2).
CLAIM_FRESH_MIN = 10


def is_decision_plane_entry(entry: dict[str, Any]) -> bool:
    """The decision plane executes exactly the entries it owns — cloud-reachable
    adapters resolved to `executor: decision-plane`. Everything else (host-local
    executor ids) is left in the queue for W5.5; the decision plane never fires
    a host-local adapter."""
    return entry.get("executor") == DECISION_PLANE


def claim_is_skippable(entry: dict[str, Any], executor_id: str,
                       now: datetime) -> bool:
    """True when a FOREIGN executor holds a fresh (< CLAIM_FRESH_MIN) claim —
    another process is mid-flight, so skip to avoid a redundant fire. Our own
    claim or a STALE foreign claim is retryable (at-least-once, safe by the
    content rule). No claim at all is claimable."""
    claimed_at = parse_iso(entry.get("claimed_at"))
    if claimed_at is None:
        return False
    if entry.get("claimed_by") == executor_id:
        return False
    return claimed_at > now - timedelta(minutes=CLAIM_FRESH_MIN)


def claim_stamp(entry: dict[str, Any], executor_id: str,
                now: datetime) -> dict[str, Any]:
    """Entry re-stamped with this executor's claim. Claim is advisory, not a
    lock (no CAS in the store) — it only suppresses a concurrent fresh foreign
    fire; correctness rests on the content rule, not the claim."""
    return {**entry, "claimed_by": executor_id, "claimed_at": iso(now)}


def adapter_invocation(entry: dict[str, Any],
                       adapter_args: Optional[dict[str, Any]] = None
                       ) -> dict[str, Any]:
    """The W5 adapter-invocation contract — a content-SAFE wake payload.

    ENFORCES the relay content rule (plan §2, spec §4): the payload is a keyed
    "check your bus" nudge carrying the idempotency key and NO per-event
    command, session mutation, or raw content — so at-least-once delivery is
    safe (N fires converge to one bus check). Routing targets come ONLY from
    allowlisted `adapter_args` (host-resolved), never from the store shard.

    Raises ValueError on an unknown adapter (fail-visible, never a silent
    mis-fire)."""
    adapter = entry.get("adapter")
    if adapter not in ADAPTERS_CLOUD | ADAPTERS_HOST_LOCAL:
        raise ValueError(f"adapter {adapter!r} not in the allowlist")
    args = adapter_args or {}
    key = idempotency_key(str(entry.get("source_shard")), str(entry.get("agent")))
    inv: dict[str, Any] = {
        "adapter": adapter,
        "agent": entry.get("agent"),
        "idempotency_key": key,
        # the ONLY content — a fixed keyed nudge, never a per-event command
        "message": (f"wake({entry.get('agent')}): a directed item is on your "
                    f"bus [{key}]. Check your inbox / needs-me. No action is "
                    f"encoded in this wake."),
    }
    # per-adapter routing target, drawn from allowlisted adapter_args only
    if adapter == "managed-agents-message":
        inv["session_ref"] = args.get("session_ref")
    elif adapter == "codex-exec-resume":
        inv["thread_id"] = args.get("thread_id")
    elif adapter == "openclaw-post":
        inv["endpoint_name"] = args.get("endpoint_name")
    # macos-notify, queued-wake-file, routine-align carry no extra target
    return inv


def record_filename(key: str) -> str:
    """Deterministic, single-writer-per-key shard name for delivered/ and
    dead-letter/ records. Keyed by the idempotency key (so a duplicate write is
    a self-overwrite, never a second shard), sanitized for the store with a key
    hash appended for collision-resistance (agent ids carry ':' — a plain
    substitution could collide)."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", key)
    return f"{safe}-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:8]}.json"


def delivery_record(entry: dict[str, Any], delivered_at: str) -> dict[str, Any]:
    """A successful-execution record shard (plan §2). Idempotency-keyed;
    carries exactly what `fold_delivered` reads plus provenance."""
    return {
        "key": idempotency_key(str(entry.get("source_shard")),
                               str(entry.get("agent"))),
        "agent": entry.get("agent"),
        "source_shard": entry.get("source_shard"),
        "adapter": entry.get("adapter"),
        "executor": entry.get("executor"),
        "delivered_at": delivered_at,
    }


def dead_letter_record(entry: dict[str, Any], *, attempts: int,
                       last_error: str, gave_up_at: str) -> dict[str, Any]:
    """A bounded-retry-exhausted record (plan §2): the full queue entry plus the
    audit fields. Idempotency-keyed by the caller, so a concurrent duplicate
    transition is a self-overwrite no-op."""
    return {**entry, "attempts": attempts, "last_error": last_error,
            "gave_up_at": gave_up_at}


# --- W7: shadow-mode delivery-probe evidence (plan W7) -----------------------
#
# During the read-only shadow window the router LOGS a decision for every
# directed item but enqueues nothing; the live delivery paths (listener tick,
# adapter execution, watchdog/fleet loop) each write a tiny evidence shard to
# `_coord/router/shadow-evidence/` at the moment delivery SUCCEEDS. The
# acceptance report correlates router decisions against these probes on the
# idempotency key. Zero model tokens; the whole probe is removable after
# acceptance.

SHADOW_EVIDENCE_SUBPATH = "shadow-evidence/"

#: The live-delivery mechanisms a probe shard may attribute a wake to.
SHADOW_EVIDENCE_PATHS = frozenset({"listener", "adapter", "watchdog"})


def shadow_evidence_filename(agent: str, key: str) -> str:
    """`<agent>-<hash>.json` — one shard per (agent, idempotency-key). Sanitized
    agent prefix for legibility + a key hash for uniqueness and collision-safety
    (agent ids carry ':'). Same key ⇒ same filename (self-overwriting, so a
    duplicate probe write is idempotent, never a second shard)."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", agent)
    return f"{safe}-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:8]}.json"


def shadow_evidence_record(*, key: str, agent: str, delivered_at: str,
                           path: str) -> dict[str, Any]:
    """A delivery-probe evidence shard — `{key, agent, delivered_at, path}`.
    `path` is the delivery mechanism and MUST be one of SHADOW_EVIDENCE_PATHS;
    an unknown path is a fail-visible ValueError, never a silently
    mis-attributed measurement (the report's classification depends on it)."""
    if path not in SHADOW_EVIDENCE_PATHS:
        raise ValueError(
            f"shadow-evidence path {path!r} not in "
            f"{sorted(SHADOW_EVIDENCE_PATHS)}")
    return {"key": key, "agent": agent, "delivered_at": delivered_at,
            "path": path}
