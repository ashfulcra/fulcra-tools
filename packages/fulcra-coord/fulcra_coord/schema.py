"""Task schema, status machine, and validation for fulcra-coord."""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from . import SCHEMA_VERSION, task_file_path

# ---------------------------------------------------------------------------
# Valid statuses and allowed transitions
# ---------------------------------------------------------------------------

VALID_STATUSES = {"proposed", "active", "waiting", "blocked", "done", "abandoned"}

TERMINAL_STATUSES = {"done", "abandoned"}

# Maps current status -> set of allowed next statuses
STATUS_TRANSITIONS: dict[str, set[str]] = {
    "proposed": {"active", "waiting", "abandoned"},
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
    task_id: Optional[str] = None,
    dt: Optional[datetime] = None,
) -> dict[str, Any]:
    if dt is None:
        dt = datetime.now(timezone.utc)
    now_iso = dt.isoformat().replace("+00:00", "Z")

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


def make_presence(
    agent: str,
    *,
    workstreams: Optional[list[str]] = None,
    summary: str = "",
    last_seen: Optional[str] = None,
    session: Optional[str] = None,
) -> dict[str, Any]:
    """Build a validated per-agent presence record (``presence/<slug>.json``).

    Presence is the situational-awareness primitive: an agent declares the major
    workstream(s) it is currently on, so the human sees what it is working on
    EVEN WHEN it owns no active coordination task. Only the agent itself writes
    its own record, so there is no cross-agent write contention — mirroring the
    per-entity tasks→views pattern.

    ``last_seen`` defaults to now (ISO-Z) so the liveness model in
    views.build_presence can age it; ``session`` is an opaque optional key the
    connecting surface may pass for traceability."""
    if last_seen is None:
        last_seen = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "schema": PRESENCE_SCHEMA,
        "agent": agent,
        "workstreams": _normalize_workstreams(workstreams),
        "summary": summary or "",
        "last_seen": last_seen,
        "session": session,
    }


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
    now_iso = dt.isoformat().replace("+00:00", "Z")

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
    task["tags"] = build_tags(
        status=new_status,
        workstream=task["workstream"],
        agent=task["owner_agent"],
        kind=_extract_kind_from_tags(task["tags"]),
        priority=task["priority"],
        extra=_extra or None,
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
    now_iso = dt.isoformat().replace("+00:00", "Z")

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
    now_iso = dt.isoformat().replace("+00:00", "Z")

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
    for tag in tags:
        if tag.startswith("kind:"):
            return tag[5:]
    return "ops"


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

      * ``last_touched_by`` — build_agent_view groups by owner_agent OR
        last_touched_by, so a hand-off agent (touched but doesn't own) would lose
        its per-agent view if this were dropped.
      * ``done_at`` — flattened from ``done.done_at`` (which the full body nests).
        build_search_index / build_recently_done / build_workstream_view /
        build_agent_view all gate done/abandoned tasks on this timestamp. A
        summary has no nested ``done`` block, so the flattened key is what those
        builders read.

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
    return {
        "id": task["id"],
        "title": task["title"],
        "status": task["status"],
        "priority": task.get("priority", "P9"),
        "workstream": task["workstream"],
        "owner_agent": task["owner_agent"],
        # Read with .get so summaries of pre-assignee tasks (or any task built
        # before this field existed) render None rather than KeyError-crashing
        # the view — symmetric with priority/updated_at above.
        "assignee": task.get("assignee"),
        # last_touched_by drives build_agent_view's hand-off grouping; default to
        # owner_agent (the touching agent on a brand-new task) so an old task body
        # missing the field summarizes sensibly rather than to None.
        "last_touched_by": task.get("last_touched_by", task.get("owner_agent")),
        "current_summary": task.get("current_summary", ""),
        "next_action": task.get("next_action", ""),
        "blocked_on": task.get("blocked_on"),
        "tags": task.get("tags", []),
        "updated_at": task.get("updated_at", ""),
        # Flattened done timestamp (see docstring): the single key every
        # done/abandoned-gating view builder reads off a summary.
        "done_at": done_at,
        # Agents who have inbox-acked this directive (see docstring) — the inbox
        # builders read this off a summary instead of scanning the event log.
        "acked_by": acked_by,
        "task_file": task_file_path(task["id"]),
    }
