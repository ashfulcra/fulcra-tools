"""Structured continuity snapshots — the core of fulcra-agent-continuity.

Teams' ``member/<agent>/progress.md`` is freeform; this gives a *structured*,
resumable snapshot (objective / decisions / next actions / open questions /
artifacts) with a deterministic resume brief. Building the schema + folding many
snapshots to the latest is code; the prose is when/whether to snapshot.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

SCHEMA = "coord.teams.continuity.v1"


def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x) for x in v]
    return [str(v)]


def build_snapshot(
    *,
    agent: str,
    task: str,
    objective: str,
    now: str,
    decisions: Optional[list[str]] = None,
    next_actions: Optional[list[str]] = None,
    open_questions: Optional[list[str]] = None,
    artifacts: Optional[list[str]] = None,
    context_used_percent: Optional[float] = None,
    transcript_path: Optional[str] = None,
) -> dict[str, Any]:
    """A structured snapshot (all list fields normalized, never None)."""
    return {
        "schema": SCHEMA,
        "checkpoint_id": f"CHK-{now}-{task}",
        "agent": agent,
        "task": task,
        "objective": objective,
        "decisions": _as_list(decisions),
        "next_actions": _as_list(next_actions),
        "open_questions": _as_list(open_questions),
        "artifacts": _as_list(artifacts),
        "context_used_percent": context_used_percent,
        "transcript_path": transcript_path,
        "created_at": now,
    }


def _parseable_ts(ts: Any) -> bool:
    try:
        datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return True
    except (ValueError, TypeError):
        return False


def latest(snapshots: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Fold many snapshots to the newest by ``created_at`` (ISO sorts lexically).

    Malformed ``created_at`` values are IGNORED — lexical compare would otherwise
    let a corrupt snapshot (``not-a-date`` > ``2026-…``) shadow every valid one
    (Codex review finding). Ties break on ``checkpoint_id``."""
    valid = [s for s in snapshots
             if isinstance(s, dict) and _parseable_ts(s.get("created_at"))]
    if not valid:
        return None
    return max(valid, key=lambda s: (str(s.get("created_at")), str(s.get("checkpoint_id") or "")))


def render_resume(snapshot: Optional[dict[str, Any]]) -> str:
    """Deterministic resume brief from a snapshot (or a 'no snapshot' line)."""
    if not snapshot:
        return "No continuity snapshot found."
    lines = [
        f"Resume: {snapshot.get('task')} (as of {snapshot.get('created_at')})",
        f"  agent: {snapshot.get('agent')}",
        f"  objective: {snapshot.get('objective')}",
    ]
    cu = snapshot.get("context_used_percent")
    if cu is not None:
        lines.append(f"  context used at snapshot: {cu}%")
    for label, key in (
        ("next actions", "next_actions"),
        ("open questions", "open_questions"),
        ("recent decisions", "decisions"),
        ("artifacts", "artifacts"),
    ):
        items = snapshot.get(key) or []
        if items:
            lines.append(f"  {label}:")
            lines.extend(f"    - {x}" for x in items)
    return "\n".join(lines)
