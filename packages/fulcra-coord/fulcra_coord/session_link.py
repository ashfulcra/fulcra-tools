"""Per-session current-task pointer.

The PreCompact/SessionEnd Claude Code hooks and the OpenClaw
shutdown/bootstrap handlers need to know which task the current session owns.
The CLI writes this pointer whenever it runs inside a session, so the hooks can
look it up with zero agent effort. Keyed on the session identifier so
concurrent sessions on one machine never clobber each other.

Two identifier sources, in precedence order:
  1. ``CLAUDE_CODE_SESSION_ID`` — Claude Code's native per-invocation id.
  2. ``FULCRA_COORD_SESSION_KEY`` — a generic fallback any non-Claude-Code
     agent can set. OpenClaw handlers pass through OpenClaw's stable
     ``sessionKey`` (the conversation bucket that survives compaction) here.

Claude Code takes precedence because it is the finer-grained native id; the
generic key is only consulted when Claude Code's is absent.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

from . import cache

# A session identifier may contain characters that are awkward in a filename
# (OpenClaw's sessionKey looks like "agent:<id>:<bucket>"). Normalize anything
# outside a safe set to "_" so the on-disk pointer name is portable. Both write
# and read run keys through this, so the mapping stays consistent.
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitize(key: str) -> str:
    return _SAFE.sub("_", key)


def _session_id() -> str:
    """Return the active session identifier, or "" outside any tracked session.

    CLAUDE_CODE_SESSION_ID wins; FULCRA_COORD_SESSION_KEY is the generic fallback
    for agents (OpenClaw, etc.) that don't set the Claude Code variable.
    """
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "").strip()
    if sid:
        return sid
    return os.environ.get("FULCRA_COORD_SESSION_KEY", "").strip()


def write_pointer(task_id: str, *, agent: str, root: str) -> bool:
    """Record task_id for the current session. No-op (returns False) outside a session."""
    sid = _session_id()
    if not sid:
        return False
    path = cache.sessions_dir() / f"{_sanitize(sid)}.json"
    path.write_text(json.dumps({"task_id": task_id, "agent": agent, "root": root}))
    return True


def read_pointer(session_id: str) -> Optional[dict[str, Any]]:
    path = cache.sessions_dir() / f"{_sanitize(session_id)}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (ValueError, OSError):
        return None


def clear_for_task(task_id: str) -> int:
    """Remove any session pointers that reference task_id.

    Called when a task transitions to a terminal status (done/abandoned): once a
    task is finished, its pointer is stale, and leaving it would make the
    PreCompact/SessionEnd hooks checkpoint a dead task on the next compaction or
    session end. Returns the number of pointers removed. Best-effort: unreadable
    or unremovable pointer files are skipped, never raised.
    """
    removed = 0
    try:
        entries = list(cache.sessions_dir().glob("*.json"))
    except OSError:
        return 0
    for p in entries:
        try:
            data = json.loads(p.read_text())
        except (ValueError, OSError):
            continue
        if data.get("task_id") == task_id:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed
