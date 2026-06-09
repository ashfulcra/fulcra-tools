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
                          ``None`` when the caller does not require dedup.
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
