"""Immutable coordination events — the durable unit of truth.

PURE/no-I/O so the reducer is testable from event lists.

Leaf module: imports only ``fulcra_coord.timeutil`` and stdlib.  It must NOT
import any feature or visibility module (lifecycle, views, query, digest,
annotations, inbox, retention, presence, routing_ops, cli, writepipe, remote)
so that the reducer + test harness can stand up from a bare event list without
dragging in I/O or service dependencies.

Design notes
------------
``event_id`` encodes ``<sortable-ts>-<rand>``:

* **sortable-ts** — the ISO-8601 timestamp with `:`, `-`, `.`, and ``Z``
  stripped, leaving a monotonically-increasing numeric string (e.g.
  ``20260608T153045123456`` from ``2026-06-08T15:30:45.123456Z``).  Events
  from a single producer sort chronologically by this prefix without needing
  a shared sequence counter.
* **rand** — first 12 hex chars of a random UUID4, making each id unique
  within a microsecond even when two events share the same wall-clock instant.

The id is derived from ``at`` so its prefix encodes the same instant as the
envelope timestamp; the random suffix makes it unique even across concurrent
writers at the same microsecond.  Unlike a content-addressed hash, two calls
with the same ``at`` produce *different* ids — the id is NOT recomputable from
the envelope, so a reducer must store it on creation, never re-derive it.
"""

from __future__ import annotations

import uuid
from typing import Any

from fulcra_coord.timeutil import now_iso

EVENT_SCHEMA_VERSION = "fulcra.coordination.event.v1"


def event_id(*, at: str) -> str:
    """Return a time-sortable unique event id derived from *at*.

    Format: ``<sortable-ts>-<rand>``

    *sortable-ts* is *at* with all of ``:``, ``-``, ``.``, and ``Z``
    removed — leaving the numeric representation of the UTC microsecond
    instant (e.g. ``20260608T153045123456``).  Ids from events with
    earlier timestamps will sort lexicographically before ids from later
    ones.

    *rand* is the first 12 hex characters of a random UUID4.  This makes
    two ids generated in the same microsecond distinct while keeping the
    total length manageable (~35 chars including the separator).

    Args:
        at: The UTC ISO-8601 timestamp string (bus convention — trailing
            ``Z``, microsecond precision) that anchors this event.

    Returns:
        A string of the form ``<sortable-ts>-<rand12>``.
    """
    sortable_ts = at.replace(":", "").replace("-", "").replace(".", "").replace("Z", "")
    rand = uuid.uuid4().hex[:12]
    return f"{sortable_ts}-{rand}"


def make_event(
    *,
    family: str,
    task_id: str,
    kind: str,
    actor: str,
    payload: dict[str, Any],
    idempotency_key: str | None = None,
    at: str | None = None,
) -> dict[str, Any]:
    """Build an immutable coordination event envelope.

    The envelope is the canonical record of "something happened to a task
    on the bus."  Every field is explicit and typed; consumers must not add
    ad-hoc keys — extend ``payload`` or introduce a new event *kind* instead.

    ``at`` defaults to the current UTC wall-clock instant (``timeutil.now_iso``).
    Pass an explicit ``at`` when replaying or back-filling events so the
    stored timestamp reflects the *logical* time, not the write time.

    ``event_id`` is derived from ``at``, encoding chronological order in its
    prefix while the random suffix guarantees uniqueness within a microsecond.

    Args:
        family:           Broad category of the event (e.g. ``"tasks"``).
        task_id:          The task this event belongs to.
        kind:             Verb describing what happened (e.g. ``"updated"``).
        actor:            Who or what produced the event, in
                          ``role:host:project`` form.
        payload:          Arbitrary event-specific data.  Caller owns the
                          schema; no validation is performed here.
        idempotency_key:  Optional caller-supplied key the reducer uses to
                          deduplicate re-deliveries.  Dedup is keyed on the
                          compound ``(actor, idempotency_key)`` pair — the same
                          key from a *different* actor is a distinct event.
                          A falsy value (``None`` or ``""``) means "no dedup
                          key — this event is always applied regardless of
                          other events from the same actor."
        at:               UTC ISO-8601 timestamp.  Defaults to ``now_iso()``.

    Returns:
        A plain ``dict`` with keys: ``schema_version``, ``event_id``,
        ``family``, ``task_id``, ``kind``, ``actor``, ``at``,
        ``idempotency_key``, ``payload``.
    """
    if at is None:
        at = now_iso()
    return {
        "schema_version": EVENT_SCHEMA_VERSION,
        "event_id": event_id(at=at),
        "family": family,
        "task_id": task_id,
        "kind": kind,
        "actor": actor,
        "at": at,
        "idempotency_key": idempotency_key,
        "payload": payload,
    }


