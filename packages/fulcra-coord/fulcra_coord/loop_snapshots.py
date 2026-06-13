"""Read-side repair helpers for loop snapshots.

First-class directive records are a cache of the authoritative task plus the
loop sub-logs. A stale cache must not keep a completed review visible on the
board forever, so glance surfaces can overlay task-derived loop fields for
records that still appear open.
"""
from __future__ import annotations

from typing import Any, Optional

from . import directives, loops, remote


_TASK_DERIVED_FIELDS = (
    "directive_type",
    "from",
    "audience",
    "title",
    "workstream",
    "summary",
    "next_action",
    "priority",
    "status",
    "artifact_ref",
    "not_before",
    "due",
    "kind",
    "state",
    "expects_response",
    "sla_hours",
)


def overlay_open_records_from_tasks(
    records: list[dict[str, Any]],
    *,
    backend: Optional[list[str]] = None,
    tasks: Optional[list[dict[str, Any]]] = None,
    fetch_missing: bool = False,
) -> list[dict[str, Any]]:
    """Overlay task-derived fields onto records that still read as open.

    The authoritative task body owns assignment/status, while the directive
    snapshot owns durable loop sub-log folds such as ``acked_by``/``routing`` and
    ``outcome``. To keep the hot read surface cheap, only records that currently
    look open and have a task back-reference consult the supplied task list. If
    ``fetch_missing`` is true, missing tasks are downloaded one-by-one after the
    supplied task list misses; glance surfaces use that only for currently open
    snapshots, so the fallback is bounded to rows that could pollute the board.
    """
    task_map: Optional[dict[str, dict[str, Any]]] = None
    if tasks is not None:
        task_map = {
            task_id: task
            for task in tasks
            if isinstance(task, dict) and (task_id := task.get("id"))
        }

    out: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        task_id = record.get("task_id")
        if not task_id or not loops.is_open_loop(record):
            out.append(record)
            continue

        try:
            if task_map is not None:
                task = task_map.get(task_id)
                if task is None and fetch_missing:
                    task = remote.download_json(
                        remote.task_remote_path(task_id), backend=backend)
            elif fetch_missing:
                task = remote.download_json(
                    remote.task_remote_path(task_id), backend=backend)
            else:
                task = None
            if not isinstance(task, dict):
                out.append(record)
                continue
            expected = directives.directive_from_task(task)
        except Exception:
            out.append(record)
            continue

        merged = dict(record)
        for field in _TASK_DERIVED_FIELDS:
            if field in expected:
                merged[field] = expected[field]
        out.append(merged)
    return out
