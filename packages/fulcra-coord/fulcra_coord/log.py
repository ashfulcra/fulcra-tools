"""Local ops log writer for fulcra-coord."""

from __future__ import annotations

from typing import Any, Optional

from . import cache as _cache


def log_op(
    command: str,
    task_id: Optional[str] = None,
    status: str = "ok",
    detail: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    entry: dict[str, Any] = {
        "command": command,
        "status": status,
    }
    if task_id:
        entry["task_id"] = task_id
    if detail:
        entry["detail"] = detail
    if error:
        entry["error"] = error
    _cache.append_ops_log(entry)
