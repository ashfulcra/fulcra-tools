"""Task schema, status machine, and validation for fulcra-coord."""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import SCHEMA_VERSION, task_file_path

# ---------------------------------------------------------------------------
# Valid statuses and allowed transitions
# ---------------------------------------------------------------------------

VALID_STATUSES = {"proposed", "active", "waiting", "blocked", "done", "abandoned"}

TERMINAL_STATUSES = {"done", "abandoned"}

# Maps current status -> set of allowed next statuses
STATUS_TRANSITIONS: dict[str, set[str]] = {
    # proposed -> done is LEGAL (message-class lifecycle, 2026-06-11): a
    # delivered message's consumer closing the echo is the NORMAL case for
    # directive-tasks (tells / FYIs / verdict echoes), and forcing the
    # update->active dance first meant TWO writes over a high-latency
    # transport — which silently discouraged hygiene and let proposed
    # message-tasks pile up forever. `done` already requires --evidence (and a
    # verification level), so the single-write close preserves the audit
    # trail. proposed -> blocked stays illegal: blocking implies someone
    # picked the work up first.
    "proposed": {"active", "waiting", "abandoned", "done"},
    "active": {"waiting", "blocked", "done", "abandoned"},
    "waiting": {"active", "blocked", "abandoned"},
    "blocked": {"active", "waiting", "abandoned"},
    # Terminal states require explicit reopen — handled separately
    "done": set(),
    "abandoned": set(),
}

VALID_PRIORITIES = {"P0", "P1", "P2", "P3"}
VALID_VERIFICATION_LEVELS = {"agent-verified", "human-verified", "automated", "unverified"}
VALID_KINDS = {"ops", "feature", "bug", "research", "infra", "config", "comms", "other"}

# Workstreams are open strings; this is a suggested set — not enforced.
SUGGESTED_WORKSTREAMS = {"devops", "main-comms", "fulcra", "insights", "general", "research"}

MAX_EVENTS_INLINE = 20


class CoordError(Exception):
    """Base error for coordination operations."""


class TransitionError(CoordError):
    """Invalid status transition."""


class SchemaError(CoordError):
    """Task schema validation failure."""


class ConflictError(CoordError):
    """Remote version conflict detected."""


class NeedsReconcile(CoordError):
    """Partial upload — remote views need repair by reconciler."""


# ---------------------------------------------------------------------------
# Task ID generation
# ---------------------------------------------------------------------------