def fold_task(evs: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministically reduce an event list to a task snapshot.

    This is the pure kernel the coordination system rides on.  It is
    intentionally boring so every behaviour can be proven by a unit test
    against a plain list — no I/O, no service calls.

    Rules (in application order):

    1. **Sort by (at, event_id)** — ``at`` is the logical wall-clock instant;
       ``event_id`` is a stable tie-breaker when two events share the same
       microsecond (its sortable-ts prefix encodes the same instant and the
       random suffix provides lexicographic uniqueness).

    2. **Dedup retries by (actor, idempotency_key)** — when a caller supplies
       a truthy ``idempotency_key``, only the first occurrence (in sort order)
       of the compound ``(actor, idempotency_key)`` pair is applied; later
       duplicates are silently skipped.  A falsy ``idempotency_key`` (``None``
       or ``""``) means "no dedup key — always apply this event."

    3. **Shallow last-write-wins field merge** — each event's ``payload`` dict
       is merged into the accumulator state; the last event to set a key wins.
       This is intentionally shallow: nested dicts are replaced, not merged.
       Callers needing a deep-merge strategy should embed a versioned sub-key
       in the payload and manage merging above this layer.

    4. **Terminal stickiness is emergent, not special-cased** — a task's
       ``status`` is only changed by an event whose payload carries a
       ``status`` key, and events are applied in ``at`` order (rule 1), so the
       logically-last ``status`` wins.  Therefore:

       * A ``done`` followed by an update whose payload carries *no* ``status``
         key leaves ``status`` as ``"done"`` — the merge simply does not touch
         the key.
       * A ``done`` followed by an event that explicitly sets a non-terminal
         ``status`` (e.g. ``"active"``) reopens the task — the later write wins.
       * An event that is logically *earlier* (lower ``at``) can never revive a
         later terminal status, because sort order ensures the terminal event is
         applied last.

       There is no ``terminal_seen`` flag or suppression logic — stickiness is
       a consequence of pure LWW-by-sort, not a special case.

    5. **Bookkeeping** — ``state["id"]`` is set to the ``task_id`` shared by
       all events; ``state["_applied_event_count"]`` records how many events
       were actually applied after deduplication.

    Args:
        evs: List of event envelopes, each produced by :func:`make_event`.
             All events must share the same ``task_id``.  An empty list
             returns ``{"id": None, "_applied_event_count": 0}`` (caller
             should never fold an empty stream in practice, but the function
             handles it gracefully rather than raising).

    Returns:
        A plain ``dict`` representing the current task snapshot.  Keys come
        from payload merges plus the ``id`` and ``_applied_event_count``
        bookkeeping fields.
    """
    ordered = sorted(evs, key=lambda e: (e.get("at", ""), e.get("event_id", "")))
    seen: set[tuple[str, str]] = set()
    state: dict[str, Any] = {}
    applied = 0

    for e in ordered:
        ikey = e.get("idempotency_key")
        if ikey:
            dedup_key = (e.get("actor", ""), ikey)
            if dedup_key in seen:
                # Duplicate delivery — skip silently.
                continue
            seen.add(dedup_key)

        # Shallow last-write-wins merge — apply every payload key unconditionally.
        # Terminal stickiness is emergent: a later event without a ``status`` key
        # simply does not overwrite an existing terminal status; a later event
        # that explicitly sets a new status does overwrite it.  No suppression
        # logic is needed; pure sort-order LWW is the full spec.
        for k, v in (e.get("payload") or {}).items():
            state[k] = v

        applied += 1

    # Set after the loop so ``id`` is always present, even for an empty event list.
    state["id"] = evs[0]["task_id"] if evs else None
    state["_applied_event_count"] = applied
    return state
