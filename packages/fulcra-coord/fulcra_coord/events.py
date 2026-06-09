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


def _at_sort_key(at: str) -> str:
    """Return the numeric-microsecond sort prefix for an ISO-8601 *at* string.

    Strips ``:``, ``-``, ``.``, and ``Z`` so a timestamp like
    ``2026-06-08T15:30:45.123456Z`` collapses to ``20260608T153045123456``.

    WHY this and not a raw string compare: a lexical sort of the raw ``at``
    string INVERTS when two timestamps differ only in trailing precision or
    offset. ``"2026-06-08T00:00:00Z"`` vs ``"2026-06-08T00:00:00.000001Z"`` —
    the higher-precision form sorts FIRST under a raw compare because ``.``
    (0x2E) < ``Z`` (0x5A), even though it is LATER in real time. Removing the
    punctuation yields a fixed-shape numeric string whose lexical order equals
    chronological order. This is the single source of truth for the
    normalization, shared by :func:`event_id` (its sortable-ts prefix) and
    :func:`fold_task` (its event ordering) so the two can never drift apart.
    """
    return at.replace(":", "").replace("-", "").replace(".", "").replace("Z", "")


def event_id(*, at: str) -> str:
    """Return a time-sortable unique event id derived from *at*.

    Format: ``<sortable-ts>-<rand>``

    *sortable-ts* is *at* with all of ``:``, ``-``, ``.``, and ``Z``
    removed — leaving the numeric representation of the UTC microsecond
    instant (e.g. ``20260608T153045123456``).  Ids from events with
    earlier timestamps will sort lexicographically before ids from later
    ones.  The normalization is delegated to :func:`_at_sort_key` so the id
    prefix and the fold's event ordering share one definition.

    *rand* is the first 12 hex characters of a random UUID4.  This makes
    two ids generated in the same microsecond distinct while keeping the
    total length manageable (~35 chars including the separator).

    Args:
        at: The UTC ISO-8601 timestamp string (bus convention — trailing
            ``Z``, microsecond precision) that anchors this event.

    Returns:
        A string of the form ``<sortable-ts>-<rand12>``.
    """
    sortable_ts = _at_sort_key(at)
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


def _is_snapshot_payload(payload: dict[str, Any]) -> bool:
    """Return True if *payload* is a full-task snapshot, False if it is a legacy delta.

    A full-task snapshot carries the task schema marker (``"schema"`` key) AND
    the task id (``"id"`` key).  Phase-1 delta payloads carry neither — they are
    just a subset of changed fields (title, status, current_summary, etc.).  This
    discriminator is clean: none of the Phase-1 delta keys overlap with ``schema``
    or ``id``, so there are no false positives.

    The distinction drives ``fold_task``'s merge strategy:
    * snapshot → ``state = dict(payload)`` (replace wholesale; latest snapshot wins)
    * delta    → shallow field-merge into accumulated state (back-compat, unchanged)
    """
    return bool(payload.get("schema")) and bool(payload.get("id"))