def make_task_id(title: str, dt: Optional[datetime] = None) -> str:
    """Generate a canonical task ID from title and current date."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    date_part = dt.strftime("%Y%m%d")
    slug = _slugify(title)[:24].rstrip("-")
    suffix = hashlib.sha1(uuid.uuid4().bytes).hexdigest()[:8]
    return f"TASK-{date_part}-{slug}-{suffix}"


def _slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text or "task"


# ---------------------------------------------------------------------------
# Task construction and validation
# ---------------------------------------------------------------------------

def _validate_fields(
    *,
    kind: str,
    priority: str,
    status: str,
) -> None:
    """Raise SchemaError for invalid field values."""
    if priority not in VALID_PRIORITIES:
        raise SchemaError(
            f"Invalid priority {priority!r}. Valid: {sorted(VALID_PRIORITIES)}"
        )
    if kind not in VALID_KINDS:
        raise SchemaError(
            f"Invalid kind {kind!r}. Valid: {sorted(VALID_KINDS)}"
        )
    if status not in VALID_STATUSES:
        raise SchemaError(
            f"Invalid status {status!r}. Valid: {sorted(VALID_STATUSES)}"
        )


def parse_when(s: Optional[str], *, now: Optional[datetime] = None) -> Optional[str]:
    """Parse a human/agent-supplied "when" into a canonical ISO-Z string.

    Accepts, with NO third-party deps (the CLI is stdlib-only):
      * ISO-8601 dates and datetimes — ``2026-06-08`` (midnight UTC) or
        ``2026-06-08T18:00:00Z`` / with an explicit offset.
      * Relative offsets ``<N>d`` / ``<N>h`` / ``<N>m`` (days/hours/minutes)
        anchored to ``now`` — e.g. ``5d``, ``36h``, ``10m``.

    Returns the resolved instant as ``...Z`` (UTC). Returns ``None`` on anything
    unparseable so the caller can treat the field as simply unset rather than
    erroring on a typo — a malformed ``--not-before`` should degrade to "no
    gate", never block the whole ``block`` op.

    Pure and unit-testable: pass ``now`` (defaults to ``datetime.now(utc)``) so
    relative offsets are deterministic in tests.
    """
    if not s or not s.strip():
        return None
    s = s.strip()
    if now is None:
        now = datetime.now(timezone.utc)

    # Relative offset: <N><unit> where unit is d/h/m. Matched first so a bare
    # "5d" never falls through to the ISO parser (which would reject it).
    m = re.fullmatch(r"(\d+)([dhm])", s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        delta = {
            "d": timedelta(days=n),
            "h": timedelta(hours=n),
            "m": timedelta(minutes=n),
        }[unit]
        return (now + delta).astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")

    # ISO-8601. fromisoformat accepts a bare date (-> midnight, naive) and full
    # datetimes; normalize "Z" to "+00:00" first since older fromisoformat
    # rejects the literal "Z". A naive result is assumed UTC (matches how the
    # rest of the codebase stamps times).
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def make_task(
    *,
    title: str,
    workstream: str,
    agent: str,
    kind: str = "ops",
    priority: str = "P2",
    surface: Optional[str] = None,
    owner_agent: Optional[str] = None,
    assignee: Optional[str] = None,
    agent_instance: Optional[str] = None,
    collaborators: Optional[list[str]] = None,
    summary: str = "",
    next_action: str = "",
    not_before: Optional[str] = None,
    due: Optional[str] = None,
    task_id: Optional[str] = None,
    dt: Optional[datetime] = None,
) -> dict[str, Any]:
    if dt is None:
        dt = datetime.now(timezone.utc)
    now_iso = dt.isoformat(timespec="microseconds").replace("+00:00", "Z")

    if task_id is None:
        task_id = make_task_id(title, dt)

    _validate_fields(
        kind=kind,
        priority=priority,
        status="proposed",
    )

    tags = build_tags(
        status="proposed",
        workstream=workstream,
        agent=agent,
        kind=kind,
        priority=priority,
    )

    return {
        "schema": SCHEMA_VERSION,
        "id": task_id,
        "title": title,
        "status": "proposed",
        "origin": "human",
        "priority": priority,
        "workstream": workstream,
        "surface": surface or "local:agent",
        "source": {
            "channel": workstream,
            "message_id": None,
            "conversation_label": f"#{workstream}",
        },
        "owner_agent": owner_agent or agent,
        # The agent the work is *directed at* (distinct from owner_agent = who's
        # executing). A directive from agent 1 to agent 2 is a task with
        # assignee=agent2 created by agent 1. None on ordinary self-owned tasks.
        "assignee": assignee,
        "agent_instance": agent_instance or f"{agent}:local",
        "collaborators": collaborators or [],
        "linked_workstreams": [],
        "tags": tags,
        "current_summary": summary,
        "next_action": next_action,
        "blocked_on": None,
        # Scheduling for "blocked on you" surfaces (both ISO-Z or None):
        #   not_before — the GATING field. A task is not surfaced as DUE-NOW on
        #     the human's plate / SessionStart banner until now >= not_before, so
        #     a blocked-on-user ask the human can't act on yet (e.g. a re-auth
        #     window that opens next week) stays off the plate as "upcoming"
        #     instead of nagging every session.
        #   due — the deadline. Purely informational: drives the upcoming-list
        #     ordering and urgency display; it does NOT gate visibility.
        "not_before": not_before,
        "due": due,
        "claim": {
            "claimed_by": agent,
            "claimed_at": now_iso,
            "claim_expires_at": None,
        },
        "done": {
            "done_at": None,
            "done_by": None,
            "evidence": None,
            "verification_level": None,
            "confidence": None,
        },
        "checklist": [],
        "links": {
            "local_ticket": None,
            "files": [],
            "prs": [],
            "remote_files": [],
        },
        "events": [
            {
                "at": now_iso,
                "type": "created",
                "by": agent,
                "summary": "Task created.",
                "evidence": None,
            }
        ],
        "created_at": now_iso,
        "updated_at": now_iso,
        "last_touched_by": agent,
        "last_touched_in": surface or "local:agent",
    }


# ---------------------------------------------------------------------------
# Agent presence (workstream-on-connect)
# ---------------------------------------------------------------------------

PRESENCE_SCHEMA = "fulcra.coordination.presence.v1"


def _normalize_workstreams(workstreams: Optional[list[str]]) -> list[str]:
    """Normalize a presence record's workstreams to a sorted, unique list of
    non-empty trimmed strings.

    WHY: workstreams arrive from two merged sources (explicit ``--workstream``
    values and the distinct ``workstream`` of an agent's open tasks), so the same
    stream can appear twice, with stray whitespace, or as an empty token from a
    trailing comma. Normalizing here — at construction — means every downstream
    consumer (the roster, the read commands) sees a clean, deterministically
    ordered set without re-deduping, and two records built from the same streams
    in different orders compare equal."""
    seen = {w.strip() for w in (workstreams or []) if w and w.strip()}
    return sorted(seen)


def _normalize_capabilities(capabilities: Optional[list[str]]) -> list[str]:
    """Normalize declared capabilities to a sorted, unique list of non-empty
    trimmed strings.

    WHY: capabilities arrive from merged CLI sources (``--can-review`` sugar
    plus repeatable ``--role`` values), so the same role can appear twice, with
    stray whitespace, or as an empty token. Mirroring ``_normalize_workstreams``
    means the reviewer-pool builder sees a clean, deterministically ordered set
    and two records declaring the same roles in different orders compare equal.
    A missing/empty input yields ``[]`` — the backward-compatible default for
    agents that predate capability declaration."""
    seen = {c.strip() for c in (capabilities or []) if c and c.strip()}
    return sorted(seen)


def _normalize_declared_edges(values: Optional[list[str]]) -> list[str]:
    """Normalize declared topology edges such as presence ``maintains``."""
    seen: set[str] = set()
    for value in values or []:
        for part in str(value).split(","):
            part = part.strip()
            if part:
                seen.add(part)
    return sorted(seen)


def make_presence(
    agent: str,
    *,
    workstreams: Optional[list[str]] = None,
    summary: str = "",
    last_seen: Optional[str] = None,
    session: Optional[str] = None,
    capabilities: Optional[list[str]] = None,
    host_profile: Optional[str] = None,
    maintains: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Build a validated per-agent presence record (``presence/<slug>.json``).

    Presence is the situational-awareness primitive: an agent declares the major
    workstream(s) it is currently on, so the human sees what it is working on
    EVEN WHEN it owns no active coordination task. Only the agent itself writes
    its own record, so there is no cross-agent write contention — mirroring the
    per-entity tasks→views pattern.

    ``last_seen`` defaults to now (ISO-Z) so the liveness model in
    views.build_presence can age it; ``session`` is an opaque optional key the
    connecting surface may pass for traceability. ``capabilities`` is the set of
    declared roles (e.g. ``review``) that liveness-aware routing uses to build a
    candidate pool; it defaults to ``[]`` so records from agents predating the
    field stay valid and backward-compatible. ``host_profile`` and
    ``maintains`` are additive fleet-topology declarations: old readers ignore
    them, while new routing/health surfaces can use them to understand which
    always-on hosts maintain intermittent agents or runtime prefixes."""
    if last_seen is None:
        last_seen = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    profile = (host_profile or "").strip() or None
    if profile not in {None, "always-on", "intermittent"}:
        raise SchemaError("host_profile must be always-on or intermittent")
    return {
        "schema": PRESENCE_SCHEMA,
        "agent": agent,
        "workstreams": _normalize_workstreams(workstreams),
        "summary": summary or "",
        "last_seen": last_seen,
        "session": session,
        "capabilities": _normalize_capabilities(capabilities),
        "host_profile": profile,
        "maintains": _normalize_declared_edges(maintains),
    }


# ---------------------------------------------------------------------------
# Version manifest (version self-incorporation, operator directive 2026-06-10)
# ---------------------------------------------------------------------------

VERSION_MANIFEST_SCHEMA = "fulcra.coordination.version.v1"

# THE POINTER RULE (the reconciled spec's non-negotiable safety boundary,
# docs/superpowers/specs/2026-06-08-greenfield-reconciled.md): the bus carries
# a version POINTER, never a code payload. This key set is CLOSED — version
# string + commit sha + compatibility floor + metadata, and NOTHING else. No
# cmd, no argv, no script, no URL: an agent updates the KNOWN package from its
# LOCALLY configured trusted source; it never executes anything a bus record
# hands it. validate_version_manifest REJECTS any extra key (pinned by test)
# so a tampered manifest cannot even parse as valid, let alone instruct.
_VERSION_MANIFEST_KEYS = frozenset(
    {"schema", "package_version", "release_commit", "min_supported",
     "published_at"})


def make_version_manifest(
    package_version: str,
    release_commit: str,
    min_supported: Optional[str] = None,
) -> dict[str, Any]:
    """Build the canonical version-manifest record (``runtime/version.json``).

    Published by the maintainer's ``announce-version`` at each release so the
    fleet self-incorporates instead of needing per-host "UPDATE NOW" pings
    (operator, 2026-06-10: "i'm not going to go around and wake the entire
    fleet for each incremental upgrade"). ``release_commit`` is best-effort
    provenance (may be ``""`` when the announcing host has no git checkout);
    ``min_supported`` is the optional compatibility floor an older reader can
    compare itself against. See _VERSION_MANIFEST_KEYS for why this record
    can never carry more than these fields."""
    now = datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    return {
        "schema": VERSION_MANIFEST_SCHEMA,
        "package_version": str(package_version),
        "release_commit": str(release_commit or ""),
        "min_supported": min_supported if min_supported else None,
        "published_at": now,
    }


def validate_version_manifest(m: Any) -> list[str]:
    """Validate a version manifest; returns a list of problems ([] = valid).

    Stricter than the sibling validators in one deliberate way: UNKNOWN KEYS
    ARE ERRORS. Every other record family is additive (old readers ignore new
    fields), but this record's entire safety story is being a pointer — a
    field this validator doesn't know is, by definition, not a pointer, so the
    self-update check must treat the record as garbage (never behind)."""
    errors: list[str] = []
    if not isinstance(m, dict):
        return ["manifest is not an object"]
    extra = set(m.keys()) - _VERSION_MANIFEST_KEYS
    if extra:
        errors.append(f"unexpected keys (pointer rule): {sorted(extra)}")
    if m.get("schema") != VERSION_MANIFEST_SCHEMA:
        errors.append(f"schema must be {VERSION_MANIFEST_SCHEMA}")
    pv = m.get("package_version")
    if not isinstance(pv, str) or not pv.strip():
        errors.append("package_version must be a non-empty string")
    if not isinstance(m.get("release_commit"), str):
        errors.append("release_commit must be a string (may be empty)")
    ms = m.get("min_supported")
    if ms is not None and (not isinstance(ms, str) or not ms.strip()):
        errors.append("min_supported must be null or a non-empty string")
    if not isinstance(m.get("published_at"), str):
        errors.append("published_at must be a string timestamp")
    return errors


# ---------------------------------------------------------------------------
# First-class Directive record — the coordination LOOP record. Dual-written
# by every directive-creating command (directives.dual_write); read for
# coordination state by board / digest / review-done / the reconcile health
# and parity passes. The TASK record stays authoritative for task state.
# ---------------------------------------------------------------------------

DIRECTIVE_SCHEMA = "fulcra.coordination.directive.v1"

# The closed set of directive communication types. WHY these five:
#   tell       — an instruction from one agent to another (one-to-one).
#   broadcast  — a one-to-many instruction; audience="*" is the wildcard.
#   review     — a request for another agent (or human) to review work.
#   verdict    — the outcome of a review (approved/rejected + rationale).
#   human-ask  — a directive that surfaces to the human inbox (replaces the
#                "blocked-on-you" pattern currently encoded as task status).
_DIRECTIVE_TYPES = {"tell", "broadcast", "review", "verdict", "human-ask"}

# Valid statuses for a directive. Intentionally SMALLER than task statuses:
# directives are ephemeral routing primitives, not lifecycle-tracked work units.
_DIRECTIVE_STATUSES = {"proposed", "delivered", "acked", "acted", "expired"}
_DIRECTIVE_KEYS = {
    "schema", "id", "directive_type", "from", "audience",
    "title", "summary", "next_action", "priority", "workstream",
    "status", "acked_by", "artifact_ref", "not_before", "due",
    "routing", "created_at", "updated_at", "task_id",
}

# Loop fields (spec 2026-06-09-coordination-loops-design.md) — ADDITIVE and
# OPTIONAL. The Directive family evolves into the coordination LOOP record:
# `kind` selects a per-kind lifecycle (registry in loops.py), `state` is the
# lifecycle position, `outcome` is the bus-delivered response payload (set ONLY
# by a response event — never at creation), `expects_response` marks a loop
# that must stay open until a bus-native response arrives, `sla_hours` drives
# overdue detection. OPTIONAL (not in _DIRECTIVE_KEYS' required set) so records
# written by pre-loop hosts — which lack these keys — remain valid forever:
# the mixed-fleet floor is "new readers handle old records".
_LOOP_KEYS = {"kind", "state", "outcome", "expects_response", "sla_hours"}

# Loop kinds the validator accepts. The authoritative registry (lifecycles,
# defaults) lives in loops.py; this set exists only so a typo'd kind is caught
# at validation. `tell` is the legacy/FYI kind old records map onto.
_LOOP_KINDS = {"tell", "review", "dispatch", "idea", "question", "signoff"}

# Continuity payload fields (spec 2026-06-10-continuity-integration-design.md)
# — ADDITIVE and OPTIONAL, exactly like _LOOP_KEYS. A `handoff` dispatch loop
# carries the producing session's continuity checkpoint as a payload REF:
#   checkpoint_ref    — an OPAQUE string (a remote `continuity/...` bus path in
#                       the normal case). Coord never parses structure out of
#                       it — the checkpoint schema is owned by the standalone
#                       fulcra-continuity package, and refs-not-bodies is the
#                       decoupling the spec demands.
#   checkpoint_inline — FALLBACK ONLY: the checkpoint JSON carried inline when
#                       publishing a local checkpoint file to the remote tree
#                       failed (small docs are fine inline; without this a
#                       transport blip would strand the handoff ref on a path
#                       the recipient host can't read).
# Optional so records written by pre-continuity hosts stay valid forever — the
# same mixed-fleet floor as the loop fields.
_CONTINUITY_KEYS = {"checkpoint_ref", "checkpoint_inline"}


def make_directive_id(directive_type: str, dt: Optional[datetime] = None) -> str:
    """Generate a collision-resistant, day-sortable directive ID.

    Mirrors make_task_id's approach (date slug + random suffix). The id is
    ``DIR-<YYYYMMDD>-<type>-<rand8>``, so IDs are lexically sortable by creation
    DAY and remain human-readable in file listings. Sortability is DAY
    granularity only: two directives created on the same day sort by the random
    suffix, NOT by sub-day creation time — within a day the order is arbitrary,
    not chronological. (Use ``created_at`` for true time ordering.)
    The directive_type is embedded so a ``ls directives/`` scan is self-describing.

    WHY include a random suffix: two directives of the same type created in the
    same second by different agents (or in rapid succession) must not collide —
    the suffix gives 8 hex chars (32-bit) of per-instant entropy, matching the
    task ID scheme.
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    date_part = dt.strftime("%Y%m%d")
    type_slug = _slugify(directive_type)[:16].rstrip("-")
    suffix = hashlib.sha1(uuid.uuid4().bytes).hexdigest()[:8]
    return f"DIR-{date_part}-{type_slug}-{suffix}"


def make_directive(
    *,
    directive_type: str,
    from_agent: str,
    audience: str,
    title: str,
    workstream: str,
    summary: str = "",
    next_action: str = "",
    priority: str = "P2",
    status: str = "proposed",
    artifact_ref: Optional[dict] = None,
    not_before: Optional[str] = None,
    due: Optional[str] = None,
    task_id: Optional[str] = None,
    directive_id: Optional[str] = None,
    kind: Optional[str] = None,
    state: Optional[str] = None,
    expects_response: bool = False,
    sla_hours: Optional[int] = None,
    dt: Optional[datetime] = None,
) -> dict[str, Any]:
    """Build a first-class directive record — the coordination loop record.

    Every directive-creating command dual-writes one of these alongside its
    authoritative task (``directives.dual_write``); board, digest, review-done
    and the reconcile health/parity passes read them for coordination state.
    The task record stays authoritative for task state.

    ``from_agent`` is who issues the directive (the instructing owner);
    ``audience`` is the recipient agent id or ``'*'`` (broadcast wildcard, matching
    ``views.BROADCAST``). ``task_id`` is the back-reference to the task this
    record mirrors; it is ``None`` for a directive that mirrors no task.

    WHY a separate record type rather than a task with assignee: tasks model
    WORK (with lifecycle, evidence, checklists, done/blocked semantics). Directives
    model COMMUNICATION (who told whom what). Conflating them forces the inbox,
    ack, expiry, and routing semantics to leak into the task lifecycle state
    machine — which is exactly the coupling this record type avoids.
    """
    # --- Validate required string fields first (before the more expensive path) ---
    if directive_type not in _DIRECTIVE_TYPES:
        raise ValueError(
            f"Invalid directive_type {directive_type!r}. "
            f"Valid: {sorted(_DIRECTIVE_TYPES)}"
        )
    if not from_agent or not from_agent.strip():
        raise ValueError("from_agent must be a non-empty string.")
    if not audience or not audience.strip():
        raise ValueError("audience must be a non-empty string.")
    if not title or not title.strip():
        raise ValueError("title must be a non-empty string.")
    # workstream is a required positional kwarg with no default; guard it the
    # same way as the other required strings so an empty/whitespace value can't
    # slip through and break the workstream-keyed routing/listing downstream.
    if not workstream or not workstream.strip():
        raise ValueError("workstream must be a non-empty string.")

    if priority not in VALID_PRIORITIES:
        raise ValueError(
            f"Invalid priority {priority!r}. Valid: {sorted(VALID_PRIORITIES)}"
        )
    if status not in _DIRECTIVE_STATUSES:
        raise ValueError(
            f"Invalid status {status!r}. Valid: {sorted(_DIRECTIVE_STATUSES)}"
        )
    if kind is not None and kind not in _LOOP_KINDS:
        raise ValueError(f"Invalid kind {kind!r}. Valid: {sorted(_LOOP_KINDS)}")

    if dt is None:
        dt = datetime.now(timezone.utc)
    now_iso = dt.isoformat(timespec="microseconds").replace("+00:00", "Z")

    if directive_id is None:
        directive_id = make_directive_id(directive_type, dt)

    return {
        "schema": DIRECTIVE_SCHEMA,
        "id": directive_id,
        "directive_type": directive_type,
        # "from" is the standard JSON key name for the issuing agent. We use
        # "from" (not "from_agent") in the wire format to match the spec and keep
        # the record self-describing. Python callers pass from_agent= to avoid the
        # reserved-word clash.
        "from": from_agent,
        "audience": audience,
        "title": title,
        "summary": summary,
        "next_action": next_action,
        "priority": priority,
        "workstream": workstream,
        "status": status,
        # acked_by: agents (or humans) who have acknowledged receipt. Starts
        # empty; the dual-write folds the per-agent ack sub-log union in on
        # each re-mirror (directives.dual_write), so it never shrinks below
        # the durable sub-log truth.
        "acked_by": [],
        "artifact_ref": artifact_ref,
        "not_before": not_before,
        "due": due,
        # routing: the route-event history. Starts empty; the dual-write folds
        # the append-only routing sub-log shards in on each re-mirror, so a
        # re-routed review carries its full route trail.
        "routing": [],
        "created_at": now_iso,
        "updated_at": now_iso,
        # Back-reference to the task this record mirrors (the dual-write sets
        # it). None for a directive that mirrors no task.
        "task_id": task_id,
        # --- Loop fields (additive; see _LOOP_KEYS note) ---
        "kind": kind,
        "state": state,
        # outcome is NEVER set at creation: the closed-loop guarantee says the
        # outcome arrives only as a bus response event (loop_ops.cmd_respond /
        # review-done). Creating it as None makes that invariant inspectable.
        "outcome": None,
        "expects_response": bool(expects_response),
        "sla_hours": sla_hours,
    }


def validate_directive(d: dict) -> list[str]:
    """Return a list of human-readable validation problems (empty = valid).

    Mirrors validate_task in style: iterates over required fields, checks
    the schema string, and validates the directive_type enum. Returns ALL
    problems found — not just the first — so callers can surface a complete
    picture to the user.

    WHY pure (no raises): validators that raise stop at the first problem.
    A list-returning validator lets the CLI / inbox renderer show everything
    wrong with an inbound directive in one pass, which matters for triage.

    NOT on the write path today: production validation happens make_*-side
    (``make_directive`` constructs only valid shapes); this is the invariant
    check the test suite runs against emitted records. Wiring it into the
    write path would be a behavior change — don't do it casually.
    """
    errors: list[str] = []

    missing = sorted(_DIRECTIVE_KEYS - set(d.keys()))
    extra = sorted(set(d.keys()) - _DIRECTIVE_KEYS - _LOOP_KEYS - _CONTINUITY_KEYS)
    for field in missing:
        errors.append(f"Missing required field: {field!r}")
    for field in extra:
        errors.append(f"Unexpected field: {field!r}")

    # Required non-empty string fields — same pattern as validate_task.
    required_str = [
        "id", "directive_type", "from", "audience", "title",
        "priority", "workstream", "status", "schema", "created_at", "updated_at",
    ]
    for field in required_str:
        if field not in d:
            continue
        val = d[field]
        # A string field is "empty" if it is falsy OR whitespace-only — a bare
        # ``not val`` would PASS "   " (``not "   "`` is False), letting a
        # whitespace-only required string slip through. The ``val != 0`` guard
        # preserves the numeric-zero allowance (0 is a legitimate value, not
        # "empty"); non-string types have their own type checks below, so we
        # only apply the strip-emptiness rule to actual ``str`` values.
        is_empty = (not val and val != 0) or (isinstance(val, str) and val.strip() == "")
        if is_empty:
            errors.append(f"Required field {field!r} must be non-empty.")

    # Schema string check — must be the exact constant, not a task schema etc.
    schema = d.get("schema", "")
    if schema and schema != DIRECTIVE_SCHEMA:
        errors.append(
            f"Wrong schema {schema!r}; expected {DIRECTIVE_SCHEMA!r}."
        )

    # directive_type enum — only flag if the field is present AND non-empty
    # (the missing/empty case is already covered above).
    dtype = d.get("directive_type", "")
    if dtype and dtype not in _DIRECTIVE_TYPES:
        errors.append(
            f"Unknown directive_type {dtype!r}. Valid: {sorted(_DIRECTIVE_TYPES)}"
        )

    # status enum — same present-and-non-empty guard as directive_type.
    dstatus = d.get("status", "")
    if dstatus and dstatus not in _DIRECTIVE_STATUSES:
        errors.append(
            f"Unknown status {dstatus!r}. Valid: {sorted(_DIRECTIVE_STATUSES)}"
        )

    priority = d.get("priority", "")
    if priority and priority not in VALID_PRIORITIES:
        errors.append(
            f"Unknown priority {priority!r}. Valid: {sorted(VALID_PRIORITIES)}"
        )

    # kind enum — present-and-non-empty only (absent = legacy record = fine).
    kind = d.get("kind")
    if kind and kind not in _LOOP_KINDS:
        errors.append(f"Unknown kind {kind!r}. Valid: {sorted(_LOOP_KINDS)}")

    if "acked_by" in d and not isinstance(d.get("acked_by"), list):
        errors.append("Field 'acked_by' must be a list.")
    if "routing" in d and not isinstance(d.get("routing"), list):
        errors.append("Field 'routing' must be a list.")
    if (
        "artifact_ref" in d
        and d.get("artifact_ref") is not None
        and not isinstance(d.get("artifact_ref"), dict)
    ):
        errors.append("Field 'artifact_ref' must be an object or None.")
    for field in ("not_before", "due", "task_id", "summary", "next_action"):
        if (
            field in d
            and d.get(field) is not None
            and not isinstance(d.get(field), str)
        ):
            errors.append(f"Field {field!r} must be a string or None.")

    # Timestamp format — created_at/updated_at must follow the bus convention
    # (ISO-8601 UTC with a trailing ``Z``, as emitted by make_directive). We
    # check format only when the field is present and non-empty; the
    # missing/empty case is already covered by the required_str loop above. A
    # value that doesn't parse, or that lacks the trailing ``Z``, is flagged —
    # an offset form like ``+00:00`` parses fine but violates the bus
    # convention, so the explicit ``Z`` suffix is required.
    for field in ("created_at", "updated_at"):
        val = d.get(field)
        if isinstance(val, str) and val.strip():
            ok = val.endswith("Z")
            if ok:
                try:
                    datetime.fromisoformat(val.replace("Z", "+00:00"))
                except ValueError:
                    ok = False
            if not ok:
                errors.append(
                    f"Field {field!r} must be an ISO-8601 UTC timestamp "
                    f"ending in 'Z' (got {val!r})."
                )

    return errors


# ---------------------------------------------------------------------------
# Role registry record (roles-as-durable-identity, spec 2026-06-10)
# ---------------------------------------------------------------------------

ROLE_SCHEMA = "fulcra.coordination.role.v1"
ROLE_LEASE_SCHEMA = "fulcra.coordination.role_lease.v1"

# Holder policies. WHY only two:
#   shared    — fan-out: every fresh lease-holder is a holder (the #128
#               @role-audience default; reviews, triage, anything pooled).
#   exclusive — one active holder; a claim while another FRESH lease exists is
#               CONTESTED (visible, never silently double-held). A stale lease
#               is claimable immediately — sessions die, roles must not.
_ROLE_POLICIES = {"shared", "exclusive"}

_ROLE_KEYS = {
    "schema", "name", "description", "standing_instructions", "policy",
    "sla_hours", "maintainer", "checkpoint_ref", "holders",
    "created_at", "updated_at",
}


def make_role(
    name: str,
    description: str,
    *,
    standing_instructions: str = "",
    policy: str = "shared",
    sla_hours: Optional[int] = None,
    maintainer: Optional[str] = None,
    checkpoint_ref: Optional[str] = None,
    dt: Optional[datetime] = None,
) -> dict[str, Any]:
    """Build a role registry record (``roles/<name>.json``). Mirrors
    make_directive in style: validate enums up front, stamp bus timestamps,
    return a plain dict.

    THE INVERSION (spec 2026-06-10): the ROLE is the durable identity; a
    session is an ephemeral lease on it. ``standing_instructions`` is the job
    description — runbooks, conventions, where the role's state lives — so ANY
    fresh session that claims the role knows what to do. ``maintainer`` is the
    escalation edge: who gets the directive when the role sits vacant past
    ``sla_hours``. ``checkpoint_ref`` is the role's durable resume point:
    set by ``checkpoint --role`` and the session-exit ``park``, read by
    ``continuity_ops``, and printed (ref + resume brief) when a session
    claims the role.

    GENERALIZATION RULE (non-negotiable): core ships the MECHANISM only —
    role names are adopter data written through this constructor at runtime,
    never constants in source (pinned by tests/test_roles.py).

    ``holders`` is a PROJECTION (current lease-holders), never authoritative:
    the per-agent lease sub-log + presence freshness is the truth; this field
    exists so a bare registry read is self-describing. Starts empty."""
    if not name or not str(name).strip():
        raise ValueError("name must be a non-empty string.")
    if policy not in _ROLE_POLICIES:
        raise ValueError(
            f"Invalid policy {policy!r}. Valid: {sorted(_ROLE_POLICIES)}")
    if dt is None:
        dt = datetime.now(timezone.utc)
    now_iso = dt.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return {
        "schema": ROLE_SCHEMA,
        "name": str(name).strip(),
        "description": description or "",
        "standing_instructions": standing_instructions or "",
        "policy": policy,
        "sla_hours": sla_hours,
        "maintainer": maintainer,
        "checkpoint_ref": checkpoint_ref,
        "holders": [],
        "created_at": now_iso,
        "updated_at": now_iso,
    }


def validate_role(r: dict) -> list[str]:
    """Return a list of human-readable validation problems (empty = valid).

    Mirrors validate_directive exactly in style: collect ALL problems (missing
    + unexpected keys, empty required strings, enum/type violations, timestamp
    format) rather than raising at the first — the CLI can then show an
    operator everything wrong with a registry record in one pass.

    ``description``/``standing_instructions`` may legitimately be EMPTY
    strings (a claim on an unregistered role self-registers a minimal record);
    only their TYPE is checked.

    NOT on the write path today: production validation happens make_*-side
    (``make_role`` constructs only valid shapes); this is the invariant check
    the test suite runs against emitted records. Wiring it into the write
    path would be a behavior change — don't do it casually."""
    errors: list[str] = []

    missing = sorted(_ROLE_KEYS - set(r.keys()))
    extra = sorted(set(r.keys()) - _ROLE_KEYS)
    for field in missing:
        errors.append(f"Missing required field: {field!r}")
    for field in extra:
        errors.append(f"Unexpected field: {field!r}")

    for field in ("name", "policy", "schema", "created_at", "updated_at"):
        if field not in r:
            continue
        val = r[field]
        if not val or (isinstance(val, str) and val.strip() == ""):
            errors.append(f"Required field {field!r} must be non-empty.")

    schema = r.get("schema", "")
    if schema and schema != ROLE_SCHEMA:
        errors.append(f"Wrong schema {schema!r}; expected {ROLE_SCHEMA!r}.")

    policy = r.get("policy", "")
    if policy and policy not in _ROLE_POLICIES:
        errors.append(
            f"Unknown policy {policy!r}. Valid: {sorted(_ROLE_POLICIES)}")

    if (
        "sla_hours" in r
        and r.get("sla_hours") is not None
        and not isinstance(r.get("sla_hours"), (int, float))
    ):
        errors.append("Field 'sla_hours' must be a number or None.")
    for field in ("description", "standing_instructions"):
        if field in r and not isinstance(r.get(field), str):
            errors.append(f"Field {field!r} must be a string.")
    for field in ("maintainer", "checkpoint_ref"):
        if (
            field in r
            and r.get(field) is not None
            and not isinstance(r.get(field), str)
        ):
            errors.append(f"Field {field!r} must be a string or None.")
    if "holders" in r and not isinstance(r.get("holders"), list):
        errors.append("Field 'holders' must be a list.")

    # Same bus-timestamp convention check as validate_directive.
    for field in ("created_at", "updated_at"):
        val = r.get(field)
        if isinstance(val, str) and val.strip():
            ok = val.endswith("Z")
            if ok:
                try:
                    datetime.fromisoformat(val.replace("Z", "+00:00"))
                except ValueError:
                    ok = False
            if not ok:
                errors.append(
                    f"Field {field!r} must be an ISO-8601 UTC timestamp "
                    f"ending in 'Z' (got {val!r})."
                )

    return errors


def build_tags(
    *,
    status: str,
    workstream: str,
    agent: str,
    kind: str,
    priority: str,
    extra: Optional[list[str]] = None,
) -> list[str]:
    tags = [
        f"workstream:{workstream}",
        f"agent:{agent}",
        f"kind:{kind}",
        f"status:{status}",
        f"priority:{priority}",
    ]
    if extra:
        tags.extend(extra)
    return sorted(set(tags))


# ---------------------------------------------------------------------------
# Status transition enforcement
# ---------------------------------------------------------------------------

def apply_transition(
    task: dict[str, Any],
    new_status: str,
    *,
    by: str,
    summary: Optional[str] = None,
    next_action: Optional[str] = None,
    blocked_on: Optional[str] = None,
    evidence: Optional[str] = None,
    verification_level: Optional[str] = None,
    confidence: Optional[str] = None,
    reason: Optional[str] = None,
    dt: Optional[datetime] = None,
) -> dict[str, Any]:
    """Apply a status transition to a task, returning a modified copy."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    now_iso = dt.isoformat(timespec="microseconds").replace("+00:00", "Z")

    current = task["status"]
    if new_status not in VALID_STATUSES:
        raise TransitionError(f"Unknown status {new_status!r}.")
    if current in TERMINAL_STATUSES and new_status != current:
        raise TransitionError(
            f"Cannot transition from terminal status {current!r} without explicit reopen. "
            f"Use 'reopen' subcommand (not yet implemented)."
        )
    allowed = STATUS_TRANSITIONS.get(current, set())
    if new_status not in allowed and new_status != current:
        raise TransitionError(
            f"Transition {current!r} -> {new_status!r} is not allowed. "
            f"Allowed: {sorted(allowed) or ['none (terminal)']}"
        )

    # Validate done requirements
    if new_status == "done":
        if not evidence:
            raise SchemaError("'done' transition requires --evidence.")
        if not verification_level:
            raise SchemaError("'done' transition requires --verification-level.")
        if verification_level not in VALID_VERIFICATION_LEVELS:
            raise SchemaError(
                f"Unknown verification_level {verification_level!r}. "
                f"Valid: {sorted(VALID_VERIFICATION_LEVELS)}"
            )

    import copy
    task = copy.deepcopy(task)
    task["status"] = new_status
    task["updated_at"] = now_iso
    task["last_touched_by"] = by

    if summary is not None:
        task["current_summary"] = summary
    if next_action is not None:
        task["next_action"] = next_action
    if blocked_on is not None:
        task["blocked_on"] = blocked_on
    elif new_status != "blocked":
        task["blocked_on"] = None

    if new_status == "done":
        task["done"] = {
            "done_at": now_iso,
            "done_by": by,
            "evidence": evidence,
            "verification_level": verification_level,
            "confidence": confidence,
        }

    # Rebuild tags, preserving any non-standard tags
    _standard_prefixes = ("workstream:", "agent:", "kind:", "status:", "priority:")
    _extra = [t for t in task.get("tags", []) if not any(t.startswith(p) for p in _standard_prefixes)]
    # BUG 2: read workstream/owner_agent/priority/tags with .get so a transition
    # on a slightly-malformed body (missing one of these) rebuilds tags instead
    # of KeyError-ing mid-write and leaving the task half-updated. Defaults match
    # build_tags' own ("" / P9) so a normal task is unaffected.
    # BUG 3 (live-found 2026-06-10): preserve SECONDARY kind tags. Multi-kind
    # membership markers like kind:review / kind:review-verdict are routing-
    # load-bearing — collapsing every kind: tag to the single extracted primary
    # (kind:ops sorts before kind:review) meant a reviewer CLAIMING a review
    # (proposed->active) silently dropped kind:review, after which
    # is_review_directive() was False and review-done could not resolve the
    # original request. The read side (routing.is_review_directive) already
    # documents this exact hazard; carry every non-primary kind: tag through
    # as an extra so the write side honors it too.
    _primary_kind = _extract_kind_from_tags(task.get("tags", []))
    _secondary_kinds = [
        t for t in task.get("tags", [])
        if t.startswith("kind:") and t != f"kind:{_primary_kind}"
    ]
    task["tags"] = build_tags(
        status=new_status,
        workstream=task.get("workstream", ""),
        agent=task.get("owner_agent", ""),
        kind=_primary_kind,
        priority=task.get("priority", "P9"),
        extra=(_extra + _secondary_kinds) or None,
    )

    # Append event (bounded to MAX_EVENTS_INLINE)
    event_summary = reason or summary or f"Status changed to {new_status}."
    if new_status == "done":
        event_summary = f"Marked done. Evidence: {evidence}"
    elif new_status == "abandoned":
        event_summary = f"Abandoned. Reason: {reason or 'no reason given'}"

    event = {
        "at": now_iso,
        "type": new_status,
        "by": by,
        "summary": event_summary,
        "evidence": evidence,
    }
    events = task.get("events", [])
    events.append(event)
    task["events"] = events[-MAX_EVENTS_INLINE:]

    return task


def apply_update(
    task: dict[str, Any],
    *,
    by: str,
    summary: Optional[str] = None,
    next_action: Optional[str] = None,
    blocked_on: Optional[str] = None,
    dt: Optional[datetime] = None,
) -> dict[str, Any]:
    """Apply a non-status-changing update to a task."""
    if dt is None:
        dt = datetime.now(timezone.utc)
    now_iso = dt.isoformat(timespec="microseconds").replace("+00:00", "Z")

    import copy
    task = copy.deepcopy(task)
    task["updated_at"] = now_iso
    task["last_touched_by"] = by

    if summary is not None:
        task["current_summary"] = summary
    if next_action is not None:
        task["next_action"] = next_action
    if blocked_on is not None:
        task["blocked_on"] = blocked_on

    event = {
        "at": now_iso,
        "type": "updated",
        "by": by,
        "summary": summary or "Task updated.",
        "evidence": None,
    }
    events = task.get("events", [])
    events.append(event)
    task["events"] = events[-MAX_EVENTS_INLINE:]

    return task


# Non-status events: recognized event types that append to the log WITHOUT
# changing task status. `inbox_ack` records that the assignee has *seen* a
# directive (so the listener stops re-notifying) without claiming it — distinct
# from claiming, which is a real `active` status transition that changes owner.
NON_STATUS_EVENTS = {"inbox_ack"}


def apply_event(
    task: dict[str, Any],
    event_type: str,
    *,
    by: str,
    summary: Optional[str] = None,
    evidence: Optional[str] = None,
    dt: Optional[datetime] = None,
) -> dict[str, Any]:
    """Append a recognized non-status event to a task, returning a modified copy.

    Used for events like ``inbox_ack`` that record a fact in the event log but
    deliberately leave ``status`` untouched (unlike apply_transition). Keeping
    these out of the status machine means an ack never trips the transition
    guards and never looks like a status change to the merge logic.
    """
    if event_type not in NON_STATUS_EVENTS:
        raise SchemaError(
            f"Unknown non-status event {event_type!r}. "
            f"Valid: {sorted(NON_STATUS_EVENTS)}"
        )
    if dt is None:
        dt = datetime.now(timezone.utc)
    now_iso = dt.isoformat(timespec="microseconds").replace("+00:00", "Z")

    import copy
    task = copy.deepcopy(task)
    task["updated_at"] = now_iso
    task["last_touched_by"] = by

    event = {
        "at": now_iso,
        "type": event_type,
        "by": by,
        "summary": summary or f"{event_type} by {by}.",
        "evidence": evidence,
    }
    events = task.get("events", [])
    events.append(event)
    task["events"] = events[-MAX_EVENTS_INLINE:]
    return task


def validate_task(task: dict[str, Any]) -> list[str]:
    """Return a list of validation errors, or empty list if valid."""
    errors = []
    required = ["schema", "id", "title", "status", "workstream", "owner_agent", "created_at"]
    for field in required:
        if field not in task:
            errors.append(f"Missing required field: {field!r}")

    status = task.get("status", "")
    if status and status not in VALID_STATUSES:
        errors.append(f"Invalid status: {status!r}")

    if not re.match(r"^TASK-\d{8}-[a-z0-9-]+-[0-9a-f]{8}$", task.get("id", "")):
        errors.append(f"Invalid task id format: {task.get('id', '')!r}")

    return errors


def _extract_kind_from_tags(tags: list[str]) -> str:
    """The task's PRIMARY kind from its tags.

    2026-06-11 bug hunt C7: prefer a VALID_KINDS member. Membership markers
    (kind:idea, kind:review-verdict, …) can sort AHEAD of the real schema kind
    in the sorted tag list; blindly returning the first ``kind:`` suffix made
    downstream tag rebuilds (writepipe._repair_merged_tags, apply_transition)
    treat the marker as primary — and the actual standard kind tag, being
    "standard" and non-primary, was then dropped from the rebuilt tag set.
    Falls back to the first kind tag (no valid member present), then "ops"."""
    first = ""
    for tag in tags:
        if tag.startswith("kind:"):
            suffix = tag[5:]
            if suffix in VALID_KINDS:
                return suffix
            if not first:
                first = suffix
    return first or "ops"


# ---------------------------------------------------------------------------
# Compact summary for views
# ---------------------------------------------------------------------------

def task_summary(task: dict[str, Any]) -> dict[str, Any]:
    """Return a compact summary suitable for index/view inclusion.

    M3: ``priority`` and ``updated_at`` are read with ``.get`` defaults (matching
    the defaults used everywhere else — ``P9`` / ``""``). A task missing
    ``updated_at`` is treated as maximally stale by ``is_stale`` and so reaches
    this function on the needs-attention / reconcile path; hard-indexing those
    keys raised KeyError on exactly the dangling tasks the safety-net view exists
    to surface. The defaults let such a task render instead of crashing the view.

    PERF (summaries as a complete view source): every field a view builder reads
    MUST appear here, or build_all_views(summaries) would silently diverge from
    build_all_views(full_bodies) — and the write path now rebuilds views from the
    summaries aggregate, never re-fetched bodies. Two fields were previously
    omitted and are now included:

      * ``last_touched_by`` — the operator digest's per-agent fold
        (views.build_operator_digest) groups finished work by owner_agent OR
        last_touched_by, so a hand-off agent (touched but doesn't own) would
        lose its done-this-window credit if this were dropped.
      * ``done_at`` — flattened from ``done.done_at`` (which the full body nests).
        build_search_index / build_recently_done / build_workstream_view all
        gate done/abandoned tasks on this timestamp. A summary has no nested
        ``done`` block, so the flattened key is what those builders read.

    IDEMPOTENCE: task_summary(task_summary(t)) == task_summary(t). A summary has
    no nested ``done`` dict but carries the flat ``done_at``, so we resolve it
    from either source — re-summarizing a summary preserves the value rather than
    nulling it. This makes summaries safely re-summarizable on the rebuild path.
    """
    # Resolve the done timestamp from the nested full-body block OR an already-
    # flattened summary key, so re-summarizing a summary is a fixpoint.
    done_at = (task.get("done") or {}).get("done_at") or task.get("done_at")
    # Flatten the set of agents who have acked this as an inbox directive. The
    # inbox builders (views.is_open_directive / inbox_for) need to know "has <me>
    # acked this" to drop a handled directive — a fact that lives only in the
    # event log on a full body. A summary carries no events, so without this the
    # rebuilt inbox view would re-surface acked directives forever (and
    # build_all_views(summaries) would diverge from full bodies on any acked
    # directive). Derived from inbox_ack events when present, else carried through
    # from an existing summary's acked_by — keeping task_summary idempotent.
    if "acked_by" in task and "events" not in task:
        acked_by = list(task.get("acked_by") or [])
    else:
        acked_by = sorted({
            e.get("by") for e in task.get("events", [])
            if e.get("type") == "inbox_ack" and e.get("by")
        })
    # BUG 2: id/title/status/workstream/owner_agent were hard-indexed, so a body
    # missing ANY of them raised KeyError. task_summary runs in the best-effort
    # view loader where that exception is swallowed -> the task silently VANISHED
    # from every view. Read them all with .get + empty-string defaults so a
    # malformed task RENDERS (and is thus visible/fixable) instead of disappearing
    # — the defensive behaviour the docstring already promised. Symmetric with the
    # assignee/priority/scheduling fields that were already .get-guarded.
    return {
        "id": task.get("id", ""),
        "title": task.get("title", ""),
        "status": task.get("status", ""),
        "priority": task.get("priority", "P9"),
        "workstream": task.get("workstream", ""),
        "owner_agent": task.get("owner_agent", ""),
        # Read with .get so summaries of pre-assignee tasks (or any task built
        # before this field existed) render None rather than KeyError-crashing
        # the view — symmetric with priority/updated_at above.
        "assignee": task.get("assignee"),
        # last_touched_by drives the operator digest's hand-off grouping
        # (build_operator_digest's per-agent done fold); default to owner_agent
        # (the touching agent on a brand-new task) so an old task body missing
        # the field summarizes sensibly rather than to None.
        "last_touched_by": task.get("last_touched_by", task.get("owner_agent")),
        "current_summary": task.get("current_summary", ""),
        "next_action": task.get("next_action", ""),
        "blocked_on": task.get("blocked_on"),
        # Review-loop artifact ref. Carried on the summary so the search index
        # (build_search_index) and the summaries-aggregate search fallback can
        # surface it; omitting it made `search` report pr=None for review tasks,
        # which (with assignee) misread assigned verdicts as orphaned. .get so a
        # non-review/pre-field body summarizes to None, not KeyError.
        "pr": task.get("pr"),
        # Scheduling fields, read with .get so a pre-feature body summarizes to
        # None rather than KeyError-crashing the view (symmetric with
        # assignee/priority above). These MUST be carried on the summary because
        # the write path rebuilds views from the summaries aggregate, and
        # needs_human / upcoming_for_human read not_before/due off the summary —
        # omitting them would make build_all_views(summaries) diverge from
        # build_all_views(bodies) (TestBuildAllViewsEquivalence).
        "not_before": task.get("not_before"),
        "due": task.get("due"),
        "tags": task.get("tags", []),
        # Continuity handoff ref (spec 2026-06-10): the inbox/claim surfaces
        # read summaries, never full bodies, so the OPAQUE ref must ride the
        # summary or the recipient would have to fetch the body just to learn
        # a resume point exists. Plain .get carry-through keeps task_summary
        # idempotent and pre-continuity records summarize to None. The INLINE
        # fallback body deliberately does NOT ride summaries (size; the claim
        # path loads the full body anyway).
        "checkpoint_ref": task.get("checkpoint_ref"),
        "updated_at": task.get("updated_at", ""),
        # Flattened done timestamp (see docstring): the single key every
        # done/abandoned-gating view builder reads off a summary.
        "done_at": done_at,
        # Agents who have inbox-acked this directive (see docstring) — the inbox
        # builders read this off a summary instead of scanning the event log.
        "acked_by": acked_by,
        "task_file": task_file_path(task.get("id", "")),
    }
