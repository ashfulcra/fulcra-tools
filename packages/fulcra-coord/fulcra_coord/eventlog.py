"""Append-only event log I/O over the Fulcra Files store.

Writers only ever *create* a new immutable blob
(``events/tasks/<task_id>/<event_id>.json``) — they never overwrite a shared
file, which is what lets a no-CAS store stay correct under concurrent writers.
The path is keyed by the unique ``event_id`` (microsecond timestamp + random
suffix), so two writers for the same task at the same instant land on different
paths with no coordination required.

Leaf-adjacent module: imports only ``fulcra_coord.remote`` (the transport /
path layer) plus stdlib.  It deliberately does NOT import lifecycle, views,
query, or any other feature module — callers compose ``append_event`` /
``read_events`` with ``events.fold_task`` themselves, keeping I/O and reduction
cleanly separated.
"""

from __future__ import annotations

from typing import Any, Optional

from . import remote


def append_event(
    event: dict[str, Any],
    *,
    backend: Optional[list[str]] = None,
) -> bool:
    """Write one immutable event shard to the remote store.

    The shard path is derived from ``event["task_id"]`` and
    ``event["event_id"]``, so every call writes to a *distinct* path —
    even two concurrent appends for the same task in the same microsecond land
    on different filenames (the ``event_id`` random suffix guarantees this).
    As a result, correctness under concurrent writers requires no CAS and no
    locking.

    Args:
        event:   A plain dict produced by :func:`fulcra_coord.events.make_event`.
                 Must have ``"task_id"`` and ``"event_id"`` keys.
        backend: Optional explicit backend command list.  When ``None``,
                 ``remote.upload_json`` resolves the backend from the
                 ``FULCRA_COORD_BACKEND`` env var.

    Returns:
        ``True`` on a successful upload; ``False`` on transport failure
        (mirrors :func:`fulcra_coord.remote.upload_json`'s contract —
        signature is ``upload_json(data, remote_path, *, backend=...)``).
    """
    path = remote.event_remote_path(event["task_id"], event["event_id"])
    return remote.upload_json(event, path, backend=backend)


def read_events(
    task_id: str,
    *,
    backend: Optional[list[str]] = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Read all event shards for *task_id* from the remote store.

    Returns a list of ``(path, event_dict)`` pairs — one per shard found under
    the task's events prefix.  The list is **unordered**: callers that need a
    deterministic reduction should pass the extracted dicts to
    :func:`fulcra_coord.events.fold_task`, which sorts by ``(at, event_id)``.

    Missing prefix (no events written yet) or an unreachable store returns
    ``[]`` — the best-effort contract inherited from
    :func:`fulcra_coord.remote.list_json`.

    Args:
        task_id: The task whose event shards to retrieve.
        backend: Optional explicit backend command list.

    Returns:
        List of ``(remote_path, event_dict)`` pairs, unordered.
    """
    return remote.list_json(remote.events_prefix(task_id), backend=backend)