def fold_task(evs: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministically reduce an event list to a task snapshot.

    This is the pure kernel the coordination system rides on.  It is
    intentionally boring so every behaviour can be proven by a unit test
    against a plain list — no I/O, no service calls.

    Rules (in application order):

    1. **Sort by (numeric-instant, event_id)** — events are ordered by the
       NUMERIC microsecond instant of ``at`` (via :func:`_at_sort_key`), not
       the raw ``at`` string.  A raw-string sort inverts when timestamps differ
       only in trailing precision/offset (``...00Z`` would sort AFTER
       ``...00.000001Z`` because ``.`` < ``Z``, despite being earlier in time);
       normalizing to the punctuation-stripped numeric form makes lexical order
       equal chronological order.  ``event_id`` is a stable tie-breaker when two
       events share the same microsecond (its sortable-ts prefix encodes the
       same instant and the random suffix provides lexicographic uniqueness).

    2. **Dedup retries by (actor, idempotency_key)** — when a caller supplies
       a truthy ``idempotency_key``, only the first occurrence (in sort order)
       of the compound ``(actor, idempotency_key)`` pair is applied; later
       duplicates are silently skipped.  A falsy ``idempotency_key`` (``None``
       or ``""``) means "no dedup key — always apply this event."

    3. **Snapshot vs. delta merge strategy** — Phase 2a introduced full-task
       snapshot payloads (distinguishable via :func:`_is_snapshot_payload`).
       The merge strategy depends on the payload type:

       * **Snapshot** (``payload.get("schema")`` and ``payload.get("id")`` are
         both truthy) → ``state = dict(payload)`` — the snapshot *replaces* the
         accumulated state wholesale.  The latest snapshot in sort order wins,
         and any fields present in earlier events but absent from the latest
         snapshot are dropped (they are stale).
       * **Delta** (Phase-1 legacy — payload carries only a field subset) →
         shallow last-write-wins field merge into the accumulated state, exactly
         as before.  This preserves full backward-compat with events already on
         the live bus.

       The two strategies compose correctly in any order: a delta after a
       snapshot merges on top of the snapshot state; a delta before a snapshot
       is overwritten when the snapshot replaces state.

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
       all events (fallback when the fold produced no snapshot that carried an
       ``id`` key); ``state["_applied_event_count"]`` records how many events
       were actually applied after deduplication.

    Args:
        evs: List of event envelopes, each produced by :func:`make_event`.
             All events must share the same ``task_id``.  An empty list
             returns ``{"id": None, "_applied_event_count": 0}`` (caller
             should never fold an empty stream in practice, but the function
             handles it gracefully rather than raising).

    Returns:
        A plain ``dict`` representing the current task snapshot.  Keys come
        from payload merges/replacements plus the ``id`` and
        ``_applied_event_count`` bookkeeping fields.  Use
        :func:`fold_is_complete` to determine whether the result was
        reconstructed from a full snapshot (trustworthy) or only from legacy
        deltas (may be incomplete).
    """
    ordered = sorted(evs, key=lambda e: (_at_sort_key(e.get("at", "")), e.get("event_id", "")))
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

        payload = e.get("payload") or {}
        if _is_snapshot_payload(payload):
            # Full-task snapshot: replace the accumulated state wholesale.
            # The snapshot carries the complete task at this point in time, so
            # any fields from earlier events are stale and should be dropped.
            state = dict(payload)
        else:
            # Legacy Phase-1 delta: shallow last-write-wins merge.  Terminal
            # stickiness is emergent — a later delta without a ``status`` key
            # simply does not overwrite an existing terminal status.
            for k, v in payload.items():
                state[k] = v

        applied += 1

    # ``state["id"]`` may already be set if a snapshot payload carried it.
    # Fall back to the shared task_id from the event envelope so it is always
    # present, even for a delta-only fold or an empty event list.
    state["id"] = state.get("id") or (evs[0]["task_id"] if evs else None)
    state["_applied_event_count"] = applied
    return state


def fold_is_complete(state: dict[str, Any]) -> bool:
    """True iff the fold reconstructed a full schema-valid task.

    A complete fold means **at least one full-task snapshot** has been applied
    (the folded state carries both the task schema marker and an id) — so the
    result has the full schema, whether the latest event was that snapshot or a
    later delta that merged a few fields on top of it.  It is NOT "the latest
    event was a snapshot": a delta arriving after a snapshot still leaves a
    complete fold.  Phase 2b uses this to decide whether to trust the fold
    result or fall back to the mutable task file for a task whose events are
    delta-only (not yet snapshotted by the dual-write path).

    Returns False for:
    * delta-only folds (Phase-1 events, no snapshot ever emitted for this task)
    * the empty-list sentinel (``{"id": None, "_applied_event_count": 0}``)
    """
    return bool(state.get("schema")) and bool(state.get("id"))
