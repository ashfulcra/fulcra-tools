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

import concurrent.futures
import logging
from typing import Any, Optional

from . import remote

# Best-effort drop-detection signal (see ``read_events``). We use the stdlib
# ``logging`` module rather than the coord ops-log helper (``fulcra_coord.log``)
# on purpose: ``fulcra_coord.log`` imports ``cache``, and importing it here would
# pull a feature-layer dependency into this leaf-adjacent module — breaking the
# import boundary the ``test_layering_boundaries.py`` fitness test enforces
# (eventlog may import only remote / events / timeutil). stdlib ``logging`` has
# no such boundary cost, so it is the lightest signal that keeps eventlog a leaf.
log = logging.getLogger("fulcra_coord.eventlog")


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
) -> list[dict[str, Any]]:
    """Read all event envelopes for a task (unordered; ``fold_task`` orders them).

    Returns the event dicts only — the remote path of each shard is dropped
    here so callers can fold the result directly (``fold_task(read_events(...))``).
    A caller that needs the shard paths should use ``remote.list_json`` directly.

    Missing prefix (no events written yet) or an unreachable store returns
    ``[]`` — the best-effort contract callers (the parity check's "not yet
    dual-written" skip, the read cutover's file fallback) rely on.

    ONE LISTING serves both the read and the drop detection (PERF, 2026-06-10
    measured pass): this used to call ``remote.list_json`` (which lists the
    prefix internally) and then ``remote.list_files`` AGAIN for drop
    detection — two list subprocesses (~1.3s each) per task per reconcile
    tick, ~880 redundant spawns on a 440-task bus. The single ``list_files``
    below feeds the parallel shard downloads AND the listed-vs-parsed
    comparison.

    Drop detection (observability, non-invasive): a shard whose JSON fails to
    parse to a dict is silently dropped from the result. A dropped
    **snapshot** shard is especially dangerous — the fold would reconstruct
    STALE state from an older snapshot while ``fold_is_complete`` still
    returned True, a silent correctness hazard with zero signal. So we compare
    the count of ``.json`` paths the store *listed* against the count of
    records that actually *parsed*; if fewer parsed, we emit a best-effort
    warning naming the task and the drop count. This NEVER changes the return
    value (the good records are returned unchanged) and the warn itself is
    guarded so a logging failure can never break the read — it is a signal
    only.

    Args:
        task_id: The task whose event shards to retrieve.
        backend: Optional explicit backend command list.

    Returns:
        List of event dicts, unordered.
    """
    prefix = remote.events_prefix(task_id)
    try:
        paths = [p for p in remote.list_files(prefix, backend=backend)
                 if p.endswith(".json")]
    except Exception:
        # Unreachable store / broken listing reads as "no events" — the
        # best-effort contract above (mirrors the old list_json behaviour).
        return []
    if not paths:
        return []

    # Parallel shard downloads (the remote.list_json pool shape): each download
    # is an independent subprocess writing no shared state, so a small pool
    # collapses K serial round-trips into one batch's wall-time. Results keep
    # the listing's path order so the return is deterministic for a given bus.
    results: dict[str, dict[str, Any]] = {}
    workers = min(8, max(2, len(paths)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(remote.download_json, p, backend=backend): p
            for p in paths
        }
        for fut in concurrent.futures.as_completed(futures):
            path = futures[fut]
            try:
                rec = fut.result()
            except Exception:
                rec = None  # one failed shard must not break the read
            if isinstance(rec, dict):
                results[path] = rec
    records = [results[p] for p in paths if p in results]

    dropped = len(paths) - len(records)
    if dropped > 0:
        try:
            log.warning(
                "read_events(%s): %d of %d event shard(s) failed to parse and "
                "were dropped — fold may reconstruct stale/incomplete state.",
                task_id, dropped, len(paths),
            )
        except Exception:  # pragma: no cover — signal only, never breaks the read
            pass

    return records
